"""
Database Manager - Handle all database connections and basic operations

Step 0 of the PG pivot (db_adapter_design.md, approved 2026-08-13): a
backend-agnostic seam with PERSISTENT connections replaces connect-per-query
(measured: 308k connect/close cycles per 240-month run ~= 41% of wall-clock).
The public API is unchanged — callers never see the backend.

The engine has two DB-usage shapes, so the backend holds TWO connections:
  - statement connection (autocommit): serves the four execute_* methods —
    per-statement commit boundaries, identical to the old one-connection-
    per-call behavior.
  - block connection (caller-managed transactions): serves get_connection()
    blocks — several statements committed by the caller's conn.commit(),
    ROLLED BACK if the block exits uncommitted (the old discard-on-close
    semantics). event_logger writes issued mid-block ride the statement
    connection, independently committed — exactly the old cross-connection
    behavior, preserving crash atomicity of the 17 direct-use blocks.

Engine SQL is qmark ('?') everywhere; a future PsycopgBackend translates at
execute time. Backend selection via db_config.json "backend" (default pyodbc).
"""
import pyodbc
import json
import logging
import os
import threading
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

CONFIG_FILE_PATH = os.getenv("ENV_CONFIG")

@dataclass
class DatabaseConfig:
    """Database connection configuration"""
    driver: str = ""            # pyodbc only
    server: str = "localhost"
    database: str = ""
    username: str = ""          # psycopg: the PG role (password via pgpass.conf)
    password: str = ""          # pyodbc SQL-auth only; NEVER set for psycopg
    trusted_connection: bool = False
    encrypt: bool = False
    backend: str = "pyodbc"     # "pyodbc" (SQL Server) | "psycopg" (PostgreSQL)
    port: int = 5432            # psycopg only


class PyodbcBackend:
    """Persistent two-connection pyodbc backend (SQL Server).

    Single-threaded per process by design (workers are separate OS
    processes) — enforced by a thread-ownership assert, not convention.
    On a connection-level error the affected connection is discarded and
    lazily re-established on the NEXT call; the original error re-raises —
    never a silent statement retry (a retried non-idempotent INSERT is a
    double-write; fail-loud, the crash-leaves-no-result-row design absorbs it).
    """

    _CONN_ERRORS = (pyodbc.OperationalError, pyodbc.InterfaceError)

    def __init__(self, connection_string: str, logger: logging.Logger):
        self.connection_string = connection_string
        self.logger = logger
        self._stmt_conn = None
        # Block connections are a FREE LIST, not a singleton: the engine nests
        # get_connection() blocks (found by the step-0 smoke at funds/staff
        # init), and pre-step-0 every block was its own pooled connection with
        # an INDEPENDENT transaction. Each active block therefore gets its own
        # persistent connection; depth in practice is 2 — the list is bounded
        # by real nesting depth and connections are reused across blocks.
        self._block_free: list = []
        self._block_depth = 0
        self._owner_thread = None

    # --- connection lifecycle ------------------------------------------------

    def _connect(self, autocommit: bool):
        try:
            conn = pyodbc.connect(self.connection_string, autocommit=autocommit)
        except pyodbc.Error as e:
            self.logger.error(f"Database connection failed: {e}")
            raise
        # Required for indexed views, computed columns, etc. Once per
        # (re)connect — was a round-trip on every query pre-step-0.
        cursor = conn.cursor()
        cursor.execute("SET QUOTED_IDENTIFIER ON")
        cursor.close()
        if not autocommit:
            conn.commit()  # the SET rode an implicit txn; start the block conn idle
        return conn

    def _check_thread(self):
        ident = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = ident
        elif ident != self._owner_thread:
            raise RuntimeError(
                "DatabaseManager is single-threaded per process (workers are "
                "separate OS processes); cross-thread DB calls are a design "
                "violation, not a race to win")

    def _stmt(self):
        self._check_thread()
        if self._stmt_conn is None:
            self._stmt_conn = self._connect(autocommit=True)
        return self._stmt_conn

    def _discard_stmt(self):
        if self._stmt_conn is not None:
            try:
                self._stmt_conn.close()
            except pyodbc.Error:
                pass
            self._stmt_conn = None

    @staticmethod
    def _close_quietly(conn):
        try:
            conn.close()
        except pyodbc.Error:
            pass

    def close_all(self):
        self._discard_stmt()
        for conn in self._block_free:
            self._close_quietly(conn)
        self._block_free = []

    # --- the two usage shapes ------------------------------------------------

    def run_statement(self, query: str, params: Optional[Tuple], fetch: str):
        """One statement on the autocommit connection.
        fetch: 'all' -> rows, 'rowcount' -> affected count,
        'all_committed' -> rows from a committing statement (INSERT..OUTPUT)."""
        try:
            cursor = self._stmt().cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            if fetch == "rowcount":
                result = cursor.rowcount
            else:
                result = cursor.fetchall()
            cursor.close()
            return result
        except self._CONN_ERRORS:
            self._discard_stmt()
            raise

    @contextmanager
    def block(self):
        """A caller-managed transaction block (the old get_connection()).
        The caller commits; exit without commit rolls back (old discard-on-
        close semantics). Nested blocks each get their OWN connection from the
        free list — independent transactions, as pre-step-0."""
        self._check_thread()
        conn = self._block_free.pop() if self._block_free else self._connect(autocommit=False)
        self._block_depth += 1
        try:
            yield conn
        except self._CONN_ERRORS:
            self._close_quietly(conn)
            conn = None
            raise
        finally:
            self._block_depth -= 1
            if conn is not None:
                try:
                    # discards anything uncommitted; harmless after a commit
                    conn.rollback()
                    self._block_free.append(conn)
                except pyodbc.Error:
                    self._close_quietly(conn)

    def run_batch(self, query: str, rows: List[Tuple]) -> int:
        """Atomic batched write (step 0.5's monthly buffers): one transaction,
        one commit, fast_executemany."""
        with self.block() as conn:
            cursor = conn.cursor()
            cursor.fast_executemany = True
            cursor.executemany(query, rows)
            affected = cursor.rowcount
            cursor.close()
            conn.commit()
            return affected


