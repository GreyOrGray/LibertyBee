"""
====================================================================================
Liberty Bee Migration Manager
====================================================================================

This script manages Liberty Bee SQL Server environments. It provides a consistent,
idempotent way to:

  • Ensure an environment folder exists under:
        C:\\LibertyBee\\environments\\<envname>

  • Ensure a SQL Server database exists with the same name as the environment
        (created automatically if missing).

  • Connect via Windows trusted auth (the script runs as the invoking account),
        so no SQL login/password is stored or required.

  • Generate or update the db_config.json file inside the environment folder so
        the application and simulation engine can connect correctly.

  • Ensure a virtual environment exists in each environment folder and install
        Python dependencies from app/requirements.txt on first creation.

  • Apply all SQL migration scripts located in:
        <repo_root>/sql/migrations/
    using SchemaVersion tracking so migrations are only applied once.

  • Create ephemeral test environments with unique database names when no
        --envname is supplied.

USAGE
-----

1. To RESET a named ephemeral environment back to Gold + all migrations:

       python migration_manager.py --envname LibertyBee_Test_MyTest

   ⚠ DESTRUCTIVE (contract corrected 2026-07-03, audit F-05): this restores
   the named DB from the Gold backup (RESTORE WITH REPLACE) and re-applies
   every migration — it does NOT additively "update." It refuses any name
   outside the LibertyBee_Test_* namespace (production/baseline DBs are
   never restored by this script). It will:
       • Restore the DB from the Gold backup (wiping current contents)
       • Create the environment folder / db_config.json if missing
       • Ensure a venv exists and install requirements on first run
       • Run all migrations, then re-stamp dbo._provenance
   (The retired [Cate] login setup was removed 2026-06-13.)

2. To create a fully isolated TEST environment with a unique test database:

       python migration_manager.py

   This will:
       • Create a new test database named:
             LibertyBee_Test_<NNN>[_<label>]  (incrementing id, optional purpose label)
       • Create a matching environment folder
       • Build db_config.json
       • Run all schema + seed migrations from sql/migrations/
       • Print the environment name to stdout

3. To create a test environment with a human-readable label in the name:

       python migration_manager.py --label Phase_3_7_6

   This will:
       • Create a new test database named:
             LibertyBee_Test_Phase_3_7_6_<datetime>
       • Easier to identify what phase/feature the environment was created for
       • Sortable by name — most recent at the bottom
       • Label is sanitized: spaces → underscores, special chars stripped, max 50 chars

4. To test with scratch migrations (migrations from scratch/sql/migrations/):

       python migration_manager.py --scratch
       python migration_manager.py --label Phase_3_7_6 --scratch

   This will:
       • Create a new test database and apply migrations from scratch/sql/migrations/
       • Useful for testing new migrations before promoting to sql/migrations/

   You can also use --scratch with --envname:

       python migration_manager.py --envname LibertyBee_Test_MyTest --scratch

Folder/venv/db_config creation is idempotent. The DATABASE itself is reset
to Gold on every --envname run and on every fresh create — treat ephemeral
DB contents as disposable by definition.
"""

import argparse
import json
import re
import subprocess
import sys
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pyodbc



# -------------------------------------------------------
# CONFIG
# -------------------------------------------------------

# SQL Server connection — Windows trusted auth (no stored secret; the script runs
# as whoever invokes it). Driver/server default to a stock local install and can be
# overridden for a different ODBC driver version or a named instance via env vars:
#   set LB_SQL_DRIVER=ODBC Driver 18 for SQL Server
#   set LB_SQL_SERVER=localhost\SQLEXPRESS
SQL_DRIVER = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
SQL_SERVER = os.environ.get("LB_SQL_SERVER", "localhost")

# Base connection string WITHOUT a specific database.
SERVER_CONN_STR = (
    f"DRIVER={{{SQL_DRIVER}}};"
    f"SERVER={SQL_SERVER};"
    "Trusted_Connection=yes;"
    "Encrypt=no;"
)

# Figure out repo root based on where this script lives
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Audit F-05: the only namespace this script may destructively restore/drop.
EPHEMERAL_PREFIX = "LibertyBee_Test_"

# Root path for environment folders (relative to repo root)
ENV_ROOT = REPO_ROOT / "environments"

# Migrations
MIGRATIONS_DIR = REPO_ROOT / "sql" / "migrations"
SCRATCH_MIGRATIONS_DIR = REPO_ROOT / "scratch" / "sql" / "migrations"

# Prefix for test DBs
TEST_DB_PREFIX = "LibertyBee_Test"

# Gold seed-database backup location. Portable default (repo-relative, gitignored under
# DBBackup/); override with the LB_GOLD_BACKUP_DIR env var. The Gold .bak ships as a
# GitHub Release asset (tag "gold-baseline") — download it into this folder first; see SETUP.md.
DEFAULT_GOLD_BACKUP_DIR = Path(os.environ.get(
    "LB_BASELINE_BACKUP_DIR",
    os.environ.get("LB_GOLD_BACKUP_DIR", str(REPO_ROOT / "DBBackup" / "gold"))))

# Set by --baseline. A restore source is not always Gold: a Silver cut
# (environmentscripts/cut_silver.py) is a baseline candidate that sweeps restore
# from before it is promoted. LB_GOLD_BACKUP_DIR still works, but its name lies
# about what the directory holds, so LB_BASELINE_BACKUP_DIR and --baseline say
# what is actually meant.
_baseline_dir_override = None


def baseline_backup_dir() -> Path:
    """The directory to restore a baseline from: --baseline wins, then the
    environment, then DBBackup/gold."""
    return Path(_baseline_dir_override) if _baseline_dir_override else DEFAULT_GOLD_BACKUP_DIR

# -------------------------------------------------------
# DB helpers
# -------------------------------------------------------



def _get_default_data_log_paths(cur) -> tuple[str, str]:
    """
    Try to get instance default data/log paths. Fallback to master file directory.
    Returns (data_dir, log_dir) with trailing backslash removed.
    """
    # SQL Server 2012+ usually supports these
    cur.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS NVARCHAR(4000)), "
                "CAST(SERVERPROPERTY('InstanceDefaultLogPath')  AS NVARCHAR(4000));")
    data_dir, log_dir = cur.fetchone()
    if data_dir and log_dir:
        return (str(data_dir).rstrip("\\/"), str(log_dir).rstrip("\\/"))

    # Fallback: use master file locations
    cur.execute("""
        SELECT
            LEFT(physical_name, LEN(physical_name) - CHARINDEX('\\', REVERSE(physical_name)) + 1) AS dir_path
        FROM master.sys.master_files
        WHERE database_id = 1 AND file_id IN (1, 2)
        ORDER BY file_id;
    """)
    rows = cur.fetchall()
    # row0 = data-ish, row1 = log-ish (usually)
    data_dir = str(rows[0][0]).rstrip("\\/")
    log_dir  = str(rows[1][0]).rstrip("\\/")
    return (data_dir, log_dir)

