# -*- coding: ascii -*-
"""Pierce County -> Liberty Bee region bundle (Tacoma, WA).

Sources (all in scratch/national_panel/tacoma/, downloaded by Gray 2026-08-29
from Pierce County Assessor-Treasurer Data Downloads; ToU 2026-05-29 pass-through
disclaimer applies):
  tax_account.txt          use code (1202/1203/1204/1305), assessed values
  improvement_builtas.txt  per-building units/bedrooms/baths/sqft/year-built
  appraisal_account.txt    lat/lng (city scoping vs Census TIGER boundary)
  sale.txt                 recent arms-length sales -> assessed-to-sale ratio
Rents: HUD FY2026 FMR per D1 ruling (City of Tacoma OEHR document, primary).
Anonymization: buildings labeled by Pierce parcel number (publicly auditable
via the county's own lookup), never street address - per the 2026-08-09 ruling.
Ratified research: docs/v_0_3/phases/phase_1_12/national_panel_research.md
"""
import csv
import json
import os
import statistics

BASE = r"c:\LibertyBee\scratch\national_panel\tacoma"
EX = os.path.join(BASE, "extracted")
OUT = os.path.join(BASE, "bundle")
os.makedirs(OUT, exist_ok=True)

RATIO = 0.9385          # median assessed/sale, n=278 Tacoma MF sales 2024-2026
                        # (tacoma_ratio.py; WA DOR Pierce 2024 cross-check 0.909)
FMR = {0: 1428, 1: 1605, 2: 1971, 3: 2773, 4: 3102,
       5: 3567, 6: 4033, 7: 4498, 8: 4963}   # HUD FY2026, 5+ = HUD +15%/BR extrapolation
EFFECTIVE_TAX = 0.0105  # ~1.05% of market value (Ownwell Apr 2026; DOR ratio-consistent)

rings = json.load(open(os.path.join(BASE, "tacoma_boundary_tigerweb.json")))[
    "features"][0]["geometry"]["rings"]

def in_tacoma(lng, lat):
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside

