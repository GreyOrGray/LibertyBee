"""Tacoma-scoped assessed-to-sale ratio from Pierce sale.txt (massgis
assessment_ratios pattern): recent arms-length sales of 2-8 unit Tacoma
parcels vs their current assessed totals. WA DOR county ratio ~91% is the
cross-check."""
import json
import os
import statistics

BASE = r"c:\LibertyBee\scratch\national_panel\tacoma"
EX = os.path.join(BASE, "extracted")

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

MF = {"1202", "1203", "1204", "1305"}
assessed = {}   # parcel -> current total assessed
with open(os.path.join(EX, "tax_account.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 14 or p[4] not in MF:
            continue
        c = coords.get(p[0])
        if not c or not in_tacoma(c[1], c[0]):
            continue
        try:
            assessed[p[0]] = int(p[12])
        except ValueError:
            continue

print("Tacoma MF parcels with assessed totals:", len(assessed))

ratios = []
years = {}
with open(os.path.join(EX, "sale.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        # sale.txt layout: sale_id | parcel_count | PARCEL | date | price | deed | ...
        if len(p) < 6 or p[2] not in assessed:
            continue
        date, price, deed = p[3], p[4], p[5]
        try:
            yr = int(date.split("/")[-1])
            pr = float(price)
        except (ValueError, IndexError):
            continue
        if yr < 2024 or pr < 100000:
            continue  # recency window + non-arms-length floor
        if "Warranty" not in deed and "Statutory" not in deed:
            continue
        av = assessed[p[2]]
        if av <= 0:
            continue
        r = av / pr
        if 0.2 <= r <= 3.0:  # discard obvious partial-interest/outlier pairs
            ratios.append(r)
            years[yr] = years.get(yr, 0) + 1

print("qualifying sales 2024+:", len(ratios), "by year:", dict(sorted(years.items())))
if ratios:
    med = statistics.median(ratios)
    print(f"median assessed/sale ratio: {med:.4f}")
    print(f"(WA DOR Pierce 2024 cross-check: 0.909)")
