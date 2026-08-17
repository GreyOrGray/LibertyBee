"""
Phase 1.2.5 — Post-termination billing lifecycle invariants (#120 guard).

A lease that is removed MID-TERM (eviction execution, or an early lease break — both
with an effective exit date BEFORE LeaseEndDate) must not be billed new rent, or accrue
TCS, for any month after the tenant is gone. End-of-term exits (voluntary-at-end /
landlord non-renewal) occur AT LeaseEndDate and bill normally through their final month.

This is the permanent regression guard for #120 (rent + TCS continued on evicted /
early-broken leases for months after the tenant left — $1.07M phantom rent + $49K
phantom TCS under the proj-220 stress sweep). It is the first instance of the standing
"adversarial / lifecycle-invariant coverage" standard (epic #122): a per-entity invariant
that catches an emergent bug aggregate metrics hide.

Scenario-agnostic: the "mid-lease exit" set is derived from the run's own LeaseTermination
data (effective exit < LeaseEndDate), so it holds at baseline (where it's a no-op, ~0
mid-lease exits) and under any stress projection.

Invariants (scoped to the latest RunID):
  I1 — no MonthlyPaymentStatus row with AmountDue > 0 for a BillingMonth after a lease's
       mid-lease exit (no NEW rent obligation once the tenant is removed).
  I2 — no TCS ACCRUAL ledger row dated after a lease's mid-lease exit (no phantom accrual).
  I3 — no FORFEITED TCS balance left non-zero with no FORFEITURE ledger row (the original
       #120 symptom — a forfeiture decided but never applied/recorded).

Usage:
    python sql/regression_tests/Phase1_2_5/phase_1_2_5_test_post_termination_billing.py --env <db> --assert
"""
from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))  # PhaseX -> regression_tests -> sql -> repo
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

# Mid-lease exits: a termination whose effective exit (eviction execution date, else the
# termination date) falls strictly before the lease's scheduled LeaseEndDate.
_MIDLEASE_CTE = """
    WITH midlease AS (
        SELECT lt.LeaseID,
               COALESCE(lt.EvictionExecutionDate, lt.TerminationDate) AS eff_exit
        FROM simulation.LeaseTermination lt
        JOIN simulation.Lease l ON l.RunID = lt.RunID AND l.LeaseID = lt.LeaseID
        WHERE lt.RunID = ?
          AND COALESCE(lt.EvictionExecutionDate, lt.TerminationDate) < l.LeaseEndDate
    )
"""


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + "db_config.json"


def i1_no_new_rent_after_midlease_exit(db, run_id: int) -> bool:
    rows = db.execute_query(
        _MIDLEASE_CTE + """
        SELECT COUNT(*)
        FROM simulation.MonthlyPaymentStatus mps
        JOIN midlease m ON m.LeaseID = mps.LeaseID
        WHERE mps.RunID = ? AND mps.BillingMonth > m.eff_exit AND mps.AmountDue > 0
        """,
        (run_id, run_id),
    )
    n = rows[0][0] if rows else 0
    ok = n == 0
    print(f"  I1 no new rent obligation after mid-lease exit: {n} offending MPS rows "
          f"(AmountDue>0 after effective exit; expect 0)  [{PASS if ok else FAIL}]")
    return ok


def i2_no_tcs_accrual_after_midlease_exit(db, run_id: int) -> bool:
    # Attribute each ACCRUAL to the SPECIFIC lease it accrued on (via the rent collection),
    # not to any lease the household ever held — otherwise a legitimate accrual on a
    # household's LATER lease could false-positive against an earlier lease's mid-lease exit
    # (lb-code-review #120 Finding 2). TenantCreditLedger is household-keyed; RelatedCollectionID
    # → RentCollection.LeaseID pins it to the lease.
    rows = db.execute_query(
        _MIDLEASE_CTE + """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.RentCollection rc
             ON rc.RunID = tcl.RunID AND rc.CollectionID = tcl.RelatedCollectionID
        JOIN midlease m ON m.LeaseID = rc.LeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'ACCRUAL' AND tcl.TransactionDate > m.eff_exit
        """,
        (run_id, run_id),
    )
    n = rows[0][0] if rows else 0
    ok = n == 0
    print(f"  I2 no TCS accrual after mid-lease exit: {n} offending ACCRUAL rows "
          f"(after effective exit; expect 0)  [{PASS if ok else FAIL}]")
    return ok


def i3_forfeiture_applied(db, run_id: int) -> bool:
    """A FORFEITED balance must be applied: zeroed + a FORFEITURE ledger row (the #120
    symptom was FORFEITED status set with a non-zero balance and no ledger row)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditBalance tcb
        WHERE tcb.RunID = ? AND tcb.ExpiryStatus = 'FORFEITED' AND tcb.CurrentBalance > 0
          AND NOT EXISTS (
              SELECT 1 FROM simulation.TenantCreditLedger l
              WHERE l.RunID = tcb.RunID AND l.HouseholdID = tcb.HouseholdID
                AND l.TransactionType = 'FORFEITURE'
          )
        """,
        (run_id,),
    )
    n = rows[0][0] if rows else 0
    ok = n == 0
    print(f"  I3 forfeiture applied (no FORFEITED w/ non-zero balance + no ledger row): "
          f"{n} offending balances (expect 0)  [{PASS if ok else FAIL}]")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, help="Environment / DB name under environments/")
    parser.add_argument("--assert", dest="assert_mode", action="store_true",
                        help="acceptance mode (module exits 1 on any failure)")
    parser.add_argument("--run-id", type=int, default=None, help="RunID to validate (default: latest)")
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    db = DatabaseManager(_config_path_for(args.env))

    run_id = args.run_id
    if run_id is None:
        row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
        run_id = row[0][0] if row and row[0][0] is not None else 1

    # How many mid-lease exits does this run actually contain? (context only — the
    # invariants hold trivially at 0, so the module never SKIPs.)
    mc = db.execute_query(
        _MIDLEASE_CTE + "SELECT COUNT(*) FROM midlease", (run_id,)
    )
    n_midlease = mc[0][0] if mc else 0

    print(f"Phase 1.2.5 post-termination billing invariants against {args.env} "
          f"(RunID {run_id}, {n_midlease} mid-lease exits)")
    print("=" * 78)

    results = [
        i1_no_new_rent_after_midlease_exit(db, run_id),
        i2_no_tcs_accrual_after_midlease_exit(db, run_id),
        i3_forfeiture_applied(db, run_id),
    ]

    print("=" * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Result: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
