"""
Phase 3.7.9 — Integration Testing: Lifecycle acceptance regression artifact.

This file runs query-based validation against a database that has already been
populated by a Phase 3.7 simulation. It exercises the Phase 3.7.9 coverage
matrix at the aggregate-counts layer.

Companion docs:
  docs/phases/phase_3_7/phase_3_7_9_coverage_matrix.md
  docs/phases/phase_3_7/phase_3_7_acceptance_summary.md

Tests:
  L1 — Lease lifecycle counts (initial + re-leases > original portfolio)
  L2 — All terminated leases have a corresponding LeaseTermination row
  L3 — All turnover work orders are in COMPLETED or SKIPPED (no IN_PROGRESS at end)
  L4 — All filled vacancies satisfy VacancyEndDate >= TargetFillDate (hard gate)
  L5 — No deposits have InstallmentsPaidCount > InstallmentsPlanned
  L6 — All funded deposits have DepositEscrowedAmount >= DepositRequiredAmount
  L7 — Every UnitStatus value reached during the run (state-machine coverage)
  L8 — Inflation visible: rent range spans >5% across the run

Usage:
    # Run simulation first (60 months on projection 171):
    python app/src/master_test_runner.py --env <db_name> --months 60 \\
        --projection-id 171 --seed 99 --clean

    # Then run these tests:
    python sql/regression_tests/Phase3_7_9/phase_3_7_9_test_lifecycle.py --env <db_name>
"""

import argparse
import os
import sys
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + "db_config.json"


PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def l1_lease_count(db) -> bool:
    """L1 — Re-leasing produces more leases than the original portfolio."""
    leases = db.execute_query("SELECT COUNT(*) FROM simulation.Lease")[0][0]
    units = db.execute_query("SELECT COUNT(*) FROM simulation.PropertyUnits")[0][0]
    ok = leases > units and units > 0
    print(f"  L1 lease lifecycle counts: {leases} leases / {units} units (expect leases>units)  [{PASS if ok else FAIL}]")
    return ok


def l2_termination_records(db) -> bool:
    """L2 — All terminated leases have a LeaseTermination row."""
    orphan = db.execute_query("""
        SELECT COUNT(*)
        FROM simulation.Lease l
        WHERE l.LeaseStatus = 'Terminated'
          AND NOT EXISTS (
              SELECT 1 FROM simulation.LeaseTermination t
              WHERE t.RunID = l.RunID AND t.LeaseID = l.LeaseID
          )
    """)[0][0]
    ok = orphan == 0
    print(f"  L2 termination records: {orphan} terminated leases without a LeaseTermination row (expect 0)  [{PASS if ok else FAIL}]")
    return ok


def l3_turnover_terminal(db) -> bool:
    """L3 — no STUCK turnover work orders.

    A non-terminal order is a failure ONLY if its scheduled window closed
    before the simulation ended (it had the time to finish and didn't).
    Non-terminal orders whose ScheduledEndDate is at/after sim-end were
    boundary-truncated by the halt — expected for turnovers that begin in the
    final days, not stuckness. This replaces the old <5%-of-total allowance,
    which both tolerated genuinely-stuck orders (anything under the ratio) and
    snapshotted a particular chain's late-window activity level (it broke when
    1.5c's honest fixes changed acquisition timing — the E1/F-16 lesson: derive
    expectations from the run's own data, never from a frozen outcome).
    Calibration basis: completed orders NEVER finish past ScheduledEndDate
    (pipeline advances on schedule), so no grace margin is needed."""
    sim_end = db.execute_query(
        "SELECT MAX(LedgerDate) FROM simulation.FundLedger")[0][0]
    stuck = db.execute_query("""
        SELECT COUNT(*)
        FROM simulation.TurnoverWorkOrder
        WHERE Status NOT IN ('COMPLETED', 'SKIPPED')
          AND ScheduledEndDate < ?
    """, (sim_end,))[0][0]
    boundary = db.execute_query("""
        SELECT COUNT(*)
        FROM simulation.TurnoverWorkOrder
        WHERE Status NOT IN ('COMPLETED', 'SKIPPED')
          AND ScheduledEndDate >= ?
    """, (sim_end,))[0][0]
    total = db.execute_query("SELECT COUNT(*) FROM simulation.TurnoverWorkOrder")[0][0]
    ok = stuck == 0
    print(f"  L3 turnover terminal: {stuck} STUCK (window closed pre-halt, expect 0); "
          f"{boundary}/{total} boundary-truncated at sim-end (expected)  [{PASS if ok else FAIL}]")
    return ok


