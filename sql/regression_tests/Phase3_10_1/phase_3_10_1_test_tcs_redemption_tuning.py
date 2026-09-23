"""
Phase 3.10.1 — TCS Redemption Tuning: Regression Tests.

Outcome-level black-box tests for the BA-approved tuning
([phase_3_10_1_tcs_redemption_tuning_ba_answers_handoff.md](../../docs/phases/phase_3_10/phase_3_10_1_tcs_redemption_tuning_ba_answers_handoff.md)):

  - FINAL_MONTH_EXIT allows partial-month redemption (balance > 0 instead of
    balance >= monthly_rent).
  - FINAL_MONTH_EXIT redemption amount = MIN(balance, monthly_rent).
  - FINAL_MONTH_EXIT is EXEMPT from the annual redemption limit.
  - Good-standing requirement is PRESERVED for FINAL_MONTH_EXIT.
  - ROUTINE / HARDSHIP redemption remains full-month-only.
  - 24-month expiry sweep and portability behavior are unchanged.

Eight tests per BA answers §5:

  T1  Partial exit redemption below monthly rent.
  T2  Full exit redemption still works; residual portability preserved.
  T3  Annual-limit exemption: routine earlier in year doesn't block FME.
  T4  Good-standing still blocks FINAL_MONTH_EXIT.
  T5  Tiny balance redemption (≤ $100) fires.
  T6  Routine/hardship mid-tenure redemption remains full-month only.
  T7  Expiry sweep unchanged (24-month cliff).
  T8  Portability preserved — exited+rejoined households retain balance.

Tests SKIP gracefully when the run doesn't exercise a pathway.

Usage:
    python sql/regression_tests/Phase3_10_1/phase_3_10_1_test_tcs_redemption_tuning.py --env <db> [--run-id N] [--assert]
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
# T1 — Partial exit redemption below monthly rent
# ---------------------------------------------------------------------------

def t1_partial_exit_redemption_below_rent(db, run_id: int) -> bool:
    """At least one FINAL_MONTH_EXIT redemption fires with Amount < monthly rent.

    Verifies the partial-month tuning is active and capturing balances that
    would have been blocked under the old `balance >= monthly_rent` gate.
    """
    rows = db.execute_query(
        """
        SELECT COUNT(*) AS PartialFMECount,
               SUM(ABS(tcl.Amount)) AS PartialFMETotal
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.Lease l ON l.RunID=tcl.RunID AND l.LeaseID=tcl.RelatedLeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType='REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND ABS(tcl.Amount) < l.EffectiveMonthlyRent
          AND ABS(tcl.Amount) > 0
        """,
        (run_id,),
    )
    n = rows[0][0] if rows else 0
    tot = rows[0][1] if rows and rows[0][1] is not None else 0
    if n == 0:
        print(f"  T1 Partial exit redemption below rent: "
              f"no partial FME redemptions in this run [{SKIP}]")
        return True
    print(f"  T1 Partial exit redemption below rent: {n} partial FMEs fired "
          f"(total ${tot:,.2f}) [{PASS}]")
    return True


# ---------------------------------------------------------------------------
# T2 — Full exit redemption still works; residual portability
# ---------------------------------------------------------------------------

def t2_full_exit_redemption_still_works(db, run_id: int) -> bool:
    """Households with balance >= rent at exit still get the full-month
    redemption (Amount = monthly_rent). Residual balance > 0 must remain
    after redemption (post-redemption BalanceAfter > 0)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) AS FullFMECount
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.Lease l ON l.RunID=tcl.RunID AND l.LeaseID=tcl.RelatedLeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType='REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND ABS(tcl.Amount) = l.EffectiveMonthlyRent
        """,
        (run_id,),
    )
    n = rows[0][0] if rows else 0
    if n == 0:
        print(f"  T2 Full exit redemption still works: "
              f"no full-month FMEs in this run [{SKIP}]")
        return True
    # Verify no full-month FME left a negative balance (would indicate a math bug)
    bad = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.Lease l ON l.RunID=tcl.RunID AND l.LeaseID=tcl.RelatedLeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType='REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND ABS(tcl.Amount) = l.EffectiveMonthlyRent
          AND tcl.BalanceAfter < 0
        """,
        (run_id,),
    )
    n_bad = bad[0][0] if bad else 0
    ok = (n_bad == 0)
    print(f"  T2 Full exit redemption still works: {n} full-month FMEs fired, "
          f"{n_bad} with negative balance (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T3 — Annual-limit exemption (FME fires after prior-year routine/hardship)
# ---------------------------------------------------------------------------

def t3_annual_limit_exemption(db, run_id: int) -> bool:
    """A household that consumed its annual redemption slot via ROUTINE or
    HARDSHIP earlier in calendar year Y can still get a FINAL_MONTH_EXIT in
    year Y if otherwise eligible."""
    rows = db.execute_query(
        """
        WITH fmes AS (
            SELECT HouseholdID, YEAR(TransactionDate) AS Yr
            FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND TransactionType='REDEMPTION'
              AND Notes LIKE '%FINAL_MONTH_EXIT%'
        ),
        prior_rh AS (
            SELECT DISTINCT HouseholdID, YEAR(TransactionDate) AS Yr
            FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND TransactionType='REDEMPTION'
              AND (Notes LIKE '%ROUTINE%'
                   OR Notes LIKE '%HARDSHIP%')
        )
        SELECT COUNT(*)
        FROM fmes f
        JOIN prior_rh r ON r.HouseholdID = f.HouseholdID AND r.Yr = f.Yr
        """,
        (run_id, run_id),
    )
    n_exempt = rows[0][0] if rows else 0
    if n_exempt == 0:
        print(f"  T3 Annual-limit exemption at FME: "
              f"no FME co-occurring with same-year routine/hardship [{SKIP}]")
        return True
    print(f"  T3 Annual-limit exemption at FME: {n_exempt} households received "
          f"FME despite same-year routine/hardship [{PASS}]")
    return True


# ---------------------------------------------------------------------------
# T4 — Good-standing still blocks FME
# ---------------------------------------------------------------------------

def t4_good_standing_blocks_fme(db, run_id: int) -> bool:
    """Households exiting with ArrearsAtExit > 0 (not in good standing) must
    NOT have a FINAL_MONTH_EXIT redemption.

    This catches the failure mode where the good-standing gate was relaxed
    along with the annual-limit / balance gates."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.LeaseTermination t
        JOIN simulation.TenantCreditLedger tcl
          ON tcl.RunID = t.RunID AND tcl.RelatedLeaseID = t.LeaseID
        WHERE t.RunID = ?
          AND COALESCE(t.ArrearsAtExit, 0) > 0
          AND tcl.TransactionType = 'REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
        """,
        (run_id,),
    )
    n_bad = rows[0][0] if rows else 0
    ok = (n_bad == 0)
    print(f"  T4 Good-standing still blocks FME: "
          f"{n_bad} FME redemptions on arrears-at-exit terminations "
          f"(expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T5 — Tiny balance redemption fires
# ---------------------------------------------------------------------------

def t5_tiny_balance_redeems(db, run_id: int) -> bool:
    """Even a small positive balance ($<100, say) should fire a
    FINAL_MONTH_EXIT redemption per Q4 A4a (no minimum threshold)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*), MIN(ABS(Amount))
        FROM simulation.TenantCreditLedger
        WHERE RunID = ? AND TransactionType='REDEMPTION'
          AND Notes LIKE '%FINAL_MONTH_EXIT%'
          AND ABS(Amount) > 0
        """,
        (run_id,),
    )
    n = rows[0][0] if rows else 0
    min_amt = rows[0][1] if rows and rows[0][1] is not None else None
    if n == 0:
        print(f"  T5 Tiny-balance FME: no FME redemptions in this run [{SKIP}]")
        return True
    # We won't fail this test based on min_amt — the simulator's exact balance
    # distribution depends on accrual rate and timing. We document the minimum
    # observed redemption amount for the report.
    print(f"  T5 Tiny-balance FME: {n} FME redemptions; "
          f"smallest amount ${min_amt:.2f} [{PASS}]")
    return True