def _find_latest_bak(backup_dir: Path) -> Path:
    hint = (
        "\n  -> Get the Gold seed DB from the release:\n"
        "     gh release download gold-baseline --repo GreyOrGray/LibertyBeeDev "
        f"--pattern '*.bak' --dir \"{backup_dir}\"\n"
        "  (or set LB_GOLD_BACKUP_DIR to a folder that contains the .bak). See SETUP.md."
    )
    if not backup_dir.exists():
        raise FileNotFoundError(f"Gold backup directory not found: {backup_dir}{hint}")
    baks = sorted(backup_dir.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not baks:
        raise FileNotFoundError(f"No Gold .bak found in: {backup_dir}{hint}")
    return baks[0]

def _db_state(cur, db_name: str) -> str:
    cur.execute("SELECT state_desc FROM sys.databases WHERE name = ?", db_name)
    row = cur.fetchone()
    return row[0] if row else "MISSING"

def restore_db_from_backup(
    target_db: str,
    backup_dir: Path = None,
    source_db_label: str = "LibertyBeeGold",
) -> None:
    """
    Restores the most recent .bak from backup_dir into target_db.
    Uses REPLACE and MOVEs files to instance default data/log directories.

    backup_dir defaults to None and is resolved at CALL time via
    baseline_backup_dir(), so --baseline / LB_BASELINE_BACKUP_DIR are honoured.
    Binding the default at definition time would freeze it at import, before the
    CLI has been parsed. Logical filenames are read from the backup itself, so the
    source database's name does not matter — a Silver cut restores as readily as
    Gold.
    """
    if backup_dir is None:
        backup_dir = baseline_backup_dir()
    latest_bak = _find_latest_bak(backup_dir)

    with connect_to_server() as conn:
        cur = conn.cursor()

        # Determine default data/log paths for MOVE targets
        data_dir, log_dir = _get_default_data_log_paths(cur)

        # Read logical file names from the backup
        cur.execute(f"RESTORE FILELISTONLY FROM DISK = ?", str(latest_bak))
        filelist = cur.fetchall()
        # Columns include: LogicalName, PhysicalName, Type (D/L)
        # pyodbc returns tuples; LogicalName index 0, Type index typically 2 (but safer by name not available)
        # We'll assume RESTORE FILELISTONLY classic ordering:
        # 0 LogicalName, 1 PhysicalName, 2 Type
        logical_data = [r for r in filelist if str(r[2]).upper() == "D"]
        logical_log  = [r for r in filelist if str(r[2]).upper() == "L"]
        if not logical_data or not logical_log:
            raise RuntimeError(f"Could not detect data/log logical names from backup: {latest_bak}")

        data_logical = str(logical_data[0][0])
        log_logical  = str(logical_log[0][0])

        mdf_path = str(Path(data_dir) / f"{target_db}.mdf")
        ldf_path = str(Path(log_dir)  / f"{target_db}_log.ldf")

        print(f"Restoring [{target_db}] from: {latest_bak}")
        print(f"  MOVE data: {data_logical} -> {mdf_path}")
        print(f"  MOVE log : {log_logical}  -> {ldf_path}")

        # If target exists, force disconnects
        cur.execute(f"""
            IF DB_ID(?) IS NOT NULL
            BEGIN
                ALTER DATABASE [{target_db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
            END
        """, target_db)

        # Restore
        cur.execute(f"""
            RESTORE DATABASE [{target_db}]
            FROM DISK = ?
            WITH
                REPLACE,
                MOVE ? TO ?,
                MOVE ? TO ?,
                RECOVERY;
        """, str(latest_bak), data_logical, mdf_path, log_logical, ldf_path)
        
        time.sleep(2)
        
        with connect_to_server() as conn1:
            cur1 = conn1.cursor()
            state = _db_state(cur1, target_db)
            while state != "ONLINE":
                time.sleep(2)
                state = _db_state(cur1, target_db)
            
        # Back to multi-user
        cur.execute(f"ALTER DATABASE [{target_db}] SET MULTI_USER;")

        print(f"Restore complete: [{target_db}]")


def _format_pyodbc_message(msg):
    """
    pyodbc cursor.messages can vary by driver/version.
    Common shapes:
      1) ((sqlstate, native_code), text)
      2) ('[01000] (0)', text)
      3) (sqlstate, native_code, text)
      4) weird/driver-specific tuples

    Goal: never lose info. If formatted text is empty, fall back to raw.
    """
    try:
        # Case: (sqlstate, native, text)
        if isinstance(msg, tuple) and len(msg) == 3:
            sqlstate, native, text = msg
            text = "" if text is None else str(text)
            prefix = f"[{sqlstate}] ({native})"
            return f"{prefix} {text}".strip() if text.strip() else f"{prefix} RAW={repr(msg)}"

        # Case: (header, text)
        if isinstance(msg, tuple) and len(msg) == 2:
            header, text = msg
            text = "" if text is None else str(text)

            # header can be ('01000', 0) or '[01000] (0)' or '01000'
            if isinstance(header, tuple) and len(header) == 2:
                sqlstate, native = header
                prefix = f"[{sqlstate}] ({native})"
                if text.strip():
                    return f"{prefix} {text}".strip()
                # if text empty, do NOT lose header; include raw too
                return f"{prefix} RAW={repr(msg)}"

            # header is already a string-ish value
            header_str = "" if header is None else str(header)
            if text.strip():
                return f"{header_str} {text}".strip()

            # text empty: header might already contain the message; if not, include raw
            return header_str.strip() if header_str.strip() else f"RAW={repr(msg)}"

        # Anything else
        return f"RAW={repr(msg)}"
    except Exception:
        return f"RAW={repr(msg)}"

    
def connect_to_server():
    """Connect to SQL Server at the server level (no DB)."""
    return pyodbc.connect(SERVER_CONN_STR, autocommit=True)


def connect_to_db(db_name: str):
    """Connect to a specific database."""
    conn_str = SERVER_CONN_STR + f"DATABASE={db_name};"
    return pyodbc.connect(conn_str, autocommit=True)


def database_exists(db_name: str) -> bool:
    """Return True if the database exists on the server."""
    with connect_to_server() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DB_ID(?)", db_name)
        return cur.fetchone()[0] is not None


def ensure_database(db_name: str) -> None:
    """Ensure the database exists (create if missing).

    Trusted Windows auth means the invoking account already has rights inside the
    database, so no separate SQL db-user grant is needed (the legacy [Cate] login
    was retired 2026-06-13)."""
    with connect_to_server() as conn:
        cur = conn.cursor()
        if not database_exists(db_name):
            print(f"Database [{db_name}] does not exist. Creating...")
            cur.execute(f"CREATE DATABASE [{db_name}];")
            print(f"Created database [{db_name}]")
        else:
            print(f"Database [{db_name}] already exists.")


# -------------------------------------------------------
# Environment folder + venv helpers
# -------------------------------------------------------


def ensure_environment_folder(envname: str) -> Path:
    """Ensure the environment folder + db_config.json exist for envname."""
    env_path = ENV_ROOT / envname
    env_path.mkdir(parents=True, exist_ok=True)

    # Ensure db_config.json exists
    config_path = env_path / "db_config.json"
    if not config_path.exists():
        db_config = {
            "driver": SQL_DRIVER,
            "server": SQL_SERVER,
            "database": envname,
            # Trusted (Windows) auth — no stored secret. DatabaseConfig requires
            # these keys (and lives behind the file_guard hard-block), so keep
            # them present but empty; they are ignored when trusted_connection is
            # True. (X.4.5 Step 0, 2026-06-11.)
            "username": "",
            "password": "",
            "trusted_connection": True,
            "encrypt": False,
        }
        config_path.write_text(json.dumps(db_config, indent=2), encoding="utf-8")
        print(f"Created db_config.json for [{envname}]")
    else:
        print(f"db_config.json already exists for [{envname}]")

    return env_path


# -------------------------------------------------------
# Logging helpers
# -------------------------------------------------------


def setup_logging(env_path: Path, db_name: str) -> logging.Logger:
    """
    Setup dual logging (console + file) for migration operations.

    Args:
        env_path: Path to environment folder
        db_name: Database name for log filename

    Returns:
        Configured logger instance
    """
    # Create log filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"migration_{timestamp}.log"
    log_path = env_path / log_filename

    # Create logger
    logger = logging.getLogger('migration_manager')
    logger.setLevel(logging.DEBUG)

    # Remove any existing handlers (in case this is called multiple times)
    logger.handlers.clear()

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    simple_formatter = logging.Formatter('%(message)s')

    # Console handler (INFO level, simple format)
    # Use UTF-8 encoding for console to handle special characters
    import io
    console_stream = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding='utf-8',
        errors='replace',  # Replace encoding errors instead of crashing
        line_buffering=True
    )
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    logger.addHandler(console_handler)

    # File handler (DEBUG level, detailed format)
    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(detailed_formatter)
    logger.addHandler(file_handler)

    logger.info(f"Migration log started for database: {db_name}")
    logger.info(f"Log file: {log_path}")
    logger.info("=" * 80)

    return logger


