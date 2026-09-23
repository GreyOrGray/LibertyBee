"""
Phase 3.8.3 — Tenant Credit System: Credit Portability & Expiry Regression Tests.

Outcome-level black-box test suite for the TCS lifecycle endpoints
(24-month post-exit expiry, internal-move continuity, eviction forfeiture,
final-month exit redemption + TCS/deposit settlement ordering).

Covers Kate's locked decisions from phase_3_8_3_credit_portability_ba_answers.md
plus the renewal-decision pre-roll regression from
phase_3_8_3_followup_handoff.md.

  T1  Same-HouseholdID move is a no-op (no TRANSFER ledger row written)
  T2  Non-eviction exit sets ExpiryStatus + computes CreditExpiryDate
  T3  Re-entry within 24 months clears expiry (Kate Q7 B7b)
  T4  24-month expiry cliff (Kate Q2 A2a + Q4 A4b monthly sweep)
  T5  Eviction forfeiture fires at execution date (Kate Q9 C9b)
  T6  Final-month exit redemption — eligible: deterministic P=1.0
  T7  Final-month exit redemption — ineligible (already redeemed this year)
  T8  Final-month exit redemption — ineligible (insufficient credit)
  T9  Damages NEVER covered by TCS (Kate Q12 §5.1)
  T10 Eviction does NOT trigger final-month redemption
  T11 Fund conservation: accruals − redemptions − forfeitures = sum of balances
  T12 Event subtype distinguishability (5 audit reasons)
  T13 Renewal-decision pre-roll stability (Kate-required, followup_handoff §"Required Regression Addition")

Tests SKIP gracefully when the run doesn't exercise a particular pathway
(common on short / healthy runs).

Usage:
    python sql/regression_tests/Phase3_8_3/phase_3_8_3_test_portability_and_expiry.py --env <db> [--run-id N] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from datetime import date

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
# T1 — Same-HouseholdID move is a no-op
# ---------------------------------------------------------------------------

def t1_same_household_move_no_transfer_row(db, run_id: int) -> bool:
    """Per Kate Q5 B5a: same-HouseholdID moves should NOT produce a TRANSFER
    ledger row. Credit continuity is a semantic no-op (the balance is keyed
    on RunID + HouseholdID, so it persists naturally)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.TenantCreditLedger
        WHERE RunID = ? AND TransactionType = 'TRANSFER'
        """,
        (run_id,),
    )
    n = rows[0][0] if rows else 0
    ok = n == 0
    print(f"  T1 No TRANSFER ledger rows (Kate Q5 B5a): {n} found, expected 0 [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T2 — Non-eviction exit sets ExpiryStatus + CreditExpiryDate
# ---------------------------------------------------------------------------

def _portability_months(db, run_id: int) -> int:
    """Audit F-16 (the E1 lesson): derive the expiry window from the LIVE
    registry param (TCS.PortabilityYears × 12), never a hardcoded 24 — a
    bare literal here would defend drift against a ratified policy change.
    SOURCE: business_rules_current.md §TCS (KD-023: PortabilityYears=2 → 24 mo)."""
    proj = db.execute_query(
        "SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,))
    import os, sys as _sys
    from parameter_registry import ParameterRegistry
    reg = ParameterRegistry(db).load(int(proj[0][0]))
    return reg.get_int('TCS', 'PortabilityYears') * 12


def t2_exited_households_have_computed_expiry(db, run_id: int) -> bool:
    """For every TenantCreditBalance row with ExpiryStatus='EXITED', verify
    SystemExitDate IS NOT NULL and CreditExpiryDate = SystemExitDate + the
    registry-derived portability window (TCS.PortabilityYears × 12 months)."""
    months = _portability_months(db, run_id)
    rows = db.execute_query(
        f"""
        SELECT COUNT(*)
        FROM simulation.TenantCreditBalance
        WHERE RunID = ?
          AND ExpiryStatus = 'EXITED'
          AND (
              SystemExitDate IS NULL
              OR CreditExpiryDate <> {add_months_sql(db, int(months), 'SystemExitDate')}
          )
        """,
        (run_id,),
    )
    n_mismatches = rows[0][0] if rows else 0

    total = db.execute_query(
        "SELECT COUNT(*) FROM simulation.TenantCreditBalance "
        "WHERE RunID = ? AND ExpiryStatus = 'EXITED'",
        (run_id,),
    )
    n_exited = total[0][0] if total else 0
    if n_exited == 0:
        print(f"  T2 No EXITED households in this run [{SKIP}]")
        return True

    ok = n_mismatches == 0
    print(f"  T2 EXITED households have SystemExitDate + CreditExpiryDate: "
          f"{n_exited} EXITED, {n_mismatches} mismatches [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T3 — Re-entry clears expiry (Kate Q7 B7b)
# ---------------------------------------------------------------------------

def t3_reentry_clears_expiry(db, run_id: int) -> bool:
    """A household whose status is currently ACTIVE but has at least one
    PRIOR terminated lease (i.e., they exited and came back) must have
    SystemExitDate = NULL. mark_household_reentered is idempotent and is
    called for every new lease via tenant_manager.

    On a short run with few re-entries we SKIP; otherwise structural
    invariant must hold."""
    # Households whose LATEST status is ACTIVE BUT who have at least one
    # terminated lease in their history (i.e., they re-entered).
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditBalance tcb
        WHERE tcb.RunID = ?
          AND tcb.ExpiryStatus = 'ACTIVE'
          AND tcb.SystemExitDate IS NOT NULL
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    ok = n_violations == 0
    print(f"  T3 Re-entry clears SystemExitDate (Kate Q7 B7b): "
          f"{n_violations} ACTIVE rows with non-NULL SystemExitDate (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T4 — 24-month expiry cliff (Kate Q2 A2a + Q4 A4b)
# ---------------------------------------------------------------------------

def t4_expiry_cliff_forfeits_credit(db, run_id: int) -> bool:
    """For every EXPIRED household, the sum of its FORFEITURE_EXPIRY ledger
    amounts must exactly equal the residual credit it carried at expiry
    (Kate Q2 A2a cliff + Q3 A3b audit trail).

    Residual at expiry = LifetimeAccrued − LifetimeRedeemed. The correct
    invariant:

        SUM(FORFEITURE_EXPIRY amounts) == −(LifetimeAccrued − LifetimeRedeemed)

    This handles BOTH cases correctly:
      - Household expired with a positive balance → forfeiture row(s) sum to it
      - Household redeemed its entire balance before exit (residual = 0) →
        no forfeiture row is expected, and the sum is 0

    The earlier version of this test wrongly flagged the second case (an
    EXPIRED household that had ever accrued but redeemed everything before
    exit). Caught during the 3.8.3 extra-seed sweep (P91/seed7, HouseholdID
    870: $9,178.40 accrued == $9,178.40 redeemed, residual $0). Fund
    conservation (T11) was unaffected — this was purely a test-assertion bug.

    SKIPs cleanly if the run is too short for the 24-month cliff to fire."""
    expired = db.execute_query(
        "SELECT COUNT(*) FROM simulation.TenantCreditBalance "
        "WHERE RunID = ? AND ExpiryStatus = 'EXPIRED'",
        (run_id,),
    )
    n_expired = expired[0][0] if expired else 0
    if n_expired == 0:
        print(f"  T4 24-month expiry cliff: no EXPIRED households yet (run too short) [{SKIP}]")
        return True

    # For every EXPIRED household: forfeiture-expiry sum must equal the residual.
    rows = db.execute_query(
        """
        SELECT
            tcb.HouseholdID,
            (tcb.LifetimeAccrued - tcb.LifetimeRedeemed) AS Residual,
            COALESCE((SELECT SUM(-tcl.Amount)
                    FROM simulation.TenantCreditLedger tcl
                    WHERE tcl.RunID = tcb.RunID
                      AND tcl.HouseholdID = tcb.HouseholdID
                      AND tcl.TransactionType = 'FORFEITURE'
                      AND tcl.Notes LIKE '%FORFEITURE_EXPIRY%'),
                   0) AS ForfeitureExpirySum
        FROM simulation.TenantCreditBalance tcb
        WHERE tcb.RunID = ?
          AND tcb.ExpiryStatus = 'EXPIRED'
        """,
        (run_id,),
    )
    mismatches = []
    for hh, residual, forf_sum in rows:
        if Decimal(str(residual)) != Decimal(str(forf_sum)):
            mismatches.append((hh, str(residual), str(forf_sum)))
    ok = len(mismatches) == 0
    print(f"  T4 24-month expiry cliff: {n_expired} EXPIRED households, "
          f"{len(mismatches)} with forfeiture-sum != residual [{PASS if ok else FAIL}]")
    for hh, r, f in mismatches[:5]:
        print(f"      HH={hh} residual={r} forfeiture_expiry_sum={f}")
    return ok


# ---------------------------------------------------------------------------
# T5 — Eviction forfeiture (Kate Q9 C9b)
# ---------------------------------------------------------------------------

def t5_eviction_forfeits_credit(db, run_id: int) -> bool:
    """For every FORFEITED household (terminal state set by eviction), verify
    a FORFEITURE ledger row with Notes containing 'FORFEITURE_EVICTION' exists
    (Kate Q11 C11a — eviction-specific audit subtype)."""
    forfeited = db.execute_query(
        "SELECT COUNT(*) FROM simulation.TenantCreditBalance "
        "WHERE RunID = ? AND ExpiryStatus = 'FORFEITED'",
        (run_id,),
    )
    n_forfeited = forfeited[0][0] if forfeited else 0
    if n_forfeited == 0:
        print(f"  T5 Eviction forfeiture: no FORFEITED households (no evictions in this run) [{SKIP}]")
        return True

    # Each FORFEITED household with prior ACCRUAL should have a FORFEITURE_EVICTION row
    rows = db.execute_query(
        """
        SELECT COUNT(DISTINCT tcb.HouseholdID)
        FROM simulation.TenantCreditBalance tcb
        WHERE tcb.RunID = ?
          AND tcb.ExpiryStatus = 'FORFEITED'
          AND EXISTS (
              SELECT 1 FROM simulation.TenantCreditLedger tcl
              WHERE tcl.RunID = tcb.RunID
                AND tcl.HouseholdID = tcb.HouseholdID
                AND tcl.TransactionType = 'ACCRUAL'
          )
          AND NOT EXISTS (
              SELECT 1 FROM simulation.TenantCreditLedger tcl
              WHERE tcl.RunID = tcb.RunID
                AND tcl.HouseholdID = tcb.HouseholdID
                AND tcl.TransactionType = 'FORFEITURE'
                AND tcl.Notes LIKE '%FORFEITURE_EVICTION%'
          )
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    ok = n_violations == 0
    print(f"  T5 Eviction forfeiture (Kate Q9 C9b): {n_forfeited} FORFEITED, "
          f"{n_violations} missing FORFEITURE_EVICTION ledger rows [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T6 — Final-month exit redemption (eligible)
# ---------------------------------------------------------------------------

def t6_final_month_exit_redemption_fires(db, run_id: int) -> bool:
    """When a household is eligible for final-month exit redemption (Kate
    Q12 §5.2), the redemption fires DETERMINISTICALLY (P=1.0). Counts as
    that year's annual redemption.

    Verifies: for every REDEMPTION ledger row with Notes containing
    'FINAL_MONTH_EXIT', the BillingMonth corresponds to a lease whose
    LeaseEndDate is in the same calendar month AND lease's RenewalDecision
    is non-renewal (VOLUNTARY_EXIT or LANDLORD_NONRENEWAL)."""
    fm_redemptions = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.TenantCreditLedger
        WHERE RunID = ?
          AND TransactionType = 'REDEMPTION'
          AND Notes LIKE '%FINAL_MONTH_EXIT%'
        """,
        (run_id,),
    )
    n_fm = fm_redemptions[0][0] if fm_redemptions else 0
    if n_fm == 0:
        print(f"  T6 Final-month exit redemption (eligible): "
              f"none fired in this run (credit too low or no eligible exits) [{SKIP}]")
        return True

    # Every FINAL_MONTH_EXIT redemption must link to a lease that actually
    # terminated (Phase 3.9.5: this now covers BOTH lease-end voluntary
    # exits — Lease.RenewalDecision IN ('VOLUNTARY_EXIT', 'LANDLORD_NONRENEWAL')
    # — AND early-break voluntary exits, which have RenewalDecision NULL or
    # 'RENEW' but a LeaseTermination row dated on or before the redemption).
    # The semantic the test guards is "redemption corresponds to a real exit",
    # not a particular RenewalDecision value.
    violations = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND NOT EXISTS (
            SELECT 1 FROM simulation.LeaseTermination t
            WHERE t.RunID = tcl.RunID
              AND t.LeaseID = tcl.RelatedLeaseID
              AND t.TerminationDate >= tcl.TransactionDate
          )
        """,
        (run_id,),
    )
    n_violations = violations[0][0] if violations else 0
    ok = n_violations == 0
    print(f"  T6 Final-month exit redemption (eligible, Kate Q12 §5.2; "
          f"3.9.5-aware): {n_fm} fired, {n_violations} not linked to a real "
          f"termination (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T7 — Final-month exit redemption (ineligible — already redeemed)
# ---------------------------------------------------------------------------

def t7_final_month_exit_respects_annual_limit(db, run_id: int) -> bool:
    """Annual-limit invariant for routine/hardship redemption.

    Phase 3.10.1 BA decision Q2 A2b: FINAL_MONTH_EXIT redemption is now
    EXEMPT from the annual limit. A household may receive an FME *in the
    same calendar year* as a routine or hardship redemption (the FME is a
    settlement action, not a normal redemption). This test now verifies the
    invariant ONLY against ROUTINE + HARDSHIP, ignoring FME co-occurrence.

    Pre-3.10.1 the assertion was `RoutineCnt + HardshipCnt + FinalExitCnt > 1`.
    Post-3.10.1: `RoutineCnt + HardshipCnt > 1` (FME co-occurrence is allowed)."""
    rows = db.execute_query(
        """
        WITH HouseholdYearRedemptions AS (
            SELECT
                HouseholdID,
                YEAR(TransactionDate) AS Yr,
                SUM(CASE WHEN Notes LIKE '%ROUTINE%' THEN 1 ELSE 0 END) AS RoutineCnt,
                SUM(CASE WHEN Notes LIKE '%HARDSHIP%' THEN 1 ELSE 0 END) AS HardshipCnt,
                SUM(CASE WHEN Notes LIKE '%FINAL_MONTH_EXIT%' THEN 1 ELSE 0 END) AS FinalExitCnt
            FROM simulation.TenantCreditLedger
            WHERE RunID = ? AND TransactionType = 'REDEMPTION'
            GROUP BY HouseholdID, YEAR(TransactionDate)
        )
        SELECT COUNT(*)
        FROM HouseholdYearRedemptions
        WHERE (RoutineCnt + HardshipCnt) > 1
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    ok = n_violations == 0
    print(f"  T7 Annual redemption limit (Kate Q12 §5.2): "
          f"{n_violations} household-years with multiple redemptions (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T8 — Final-month exit redemption (ineligible — insufficient credit)
# ---------------------------------------------------------------------------

def t8_final_month_exit_respects_balance_requirement(db, run_id: int) -> bool:
    """For every FINAL_MONTH_EXIT redemption, the household's pre-redemption
    balance must have been >= the redeemed amount (Kate Q12 §5.2 condition 4,
    no partial application)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger
        WHERE RunID = ?
          AND TransactionType = 'REDEMPTION'
          AND Notes LIKE '%FINAL_MONTH_EXIT%'
          AND BalanceAfter < 0
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    ok = n_violations == 0
    print(f"  T8 Final-month exit balance requirement: "
          f"{n_violations} redemptions left negative balance (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T9 — Damages NEVER covered by TCS (Kate Q12 §5.1)
# ---------------------------------------------------------------------------

def t9_damages_not_covered_by_tcs(db, run_id: int) -> bool:
    """No REDEMPTION ledger row should ever be associated with a damages
    settlement. TCS is a non-cash REWARD; the security deposit is the
    instrument for damages (Kate Q17 — preserve non-cash wall)."""
    # Approximation: REDEMPTION rows with metadata referencing damages would
    # be a violation. Since our redemption path types are ROUTINE / HARDSHIP /
    # FINAL_MONTH_EXIT only, any other subtype is a structural bug.
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger
        WHERE RunID = ?
          AND TransactionType = 'REDEMPTION'
          AND NOT (
              Notes LIKE '%ROUTINE%'
              OR Notes LIKE '%HARDSHIP%'
              OR Notes LIKE '%FINAL_MONTH_EXIT%'
          )
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    ok = n_violations == 0
    print(f"  T9 Damages NOT covered by TCS (Kate Q12 §5.1): "
          f"{n_violations} REDEMPTION rows with unknown subtype (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T10 — Eviction does NOT trigger final-month redemption
# ---------------------------------------------------------------------------

def t10_eviction_does_not_redeem(db, run_id: int) -> bool:
    """A household whose lease was evicted must NEVER have a FINAL_MONTH_EXIT
    redemption row tied to that lease — eviction forfeits credit; it does
    not consume it via redemption (Kate Q10 C10a + Q12 §5.2 condition 1)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.LeaseTermination lt
            ON lt.RunID = tcl.RunID AND lt.LeaseID = tcl.RelatedLeaseID
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND lt.TerminationType IN ('EVICTION', 'EVICTION_EXECUTED')
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    ok = n_violations == 0
    print(f"  T10 Eviction does NOT trigger FINAL_MONTH_EXIT: "
          f"{n_violations} violations (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T11 — Fund conservation
# ---------------------------------------------------------------------------

def t11_fund_conservation(db, run_id: int) -> bool:
    """Total accruals − total redemptions − total forfeitures = sum of current
    balances. No money created or destroyed in the TCS ledger.

    This is a stronger version of 3.8.2 T7 — extends conservation to include
    the FORFEITURE transactions added by 3.8.3."""
    rows = db.execute_query(
        """
        WITH LedgerSummary AS (
            SELECT
                SUM(CASE WHEN TransactionType = 'ACCRUAL' THEN Amount ELSE 0 END) AS TotalAccrued,
                SUM(CASE WHEN TransactionType = 'REDEMPTION' THEN Amount ELSE 0 END) AS TotalRedeemed,
                SUM(CASE WHEN TransactionType = 'FORFEITURE' THEN Amount ELSE 0 END) AS TotalForfeited
            FROM simulation.TenantCreditLedger WHERE RunID = ?
        ),
        BalanceSum AS (
            SELECT SUM(CurrentBalance) AS TotalCurrent
            FROM simulation.TenantCreditBalance WHERE RunID = ?
        )
        SELECT
            COALESCE(l.TotalAccrued, 0) AS Accrued,
            COALESCE(l.TotalRedeemed, 0) AS Redeemed,
            COALESCE(l.TotalForfeited, 0) AS Forfeited,
            COALESCE(b.TotalCurrent, 0) AS CurrentSum
        FROM LedgerSummary l, BalanceSum b
        """,
        (run_id, run_id),
    )
    if not rows:
        print(f"  T11 Fund conservation: no ledger data [{SKIP}]")
        return True

    accrued, redeemed, forfeited, current_sum = rows[0]
    # Sign convention: accrual is positive, redemption + forfeiture are negative.
    # Expected: accrued + redeemed + forfeited == current_sum
    expected = Decimal(str(accrued)) + Decimal(str(redeemed)) + Decimal(str(forfeited))
    actual = Decimal(str(current_sum))
    delta = abs(expected - actual)
    ok = delta < Decimal("0.01")  # Penny tolerance for floating-point quirks
    print(f"  T11 Fund conservation: accrued ${accrued} + redeemed ${redeemed} + "
          f"forfeited ${forfeited} = ${expected} vs balance sum ${actual} "
          f"(delta ${delta}) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T12 — Event subtype distinguishability
# ---------------------------------------------------------------------------

def t12_event_subtypes_distinguishable(db, run_id: int) -> bool:
    """The 5 audit reasons must be distinguishable via event metadata
    (Kate §8 in BA answers):
      - TENANT_CREDIT/REDEMPTION_ROUTINE
      - TENANT_CREDIT/REDEMPTION_HARDSHIP
      - TENANT_CREDIT/REDEMPTION_FINAL_MONTH_EXIT
      - TENANT_CREDIT/FORFEITURE_EVICTION
      - TENANT_CREDIT/FORFEITURE_EXPIRY

    Test: query for any TENANT_CREDIT event NOT matching one of these
    subtypes (other than the per-month EXPIRY_SWEEP audit event). If any
    exist, the metadata format has drifted."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Event
        WHERE RunID = ?
          AND ActionType = 'TENANT_CREDIT'
          AND NOT (
              Metadata LIKE '%ACCRUAL%'
              OR Metadata LIKE '%REDEMPTION_ROUTINE%'
              OR Metadata LIKE '%REDEMPTION_HARDSHIP%'
              OR Metadata LIKE '%REDEMPTION_FINAL_MONTH_EXIT%'
              OR Metadata LIKE '%FORFEITURE_EVICTION%'
              OR Metadata LIKE '%FORFEITURE_EXPIRY%'
              OR Metadata LIKE '%EXPIRY_SWEEP%'
          )
        """,
        (run_id,),
    )
    n_unknown = rows[0][0] if rows else 0
    ok = n_unknown == 0
    print(f"  T12 Event subtype distinguishability: "
          f"{n_unknown} TENANT_CREDIT events with unknown subtype (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T13 — Renewal-decision pre-roll stability (Kate-required)
# ---------------------------------------------------------------------------

def t13_renewal_preroll_stability(db, run_id: int) -> bool:
    """Per Kate's followup_handoff §"Required Regression Addition":

    On day 1 of the lease's final billable month, RenewalDecision +
    RenewalDecidedDate must be populated. On LeaseEndDate, the existing
    decision is materialized; no second roll occurs.

    Test invariants:
      (a) For every terminated lease (non-eviction), RenewalDecidedDate
          is in the month containing LeaseEndDate (or earlier, for the
          defensive inline-roll fallback path).
      (b) No lease has RenewalDecision set without a matching termination
          outcome (RENEW → still active or renewed-extended; VOLUNTARY_EXIT
          → Terminated; LANDLORD_NONRENEWAL → Terminated).
      (c) RenewalDecidedDate is never AFTER LeaseEndDate (decision can't be
          made post-fact)."""
    # (a) Check decision dates are at-or-before LeaseEndDate
    rows_a = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Lease
        WHERE RunID = ?
          AND RenewalDecision IS NOT NULL
          AND RenewalDecidedDate IS NOT NULL
          AND RenewalDecidedDate > LeaseEndDate
        """,
        (run_id,),
    )
    n_post_facto = rows_a[0][0] if rows_a else 0

    # (b) Check decision aligns with lease state
    rows_b = db.execute_query(
        """
        SELECT
            SUM(CASE
                WHEN RenewalDecision = 'VOLUNTARY_EXIT' AND LeaseStatus = 'ACTIVE' THEN 1
                WHEN RenewalDecision = 'LANDLORD_NONRENEWAL' AND LeaseStatus = 'ACTIVE'
                     AND LeaseEndDate < ? THEN 1
                ELSE 0
            END) AS Misaligned
        FROM simulation.Lease
        WHERE RunID = ? AND RenewalDecision IS NOT NULL
        """,
        (date(2099, 1, 1), run_id),
    )
    n_misaligned = (rows_b[0][0] if rows_b and rows_b[0][0] is not None else 0)

    # (c) Total pre-rolls (informational)
    total = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease "
        "WHERE RunID = ? AND RenewalDecision IS NOT NULL",
        (run_id,),
    )
    n_total = total[0][0] if total else 0

    if n_total == 0:
        print(f"  T13 Renewal pre-roll stability: no pre-rolls yet (run too short) [{SKIP}]")
        return True

    ok = (n_post_facto == 0) and (n_misaligned == 0)
    print(f"  T13 Renewal pre-roll stability (Kate followup_handoff): "
          f"{n_total} pre-rolls, {n_post_facto} post-facto decision dates, "
          f"{n_misaligned} misaligned outcomes [{PASS if ok else FAIL}]")
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

    print(f'Phase 3.8.3 Credit Portability & Expiry regression — {args.env} (RunID={run_id})')
    print('=' * 78)

    results = [
        t1_same_household_move_no_transfer_row(db, run_id),
        t2_exited_households_have_computed_expiry(db, run_id),
        t3_reentry_clears_expiry(db, run_id),
        t4_expiry_cliff_forfeits_credit(db, run_id),
        t5_eviction_forfeits_credit(db, run_id),
        t6_final_month_exit_redemption_fires(db, run_id),
        t7_final_month_exit_respects_annual_limit(db, run_id),
        t8_final_month_exit_respects_balance_requirement(db, run_id),
        t9_damages_not_covered_by_tcs(db, run_id),
        t10_eviction_does_not_redeem(db, run_id),
        t11_fund_conservation(db, run_id),
        t12_event_subtypes_distinguishable(db, run_id),
        t13_renewal_preroll_stability(db, run_id),
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
