"""
Phase 1.3 (#48) — inflation regime regression tests (implementation gates 1-3).

Gate 1 (max(0) clamp removal): a Downturn-Financial window must produce a
cumulative property factor < 1.0; a Downturn-Shock window must dip the rent
factor below its prior peak. Pre-#48, every monthly rate was floored at 0 —
downturns were structurally inert.
Gate 2 (invariant #1): a sitting tenant's rent is frozen at signing — the
schedule reprices at TURNOVER only. Every lease's MonthlyRent must equal the
signing-date recompute regardless of what regimes did afterward.
Gate 3 (invariant #8): same seed => identical schedule.

T1-T5 drive the REAL generator core (InflationEngine._generate_monthly_rates)
read-only against the env DB's registry + projection config — no run rows,
no writes. Forced-archetype windows are injected via a registry shim (the
same mechanism per-projection INF.ForcedRegime rows use in production).
T6 checks the latest completed run in the DB.
"""

import argparse
import os
import random
import sys
from decimal import Decimal

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

PROJ = 206  # $8.0M baseline template


class _RegShim:
    """Delegates to the real registry; overlays test-local overrides — the
    same shape a per-projection INF.ForcedRegime override produces."""

    def __init__(self, reg, overrides: dict):
        self._reg = reg
        self._ov = overrides  # ('INF','Name') -> value

    def has(self, cat, name):
        return (cat, name) in self._ov or self._reg.has(cat, name)

    def _get(self, cat, name):
        if (cat, name) in self._ov:
            return self._ov[(cat, name)]
        return self._reg.get(cat, name)

    def get_str(self, cat, name):
        return str(self._get(cat, name))

    def get_int(self, cat, name):
        return int(self._get(cat, name))

    def get_float(self, cat, name):
        return float(self._get(cat, name))


def _load(env):
    from database_manager import DatabaseManager
    from configuration_loader import ConfigurationLoader
    from parameter_registry import ParameterRegistry
    from inflation_engine import InflationEngine

    db = DatabaseManager(ENV_BASE + env + os.sep + "db_config.json")
    cl = ConfigurationLoader(db)
    projection = cl.load_projection(PROJ)
    reg = ParameterRegistry(db).load(PROJ)
    eng = InflationEngine(db, cl)
    return db, projection, reg, eng


def _cum_factor(rates, attr, lo=None, hi=None):
    f = Decimal("1")
    for r in rates:
        if lo is not None and r.month_index < lo:
            continue
        if hi is not None and r.month_index > hi:
            continue
        f *= (Decimal("1") + getattr(r, attr))
    return f


def t1_seed_reproducibility(projection, reg, eng) -> bool:
    """Gate 3: same seed => byte-identical schedule; different seed differs."""
    a = eng._generate_monthly_rates(projection, 240, "Regime", reg, random.Random(777 + 30930))
    b = eng._generate_monthly_rates(projection, 240, "Regime", reg, random.Random(777 + 30930))
    c = eng._generate_monthly_rates(projection, 240, "Regime", reg, random.Random(778 + 30930))
    same = all(
        (x.rent_rate, x.opex_rate, x.property_rate, x.scenario_phase, x.vacancy_delta)
        == (y.rent_rate, y.opex_rate, y.property_rate, y.scenario_phase, y.vacancy_delta)
        for x, y in zip(a, b)
    ) and len(a) == len(b)
    diff = any(x.rent_rate != y.rent_rate for x, y in zip(a, c))
    ok = same and diff
    print(f"  T1 seed-repro: same-seed identical={same}, cross-seed differs={diff} [{PASS if ok else FAIL}]")
    return ok


def t2_downturn_financial_property_falls(projection, reg, eng) -> bool:
    """Gate 1a: forced DF window (months 13-72, ~5yr) => property factor < 1.0
    over the window. Pre-#48 the max(0) clamp made this structurally impossible."""
    shim = _RegShim(reg, {
        ('INF', 'ForcedRegime'): 'DownturnFinancial',
        ('INF', 'ForcedRegimeStartMonth'): 13,
        ('INF', 'ForcedRegimeDwellMonths'): 60,
    })
    rates = eng._generate_monthly_rates(projection, 120, "Regime", shim, random.Random(4242 + 30930))
    window = _cum_factor(rates, "property_rate", lo=13, hi=72)
    any_negative = any(r.property_rate < 0 for r in rates if 13 <= r.month_index <= 72)
    regimes_ok = all(r.scenario_phase == 'DownturnFinancial' for r in rates if 13 <= r.month_index <= 72)
    ok = window < Decimal("1") and any_negative and regimes_ok
    print(f"  T2 DF window: property factor {window:.4f} < 1.0={window < 1}, "
          f"negative months present={any_negative}, window scripted={regimes_ok} [{PASS if ok else FAIL}]")
    return ok