# -------------------------------------------------------
# Migration helpers
# -------------------------------------------------------


def ensure_schema_version_table(cur) -> None:
    """Create SchemaVersion table if it doesn't exist."""
    cur.execute(
        """
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables WHERE name = 'SchemaVersion' AND schema_id = SCHEMA_ID('dbo')
        )
        BEGIN
            CREATE TABLE dbo.SchemaVersion (
                Version      NVARCHAR(50) NOT NULL PRIMARY KEY,
                Description  NVARCHAR(255) NULL,
                ScriptPath   NVARCHAR(260) NULL,
                AppliedAt    DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
                AppliedBy    NVARCHAR(128) NOT NULL DEFAULT SUSER_SNAME()
            );
        END
        """
    )


def get_applied_versions(cur) -> set[str]:
    """Return set of migration versions already applied in this DB."""
    ensure_schema_version_table(cur)
    cur.execute("SELECT Version FROM dbo.SchemaVersion;")
    return {row[0] for row in cur.fetchall()}


def split_sql_batches(sql_text: str) -> list[str]:
    """Split SQL into batches separated by lines that are just GO (case-insensitive)."""
    lines = sql_text.splitlines()
    batches: list[str] = []
    current: list[str] = []

    for line in lines:
        # Match lines like: GO,   go, GO   -- comment
        if re.match(r"^\s*GO\s*(?:--.*)?$", line, flags=re.IGNORECASE):
            if current:
                batches.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)

    if current:
        batches.append("\n".join(current).strip())

    return [b for b in batches if b]


def _drain_cursor_messages(cur) -> list[object]:
    """Return and clear any informational messages emitted by SQL Server.

    NOTE: Do NOT coerce/flatten messages here. pyodbc message tuple shapes vary
    by driver/version; flattening can drop the actual message text.
    """
    raw_msgs = getattr(cur, "messages", None)
    if not raw_msgs:
        return []

    # Copy then clear best-effort
    drained = list(raw_msgs)
    try:
        raw_msgs.clear()  # type: ignore[attr-defined]
    except Exception:
        try:
            cur.messages = []  # type: ignore[attr-defined]
        except Exception:
            pass

    return drained


def _message_to_text(msg: object) -> str:
    """Best-effort plain-text extraction from a pyodbc cursor.messages entry."""
    try:
        # Common shapes:
        #   ((sqlstate, native_code), text)
        #   ('[01000] (0)', text)
        #   (sqlstate, native_code, text)
        if isinstance(msg, tuple):
            if len(msg) == 3:
                # (sqlstate, native, text)
                return str(msg[2] or "")
            if len(msg) == 2:
                # (header, text)
                return str(msg[1] or "")
        return str(msg or "")
    except Exception:
        return ""



_ERROR_MESSAGE_PATTERNS = [
    re.compile(r"\bMsg\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bLevel\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bis being referenced by table\b", re.IGNORECASE),
    re.compile(r"\bThe constraint\b", re.IGNORECASE),
    re.compile(r"\bFOREIGN KEY constraint\b", re.IGNORECASE),
]


