"""
KD-040 + KD-012 (#162 + #44) — one-rent-basis regression gates.

The fix: the bath-adjustment formula's unguarded `baths > 1` branch is
corrected (fractional baths price the fraction at 5%, not 7.5%; V00066
regenerates reference.Units.AdjustedRent), and charging moves onto the
underwritten AdjustedRent (carried into simulation.PropertyUnits at
acquisition; retention's market anchor moves in the same motion).

T1  Formula parity + bucket enumeration — stored reference.Units.AdjustedRent
    == the corrected generator formula for ALL units, to the cent; every
    distinct Baths value maps to its intended multiplier (0.5 -> x0.95,
    1.0 -> base, whole n -> 1 + 0.075(n-1), fractional -> 7.5% whole / 5%
    fraction).
T2  Change-set direction — units with frac(baths)>0 AND baths>1 (the KD-040
    set, 768 in the frozen corpus): corrected value strictly BELOW the buggy
    formula's value; everything else identical under both formulas (the
    bug was overpricing-only).
T3  Carry fidelity — every simulation.PropertyUnits row matches its
    reference.Units row (join via simulation.Properties.AddressID — the
    reference PropertyID — + unit number): AdjustedRent AND BaseRent equal.
T4  Charge basis (independent re-derivation, not the production helper) —
    every lease's MonthlyRent == round(PropertyUnits.AdjustedRent x
    (1 - PROP.BelowMarketRentPct) x cumulative RentRate factor at signing).
    1-bath units double as the no-op proof (AdjustedRent == BaseRent there).
T5  Deal invariance (the half-moved-basis detector) — for multi-bath leases,
    retention_model.breakdown(market_anchor_rent=AdjustedRent) yields
    effective_discount == the below-market pct (cent-rounding tolerance);
    anchoring the same lease on BaseRent yields a SMALLER discount (what a
    half-moved basis would have silently done to retention).
T6  NULL fail-loud — pricing a synthetic unit with NULL AdjustedRent raises
    RuntimeError (no BaseRent fallback); synthetic row deleted in finally.
T7  Sitting-tenant invariance — for every lease, EffectiveMonthlyRent <=
    MonthlyRent (never raised, invariant #1) and EffectiveMonthlyRent ==
    MonthlyRent x (1 - CumulativeRentReductionPct) within a cent (rent moves
    only via the RR schedule — a basis leak into a sitting lease breaks it).

T3, T4, T5, T7 read the suite's populated baseline run.
"""

import argparse
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_CENT = Decimal("0.01")
SYNTH_UNIT_ID = 990001


def _corrected(base_rent: Decimal, baths: Decimal) -> Decimal:
    """The corrected generator formula (mirrors pass4_load_properties.py)."""
    if baths == 1:
        return base_rent
    elif baths > 1 and baths == int(baths):
        return base_rent + ((baths - 1) * base_rent * Decimal("0.075"))
    elif baths == Decimal("0.5"):
        return base_rent - (base_rent * Decimal("0.05"))
    else:
        floor_baths = int(baths)
        fractional = baths - floor_baths
        return base_rent + (floor_baths - 1) * base_rent * Decimal("0.075") \
            + fractional * base_rent * Decimal("0.05")


def _buggy(base_rent: Decimal, baths: Decimal) -> Decimal:
    """The pre-fix formula (unguarded `baths > 1`) — the KD-040 bug."""
    if baths == 1:
        return base_rent
    elif baths > 1:
        return base_rent + ((baths - 1) * base_rent * Decimal("0.075"))
    elif baths == Decimal("0.5"):
        return base_rent - (base_rent * Decimal("0.05"))
    else:
        floor_baths = int(baths)
        fractional = baths - floor_baths
        return base_rent + (floor_baths - 1) * base_rent * Decimal("0.075") \
            + fractional * base_rent * Decimal("0.05")


def _q(x: Decimal) -> Decimal:
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def _ref_units(db):
    return db.execute_query(
        "SELECT UnitID, Baths, BaseRent, AdjustedRent FROM reference.Units "
        "WHERE BaseRent IS NOT NULL AND Baths IS NOT NULL")


