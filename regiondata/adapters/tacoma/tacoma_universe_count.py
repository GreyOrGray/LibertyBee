"""Tacoma-scoped small-multifamily universe count: join tax_account (use codes)
+ improvement_builtas (units) + appraisal_account (lat/lng), point-in-polygon
against the Census TIGERweb Tacoma city boundary. Recon for the pierce builder."""
import json
import os

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

# parcel -> (lat, lng)
coords = {}
with open(os.path.join(EX, "appraisal_account.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 24:
            continue
        try:
            lat, lng = float(p[22]), float(p[23])
        except ValueError:
            continue
        coords[p[0]] = (lat, lng)

# parcel -> total units (sum of builtas units across buildings)
units = {}
with open(os.path.join(EX, "improvement_builtas.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 17:
            continue
        try:
            u = int(float(p[16]))
        except ValueError:
            continue
        units[p[0]] = units.get(p[0], 0) + u

MF_CODES = {"1202": "duplex", "1203": "triplex", "1204": "fourplex", "1305": "apts5plus"}
county = {}
tacoma = {}
t58 = 0
with open(os.path.join(EX, "tax_account.txt"), encoding="latin-1") as f:
    for line in f:
        p = line.rstrip("\n").split("|")
        if len(p) < 8 or p[4] not in MF_CODES:
            continue
        kind = MF_CODES[p[4]]
        county[kind] = county.get(kind, 0) + 1
        c = coords.get(p[0])
        if c and in_tacoma(c[1], c[0]):
            tacoma[kind] = tacoma.get(kind, 0) + 1
            if kind == "apts5plus" and 5 <= units.get(p[0], 0) <= 8:
                t58 += 1

print("county-wide:", county)
print("TACOMA CITY:", tacoma)
print("TACOMA 1305 parcels with builtas units 5-8:", t58)
total = sum(tacoma.get(k, 0) for k in ("duplex", "triplex", "fourplex")) + t58
print("TACOMA 2-8 unit universe (2-4 codes + 5-8 slice):", total)
