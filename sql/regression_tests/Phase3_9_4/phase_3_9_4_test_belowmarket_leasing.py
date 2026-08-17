"""
Phase 3.9.4 — Restore Below-Market Leasing regression tests.

Outcome-level black-box suite verifying that new leases are signed at the
Liberty Bee below-market rate:

    MonthlyRent = AdjustedRent × (1 − PROP_BelowMarketRentPct) × cumulative inflation

(Basis re-pinned BaseRent → AdjustedRent at KD-012/V00066 — the bath-adjusted
market figure; B1–B5 call the production helper so their logic is unchanged.)

  B1  Signed rent equals the discounted-base formula, using the *production*
      inflation/date logic (calls tenant_manager._compute_inflation_adjusted_rent)
  B2  Signed rent is below the market-rate equivalent by exactly the discount
  B3  Leasing keys off the same PROP_BelowMarketRentPct the acquisition
      underwriting uses (leasing/underwriting consistency)
  B4  A new tenant after turnover does NOT inherit the prior tenant's
      tenure-reduced rent (protects the Phase 3.7.8 turnover-hygiene intent)
  B5  A new lease starts clean — EffectiveMonthlyRent = MonthlyRent,
      CumulativeRentReductionPct = 0 at creation
  B6  Informational reconciliation of earliest rent vs discounted AdjustedRent

BA plan review: phase_3_9_4_plan_review_ba_handoff.md (§2.1–2.6).
Tests SKIP gracefully when a run doesn't exercise a pathway.

Usage:
    python sql/regression_tests/Phase3_9_4/phase_3_9_4_test_belowmarket_leasing.py --env <db> [--run-id N] [--assert]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
if os.path.basename(_REPO_ROOT) == 'scratch':
    REPO_ROOT = os.path.dirname(_REPO_ROOT)
else:
    REPO_ROOT = _REPO_ROOT
# Pre-promotion the 3.9.4 code lives only in scratch/ — scratch takes import
# precedence (inserted last so it lands at sys.path[0]).
sys.path.insert(0, os.path.join(REPO_ROOT, 'app', 'src'))
_SCRATCH_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
if os.path.isdir(_SCRATCH_SRC) and _SCRATCH_SRC not in sys.path:
    sys.path.insert(0, _SCRATCH_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep
PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
SKIP = '\033[93mSKIP\033[0m'
_CENT = Decimal('0.01')


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _resolve_run_id(db, override) -> int:
    if override is not None:
        return int(override)
    row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
    if not row or row[0][0] is None:
        raise SystemExit("No runs found in simulation.Run.")
    return int(row[0][0])


def _run_context(db, run_id):
    """Return (projection_id, run_seed, below_market_rent_pct) for the run."""
    row = db.execute_query(
        "SELECT ProjectionID, RandomSeed FROM simulation.Run WHERE RunID = ?",
        (run_id,),
    )
    proj_id = int(row[0][0])
    run_seed = int(row[0][1]) if row[0][1] is not None else 0
    # PROP.BelowMarketRentPct from the registry (canonical store; #75 re-point — was
    # reference.ProjectionParameters, dead on V0.2 + absent for registry-native
    # projections like the #106 stress set).
    from parameter_registry import ParameterRegistry  # type: ignore
    bmp = ParameterRegistry(db).load(proj_id).get_decimal('PROP', 'BelowMarketRentPct')
    return proj_id, run_seed, bmp


def _make_tenant_manager(db, run_id, run_seed, below_market_rent_pct):
    """Construct a TenantManager so B1 can call the production rent helper."""
    from tenant_manager import TenantManager
    from event_logger import EventLogger
    el = EventLogger(db)
    el.set_run_id(run_id)
    return TenantManager(
        db_manager=db, event_logger=el, run_id=run_id, run_seed=run_seed,
        below_market_rent_pct=float(below_market_rent_pct),
    )


# ---------------------------------------------------------------------------
# B1 — signed rent equals the discounted-base formula (production logic)
# ---------------------------------------------------------------------------

def b1_signed_rent_matches_formula(db, run_id, ctx) -> bool:
    """Every signed lease's MonthlyRent equals
    ROUND(AdjustedRent × (1 − below_market_pct) × cumulative_inflation, 2).

    Expected rent is computed by calling the PRODUCTION helper
    tenant_manager._compute_inflation_adjusted_rent(unit_id, LeaseSignedDate) —
    the exact inflation-schedule window + date logic the run used (BA #2.3),
    not an independent re-implementation."""
    _, run_seed, bmp = ctx
    try:
        tm = _make_tenant_manager(db, run_id, run_seed, bmp)
    except Exception as e:
        print(f"  B1 Signed rent matches formula: could not build TenantManager — {e!r} [{FAIL}]")
        return False

    leases = db.execute_query(
        "SELECT LeaseID, UnitID, LeaseSignedDate, MonthlyRent FROM simulation.Lease WHERE RunID = ?",
        (run_id,),
    )
    if not leases:
        print(f"  B1 Signed rent matches formula: no leases in run [{SKIP}]")
        return True
    bad = []
    for lease_id, unit_id, signed, monthly_rent in leases:
        expected = tm._compute_inflation_adjusted_rent(unit_id, signed).quantize(
            _CENT, rounding=ROUND_HALF_UP)
        if expected != Decimal(str(monthly_rent)):
            bad.append((lease_id, str(monthly_rent), str(expected)))
    ok = len(bad) == 0
    print(f"  B1 Signed rent matches discounted formula (BA #2.3, production logic): "
          f"{len(leases)} leases, {len(bad)} mismatches [{PASS if ok else FAIL}]")
    for lid, got, exp in bad[:5]:
        print(f"      LeaseID={lid}: MonthlyRent={got}, expected={exp}")
    return ok


# ---------------------------------------------------------------------------
# B2 — signed rent is below market by exactly the discount
# ---------------------------------------------------------------------------

def b2_below_market_by_discount(db, run_id, ctx) -> bool:
    """For every lease, signed MonthlyRent is below the market-rate equivalent
    (AdjustedRent × cumulative inflation) by exactly PROP_BelowMarketRentPct."""
    _, run_seed, bmp = ctx
    try:
        tm = _make_tenant_manager(db, run_id, run_seed, bmp)
    except Exception as e:
        print(f"  B2 Below market by discount: could not build TenantManager — {e!r} [{FAIL}]")
        return False
    leases = db.execute_query(
        "SELECT LeaseID, UnitID, LeaseSignedDate, MonthlyRent FROM simulation.Lease WHERE RunID = ?",
        (run_id,),
    )
    if not leases:
        print(f"  B2 Below market by discount: no leases [{SKIP}]")
        return True
    one_minus = Decimal('1') - bmp
    bad = []
    for lease_id, unit_id, signed, monthly_rent in leases:
        discounted = tm._compute_inflation_adjusted_rent(unit_id, signed)  # AdjustedRent×(1−pct)×factor
        market = discounted / one_minus                                   # AdjustedRent×factor
        mr = Decimal(str(monthly_rent))
        if mr >= market.quantize(_CENT, rounding=ROUND_HALF_UP):
            bad.append((lease_id, f"rent={mr}", f"market={market.quantize(_CENT)}"))
            continue
        implied_discount = (Decimal('1') - mr / market).quantize(Decimal('0.0001'))
        if implied_discount != bmp.quantize(Decimal('0.0001')):
            bad.append((lease_id, f"implied_discount={implied_discount}", f"expected={bmp}"))
    ok = len(bad) == 0
    print(f"  B2 Signed rent below market by the discount: {len(leases)} leases, "
          f"{len(bad)} off [{PASS if ok else FAIL}]")
    for lid, a, b in bad[:5]:
        print(f"      LeaseID={lid}: {a}, {b}")
    return ok


# ---------------------------------------------------------------------------
# B3 — leasing / underwriting consistency
# ---------------------------------------------------------------------------

def b3_leasing_underwriting_consistent(db, run_id, ctx) -> bool:
    """The discount embedded in signed leases must equal the projection's
    PROP_BelowMarketRentPct — the same parameter the acquisition underwriting
    applies (property_acquisition_manager: annual income × (1 − below_market)).
    Confirms leasing and underwriting key off one source of truth."""
    proj_id, run_seed, bmp = ctx
    try:
        tm = _make_tenant_manager(db, run_id, run_seed, bmp)
    except Exception as e:
        print(f"  B3 Leasing/underwriting consistency: could not build TenantManager — {e!r} [{FAIL}]")
        return False
    # Sample up to 200 leases; derive the discount each was priced with.
    leases = db.execute_query(
        "SELECT LeaseID, UnitID, LeaseSignedDate, MonthlyRent "
        "FROM simulation.Lease WHERE RunID = ? ORDER BY LeaseID "
        "OFFSET 0 ROWS FETCH FIRST 200 ROWS ONLY",
        (run_id,),
    )
    if not leases:
        print(f"  B3 Leasing/underwriting consistency: no leases [{SKIP}]")
        return True
    one_minus = Decimal('1') - bmp
    mismatches = 0
    for lease_id, unit_id, signed, monthly_rent in leases:
        discounted = tm._compute_inflation_adjusted_rent(unit_id, signed)
        market = discounted / one_minus
        implied = (Decimal('1') - Decimal(str(monthly_rent)) / market).quantize(Decimal('0.0001'))
        if implied != bmp.quantize(Decimal('0.0001')):
            mismatches += 1
    ok = mismatches == 0
    print(f"  B3 Leasing/underwriting consistency: discount on {len(leases)} sampled "
          f"leases vs registry PROP.BelowMarketRentPct={bmp}, "
          f"{mismatches} mismatches [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# B4 — turnover does not inherit the prior tenant's reduced rent
# ---------------------------------------------------------------------------

def b4_turnover_no_inherit(db, run_id, ctx) -> bool:
    """A new lease on a unit that previously held a lease must be priced fresh
    from the discounted base — never from the prior tenant's reduced rent
    (Phase 3.7.8 turnover-hygiene intent, BA #2.1).

    Two checks on non-first leases of multi-lease units:
      (a) each still satisfies the B1 fresh-formula price, AND
      (b) none has MonthlyRent equal to an earlier lease's EffectiveMonthlyRent
          where that earlier lease had carried a reduction."""
    _, run_seed, bmp = ctx
    try:
        tm = _make_tenant_manager(db, run_id, run_seed, bmp)
    except Exception as e:
        print(f"  B4 Turnover no-inherit: could not build TenantManager — {e!r} [{FAIL}]")
        return False
    # Non-first leases on units that have held more than one lease.
    later = db.execute_query(
        """
        SELECT l.LeaseID, l.UnitID, l.LeaseSignedDate, l.MonthlyRent
        FROM simulation.Lease l
        WHERE l.RunID = ?
          AND EXISTS (
              SELECT 1 FROM simulation.Lease p
              WHERE p.RunID = l.RunID AND p.UnitID = l.UnitID
                AND p.LeaseStartDate < l.LeaseStartDate)
        """,
        (run_id,),
    )
    if not later:
        print(f"  B4 Turnover no-inherit: no turnover (multi-lease) units in run [{SKIP}]")
        return True
    formula_bad = 0
    for lease_id, unit_id, signed, monthly_rent in later:
        expected = tm._compute_inflation_adjusted_rent(unit_id, signed).quantize(
            _CENT, rounding=ROUND_HALF_UP)
        if expected != Decimal(str(monthly_rent)):
            formula_bad += 1
    # (b) a later lease whose MonthlyRent equals a prior reduced lease's
    #     EffectiveMonthlyRent on the same unit = smoking-gun inheritance.
    inherit = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Lease l
        JOIN simulation.Lease p
          ON p.RunID = l.RunID AND p.UnitID = l.UnitID
         AND p.LeaseStartDate < l.LeaseStartDate
        WHERE l.RunID = ?
          AND p.CumulativeRentReductionPct > 0
          AND l.MonthlyRent = p.EffectiveMonthlyRent
        """,
        (run_id,),
    )
    n_inherit = inherit[0][0] if inherit else 0
    ok = formula_bad == 0 and n_inherit == 0
    print(f"  B4 Turnover no-inherit (BA #2.1): {len(later)} post-turnover leases, "
          f"{formula_bad} off fresh-formula, {n_inherit} inheriting a prior reduced rent "
          f"[{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# B5 — new lease starts clean
# ---------------------------------------------------------------------------

def b5_new_lease_starts_clean(db, run_id, ctx) -> bool:
    """At creation, a lease has EffectiveMonthlyRent = MonthlyRent and
    CumulativeRentReductionPct = 0 (BA #2.2).

    Verified two ways, since later rent reductions mutate current state:
      - leases with NO RENT_REDUCTION event must still show
        EffectiveMonthlyRent = MonthlyRent and pct = 0 (untouched since creation);
      - leases WITH reduction events: the earliest event's before-state
        (PreviousCumulativeRentReductionPct, EffectiveMonthlyRentBefore) must be
        0 and the original MonthlyRent."""
    # Untouched leases.
    untouched_bad = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.Lease l
        WHERE l.RunID = ?
          AND NOT EXISTS (
              SELECT 1 FROM simulation.Event e
              WHERE e.RunID = l.RunID AND e.ActionType = 'RENT_REDUCTION'
                AND e.EntityID = l.LeaseID)
          AND (l.EffectiveMonthlyRent <> l.MonthlyRent
               OR l.CumulativeRentReductionPct <> 0)
        """,
        (run_id,),
    )
    n_untouched_bad = untouched_bad[0][0] if untouched_bad else 0

    # Reduced leases — earliest reduction event must show a clean before-state.
    ev = db.execute_query(
        """
        SELECT e.EntityID, e.EventID,
               e.Metadata AS Meta,
               l.MonthlyRent
        FROM simulation.Event e
        JOIN simulation.Lease l ON l.RunID = e.RunID AND l.LeaseID = e.EntityID
        WHERE e.RunID = ? AND e.ActionType = 'RENT_REDUCTION'
        """,
        (run_id,),
    )
    first_by_lease = {}
    for entity_id, event_id, meta, monthly_rent in ev:
        cur = first_by_lease.get(entity_id)
        if cur is None or event_id < cur[0]:
            first_by_lease[entity_id] = (event_id, meta, monthly_rent)
    reduced_bad = 0
    for entity_id, (event_id, meta, monthly_rent) in first_by_lease.items():
        try:
            m = json.loads(meta)
            prev_pct = Decimal(str(m.get('PreviousCumulativeRentReductionPct')))
            eff_before = Decimal(str(m.get('EffectiveMonthlyRentBefore')))
        except Exception:
            reduced_bad += 1
            continue
        if prev_pct != 0 or eff_before != Decimal(str(monthly_rent)):
            reduced_bad += 1

    ok = n_untouched_bad == 0 and reduced_bad == 0
    print(f"  B5 New lease starts clean (BA #2.2): {n_untouched_bad} untouched leases "
          f"off creation-state, {reduced_bad}/{len(first_by_lease)} reduced leases with "
          f"unclean first-event before-state [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# B6 — informational reconciliation of earliest signed rent vs discounted base
# ---------------------------------------------------------------------------

def b6_currentrent_reconciliation(db, run_id, ctx) -> bool:
    """Informational (BA #2.6, not a gating assertion). PropertyUnits.CurrentRent
    was retired at V00058 (#154/#45 — it was a set-once 0.9xBaseRent snapshot).
    The honest reconciliation: the EARLIEST lease (little inflation) should sign
    at ~ AdjustedRent x (1 - PROP.BelowMarketRentPct) (basis: KD-012/V00066)."""
    early = db.execute_query(
        """
        SELECT l.MonthlyRent, u.AdjustedRent
        FROM simulation.Lease l
        JOIN simulation.PropertyUnits u ON u.RunID = l.RunID AND u.UnitID = l.UnitID
        WHERE l.RunID = ? ORDER BY l.LeaseSignedDate, l.LeaseID
        OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
        """,
        (run_id,),
    )
    # Resolved override-else-default for THIS run's projection (V00071 split the
    # registry into Default/Defined). Resolving rather than reading the default
    # keeps the check correct for a scenario projection, which carries its own
    # below-market override — reading the default alone would compare a
    # deep-discount run against the standard 10% and report a false mismatch.
    pct = db.execute_query(
        "SELECT COALESCE("
        "  (SELECT d.Value FROM reference.ParameterRegistryDefined d"
        "     JOIN simulation.Run r ON r.ProjectionID = d.ProjectionID"
        "    WHERE r.RunID = ? AND d.Category='PROP' AND d.Name='BelowMarketRentPct'),"
        "  (SELECT Value FROM reference.ParameterRegistryDefault"
        "    WHERE Category='PROP' AND Name='BelowMarketRentPct'))",
        (run_id,))
    if early and pct:
        mr, br = Decimal(str(early[0][0])), Decimal(str(early[0][1]))
        discount = Decimal(str(pct[0][0]))
        expected = br * (Decimal('1') - discount)
        ratio = (mr / expected).quantize(Decimal('0.0001')) if expected else Decimal('0')
        print(f"  B6 discounted-base reconciliation (informational): earliest lease "
              f"rent/(AdjustedRent x (1-{discount})) = {ratio} (~1.0 + early inflation) [{PASS}]")
    else:
        print(f"  B6 discounted-base reconciliation: no leases [{SKIP}]")
    return True  # informational — never fails the suite


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--assert', dest='assert_mode', action='store_true')
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    db = DatabaseManager(_config_path_for(args.env))
    run_id = _resolve_run_id(db, args.run_id)
    ctx = _run_context(db, run_id)

    print(f'Phase 3.9.4 Restore Below-Market Leasing regression — {args.env} (RunID={run_id})')
    print(f'  projection={ctx[0]}  below_market_rent_pct={ctx[2]}')
    print('=' * 78)

    results = [
        b1_signed_rent_matches_formula(db, run_id, ctx),
        b2_below_market_by_discount(db, run_id, ctx),
        b3_leasing_underwriting_consistent(db, run_id, ctx),
        b4_turnover_no_inherit(db, run_id, ctx),
        b5_new_lease_starts_clean(db, run_id, ctx),
        b6_currentrent_reconciliation(db, run_id, ctx),
    ]

    print('=' * 78)
    passed = sum(1 for r in results if r)
    print(f'Result: {passed}/{len(results)} tests passed')
    if args.assert_mode and passed != len(results):
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
