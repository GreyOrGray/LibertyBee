"""Regenerate the Liberty Bee Monte Carlo corpus (the corpus CLI).

The generic runner — worker pool, sim invocation, the run loop, provenance,
pacing, checks, projection resolution — lives in corpus_runner.py and is shared.
This module is the corpus-specific half: the CorpusStore (a full v1.* extract) and
the command-line interface. A second caller (the living farm) supplies a different
Store to the same runner.

    python regenerate_corpus.py --corpus <db> --rungs 200-209,300-305 --seeds 1-50
"""
import argparse
import datetime
import json
import os
import socket
import sys

import pyodbc

from corpus_runner import *          # noqa: F401,F403  the shared generic runner
from corpus_runner import (          # explicit re-import for the names used below,
    CONN_TMPL, DEV_REPO_MARKER, ENGINE_VERSION, REPO, STOP_FLAG,  # so a reader sees the surface
    Store, apply_sweep_mode, build_worker_db, conn, harness_provenance,
    init_worker_pool, load_checks, parse_int_set, release_worker_db, run_sim,
    run_sweep,
)

def bind_scenario(corpus, scenario, sweep_mode, allow_dirty=False, allow_dev_tree=False):
    """Bind this sweep to the corpus's scenario and stamp provenance.

    A corpus holds exactly one scenario. If it already contains runs under a
    different one, abort: the failure mode this prevents is resuming a sweep
    without --scenario, silently appending standard-affordability runs to a
    deep-discount corpus and blending two populations into a single dataset that
    looks entirely normal.

    Also refuses to generate a corpus of record from a modified working tree,
    because such a corpus cannot be reproduced from any published commit.
    """
    commit, dirty, root, origin = harness_provenance()

    if origin and DEV_REPO_MARKER in origin and not allow_dev_tree:
        raise SystemExit(
            f"REFUSING: this is the development tree ({origin}).\n"
            f"          Corpora of record are generated from a PROMOTED checkout, so that\n"
            f"          what ran is a published commit rather than whatever the dev tree\n"
            f"          happened to contain. Promote the branch, check it out elsewhere,\n"
            f"          and run the sweep from there.\n"
            f"          --allow-dev-tree overrides this for smoke tests only.")

    if dirty and not allow_dirty:
        raise SystemExit(
            f"REFUSING: the harness tree at {root} has uncommitted changes.\n"
            f"          A corpus generated from a modified tree cannot be reproduced\n"
            f"          from any published commit, so its provenance is unverifiable.\n"
            f"          Commit the changes, or pass --allow-dirty for a throwaway run\n"
            f"          (the corpus will be permanently marked HarnessDirty=1).")

    with conn(corpus) as c:
        cur = c.cursor()
        cur.execute("SELECT DISTINCT Scenario FROM v1.corpus_meta")
        seen = sorted(r[0] for r in cur.fetchall())
        if seen and scenario not in seen:
            raise SystemExit(
                f"REFUSING: corpus [{corpus}] already holds runs under scenario "
                f"{seen!r},\n          but this sweep requested '{scenario}'. A corpus is "
                f"single-scenario;\n          blending them would silently corrupt the dataset.\n"
                f"          Use a different corpus database, or name projections tagged {seen[0]!r}.")

        cur.execute("""
            INSERT INTO v1.corpus_meta
              (Scenario, SweepMode, EngineVersion, HarnessCommit, HarnessDirty,
               HarnessRoot, HostName, StartedUTC)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (scenario, sweep_mode, ENGINE_VERSION, commit, 1 if dirty else 0,
              str(root), socket.gethostname(), datetime.datetime.utcnow()))

    return commit, dirty


def already_done(central_db, funds_tag, seed):
    """Check v1.run_summary for an existing row for (funds_tag, seed)."""
    with conn(central_db) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT 1 FROM v1.run_summary WHERE Rung = ? AND Seed = ?",
            (funds_tag, seed),
        )
        return cur.fetchone() is not None


def force_clear(central_db, funds_tag, seed):
    """Remove any existing v1.* rows for (funds_tag, seed). For --rerun support.

    Does NOT clear v1.projection_parameters — that table is keyed by ProjectionID,
    not (Rung, Seed), and is locked-identical across all sims of a given rung.
    A force-rerun should leave it alone.
    """
    with conn(central_db) as c:
        cur = c.cursor()
        rung_val = funds_tag
        for tbl in ("run_summary", "fund_ledger", "tcs_ledger", "lease",
                    "lease_termination", "lease_termination_ledger", "event_summary",
                    "household", "properties", "property_units", "inflation_schedule",
                    # per-lease / staffing reviewability extracts:
                    "monthly_payment_status", "employees", "payroll"):
            cur.execute(f"DELETE FROM v1.{tbl} WHERE Rung = ? AND Seed = ?", (rung_val, seed))


def ensure_projection_parameters(central_db, worker_db, projection_id, sweep_mode):
    """Populate v1.projection_parameters for `projection_id` from the WORKER's
    reference registry, resolved override-else-default (the legacy wide
    reference.ProjectionParameters table is gone as of V00070). InflationMode
    records the leg (sweep_mode) directly."""
    with conn(central_db) as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM v1.projection_parameters WHERE ProjectionID = ?", (projection_id,))
        if cur.fetchone() is not None:
            return  # already populated by a sibling worker
    wanted = {
        ("FIN", "StartingFunds"),
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
        # Override-else-default across the split tables (V00071); IsOverride marks
        # which side a row came from so precedence does not depend on row order.
        wcur.execute("""
            SELECT Category, Name, Value, 0 AS IsOverride
            FROM reference.ParameterRegistryDefault
            UNION ALL
            SELECT Category, Name, Value, 1 AS IsOverride
            FROM reference.ParameterRegistryDefined
            WHERE ProjectionID = ?
        """, (projection_id,))
        for cat, name, val, is_override in wcur.fetchall():
            key = (cat, name)
            if key not in wanted:
                continue
            if is_override:
                resolved[key] = val
                overrides.add(key)
            elif key not in overrides:
                resolved[key] = val
        # The projection NAME is identity and lives on the entity, not among the
        # parameters (V00071).
        nm = wcur.execute(
            "SELECT Name FROM reference.Projection WHERE ProjectionID = ?",
            (projection_id,)).fetchone()
        resolved[("SIM", "ProjectionName")] = nm[0] if nm else None
    def g(cat, name):
        return resolved.get((cat, name))
    try:
        with conn(central_db) as c:
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
                  "Static" if sweep_mode == "static" else "Regime",
                  g("RR", "FirstReductionMonths"), g("RR", "FirstReductionPct"),
                  g("RR", "SecondReductionMonths"), g("RR", "SecondReductionPct"),
                  g("RR", "ThirdReductionMonths"), g("RR", "ThirdReductionPct"),
                  g("RR", "FourthReductionMonths"), g("RR", "FourthReductionPct"),
                  g("RR", "FifthReductionMonths"), g("RR", "FifthReductionPct")))
    except pyodbc.IntegrityError:
        pass  # sibling worker raced us and won - same end state, harmless


def extract_to_central(central_db, worker_db, rung, funds_tag, seed, run_id,
                       started_utc, completed_utc, months=DEFAULT_MONTHS):
    """Copy result rows from worker.simulation.* into the corpus DB's v1.*
    tables tagged (Rung, Seed). Also computes v1.run_summary.

    All INSERTs are wrapped in a single transaction (autocommit=False). If any
    INSERT fails, the entire extract rolls back — so v1.run_summary remains
    absent and the driver's skip-already-done check correctly re-processes
    this (rung, seed) on the next sweep call. No partial state in v1.* tables.
    """
    rung_val = funds_tag
    # Cross-DB inserts via 4-part naming: [db].[schema].[table].
    # autocommit=False so the extract is atomic: all-or-nothing.
    central_conn = pyodbc.connect(CONN_TMPL.format(central_db), timeout=30, autocommit=False)
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
              (Rung, Seed, UnitID, PropertyID, Beds, Baths, BaseRent, AdjustedRent)
            SELECT ?, ?, UnitID, PropertyID, Beds, Baths, BaseRent, AdjustedRent
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

        # ----- v1.compliance (per-work-item detail) ---------------------
        # Event summary aggregates cannot separate a compliance item's CASH cost
        # from its OCCUPANCY-DELAY cost (a unit is unrentable while work is open).
        # Lead abatement needs exactly that split, so each work item is recorded
        # individually rather than rolled up.
        cur.execute(f"""
            INSERT INTO v1.compliance
              (Rung, Seed, WorkItemID, PropertyID, UnitID, WorkType, Status, Severity,
               CostEstimate, ActualCost, DurationDays, StartDate, CompletedDate)
            SELECT ?, ?, WorkItemID, PropertyID, UnitID, WorkType, Status, Severity,
                   CostEstimate, ActualCost, DurationDays, StartDate, CompletedDate
            FROM [{worker_db}].simulation.ComplianceWorkItem
            WHERE RunID = ?
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

# ---------------------------------------------------------------------------
# Store adapter — the seam between the generic runner and where results land.
#
# The runner (worker build, sim, checks, the loop) knows nothing about the result
# schema. A Store decides how a completed run is recorded and how "already done"
# is judged, so the same runner can fill the corpus (a full v1.* extract) or,
# later, the living-farm summary — the only difference is which Store is passed.
# retain=full|summary is not a runner flag; it is which Store you construct.
# ---------------------------------------------------------------------------

class CorpusStore(Store):
    """The corpus of record: a full v1.* extract tagged (Rung, Seed), plus
    v1.projection_parameters and v1.corpus_meta. Holds all corpus-specific state
    so the generic runner carries none of it.

    Workers (design B): built beside the seed database on the default instance,
    via this repo's migration_manager — the corpus legitimately runs next to
    Gold, which the farm's store must never do. Two-phase: construct, then
    resolve(rungs) reads the ladder from the seeded data, then bind().
    """
    def __init__(self, central_db, sweep_mode):
        self.central_db = central_db
        self.sweep_mode = sweep_mode
        self.rung_funds = None    # set by resolve()
        self.scenario = None      # set by resolve()

    # --- workers: the local (default-instance) backend ----------------------
    def build_worker(self):
        return build_worker_db()

    def release_worker(self, worker_db):
        release_worker_db(worker_db)

    def worker_conn(self, worker_db):
        return conn(worker_db)

    def run_sim(self, worker_db, rung, seed, months):
        return run_sim(worker_db, rung, seed, months, self.sweep_mode)

    # --- results -------------------------------------------------------------
    def central_conn(self):
        return conn(self.central_db)

    def already_done(self, rung, seed):
        return already_done(self.central_db, self.rung_funds[rung], seed)

    def prepare_worker(self, worker_db, rung):
        apply_sweep_mode(worker_db, self.sweep_mode)
        self.verify_projection(worker_db, rung)

    def record(self, worker_db, rung, seed, run_id, started_utc, completed_utc, months):
        # projection_parameters first (own connection, race-safe), then the extract
        ensure_projection_parameters(self.central_db, worker_db, rung, self.sweep_mode)
        extract_to_central(self.central_db, worker_db, rung, self.rung_funds[rung],
                           seed, run_id, started_utc, completed_utc, months)

    def clear(self, rung, seed):
        force_clear(self.central_db, self.rung_funds[rung], seed)

    def bind(self, allow_dirty=False, allow_dev_tree=False):
        return bind_scenario(self.central_db, self.scenario, self.sweep_mode,
                             allow_dirty=allow_dirty, allow_dev_tree=allow_dev_tree)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", required=True,
                   help="Target central corpus database to write results into (required)")
    p.add_argument("--seeds", default=None,
                   help="Seeds, mixed list + ranges: '1-50' or '1,2,3' or '1-3,7,10-15'")
    p.add_argument("--rungs", default=None,
                   help="Projection IDs to run, mixed list + ranges: "
                        "'200-209,300-305' or '206' or '100,125,130-150'. Required — "
                        "there is no default ladder; the recipe states what actually "
                        "ran (see REPRODUCE.md for the published-corpus set).")
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
    p.add_argument("--scenario", default=None,
                   help="OPTIONAL assertion of the affordability scenario. The user "
                        "names projections via --rungs; the scenario is READ from their "
                        "ScenarioTag and required to be single-valued. Passing "
                        "--scenario asserts that resolved value matches — a guard "
                        "against naming the wrong projections, not a selector.")
    p.add_argument("--allow-dirty", action="store_true",
                   help="permit generation from a modified working tree; the corpus "
                        "is permanently marked HarnessDirty=1 (throwaway runs only)")
    p.add_argument("--allow-dev-tree", action="store_true",
                   help="permit generation from the development checkout (smoke tests "
                        "only; a corpus of record must come from a promoted checkout)")
    p.add_argument("--throttle", action="store_true",
                   help="yield to interactive use: drip to 1 worker on input, blast "
                        "only after sustained idle. Affects scheduling only, never "
                        "results. (app/src/pacing.py; Windows-only, like the engine.)")
    p.add_argument("--blast-after", type=float, default=600.0,
                   help="seconds of sustained idle before blasting (with --throttle)")
    p.add_argument("--cpu-max", type=float, default=25.0,
                   help="max system CPU%% to enter blast (with --throttle)")
    p.add_argument("--drip-gap", type=float, default=20.0,
                   help="inter-run gap while dripping (with --throttle)")
    p.add_argument("--checks", default=None,
                   help="comma-separated corpus checks to run during the sweep, or "
                        "'all' / 'none'. Overrides corpus_checks/checks.json. "
                        "See corpus_checks/README.md.")
    p.add_argument("--check-every", type=int, default=None,
                   help="run enabled checks after every N completed runs "
                        "(default: the 'every' value in checks.json, else 250)")
    args = p.parse_args()

    init_worker_pool(args.workers)
    central_db = args.corpus
    sweep_mode = args.mode
    # The rung->funds map and the scenario are resolved from the seeded data below,
    # once the rung set is known, and handed to the CorpusStore — no module globals.

    # Imported lazily and only when asked for: the pacer is Windows-API-bound
    # (GetLastInputInfo), so nothing else in the harness should load it.
    pacer = None
    if args.throttle:
        sys.path.insert(0, os.path.join(REPO, "app", "src"))
        try:
            import pacing
        except ImportError as e:
            p.error(f"--throttle needs app/src/pacing.py, which is not "
                    f"importable from {REPO} ({e}). Re-run without --throttle.")
        pacer = pacing.Pacer(blast_after_s=args.blast_after, cpu_max_pct=args.cpu_max)

    # Checks: --checks wins over checks.json; 'none' disables entirely.
    if args.checks is None:
        check_names = None
    elif args.checks.strip().lower() == "none":
        check_names = []
    else:
        check_names = [n.strip() for n in args.checks.split(",") if n.strip()]
    checks = load_checks(check_names)

    check_every = args.check_every
    if check_every is None:
        cfg_path = os.path.join(CHECKS_DIR, "checks.json")
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                check_every = int(json.load(fh).get("every", 250))
        except (OSError, ValueError, TypeError) as e:
            if checks:  # only worth mentioning if checks will actually run
                print(f"  [checks] could not read 'every' from {cfg_path} ({e}); "
                      f"defaulting to 250", flush=True)
            check_every = 250
    if check_every < 1:
        p.error(f"--check-every must be >= 1 (got {check_every})")

    if STOP_FLAG.exists():
        p.error(f"{STOP_FLAG} exists from a previous drain — remove it before starting.")

    # --- determine the rung + seed set (both paths) -----------------------------
    force_rerun = bool(args.rerun_rung and args.rerun_seed)
    if force_rerun:
        rungs = [args.rerun_rung]
        seeds = [args.rerun_seed]
    else:
        if not args.rungs:
            p.error("--rungs is required (e.g. '200-209,300-305'); there is no default "
                    "ladder. See REPRODUCE.md for the published-corpus set.")
        rungs = parse_int_set(args.rungs)
        if args.smoke_per_rung:
            seeds = list(range(1, args.smoke_per_rung + 1))
        elif args.seeds:
            seeds = parse_int_set(args.seeds)
        else:
            p.error("Must specify --seeds, --smoke-per-rung, or --rerun-rung/--rerun-seed")

    # --- dry-run: print the plan and stop, with no side effects -----------------
    # A dry-run does not build a probe or stamp corpus_meta; it reports what would
    # run. Funds/scenario resolution needs a live environment, so a dry-run shows
    # projection ids rather than funding amounts.
    if args.dry_run:
        print(f"DRY RUN: corpus={central_db}, mode={sweep_mode}, engine={ENGINE_VERSION}")
        print(f"  {len(rungs)} projection(s) x {len(seeds)} seed(s) = "
              f"{len(rungs) * len(seeds)} pairs")
        print(f"  projections: {rungs}")
        print(f"  (funds/scenario are resolved from the seeded data on a real run)")
        sys.exit(0)

    # --- resolve funds + scenario from the SEEDED DATA (the store's own probe) ---
    store = CorpusStore(central_db, sweep_mode)
    rung_funds, scenario = store.resolve(rungs, args.scenario)

    commit, dirty = store.bind(allow_dirty=args.allow_dirty, allow_dev_tree=args.allow_dev_tree)

    print(f"Corpus regeneration: corpus={central_db}, scenario={scenario}, "
          f"mode={sweep_mode}, engine={ENGINE_VERSION}")
    print(f"  rungs:   {len(rung_funds)} projections {sorted(rung_funds)} "
          f"(funds + scenario read from the seeded data, verified per worker)")
    print(f"  harness: {commit[:12] if commit else 'unknown (not a git checkout)'}"
          f"{'  *** DIRTY TREE ***' if dirty else ''}  @ {REPO}")
    print(f"  checks:  {', '.join(n for n, _ in checks) if checks else 'none'}"
          f"{f' (every {check_every} runs)' if checks else ''}")

    # Seed-major: every rung gets seed 1, then every rung gets seed 2, and so on.
    # For a bounded corpus that completes, order does not change the final contents
    # — but it makes the corpus grow BALANCED across rungs rather than filling one
    # rung fully before the next. That is the farm's "even growth" for free, with no
    # allocator, and it also lets the periodic checks see all rungs early instead of
    # only the first. Each sim is independent (fresh worker, seeded by (proj, seed)),
    # so execution order never affects any individual result.
    pairs = [(r, s) for s in seeds for r in rungs]
    stats, completed, failed, halted_by = run_sweep(pairs, args.workers, args.months, store,
                                          dry_run=False, force_rerun=force_rerun,
                                          pacer=pacer, drip_gap=args.drip_gap,
                                          checks=checks, check_every=check_every)

    # Exit code distinguishes "some sims failed" from "a check stopped the sweep":
    # a halted sweep can have zero failures and still be a corpus you must not use.
    if halted_by:
        print(f"\nExiting 2: sweep was halted by check '{halted_by}'. The corpus is "
              f"INCOMPLETE — investigate before freezing or citing it.")
        sys.exit(2)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
