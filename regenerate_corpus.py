"""Regenerate the Liberty Bee Monte Carlo corpus.

Builds a fresh worker database per (rung, seed) from the Gold seed database,
runs the simulation engine, extracts the result rows — tagged (Rung, Seed) —
into a central corpus database, then returns the worker to the pool for reuse.
Repeats across every (rung, seed) pair to rebuild the full corpus.

Per-(rung, seed) lifecycle:
    1. Skip if (rung, seed) already in v1.run_summary (restartable).
    2. Reset a worker database to Gold via migration_manager.
    3. Run `simulation.py --env <db> --projection-id <rung_id> --seed <seed> --months 240`.
    4. On exit 0: extract result rows into <corpus>.v1.* tagged (Rung, Seed);
       insert v1.run_summary; return the worker DB to the pool.
    5. On exit != 0: log failure; retain the worker DB for inspection.

The corpus spans sixteen funding rungs: ten stored projections (200-209,
defined in the seed database) plus six synthetic rungs (300-305, cloned from
projection 206 at runtime). Results are written to the database named by
--corpus.

Parallelism: a pool of worker slots processes (rung, seed) pairs concurrently.

Restart: at startup the driver reads v1.run_summary and skips pairs already
present, so an interrupted run resumes cleanly on re-invocation.

Failure retention: a worker DB whose sim failed is retained; the status file
lists it alongside the (rung, seed) and sim exit context.

Usage:
    # Full corpus: sixteen rungs x fifty seeds (800 runs)
    python regenerate_corpus.py --corpus <corpus_db> --seeds 1-50

    # A single rung across a seed range
    python regenerate_corpus.py --corpus <corpus_db> --rungs 206 --seeds 1-50

    # Resume an interrupted run (restart is automatic; done pairs skip)
    python regenerate_corpus.py --corpus <corpus_db> --seeds 1-50

    # Force re-run one (rung, seed) pair (clears its existing v1.* rows first)
    python regenerate_corpus.py --corpus <corpus_db> --rerun-rung 200 --rerun-seed 42

    # Dry run: show what would be done without doing it
    python regenerate_corpus.py --corpus <corpus_db> --seeds 1-50 --dry-run
"""

import argparse
import concurrent.futures
import datetime
import os
import subprocess
import sys
import time

import pyodbc

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_VERSION = "0.5.0"  # simulation engine version of record for this corpus

# Central corpus database — the target that results are written into. Set from
# --corpus at startup (see main()).
CENTRAL_DB = None

# Connection template. Server and driver are overridable via the environment so
# the tool is portable across machines. CONN_TMPL keeps a single `{}` slot for
# the database name, filled per-connection via CONN_TMPL.format(db_name).
SQL_SERVER = os.environ.get("LB_SQL_SERVER", "localhost")
SQL_DRIVER = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
CONN_TMPL = "DRIVER={{" + SQL_DRIVER + "}};SERVER=" + SQL_SERVER + ";Trusted_Connection=yes;DATABASE={}"

# Stored funding rungs: ProjectionParameters.ID -> FIN_StartingFunds. These ten
# projections are defined directly in the seed database (locked by migration
# V00034) and run via --projection-id.
RUNGS = {
    200: 5_000_000.00,
    201: 5_500_000.00,
    202: 6_000_000.00,
    203: 6_500_000.00,
    204: 7_000_000.00,
    205: 7_500_000.00,
    206: 8_000_000.00,
    207: 9_000_000.00,
    208: 10_000_000.00,
    209: 11_000_000.00,
}

# Synthetic low-funding rungs (300-305) — NOT stored projections. Each is cloned
# per-worker at runtime from projection 206's registry rows, with ONLY
# StartingFunds/ProjectionName replaced (see seed_extension_projection). This
# keeps the under-funded rungs self-contained without adding stored projections.
EXTENSION_BASE_PROJ = 206
EXTENSION_RUNGS = {
    300: 2_000_000.00,
    301: 2_500_000.00,
    302: 3_000_000.00,
    303: 3_500_000.00,
    304: 4_000_000.00,
    305: 4_500_000.00,
}
RUNGS.update(EXTENSION_RUNGS)

# Inflation leg: 'regime' (INF.Mode stays the live Regime default) or 'static'
# (the worker's global INF.Mode row is flipped to Static before the sim).
SWEEP_MODE = "regime"

# The script ships at the repository root; all paths are resolved relative to it.
REPO = os.path.dirname(os.path.abspath(__file__))
SIM_SCRIPT = os.path.join(REPO, "app", "src", "simulation.py")
STATUS_FILE = os.path.join(REPO, "corpus_regen_status.txt")
LOG_DIR = os.path.join(REPO, "corpus_regen_logs")