class _CIRow(tuple):
    """Tuple + case-insensitive attribute access — pyodbc Row compatibility.
    Engine code mixes row[0] indexing with row.ColumnName attribute access;
    PG folds unquoted column names to lowercase, so the attribute lookup maps
    through a lowercased name index."""

    def __new__(cls, values, names):
        obj = super().__new__(cls, values)
        obj._names = names
        return obj

    def __getattr__(self, name):
        try:
            return self[self._names[name.lower()]]
        except KeyError:
            raise AttributeError(name)


def _ci_row_factory(cursor):
    names = {d.name.lower(): i for i, d in enumerate(cursor.description or [])}

    def make(values):
        return _CIRow(values, names)

    return make


class PsycopgBackend:
    """Persistent two-connection psycopg backend (PostgreSQL).

    Mirrors PyodbcBackend's two shapes exactly (statement autocommit
    connection + block free-list — see that class). Engine SQL stays qmark
    and common-subset; this backend translates at execute time:
      - qmark -> %s, literal-aware (single-quoted segments untouched), cached
        per SQL string;
      - the ONE per-backend T-SQL fragment (compliance_manager per-row date
        arithmetic) mapped explicitly below — additions to this map require
        a pg_port_design.md note.
    Auth: the PG role's password comes from pgpass.conf ONLY — never from
    config files (2026-08-13 credential ruling).
    """

    _TSQL_FRAGMENTS = {
        # compliance_manager:~1192 — DATEADD over row columns has no
        # T-SQL/PG common subset (pg_port_design.md, documented exception)
        "DATEADD(day, DurationDays, StartDate)":
            "(StartDate + make_interval(days => DurationDays))",
        # compliance_manager's recursive work-item CTEs (2 sites): T-SQL
        # recurses with plain WITH; PG requires WITH RECURSIVE, which T-SQL
        # rejects — a true dialect fork (documented exception #2)
        "WITH descendants AS (":
            "WITH RECURSIVE descendants AS (",
    }
    _translated: dict = {}  # sql -> translated sql (shared cache)

    def __init__(self, config: DatabaseConfig, logger: logging.Logger):
        import psycopg  # lazy: pyodbc-only installs don't need it
        self._psycopg = psycopg
        self._conn_errors = (psycopg.OperationalError, psycopg.InterfaceError)
        self.config = config
        self.logger = logger
        self._stmt_conn = None
        self._block_free: list = []
        self._block_depth = 0
        self._owner_thread = None

    @staticmethod
    def _qmark_to_pyformat(sql: str) -> str:
        """? -> %s outside string literals AND outside -- line comments.
        (A quote-parity split failed on an apostrophe INSIDE a comment —
        rent_collection's "can't" swallowed five placeholders.) Handles ''
        escapes inside literals."""
        out = []
        i, n = 0, len(sql)
        NORMAL, STRING, COMMENT = 0, 1, 2
        state = NORMAL
        while i < n:
            c = sql[i]
            if c == "%":
                # literal % (LIKE patterns etc.) must escape to %% — psycopg
                # parses %-sequences wherever they appear; the backend always
                # passes a params sequence so %% consistently unescapes
                out.append("%%")
                i += 1
                continue
            if state == NORMAL:
                if c == "?":
                    out.append("%s")
                    i += 1
                    continue
                out.append(c)
                if c == "'":
                    state = STRING
                elif c == "-" and sql[i:i + 2] == "--":
                    state = COMMENT
            elif state == STRING:
                out.append(c)
                if c == "'":
                    if sql[i + 1:i + 2] == "'":
                        out.append("'")
                        i += 2
                        continue
                    state = NORMAL
            else:  # COMMENT — runs to end of line
                out.append(c)
                if c == "\n":
                    state = NORMAL
            i += 1
        return "".join(out)

    @classmethod
    def translate(cls, sql: str) -> str:
        pg_sql = cls._translated.get(sql)
        if pg_sql is None:
            work = sql
            for frag, repl in cls._TSQL_FRAGMENTS.items():
                work = work.replace(frag, repl)
            pg_sql = cls._qmark_to_pyformat(work)
            cls._translated[sql] = pg_sql
        return pg_sql

    def _connect(self, autocommit: bool):
        try:
            return self._psycopg.connect(
                host=self.config.server, port=self.config.port,
                dbname=self.config.database, user=self.config.username,
                autocommit=autocommit, row_factory=_ci_row_factory)
        except self._psycopg.Error as e:
            self.logger.error(f"Database connection failed: {e}")
            raise

    def _check_thread(self):
        ident = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = ident
        elif ident != self._owner_thread:
            raise RuntimeError(
                "DatabaseManager is single-threaded per process (workers are "
                "separate OS processes); cross-thread DB calls are a design "
                "violation, not a race to win")

    def _stmt(self):
        self._check_thread()
        if self._stmt_conn is None:
            self._stmt_conn = self._connect(autocommit=True)
        return self._stmt_conn

    def _discard_stmt(self):
        if self._stmt_conn is not None:
            try:
                self._stmt_conn.close()
            except self._psycopg.Error:
                pass
            self._stmt_conn = None

    def _close_quietly(self, conn):
        try:
            conn.close()
        except self._psycopg.Error:
            pass

    def close_all(self):
        self._discard_stmt()
        for conn in self._block_free:
            self._close_quietly(conn)
        self._block_free = []

    @staticmethod
    def _norm_params(params):
        """pyodbc accepts a bare scalar parameter; psycopg needs a sequence.
        Python bools map to int — bit columns are smallint on PG (TYPE_MAP
        note in pg_schema_cut.py), and psycopg would bind bool as boolean."""
        if params is None:
            return None
        if not isinstance(params, (tuple, list)):
            params = (params,)
        return tuple(int(v) if isinstance(v, bool) else v for v in params)

    def run_statement(self, query: str, params: Optional[Tuple], fetch: str):
        try:
            cursor = self._stmt().cursor()
            # always a sequence (never None) — translate escapes literal %
            # to %%, which psycopg only unescapes when params are processed
            cursor.execute(self.translate(query), self._norm_params(params) or ())
            if fetch == "rowcount":
                result = cursor.rowcount
            else:
                result = cursor.fetchall()
            cursor.close()
            return result
        except self._conn_errors:
            self._discard_stmt()
            raise

    @contextmanager
    def block(self):
        self._check_thread()
        conn = self._block_free.pop() if self._block_free else self._connect(autocommit=False)
        self._block_depth += 1
        try:
            yield _PsycopgBlockConn(conn, self.translate)
        except self._conn_errors:
            self._close_quietly(conn)
            conn = None
            raise
        finally:
            self._block_depth -= 1
            if conn is not None:
                try:
                    conn.rollback()
                    self._block_free.append(conn)
                except self._psycopg.Error:
                    self._close_quietly(conn)

    def run_batch(self, query: str, rows: List[Tuple]) -> int:
        with self.block() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                self.translate(query),
                [tuple(int(v) if isinstance(v, bool) else v for v in row)
                 for row in rows])
            affected = cursor.rowcount
            conn.commit()
            return affected