def _messages_indicate_error(messages: list[str]) -> tuple[bool, list[str]]:
    """Heuristic: SQL Server sometimes emits 'errors' as messages; treat those as fatal."""
    if not messages:
        return (False, [])

    flagged: list[str] = []
    for m in messages:
        m_str = (m or "").strip()
        if not m_str:
            continue

        # If it includes "Msg N, Level L" and L >= 11, treat as error
        msg_match = re.search(r"\bMsg\s+\d+\b.*?\bLevel\s+(\d+)\b", m_str, flags=re.IGNORECASE)
        if msg_match:
            try:
                level = int(msg_match.group(1))
                if level >= 11:
                    flagged.append(m_str)
                    continue
            except Exception:
                flagged.append(m_str)
                continue

        lowered = m_str.lower()

        # Constraint/foreign-key dependency failures commonly show up like this in messages
        if ("being referenced by table" in lowered and "constraint" in lowered) or ("foreign key constraint" in lowered):
            flagged.append(m_str)
            continue

        # Generic patterns: only flag if it looks like SQL Server system message
        if any(p.search(m_str) for p in _ERROR_MESSAGE_PATTERNS):
            if "sql server" in lowered or "level" in lowered or "constraint" in lowered:
                flagged.append(m_str)

    return (len(flagged) > 0, flagged)


def _log_cursor_resultsets(cur, logger: logging.Logger, max_rows: int = 50, fail_on_message_errors: bool = True) -> None:
    """Log any resultsets emitted by the last executed batch.

    IMPORTANT: For multi-statement batches, SQL Server can surface errors when advancing
    to the next result set. We must NOT swallow nextset() exceptions, or we can miss
    real failures (e.g., Msg 3725/3727 FK/constraint dependency errors).
    """
    set_idx = 0
    while True:
        if cur.description:
            set_idx += 1
            cols = [d[0] for d in cur.description]
            logger.info(f"SQL RESULTSET {set_idx}: columns={cols}")

            try:
                rows = cur.fetchmany(max_rows)
            except Exception as e:
                logger.info(f"SQL RESULTSET {set_idx}: fetch failed: {e}")
                rows = []

            for r_idx, row in enumerate(rows, 1):
                logger.info(f"  row {r_idx}: {tuple(row)}")

            # If there are more rows, note it (without exhausting the cursor)
            try:
                more = cur.fetchone()
                if more is not None:
                    logger.info(f"  ... (more rows not shown; max_rows={max_rows})")
            except Exception:
                pass

        # Drain and log any SQL messages produced up to this point
        raw_messages = _drain_cursor_messages(cur)
        message_texts: list[str] = []
        for msg in raw_messages:
            logger.info("SQL MESSAGE: %s", _format_pyodbc_message(msg))
            txt = (_message_to_text(msg) or "").strip()
            if not txt:
                txt = _format_pyodbc_message(msg)
            message_texts.append(txt)

        if fail_on_message_errors and message_texts:
            is_err, flagged = _messages_indicate_error(message_texts)
            if is_err:
                logger.error("SQL output indicated an error while draining resultsets")
                for fm in flagged:
                    logger.error("SQL MESSAGE ERROR: %s", fm)
                raise RuntimeError("SQL emitted error-like messages; aborting migration.")

        # Advance to next resultset. DO NOT swallow errors here.
        has_next = cur.nextset()
        if not has_next:
            break

def _run_sql_batch(
    cur,
    batch: str,
    logger: logging.Logger,
    batch_idx: int,
    batch_count: int,
    fail_on_message_errors: bool = True,
) -> None:
    """Execute a single SQL batch and log output/messages/resultsets."""
    # Clear any leftover messages from previous operations
    _drain_cursor_messages(cur)

    cur.execute(batch)

    # Drain + log SQL Server messages (PRINT, RAISERROR <= 10, some swallowed TRY/CATCH output, etc.)
    raw_messages = _drain_cursor_messages(cur)

    message_texts: list[str] = []
    for msg in raw_messages:
        # Always log the fullest representation we can
        logger.info("SQL MESSAGE: %s", _format_pyodbc_message(msg))

        # Build plain-text list for error heuristics (never lose the raw header+text)
        txt = (_message_to_text(msg) or "").strip()
        if not txt:
            txt = _format_pyodbc_message(msg)
        message_texts.append(txt)

    if fail_on_message_errors:
        is_err, flagged = _messages_indicate_error(message_texts)
        if is_err:
            logger.error("SQL output indicated an error in batch %s/%s", batch_idx, batch_count)
            for fm in flagged:
                logger.error("SQL MESSAGE ERROR: %s", fm)
            raise RuntimeError("SQL batch emitted error-like messages; aborting migration.")

    # Log any SELECT outputs / resultsets
    _log_cursor_resultsets(cur, logger, fail_on_message_errors=fail_on_message_errors)

    # If no resultsets, still log rowcount if meaningful
    try:
        if cur.description is None and cur.rowcount not in (-1, None):
            logger.info(f"SQL ROWCOUNT: {cur.rowcount}")
    except Exception:
        pass

def run_sql_file(cur, path: Path, logger: logging.Logger = None) -> None:
    """Execute a .sql file, respecting GO batch separators and encodings.

    Outputs:
      - SQL Server informational messages (PRINT/RAISERROR <= 10) as 'SQL MESSAGE'
      - Any SELECT resultsets (bounded sample)
      - Rowcounts when available

    Behavior:
      - If SQL Server emits an error-like message (commonly constraint/FK dependency
        failures that sometimes show up in cursor.messages), the migration fails
        loudly by raising an exception.
    """
    if logger:
        logger.debug(f"Reading SQL file: {path}")

    raw = path.read_bytes()

    decoded_text = None
    for enc in ("utf-8-sig", "utf-16", "utf-16-le"):
        try:
            decoded_text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            decoded_text = None

    if decoded_text is None:
        decoded_text = raw.decode("latin-1")  # last resort

    if "\x00" in decoded_text:
        decoded_text = decoded_text.replace("\x00", "")

    batches = split_sql_batches(decoded_text)
    if logger:
        logger.debug(f"Split into {len(batches)} batch(es)")

    for idx, batch in enumerate(batches, 1):
        if not batch.strip():
            continue

        if logger:
            batch_preview = batch[:200].replace("\n", " ").strip()
            if len(batch) > 200:
                batch_preview += "..."
            logger.debug(f"Executing batch {idx}/{len(batches)}: {batch_preview}")

        try:
            if logger:
                _run_sql_batch(cur, batch, logger, idx, len(batches), fail_on_message_errors=True)
                logger.debug(f"Batch {idx}/{len(batches)} executed successfully")
            else:
                cur.execute(batch)
        except Exception as e:
            if logger:
                logger.error(f"Batch {idx}/{len(batches)} failed: {str(e)}")
                logger.error(f"Failed SQL batch:\n{batch}")
            raise