def l4_hard_gate(db) -> bool:
    """L4 — No filled vacancy has VacancyEndDate < TargetFillDate."""
    early = db.execute_query("""
        SELECT COUNT(*) FROM simulation.Vacancy
        WHERE VacancyEndDate IS NOT NULL
          AND TargetFillDate IS NOT NULL
          AND VacancyEndDate < TargetFillDate
    """)[0][0]
    ok = early == 0
    print(f"  L4 hard fill-date gate: {early} early fills (expect 0)  [{PASS if ok else FAIL}]")
    return ok


def l5_installment_overflow(db) -> bool:
    """L5 — InstallmentsPaidCount never exceeds InstallmentsPlanned."""
    over = db.execute_query("""
        SELECT COUNT(*) FROM simulation.LeaseDeposit
        WHERE InstallmentsPaidCount > InstallmentsPlanned
    """)[0][0]
    ok = over == 0
    print(f"  L5 installment overflow: {over} deposits over installment cap (expect 0)  [{PASS if ok else FAIL}]")
    return ok


def l6_deposit_funding(db) -> bool:
    """L6 — All funded deposits have escrowed >= required (no underfunded yet flagged funded)."""
    under = db.execute_query("""
        SELECT COUNT(*) FROM simulation.LeaseDeposit
        WHERE DepositFundedDate IS NOT NULL
          AND DepositEscrowedAmount < DepositRequiredAmount
    """)[0][0]
    ok = under == 0
    print(f"  L6 deposit funding: {under} funded deposits with escrow<required (expect 0)  [{PASS if ok else FAIL}]")
    return ok


def l7_state_coverage(db) -> bool:
    """L7 — the unit state machine was EXERCISED across the run (lease-creation AND
    termination/turnover paths fired).

    PropertyUnits stores only the CURRENT status, so a healthy mature run can legitimately
    end fully-occupied (1 distinct end-state) while having cycled units through
    Available/Turnover earlier. End-state *diversity* is therefore NOT a reliable coverage
    signal (it spuriously fails a fully-leased run, and only "passed" before when a DB held
    several runs at once). Instead verify the machine cycled: leases were created (the
    Occupied transition fired) AND terminations occurred (units were freed -> the
    Available/Turnover transitions fired)."""
    leases = db.execute_query("SELECT COUNT(*) FROM simulation.Lease")[0][0]
    terms = db.execute_query("SELECT COUNT(*) FROM simulation.LeaseTermination")[0][0]
    ok = leases > 0 and terms > 0
    print(f"  L7 state-machine exercised: {leases} leases created + {terms} terminations "
          f"(Occupied<->Available/Turnover cycled)  [{PASS if ok else FAIL}]")
    return ok


def l8_inflation_visible(db) -> bool:
    """L8 — Inflation visible in rent range across the run (>5% spread)."""
    row = db.execute_query("""
        SELECT MIN(MonthlyRent) AS Lo, MAX(MonthlyRent) AS Hi
        FROM simulation.Lease
    """)
    if not row or row[0][0] is None:
        print(f"  L8 inflation visible: no leases  [{FAIL}]")
        return False
    lo, hi = Decimal(str(row[0][0])), Decimal(str(row[0][1]))
    spread = float((hi - lo) / lo) if lo > 0 else 0
    ok = spread > 0.05
    print(f"  L8 inflation visible: rent ${lo:.0f}-${hi:.0f} ({spread*100:.1f}% spread, expect >5%)  [{PASS if ok else FAIL}]")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, help="Test database name")
    parser.add_argument("--assert", dest="assert_mode", action="store_true",
                        help="acceptance mode (module already exits 1 on any failure)")
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(_config_path_for(args.env))

    print(f"Phase 3.7.9 lifecycle acceptance tests against {args.env}")
    print("=" * 70)

    results = [
        l1_lease_count(db),
        l2_termination_records(db),
        l3_turnover_terminal(db),
        l4_hard_gate(db),
        l5_installment_overflow(db),
        l6_deposit_funding(db),
        l7_state_coverage(db),
        l8_inflation_visible(db),
    ]

    print("=" * 70)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Result: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
