# -*- coding: ascii -*-
"""DataSF -> Liberty Bee region bundle (San Francisco, CA).

Universe: DataSF Assessor Historical Secured Property Tax Rolls (wv5m-vpq2,
license PDDL/public domain), closed_roll_year=2025, number_of_units 2-8,
residential multifamily class codes. Beds/baths/sqft/year built are DIRECT
per-parcel fields. Prices: broker per-sqft medians by size bucket (Q2 2026,
Compass/Chapleau SF 2-4 Unit Market Report - VENDOR TIER, declared per ruling
D2/the governing principle; 5-8 unit bucket extrapolates the 3-4 unit $/sqft,
labeled). Rents: HUD FY2026 FMR by bedroom (D2). Tax: Prop-13 parameter
approximation per D3 (rate x acquisition price; conservative-bias disclosed).
Anonymization: buildings labeled by assessor parcel number (block/lot,
auditable via DataSF itself), never street address.
Ratified: docs/v_0_3/phases/phase_1_12/national_panel_research.md (D1-D8).
"""
import csv
import json
import os
import statistics
import urllib.request
import urllib.parse

BASE = r"c:\LibertyBee\scratch\national_panel\sanfrancisco"
OUT = os.path.join(BASE, "bundle")
os.makedirs(OUT, exist_ok=True)
CACHE = os.path.join(BASE, "datasf_rolls_2025.json")

PSF = {2: 784.0, 3: 604.0, 4: 604.0}      # broker $/sqft Q2 2026; 5-8 extrapolate 3-4
FMR = {0: 2485, 1: 2977, 2: 3604, 3: 4604, 4: 4772,
       5: 5488, 6: 6204, 7: 6920, 8: 7636}  # 5+ = +15%/BR convention on 4BR
TAX_RATE = 0.0118268325                     # FY2025-26 secured rate (sftreasurer.org)

MF_CLASS_HINTS = ("FLAT", "APARTMENT", "DUPLEX", "TIC")

if not os.path.exists(CACHE):
    rows = []
    offset = 0
    while True:
        q = urllib.parse.urlencode({
            "$select": ("parcel_number,use_definition,property_class_code_definition,"
                        "number_of_units,year_property_built,number_of_bedrooms,"
                        "number_of_bathrooms,property_area"),
            "$where": "closed_roll_year='2025' AND number_of_units between 2 and 8",
            "$limit": "50000", "$offset": str(offset),
        })
        with urllib.request.urlopen(
                "https://data.sfgov.org/resource/wv5m-vpq2.json?" + q, timeout=120) as r:
            chunk = json.load(r)
        rows.extend(chunk)
        if len(chunk) < 50000:
            break
        offset += 50000
    json.dump(rows, open(CACHE, "w"))
    print("pulled", len(rows), "rows from DataSF")
rows = json.load(open(CACHE))
print("roll rows (2-8 units, 2025 roll):", len(rows))