DEFAULT_WORKERS = 8
DEFAULT_MONTHS = 240

# Fixed per-slot worker DB names, reset to Gold per sim via migration_manager
# --envname (ephemeral-prefix-guarded, re-stamps provenance). A fixed pool of
# names avoids the mint-counter race that concurrent fresh mints hit (all slots
# computing the same next number). A failed sim RETAINS its slot DB for
# inspection (the pool shrinks).
import queue as _queue
WORKER_NAME_POOL = _queue.Queue()

def init_worker_pool(n):
    for i in range(n):
        WORKER_NAME_POOL.put(f"LibertyBee_Test_cw{i:02d}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def conn(db_name):
    return pyodbc.connect(CONN_TMPL.format(db_name), timeout=30, autocommit=True)


def parse_seed_range(spec):
    """Parse '1-84' or '1,2,3' or '1-3,7,10-15' -> sorted list of seeds."""
    seeds = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            seeds.update(range(int(lo), int(hi) + 1))
        else:
            seeds.add(int(part))
    return sorted(seeds)


def parse_rungs(spec):
    if spec == "all" or spec is None:
        return sorted(RUNGS.keys())
    return sorted(int(x) for x in spec.split(","))


def build_worker_db():
    """Take a slot name from the pool and destructively reset it to
    Gold+migrations via migration_manager --envname (subprocess: its logging
    setup clashes with threaded stdout). Returns the DB name. On failure the
    slot name is NOT returned to the pool (retained for inspection)."""
    name = WORKER_NAME_POOL.get(timeout=3600)
    proc = subprocess.run(
        [sys.executable, "environmentscripts/migration_manager.py", "--envname", name],
        capture_output=True, text=True, cwd=REPO, timeout=900,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-300:] + (proc.stderr or "")[-200:]
        raise RuntimeError(f"migration_manager --envname {name} exit {proc.returncode}: {tail}")
    return name


def release_worker_db(db_name):
    """Return a slot to the pool for the next (rung, seed). The DB is NOT
    dropped between sims - the next reset-to-Gold wipes it."""
    WORKER_NAME_POOL.put(db_name)


def drop_worker_db(db_name):
    """Drop an ephemeral worker DB. master connection required."""
    try:
        with conn("master") as c:
            cur = c.cursor()
            cur.execute(f"ALTER DATABASE [{db_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE [{db_name}]")
    except Exception as e:
        print(f"  [warn] drop_worker_db({db_name}) failed: {e}", flush=True)


def already_done(rung, seed):
    """Check v1.run_summary for an existing row for (rung, seed)."""
    with conn(CENTRAL_DB) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT 1 FROM v1.run_summary WHERE Rung = ? AND Seed = ?",
            (RUNGS[rung], seed),
        )
        return cur.fetchone() is not None


def force_clear(rung, seed):
    """Remove any existing v1.* rows for (rung, seed). For --rerun support.

    Does NOT clear v1.projection_parameters — that table is keyed by ProjectionID,
    not (Rung, Seed), and is locked-identical across all sims of a given rung.
    A force-rerun should leave it alone.
    """
    with conn(CENTRAL_DB) as c:
        cur = c.cursor()
        rung_val = RUNGS[rung]
        for tbl in ("run_summary", "fund_ledger", "tcs_ledger", "lease",
                    "lease_termination", "lease_termination_ledger", "event_summary",
                    "household", "properties", "property_units", "inflation_schedule",
                    # per-lease / staffing reviewability extracts:
                    "monthly_payment_status", "employees", "payroll"):
            cur.execute(f"DELETE FROM v1.{tbl} WHERE Rung = ? AND Seed = ?", (rung_val, seed))


