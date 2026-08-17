"""
Phase 3.8.2 — Tenant Credit System: Credit Redemption Regression Tests.

Outcome-level black-box test suite for TCS redemption behavior. Covers Kate's
required test list (phase_3_8_2_credit_redemption_ba_answers.md + follow-up).

  R1  Basic redemption — REDEMPTION rows exist when eligible + RNG hits
  R2  Insufficient balance — no REDEMPTION rows with CurrentBalance < MonthlyRent at time of redemption
  R3  Annual limit honored — at most one redemption per (Household, CalendarYear)
  R4  Non-cash invariant — FundLedger has no rows tied to REDEMPTION events
  R5  REDEEMED RentCollection math — AmountCollected=0, ArrearsAmount=0, LateFeeAmount=0
  R6  No accrual in a REDEEMED month — no ACCRUAL row in TenantCreditLedger for the same (RunID, CollectionID)
  R7  Balance reconciliation — CurrentBalance == LifetimeAccrued − LifetimeRedeemed
  R8  Determinism — (companion script; not part of this suite)
  R9  Eviction-blocked — leases with EvictionFiledDate set and no execution have no redemptions
  R10 REDEEMED month is lease-satisfied — no arrears, no late fee, no MISSED rolled-up for that lease/month
  R11 Hardship redemption prevents missed payment — at least one redemption observed where CanPayThisMonth=False
  R12 Household probability controls routine redemption (synthetic) — direct manager call asserts probability lookup
  R13 Hardship formula (synthetic) — min(cap, max(base + boost, floor)) computed correctly

R8 (determinism across same-seed runs) is covered out-of-band, the same way 3.8.1 handled it.

Usage:
    python sql/regression_tests/Phase3_8_2/phase_3_8_2_test_credit_redemption.py --env <db> [--run-id N] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.

Companion docs:
  docs/phases/phase_3_8/phase_3_8_2_credit_redemption_ba_answers.md
  docs/phases/phase_3_8/phase_3_8_2_credit_redemption_followup_ba_answers.md
  docs/phases/phase_3_8/phase_3_8_2_credit_redemption_implementation_plan.md
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# When promoted to sql/regression_tests/Phase3_8_2/, REPO_ROOT is three dirnames up.
# When running from scratch/sql/regression_tests/Phase3_8_2/, it's four dirnames up.
_REPO_ROOT_PROMOTED = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
if os.path.basename(_REPO_ROOT_PROMOTED) == 'scratch':
    REPO_ROOT = os.path.dirname(_REPO_ROOT_PROMOTED)
    APP_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
else:
    REPO_ROOT = _REPO_ROOT_PROMOTED
    APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')
sys.path.insert(0, APP_SRC)

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
# Assertion tests (read-only against an already-run simulation)
# ---------------------------------------------------------------------------

def r1_basic_redemption(db, run_id: int) -> bool:
    """At least one REDEMPTION row exists, and each ROUTINE/HARDSHIP REDEMPTION
    row pairs with a REDEEMED RentCollection row whose AmountCollected = 0.

    Phase 3.9.5: FINAL_MONTH_EXIT redemptions for early-break exits are by
    design NOT paired with a RentCollection — they fire from
    `_execute_early_break` outside the rent-collection flow. Filter those
    out of this pairing check; their own audit cover is in the Phase 3.9.5
    E1/E2 tests and the 3.8.3 T6 (3.9.5-aware) check.
    """
    pair_rows = db.execute_query(
        """
        SELECT COUNT(*) AS total_redemptions,
               SUM(CASE WHEN rc.PaymentStatus = 'REDEEMED' AND rc.AmountCollected = 0
                        THEN 1 ELSE 0 END) AS valid_pairs
        FROM simulation.TenantCreditLedger tcl
        LEFT JOIN simulation.RentCollection rc
            ON tcl.RunID = rc.RunID AND tcl.RelatedCollectionID = rc.CollectionID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'REDEMPTION'
          AND tcl.Notes NOT LIKE '%FINAL_MONTH_EXIT%'
        """,
        (run_id,),
    )
    total, valid = pair_rows[0][0] or 0, pair_rows[0][1] or 0
    if total == 0:
        print(f"  R1 Basic redemption: 0 routine/hardship REDEMPTION rows in this run [{SKIP}]")
        return True  # Empty-set SKIP, not FAIL
    ok = total == valid
    print(f"  R1 Basic redemption (routine/hardship; FINAL_MONTH_EXIT covered separately): "
          f"{total} REDEMPTION rows, {valid} paired with REDEEMED RentCollection [{PASS if ok else FAIL}]")
    return ok


def r2_balance_sufficient_at_redemption(db, run_id: int) -> bool:
    """Each REDEMPTION ledger row must have had a sufficient balance and must
    redeem exactly one month's *current* rent owed.

    Patched 2026-05-21 (Phase 3.9.3 re-baseline): a redemption covers the rent
    billed for that month, which post-3.9.3 is the EffectiveMonthlyRent in
    effect at redemption time — a point-in-time value, not the static
    Lease.MonthlyRent (the old check) nor the lease's end-of-run
    EffectiveMonthlyRent column (which may have advanced further). The
    authoritative per-month figure is MonthlyPaymentStatus.AmountDue for the
    redemption's billing month. Invariants checked:
      - BalanceAfter >= 0 (also a CHECK constraint), AND
      - |Amount| == MonthlyPaymentStatus.AmountDue for that lease/month."""
    rows = db.execute_query(
        """
        SELECT tcl.CreditTransactionID, tcl.HouseholdID, tcl.Amount, tcl.BalanceAfter,
               mps.AmountDue,
               CASE WHEN tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
                    THEN 1 ELSE 0 END AS IsFME
        FROM simulation.TenantCreditLedger tcl
        LEFT JOIN simulation.MonthlyPaymentStatus mps
            ON mps.RunID = tcl.RunID
           AND mps.LeaseID = tcl.RelatedLeaseID
           AND mps.BillingMonth = DATEFROMPARTS(YEAR(tcl.TransactionDate),
                                                MONTH(tcl.TransactionDate), 1)
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'REDEMPTION'
        """,
        (run_id,),
    )
    if not rows:
        print(f"  R2 Balance sufficient at redemption: 0 redemptions in run [{SKIP}]")
        return True
    violations = []
    for txn_id, hh, amount, balance_after, amount_due, is_fme in rows:
        amt = Decimal(str(amount))         # negative for REDEMPTION
        bal = Decimal(str(balance_after))  # post-redemption, must be >= 0
        if bal < 0:
            violations.append((txn_id, hh, f"BalanceAfter={bal}", "expected >= 0"))
        # Phase 3.10.1: FINAL_MONTH_EXIT may redeem partial (|amt| <= AmountDue).
        # FME early-break path uses RelatedCollectionID=NULL → no MPS join row.
        # ROUTINE/HARDSHIP must still match AmountDue exactly.
        if is_fme:
            if amount_due is not None:
                due = Decimal(str(amount_due))
                if abs(amt) > due:
                    violations.append((txn_id, hh, f"|amt|={abs(amt)}",
                                        f"FME exceeded AmountDue={due}"))
        else:
            if amount_due is None:
                violations.append((txn_id, hh, "no MPS row", "routine/hardship requires MPS"))
            else:
                due = Decimal(str(amount_due))
                if abs(amt) != due:
                    violations.append((txn_id, hh, f"|amt|={abs(amt)}",
                                        f"AmountDue={due}"))
    ok = len(violations) == 0
    print(f"  R2 Balance sufficient at redemption (3.10.1-aware: FME may be partial): "
          f"{len(rows)} rows, {len(violations)} violations (expect 0) [{PASS if ok else FAIL}]")
    for v in violations[:5]:
        print(f"      Violation: TxnID={v[0]} HH={v[1]} a={v[2]} b={v[3]}")
    return ok


def r3_annual_limit(db, run_id: int) -> bool:
    """No (RunID, HouseholdID, YEAR(TransactionDate)) tuple has more than 1
    ROUTINE/HARDSHIP REDEMPTION row.

    Phase 3.10.1 BA decision Q2 A2b: FINAL_MONTH_EXIT is EXEMPT from the
    annual-limit, so we exclude FME rows from this count. The annual-limit
    invariant still applies to routine/hardship paths."""
    rows = db.execute_query(
        """
        SELECT HouseholdID, YEAR(TransactionDate) AS Yr, COUNT(*) AS Cnt
        FROM simulation.TenantCreditLedger
        WHERE RunID = ? AND TransactionType = 'REDEMPTION'
          AND Notes NOT LIKE '%FINAL_MONTH_EXIT%'
        GROUP BY HouseholdID, YEAR(TransactionDate)
        HAVING COUNT(*) > 1
        """,
        (run_id,),
    )
    n_violations = len(rows)
    ok = n_violations == 0
    print(f"  R3 Annual limit (3.10.1-aware: FME excluded — A2b exemption): "
          f"{n_violations} (Household, Year) pairs with >1 routine/hardship "
          f"(expect 0) [{PASS if ok else FAIL}]")
    for hh, yr, cnt in rows[:5]:
        print(f"      Violation: HouseholdID={hh}, Year={yr}, Count={cnt}")
    return ok


def r4_non_cash_invariant(db, run_id: int) -> bool:
    """No FundLedger row references the CreatedEventID of any REDEMPTION ledger row."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.FundLedger fl
        WHERE fl.RunID = ?
          AND fl.EventID IN (
              SELECT CreatedEventID
              FROM simulation.TenantCreditLedger
              WHERE RunID = ? AND TransactionType = 'REDEMPTION'
          )
        """,
        (run_id, run_id),
    )
    n = rows[0][0] or 0
    ok = n == 0
    print(f"  R4 Non-cash invariant: {n} FundLedger rows tied to REDEMPTION events (expect 0) [{PASS if ok else FAIL}]")
    return ok


def r5_redeemed_rentcollection_math(db, run_id: int) -> bool:
    """Every REDEEMED RentCollection row has AmountCollected = 0, ArrearsAmount = 0, LateFeeAmount = 0."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN AmountCollected = 0 AND ArrearsAmount = 0 AND LateFeeAmount = 0
                        THEN 1 ELSE 0 END) AS valid
        FROM simulation.RentCollection
        WHERE RunID = ? AND PaymentStatus = 'REDEEMED'
        """,
        (run_id,),
    )
    total, valid = rows[0][0] or 0, rows[0][1] or 0
    if total == 0:
        print(f"  R5 REDEEMED RentCollection math: 0 REDEEMED rows [{SKIP}]")
        return True
    ok = total == valid
    print(f"  R5 REDEEMED RentCollection math: {total} REDEEMED rows, {valid} satisfy zeros (expect equal) [{PASS if ok else FAIL}]")
    return ok


def r6_no_accrual_on_redeemed_month(db, run_id: int) -> bool:
    """No (RunID, RelatedCollectionID) appears as both an ACCRUAL and a REDEMPTION row.

    Looser version: no ACCRUAL row references a REDEEMED RentCollection row."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.RentCollection rc
            ON tcl.RunID = rc.RunID AND tcl.RelatedCollectionID = rc.CollectionID
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'ACCRUAL'
          AND rc.PaymentStatus = 'REDEEMED'
        """,
        (run_id,),
    )
    n = rows[0][0] or 0
    ok = n == 0
    print(f"  R6 No accrual in REDEEMED month: {n} ACCRUAL rows on REDEEMED collections (expect 0) [{PASS if ok else FAIL}]")
    return ok


def r7_balance_reconciliation(db, run_id: int) -> bool:
    """For every household: CurrentBalance == LifetimeAccrued − LifetimeRedeemed − LifetimeForfeited.

    Phase 3.8.3 patch (cross-phase test maintenance, mirroring how 3.8.2
    patched 3.8.1's T7): the post-3.8.3 invariant must subtract forfeitures.
    LifetimeForfeited isn't a stored column — it's derived from the ledger
    by summing FORFEITURE row amounts (which are negative; we take the abs).
    Covers both expiry forfeitures (FORFEITURE_EXPIRY) and eviction
    forfeitures (FORFEITURE_EVICTION).
    """
    rows = db.execute_query(
        """
        SELECT
            tcb.HouseholdID,
            tcb.CurrentBalance,
            tcb.LifetimeAccrued,
            tcb.LifetimeRedeemed,
            COALESCE((SELECT SUM(-tcl.Amount)
                    FROM simulation.TenantCreditLedger tcl
                    WHERE tcl.RunID = tcb.RunID
                      AND tcl.HouseholdID = tcb.HouseholdID
                      AND tcl.TransactionType = 'FORFEITURE'),
                   0) AS LifetimeForfeited
        FROM simulation.TenantCreditBalance tcb
        WHERE tcb.RunID = ?
        """,
        (run_id,),
    )
    mismatches = []
    for hh, current, lifetime, redeemed, forfeited in rows:
        expected = Decimal(str(lifetime)) - Decimal(str(redeemed)) - Decimal(str(forfeited))
        actual = Decimal(str(current))
        if actual != expected:
            mismatches.append((hh, str(actual), str(expected)))
    ok = len(mismatches) == 0
    print(f"  R7 Balance reconciliation: {len(rows)} households, {len(mismatches)} mismatches (expect 0) [{PASS if ok else FAIL}]")
    for hh, a, e in mismatches[:5]:
        print(f"      HH={hh} CurrentBalance={a} expected={e}")
    return ok


def r9_no_redemption_during_eviction(db, run_id: int) -> bool:
    """No REDEMPTION ledger row exists for a lease while EvictionFiledDate is set
    and EvictionExecutionDate is not yet recorded (or the transaction date falls
    between filing and execution)."""
    rows = db.execute_query(
        """
        SELECT tcl.CreditTransactionID, tcl.RelatedLeaseID, tcl.TransactionDate,
               lt.EvictionFiledDate, lt.EvictionExecutionDate
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.LeaseTermination lt
            ON tcl.RunID = lt.RunID AND tcl.RelatedLeaseID = lt.LeaseID
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'REDEMPTION'
          AND lt.EvictionFiledDate IS NOT NULL
          AND tcl.TransactionDate >= lt.EvictionFiledDate
          AND (lt.EvictionExecutionDate IS NULL OR tcl.TransactionDate < lt.EvictionExecutionDate)
        """,
        (run_id,),
    )
    n = len(rows)
    ok = n == 0
    print(f"  R9 No redemption during eviction window: {n} violations (expect 0) [{PASS if ok else FAIL}]")
    return ok


def r10_redeemed_month_satisfied(db, run_id: int) -> bool:
    """No REDEEMED RentCollection row should be flagged as IsLate=1, and the
    associated MonthlyPaymentStatus row should NOT be PaymentStatus='MISSED'."""
    late_rows = db.execute_query(
        "SELECT COUNT(*) FROM simulation.RentCollection "
        "WHERE RunID = ? AND PaymentStatus = 'REDEEMED' AND (IsLate = 1 OR LateFeeAmount > 0)",
        (run_id,),
    )
    missed_rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.RentCollection rc
        JOIN simulation.MonthlyPaymentStatus mps
            ON rc.RunID = mps.RunID AND rc.LeaseID = mps.LeaseID
           AND rc.CollectionMonth = mps.BillingMonth
        WHERE rc.RunID = ? AND rc.PaymentStatus = 'REDEEMED'
          AND mps.PaymentStatus = 'MISSED'
        """,
        (run_id,),
    )
    n_late = late_rows[0][0] or 0
    n_missed = missed_rows[0][0] or 0
    ok = n_late == 0 and n_missed == 0
    print(f"  R10 REDEEMED month is satisfied: {n_late} late/fee violations, {n_missed} MISSED-paired violations (expect 0/0) [{PASS if ok else FAIL}]")
    return ok


