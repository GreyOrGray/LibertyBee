"""
Phase 3.9.5 — TCS Exit Redemption & Credit Expiry Correctness: Regression Tests.

Outcome-level black-box tests for the four Kate-specified scenarios. Run
against a fresh sim that exercises both lease-end and early-break exits.

  E1  Clean lease-end exit with eligible TCS balance — exit redemption fires
  E2  Clean early-break exit with eligible TCS balance — exit redemption fires (NEW)
  E3  Active household carrying a balance must NOT have credit expired
  E4  Exited household: expiry forfeiture fires on/after exit + 24 months

E2 is the central regression for the Phase 3.9.5 bug fix. Pre-fix, this count
is zero. Post-fix, every clean (non-arrears) early-break exit with a balance
>= EffectiveMonthlyRent + no prior calendar-year redemption should produce a
FINAL_MONTH_EXIT redemption.

Tests SKIP gracefully when the run doesn't exercise a pathway (e.g. a sim
with no early breaks at all).

Usage:
    python sql/regression_tests/Phase3_9_5/phase_3_9_5_test_exit_redemption.py --env <db> [--run-id N] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_PROMOTED = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
if os.path.basename(_REPO_ROOT_PROMOTED) == 'scratch':
    REPO_ROOT = os.path.dirname(_REPO_ROOT_PROMOTED)
    APP_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
else:
    REPO_ROOT = _REPO_ROOT_PROMOTED
    APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')
sys.path.insert(0, APP_SRC)
sys.path.insert(0, os.path.join(REPO_ROOT, 'sql', 'regression_tests'))
from _dialect import add_months_sql, add_days_sql, month_diff_sql  # noqa: E402


ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
SKIP = '\033[93mSKIP\033[0m'


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _resolve_run_id(db, override) -> int:
    if override is not None:
        return int(override)
    row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
    if not row or row[0][0] is None:
        raise SystemExit("No runs found in simulation.Run; cannot pick a RunID.")
    return int(row[0][0])


# ---------------------------------------------------------------------------
# E1 — Clean lease-end exit with eligible balance: redemption fires
# ---------------------------------------------------------------------------

def e1_lease_end_exit_redemption_fires(db, run_id: int) -> bool:
    """For voluntary lease-end exits with an eligible TCS balance, the
    deterministic final-month exit redemption (P=1.0) must fire.

    Defines 'eligible' as: RenewalDecision='VOLUNTARY_EXIT' (or
    'LANDLORD_NONRENEWAL'), termination row written, ArrearsAtExit=0, and
    CurrentBalance >= EffectiveMonthlyRent at the time of exit (approximated
    as the running balance from the ledger immediately before the
    TerminationDate).

    SKIP if the run has no lease-end voluntary exits at all.
    """
    eligible_exits = db.execute_query(
        """
        SELECT t.LeaseID, l.HouseholdID, l.EffectiveMonthlyRent, t.TerminationDate
        FROM simulation.LeaseTermination t
        JOIN simulation.Lease l ON l.RunID = t.RunID AND l.LeaseID = t.LeaseID
        WHERE t.RunID = ?
          AND t.TerminationReason = 'Tenant voluntary exit at lease end'
          AND COALESCE(t.ArrearsAtExit, 0) = 0
          AND l.RenewalDecision IN ('VOLUNTARY_EXIT', 'LANDLORD_NONRENEWAL')
        """,
        (run_id,),
    )
    if not eligible_exits:
        print(f"  E1 Lease-end exit redemption (eligible): "
              f"no lease-end voluntary exits in this run [{SKIP}]")
        return True

    # For each candidate, was a FINAL_MONTH_EXIT redemption recorded on/near
    # the termination date for the same household?
    redeemed = 0
    eligible = 0
    for lease_id, household_id, rent, term_date in eligible_exits:
        bal_rows = db.execute_query(
            """
            SELECT BalanceAfter FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND HouseholdID = ? AND TransactionDate < ?
            ORDER BY TransactionDate DESC, CreditTransactionID DESC
            OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """,
            (run_id, household_id, term_date),
        )
        if not bal_rows or bal_rows[0][0] is None:
            continue
        balance_at_exit = bal_rows[0][0]
        if balance_at_exit < rent:
            continue
        # Skip households that already redeemed in the same calendar year via
        # any non-FME path. The annual-limit rule (Kate Q12 §5.2 condition 3)
        # correctly blocks exit redemption when routine/hardship took the slot,
        # including same-day-as-termination — e.g. when rent_collection routine-
        # redeems on the morning of the lease-end day, exit redemption is
        # ineligible later that same day. Compare by YEAR() only, no date order.
        prior = db.execute_query(
            """
            SELECT COUNT(*) FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND HouseholdID = ? AND TransactionType = 'REDEMPTION'
              AND YEAR(TransactionDate) = YEAR(?)
              AND Notes NOT LIKE '%FINAL_MONTH_EXIT%'
            """,
            (run_id, household_id, term_date),
        )
        if prior and prior[0][0] > 0:
            continue
        eligible += 1
        red = db.execute_query(
            f"""
            SELECT COUNT(*) FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND HouseholdID = ? AND TransactionType = 'REDEMPTION'
              AND Notes LIKE '%FINAL_MONTH_EXIT%'
              AND TransactionDate BETWEEN {add_days_sql(db, -31, '?')} AND ?
            """,
            (run_id, household_id, term_date, term_date),
        )
        if red and red[0][0] > 0:
            redeemed += 1

    if eligible == 0:
        print(f"  E1 Lease-end exit redemption (eligible): "
              f"no eligible lease-end exits had balance >= rent and no prior-year redemption [{SKIP}]")
        return True
    ok = (redeemed == eligible)
    print(f"  E1 Lease-end exit redemption (P=1.0 when eligible): "
          f"{redeemed}/{eligible} eligible exits redeemed [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# E2 — Clean early-break exit with eligible balance: redemption fires (NEW)
# ---------------------------------------------------------------------------

def e2_early_break_exit_redemption_fires(db, run_id: int) -> bool:
    """**Phase 3.9.5 central regression.** For voluntary early-break exits
    with an eligible TCS balance, the deterministic final-month exit
    redemption (P=1.0) must fire.

    Pre-fix: this count was zero — the early-break code path had no
    exit-redemption hook. Post-fix: every eligible early-break must redeem.

    SKIP if the run has no early-break terminations.
    """
    eligible_breaks = db.execute_query(
        """
        SELECT t.LeaseID, l.HouseholdID, l.EffectiveMonthlyRent, t.TerminationDate
        FROM simulation.LeaseTermination t
        JOIN simulation.Lease l ON l.RunID = t.RunID AND l.LeaseID = t.LeaseID
        WHERE t.RunID = ?
          AND t.TerminationReason = 'Tenant early break - voluntary exit during active lease'
          AND COALESCE(t.ArrearsAtExit, 0) = 0
        """,
        (run_id,),
    )
    if not eligible_breaks:
        print(f"  E2 Early-break exit redemption (eligible, NEW): "
              f"no clean early-break exits in this run [{SKIP}]")
        return True

    redeemed = 0
    eligible = 0
    for lease_id, household_id, rent, term_date in eligible_breaks:
        # Balance just before the early-break event.
        bal_rows = db.execute_query(
            """
            SELECT BalanceAfter FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND HouseholdID = ? AND TransactionDate < ?
            ORDER BY TransactionDate DESC, CreditTransactionID DESC
            OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """,
            (run_id, household_id, term_date),
        )
        if not bal_rows or bal_rows[0][0] is None:
            continue
        balance_at_exit = bal_rows[0][0]
        if balance_at_exit < rent:
            continue
        # Skip households that already redeemed in the same calendar year via
        # any non-FME path — annual-limit rule correctly blocks exit redemption
        # (same-day routine counts). Compare by YEAR() only, no date order.
        prior = db.execute_query(
            """
            SELECT COUNT(*) FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND HouseholdID = ? AND TransactionType = 'REDEMPTION'
              AND YEAR(TransactionDate) = YEAR(?)
              AND Notes NOT LIKE '%FINAL_MONTH_EXIT%'
            """,
            (run_id, household_id, term_date),
        )
        if prior and prior[0][0] > 0:
            continue
        eligible += 1
        red = db.execute_query(
            """
            SELECT COUNT(*) FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND HouseholdID = ? AND TransactionType = 'REDEMPTION'
              AND Notes LIKE '%FINAL_MONTH_EXIT%'
              AND TransactionDate = ?
            """,
            (run_id, household_id, term_date),
        )
        if red and red[0][0] > 0:
            redeemed += 1

    if eligible == 0:
        print(f"  E2 Early-break exit redemption (eligible, NEW): "
              f"no eligible early-break exits found in this run [{SKIP}]")
        return True
    ok = (redeemed == eligible)
    print(f"  E2 Early-break exit redemption (P=1.0 when eligible, NEW): "
          f"{redeemed}/{eligible} eligible early-breaks redeemed [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# E3 — Active household: credit must NOT be expired
# ---------------------------------------------------------------------------

def e3_active_household_credit_not_expired(db, run_id: int) -> bool:
    """The expiry sweep is gated to ExpiryStatus='EXITED'. A still-active
    household must never have its credit forfeited via the expiry pathway,
    regardless of how long they have held a balance.

    Validates: zero rows where a household has ExpiryStatus='ACTIVE' AND
    any FORFEITURE_EXPIRY ledger entry.
    """
    violations = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditBalance b
        WHERE b.RunID = ?
          AND b.ExpiryStatus = 'ACTIVE'
          AND EXISTS (
            SELECT 1 FROM simulation.TenantCreditLedger l
            WHERE l.RunID = b.RunID
              AND l.HouseholdID = b.HouseholdID
              AND l.TransactionType = 'FORFEITURE'
              AND l.Notes LIKE '%EXPIRY%'
          )
        """,
        (run_id,),
    )
    n_violations = violations[0][0] if violations else 0
    ok = n_violations == 0
    print(f"  E3 Active household credit not expired: "
          f"{n_violations} ACTIVE households with expiry forfeitures (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# E4 — Expiry sweep: forfeiture fires on/after exit + 24 months
# ---------------------------------------------------------------------------

def e4_expiry_at_24_months_post_exit(db, run_id: int) -> bool:
    """For every FORFEITURE_EXPIRY ledger row, the transaction date must be
    on or after the household's SystemExitDate + 24 months (the cliff per
    Kate Q2 A2a). Pre-cliff sweeps would be a bug.

    SKIP if no expiry forfeitures occurred (short or healthy runs).
    """
    rows = db.execute_query(
        f"""
        SELECT COUNT(*), SUM(CASE WHEN {month_diff_sql(db, 'b.SystemExitDate', 'l.TransactionDate')} < 24
                                  THEN 1 ELSE 0 END) AS PreCliff
        FROM simulation.TenantCreditLedger l
        JOIN simulation.TenantCreditBalance b
            ON b.RunID = l.RunID AND b.HouseholdID = l.HouseholdID
        WHERE l.RunID = ?
          AND l.TransactionType = 'FORFEITURE'
          AND l.Notes LIKE '%EXPIRY%'
          AND b.SystemExitDate IS NOT NULL
        """,
        (run_id,),
    )
    n_total = rows[0][0] if rows and rows[0][0] is not None else 0
    n_pre = rows[0][1] if rows and rows[0][1] is not None else 0
    if n_total == 0:
        print(f"  E4 Expiry forfeiture at 24-month cliff: "
              f"no expiry forfeitures in this run [{SKIP}]")
        return True
    ok = (n_pre == 0)
    print(f"  E4 Expiry forfeiture at 24-month cliff: "
          f"{n_total} expiries, {n_pre} fired pre-cliff (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', required=True, help='Test database name')
    parser.add_argument('--run-id', type=int, default=None)
    parser.add_argument('--assert', dest='assert_mode', action='store_true',
                        help='Exit 1 on any FAIL (CI mode)')
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(_config_path_for(args.env))
    run_id = _resolve_run_id(db, args.run_id)
    print(f"\n=== Phase 3.9.5 — Exit Redemption & Credit Expiry "
          f"(env={args.env}, RunID={run_id}) ===\n")

    results = [
        e1_lease_end_exit_redemption_fires(db, run_id),
        e2_early_break_exit_redemption_fires(db, run_id),
        e3_active_household_credit_not_expired(db, run_id),
        e4_expiry_at_24_months_post_exit(db, run_id),
    ]
    n_pass = sum(1 for r in results if r)
    print(f"\nResults: {n_pass}/{len(results)} passed\n")

    if args.assert_mode and n_pass < len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