def list_migration_files(use_scratch: bool = False) -> list[Path]:
    """Return all .sql files under MIGRATIONS_DIR or SCRATCH_MIGRATIONS_DIR, sorted."""
    migrations_dir = SCRATCH_MIGRATIONS_DIR if use_scratch else MIGRATIONS_DIR
    return sorted(migrations_dir.rglob("*.sql"))


def apply_migration(cur, version: str, description: str, script_path: Path, logger: logging.Logger = None) -> None:
    """Execute a migration script and record it in SchemaVersion."""
    msg = f"Applying {version} - {description} ({script_path.name})"
    if logger:
        logger.info(msg)
    else:
        print(msg)

    run_sql_file(cur, script_path, logger)

    if logger:
        logger.debug(f"Recording migration {version} in SchemaVersion table")

    cur.execute(
        """
        INSERT INTO dbo.SchemaVersion (Version, Description, ScriptPath)
        VALUES (?, ?, ?)
        """,
        (version, description, str(script_path)),
    )

    if logger:
        logger.info(f"[OK] Migration {version} completed successfully")


def run_all_migrations(db_name: str, use_scratch: bool = False, env_path: Path = None) -> None:
    """Run all pending migrations (schema then seed) against the given DB."""
    # Setup logging if env_path provided
    logger = None
    if env_path:
        logger = setup_logging(env_path, db_name)

    if logger:
        logger.info(f"Starting migration process for database: {db_name}")
        logger.info(f"Using migrations from: {'scratch/sql/migrations' if use_scratch else 'sql/migrations'}")

    try:
        with connect_to_db(db_name) as conn:
            cur = conn.cursor()
            applied = get_applied_versions(cur)

            if logger:
                logger.info(f"Found {len(applied)} already applied migration(s)")

            all_files = list_migration_files(use_scratch=use_scratch)
            v_files = [f for f in all_files if f.name.startswith("V")]
            s_files = [f for f in all_files if f.name.startswith("S")]

            pending_count = 0
            for path in v_files + s_files:
                file_name = path.name
                if "__" in file_name:
                    version, desc_part = file_name.split("__", 1)
                    description = desc_part.rsplit(".sql", 1)[0]
                else:
                    version = file_name
                    description = file_name

                if version in applied:
                    if logger:
                        logger.debug(f"Skipping already applied migration: {version}")
                    continue

                pending_count += 1
                apply_migration(cur, version, description, path, logger)

            migrations_source = "scratch/sql/migrations" if use_scratch else "sql/migrations"
            completion_msg = f"All pending migrations from {migrations_source} applied to [{db_name}]."

            if pending_count == 0:
                completion_msg = f"No pending migrations found in {migrations_source}. Database [{db_name}] is up to date."

            if logger:
                logger.info("=" * 80)
                logger.info(f"Migration process complete. Applied {pending_count} migration(s).")
                logger.info(completion_msg)
            else:
                print(completion_msg)
    except Exception:
        if logger:
            logger.exception("Migration process failed")
        raise


# -------------------------------------------------------
# Environment orchestration
# -------------------------------------------------------


def _sanitize_label(label: str) -> str:
    """Sanitize a user-provided label for use in a database name."""
    import re as _re
    sanitized = label.strip().replace(" ", "_")
    sanitized = _re.sub(r"[^A-Za-z0-9_]", "", sanitized)
    return sanitized[:50]