def t1_parity_and_buckets(db) -> bool:
    rows = _ref_units(db)
    mismatches = []
    buckets = {}
    for unit_id, baths, base, stored in rows:
        baths, base = Decimal(str(baths)), Decimal(str(base))
        expected = _q(_corrected(base, baths))
        if Decimal(str(stored)) != expected:
            mismatches.append((unit_id, str(stored), str(expected)))
        # bucket check: stored/base multiplier per distinct Baths value
        mult = (Decimal(str(stored)) / base).quantize(Decimal("0.0001"))
        buckets.setdefault(str(baths), set()).add(mult)
    # intended multiplier per bucket (cent-rounding makes per-unit multipliers
    # wobble in the 4th decimal, so compare against the formula's own ratio range)
    bucket_bad = []
    for b, mults in sorted(buckets.items()):
        baths = Decimal(b)
        intended = (_corrected(Decimal("1000"), baths) / Decimal("1000")).quantize(Decimal("0.0001"))
        if any(abs(m - intended) > Decimal("0.0002") for m in mults):
            bucket_bad.append((b, str(intended), sorted(str(m) for m in mults)))
    ok = not mismatches and not bucket_bad
    print(f"  T1 parity: {len(rows)} units, {len(mismatches)} formula mismatches; "
          f"{len(buckets)} bath buckets, {len(bucket_bad)} off intended multiplier "
          f"[{PASS if ok else FAIL}]")
    for u, got, exp in mismatches[:5]:
        print(f"      UnitID={u}: stored={got}, corrected-formula={exp}")
    for b, intended, mults in bucket_bad[:5]:
        print(f"      Baths={b}: intended x{intended}, saw {mults}")
    return ok


def t2_changeset_direction(db) -> bool:
    rows = _ref_units(db)
    changed = same = wrong_dir = wrong_same = 0
    for unit_id, baths, base, stored in rows:
        baths, base, stored = Decimal(str(baths)), Decimal(str(base)), Decimal(str(stored))
        old, new = _q(_buggy(base, baths)), _q(_corrected(base, baths))
        is_kd040 = baths > 1 and baths != int(baths)
        if is_kd040:
            changed += 1
            if not (new < old and stored == new):
                wrong_dir += 1
        else:
            same += 1
            if old != new or stored != new:
                wrong_same += 1
    ok = wrong_dir == 0 and wrong_same == 0 and changed == 768
    print(f"  T2 change-set: {changed} fractional>1 units (expect 768), "
          f"{wrong_dir} not strictly-down; {same} others, {wrong_same} moved "
          f"[{PASS if ok else FAIL}]")
    return ok


