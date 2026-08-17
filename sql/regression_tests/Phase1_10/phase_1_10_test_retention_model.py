"""
Phase 1.10 retention model — pure-formula regression (KD-041/#167).

Tests the RetentionModel voluntary-exit formula in isolation (no DB): monotonicity
in the deal and in scarcity, clamp bounds [floor_exit, base_exit], the inverted-
discount (downturn) edge, the affordability response, and the zero-discount /
full-availability base case. The model owns no RNG, so the formula is fully
deterministic and DB-independent — the --env arg is accepted only to fit the suite
harness (run_suite.py invokes every test with `--env <db> --assert`).

Design: docs/v_0_3/phases/phase_1_10/phase_1_10_retention_ba_answers.md
Evidence: docs/reference/evidence_base.md §10.
"""
import argparse
import os
import sys
from datetime import date

# repo/sql/regression_tests/Phase1_10/<this> -> up 4 to repo root -> app/src
APP_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "app", "src")
sys.path.insert(0, APP_SRC)

from retention_model import RetentionModel, _DateContext  # noqa: E402

PASS, FAIL = "PASS", "FAIL"


def _model():
    return RetentionModel(
        None, base_exit=0.20, beta=1.0, gamma=0.5, floor_exit=0.05,
        vac_ref=0.07, burden_ceiling=0.50, burden_floor=0.30, regional_vacancy_rate=0.034)


def _ctx(regional_vacancy):
    # rent_factor_now = 1.0 (no inflation); empty wage schedule -> wage factor 1.0
    # as of any date, so income(t) == signing_income.
    return _DateContext(rent_factor_now=1.0, wage_factor_now=1.0,
                        wage_months=[], wage_prefix=[], regional_vacancy=regional_vacancy)


def _exit(m, ctx, eff_rent, market_anchor_rent, income):
    # market_anchor_rent = the unit's AdjustedRent post-KD-012 (param renamed
    # so a future reader can't quietly re-anchor retention on BaseRent).
    return m.voluntary_exit_prob(
        ctx, effective_rent=eff_rent, market_anchor_rent=market_anchor_rent,
        signing_income=income, income_reference_date=date(2025, 1, 1))


def t_base_case(m):
    """No discount, full availability, affordable -> exit == base_exit."""
    p = _exit(m, _ctx(0.07), 1000.0, 1000.0, 10000.0)  # disc 0; burden 0.10 -> gap 0; avail 1.0
    ok = abs(p - 0.20) < 1e-9
    print(f"  base_case exit={p:.4f} (expect 0.20) [{PASS if ok else FAIL}]")
    return ok


def t_discount_lowers_exit(m):
    ctx = _ctx(0.07)
    p0 = _exit(m, ctx, 1000.0, 1000.0, 10000.0)  # disc 0.0
    p1 = _exit(m, ctx, 800.0, 1000.0, 10000.0)   # disc 0.2
    p2 = _exit(m, ctx, 600.0, 1000.0, 10000.0)   # disc 0.4
    ok = p0 > p1 > p2 and abs(p1 - 0.20 * 0.8) < 1e-9
    print(f"  discount monotone: {p0:.4f} > {p1:.4f} > {p2:.4f} [{PASS if ok else FAIL}]")
    return ok


def t_scarcity_lowers_exit(m):
    # lower regional vacancy -> lower availability -> higher scarcity -> lower exit
    p_hi = _exit(m, _ctx(0.070), 1000.0, 1000.0, 10000.0)  # avail 1.0, scar 0.0
    p_mid = _exit(m, _ctx(0.035), 1000.0, 1000.0, 10000.0)  # avail 0.5, scar 0.5
    p_lo = _exit(m, _ctx(0.007), 1000.0, 1000.0, 10000.0)  # avail 0.1, scar 0.9
    ok = p_hi > p_mid > p_lo
    print(f"  scarcity monotone: {p_hi:.4f} > {p_mid:.4f} > {p_lo:.4f} [{PASS if ok else FAIL}]")
    return ok


def t_unaffordable_lowers_exit(m):
    # lower income -> higher rent burden -> higher affordability_gap -> lower exit
    ctx = _ctx(0.07)  # availability 1.0
    p_rich = _exit(m, ctx, 1000.0, 1000.0, 10000.0)  # burden 0.10 -> gap 0.0
    p_poor = _exit(m, ctx, 1000.0, 1000.0, 2500.0)   # burden 0.40 -> gap 0.5
    ok = p_rich > p_poor
    print(f"  affordability: rich {p_rich:.4f} > poor {p_poor:.4f} [{PASS if ok else FAIL}]")
    return ok


def t_inverted_discount_clamps_to_base(m):
    """Downturn: effective_rent > market -> discount<0 -> raw>base -> clamp to base_exit."""
    p = _exit(m, _ctx(0.07), 1200.0, 1000.0, 10000.0)  # disc -0.2 -> raw 0.24 -> clamp 0.20
    ok = abs(p - 0.20) < 1e-9
    print(f"  inverted-discount clamp exit={p:.4f} (expect 0.20 base) [{PASS if ok else FAIL}]")
    return ok


def t_floor_clamp(m):
    """Deep discount + high scarcity -> raw below floor -> clamp to floor_exit."""
    p = _exit(m, _ctx(0.007), 100.0, 1000.0, 10000.0)  # disc 0.9, scar 0.9 -> raw ~0.011 -> 0.05
    ok = abs(p - 0.05) < 1e-9
    print(f"  floor clamp exit={p:.4f} (expect 0.05 floor) [{PASS if ok else FAIL}]")
    return ok


def t_zero_income_locks_in(m):
    """Missing/zero income -> affordability_gap = 1 -> maximally locked (no divide error)."""
    p = _exit(m, _ctx(0.07), 1000.0, 1000.0, 0.0)   # gap 1.0 -> scar 1.0 -> raw 0.20*0.5
    p_none = _exit(m, _ctx(0.07), 1000.0, 1000.0, None)
    ok = abs(p - 0.10) < 1e-9 and abs(p_none - 0.10) < 1e-9
    print(f"  zero/None income exit={p:.4f}/{p_none:.4f} (expect 0.10) [{PASS if ok else FAIL}]")
    return ok


def t_bounds_always_in_range(m):
    """Sweep a grid; every result stays in [floor_exit, base_exit]."""
    ok = True
    for vac in (0.001, 0.02, 0.05, 0.07, 0.12):
        for eff in (100.0, 500.0, 900.0, 1000.0, 1300.0):
            for inc in (1500.0, 3000.0, 8000.0, 20000.0):
                p = _exit(m, _ctx(vac), eff, 1000.0, inc)
                if not (0.05 - 1e-9 <= p <= 0.20 + 1e-9):
                    ok = False
    print(f"  bounds [floor,base] over 100-cell grid [{PASS if ok else FAIL}]")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=None)  # unused (pure formula) — accepted for the suite
    ap.add_argument("--assert", dest="do_assert", action="store_true")
    ap.parse_args()

    m = _model()
    print("Phase 1.10 - retention model formula regression")
    results = [
        t_base_case(m),
        t_discount_lowers_exit(m),
        t_scarcity_lowers_exit(m),
        t_unaffordable_lowers_exit(m),
        t_inverted_discount_clamps_to_base(m),
        t_floor_clamp(m),
        t_zero_income_locks_in(m),
        t_bounds_always_in_range(m),
    ]
    npass = sum(results)
    print(f"\nResult: {npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
