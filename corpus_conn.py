"""Shared corpus-harness connection layer (D3, pg_corpus_harness_design.md).

Consolidates the three drifting connection-string copies (create_corpus,
corpus_runner, reproduction_gate) behind one backend-selected `connect()`:

    LB_CORPUS_BACKEND = psycopg (default) | pyodbc
    (or programmatically: corpus_conn.set_backend("psycopg") — the CLIs'
    --pg flags call this, overriding the environment)

The psycopg path wraps connections/cursors so the harness's pyodbc idioms run
unchanged: qmark placeholders (string- and comment-aware translation — the
scanner mirrors app/src/database_manager.PsycopgBackend, housed here because
the harness ships standalone), bare-scalar/varargs params, bool->int binds.
Auth: SQL Server = trusted connection; PG = pgpass.conf ONLY (the 2026-08-13
credential ruling — no password fields anywhere).

Also carries the two date-arithmetic snippet builders the extract needs —
T-SQL DATEDIFF has keyword-style dateparts and cannot be shimmed as a PG
function (unlike YEAR()/MONTH()/DAY(), which the engine schema shims).
"""
import os

SQL_SERVER = os.environ.get("LB_SQL_SERVER", "localhost")
SQL_DRIVER = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
CONN_TMPL = ("DRIVER={{" + SQL_DRIVER + "}};SERVER=" + SQL_SERVER
             + ";Trusted_Connection=yes;DATABASE={}")

PG_HOST = os.environ.get("LB_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("LB_PG_PORT", "5432"))
PG_USER = os.environ.get("LB_PG_USER", "libertybee")

_backend = os.environ.get("LB_CORPUS_BACKEND", "psycopg")


def set_backend(name: str) -> None:
    global _backend
    if name not in ("pyodbc", "psycopg"):
        raise ValueError(f"unknown corpus backend {name!r}")
    _backend = name


def backend() -> str:
    return _backend


def is_pg() -> bool:
    return _backend == "psycopg"


# --- qmark translation (mirrors PsycopgBackend._qmark_to_pyformat) ----------

_translated: dict = {}


def _qmark_to_pyformat(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    NORMAL, STRING, COMMENT = 0, 1, 2
    state = NORMAL
    while i < n:
        c = sql[i]
        if c == "%":
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
        else:
            out.append(c)
            if c == "\n":
                state = NORMAL
        i += 1
    return "".join(out)


def _translate(sql: str) -> str:
    pg_sql = _translated.get(sql)
    if pg_sql is None:
        pg_sql = _qmark_to_pyformat(sql)
        _translated[sql] = pg_sql
    return pg_sql


def _norm_params(params):
    if not params:
        return ()
    if len(params) == 1 and isinstance(params[0], (tuple, list)):
        params = params[0]
    return tuple(int(v) if isinstance(v, bool) else v for v in params)


class _CIRow(tuple):
    """Tuple + case-insensitive attribute access — pyodbc Row compatibility
    (harness code reads r.Span / r.PropertyCount; PG folds column names to
    lowercase). Mirrors app/src/database_manager._CIRow."""

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


class _PgCursor:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, *params):
        self._c.execute(_translate(sql), _norm_params(params))
        return self

    def executemany(self, sql, rows):
        return self._c.executemany(
            _translate(sql),
            [tuple(int(v) if isinstance(v, bool) else v for v in r) for r in rows])

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def fetchmany(self, n):
        return self._c.fetchmany(n)

    def close(self):
        self._c.close()

    @property
    def rowcount(self):
        return self._c.rowcount


class _PgConn:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def execute(self, sql, *params):
        return self.cursor().execute(sql, *params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def connect(db_name: str, autocommit: bool = True, timeout: int = 30):
    """One connection API, both engines. Returns a pyodbc connection or a
    qmark-wrapped psycopg connection — the harness code is identical either
    way."""
    if is_pg():
        import psycopg
        return _PgConn(psycopg.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=db_name,
            autocommit=autocommit, connect_timeout=timeout,
            row_factory=_ci_row_factory))
    import pyodbc
    return pyodbc.connect(CONN_TMPL.format(db_name), timeout=timeout,
                          autocommit=autocommit)


def integrity_errors():
    """The engine-appropriate unique/FK-violation exception classes."""
    if is_pg():
        import psycopg
        return (psycopg.errors.UniqueViolation, psycopg.errors.IntegrityError)
    import pyodbc
    return (pyodbc.IntegrityError,)


def db_errors():
    """The engine-appropriate base database-error class (broad catches, e.g.
    a query against a table that may not exist)."""
    if is_pg():
        import psycopg
        return (psycopg.Error,)
    import pyodbc
    return (pyodbc.Error,)


# --- date-arithmetic snippets (no T-SQL/PG common subset exists) ------------

def month_diff_sql(a: str, b: str) -> str:
    """Months from a to b — T-SQL DATEDIFF(MONTH, …) month-BOUNDARY
    semantics on both engines."""
    if is_pg():
        return (f"((EXTRACT(YEAR FROM {b}) - EXTRACT(YEAR FROM {a})) * 12 "
                f"+ (EXTRACT(MONTH FROM {b}) - EXTRACT(MONTH FROM {a})))")
    return f"DATEDIFF(MONTH, {a}, {b})"


def day_diff_sql(a: str, b: str) -> str:
    """Days from a to b — T-SQL DATEDIFF(DAY, …) semantics (date-boundary
    count; both operands are DATEs in the extract, so plain subtraction is
    exact on PG)."""
    if is_pg():
        return f"({b} - {a})"
    return f"DATEDIFF(DAY, {a}, {b})"