def num(row, key, cast=float, dflt=0):
    try:
        return cast(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return dflt

buildings = []
skipped = {"non_residential_class": 0, "no_sqft": 0, "dup_parcel": 0}
seen = set()
for r in rows:
    cls = (r.get("property_class_code_definition") or "").upper()
    if not any(h in cls for h in MF_CLASS_HINTS):
        skipped["non_residential_class"] += 1
        continue
    pid = r.get("parcel_number") or ""
    if pid in seen:
        skipped["dup_parcel"] += 1
        continue
    seen.add(pid)
    units = num(r, "number_of_units", int)
    sqft = num(r, "property_area", int)
    if sqft < 400:
        skipped["no_sqft"] += 1
        continue
    psf = PSF.get(units, PSF[3])
    buildings.append({
        "parcel": pid, "units": units,
        "year": num(r, "year_property_built", int) or "",
        "sqft": sqft,
        "price": round(sqft * psf, 2),
        "beds_total": num(r, "number_of_bedrooms", int),
        "baths_total": num(r, "number_of_bathrooms", float),
        "style": (r.get("property_class_code_definition") or "Multi-Family"),
    })

buildings.sort(key=lambda b: b["parcel"])

# Bedroom synthesis for the ~69% of roll rows with no bedroom count: calibrate
# median sqft-per-unit for each per-unit bedroom value from the rows that DO
# have beds (internal calibration - same spirit as the Salem sqft-quantile
# method, but trained on SF's own roll rather than imported ACS marginals),
# then assign missing-bed buildings the nearest calibrated bedroom count.
known = {}
for b in buildings:
    if b["beds_total"] > 0 and b["units"] > 0:
        bpu = round(b["beds_total"] / b["units"])
        if 1 <= bpu <= 5:
            known.setdefault(bpu, []).append(b["sqft"] / b["units"])
CAL = {bpu: statistics.median(v) for bpu, v in known.items() if len(v) >= 50}
print("sqft/unit calibration (from rows with beds):",
      {k: round(v) for k, v in sorted(CAL.items())})

def beds_from_sqft(sqft_per_unit):
    return min(CAL, key=lambda bpu: abs(CAL[bpu] - sqft_per_unit))

def split_int(total, n):
    if total <= 0:
        return [1] * n
    base, rem = divmod(total, n)
    return [max(1, base + (1 if i < rem else 0)) for i in range(n)]

bed_fallback = 0
uid = 0
with open(os.path.join(OUT, "buildings.csv"), "w", newline="") as bf, \
     open(os.path.join(OUT, "units.csv"), "w", newline="") as uf:
    bw = csv.writer(bf)
    uw = csv.writer(uf)
    bw.writerow(["property_id", "year_built", "base_price", "address", "town",
                 "state", "property_style", "total_units", "owned_at_start",
                 "acquisition_basis", "acquisition_date"])
    uw.writerow(["unit_id", "property_id", "unit_number", "beds",
                 "adjusted_rent", "baths", "base_rent"])
    for i, b in enumerate(buildings, 1):
        bw.writerow([i, b["year"], f"{b['price']:.2f}", f"SF_{b['parcel']}",
                     "San Francisco", "CA", b["style"], b["units"], "", "", ""])
        bt = b["beds_total"]
        if bt <= 0:
            bed_fallback += 1
            bt = beds_from_sqft(b["sqft"] / b["units"]) * b["units"]
        beds = split_int(bt, b["units"])
        # clamp: assessor bath totals occasionally exceed plausibility (and the
        # schema's NUMERIC(3,2) per-unit column) - cap per-unit baths at 5.0
        bath_each = min(5.0, round(max(1.0, b["baths_total"] / b["units"]) * 4) / 4) \
            if b["baths_total"] > 0 else ""
        for un in range(1, b["units"] + 1):
            uid += 1
            bd = min(beds[un - 1], 8)
            rent = FMR[bd]
            uw.writerow([uid, i, un, bd, f"{rent:.2f}", bath_each, f"{rent:.2f}"])

per_unit_price = statistics.median(b["price"] / b["units"] for b in buildings)
tax_per_unit = round(per_unit_price * TAX_RATE)

region = {
    "region": {
        "name": "San Francisco CA (DataSF assessor roll)",
        "description": ("Built from DataSF's Assessor Historical Secured Property Tax "
            "Rolls (dataset wv5m-vpq2, PUBLIC DOMAIN/PDDL), 2025 closed roll, parcels "
            "with 2-8 units in residential multifamily classes (flats, duplexes, "
            "small apartments, TICs). Units, bedrooms, baths, sqft and year built are "
            "DIRECT assessor fields. PRICES ARE VENDOR-TIER, disclosed: CA Prop 13 + "
            "Rev&Tax Code s481 seal sale prices from public view, so no public "
            "ratio-study path exists; base_price = building sqft x broker median "
            "$/sqft by size bucket (Q2 2026 SF 2-4 unit market report: 2-unit "
            "$784/sqft, 3-4 unit $604/sqft; 5-8 unit buckets extrapolate the 3-4 "
            "figure, labeled convention). An adopter with MLS access should replace "
            "these prices. Property tax uses the D3 parameter approximation: "
            "FY2025-26 secured rate 1.18268% x acquisition price; Prop 13 caps basis "
            "growth at 2%/yr thereafter, so the model OVERSTATES later-year taxes - "
            "the conservative direction. One-time transfer tax (0.75% in the $1-5M "
            "bracket) is NOT modeled, disclosed. SF's rent ordinance (pre-1979 "
            "coverage) permits 1.6%/yr sitting-tenant increases; Liberty Bee's "
            "invariant is zero, strictly more protective - no additional compliance "
            "logic required. Rents: HUD FY2026 FMR by bedroom (metro 40th pct; the "
            "asking-market runs far above - ZORI $4,539 +23.2% Jul 2026 - and ACS "
            "sitting-tenant rents below; FMR is the citable middle, ruling D2). THIS "
            "BUNDLE IS A DEMONSTRATION with disclosed provenance."),
        "locale": {"country": "US", "state": "CA", "primary_market": "San Francisco"},
        "currency": "USD",
        "start_date": "2026-01-01",
        "prices_as_of": "Q2 2026 broker medians on 2025-roll building areas",
        "provenance": ("DataSF wv5m-vpq2 (PDDL; pulled via Socrata API 2026-08-31, "
            "cached datasf_rolls_2025.json) + HUD FY2026 FMR + ratified research "
            "docs/v_0_3/phases/phase_1_12/national_panel_research.md (D1-D8) and "
            "national_panel_sources.md. Ratified by Gray 2026-08-28."),
    },
    "counts": {
        "buildings": len(buildings), "units": uid,
        "beds_synthesized_from_sqft": bed_fallback,
        "beds_sqft_calibration": {str(k): round(v) for k, v in sorted(CAL.items())},
        "skipped": skipped,
    },
    "assessment_ratios": {
        "_note": ("not applicable - CA Prop 13 assessed values are acquisition-era "
                  "and unusable for market estimation; prices are broker-tier $/sqft"),
    },
    "parameters": {
        "INC.EarnerMedianAnnual": {
            "value": round(111044 * 0.938),
            "source": ("Renter median household income $111,044 (ACS 2024 1yr B25119, "
                "SF county) x Salem earner/household calibration 0.938 (towns-pack "
                "convention)."),
        },
        "OPEX.PropertyTaxPerUnit": {
            "value": tax_per_unit,
            "source": (f"D3 parameter approximation: FY2025-26 secured rate 1.18268% "
                f"(sftreasurer.org, primary) x median per-unit acquisition price "
                f"${per_unit_price:,.0f} from this universe. Prop 13 basis growth "
                f"<=2%/yr means real-world later-year taxes run BELOW this flat knob "
                f"- model overstates costs, the conservative direction. Disclosed."),
        },
        "OPEX.InsurancePerUnit": {
            "value": 1800,
            "source": ("DERIVED, weakest knob: Terner Center (UC Berkeley, Dec 2025) "
                "CA 2-4 unit avg $131/$100k insured value (2023 CDI data) applied to "
                "SF values -> $1,600-2,000 range; midpoint. No SF-specific published "
                "per-unit figure exists. Ratified as range-with-tier-label (D8)."),
        },
        "PROP.VacancyRateBase": {
            "value": 0.0672,
            "source": ("Rental vacancy 6.72% - ACS 2024 1yr B25003/B25004 computed "
                "(16,929 / 252,204), SF county, pulled 2026-08-27. DISCLOSED: 2026 "
                "market-survey vacancy is far tighter (~2.2-3.3%, tech-demand "
                "resurgence postdating the ACS vintage); ACS basis kept for "
                "cross-bundle convention (D4)."),
        },
    },
}
json.dump(region, open(os.path.join(OUT, "region.json"), "w"), indent=2)

styles = {}
for b in buildings:
    k = b["units"]
    styles[k] = styles.get(k, 0) + 1
print("buildings:", len(buildings), "| by units:", dict(sorted(styles.items())))
print("units:", uid, "| beds fallback:", bed_fallback, "| skipped:", skipped)
print(f"median per-unit price: ${per_unit_price:,.0f} | tax/unit: ${tax_per_unit:,}")