def t3_carry_fidelity(db, run_id) -> bool:
    total = db.execute_query(
        "SELECT COUNT(*) FROM simulation.PropertyUnits WHERE RunID = ?", (run_id,))[0][0]
    if total == 0:
        print(f"  T3 carry fidelity: no units in run — cannot gate [{FAIL}]")
        return False
    # join back through AddressID (the reference PropertyID — sim PropertyID is
    # run-local; the KD-042 T7 lesson) + unit number within the property
    bad = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.PropertyUnits pu
        JOIN simulation.Properties sp
          ON sp.RunID = pu.RunID AND sp.PropertyID = pu.PropertyID
        LEFT JOIN reference.Units ru
          ON ru.PropertyID = sp.AddressID
         AND CAST(ru.UnitNumber AS VARCHAR(20)) = pu.Unit
        WHERE pu.RunID = ?
          AND (ru.UnitID IS NULL
               OR ru.AdjustedRent <> pu.AdjustedRent
               OR ru.BaseRent <> pu.BaseRent
               OR pu.AdjustedRent IS NULL)
        """,
        (run_id,))[0][0]
    ok = bad == 0
    print(f"  T3 carry fidelity: {total} carried units, {bad} not matching their "
          f"reference row (AdjustedRent+BaseRent) [{PASS if ok else FAIL}]")
    return ok


def _below_market_pct(db, run_id) -> Decimal:
    row = db.execute_query(
        "SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,))
    from parameter_registry import ParameterRegistry
    return ParameterRegistry(db).load(int(row[0][0])).get_decimal("PROP", "BelowMarketRentPct")


def _inflation_factor(db, run_id, target_date) -> Decimal:
    rows = db.execute_query(
        "SELECT RentRate FROM simulation.InflationSchedule "
        "WHERE RunID = ? AND InflationDate <= ? ORDER BY InflationDate",
        (run_id, target_date))
    f = Decimal("1.0")
    for (r,) in rows:
        f *= (Decimal("1.0") + Decimal(str(r)))
    return f


def t4_charge_basis(db, run_id) -> bool:
    pct = _below_market_pct(db, run_id)
    leases = db.execute_query(
        """
        SELECT l.LeaseID, l.LeaseSignedDate, l.MonthlyRent,
               pu.AdjustedRent, pu.BaseRent, pu.Baths
        FROM simulation.Lease l
        JOIN simulation.PropertyUnits pu
          ON pu.RunID = l.RunID AND pu.UnitID = l.UnitID
        WHERE l.RunID = ?
        """,
        (run_id,))
    if not leases:
        print(f"  T4 charge basis: no leases in run — cannot gate [{FAIL}]")
        return False
    factors = {}
    bad = 0
    onebath_noop = 0
    for lease_id, signed, rent, adj, base, baths in leases:
        if signed not in factors:
            factors[signed] = _inflation_factor(db, run_id, signed)
        expected = _q(Decimal(str(adj)) * (Decimal("1") - pct) * factors[signed])
        if expected != Decimal(str(rent)):
            bad += 1
        if Decimal(str(baths)) == 1 and Decimal(str(adj)) == Decimal(str(base)):
            onebath_noop += 1
    ok = bad == 0
    print(f"  T4 charge basis (independent re-derivation): {len(leases)} leases, "
          f"{bad} off AdjustedRent x (1-{pct}) x inflation; "
          f"{onebath_noop} 1-bath leases double as the no-op proof [{PASS if ok else FAIL}]")
    return ok


def t5_deal_invariance(db, run_id) -> bool:
    from retention_model import RetentionModel, _DateContext
    pct = _below_market_pct(db, run_id)
    # fresh multi-bath leases (no reduction yet) — the exact KD-012 catch case
    leases = db.execute_query(
        """
        SELECT l.LeaseID, l.LeaseSignedDate, l.MonthlyRent,
               pu.AdjustedRent, pu.BaseRent
        FROM simulation.Lease l
        JOIN simulation.PropertyUnits pu
          ON pu.RunID = l.RunID AND pu.UnitID = l.UnitID
        WHERE l.RunID = ? AND l.CumulativeRentReductionPct = 0
          AND pu.AdjustedRent <> pu.BaseRent
        ORDER BY l.LeaseID
        OFFSET 0 ROWS FETCH FIRST 50 ROWS ONLY
        """,
        (run_id,))
    if not leases:
        print(f"  T5 deal invariance: no fresh multi-bath leases in run — cannot gate [{FAIL}]")
        return False
    m = RetentionModel(None, base_exit=0.20, beta=1.0, gamma=0.5, floor_exit=0.05,
                       vac_ref=0.07, burden_ceiling=0.50, burden_floor=0.30, regional_vacancy_rate=0.034)
    bad_adj = bad_base = 0
    for lease_id, signed, rent, adj, base in leases:
        f = float(_inflation_factor(db, run_id, signed))
        ctx = _DateContext(rent_factor_now=f, wage_factor_now=1.0,
                           wage_months=[], wage_prefix=[], regional_vacancy=0.07)
        disc_adj = m.breakdown(
            ctx, effective_rent=float(rent), market_anchor_rent=float(adj),
            signing_income=10000.0, income_reference_date=signed)["effective_discount"]
        if abs(disc_adj - float(pct)) > 1e-4:
            bad_adj += 1
        # the half-moved-basis counterfactual: BaseRent anchor understates the deal
        disc_base = m.breakdown(
            ctx, effective_rent=float(rent), market_anchor_rent=float(base),
            signing_income=10000.0, income_reference_date=signed)["effective_discount"]
        if not disc_base < float(pct) - 1e-6:
            bad_base += 1
    ok = bad_adj == 0 and bad_base == 0
    print(f"  T5 deal invariance: {len(leases)} fresh multi-bath leases, "
          f"{bad_adj} off effective_discount=={pct} on the AdjustedRent anchor, "
          f"{bad_base} where a BaseRent anchor would NOT have understated the deal "
          f"[{PASS if ok else FAIL}]")
    return ok


def t6_null_fail_loud(db, run_id) -> bool:
    from tenant_manager import TenantManager
    from event_logger import EventLogger
    pct = _below_market_pct(db, run_id)
    el = EventLogger(db)
    el.set_run_id(run_id)
    tm = TenantManager(db_manager=db, event_logger=el, run_id=run_id,
                       run_seed=99, below_market_rent_pct=float(pct))
    host = db.execute_query(
        "SELECT PropertyID, EventID FROM simulation.PropertyUnits "
        "WHERE RunID = ? ORDER BY UnitID OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY",
        (run_id,))
    if not host:
        print(f"  T6 NULL fail-loud: no host row to borrow FKs from [{FAIL}]")
        return False
    pid, eid = host[0]
    ok = False
    try:
        db.execute_non_query(
            "INSERT INTO simulation.PropertyUnits "
            "(RunID, UnitID, PropertyID, EventID, Unit, Beds, Baths, BaseRent, "
            " AdjustedRent, IsOccupied, UnitStatus) "
            "VALUES (?, ?, ?, ?, 'X', 1, 1, 1000.00, NULL, 0, 'Available')",
            (run_id, SYNTH_UNIT_ID, pid, eid))
        try:
            tm._compute_inflation_adjusted_rent(SYNTH_UNIT_ID, __import__("datetime").date(2026, 1, 1))
            print(f"  T6 NULL fail-loud: pricing a NULL-AdjustedRent unit did NOT raise [{FAIL}]")
        except RuntimeError as e:
            ok = "AdjustedRent" in str(e)
            print(f"  T6 NULL fail-loud: RuntimeError raised, mentions AdjustedRent={ok} "
                  f"[{PASS if ok else FAIL}]")
    finally:
        db.execute_non_query(
            "DELETE FROM simulation.PropertyUnits WHERE RunID = ? AND UnitID = ?",
            (run_id, SYNTH_UNIT_ID))
    return ok


def t7_sitting_tenant_invariance(db, run_id) -> bool:
    raised = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease "
        "WHERE RunID = ? AND EffectiveMonthlyRent > MonthlyRent", (run_id,))[0][0]
    off_schedule = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.Lease
        WHERE RunID = ?
          AND ABS(EffectiveMonthlyRent
                  - MonthlyRent * (1 - CumulativeRentReductionPct)) > 0.01
        """,
        (run_id,))[0][0]
    total = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease WHERE RunID = ?", (run_id,))[0][0]
    ok = raised == 0 and off_schedule == 0 and total > 0
    print(f"  T7 sitting-tenant invariance: {total} leases, {raised} with effective "
          f"rent ABOVE signed rent, {off_schedule} off the RR-schedule identity "
          f"[{PASS if ok else FAIL}]")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--assert", dest="assert_mode", action="store_true")
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(ENV_BASE + args.env + os.sep + "db_config.json")
    if args.run_id is not None:
        run_id = int(args.run_id)
    else:
        row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
        if not row or row[0][0] is None:
            raise SystemExit("No runs found in simulation.Run.")
        run_id = int(row[0][0])

    print(f"KD-040/KD-012 one-rent-basis gates — {args.env} (RunID={run_id})")
    print("=" * 78)
    results = [
        t1_parity_and_buckets(db),
        t2_changeset_direction(db),
        t3_carry_fidelity(db, run_id),
        t4_charge_basis(db, run_id),
        t5_deal_invariance(db, run_id),
        t6_null_fail_loud(db, run_id),
        t7_sitting_tenant_invariance(db, run_id),
    ]
    print("=" * 78)
    passed = sum(1 for r in results if r)
    print(f"Result: {passed}/{len(results)} gates passed")
    if args.assert_mode and passed != len(results):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