def ensure_projection_parameters(worker_db, projection_id):
    """Populate v1.projection_parameters for `projection_id` from the WORKER's
    reference.ParameterRegistry, resolved override-else-global (the legacy wide
    reference.ProjectionParameters table still exists but is STALE and must
    never feed corpus metadata). InflationMode records the leg (SWEEP_MODE)
    directly."""
    with conn(CENTRAL_DB) as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM v1.projection_parameters WHERE ProjectionID = ?", (projection_id,))
        if cur.fetchone() is not None:
            return  # already populated by a sibling worker
    wanted = {
        ("SIM", "ProjectionName"), ("FIN", "StartingFunds"),
        ("PROP", "BelowMarketRentPct"), ("INF", "RentInflationRate"),
        ("RR", "FirstReductionMonths"), ("RR", "FirstReductionPct"),
        ("RR", "SecondReductionMonths"), ("RR", "SecondReductionPct"),
        ("RR", "ThirdReductionMonths"), ("RR", "ThirdReductionPct"),
        ("RR", "FourthReductionMonths"), ("RR", "FourthReductionPct"),
        ("RR", "FifthReductionMonths"), ("RR", "FifthReductionPct"),
    }
    resolved = {}
    overrides = set()
    with conn(worker_db) as w:
        wcur = w.cursor()
        wcur.execute("""
            SELECT Category, Name, ProjectionID, Value
            FROM reference.ParameterRegistry
            WHERE ProjectionID IS NULL OR ProjectionID = ?
        """, (projection_id,))
        for cat, name, pid, val in wcur.fetchall():
            key = (cat, name)
            if key not in wanted:
                continue
            if pid is not None:
                resolved[key] = val
                overrides.add(key)
            elif key not in overrides:
                resolved[key] = val
    def g(cat, name):
        return resolved.get((cat, name))
    try:
        with conn(CENTRAL_DB) as c:
            cur = c.cursor()
            cur.execute("""
                INSERT INTO v1.projection_parameters
                  (ProjectionID, ProjectionName, StartingFunds, BelowMarketRentPct,
                   RentInflationRate, InflationMode,
                   RR_FirstReductionMonths, RR_FirstReductionPct,
                   RR_SecondReductionMonths, RR_SecondReductionPct,
                   RR_ThirdReductionMonths, RR_ThirdReductionPct,
                   RR_FourthReductionMonths, RR_FourthReductionPct,
                   RR_FifthReductionMonths, RR_FifthReductionPct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (projection_id, g("SIM", "ProjectionName"), g("FIN", "StartingFunds"),
                  g("PROP", "BelowMarketRentPct"), g("INF", "RentInflationRate"),
                  "Static" if SWEEP_MODE == "static" else "Regime",
                  g("RR", "FirstReductionMonths"), g("RR", "FirstReductionPct"),
                  g("RR", "SecondReductionMonths"), g("RR", "SecondReductionPct"),
                  g("RR", "ThirdReductionMonths"), g("RR", "ThirdReductionPct"),
                  g("RR", "FourthReductionMonths"), g("RR", "FourthReductionPct"),
                  g("RR", "FifthReductionMonths"), g("RR", "FifthReductionPct")))
    except pyodbc.IntegrityError:
        pass  # sibling worker raced us and won - same end state, harmless


def seed_extension_projection(worker_db, proj_id):
    """Clone EXTENSION_BASE_PROJ's per-projection registry rows to `proj_id` in
    the worker, replacing only StartingFunds + ProjectionName. Idempotent per
    worker reset (the reset wipes it)."""
    if proj_id not in EXTENSION_RUNGS:
        return
    funds = EXTENSION_RUNGS[proj_id]
    name = f"Extension_${funds/1e6:.1f}M"
    with conn(worker_db) as w:
        cur = w.cursor()
        cur.execute("""
            INSERT INTO reference.ParameterRegistry (Category, Name, ProjectionID, Value, DataType, Description)
            SELECT Category, Name, ?,
                   CASE WHEN Category='FIN' AND Name='StartingFunds' THEN ?
                        WHEN Category='SIM' AND Name='ProjectionName' THEN ?
                        ELSE Value END,
                   DataType, Description
            FROM reference.ParameterRegistry
            WHERE ProjectionID = ?
        """, (proj_id, f"{funds:.2f}", name, EXTENSION_BASE_PROJ))
        if cur.rowcount < 3:
            raise RuntimeError(f"extension seeding for proj {proj_id} copied only {cur.rowcount} rows")


def apply_sweep_mode(worker_db):
    """Static leg: flip the WORKER's global INF.Mode row to Static (the worker
    is per-sim disposable, so the global flip scopes exactly one sim). Fail-loud
    if the row shape is unexpected."""
    if SWEEP_MODE != "static":
        return
    with conn(worker_db) as w:
        cur = w.cursor()
        cur.execute("""
            UPDATE reference.ParameterRegistry SET Value = 'Static'
            WHERE ProjectionID IS NULL AND Category = 'INF' AND Name = 'Mode'
        """)
        if cur.rowcount != 1:
            raise RuntimeError("static-mode flip touched %d rows on %s (expected 1)" % (cur.rowcount, worker_db))


def run_sim(worker_db, rung, seed, months):
    """Invoke simulation.py as a subprocess. Returns (exit_code, started_utc, completed_utc, log_path)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"sim_{SWEEP_MODE}_r{rung}_s{seed}.log")
    started = datetime.datetime.utcnow()
    with open(log_path, "w") as log:
        proc = subprocess.run(
            [sys.executable, str(SIM_SCRIPT),
             "--env", worker_db,
             "--projection-id", str(rung),
             "--seed", str(seed),
             "--months", str(months)],
            stdout=log, stderr=subprocess.STDOUT,
            cwd=REPO,
        )
    completed = datetime.datetime.utcnow()
    return proc.returncode, started, completed, log_path


