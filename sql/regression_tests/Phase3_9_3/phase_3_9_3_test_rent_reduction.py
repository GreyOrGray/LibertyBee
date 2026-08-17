"""
Phase 3.9.3 — Rent Reduction Schedule Wiring regression tests.

Outcome-level black-box test suite for the tenure-based rent-reduction schedule
(RR_* tiers, cumulative-off-original reductions, effective-rent billing, audit
events). Covers Kate's locked decisions in
phase_3_9_3_rent_reduction_ba_answers_handoff.md and the BA plan-review
revisions in phase_3_9_3_implementation_plan_ba_review_handoff.md.

  R1  Tier-1 fires at exactly 36 months tenure, on the 1st of the month
  R2  Reductions are cumulative off original (prefix sums), never compounded
  R3  Renewal continuity — a 36+ month lease span carries an earned reduction
  R4  Turnover/young-lease reset — a lease spanning < 36 months has 0 reduction
  R5  Unit-move reset — fresh clock on a different unit (SKIPs: no internal moves)
  R6  All-NULL schedule is a safe no-op (logic test)
  R7  Partial/malformed tier is dropped safely, others kept (logic test)
  R8  Rent collection bills EffectiveMonthlyRent after a reduction
  R9  TCS redemption uses the reduced effective rent
  R10 Idempotency — no tier applied twice to the same lease
  R11 Effective-rent consistency — EffectiveMonthlyRent = ROUND(MonthlyRent*(1-pct),2)
  R12 Event/state reconciliation — every reduced lease has reconciling events
  R13 (folded into R6) all-NULL ⇒ no reductions, no events, EffectiveMonthlyRent=MonthlyRent
  E1  Config-loader echo validation — positional index re-map left fields intact

Tests SKIP gracefully when the run doesn't exercise a particular pathway.

Usage:
    python sql/regression_tests/Phase3_9_3/phase_3_9_3_test_rent_reduction.py --env <db> [--run-id N] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_PROMOTED = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
if os.path.basename(_REPO_ROOT_PROMOTED) == 'scratch':
    REPO_ROOT = os.path.dirname(_REPO_ROOT_PROMOTED)
    APP_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
else:
    REPO_ROOT = _REPO_ROOT_PROMOTED
    APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')
# Phase 3.9.3 logic tests (R6/R7/E1) import the simulation modules. Pre-promotion
# the 3.9.3 code lives only in scratch/, so scratch must take import precedence
# over the promoted app/src copy — insert it LAST so it lands at sys.path[0].
sys.path.insert(0, APP_SRC)
sys.path.insert(0, os.path.join(REPO_ROOT, 'sql', 'regression_tests'))
from _dialect import add_months_sql, add_days_sql, month_diff_sql  # noqa: E402

_SCRATCH_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
if os.path.isdir(_SCRATCH_SRC) and _SCRATCH_SRC not in sys.path:
    sys.path.insert(0, _SCRATCH_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
SKIP = '\033[93mSKIP\033[0m'

# Canonical RR_* schedule — SOURCE: business_rules_current.md §rent-reduction
# (3 tiers: 36mo→5%, 72mo→+5%, 120mo→+10%; 20% CUMULATIVE MAX). Cite the
# governing canon, never snapshot the live seed: the pre-#104 version of this
# constant was derived from the DB (which carried the unratified 4th-tier
# artifact, KD-104) and so DEFENDED the artifact against its own correction.
# A BA "the data is authoritative" ruling authorizes the ENGINE to read the
# data — it does not make the data the specification (Phase 1.4 lesson).
CANONICAL_TIERS = [(36, Decimal('0.05')), (72, Decimal('0.05')),
                   (120, Decimal('0.10'))]
VALID_CUMULATIVE = []
_acc = Decimal('0')
for _m, _p in CANONICAL_TIERS:
    _acc += _p
    VALID_CUMULATIVE.append(_acc)  # 0.05, 0.10, 0.20


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
# R1 — Tier-1 fires at exactly 36 months tenure, on the 1st
# ---------------------------------------------------------------------------

def r1_threshold_timing(db, run_id: int) -> bool:
    """BA plan review #2: pinned threshold timing. For LeaseStartDate
    2025-01-01 the 36-month threshold fires on 2028-01-01 (start of month 37),
    never on December 2027 rent.

    Every tier-1 (TierMonths=36) RENT_REDUCTION event must satisfy:
      - EffectiveDate is the 1st of the month, AND
      - DATEDIFF(MONTH, LeaseStartDate, EffectiveDate) = 36 exactly.
    """
    total = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.Event
        WHERE RunID = ? AND ActionType = 'RENT_REDUCTION'
          AND JSON_VALUE(Metadata, '$.TierMonths') = '36'
        """,
        (run_id,),
    )
    n_tier1 = total[0][0] if total else 0
    if n_tier1 == 0:
        print(f"  R1 Threshold timing: no tier-1 reductions fired (run too short) [{SKIP}]")
        return True

    bad = db.execute_query(
        f"""
        SELECT COUNT(*)
        FROM simulation.Event e
        JOIN simulation.Lease l
          ON l.RunID = e.RunID AND l.LeaseID = e.EntityID
        WHERE e.RunID = ? AND e.ActionType = 'RENT_REDUCTION'
          AND JSON_VALUE(e.Metadata, '$.TierMonths') = '36'
          AND (
              DAY(e.EffectiveDate) <> 1
              OR {month_diff_sql(db, 'l.LeaseStartDate', 'e.EffectiveDate')} <> 36
          )
        """,
        (run_id,),
    )
    n_bad = bad[0][0] if bad else 0
    ok = n_bad == 0
    print(f"  R1 Threshold timing (BA review #2): {n_tier1} tier-1 events, "
          f"{n_bad} with wrong day-of-month or tenure != 36 [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R2 — Cumulative off original, never compounded
# ---------------------------------------------------------------------------

def r2_cumulative_not_compounded(db, run_id: int) -> bool:
    """Reductions stack by summing tier pcts (cumulative off original signed
    rent), so every reduced lease's CumulativeRentReductionPct must equal one
    of the prefix sums {0.05, 0.10, 0.20, 0.25}. Compounding would never land
    on these exact values."""
    rows = db.execute_query(
        """
        SELECT CumulativeRentReductionPct, COUNT(*)
        FROM simulation.Lease
        WHERE RunID = ? AND CumulativeRentReductionPct > 0
        GROUP BY CumulativeRentReductionPct
        """,
        (run_id,),
    )
    if not rows:
        print(f"  R2 Cumulative not compounded: no reduced leases (run too short) [{SKIP}]")
        return True
    bad = [(str(pct), n) for pct, n in rows
           if Decimal(str(pct)) not in VALID_CUMULATIVE]
    n_reduced = sum(n for _, n in rows)
    ok = len(bad) == 0
    print(f"  R2 Cumulative not compounded (BA Q3 A3a): {n_reduced} reduced leases, "
          f"{len(bad)} with off-schedule pct [{PASS if ok else FAIL}]")
    for pct, n in bad[:5]:
        print(f"      pct={pct} ({n} leases) not in {[str(v) for v in VALID_CUMULATIVE]}")
    return ok


# ---------------------------------------------------------------------------
# R3 — Renewal continuity
# ---------------------------------------------------------------------------

def r3_renewal_continuity(db, run_id: int) -> bool:
    """BA Q5 A5a: renewal continues the clock. A renewal extends LeaseEndDate
    on the SAME LeaseID, so a lease that genuinely reached its 36-month tenure
    tick *while active and within the simulation window* MUST carry at least
    the tier-1 reduction. If renewal reset the clock (e.g. a new LeaseID per
    renewal), no single lease row would ever reach a 36-month tenure tick.

    "Genuinely reached the tick" requires BOTH:
      - DATEADD(MONTH,36,LeaseStartDate) <= LeaseEndDate  (lease still ran then), AND
      - DATEADD(MONTH,36,LeaseStartDate) <= sim_end        (the sim ran that month).
    A lease created late in the run can have a renewal-extended LeaseEndDate
    past the simulation's end — its 36-month tick never arrived, so zero
    reduction is correct and is NOT a violation."""
    # Horizon = the run's ACTUAL last simulated month, not the projection's
    # configured SIM_EndDate (the full ~240-month end — wrong when the run used a
    # --months override). MAX(MonthlyPaymentStatus.BillingMonth) is the last month
    # rent was billed = the daily loop's true reach, and is NOT polluted by
    # trailing post-horizon deposit settlements. (Also drops the legacy
    # reference.ProjectionParameters read — superseded by the registry, #75.)
    se = db.execute_query(
        "SELECT MAX(BillingMonth) FROM simulation.MonthlyPaymentStatus WHERE RunID = ?",
        (run_id,),
    )
    sim_end = se[0][0] if se and se[0][0] is not None else None
    if sim_end is None:
        print(f"  R3 Renewal continuity: no billing months found (empty run) [{SKIP}]")
        return True

    eligible = db.execute_query(
        f"""
        SELECT COUNT(*) FROM simulation.Lease
        WHERE RunID = ?
          AND {add_months_sql(db, 36, 'LeaseStartDate')} <= LeaseEndDate
          AND {add_months_sql(db, 36, 'LeaseStartDate')} <= ?
        """,
        (run_id, sim_end),
    )
    n_eligible = eligible[0][0] if eligible else 0
    if n_eligible == 0:
        print(f"  R3 Renewal continuity: no leases reached a 36-month tick (run too short) [{SKIP}]")
        return True
    bad = db.execute_query(
        f"""
        SELECT COUNT(*) FROM simulation.Lease
        WHERE RunID = ?
          AND {add_months_sql(db, 36, 'LeaseStartDate')} <= LeaseEndDate
          AND {add_months_sql(db, 36, 'LeaseStartDate')} <= ?
          AND CumulativeRentReductionPct = 0
        """,
        (run_id, sim_end),
    )
    n_bad = bad[0][0] if bad else 0
    ok = n_bad == 0
    print(f"  R3 Renewal continuity (BA Q5 A5a): {n_eligible} leases reached a 36-month "
          f"tick while active, {n_bad} with zero reduction [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R4 — Turnover / young-lease reset
# ---------------------------------------------------------------------------

def r4_turnover_reset(db, run_id: int) -> bool:
    """BA Q6 A6a: turnover resets reduction to zero. Each lease is a fresh row
    with its own LeaseStartDate, so a lease that has never spanned 36 months
    (LeaseEndDate < LeaseStartDate + 36 months) cannot have earned tier 1 and
    MUST have CumulativeRentReductionPct = 0. A non-zero reduction on a
    short-span lease would mean reduction leaked across turnover or was
    unit-attached."""
    rows = db.execute_query(
        f"""
        SELECT COUNT(*) FROM simulation.Lease
        WHERE RunID = ?
          AND {month_diff_sql(db, 'LeaseStartDate', 'LeaseEndDate')} < 36
          AND CumulativeRentReductionPct > 0
        """,
        (run_id,),
    )
    n_bad = rows[0][0] if rows else 0
    ok = n_bad == 0
    print(f"  R4 Turnover/young-lease reset (BA Q6 A6a): "
          f"{n_bad} sub-36-month leases with non-zero reduction (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R5 — Unit-move reset
# ---------------------------------------------------------------------------

def r5_unit_move_reset(db, run_id: int) -> bool:
    """BA Q2 A2a: a household moving to a different LB unit starts a fresh
    rent-reduction clock on the new unit. The current simulation does not
    materialize internal unit-to-unit moves (a household exits, then a
    different household fills the vacancy), so this pathway is detected and
    SKIPs cleanly when absent. When present, the new-unit lease's reduction
    must be consistent with its OWN tenure (covered structurally by R4/R11)."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) FROM (
            SELECT HouseholdID
            FROM simulation.Lease
            WHERE RunID = ?
            GROUP BY HouseholdID
            HAVING COUNT(DISTINCT UnitID) > 1
        ) x
        """,
        (run_id,),
    )
    n_movers = rows[0][0] if rows else 0
    if n_movers == 0:
        print(f"  R5 Unit-move reset: no households occupy multiple units "
              f"(no internal moves in this run) [{SKIP}]")
        return True
    # Movers exist — their per-lease reduction is governed by per-lease tenure;
    # R4 (no reduction below 36mo span) and R11 (effective-rent consistency)
    # already enforce per-lease correctness, so a clean R4+R11 covers movers.
    print(f"  R5 Unit-move reset: {n_movers} multi-unit households present — "
          f"per-lease correctness enforced by R4 + R11 [{SKIP}]")
    return True


# ---------------------------------------------------------------------------
# R6 — All-NULL schedule no-op (logic test)
# ---------------------------------------------------------------------------

def r6_all_null_schedule_noop(db, run_id: int) -> bool:
    """BA §3.4 + plan-review R13: an all-NULL RR_* schedule is a valid no-op.
    The configuration loader yields an empty tier list, and the manager applies
    no reductions and logs no events. Verified as a logic test (no canonical
    projection has an all-NULL schedule)."""
    try:
        from configuration_loader import ConfigurationLoader
        from rent_reduction_manager import RentReductionManager
        from datetime import date

        cl = ConfigurationLoader(db)
        tiers = cl._build_rent_reduction_tiers(
            [(1, None, None), (2, None, None), (3, None, None),
             (4, None, None), (5, None, None)],
            projection_id=999,
        )
        empty_ok = tiers == []

        mgr = RentReductionManager(
            db_manager=db, event_logger=None, run_id=run_id,
            rent_reduction_tiers=tiers,
        )
        advanced = mgr.process_monthly_rent_reductions(date(2030, 1, 1))
        noop_ok = advanced == 0

        ok = empty_ok and noop_ok
        print(f"  R6 All-NULL schedule no-op (BA §3.4): empty tier list={empty_ok}, "
              f"manager applied {advanced} reductions [{PASS if ok else FAIL}]")
        return ok
    except Exception as e:
        print(f"  R6 All-NULL schedule no-op: raised {e!r} [{FAIL}]")
        return False


# ---------------------------------------------------------------------------
# R7 — Partial/malformed tier safety (logic test)
# ---------------------------------------------------------------------------

def r7_malformed_tier_safety(db, run_id: int) -> bool:
    """BA §3.5 + plan-review #7: a tier with exactly one side populated is
    malformed. It must be dropped (not crash projection loading), while
    well-formed tiers in the same schedule are kept. Verified as a logic
    test against the configuration loader."""
    try:
        from configuration_loader import ConfigurationLoader
        cl = ConfigurationLoader(db)  # event_logger=None → warns via Python logger
        tiers = cl._build_rent_reduction_tiers(
            [
                (1, 36, Decimal('0.05')),    # well-formed
                (2, 72, None),               # malformed — pct NULL
                (3, None, Decimal('0.10')),  # malformed — months NULL
                (4, 180, Decimal('0.05')),   # well-formed
                (5, None, None),             # inactive
            ],
            projection_id=999,
        )
        months = sorted(t.months for t in tiers)
        ok = months == [36, 180] and len(tiers) == 2
        print(f"  R7 Malformed tier safety (BA §3.5): kept tiers at {months}, "
              f"expected [36, 180] [{PASS if ok else FAIL}]")
        return ok
    except Exception as e:
        print(f"  R7 Malformed tier safety: raised {e!r} [{FAIL}]")
        return False


# ---------------------------------------------------------------------------
# R8 — Effective rent drives rent collection
# ---------------------------------------------------------------------------

def r8_effective_rent_drives_collection(db, run_id: int) -> bool:
    """BA §3.7: rent collection bills EffectiveMonthlyRent. For every reduced
    lease, MonthlyPaymentStatus rows for billing months strictly AFTER the
    lease's final reduction event must have AmountDue = EffectiveMonthlyRent
    (the current billable rent), not the original MonthlyRent."""
    n_reduced = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease WHERE RunID = ? AND CumulativeRentReductionPct > 0",
        (run_id,),
    )
    if not n_reduced or n_reduced[0][0] == 0:
        print(f"  R8 Effective rent drives collection: no reduced leases [{SKIP}]")
        return True
    rows = db.execute_query(
        """
        WITH FinalReduction AS (
            SELECT EntityID AS LeaseID, MAX(EffectiveDate) AS LastReductionDate
            FROM simulation.Event
            WHERE RunID = ? AND ActionType = 'RENT_REDUCTION'
            GROUP BY EntityID
        )
        SELECT COUNT(*)
        FROM simulation.MonthlyPaymentStatus mps
        JOIN simulation.Lease l
          ON l.RunID = mps.RunID AND l.LeaseID = mps.LeaseID
        JOIN FinalReduction fr ON fr.LeaseID = mps.LeaseID
        WHERE mps.RunID = ?
          AND l.CumulativeRentReductionPct > 0
          AND mps.BillingMonth > fr.LastReductionDate
          AND mps.AmountDue <> l.EffectiveMonthlyRent
        """,
        (run_id, run_id),
    )
    n_bad = rows[0][0] if rows else 0
    ok = n_bad == 0
    print(f"  R8 Effective rent drives collection (BA §3.7): "
          f"{n_bad} post-reduction billing months with AmountDue != EffectiveMonthlyRent "
          f"[{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R9 — TCS redemption uses reduced effective rent
# ---------------------------------------------------------------------------

def r9_tcs_follows_effective_rent(db, run_id: int) -> bool:
    """BA §3.7: TCS redemption covers the current rent owed. A REDEMPTION
    ledger row satisfies one month's rent, so its magnitude must equal the
    MonthlyPaymentStatus.AmountDue for that lease/month — which is the reduced
    effective rent for a reduced lease. Checks reduced leases only."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.Lease l
          ON l.RunID = tcl.RunID AND l.LeaseID = tcl.RelatedLeaseID
        JOIN simulation.MonthlyPaymentStatus mps
          ON mps.RunID = tcl.RunID AND mps.LeaseID = tcl.RelatedLeaseID
         AND mps.BillingMonth = DATEFROMPARTS(YEAR(tcl.TransactionDate), MONTH(tcl.TransactionDate), 1)
        WHERE tcl.RunID = ?
          AND tcl.TransactionType = 'REDEMPTION'
          AND l.CumulativeRentReductionPct > 0
          -- redemption must never EXCEED the reduced effective rent (using the
          -- higher original rent would be the bug). It may legitimately be LESS
          -- when balance-capped (balance < 1 month) or an FME partial, so flag
          -- only amounts ABOVE AmountDue, not any inequality.
          AND ABS(tcl.Amount) > mps.AmountDue
        """,
        (run_id,),
    )
    n_bad = rows[0][0] if rows else 0
    n_red = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.Lease l
          ON l.RunID = tcl.RunID AND l.LeaseID = tcl.RelatedLeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'REDEMPTION'
          AND l.CumulativeRentReductionPct > 0
        """,
        (run_id,),
    )
    n_redemptions = n_red[0][0] if n_red else 0
    if n_redemptions == 0:
        print(f"  R9 TCS follows effective rent: no redemptions on reduced leases [{SKIP}]")
        return True
    ok = n_bad == 0
    print(f"  R9 TCS follows effective rent (BA §3.7): {n_redemptions} redemptions on "
          f"reduced leases, {n_bad} exceeding the reduced effective rent [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R10 — Idempotency
# ---------------------------------------------------------------------------

def r10_idempotency(db, run_id: int) -> bool:
    """BA §3.2: each month the manager compares expected vs stored pct and only
    advances when expected is greater, so a tier is never applied twice. No
    (LeaseID, TierMonths) pair may have more than one RENT_REDUCTION event."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) FROM (
            SELECT EntityID,
                   JSON_VALUE(Metadata, '$.TierMonths') AS TierMonths,
                   COUNT(*) AS Cnt
            FROM simulation.Event
            WHERE RunID = ? AND ActionType = 'RENT_REDUCTION'
            GROUP BY EntityID,
                     JSON_VALUE(Metadata, '$.TierMonths')
            HAVING COUNT(*) > 1
        ) dupes
        """,
        (run_id,),
    )
    n_dupes = rows[0][0] if rows else 0
    total = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Event WHERE RunID = ? AND ActionType = 'RENT_REDUCTION'",
        (run_id,),
    )
    n_events = total[0][0] if total else 0
    if n_events == 0:
        print(f"  R10 Idempotency: no rent-reduction events (run too short) [{SKIP}]")
        return True
    ok = n_dupes == 0
    print(f"  R10 Idempotency (BA §3.2): {n_events} reduction events, "
          f"{n_dupes} duplicated (LeaseID, TierMonths) pairs [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R11 — Effective-rent consistency
# ---------------------------------------------------------------------------

def r11_effective_rent_consistency(db, run_id: int) -> bool:
    """BA plan review #5: EffectiveMonthlyRent must always be derivable as
    ROUND(MonthlyRent * (1 - CumulativeRentReductionPct), 2). Checked for ALL
    leases — proves EffectiveMonthlyRent never became an independent source of
    truth."""
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Lease
        WHERE RunID = ?
          AND EffectiveMonthlyRent
              <> ROUND(MonthlyRent * (1 - CumulativeRentReductionPct), 2)
        """,
        (run_id,),
    )
    n_bad = rows[0][0] if rows else 0
    total = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease WHERE RunID = ?", (run_id,))
    n_total = total[0][0] if total else 0
    ok = n_bad == 0
    print(f"  R11 Effective-rent consistency (BA review #5): {n_total} leases, "
          f"{n_bad} where EffectiveMonthlyRent != formula [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R12 — Event/state reconciliation
# ---------------------------------------------------------------------------

def r12_event_state_reconciliation(db, run_id: int) -> bool:
    """BA plan review #4: simulation.Event is the rent-reduction audit trail
    (no ledger table). For every lease with CumulativeRentReductionPct > 0:
      - at least one RENT_REDUCTION event exists, AND
      - MAX(event NewCumulativeRentReductionPct) = Lease.CumulativeRentReductionPct.
    """
    n_reduced = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease WHERE RunID = ? AND CumulativeRentReductionPct > 0",
        (run_id,),
    )
    if not n_reduced or n_reduced[0][0] == 0:
        print(f"  R12 Event/state reconciliation: no reduced leases [{SKIP}]")
        return True

    # Reduced leases with no RENT_REDUCTION event at all.
    missing = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Lease l
        WHERE l.RunID = ? AND l.CumulativeRentReductionPct > 0
          AND NOT EXISTS (
              SELECT 1 FROM simulation.Event e
              WHERE e.RunID = l.RunID
                AND e.ActionType = 'RENT_REDUCTION'
                AND e.EntityID = l.LeaseID
          )
        """,
        (run_id,),
    )
    n_missing = missing[0][0] if missing else 0

    # Reduced leases where the max event pct does not reconcile to lease pct.
    mismatch = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Lease l
        JOIN (
            SELECT EntityID AS LeaseID,
                   MAX(CAST(JSON_VALUE(Metadata,
                       '$.NewCumulativeRentReductionPct') AS DECIMAL(5,4))) AS MaxNewPct
            FROM simulation.Event
            WHERE RunID = ? AND ActionType = 'RENT_REDUCTION'
            GROUP BY EntityID
        ) ev ON ev.LeaseID = l.LeaseID
        WHERE l.RunID = ? AND l.CumulativeRentReductionPct > 0
          AND ev.MaxNewPct <> l.CumulativeRentReductionPct
        """,
        (run_id, run_id),
    )
    n_mismatch = mismatch[0][0] if mismatch else 0

    ok = n_missing == 0 and n_mismatch == 0
    print(f"  R12 Event/state reconciliation (BA review #4): "
          f"{n_missing} reduced leases with no event, "
          f"{n_mismatch} with max-event-pct != lease pct [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# E1 — Config-loader echo validation
# ---------------------------------------------------------------------------

def e1_config_loader_echo(db, run_id: int) -> bool:
    """BA plan review #6: adding the 5th RR_* tier pair to the SELECT shifted
    positional row indices. Echo-validate that unrelated ProjectionConfig
    fields still load correctly + the rent-reduction tiers match the canonical
    schedule. Sampled across the released baseline (206) + a stress projection
    (220) — repointed from the retired grid's 13/91 when that scenario grid was
    purged at V00062 (release hygiene)."""
    try:
        from configuration_loader import ConfigurationLoader
        cl = ConfigurationLoader(db)
        problems = []
        for pid in (206, 220):
            c = cl.load_projection(pid)
            if c is None:
                problems.append(f"projection {pid} failed to load")
                continue
            if c.starting_funds is None or Decimal(str(c.starting_funds)) <= 0:
                problems.append(f"p{pid} starting_funds={c.starting_funds}")
            if c.csf_reserve_months_peak is None or c.csf_reserve_months_floor is None \
                    or c.csf_reserve_n0_properties is None or c.csf_reserve_months_peak <= 0:
                problems.append(f"p{pid} #97 reserve-curve params missing")
            if c.csf_topup_fraction_per_month is None or c.cash_floor_months is None:
                problems.append(f"p{pid} CSF/cash-floor params missing")
            if c.tcs_redemption_prob_default is None or c.credit_cap is None:
                problems.append(f"p{pid} TCS params missing")
            if c.maint_crossover_properties is None or c.units_per_admin_early is None:
                problems.append(f"p{pid} staffing params missing")
            if c.property_tax_per_unit is None or c.routine_cost_mean is None:
                problems.append(f"p{pid} #100 OpEx/MAINT params missing")
            if c.raise_pct_min is None or c.raise_pct_12mo is None:
                problems.append(f"p{pid} raise params missing")
            tiers = [(t.months, t.pct) for t in c.rent_reduction_tiers]
            if tiers != CANONICAL_TIERS:
                problems.append(f"p{pid} tiers={tiers} != {CANONICAL_TIERS}")
        ok = len(problems) == 0
        print(f"  E1 Config-loader echo (BA review #6): "
              f"{len(problems)} field problems [{PASS if ok else FAIL}]")
        for p in problems[:8]:
            print(f"      {p}")
        return ok
    except Exception as e:
        print(f"  E1 Config-loader echo: raised {e!r} [{FAIL}]")
        return False


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

    print(f'Phase 3.9.3 Rent Reduction Schedule Wiring regression — {args.env} (RunID={run_id})')
    print('=' * 78)

    results = [
        r1_threshold_timing(db, run_id),
        r2_cumulative_not_compounded(db, run_id),
        r3_renewal_continuity(db, run_id),
        r4_turnover_reset(db, run_id),
        r5_unit_move_reset(db, run_id),
        r6_all_null_schedule_noop(db, run_id),
        r7_malformed_tier_safety(db, run_id),
        r8_effective_rent_drives_collection(db, run_id),
        r9_tcs_follows_effective_rent(db, run_id),
        r10_idempotency(db, run_id),
        r11_effective_rent_consistency(db, run_id),
        r12_event_state_reconciliation(db, run_id),
        e1_config_loader_echo(db, run_id),
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
