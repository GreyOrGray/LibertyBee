"""Cross-engine dialect helpers for the regression suite (PG port).

Tests import this for the few checks that cannot be common-subset SQL:
catalog lookups (index existence) and planner instrumentation. Policy:

- EXISTENCE checks run on BOTH engines (real assertions both sides).
- PLANNER-INSTRUMENTATION assertions (user_seeks bumps etc.) are
  SQL Server regression gates for specific historical index passes
  (V00031, V00067). PG has its own planner and its own stats model —
  on psycopg these SKIP LOUDLY (returning None) rather than fake a pass;
  the test prints the skip and does not count the gate. If PG planner
  behavior ever needs pinning, that's a new gate designed for PG, not a
  translation of a SQL Server one.

The SQL Server branches are byte-identical to the queries the tests
carried before the port — the incumbent engine's behavior is unchanged.
"""


def is_pg(db) -> bool:
    return db.config.backend == "psycopg"


def index_exists(db, table: str, name: str) -> bool:
    """True if the named index exists on schema-qualified `table`."""
    if is_pg(db):
        schema, tbl = table.split(".", 1)
        row = db.execute_query(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE schemaname = ? AND tablename = ? AND LOWER(indexname) = ?",
            (schema.lower(), tbl.lower(), name.lower()))
    else:
        row = db.execute_query(
            "SELECT COUNT(*) FROM sys.indexes WHERE name = ? AND object_id = OBJECT_ID(?)",
            (name, table))
    return bool(row and row[0][0])


def index_user_seeks(db, table: str, name: str):
    """SQL Server: the index's user_seeks counter (the historical gate
    metric). PG: None — planner instrumentation does not translate; the
    caller must print a loud skip."""
    if is_pg(db):
        return None
    row = db.execute_query(
        """
        SELECT COALESCE(u.user_seeks, 0)
        FROM sys.indexes i
        LEFT JOIN sys.dm_db_index_usage_stats u
          ON u.object_id = i.object_id AND u.index_id = i.index_id
         AND u.database_id = DB_ID()
        WHERE i.name = ? AND i.object_id = OBJECT_ID(?)
        """, (name, table))
    return row[0][0] if row else 0


def index_usage_triple(db, table: str, name: str):
    """SQL Server: (user_seeks, user_scans, user_lookups). PG: None (loud
    skip at the caller)."""
    if is_pg(db):
        return None
    row = db.execute_query(
        """
        SELECT s.user_seeks, s.user_scans, s.user_lookups
        FROM sys.dm_db_index_usage_stats s
        JOIN sys.indexes i ON i.object_id = s.object_id AND i.index_id = s.index_id
        WHERE s.object_id = OBJECT_ID(?)
          AND s.database_id = DB_ID()
          AND i.name = ?
        """, (table, name))
    return tuple(row[0]) if row else (0, 0, 0)


SKIP_NOTE = ("SKIP on PostgreSQL — planner-instrumentation gate is engine-"
             "specific (pins a SQL Server index pass); existence gates still ran")


def month_diff_sql(db, a: str, b: str) -> str:
    """SQL snippet: months from `a` to `b`, T-SQL DATEDIFF(MONTH,...)
    semantics (month-BOUNDARY count, not elapsed time) on both engines."""
    if is_pg(db):
        return (f"((EXTRACT(YEAR FROM {b}) - EXTRACT(YEAR FROM {a})) * 12 "
                f"+ (EXTRACT(MONTH FROM {b}) - EXTRACT(MONTH FROM {a})))")
    return f"DATEDIFF(MONTH, {a}, {b})"


def add_months_sql(db, n, expr: str) -> str:
    """SQL snippet: `expr` + n months. Both engines clamp to month-end
    (Jan 31 + 1 month = Feb 28) — T-SQL DATEADD semantics preserved."""
    if is_pg(db):
        return f"({expr} + INTERVAL '{int(n)} months')"
    return f"DATEADD(MONTH, {int(n)}, {expr})"


def add_days_sql(db, n, expr: str) -> str:
    """SQL snippet: `expr` + n days."""
    if is_pg(db):
        return f"({expr} + INTERVAL '{int(n)} days')"
    return f"DATEADD(DAY, {int(n)}, {expr})"