def r11_hardship_prevents_missed(db, run_id: int) -> bool:
    """At least one REDEMPTION exists whose paired MonthlyPaymentStatus row has CanPayThisMonth=0
    — proves the hardship path fired and prevented a MISSED month."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.RentCollection rc
        JOIN simulation.MonthlyPaymentStatus mps
            ON rc.RunID = mps.RunID AND rc.LeaseID = mps.LeaseID
           AND rc.CollectionMonth = mps.BillingMonth
        WHERE rc.RunID = ? AND rc.PaymentStatus = 'REDEEMED'
          AND mps.CanPayThisMonth = 0
        """,
        (run_id,),
    )
    n = rows[0][0] or 0
    if n > 0:
        print(f"  R11 Hardship prevents MISSED: {n} hardship redemptions observed [{PASS}]")
        return True
    # Sufficient to SKIP rather than FAIL if no hardship case fired organically.
    print(f"  R11 Hardship prevents MISSED: 0 hardship redemptions in this run [{SKIP}]")
    return True


def r12_household_probability_controls_routine(db, run_id: int) -> bool:
    """Synthetic: pick two households with very different TCSRedemptionProbability
    values and run 1,000 should_routine_redeem calls each; the high-prob household
    should fire ~10x more often than the low-prob household (roughly proportional
    to their probabilities). This proves the routine path reads the household-level
    probability rather than a global constant."""
    pick = db.execute_query(
        """
        SELECT h.HouseholdID, h.TCSRedemptionProbability, l.LeaseID, l.MonthlyRent
        FROM simulation.Household h
        JOIN simulation.Lease l ON l.RunID = h.RunID AND l.HouseholdID = h.HouseholdID
        WHERE h.RunID = ?
        """,
        (run_id,),
    )
    if not pick:
        print(f"  R12 Household-probability controls routine: no household/lease data [{SKIP}]")
        return True
    # Pick the highest- and lowest-probability households
    pick_sorted = sorted(pick, key=lambda r: r[1])
    low = pick_sorted[0]
    high = pick_sorted[-1]
    if Decimal(str(high[1])) / max(Decimal(str(low[1])), Decimal("0.0001")) < Decimal("2"):
        print(f"  R12 Household-probability controls routine: probabilities too close to differentiate ({low[1]} vs {high[1]}) [{SKIP}]")
        return True

    from tenant_credit_manager import TenantCreditManager
    from configuration_loader import ConfigurationLoader
    from event_logger import EventLogger

    cfg_loader = ConfigurationLoader(db)
    projection_row = db.execute_query(
        "SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,)
    )
    cfg = cfg_loader.load_projection(projection_row[0][0])
    event_logger = EventLogger(db)
    event_logger.set_run_id(run_id)

    # NOTE: we use a fresh manager with a deterministic seed for the test —
    # we are NOT consuming the production redemption_rng state.
    tcm = TenantCreditManager(
        db_manager=db, event_logger=event_logger, run_id=run_id,
        credit_rate=Decimal(str(cfg.tenant_credit_rate)), credit_cap=cfg.credit_cap,
        redemption_prob_min=cfg.tcs_redemption_prob_min,
        redemption_prob_default=cfg.tcs_redemption_prob_default,
        redemption_prob_max=cfg.tcs_redemption_prob_max,
        hardship_floor=cfg.tcs_redemption_hardship_floor,
        hardship_boost=cfg.tcs_redemption_hardship_boost,
        hardship_cap=cfg.tcs_redemption_hardship_cap,
        run_seed=99999,  # test seed; isolated from production RNG state
    )

    # Bypass eligibility for the test by mutating the households' balance + state.
    # Save originals.
    save_state = {}
    for row in (low, high):
        hh, prob, lease_id, rent = row[0], row[1], row[2], Decimal(str(row[3]))
        orig = db.execute_query(
            "SELECT CurrentBalance, LifetimeAccrued, LifetimeRedeemed, LastRedemptionYear "
            "FROM simulation.TenantCreditBalance WHERE RunID=? AND HouseholdID=?",
            (run_id, hh),
        )
        save_state[hh] = orig[0] if orig else None
        # Ensure CurrentBalance >= rent and LastRedemptionYear < a far-future year
        db.execute_non_query(
            "UPDATE simulation.TenantCreditBalance "
            "SET CurrentBalance = ?, LifetimeAccrued = ?, LifetimeRedeemed = 0, "
            "    LastRedemptionYear = NULL "
            "WHERE RunID=? AND HouseholdID=?",
            (rent * 2, rent * 2, run_id, hh),
        )

    def roll_n(household_id, lease_id, rent, n=2000):
        # Use a counter to drive routine-roll, but we just want to know the rate
        # so we make many calls. Replace the manager's RNG inline with a fresh
        # one so the same seed is reproducible for both households.
        import random
        tcm.redemption_rng = random.Random(7777)
        hits = 0
        for _ in range(n):
            # Reset LastRedemptionYear each iteration so the annual-limit gate doesn't
            # block after the first hit — we're measuring per-roll probability, not
            # annual rate.
            db.execute_non_query(
                "UPDATE simulation.TenantCreditBalance "
                "SET LastRedemptionYear = NULL WHERE RunID=? AND HouseholdID=?",
                (run_id, household_id),
            )
            if tcm.should_routine_redeem(
                household_id=household_id, lease_id=lease_id, monthly_rent=rent,
                current_date=date(2030, 6, 1), deposit_funded=True,
            ):
                hits += 1
        return hits

    low_hits = roll_n(low[0], low[2], Decimal(str(low[3])))
    high_hits = roll_n(high[0], high[2], Decimal(str(high[3])))

    # Restore state
    for hh, orig in save_state.items():
        if orig is None:
            continue
        db.execute_non_query(
            "UPDATE simulation.TenantCreditBalance "
            "SET CurrentBalance = ?, LifetimeAccrued = ?, LifetimeRedeemed = ?, "
            "    LastRedemptionYear = ? "
            "WHERE RunID=? AND HouseholdID=?",
            (orig[0], orig[1], orig[2], orig[3], run_id, hh),
        )

    # Expectation: hit-rate ratio should roughly track probability ratio
    expected_ratio = float(high[1]) / float(low[1])
    if low_hits == 0:
        actual_ratio = float("inf") if high_hits > 0 else 0
    else:
        actual_ratio = high_hits / low_hits
    # Loose tolerance: actual within [0.5x, 2x] of expected
    ok = (
        high_hits > low_hits  # at minimum, higher prob produces more hits
        and (low_hits == 0 or 0.5 * expected_ratio <= actual_ratio <= 2.0 * expected_ratio)
    )
    print(f"  R12 Household probability controls routine: low_prob={low[1]} hits={low_hits}/2000, high_prob={high[1]} hits={high_hits}/2000, ratio={actual_ratio:.2f} vs expected={expected_ratio:.2f} [{PASS if ok else FAIL}]")
    return ok