def t3_shock_rent_dips(projection, reg, eng) -> bool:
    """Gate 1b: forced Shock window (months 13-24) => the cumulative rent
    factor at window end sits below its pre-window peak (the dip; the
    overshoot is the Shock->Surge transition in stochastic runs).

    CALIBRATION-PINNED (Keight #4): with the V00050 values (Shock rent mean
    -5%/yr, vol 3%/yr) and this fixed seed, the dip is deterministic — the
    assertion cannot flake in CI. But the margin is within ~1 sigma of the
    noise, so RECALIBRATING DownturnShock_RentMean/Vol may flip it: that is
    an intentional tripwire — a recalibration must revisit this assertion
    (and the dwell/window) deliberately, not silently."""
    shim = _RegShim(reg, {
        ('INF', 'ForcedRegime'): 'DownturnShock',
        ('INF', 'ForcedRegimeStartMonth'): 13,
        ('INF', 'ForcedRegimeDwellMonths'): 12,
    })
    rates = eng._generate_monthly_rates(projection, 36, "Regime", shim, random.Random(4242 + 30930))
    peak = _cum_factor(rates, "rent_rate", hi=12)
    at_end = _cum_factor(rates, "rent_rate", hi=24)
    vac = [r.vacancy_delta for r in rates if 13 <= r.month_index <= 24]
    ok = at_end < peak and all(v > 0 for v in vac)
    print(f"  T3 Shock window: rent factor {peak:.4f} -> {at_end:.4f} (dip={at_end < peak}), "
          f"vacancy delta active={all(v > 0 for v in vac)} [{PASS if ok else FAIL}]")
    return ok


def t4_transition_matrix_valid(reg) -> bool:
    """Live-registry data test: 5x5 matrix, rows sum to 1, probs in [0,1]."""
    from inflation_engine import REGIMES
    ok = True
    for src in REGIMES:
        row = [reg.get_float('INF', f'Trans_{src}_{dst}') for dst in REGIMES]
        if abs(sum(row) - 1.0) > 1e-6 or any(p < 0 or p > 1 for p in row):
            ok = False
            print(f"  T4 BAD ROW {src}: {row} (sum {sum(row)})")
    print(f"  T4 transition matrix: rows sum to 1, probs in [0,1] [{PASS if ok else FAIL}]")
    return ok


def t5_static_mode_flat(projection, reg, eng) -> bool:
    """Static mode = flat annual/12, zero vacancy delta, no RNG dependence."""
    rates = eng._generate_monthly_rates(projection, 24, "Static", reg, random.Random(1 + 30930))
    exp_rent = Decimal(str(round(float(projection.rent_inflation_rate) / 12, 6)))
    ok = (all(r.rent_rate == exp_rent for r in rates)
          and all(r.vacancy_delta == 0 for r in rates)
          and len({r.opex_rate for r in rates}) == 1)
    print(f"  T5 Static flat: rent {exp_rent}/mo constant, deltas 0 [{PASS if ok else FAIL}]")
    return ok


def t6_lease_rent_frozen_at_signing(env) -> bool:
    """Gate 2 (invariant #1): every lease's MonthlyRent equals the PRODUCTION
    signing-date recompute (tenant_manager._compute_inflation_adjusted_rent at
    LeaseSignedDate) — rent is frozen at signing; later regimes never touch a
    sitting tenant (repricing at turnover only). Mirrors Phase3_9_4 B1's
    production-helper pattern; pinned here for the #48 gate's traceability
    under a REGIME schedule (B1 predates regimes)."""
    from decimal import ROUND_HALF_UP
    from database_manager import DatabaseManager
    from parameter_registry import ParameterRegistry
    from tenant_manager import TenantManager
    from event_logger import EventLogger

    db = DatabaseManager(ENV_BASE + env + os.sep + "db_config.json")
    run_row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
    run_id = run_row[0][0] if run_row and run_row[0][0] else None
    if not run_id:
        print(f"  T6 no runs in DB [{SKIP}]")
        return True
    ctx = db.execute_query(
        "SELECT ProjectionID, RandomSeed FROM simulation.Run WHERE RunID = ?", (run_id,))
    proj_id, run_seed = int(ctx[0][0]), int(ctx[0][1] or 0)
    bmp = ParameterRegistry(db).load(proj_id).get_decimal('PROP', 'BelowMarketRentPct')

    el = EventLogger(db)
    el.set_run_id(run_id)
    tm = TenantManager(db_manager=db, event_logger=el, run_id=run_id,
                       run_seed=run_seed, below_market_rent_pct=float(bmp))

    leases = db.execute_query(
        "SELECT LeaseID, UnitID, LeaseSignedDate, MonthlyRent FROM simulation.Lease WHERE RunID = ?",
        (run_id,),
    )
    if not leases:
        print(f"  T6 no leases in RunID={run_id} [{SKIP}]")
        return True

    cent = Decimal("0.01")
    mismatches = 0
    for lease_id, unit_id, signed, monthly_rent in leases:
        expected = tm._compute_inflation_adjusted_rent(unit_id, signed).quantize(
            cent, rounding=ROUND_HALF_UP)
        if expected != Decimal(str(monthly_rent)):
            mismatches += 1
            if mismatches <= 3:
                print(f"  T6 MISMATCH Lease {lease_id}: rent {monthly_rent} != signing-date recompute {expected}")
    ok = mismatches == 0
    print(f"  T6 invariant-#1 rent frozen at signing (production recompute): "
          f"{len(leases)} leases, {mismatches} mismatches [{PASS if ok else FAIL}]")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, help="Test database name")
    parser.add_argument("--assert", dest="assert_mode", action="store_true")
    args = parser.parse_args()

    print(f"Phase 1.3 (#48) Inflation Regimes — gate regressions against {args.env}")
    print("=" * 70)

    db, projection, reg, eng = _load(args.env)
    results = [
        t1_seed_reproducibility(projection, reg, eng),
        t2_downturn_financial_property_falls(projection, reg, eng),
        t3_shock_rent_dips(projection, reg, eng),
        t4_transition_matrix_valid(reg),
        t5_static_mode_flat(projection, reg, eng),
        t6_lease_rent_frozen_at_signing(args.env),
    ]

    print("=" * 70)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Result: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
