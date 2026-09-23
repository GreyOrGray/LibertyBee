"""
Phase 3.8.1 — Tenant Credit System: Credit Accrual Regression Tests.

Outcome-level black-box test suite for TCS accrual behavior. Covers Kate's
required test list (phase_3_8_1_credit_accrual_ba_answers.md §6):

  T1  Basic accrual              — ON_TIME + deposit-funded → ledger row at 5% of basis
  T2  LATE payment exclusion     — LATE row earns 0 (no ledger entry)
  T3  Deposit gate (unfunded)    — ON_TIME but deposit not funded → 0
  T4  Same-month deposit funding — deposit funded same month as ON_TIME payment → accrual allowed
  T5  Accrual basis              — basis = AmountCollected − ArrearsAmount − LateFeeAmount
  T6  Cap enforcement (synthetic) — household near cap → partial accrual lands exactly at cap
  T7  Ledger/balance reconcile   — sum of ACCRUAL Amount = LifetimeAccrued, = CurrentBalance in 3.8.1
  T8  Determinism                — (companion script; not part of this suite)
  T9  Non-cash invariant         — TCS accrual does not change Cash/CSF/EIP/Escrow

T6 is a synthetic test: it temporarily mutates a TenantCreditBalance row to
$14,950 and invokes TenantCreditManager.process_credit_accrual() directly
against a real RentCollection row to verify the partial-accrual math. It
restores the original state on the way out.

Usage:
    python sql/regression_tests/Phase3_8_1/phase_3_8_1_test_credit_accrual.py --env <db> [--run-id N] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.

Companion docs:
  docs/phases/phase_3_8/phase_3_8_1_credit_accrual_ba_answers.md
  docs/phases/phase_3_8/phase_3_8_1_credit_accrual_implementation_plan.md
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
SKIP = '\033[93mSKIP\033[0m'


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _registry_for_run(db, run_id):
    """Load the registry for the run's projection (the canonical param store; #75 —
    superseded reference.ProjectionParameters, which is dead on V0.2 and absent for
    registry-native projections like the #106 stress set)."""
    from parameter_registry import ParameterRegistry  # type: ignore
    proj = db.execute_query("SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,))[0][0]
    return ParameterRegistry(db).load(proj)


def _resolve_run_id(db, override) -> int:
    if override is not None:
        return int(override)
    row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
    if not row or row[0][0] is None:
        raise SystemExit("No runs found in simulation.Run; cannot pick a RunID.")
    return int(row[0][0])


# ---------------------------------------------------------------------------
# Assertion tests (read-only against an already-run simulation)
# ---------------------------------------------------------------------------

def t1_basic_accrual(db, run_id: int) -> bool:
    """Every ACCRUAL ledger row in this run must correspond to an ON_TIME
    RentCollection row whose deposit was funded at the time."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) AS total_accruals,
               SUM(CASE WHEN rc.PaymentStatus = 'ON_TIME' THEN 1 ELSE 0 END) AS ontime_accruals
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.RentCollection rc
            ON tcl.RunID = rc.RunID AND tcl.RelatedCollectionID = rc.CollectionID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'ACCRUAL'
        """,
        (run_id,),
    )
    total = rows[0][0] or 0
    ontime = rows[0][1] or 0
    ok = (total == ontime) and total > 0
    status = PASS if ok else (FAIL if total > 0 else SKIP)
    print(f"  T1 Basic accrual: {total} ACCRUAL rows, {ontime} are ON_TIME (expect equal & >0) [{status}]")
    return ok or total == 0  # SKIP if no accruals to test against


def t2_late_exclusion(db, run_id: int) -> bool:
    """No ACCRUAL row should be linked to a LATE RentCollection."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.RentCollection rc
            ON tcl.RunID = rc.RunID AND tcl.RelatedCollectionID = rc.CollectionID
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'ACCRUAL'
          AND rc.PaymentStatus = 'LATE'
        """,
        (run_id,),
    )
    n_violations = rows[0][0] or 0
    ok = n_violations == 0
    print(f"  T2 LATE payment exclusion: {n_violations} accruals on LATE rows (expect 0) [{PASS if ok else FAIL}]")
    return ok