class _PsycopgBlockConn:
    """Wraps a psycopg connection for get_connection() blocks so the direct-
    use callers' pyodbc-idiom code (conn.cursor().execute('... ? ...'),
    conn.commit()) runs unchanged: cursors translate qmark SQL at execute."""

    def __init__(self, conn, translate):
        self._conn = conn
        self._translate = translate

    def cursor(self):
        return _PsycopgBlockCursor(self._conn.cursor(), self._translate)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


class _PsycopgBlockCursor:
    def __init__(self, cursor, translate):
        self._cursor = cursor
        self._translate = translate

    def execute(self, sql, *params):
        # pyodbc accepts all three idioms: (sql, (p1, p2)), (sql, scalar),
        # and (sql, p1, p2, ...) varargs — normalize to a sequence; bools
        # map to int (bit columns are smallint on PG)
        if len(params) == 1 and isinstance(params[0], (tuple, list)):
            args = params[0]
        else:
            args = params
        args = tuple(int(v) if isinstance(v, bool) else v for v in args)
        self._cursor.execute(self._translate(sql), args)
        return self

    def executemany(self, sql, rows):
        return self._cursor.executemany(self._translate(sql), rows)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class DatabaseManager:
    """Centralized database connection and operations management"""

    def __init__(self, config_file: str):
        self.config = self._load_config(config_file)
        self.connection_string = self._build_connection_string()
        self.logger = logging.getLogger(__name__)
        self.event_logger = None
        self.debug_tracer = None
        if self.config.backend == "pyodbc":
            self._backend = PyodbcBackend(self.connection_string, self.logger)
        elif self.config.backend == "psycopg":
            if self.config.password:
                raise ValueError(
                    "psycopg configs must not carry a password — auth rides "
                    "pgpass.conf (2026-08-13 credential ruling)")
            self._backend = PsycopgBackend(self.config, self.logger)
        else:
            raise ValueError(
                f"unknown db backend '{self.config.backend}' — "
                f"'pyodbc' or 'psycopg' (db_adapter_design.md)")

    def set_debug_tracer(self, tracer) -> None:
        """Wire up a DebugTracer to log every DB operation."""
        self.debug_tracer = tracer

    def _log_db(self, method: str, sql: str, params, result) -> None:
        if self.debug_tracer:
            rows = result if isinstance(result, int) else (len(result) if result is not None else None)
            self.debug_tracer.log_db_call(method, sql, params, rows)

    def _load_config(self, config_file: str) -> DatabaseConfig:
        """Load database configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                config_data = json.load(f)
            return DatabaseConfig(**config_data)
        except FileNotFoundError:
            raise FileNotFoundError(f"Database config file '{config_file}' not found")
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in config file '{config_file}'")

    def _build_connection_string(self) -> str:
        """Build pyodbc connection string from config"""
        if self.config.trusted_connection:
            return (f"DRIVER={self.config.driver};"
                   f"SERVER={self.config.server};"
                   f"DATABASE={self.config.database};"
                   f"Trusted_Connection=yes;")
        else:
            return (f"DRIVER={self.config.driver};"
                   f"SERVER={self.config.server};"
                   f"DATABASE={self.config.database};"
                   f"UID={self.config.username};"
                   f"PWD={self.config.password};")

    def get_connection(self):
        """Context manager for a caller-managed transaction block on the
        persistent block connection. The caller's conn.commit() works as
        before; exiting without commit rolls back (the old discard-on-close
        semantics). Do not nest."""
        return self._backend.block()

    def close(self):
        """Close both persistent connections (clean shutdown; optional)."""
        self._backend.close_all()

    def execute_query(self, query: str, params: Optional[Tuple] = None) -> List[Tuple]:
        """Execute a SELECT query and return results"""
        results = self._backend.run_statement(query, params, fetch="all")
        self._log_db('execute_query', query, params, results)
        return results

    def execute_non_query(self, query: str, params: Optional[Tuple] = None) -> int:
        """Execute INSERT/UPDATE/DELETE and return affected rows"""
        affected_rows = self._backend.run_statement(query, params, fetch="rowcount")
        self._log_db('execute_non_query', query, params, affected_rows)
        return affected_rows

    def execute_insert_returning(self, query: str, params: Optional[Tuple] = None) -> List[Tuple]:
        """Execute INSERT with OUTPUT clause, returning results"""
        results = self._backend.run_statement(query, params, fetch="all_committed")
        self._log_db('execute_insert_returning', query, params, results)
        return results

    def execute_scalar(self, query: str, params: Optional[Tuple] = None) -> Any:
        """Execute query and return single value"""
        results = self.execute_query(query, params)
        if results:
            return results[0][0]
        return None

    def executemany(self, query: str, rows: List[Tuple]) -> int:
        """Atomic batched write — one transaction, one commit (step 0.5)."""
        affected = self._backend.run_batch(query, rows)
        self._log_db('executemany', query, f"<{len(rows)} rows>", affected)
        return affected