def _next_test_seq() -> int:
    """Next ephemeral sequence number = max existing LibertyBee_Test_<NNN> on the server + 1.
    Matches only the new short id (1-4 digits terminated by _ or end), so legacy
    8-digit-timestamp names (LibertyBee_Test_20260528_...) are ignored."""
    import re as _re
    pat = _re.compile(_re.escape(TEST_DB_PREFIX) + r"_(\d{1,4})(?:_|$)")
    mx = 0
    with connect_to_server() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sys.databases WHERE name LIKE ?", TEST_DB_PREFIX + "[_]%")
        for (name,) in cur.fetchall():
            m = pat.match(name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1


def _git_info():
    """(branch, short-commit) for provenance; ('unknown', 'unknown') if git is unavailable."""
    def _run(rest):
        try:
            out = subprocess.run(["git", *rest], cwd=str(REPO_ROOT),
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"
    return _run(["rev-parse", "--abbrev-ref", "HEAD"]), _run(["rev-parse", "--short", "HEAD"])


def _stamp_provenance(db_name: str, label: str = "", use_scratch: bool = False) -> None:
    """Record what this ephemeral DB is for, inside the DB itself: a one-row dbo._provenance
    table + the database's MS_Description extended property (shown in SSMS)."""
    branch, commit = _git_info()
    note = label or "(unlabeled)"
    with connect_to_db(db_name) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            IF OBJECT_ID('dbo._provenance') IS NULL
            CREATE TABLE dbo._provenance (
                Label             NVARCHAR(200) NULL,
                CreatedUtc        DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
                CreatedBy         NVARCHAR(128) NULL,
                SourceBranch      NVARCHAR(200) NULL,
                SourceCommit      NVARCHAR(40)  NULL,
                MigrationsScratch BIT           NOT NULL DEFAULT 0,
                Note              NVARCHAR(400) NULL
            );
            """
        )
        # A Gold minted from a stamped ephemeral carries _provenance rows + the
        # MS_Description extended property through the restore, so clear/replace rather
        # than assume the restore wiped them (keeps _provenance one-row and the stamp
        # idempotent — otherwise a re-stamp collides with "MS_Description already exists").
        cur.execute("DELETE FROM dbo._provenance;")
        cur.execute(
            "INSERT INTO dbo._provenance (Label, CreatedBy, SourceBranch, SourceCommit, MigrationsScratch, Note) "
            "VALUES (?, SUSER_SNAME(), ?, ?, ?, ?)",
            label, branch, commit, 1 if use_scratch else 0, note,
        )
        cur.execute(
            "IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 0 AND name = N'MS_Description') "
            "EXEC sys.sp_dropextendedproperty @name=N'MS_Description';"
        )
        cur.execute(
            "EXEC sys.sp_addextendedproperty @name=N'MS_Description', @value=?",
            f"Liberty Bee ephemeral test DB | label={note} | {branch}@{commit}",
        )
    print(f"Provenance stamped on [{db_name}]: label={note}, {branch}@{commit}")


def list_ephemeral() -> None:
    """List all LibertyBee_Test_* databases (seq / label / created / size). Read-only."""
    import re as _re
    pat = _re.compile(_re.escape(TEST_DB_PREFIX) + r"_(\d+)(?:_(.*))?$")
    with connect_to_server() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT d.name, d.create_date, CAST(SUM(mf.size) * 8.0 / 1024 AS DECIMAL(10,1)) "
            "FROM sys.databases d JOIN sys.master_files mf ON mf.database_id = d.database_id "
            "WHERE d.name LIKE ? GROUP BY d.name, d.create_date ORDER BY d.name",
            TEST_DB_PREFIX + "[_]%",
        )
        rows = cur.fetchall()
    if not rows:
        print("No LibertyBee_Test_* databases.")
        return
    print(f"{'#':>5}  {'label':<22} {'created':<19} {'MB':>9}  database")
    for name, created, size_mb in rows:
        m = pat.match(name)
        seq = m.group(1) if m else "?"
        label = m.group(2) if (m and m.group(2)) else "(unlabeled)"
        print(f"{seq:>5}  {label:<22} {str(created)[:19]:<19} {size_mb:>9}  {name}")
    print(f"\n{len(rows)} ephemeral database(s).")


def drop_ephemeral(db_name: str, assume_yes: bool = False) -> None:
    """Manually drop ONE ephemeral DB (+ its env folder). Hard-scoped to the
    LibertyBee_Test_ prefix, so it can never touch a named / production database."""
    if not db_name.startswith(TEST_DB_PREFIX + "_"):
        print(f"Refusing to drop [{db_name}] - only {TEST_DB_PREFIX}_* databases may be dropped.")
        return
    if not assume_yes:
        print(f"DRY RUN - would drop database [{db_name}] and remove environments/{db_name}/. "
              f"Re-run with --yes to execute.")
        return
    with connect_to_server() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM sys.databases WHERE name = ?", db_name)
        if cur.fetchone():
            cur.execute(f"ALTER DATABASE [{db_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;")
            cur.execute(f"DROP DATABASE [{db_name}];")
            print(f"Dropped database [{db_name}].")
        else:
            print(f"Database [{db_name}] not found (already dropped).")
    folder = ENV_ROOT / db_name
    if folder.exists():
        import shutil
        shutil.rmtree(folder)
        print(f"Removed environments/{db_name}/.")


def create_and_prepare_test_env(use_scratch: bool = False, label: str = "") -> str:
    """Create a new test DB + env folder, run migrations, stamp provenance; return its name.

    Name = LibertyBee_Test_<NNN>[_<label>] - an incrementing id (id-first) + optional purpose
    label (e.g. pr-66, issue-63, phase-1-2). Each DB self-describes via dbo._provenance."""
    seq = f"{_next_test_seq():04d}"
    if label:
        db_name = f"{TEST_DB_PREFIX}_{seq}_{_sanitize_label(label)}"
    else:
        db_name = f"{TEST_DB_PREFIX}_{seq}"

    restore_db_from_backup(db_name)
    ensure_database(db_name)

    env_path = ensure_environment_folder(db_name)
    run_all_migrations(db_name, use_scratch=use_scratch, env_path=env_path)
    _stamp_provenance(db_name, label=label, use_scratch=use_scratch)

    print(f"Test environment ready: {db_name}")
    return db_name


# -------------------------------------------------------
# PostgreSQL path (PG-port; phases/phase_1_12/pg_port_design.md)
#
# The PG lifecycle is structurally simpler than the SQL Server restore
# path: `CREATE DATABASE ... TEMPLATE libertybee_gold` IS the Gold restore
# (the baseline is built/rebuilt by environmentscripts/pg_schema_cut.py,
# never here). Pending migrations (post-cut, common-subset by design) apply
# from the SAME sql/migrations/ chain via psycopg — GO lines split batches
# on both engines. Drops route through THIS tool only (the guard ruling:
# raw DROP DATABASE in a shell is blocked by bash_guard, deliberately).
# -------------------------------------------------------

PG_TEST_PREFIX = "libertybee_test"
PG_GOLD_DB = "libertybee_gold"
# The template envs mint from. Overridable so sweep workers can reset to a
# corpus-base template (e.g. libertybee_salem_gold, the supersede's A2 bake)
# instead of the engine gold.
PG_TEMPLATE = os.getenv("LB_PG_TEMPLATE", PG_GOLD_DB)
PG_HOST = os.getenv("LB_PG_HOST", "localhost")
PG_PORT = int(os.getenv("LB_PG_PORT", "5432"))
PG_USER = os.getenv("LB_PG_USER", "libertybee")  # password via pgpass.conf ONLY


def _pg_connect(dbname: str = "postgres", autocommit: bool = True):
    import psycopg
    return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                           dbname=dbname, autocommit=autocommit)


def _pg_next_test_seq() -> int:
    import re as _re
    pat = _re.compile(_re.escape(PG_TEST_PREFIX) + r"_(\d{1,4})(?:_|$)")
    mx = 0
    with _pg_connect() as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (PG_TEST_PREFIX + r"\_%",)).fetchall()
    for (name,) in rows:
        m = pat.match(name)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _pg_ensure_environment_folder(envname: str) -> Path:
    env_path = ENV_ROOT / envname
    env_path.mkdir(parents=True, exist_ok=True)
    config_path = env_path / "db_config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps({
            "backend": "psycopg",
            "server": PG_HOST,
            "port": PG_PORT,
            "database": envname,
            "username": PG_USER,
            # no password field, ever — auth rides pgpass.conf (the
            # 2026-08-13 credential ruling; DatabaseManager REFUSES a
            # password on the psycopg backend)
        }, indent=2), encoding="utf-8")
        print(f"Created db_config.json for [{envname}]")
    return env_path


def _pg_apply_pending_migrations(db_name: str) -> int:
    """Apply sql/migrations/ files not yet in the env's SchemaVersion.
    Post-cut migrations are common-subset by design (pg_port_design.md) —
    a T-SQL-only construct here fails LOUD, which is correct."""
    applied_n = 0
    with _pg_connect(db_name, autocommit=False) as conn:
        applied = {r[0] for r in conn.execute(
            "SELECT Version FROM dbo.SchemaVersion").fetchall()}
        for path in list_migration_files():
            version = path.name.split("__", 1)[0]
            if version in applied:
                continue
            sql_text = path.read_text(encoding="utf-8")
            for batch in split_sql_batches(sql_text):
                if batch.strip():
                    conn.execute(batch)
            conn.execute(
                "INSERT INTO dbo.SchemaVersion (Version, Description, ScriptPath) "
                "VALUES (%s, %s, %s)",
                (version, path.stem.split("__", 1)[-1].replace("_", " "), str(path)))
            conn.commit()
            applied_n += 1
            print(f"Applied migration {version} to [{db_name}]")
    return applied_n


def _pg_stamp_provenance(db_name: str, label: str = "") -> None:
    branch, commit = _git_info()
    with _pg_connect(db_name) as conn:
        conn.execute("DELETE FROM dbo._provenance")
        conn.execute(
            "INSERT INTO dbo._provenance "
            "(Label, CreatedBy, SourceBranch, SourceCommit, MigrationsScratch, Note) "
            "VALUES (%s, CURRENT_USER, %s, %s, 0, %s)",
            (label or "(unlabeled)", branch, commit,
             f"minted from TEMPLATE {PG_GOLD_DB}"))
    print(f"Provenance stamped on [{db_name}]: label={label or '(unlabeled)'}, "
          f"{branch}@{commit}")


def pg_reset_named_env(db_name: str) -> str:
    """DESTRUCTIVE named reset to the template — the PG analogue of the
    SQL Server --envname restore path, for fixed worker-pool slots. The same
    hard allow-list applies: ephemeral names only, no override flag."""
    if not db_name.startswith(PG_TEST_PREFIX + "_"):
        raise SystemExit(
            f"Refusing to reset [{db_name}] — --pg --envname resets only "
            f"{PG_TEST_PREFIX}_* ephemeral envs (F-05 guard, PG form).")
    with _pg_connect() as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (PG_TEMPLATE,)).fetchone():
            raise SystemExit(f"template {PG_TEMPLATE} not found")
        conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
        conn.execute(f"CREATE DATABASE {db_name} TEMPLATE {PG_TEMPLATE}")
    print(f"Reset [{db_name}] from TEMPLATE {PG_TEMPLATE}")
    n = _pg_apply_pending_migrations(db_name)
    print(f"Pending migrations applied: {n}")
    _pg_ensure_environment_folder(db_name)
    _pg_stamp_provenance(db_name, label="worker-reset")
    print(f"Test environment ready: {db_name}")
    return db_name


def pg_promote_template(src_env: str, template_name: str) -> None:
    """Designate an imported env as a named corpus-base template (Track A2:
    region_importer --pg output -> libertybee_salem_gold). A plain rename —
    any PG database can serve as a TEMPLATE source. Refuses to overwrite."""
    if not src_env.startswith(PG_TEST_PREFIX + "_"):
        raise SystemExit(f"--promote-template source must be a {PG_TEST_PREFIX}_* env")
    with _pg_connect() as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                        (template_name,)).fetchone():
            raise SystemExit(
                f"{template_name} already exists — drop it via tooling first "
                f"(refusing to overwrite a baseline silently)")
        conn.execute(f"ALTER DATABASE {src_env} RENAME TO {template_name}")
    env_path = ENV_ROOT / src_env
    if env_path.exists():
        import shutil
        shutil.rmtree(env_path)
    print(f"Promoted [{src_env}] -> template [{template_name}] "
          f"(env folder removed — templates are not run targets)")


def pg_create_and_prepare_test_env(label: str = "") -> str:
    """Mint a PG test env from the template; apply pending migrations;
    stamp provenance. Name = libertybee_test_<NNNN>[_<label>] (lowercase —
    PG folds unquoted identifiers)."""
    with _pg_connect() as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (PG_TEMPLATE,)).fetchone():
            raise SystemExit(
                f"{PG_TEMPLATE} not found — build the gold first: "
                f"python environmentscripts/pg_schema_cut.py --env <sqlserver_env> --build-gold")
        seq = f"{_pg_next_test_seq():04d}"
        db_name = f"{PG_TEST_PREFIX}_{seq}"
        if label:
            db_name += f"_{_sanitize_label(label).lower()}"
        conn.execute(f"CREATE DATABASE {db_name} TEMPLATE {PG_TEMPLATE}")
    print(f"Minted [{db_name}] from TEMPLATE {PG_TEMPLATE}")
    n = _pg_apply_pending_migrations(db_name)
    print(f"Pending migrations applied: {n}")
    _pg_ensure_environment_folder(db_name)
    _pg_stamp_provenance(db_name, label=label)
    print(f"Test environment ready: {db_name}")
    return db_name


def pg_list_ephemeral() -> None:
    with _pg_connect() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s "
            "ORDER BY datname", (PG_TEST_PREFIX + r"\_%",)).fetchall()]
    if not names:
        print(f"No {PG_TEST_PREFIX}_* databases on {PG_HOST}:{PG_PORT}.")
        return
    print(f"PG ephemeral test databases ({PG_HOST}:{PG_PORT}):")
    for name in names:
        note = ""
        try:
            with _pg_connect(name) as c:
                row = c.execute(
                    "SELECT Label, CreatedUtc, SourceBranch, SourceCommit "
                    "FROM dbo._provenance "
                    "ORDER BY CreatedUtc DESC OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY"
                ).fetchone()
            if row:
                note = f"  — {row[0]} ({row[2]}@{row[3]}, {row[1]:%Y-%m-%d})"
        except Exception:
            note = "  — (no provenance)"
        print(f"  {name}{note}")


def pg_drop_ephemeral(db_name: str, assume_yes: bool = False) -> None:
    """Drop a PG ephemeral env. Refuses anything outside the test prefix —
    libertybee_gold and every non-ephemeral name are untouchable here."""
    if not db_name.startswith(PG_TEST_PREFIX + "_"):
        raise SystemExit(
            f"Refusing to drop [{db_name}] — only {PG_TEST_PREFIX}_* ephemeral "
            f"envs are droppable via this tool.")
    if not assume_yes:
        print(f"DRY RUN: would drop [{db_name}] and its environments/ folder. "
              f"Re-run with --yes to execute.")
        return
    with _pg_connect() as conn:
        cur = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone():
            conn.execute(f"DROP DATABASE {db_name}")
            print(f"Dropped [{db_name}]")
        else:
            print(f"Database [{db_name}] not found (already dropped).")
    env_path = ENV_ROOT / db_name
    if env_path.exists():
        import shutil
        shutil.rmtree(env_path)
        print(f"Removed {env_path}")


# -------------------------------------------------------
# CLI entry point
# -------------------------------------------------------


def main() -> str:
    parser = argparse.ArgumentParser(description="Liberty Bee Migration Manager")
    parser.add_argument(
        "--envname",
        type=str,
        help=(
            "Environment name to use/create. If omitted, a new ephemeral test "
            "environment and database will be created."
        ),
    )
    parser.add_argument(
        "--scratch",
        action="store_true",
        help=(
            "Use migrations from scratch/sql/migrations/ instead of sql/migrations/. "
            "Useful for testing migrations before promoting them."
        ),
    )
    parser.add_argument(
        "--label",
        type=str,
        default="",
        help=(
            "Purpose label embedded in the ephemeral DB name (id-first), e.g. "
            "--label phase-1-2 -> LibertyBee_Test_0007_phase-1-2. Ignored when --envname is supplied."
        ),
    )
    parser.add_argument("--baseline", type=str, default="",
                        help=("Directory holding the baseline .bak to restore from "
                              "(most recent .bak wins). Defaults to LB_BASELINE_BACKUP_DIR / "
                              "LB_GOLD_BACKUP_DIR / DBBackup/gold. Use this to build an "
                              "environment from a Silver cut instead of Gold."))
    parser.add_argument("--issue", type=str, default="",
                        help="Convenience: sets the label to issue-<n>.")
    parser.add_argument("--pr", type=str, default="",
                        help="Convenience: sets the label to pr-<n>.")
    parser.add_argument("--list", action="store_true",
                        help="List all LibertyBee_Test_* databases (seq / label / created / size) and exit.")
    parser.add_argument("--drop", type=str, default="",
                        help="Drop ONE LibertyBee_Test_* database + its env folder (dry-run unless --yes).")
    parser.add_argument("--yes", action="store_true",
                        help="Confirm a --drop (without it, --drop only previews).")
    parser.add_argument("--pg", action="store_true",
                        help=("PostgreSQL mode: mint (TEMPLATE + pending migrations), "
                              "--envname (destructive named reset, ephemeral-only), --list, "
                              "--drop, or --promote-template. Template = LB_PG_TEMPLATE "
                              "(default libertybee_gold; the gold itself is built by "
                              "pg_schema_cut.py, never here)."))
    parser.add_argument("--promote-template", type=str, default="",
                        help=("with --pg --envname <src>: rename an imported env to a named "
                              "corpus-base template (e.g. libertybee_salem_gold — Track A2)."))

    args = parser.parse_args()

    global _baseline_dir_override

    if args.baseline:

        _baseline_dir_override = args.baseline

        print(f"Baseline source: {args.baseline}")

    if args.pg:
        if args.scratch or args.baseline:
            raise SystemExit("--pg does not combine with --scratch/--baseline "
                             "(PG envs mint from templates only)")
        if args.list:
            pg_list_ephemeral()
            return ""
        if args.drop:
            pg_drop_ephemeral(args.drop, assume_yes=args.yes)
            return ""
        if args.promote_template:
            if not args.envname:
                raise SystemExit("--promote-template needs --envname <source env>")
            pg_promote_template(args.envname, args.promote_template)
            return ""
        if args.envname:
            return pg_reset_named_env(args.envname)
        return pg_create_and_prepare_test_env(
            label=args.label or (f"issue-{args.issue}" if args.issue else "")
            or (f"pr-{args.pr}" if args.pr else ""))

    if args.list:
        list_ephemeral()
        return ""
    if args.drop:
        drop_ephemeral(args.drop, assume_yes=args.yes)
        return ""

    label = args.label or (f"issue-{args.issue}" if args.issue else "") or (f"pr-{args.pr}" if args.pr else "")

    if args.envname:
        envname = args.envname
        # Audit F-05 (2026-07-03): this path is a DESTRUCTIVE reset-to-Gold
        # (RESTORE WITH REPLACE), and previously had no name guard — a typo'd
        # `--envname LibertyBee` would have restored Gold over production.
        # Hard allow-list: ephemeral names only. There is deliberately no
        # override flag; restoring a non-ephemeral DB is a human-only act.
        if not envname.startswith(EPHEMERAL_PREFIX):
            print(
                f"REFUSED: --envname targets '{envname}', which is not an ephemeral "
                f"test database ({EPHEMERAL_PREFIX}*). This path DESTRUCTIVELY "
                "restores the named DB from the Gold backup. Production/baseline "
                "databases are never restored by this script."
            )
            sys.exit(2)
        print(f"Using supplied environment: {envname}")
        print("NOTE: --envname RESETS this DB to Gold + re-applies migrations "
              "(destructive reset, not additive).")
        if args.scratch:
            print("Using migrations from scratch/sql/migrations/")

        restore_db_from_backup(envname)
        ensure_database(envname)

        env_path = ensure_environment_folder(envname)
        run_all_migrations(envname, use_scratch=args.scratch, env_path=env_path)
        # Audit F-06: the restore wipes dbo._provenance + MS_Description —
        # re-stamp so re-applied envs keep self-describing.
        _stamp_provenance(envname, label=(label or "re-applied"), use_scratch=args.scratch)

        return envname

    # No envname: create a fresh ephemeral test environment
    if label:
        print(f"Creating test environment with label: {label}")
    if args.scratch:
        print("Using migrations from scratch/sql/migrations/")
    envname = create_and_prepare_test_env(use_scratch=args.scratch, label=label)
    return envname


if __name__ == "__main__":
    try:
        name = main()
        if name:
            print(f"Use this environment for your tests: {name}")
    except Exception as exc:
        # Audit F-07: failures BEFORE run_all_migrations' logging starts
        # (restore, ensure_database, missing Gold backup) used to exit(1)
        # silently, discarding even the helpful missing-backup hint. Print
        # the error; run_all_migrations' own logged exceptions just repeat
        # one line here, which beats silence.
        print(f"ERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)