"""
Phase 1.8 "who Liberty Bee can't reach" + reach-extension probe (ba_answers P +
the keep-the-tail promise; feeds the baseline-of-record + the public site).

Measures, against the renter-calibrated applicant income population (IncomeModel,
month-0 dollars):
  * "Can't reach" = share of households whose income can't clear the 30% screen
    (MaxRentToIncomeRatio) even against Liberty Bee's BELOW-market rent.
  * Reach EXTENSION = how much further down the income ladder the below-market
    discount reaches vs a market landlord (the mission, quantified).
Against the actual unit-rent distribution the model uses (PropertyUnits.BaseRent
= market rent; LB rent = BaseRent x (1 - BelowMarketRentPct)).

Usage: python reach_probe.py --env <db> --run-id <n>
"""
import argparse, os, random, statistics, sys
from datetime import date
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app", "src"))
from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from income_model import IncomeModel

N = 40000
PROBE_DATE = date(2025, 1, 1)


def reach_share(monthly_incomes, monthly_rent, max_ratio):
    """Share who CAN afford this rent at the 30% screen (rent/income <= max_ratio)."""
    need = monthly_rent / float(max_ratio)  # income needed
    return sum(1 for m in monthly_incomes if m >= need) / len(monthly_incomes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--run-id", type=int, required=True)
    args = ap.parse_args()
    db = DatabaseManager(os.path.join(REPO, "environments", args.env, "db_config.json"))
    reg = ParameterRegistry(db).load_globals()

    discount = float(reg.get_decimal('PROP', 'BelowMarketRentPct'))
    max_ratio = float(reg.get_decimal('QUAL', 'MaxRentToIncomeRatio'))
    prescreen = float(reg.get_decimal('QUAL', 'PreScreenRentToIncomeRatio'))

    # --- generate the renter-calibrated income population (month-0) ------------
    model = IncomeModel(db, reg, run_id=args.run_id)
    ht_w = [reg.get_float('TNT', n) for n in (
        'HouseholdTypeSingleWeight','HouseholdTypeCoupleWeight',
        'HouseholdTypeFamilyWeight','HouseholdTypeRoommatesWeight')]
    rm_w = [reg.get_float('TNT', f'RoommatesAdults{i}Weight') for i in (2,3,4)]
    rng = random.Random(20260706)
    incomes = []
    for i in range(N):
        ht = rng.choices(['SINGLE','COUPLE','FAMILY','ROOMMATES'], weights=ht_w)[0]
        adults = rng.choices([2,3,4], weights=rm_w)[0] if ht=='ROOMMATES' else (1 if ht=='SINGLE' else 2)
        m,_ = model.generate_household_income(ht, adults, random.Random(f"reach_{i}"), PROBE_DATE)
        incomes.append(float(m))
    incomes.sort()

    # --- real unit rents from the run (BaseRent = market; LB = x(1-discount)) --
    rows = db.execute_query(
        "SELECT Beds, BaseRent FROM simulation.PropertyUnits WHERE RunID=? AND BaseRent IS NOT NULL", (args.run_id,))
    market_rents = [float(r[1]) for r in rows]
    by_bed = {}
    for beds, br in rows:
        by_bed.setdefault(int(beds), []).append(float(br))
    med_market = statistics.median(market_rents)
    med_lb = med_market * (1 - discount)

    print(f"=== reach probe — {N:,} households, {len(market_rents)} units, run {args.run_id} ===")
    print(f"discount {discount:.0%} | 30%-screen ratio {max_ratio} | prescreen {prescreen}")
    print(f"median unit: market ${med_market:,.0f}/mo -> LB ${med_lb:,.0f}/mo")
    print(f"income needed @30%: market ${med_market/max_ratio*12:,.0f}/yr | LB ${med_lb/max_ratio*12:,.0f}/yr\n")

    r_lb = reach_share(incomes, med_lb, max_ratio)
    r_mkt = reach_share(incomes, med_market, max_ratio)
    print("[MEDIAN UNIT]")
    print(f"  can afford LB unit @30%:      {100*r_lb:5.1f}%   -> CANNOT REACH: {100*(1-r_lb):5.1f}%")
    print(f"  can afford MARKET unit @30%:  {100*r_mkt:5.1f}%   -> cannot reach: {100*(1-r_mkt):5.1f}%")
    print(f"  ★ reach EXTENSION (LB - market): +{100*(r_lb-r_mkt):.1f} pts of the renter population")

    # cheapest vs priciest unit (accessibility range)
    cheapest, priciest = min(market_rents)*(1-discount), max(market_rents)*(1-discount)
    print(f"\n[LB RANGE] cheapest unit ${cheapest:,.0f}/mo -> {100*reach_share(incomes,cheapest,max_ratio):.1f}% reachable; "
          f"priciest ${priciest:,.0f}/mo -> {100*reach_share(incomes,priciest,max_ratio):.1f}% reachable")

    print("\n[BY BED-SIZE — market vs LB, % of renter population that can afford @30%]")
    print(f"  {'unit':>5} | {'market $':>9} {'afford%':>8} | {'LB $':>9} {'afford%':>8} | ext")
    for beds in sorted(by_bed):
        mr = statistics.median(by_bed[beds]); lr = mr*(1-discount)
        rm = 100*reach_share(incomes, mr, max_ratio); rl = 100*reach_share(incomes, lr, max_ratio)
        print(f"  {beds:>4}BR | ${mr:>8,.0f} {rm:>7.1f}% | ${lr:>8,.0f} {rl:>7.1f}% | +{rl-rm:.1f}pt")

    print(f"\n[POP MEDIAN] household income ${statistics.median(incomes)*12:,.0f}/yr")


if __name__ == "__main__":
    main()