def t3_deposit_gate(db, run_id: int) -> bool:
    """No ACCRUAL row should have a transaction date predating its lease's
    DepositFundedDate (i.e., accruing before deposit was funded)."""
    rows = db.execute_query(
        """
        SELECT tcl.CreditTransactionID, tcl.HouseholdID, tcl.RelatedLeaseID,
               tcl.TransactionDate, ld.DepositFundedDate
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.LeaseDeposit ld
            ON tcl.RunID = ld.RunID AND tcl.RelatedLeaseID = ld.LeaseID
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'ACCRUAL'
          AND (ld.DepositFundedDate IS NULL OR tcl.TransactionDate < ld.DepositFundedDate)
        """,
        (run_id,),
    )
    n_violations = len(rows)
    ok = n_violations == 0
    print(f"  T3 Deposit gate: {n_violations} accruals before deposit funded (expect 0) [{PASS if ok else FAIL}]")
    if not ok:
        for r in rows[:5]:
            print(f"      Violation: TxnID={r[0]} HH={r[1]} Lease={r[2]} TxnDate={r[3]} FundedDate={r[4]}")
    return ok


def t4_same_month_funding_allowed(db, run_id: int) -> bool:
    """Confirm at least one ACCRUAL row exists whose TransactionDate falls in
    the same calendar month as the lease's DepositFundedDate. (Permissive:
    SKIP if no such case occurred organically.)"""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.LeaseDeposit ld
            ON tcl.RunID = ld.RunID AND tcl.RelatedLeaseID = ld.LeaseID
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'ACCRUAL'
          AND YEAR(tcl.TransactionDate)  = YEAR(ld.DepositFundedDate)
          AND MONTH(tcl.TransactionDate) = MONTH(ld.DepositFundedDate)
        """,
        (run_id,),
    )
    n = rows[0][0] or 0
    if n > 0:
        print(f"  T4 Same-month deposit funding allowed: {n} cases observed [{PASS}]")
        return True
    print(f"  T4 Same-month deposit funding allowed: no organic cases in this run [{SKIP}]")
    return True


def t5_accrual_basis(db, run_id: int) -> bool:
    """For each ACCRUAL row, Amount must equal
    (AmountCollected − ArrearsAmount − LateFeeAmount) × credit_rate,
    quantized to cents. ON_TIME rows have arrears=0 and late_fee=0 normally,
    so this typically simplifies to AmountCollected * rate, but we verify
    the full formula."""
    # Resolve TCS.CreditRate + TCS.CreditCap from the registry (canonical store; #75
    # re-point — was reference.ProjectionParameters, dead on V0.2 + absent for
    # registry-native projections like the #106 stress set; fail-loud, no SKIP).
    reg = _registry_for_run(db, run_id)
    rate = reg.get_decimal('TCS', 'CreditRate')
    cap = reg.get_decimal('TCS', 'CreditCap')

    rows = db.execute_query(
        """
        SELECT tcl.CreditTransactionID, tcl.Amount, tcl.BalanceAfter,
               rc.AmountCollected, rc.ArrearsAmount, rc.LateFeeAmount
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.RentCollection rc
            ON tcl.RunID = rc.RunID AND tcl.RelatedCollectionID = rc.CollectionID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'ACCRUAL'
        """,
        (run_id,),
    )
    mismatches = []
    for txn_id, amount, balance_after, amount_collected, arrears, late_fee in rows:
        basis = Decimal(str(amount_collected)) - Decimal(str(arrears or 0)) - Decimal(str(late_fee or 0))
        expected_unrounded = basis * rate
        expected = expected_unrounded.quantize(Decimal("0.01"))
        actual = Decimal(str(amount))
        # Allow the accrual to be smaller than expected ONLY due to cap enforcement.
        if actual != expected:
            # A valid cap-clipped accrual lands the balance EXACTLY at the cap.
            # Patched 2026-05-21 (Phase 3.9.3 re-baseline): check this ledger
            # row's own BalanceAfter (point-in-time), NOT the household's
            # end-of-run CurrentBalance. Post-3.8.2 a household can hit the cap,
            # accrue a clipped amount, then redeem — leaving CurrentBalance below
            # the cap and the old end-of-run check falsely flagging the clip.
            if actual < expected and Decimal(str(balance_after)) == cap:
                continue  # cap-clipped, valid
            mismatches.append((txn_id, str(actual), str(expected), str(basis)))
    ok = len(mismatches) == 0
    print(f"  T5 Accrual basis math: {len(mismatches)} mismatched rows (expect 0) [{PASS if ok else FAIL}]")
    for txn_id, act, exp, basis in mismatches[:5]:
        print(f"      TxnID={txn_id}: amount=${act}, expected=${exp}, basis=${basis}")
    return ok


def t6_cap_enforcement_synthetic(db, run_id: int) -> bool:
    """Synthetic: set a household's CurrentBalance to $14,950, invoke
    TenantCreditManager.process_credit_accrual against an ON_TIME
    RentCollection that would normally accrue >$50, and verify partial
    accrual lands the balance at exactly the cap.

    Mutates and restores TenantCreditBalance + cleans up any test ledger
    rows it creates. Read-only with respect to the run's "real" state on
    exit.
    """
    # Pick a household that has at least one ON_TIME, deposit-funded
    # RentCollection we can replay against.
    pick = db.execute_query(
        """
        SELECT l.HouseholdID, rc.LeaseID, rc.CollectionID, rc.AmountCollected,
               rc.ArrearsAmount, rc.LateFeeAmount, rc.CreatedEventID, rc.CollectionDate
        FROM simulation.RentCollection rc
        JOIN simulation.Lease l ON l.RunID = rc.RunID AND l.LeaseID = rc.LeaseID
        JOIN simulation.LeaseDeposit ld ON ld.RunID = rc.RunID AND ld.LeaseID = rc.LeaseID
        WHERE rc.RunID = ?
          AND rc.PaymentStatus = 'ON_TIME'
          AND ld.DepositFundedDate IS NOT NULL
          AND rc.CollectionDate >= ld.DepositFundedDate
        ORDER BY rc.AmountCollected DESC
        OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
        """,
        (run_id,),
    )
    if not pick:
        print(f"  T6 Cap enforcement (synthetic): no ON_TIME/funded RentCollection to replay against [{SKIP}]")
        return True
    hh, lease_id, coll_id, amount_collected, arrears, late_fee, event_id, coll_date = pick[0]
    arrears = arrears or Decimal("0")
    late_fee = late_fee or Decimal("0")

    # Save original state so we can restore.
    orig = db.execute_query(
        "SELECT CurrentBalance, LifetimeAccrued, LifetimeRedeemed, LastAccrualDate, LastUpdatedEventID "
        "FROM simulation.TenantCreditBalance WHERE RunID=? AND HouseholdID=?",
        (run_id, hh),
    )
    if not orig:
        print(f"  T6 Cap enforcement (synthetic): household {hh} has no TenantCreditBalance row [{SKIP}]")
        return True
    orig_balance, orig_lifetime, orig_redeemed, orig_last_date, orig_last_event = orig[0]

    # Lookup cap from the registry (#75 re-point — was reference.ProjectionParameters)
    cap = _registry_for_run(db, run_id).get_decimal('TCS', 'CreditCap')

    # Mutate: set CurrentBalance to cap - $50 (so a $100 attempted accrual partial-clips to $50).
    # Phase 3.9.5: also zero LifetimeRedeemed because the picked household may now have
    # FINAL_MONTH_EXIT redemptions on its ledger (early-break and lease-end paths can
    # both produce them post-3.9.5), which would otherwise violate the
    # CHK_simTenantCreditBalance_Reconciliation constraint
    # (LifetimeAccrued >= CurrentBalance + LifetimeRedeemed).
    target_balance = cap - Decimal("50.00")
    db.execute_non_query(
        """
        UPDATE simulation.TenantCreditBalance
        SET CurrentBalance = ?, LifetimeAccrued = ?, LifetimeRedeemed = 0
        WHERE RunID = ? AND HouseholdID = ?
        """,
        (target_balance, target_balance, run_id, hh),
    )

    # Invoke the manager directly with a contrived basis that would yield >$50 at 5%.
    # Easiest: pretend amount_collected was $2,000 with no arrears/fees → basis=$2,000 → proposed=$100.
    # Override the existing RentCollection-linked values for this one call.
    from tenant_credit_manager import TenantCreditManager
    from event_logger import EventLogger
    from configuration_loader import ConfigurationLoader

    cfg_loader = ConfigurationLoader(db)
    projection_row = db.execute_query(
        "SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,)
    )
    cfg = cfg_loader.load_projection(projection_row[0][0])

    event_logger = EventLogger(db)
    event_logger.set_run_id(run_id)

    tcm = TenantCreditManager(
        db_manager=db,
        event_logger=event_logger,
        run_id=run_id,
        credit_rate=Decimal(str(cfg.tenant_credit_rate)),
        credit_cap=cap,
    )

    test_basis_amount = Decimal("2000.00")  # × 0.05 = $100 proposed, cap headroom = $50
    result = tcm.process_credit_accrual(
        lease_id=lease_id,
        collection_id=coll_id,
        amount_collected=test_basis_amount,
        arrears_amount=Decimal("0"),
        late_fee_amount=Decimal("0"),
        payment_status='ON_TIME',
        deposit_funded=True,
        transaction_date=coll_date,
        event_id=event_id,
    )

    new_balance = tcm.get_balance(hh)
    expected_accrual = Decimal("50.00")  # because 0.05 rate × $2000 = $100, but cap only allows $50
    accrual_ok = result.accrued_amount == expected_accrual
    balance_ok = new_balance == cap

    # Cleanup: delete the synthetic ledger row + restore balance row
    if result.credit_transaction_id is not None:
        db.execute_non_query(
            "DELETE FROM simulation.TenantCreditLedger WHERE RunID = ? AND CreditTransactionID = ?",
            (run_id, result.credit_transaction_id),
        )
    db.execute_non_query(
        """
        UPDATE simulation.TenantCreditBalance
        SET CurrentBalance = ?, LifetimeAccrued = ?, LifetimeRedeemed = ?,
            LastAccrualDate = ?, LastUpdatedEventID = ?
        WHERE RunID = ? AND HouseholdID = ?
        """,
        (orig_balance, orig_lifetime, orig_redeemed, orig_last_date, orig_last_event, run_id, hh),
    )

    ok = accrual_ok and balance_ok
    if ok:
        print(f"  T6 Cap enforcement (synthetic): partial-accrued ${result.accrued_amount} to land at cap ${new_balance} [{PASS}]")
    else:
        print(f"  T6 Cap enforcement (synthetic): accrued ${result.accrued_amount} (expected $50.00), final balance ${new_balance} (expected ${cap}) [{FAIL}]")
    return ok


def t7_ledger_balance_reconcile(db, run_id: int) -> bool:
    """For every household:
    (a) sum of ACCRUAL Amount over TenantCreditLedger == LifetimeAccrued
    (b) CurrentBalance == LifetimeAccrued − LifetimeRedeemed − LifetimeForfeited

    Phase 3.8.3 patch (2026-05-19, mirrors the 3.8.2 patch from 3.8.1's
    original "CurrentBalance == LifetimeAccrued"): the post-3.8.3 invariant
    must also subtract forfeitures (expiry + eviction). LifetimeForfeited
    isn't a stored column — derived from the ledger via sum of FORFEITURE
    Amount values (negative, so we negate the sum).
    """
    rows = db.execute_query(
        """
        SELECT
            tcb.HouseholdID,
            tcb.CurrentBalance,
            tcb.LifetimeAccrued,
            tcb.LifetimeRedeemed,
            COALESCE((
                SELECT SUM(Amount)
                FROM simulation.TenantCreditLedger
                WHERE RunID = tcb.RunID
                  AND HouseholdID = tcb.HouseholdID
                  AND TransactionType = 'ACCRUAL'
            ), 0) AS LedgerAccrualSum,
            COALESCE((
                SELECT SUM(-Amount)
                FROM simulation.TenantCreditLedger
                WHERE RunID = tcb.RunID
                  AND HouseholdID = tcb.HouseholdID
                  AND TransactionType = 'FORFEITURE'
            ), 0) AS LifetimeForfeited
        FROM simulation.TenantCreditBalance tcb
        WHERE tcb.RunID = ?
        """,
        (run_id,),
    )
    mismatches = []
    for hh, current, lifetime, redeemed, ledger_sum, forfeited in rows:
        if Decimal(str(lifetime)) != Decimal(str(ledger_sum)):
            mismatches.append(("lifetime_vs_ledger_sum", hh, str(lifetime), str(ledger_sum)))
        # Phase 3.8.3 invariant: CurrentBalance == LifetimeAccrued − LifetimeRedeemed − LifetimeForfeited
        expected_current = (
            Decimal(str(lifetime))
            - Decimal(str(redeemed))
            - Decimal(str(forfeited))
        )
        if Decimal(str(current)) != expected_current:
            mismatches.append(("current_vs_acc_minus_red_minus_forf", hh,
                               str(current), str(expected_current)))
    ok = len(mismatches) == 0
    print(f"  T7 Ledger/balance reconciliation: {len(rows)} households, {len(mismatches)} mismatches (expect 0) [{PASS if ok else FAIL}]")
    for kind, hh, a, b in mismatches[:5]:
        print(f"      {kind}: HouseholdID={hh}, got={a}, expected={b}")
    return ok


def t9_non_cash_invariant(db, run_id: int) -> bool:
    """Confirm TCS accrual events have no FundLedger counterpart.

    Approach: For each ACCRUAL TenantCreditLedger row, its CreatedEventID
    should NOT appear in any fund-movement table. (A FundLedger row tied
    to that event would prove cash moved.) The simulation.Event row for
    the accrual exists, but it should be the only place that event id
    appears.

    Practical check: every ACCRUAL CreatedEventID is referenced by exactly
    one row (the one we already know about — the RentCollection.CreatedEventID
    or our own TENANT_CREDIT event). We assert no FundLedger row points
    to our accrual event ids.
    """
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.FundLedger fl
        WHERE fl.RunID = ?
          AND fl.EventID IN (
              SELECT CreatedEventID
              FROM simulation.TenantCreditLedger
              WHERE RunID = ? AND TransactionType = 'ACCRUAL'
          )
        """,
        (run_id, run_id),
    )
    n = rows[0][0] or 0
    ok = n == 0
    print(f"  T9 Non-cash invariant: {n} FundLedger rows tied to accrual events (expect 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True, help='DB name under environments/')
    parser.add_argument('--run-id', default=None, help='RunID to test (default: latest)')
    parser.add_argument('--assert', dest='assert_mode', action='store_true',
                        help='Exit 1 on any FAIL')
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    db = DatabaseManager(_config_path_for(args.env))

    run_id = _resolve_run_id(db, args.run_id)
    print(f'Phase 3.8.1 TCS credit accrual regression — {args.env} (RunID={run_id})')
    print('=' * 78)

    results = [
        t1_basic_accrual(db, run_id),
        t2_late_exclusion(db, run_id),
        t3_deposit_gate(db, run_id),
        t4_same_month_funding_allowed(db, run_id),
        t5_accrual_basis(db, run_id),
        t6_cap_enforcement_synthetic(db, run_id),
        t7_ledger_balance_reconcile(db, run_id),
        t9_non_cash_invariant(db, run_id),
    ]

    print('=' * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f'Result: {passed}/{total} tests passed')

    if args.assert_mode and passed != total:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