def extract_to_central(worker_db, rung, seed, run_id, started_utc, completed_utc, months=DEFAULT_MONTHS):
    """Copy result rows from worker.simulation.* into the corpus DB's v1.*
    tables tagged (Rung, Seed). Also computes v1.run_summary.

    All INSERTs are wrapped in a single transaction (autocommit=False). If any
    INSERT fails, the entire extract rolls back — so v1.run_summary remains
    absent and the driver's skip-already-done check correctly re-processes
    this (rung, seed) on the next sweep call. No partial state in v1.* tables.
    """
    rung_val = RUNGS[rung]
    # Cross-DB inserts via 4-part naming: [db].[schema].[table].
    # autocommit=False so the extract is atomic: all-or-nothing.
    central_conn = pyodbc.connect(CONN_TMPL.format(CENTRAL_DB), timeout=30, autocommit=False)
    try:
        cur = central_conn.cursor()

        # ----- v1.fund_ledger (all rows) ---------------------------------
        cur.execute(f"""
            INSERT INTO v1.fund_ledger
              (Rung, Seed, EventID, LedgerDate,
               CashDebit, CashCredit, CashBalance,
               CSFDebit, CSFCredit, CSFBalance,
               EIPDebit, EIPCredit, EIPBalance,
               CashHoldCredit, CashHoldDebit, CashHoldBalance,
               EscrowDebit, EscrowCredit, EscrowBalance)
            SELECT ?, ?, EventID, LedgerDate,
                   CashDebit, CashCredit, CashBalance,
                   CSFDebit, CSFCredit, CSFBalance,
                   EIPDebit, EIPCredit, EIPBalance,
                   CashHoldCredit, CashHoldDebit, CashHoldBalance,
                   EscrowDebit, EscrowCredit, EscrowBalance
            FROM [{worker_db}].simulation.FundLedger
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.tcs_ledger ---------------------------------------------
        cur.execute(f"""
            INSERT INTO v1.tcs_ledger
              (Rung, Seed, CreditTransactionID, HouseholdID, TransactionDate,
               TransactionType, Amount, BalanceAfter,
               RelatedCollectionID, RelatedLeaseID, CreatedEventID, Notes)
            SELECT ?, ?, CreditTransactionID, HouseholdID, TransactionDate,
                   TransactionType, Amount, BalanceAfter,
                   RelatedCollectionID, RelatedLeaseID, CreatedEventID, Notes
            FROM [{worker_db}].simulation.TenantCreditLedger
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.lease ---------------------------------------------------
        cur.execute(f"""
            INSERT INTO v1.lease
              (Rung, Seed, LeaseID, HouseholdID, UnitID, VacancyID,
               LeaseSignedDate, LeaseStartDate, LeaseEndDate, MonthlyRent,
               LeaseStatus, TerminationDate, TerminationReason,
               ConsecutiveMissedPayments, RenewalDecision,
               CumulativeRentReductionPct, EffectiveMonthlyRent)
            SELECT ?, ?, LeaseID, HouseholdID, UnitID, VacancyID,
                   LeaseSignedDate, LeaseStartDate, LeaseEndDate, MonthlyRent,
                   LeaseStatus, NULL, NULL,  -- Lease.TerminationDate/Reason DROPPED at V00054 (dead cols); truth in v1.lease_termination
                   ConsecutiveMissedPayments, RenewalDecision,
                   CumulativeRentReductionPct, EffectiveMonthlyRent
            FROM [{worker_db}].simulation.Lease
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.lease_termination --------------------------------------
        cur.execute(f"""
            INSERT INTO v1.lease_termination
              (Rung, Seed, LeaseID, TerminationType, TerminationDate, TerminationReason,
               ArrearsAtExit, DepositWithheldAmount, DepositForfeited, EarlyBreakPenalty,
               EvictionFiledDate, EvictionExecutionDate)
            SELECT ?, ?, LeaseID, TerminationType, TerminationDate, TerminationReason,
                   ArrearsAtExit, DepositWithheldAmount, DepositForfeited, EarlyBreakPenalty,
                   EvictionFiledDate, EvictionExecutionDate
            FROM [{worker_db}].simulation.LeaseTermination
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.lease_termination_ledger -------------------------------
        cur.execute(f"""
            INSERT INTO v1.lease_termination_ledger
              (Rung, Seed, LedgerID, LeaseID, EventID, TxnDate, TxnType,
               Description, Amount, Metadata)
            SELECT ?, ?, LedgerID, LeaseID, EventID, TxnDate, TxnType,
                   Description, Amount, Metadata
            FROM [{worker_db}].simulation.LeaseTerminationLedger
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.household ------------------------------------------------
        cur.execute(f"""
            INSERT INTO v1.household
              (Rung, Seed, HouseholdID, HouseholdType, HouseholdIncomeBand,
               AdultCount, ChildCount, CreatedDate, TCSRedemptionProbability, SigningMonthlyIncome)
            SELECT ?, ?, HouseholdID, HouseholdType, HouseholdIncomeBand,
                   AdultCount, ChildCount, CreatedDate, TCSRedemptionProbability, SigningMonthlyIncome
            FROM [{worker_db}].simulation.Household
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.properties ----------------------------------------------
        cur.execute(f"""
            INSERT INTO v1.properties
              (Rung, Seed, PropertyID, PropertyType, AcquisitionDate,
               BasePrice, InflationAdjustedPrice, TotalUnits)
            SELECT ?, ?, PropertyID, PROPERTYTYPE, AcquisitionDate,
                   BasePrice, InflationAdjustedPrice, TotalUnits
            FROM [{worker_db}].simulation.Properties
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.property_units ------------------------------------------
        cur.execute(f"""
            INSERT INTO v1.property_units
              (Rung, Seed, UnitID, PropertyID, Beds, Baths, BaseRent)
            SELECT ?, ?, UnitID, PropertyID, Beds, Baths, BaseRent
            FROM [{worker_db}].simulation.PropertyUnits
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.inflation_schedule (per-month inflation path) ---------
        # Extracted so reports can reconstruct month-by-month market rent
        # (PropertyUnits.BaseRent × cumulative RentRate factor). Every
        # (Rung, Seed) may carry an identical path, but we extract
        # per-(Rung, Seed) for schema stability.
        cur.execute(f"""
            INSERT INTO v1.inflation_schedule
              (Rung, Seed, MonthIndex, InflationDate,
               GeneralRate, RentRate, OpExRate, PropertyRate,
               ScenarioType, ScenarioPhase, Notes)
            SELECT ?, ?, MonthIndex, InflationDate,
                   GeneralRate, RentRate, OpExRate, PropertyRate,
                   ScenarioType, ScenarioPhase, Notes
            FROM [{worker_db}].simulation.InflationSchedule
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.event_summary (aggregated) -----------------------------
        cur.execute(f"""
            INSERT INTO v1.event_summary
              (Rung, Seed, EventType, EntityType, ActionType, EventCount, TotalAmount)
            SELECT ?, ?, EventType, EntityType, ActionType,
                   COUNT(*), SUM(Amount)
            FROM [{worker_db}].simulation.Event
            WHERE RunID = ?
            GROUP BY EventType, EntityType, ActionType
        """, (rung_val, seed, run_id))

        # ===== Reviewability extracts ====================================

        # ----- v1.monthly_payment_status ---------------------------------
        # Extract verbatim per-month rows for any lease that EVER had a
        # non-ON_TIME month (LATE / MISSED / REDEEMED); one sentinel summary
        # row per purely-ON_TIME lease.
        #
        # Step 1: per-month rows for ever-distressed leases.
        cur.execute(f"""
            WITH ever_distressed AS (
                SELECT DISTINCT LeaseID
                FROM [{worker_db}].simulation.MonthlyPaymentStatus
                WHERE RunID = ? AND (PaymentStatus <> 'ON_TIME' OR PaymentStatus IS NULL)
            )
            INSERT INTO v1.monthly_payment_status
              (Rung, Seed, LeaseID, MonthIndex, PaymentStatus,
               DaysLate, AmountOwed, AmountPaid, LateFeeAccrued, OnTimeMonthsCount)
            -- Schema remap: the V0.3 MonthlyPaymentStatus schema replaced
            -- MonthIndex/DaysLate/AmountOwed/AmountPaid/LateFeeAccrued with
            -- BillingMonth/PaymentDueDate/ActualPaymentDate/AmountDue/
            -- LateFeeAmount. Honest mapping: MonthIndex derived from the run
            -- StartDate; DaysLate = actual-vs-due (NULL if never paid/on time);
            -- AmountPaid not tracked per-month in V0.3 -> NULL.
            SELECT ?, ?, mps.LeaseID,
                   DATEDIFF(MONTH, r.StartDate, mps.BillingMonth) + 1,
                   ISNULL(mps.PaymentStatus, 'UNRESOLVED'),
                   CASE WHEN mps.ActualPaymentDate > mps.PaymentDueDate
                        THEN DATEDIFF(DAY, mps.PaymentDueDate, mps.ActualPaymentDate) END,
                   mps.AmountDue, NULL, mps.LateFeeAmount, NULL
            FROM [{worker_db}].simulation.MonthlyPaymentStatus mps
            CROSS JOIN (SELECT StartDate FROM [{worker_db}].simulation.Run WHERE RunID = ?) r
            WHERE mps.RunID = ?
              AND mps.LeaseID IN (SELECT LeaseID FROM ever_distressed)
        """, (run_id, rung_val, seed, run_id, run_id))

        # Step 2: sentinel summary row for purely-ON_TIME leases.
        # MonthIndex = -1 sentinel + PaymentStatus = 'ALL_ON_TIME' + OnTimeMonthsCount.
        cur.execute(f"""
            WITH ever_distressed AS (
                SELECT DISTINCT LeaseID
                FROM [{worker_db}].simulation.MonthlyPaymentStatus
                WHERE RunID = ? AND (PaymentStatus <> 'ON_TIME' OR PaymentStatus IS NULL)
            ),
            on_time_only AS (
                SELECT mps.LeaseID, COUNT(*) AS OnTimeMonthsCount
                FROM [{worker_db}].simulation.MonthlyPaymentStatus mps
                WHERE mps.RunID = ?
                  AND mps.LeaseID NOT IN (SELECT LeaseID FROM ever_distressed)
                GROUP BY mps.LeaseID
            )
            INSERT INTO v1.monthly_payment_status
              (Rung, Seed, LeaseID, MonthIndex, PaymentStatus,
               DaysLate, AmountOwed, AmountPaid, LateFeeAccrued, OnTimeMonthsCount)
            SELECT ?, ?, ot.LeaseID, -1, 'ALL_ON_TIME',
                   NULL, NULL, NULL, NULL, ot.OnTimeMonthsCount
            FROM on_time_only ot
        """, (run_id, run_id, rung_val, seed))

        # ----- v1.employees -----------------------------------------------
        # Verbatim per-employee with denormalized RoleName.
        cur.execute(f"""
            INSERT INTO v1.employees
              (Rung, Seed, EmployeeID, RoleID, RoleName,
               HiredDate, TerminatedDate, BaseSalary, BenefitsCost, IsActive)
            SELECT ?, ?, e.EmployeeID, e.RoleID, er.Role,
                   e.HiredDate, e.TerminatedDate, e.BaseSalary, e.BenefitsCost, e.IsActive
            FROM [{worker_db}].simulation.Employees e
            INNER JOIN [{worker_db}].reference.EmployeeRole er ON er.RoleID = e.RoleID
            WHERE e.RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.payroll -------------------------------------------------
        # Verbatim per-payroll-event.
        cur.execute(f"""
            INSERT INTO v1.payroll
              (Rung, Seed, PayrollID, EmployeeID, PayrollDate,
               GrossPay, BenefitsCost, TotalCost)
            SELECT ?, ?, PayrollID, EmployeeID, PayrollDate,
                   GrossPay, BenefitsCost, TotalCost
            FROM [{worker_db}].simulation.Payroll
            WHERE RunID = ?
        """, (rung_val, seed, run_id))

        # ----- v1.run_summary (one row per sim) --------------------------
        # Compute summary stats with subqueries against the worker DB.
        cur.execute(f"""
            INSERT INTO v1.run_summary
              (Rung, Seed, ProjectionID, EngineVersion, EphemeralDBName,
               StartedAtUtc, CompletedAtUtc,
               FinalCash, FinalCSF, FinalEIP, Survived,
               LeaseCount, PropertyCount, UnitCount, HouseholdCount, PersonCount,
               EvictionCount, LNRCount, EarlyBreakCount, VolExitCount, RenewalCount,
               TCSAccruedTotal, TCSRedeemedTotal, TCSForfeitedTotal)
            SELECT
                ?, ?, ?, ?, ?,
                ?, ?,
                final_balances.CashBalance,
                final_balances.CSFBalance,
                final_balances.EIPBalance,
                -- Survival ALSO requires the run to reach the requested
                -- horizon. Halted runs die with small POSITIVE residuals
                -- (the halt fires at combined < payroll, not at 0), so
                -- balance-only scoring would mislabel deaths as survivors
                -- (observed on the low-funding extension rungs).
                CASE WHEN final_balances.CashBalance + final_balances.CSFBalance > 0
                          AND (SELECT DATEDIFF(MONTH, MIN(LedgerDate), MAX(LedgerDate))
                               FROM [{worker_db}].simulation.FundLedger WHERE RunID = ?) >= ?
                     THEN 1 ELSE 0 END,
                (SELECT COUNT(*) FROM [{worker_db}].simulation.Lease WHERE RunID = ?),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.Properties WHERE RunID = ?),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.PropertyUnits WHERE RunID = ?),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.Household WHERE RunID = ?),
                (SELECT ISNULL(SUM(AdultCount + ChildCount), 0)
                   FROM [{worker_db}].simulation.Household WHERE RunID = ?),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.LeaseTerminationLedger
                   WHERE RunID = ? AND TxnType = 'EVICTION_EXECUTED'),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.Lease
                   WHERE RunID = ? AND RenewalDecision = 'LANDLORD_NONRENEWAL'),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.LeaseTermination
                   WHERE RunID = ? AND TerminationReason LIKE '%early break%'),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.LeaseTermination
                   WHERE RunID = ? AND TerminationReason LIKE '%voluntary exit at lease end%'),
                (SELECT COUNT(*) FROM [{worker_db}].simulation.LeaseTerminationLedger
                   WHERE RunID = ? AND TxnType = 'LEASE_RENEWED'),
                (SELECT ISNULL(SUM(Amount), 0) FROM [{worker_db}].simulation.TenantCreditLedger
                   WHERE RunID = ? AND TransactionType = 'ACCRUAL'),
                (SELECT ISNULL(SUM(-Amount), 0) FROM [{worker_db}].simulation.TenantCreditLedger
                   WHERE RunID = ? AND TransactionType = 'REDEMPTION'),
                (SELECT ISNULL(SUM(-Amount), 0) FROM [{worker_db}].simulation.TenantCreditLedger
                   WHERE RunID = ? AND TransactionType = 'FORFEITURE')
            FROM (
                SELECT TOP 1 CashBalance, CSFBalance, EIPBalance
                FROM [{worker_db}].simulation.FundLedger
                WHERE RunID = ?
                ORDER BY LedgerDate DESC, EventID DESC
            ) AS final_balances
        """, (rung_val, seed, rung, ENGINE_VERSION, worker_db,
              started_utc, completed_utc,
              run_id, months,
              run_id, run_id, run_id, run_id, run_id,
              run_id, run_id, run_id, run_id, run_id,
              run_id, run_id, run_id, run_id))

        # All INSERTs succeeded — commit the entire extract atomically.
        central_conn.commit()
    except Exception:
        # Any failure rolls back ALL v1.* INSERTs for this (rung, seed) →
        # no partial state; next sweep re-processes cleanly.
        central_conn.rollback()
        raise
    finally:
        central_conn.close()


# ---------------------------------------------------------------------------
# Per-(rung, seed) worker function
# ---------------------------------------------------------------------------

def process_pair(rung, seed, months, dry_run=False):
    """The full lifecycle for one (rung, seed). Runs on a worker thread."""
    result = {"rung": rung, "seed": seed, "status": "pending",
              "worker_db": None, "wall_sec": None, "error": None}

    if dry_run:
        result["status"] = "dry_run"
        return result

    if already_done(rung, seed):
        result["status"] = "skipped (already in v1.run_summary)"
        return result

    t0 = time.time()
    try:
        worker_db = build_worker_db()
        result["worker_db"] = worker_db
    except Exception as e:
        result["status"] = "failed (build_worker_db)"
        result["error"] = str(e)
        return result

    try:
        apply_sweep_mode(worker_db)  # static leg: flip worker INF.Mode (no-op on regime)
        seed_extension_projection(worker_db, rung)  # sub-$5M synthetic rungs (no-op otherwise)
    except Exception as e:
        result["status"] = "failed (worker prep); worker DB retained"
        result["error"] = str(e)
        return result

    exit_code, started_utc, completed_utc, log_path = run_sim(worker_db, rung, seed, months)
    if exit_code != 0:
        result["status"] = f"failed (sim exit {exit_code}); worker DB retained"
        result["error"] = f"see log: {log_path}"
        return result

    # Sim succeeded — extract and drop.
    try:
        # Determine RunID inside the worker DB (always 1 for fresh-DB-per-sim).
        with conn(worker_db) as c:
            cur = c.cursor()
            cur.execute("SELECT MAX(RunID) FROM simulation.Run")
            run_id = cur.fetchone()[0]
        if run_id is None:
            result["status"] = "failed (no Run row in worker DB after sim)"
            return result

        # Populate v1.projection_parameters for this rung if not already there.
        # Runs on its own connection so a race with a sibling worker doesn't
        # poison the main extract transaction.
        ensure_projection_parameters(worker_db, rung)

        extract_to_central(worker_db, rung, seed, run_id, started_utc, completed_utc, months)
        release_worker_db(worker_db)
        result["status"] = "completed"
    except Exception as e:
        result["status"] = "failed (extract); worker DB retained"
        result["error"] = str(e)
        return result
    finally:
        result["wall_sec"] = round(time.time() - t0, 1)

    return result


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------

def write_status(stats, pending, completed, failed, started, target):
    elapsed = (time.time() - started) / 60.0
    pct = (completed + failed) / target * 100.0 if target else 0
    eta_min = (elapsed / max(completed, 1)) * pending if completed > 0 else None
    lines = [
        f"Corpus regeneration — status as of {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Target sims:    {target}",
        f"Completed:      {completed}",
        f"Failed:         {failed}",
        f"Pending:        {pending}",
        f"Skipped:        {stats.get('skipped', 0)}",
        f"Progress:       {pct:.1f}%",
        f"Elapsed:        {elapsed:.1f} min",
        f"ETA:            {eta_min:.1f} min" if eta_min else "ETA:            N/A",
        "",
        "Recent failures (worker DB retained for inspection):",
    ]
    for f in stats.get("failures", [])[-10:]:
        lines.append(f"  rung={f['rung']} seed={f['seed']}: {f['status']}; worker={f['worker_db']}; error={f.get('error', '')}")
    with open(STATUS_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def run_sweep(pairs, workers, months, dry_run=False, force_rerun=False):
    """Run the sweep across all (rung, seed) pairs with `workers` concurrent slots."""
    started = time.time()
    if force_rerun:
        for rung, seed in pairs:
            force_clear(rung, seed)

    stats = {"skipped": 0, "failures": []}
    completed = 0
    failed = 0
    target = len(pairs)

    print(f"Starting sweep: {target} pairs, {workers}-parallel, dry_run={dry_run}", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_pair, r, s, months, dry_run): (r, s) for r, s in pairs}
        for fut in concurrent.futures.as_completed(futures):
            r, s = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = {"rung": r, "seed": s, "status": "failed (exception)", "error": str(e), "worker_db": None}

            if "skipped" in result["status"]:
                stats["skipped"] += 1
                completed += 1
            elif result["status"] == "completed":
                completed += 1
            elif result["status"] == "dry_run":
                completed += 1
            else:
                failed += 1
                stats["failures"].append(result)

            pending = target - completed - failed
            print(f"  [{completed+failed}/{target}] rung={r} seed={s}: {result['status']} "
                  f"({result.get('wall_sec', '?')}s)", flush=True)
            write_status(stats, pending, completed, failed, started, target)

    elapsed_min = (time.time() - started) / 60.0
    print(f"\nSweep complete: {completed} done ({stats['skipped']} skipped), "
          f"{failed} failed in {elapsed_min:.1f} min", flush=True)
    return stats, completed, failed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", required=True,
                   help="Target central corpus database to write results into (required)")
    p.add_argument("--seeds", default=None, help="Seed range, e.g. '1-50' or '1,2,3' or '1-3,7'")
    p.add_argument("--rungs", default="all",
                   help="Rung IDs to sweep (default: all sixteen — 200-209 stored, "
                        "300-305 synthetic)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--months", type=int, default=DEFAULT_MONTHS)
    p.add_argument("--smoke-per-rung", type=int, default=None,
                   help="Smoke mode: run N sims per rung (uses seeds 1..N)")
    p.add_argument("--rerun-rung", type=int, default=None)
    p.add_argument("--rerun-seed", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--mode", choices=["regime", "static"], default="regime",
                   help="inflation leg: regime (live Markov inflation) or "
                        "static (flips each worker's global INF.Mode)")
    args = p.parse_args()

    global SWEEP_MODE, CENTRAL_DB
    init_worker_pool(args.workers)
    SWEEP_MODE = args.mode
    CENTRAL_DB = args.corpus
    print(f"Corpus regeneration: mode={SWEEP_MODE}, corpus={CENTRAL_DB}, engine={ENGINE_VERSION}")

    if args.rerun_rung and args.rerun_seed:
        pairs = [(args.rerun_rung, args.rerun_seed)]
        stats, completed, failed = run_sweep(pairs, args.workers, args.months,
                                              dry_run=args.dry_run, force_rerun=True)
    else:
        rungs = parse_rungs(args.rungs)
        if args.smoke_per_rung:
            seeds = list(range(1, args.smoke_per_rung + 1))
        elif args.seeds:
            seeds = parse_seed_range(args.seeds)
        else:
            p.error("Must specify --seeds, --smoke-per-rung, or --rerun-rung/--rerun-seed")
        pairs = [(r, s) for r in rungs for s in seeds]
        stats, completed, failed = run_sweep(pairs, args.workers, args.months, dry_run=args.dry_run)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