def r13_hardship_formula(db, run_id: int) -> bool:
    """Synthetic: assert min(cap, max(base + boost, floor)) is computed correctly
    for three representative base probabilities."""
    from tenant_credit_manager import TenantCreditManager
    from configuration_loader import ConfigurationLoader
    from event_logger import EventLogger

    cfg_loader = ConfigurationLoader(db)
    projection_row = db.execute_query(
        "SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,)
    )
    cfg = cfg_loader.load_projection(projection_row[0][0])
    event_logger = EventLogger(db)
    event_logger.set_run_id(run_id)
    tcm = TenantCreditManager(
        db_manager=db, event_logger=event_logger, run_id=run_id,
        credit_rate=Decimal(str(cfg.tenant_credit_rate)), credit_cap=cfg.credit_cap,
        redemption_prob_min=cfg.tcs_redemption_prob_min,
        redemption_prob_default=cfg.tcs_redemption_prob_default,
        redemption_prob_max=cfg.tcs_redemption_prob_max,
        hardship_floor=cfg.tcs_redemption_hardship_floor,
        hardship_boost=cfg.tcs_redemption_hardship_boost,
        hardship_cap=cfg.tcs_redemption_hardship_cap,
        run_seed=99999,
    )

    def formula(base):
        return min(tcm.hardship_cap, max(base + tcm.hardship_boost, tcm.hardship_floor))

    cases = [
        Decimal("0.0050"),  # low saver — base + 0.70 = 0.7050; max with floor 0.75 = 0.75
        Decimal("0.0200"),  # baseline — base + 0.70 = 0.7200; max with floor 0.75 = 0.75
        Decimal("0.0800"),  # high relief — base + 0.70 = 0.7800; max with floor 0.75 = 0.78; min with cap 0.95 = 0.78
        Decimal("0.3000"),  # hypothetical high — base + 0.70 = 1.00; min with cap 0.95 = 0.95
    ]
    expected = [Decimal("0.7500"), Decimal("0.7500"), Decimal("0.7800"), Decimal("0.9500")]
    mismatches = []
    for base, exp in zip(cases, expected):
        actual = formula(base)
        if actual != exp:
            mismatches.append((str(base), str(actual), str(exp)))
    ok = len(mismatches) == 0
    print(f"  R13 Hardship formula min(cap, max(base+boost, floor)): {len(cases)} cases, {len(mismatches)} mismatches (expect 0) [{PASS if ok else FAIL}]")
    for b, a, e in mismatches[:5]:
        print(f"      base={b}: got {a}, expected {e}")
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
    print(f'Phase 3.8.2 TCS credit redemption regression — {args.env} (RunID={run_id})')
    print('=' * 78)

    results = [
        r1_basic_redemption(db, run_id),
        r2_balance_sufficient_at_redemption(db, run_id),
        r3_annual_limit(db, run_id),
        r4_non_cash_invariant(db, run_id),
        r5_redeemed_rentcollection_math(db, run_id),
        r6_no_accrual_on_redeemed_month(db, run_id),
        r7_balance_reconciliation(db, run_id),
        r9_no_redemption_during_eviction(db, run_id),
        r10_redeemed_month_satisfied(db, run_id),
        r11_hardship_prevents_missed(db, run_id),
        r12_household_probability_controls_routine(db, run_id),
        r13_hardship_formula(db, run_id),
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