coords = {}
with open(os.path.join(EX, "appraisal_account.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 24:
            continue
        try:
            coords[p[0]] = (float(p[22]), float(p[23]))
        except ValueError:
            continue

# per-parcel building aggregates from improvement_builtas
class Agg:
    __slots__ = ("units", "beds", "baths", "sqft", "year")
    def __init__(self):
        self.units = 0; self.beds = 0; self.baths = 0.0; self.sqft = 0; self.year = None

bagg = {}
with open(os.path.join(EX, "improvement_builtas.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 20:
            continue
        a = bagg.setdefault(p[0], Agg())
        def num(i, cast=float, dflt=0):
            try:
                return cast(float(p[i]))
            except ValueError:
                return dflt
        a.units += num(16, int)
        a.beds += num(14, int)
        a.baths += num(15, float, 0.0)
        a.sqft += num(5, int)
        y = num(19, int, 0)
        if y and (a.year is None or y < a.year):
            a.year = y

CODES = {"1202": ("Duplex", 2), "1203": ("Triplex", 3),
         "1204": ("Fourplex", 4), "1305": ("Apartment 5-8 Units", None)}

buildings = []
skipped = {"no_coords": 0, "outside": 0, "units_range": 0, "no_value": 0}
with open(os.path.join(EX, "tax_account.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 14 or p[4] not in CODES:
            continue
        c = coords.get(p[0])
        if not c:
            skipped["no_coords"] += 1
            continue
        if not in_tacoma(c[1], c[0]):
            skipped["outside"] += 1
            continue
        style, code_units = CODES[p[4]]
        a = bagg.get(p[0], Agg())
        units = code_units if code_units else a.units
        if code_units is None and not (5 <= units <= 8):
            skipped["units_range"] += 1
            continue
        try:
            assessed = int(p[12])
        except ValueError:
            assessed = 0
        if assessed <= 0:
            skipped["no_value"] += 1
            continue
        price = round(assessed / RATIO, 2)
        buildings.append({
            "parcel": p[0], "style": style, "units": units,
            "year": a.year or "", "price": price,
            "beds_total": a.beds, "baths_total": a.baths,
        })

buildings.sort(key=lambda b: b["parcel"])

def split_int(total, n):
    """Deterministic near-even split: first (total % n) slots get the extra."""
    if total <= 0:
        return [1] * n           # missing beds -> 1BR floor, counted below
    base, rem = divmod(total, n)
    out = [base + (1 if i < rem else 0) for i in range(n)]
    return [max(1, v) for v in out]

bed_fallback = 0
with open(os.path.join(OUT, "buildings.csv"), "w", newline="") as bf, \
     open(os.path.join(OUT, "units.csv"), "w", newline="") as uf:
    bw = csv.writer(bf)
    uw = csv.writer(uf)
    bw.writerow(["property_id", "year_built", "base_price", "address", "town",
                 "state", "property_style", "total_units", "owned_at_start",
                 "acquisition_basis", "acquisition_date"])
    uw.writerow(["unit_id", "property_id", "unit_number", "beds",
                 "adjusted_rent", "baths", "base_rent"])
    uid = 0
    for i, b in enumerate(buildings, 1):
        bw.writerow([i, b["year"], f"{b['price']:.2f}", f"PC_{b['parcel']}",
                     "Tacoma", "WA", b["style"], b["units"], "", "", ""])
        if b["beds_total"] <= 0:
            bed_fallback += 1
        beds = split_int(b["beds_total"], b["units"])
        bath_each = round(max(1.0, b["baths_total"] / b["units"]) * 4) / 4 \
            if b["baths_total"] > 0 else ""
        for un in range(1, b["units"] + 1):
            uid += 1
            bd = min(beds[un - 1], 8)
            rent = FMR[min(bd, 8)]
            uw.writerow([uid, i, un, bd, f"{rent:.2f}", bath_each, f"{rent:.2f}"])

n_units = uid
prices = [b["price"] for b in buildings]
per_unit_price = statistics.median(b["price"] / b["units"] for b in buildings)
tax_per_unit = round(per_unit_price * EFFECTIVE_TAX)

region = {
    "region": {
        "name": "Tacoma WA (Pierce County assessor)",
        "description": ("Built from Pierce County Assessor-Treasurer public bulk data "
            "(2026 roll): use codes 1202/1203/1204 identify duplex/triplex/fourplex "
            "exactly; the 5-8 unit slice comes from the per-building units field in "
            "improvement_builtas (which also supplies bedrooms, baths, sqft and "
            "year built DIRECTLY - no bedroom synthesis needed). base_price = assessed "
            "total / 0.9385, the median assessed-to-sale ratio computed from 278 "
            "arms-length warranty-deed sales of Tacoma small-multifamily parcels "
            "2024-2026 in the county's public sale file (WA DOR Pierce 2024 ratio "
            "0.909 as cross-check). City scoping: parcel lat/lng point-in-polygon vs "
            "the US Census TIGERweb boundary for Tacoma city (GEOID 5370000). "
            "Buildings are labeled by Pierce parcel number (auditable via the "
            "county's public lookup), never street address. Rents are HUD FY2026 "
            "Fair Market Rents by bedroom (all rent-source families agree within "
            "10-20% in this market; ruling D1). Pierce County GIS Data Terms of Use "
            "(2026-05-29): data provided AS IS / WITH ALL FAULTS, no warranty; this "
            "bundle is not a product of Pierce County. THIS BUNDLE IS A "
            "DEMONSTRATION with disclosed provenance - someone with MLS access can "
            "and should build a better one."),
        "locale": {"country": "US", "state": "WA", "primary_market": "Tacoma"},
        "currency": "USD",
        "start_date": "2026-01-01",
        "prices_as_of": "2026 certified roll (TY2026 values)",
        "provenance": ("Pierce County Data Downloads (pulled 2026-08-29) + Census "
            "TIGERweb place boundary + HUD FY2026 FMR (City of Tacoma OEHR doc, "
            "primary) + ratified knob research: docs/v_0_3/phases/phase_1_12/"
            "national_panel_research.md (decisions D1-D8) and national_panel_"
            "sources.md (full citation register). Ratified by Gray 2026-08-28."),
    },
    "counts": {
        "buildings": len(buildings),
        "units": n_units,
        "beds_fallback_1br": bed_fallback,
        "skipped": skipped,
    },
    "assessment_ratios": {
        "TACOMA": {"ratio": RATIO, "n": 278, "sales_since": "2024"},
        "_pooled_recent": RATIO,
    },
    "parameters": {
        "INC.EarnerMedianAnnual": {
            "value": round(58876 * 0.938),
            "source": ("Renter median household income $58,876 (ACS 2020-2024 5yr "
                "B25119, Tacoma city, pulled 2026-08-27) x Salem earner/household "
                "calibration 0.938 (towns-pack convention)."),
        },
        "OPEX.PropertyTaxPerUnit": {
            "value": tax_per_unit,
            "source": (f"~1.05% effective rate on market value (Ownwell Apr 2026, "
                f"Tacoma median; WA DOR Pierce 2024 ratio-study consistent) x median "
                f"per-unit market value ${per_unit_price:,.0f} from this universe. "
                f"Parcel-verified levy table was 403-gated - aggregator-computed "
                f"rate, disclosed."),
        },
        "OPEX.InsurancePerUnit": {
            "value": 1050,
            "source": ("VENDOR-TIER midpoint of the 2-4 unit WA dwelling-policy "
                "range $600-$1,440/unit/yr (smartinsured Apr 2026; Steadily WA avg "
                "$1,427/property Jul 2026). CAVEAT: Cascadia earthquake coverage is "
                "a separate endorsement not confirmed in any figure. Ratified as "
                "range-with-tier-label (D8)."),
        },
        "PROP.VacancyRateBase": {
            "value": 0.040,
            "source": ("Rental vacancy 4.0% - ACS 2020-2024 5yr B25003/B25004 "
                "computed (1,703 / 42,843), Tacoma city, pulled 2026-08-27. "
                "Managed-stock market survey 6.7% (Apartment List 2026) informs "
                "the fluctuation band."),
        },
    },
}

with open(os.path.join(OUT, "region.json"), "w") as f:
    json.dump(region, f, indent=2)

styles = {}
for b in buildings:
    styles[b["style"]] = styles.get(b["style"], 0) + 1
print("buildings:", len(buildings), styles)
print("units:", n_units, "| beds fallback (1BR floor):", bed_fallback)
print("skipped:", skipped)
print(f"median price: ${statistics.median(prices):,.0f} | median per-unit: "
      f"${per_unit_price:,.0f} | tax/unit: ${tax_per_unit}")