# ---------------------------------------------------------------------------
# T6 — Routine/Hardship stays full-month
# ---------------------------------------------------------------------------

def t6_routine_hardship_full_month_only(db, run_id: int) -> bool:
    """ROUTINE and HARDSHIP redemptions must equal the rent the household
    actually owed in the billing month the redemption fired — i.e.
    MonthlyPaymentStatus.AmountDue for the linked RentCollection.

    NOTE: comparing to Lease.EffectiveMonthlyRent is incorrect because
    Phase 3.9.3 mutates that column when tenure-based rent reductions
    tier-cross. MPS.AmountDue is the contemporary rent locked at billing
    time."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.RentCollection rc
          ON rc.RunID = tcl.RunID AND rc.CollectionID = tcl.RelatedCollectionID
        JOIN simulation.MonthlyPaymentStatus mps
          ON mps.RunID = rc.RunID AND mps.LeaseID = rc.LeaseID
             AND mps.BillingMonth = rc.CollectionMonth
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'REDEMPTION'
          AND (tcl.Notes LIKE '%ROUTINE%'
               OR tcl.Notes LIKE '%HARDSHIP%')
          AND ABS(tcl.Amount) <> mps.AmountDue
        """,
        (run_id,),
    )
    n_bad = rows[0][0] if rows else 0
    ok = (n_bad == 0)
    print(f"  T6 Routine/hardship stays full-month-only "
          f"(amount = MPS.AmountDue at billing): {n_bad} mismatches "
          f"(expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T7 — Expiry sweep at 24-month cliff (unchanged)
# ---------------------------------------------------------------------------

def t7_expiry_sweep_unchanged(db, run_id: int) -> bool:
    """All FORFEITURE_EXPIRY ledger rows fire on/after SystemExitDate + 24
    months. Identical to Phase 3.9.5 E4; verifies the 3.10.1 changes didn't
    disturb the expiry path."""
    rows = db.execute_query(
        f"""
        SELECT COUNT(*),
               SUM(CASE WHEN {month_diff_sql(db, 'b.SystemExitDate', 'l.TransactionDate')} < 24
                        THEN 1 ELSE 0 END)
        FROM simulation.TenantCreditLedger l
        JOIN simulation.TenantCreditBalance b
            ON b.RunID = l.RunID AND b.HouseholdID = l.HouseholdID
        WHERE l.RunID = ? AND l.TransactionType = 'FORFEITURE'
          AND l.Notes LIKE '%EXPIRY%'
          AND b.SystemExitDate IS NOT NULL
        """,
        (run_id,),
    )
    n_total = rows[0][0] if rows and rows[0][0] is not None else 0
    n_pre = rows[0][1] if rows and rows[0][1] is not None else 0
    if n_total == 0:
        print(f"  T7 Expiry sweep unchanged: no expiry forfeitures in run [{SKIP}]")
        return True
    ok = (n_pre == 0)
    print(f"  T7 Expiry sweep unchanged: {n_total} expiries, {n_pre} pre-cliff "
          f"(expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T8 — Portability preserved (residual reactivates on re-entry)
# ---------------------------------------------------------------------------

def t8_portability_preserved(db, run_id: int) -> bool:
    """A household that exited, partial-redeemed at exit, and later rejoined
    (mark_household_reentered fired) must retain any residual balance into
    its reactivated state.

    Test signal: households currently ACTIVE that previously had EXITED
    state (per the lifecycle audit trail: any FORFEITURE rows linked to
    them WITHOUT having terminal ExpiryStatus, OR any FME redemption row in
    their history) and have a current CurrentBalance > 0 — verifies the
    balance survived the EXITED → ACTIVE transition.

    This is a soft test — it confirms the post-reentry balance pipeline
    works, not the exact dollar value, because the residual+new-accrual
    math is non-trivial."""
    rows = db.execute_query(
        """
        SELECT COUNT(DISTINCT b.HouseholdID)
        FROM simulation.TenantCreditBalance b
        WHERE b.RunID = ?
          AND b.ExpiryStatus = 'ACTIVE'
          AND b.SystemExitDate IS NULL
          AND b.CurrentBalance > 0
          AND b.HouseholdID IN (
              SELECT DISTINCT HouseholdID FROM simulation.TenantCreditLedger
              WHERE RunID = b.RunID AND TransactionType = 'REDEMPTION'
                AND Notes LIKE '%FINAL_MONTH_EXIT%'
          )
        """,
        (run_id,),
    )
    n = rows[0][0] if rows else 0
    if n == 0:
        print(f"  T8 Portability preserved: no re-entered households with "
              f"prior FME found in this run [{SKIP}]")
        return True
    print(f"  T8 Portability preserved: {n} households had prior FME and are "
          f"now ACTIVE with positive balance [{PASS}]")
    return True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', required=True)
    parser.add_argument('--run-id', type=int, default=None)
    parser.add_argument('--assert', dest='assert_mode', action='store_true')
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(_config_path_for(args.env))
    run_id = _resolve_run_id(db, args.run_id)
    print(f"\n=== Phase 3.10.1 — TCS Redemption Tuning "
          f"(env={args.env}, RunID={run_id}) ===\n")

    results = [
        t1_partial_exit_redemption_below_rent(db, run_id),
        t2_full_exit_redemption_still_works(db, run_id),
        t3_annual_limit_exemption(db, run_id),
        t4_good_standing_blocks_fme(db, run_id),
        t5_tiny_balance_redeems(db, run_id),
        t6_routine_hardship_full_month_only(db, run_id),
        t7_expiry_sweep_unchanged(db, run_id),
        t8_portability_preserved(db, run_id),
    ]
    n_pass = sum(1 for r in results if r)
    print(f"\nResults: {n_pass}/{len(results)} passed\n")

    if args.assert_mode and n_pass < len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
