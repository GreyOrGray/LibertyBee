#!/usr/bin/env python
"""region_importer.py — load a Region bundle into a fresh Liberty Bee database.

Portability, Phase 1.12.2. A Region bundle is a folder (see
docs/.../ingest_contract_spec.md):
    <region>/region.json  buildings.csv  units.csv  [names.csv]

The importer:
  1. VALIDATES the bundle fail-loud (required fields, types, FK integrity, sane ranges).
  2. Creates a fresh DB (Gold base + migrations, via migration_manager).
  3. Overwrites the region: reference.Properties / reference.Units from the CSVs, and the
     region-calibrated parameters from region.json into reference.ParameterRegistryDefault.
  4. Provenance-stamps the ingest.

MVP (1.12.2a): base = Gold (already carries schema + universal config + default params);
the bundle's params overwrite the relevant defaults. The round-trip gate imports
regiondata/fixtures/salem_reference_v2 and asserts master_test_runner --regression
reproduces V2. Generic-national defaults + a region-blank base (for adopters who omit
params) are 1.12.2b.

Usage:
    python region_importer.py --bundle regiondata/bundles/massachusetts/salem --label <purpose>
    python region_importer.py --bundle <dir> --validate-only
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys

import pyodbc

REPO = os.path.dirname(os.path.abspath(__file__))
MIGRATION_MANAGER = os.path.join(REPO, "environmentscripts", "migration_manager.py")
REGION_DEFAULTS = os.path.join(REPO, "regiondata", "defaults.json")

REQUIRED_BUILDING = ["property_id", "year_built", "base_price"]
REQUIRED_UNIT = ["property_id", "unit_number", "beds", "adjusted_rent"]  # unit_id optional — we generate it


def load_defaults():
    return json.loads(open(REGION_DEFAULTS, encoding="utf-8").read())


def required_params(defaults):
    return [k for k in defaults.get("required", {}) if not k.startswith("_")]


def bucket2_defaults(defaults):
    """Flat {param_name: {value, tier, source}} for every bucket-2 national default,
    expanding the 12-month seasonal groups to <base>_<Mon> registry names."""
    out = {}
    for name, spec in defaults.get("national_defaults_scalar", {}).items():
        if name.startswith("_"):
            continue
        out[name] = spec
    for base, spec in defaults.get("national_defaults_monthly", {}).items():
        if base.startswith("_"):
            continue
        for mon, val in spec["months"].items():
            out[f"{base}_{mon}"] = {"value": val, "tier": spec["tier"], "source": spec["source"]}
    return out


class BundleError(Exception):
    pass


# --------------------------------------------------------------------------- #
# validation (fail-loud, collect ALL errors)
# --------------------------------------------------------------------------- #

def _int(v, ctx, errs, allow_blank=False):
    if v == "" or v is None:
        if allow_blank:
            return None
        errs.append(f"{ctx}: required, is blank")
        return None
    try:
        return int(v)
    except ValueError:
        errs.append(f"{ctx}: '{v}' is not an integer")
        return None


def _dec(v, ctx, errs, positive=False, allow_blank=False):
    if v == "" or v is None:
        if allow_blank:
            return None
        errs.append(f"{ctx}: required, is blank")
        return None
    try:
        d = float(v)
    except ValueError:
        errs.append(f"{ctx}: '{v}' is not a number")
        return None
    if positive and d <= 0:
        errs.append(f"{ctx}: must be > 0 (got {d})")
    return d


def validate_bundle(path, required):
    """Returns (buildings, units, manifest). Raises BundleError with every problem found."""
    errs = []
    if not os.path.isdir(path):
        raise BundleError(f"bundle path is not a directory: {path}")
    for fn in ("region.json", "buildings.csv", "units.csv"):
        if not os.path.isfile(os.path.join(path, fn)):
            errs.append(f"missing required file: {fn}")
    if errs:
        raise BundleError("; ".join(errs))

    manifest = json.loads(open(os.path.join(path, "region.json"), encoding="utf-8").read())
    if "region" not in manifest or "parameters" not in manifest:
        errs.append("region.json must contain 'region' and 'parameters'")

    # buildings
    buildings, b_ids = [], set()
    with open(os.path.join(path, "buildings.csv"), encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        missing = [c for c in REQUIRED_BUILDING if c not in (rd.fieldnames or [])]
        if missing:
            errs.append(f"buildings.csv missing columns: {missing}")
        else:
            for i, row in enumerate(rd, 2):
                ctx = f"buildings.csv:{i}"
                pid = _int(row["property_id"], f"{ctx} property_id", errs)
                _int(row["year_built"], f"{ctx} year_built", errs, allow_blank=True)
                _dec(row["base_price"], f"{ctx} base_price", errs, positive=True)
                if pid is not None:
                    if pid in b_ids:
                        errs.append(f"{ctx}: duplicate property_id {pid}")
                    b_ids.add(pid)
                buildings.append(row)

    # units
    units = []
    with open(os.path.join(path, "units.csv"), encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        missing = [c for c in REQUIRED_UNIT if c not in (rd.fieldnames or [])]
        if missing:
            errs.append(f"units.csv missing columns: {missing}")
        else:
            u_ids, pu_seen = set(), set()
            for i, row in enumerate(rd, 2):
                ctx = f"units.csv:{i}"
                uid = _int(row.get("unit_id"), f"{ctx} unit_id", errs, allow_blank=True)  # optional — generated if omitted
                fk = _int(row["property_id"], f"{ctx} property_id", errs)
                label = (row.get("unit_number") or "").strip()  # free-form: "Apt A1", "Suite 2", "#3", "1"
                if label == "":
                    errs.append(f"{ctx} unit_number: required, is blank")
                _int(row["beds"], f"{ctx} beds", errs)
                _dec(row["adjusted_rent"], f"{ctx} adjusted_rent", errs, positive=True)
                if uid is not None:
                    if uid in u_ids:
                        errs.append(f"{ctx}: duplicate unit_id {uid}")
                    u_ids.add(uid)
                if fk is not None and label:
                    if (fk, label) in pu_seen:  # (property, unit label) must be unique — the compliance join key
                        errs.append(f"{ctx}: duplicate unit '{label}' within property {fk}")
                    pu_seen.add((fk, label))
                if fk is not None and b_ids and fk not in b_ids:
                    errs.append(f"{ctx}: property_id {fk} has no building (FK orphan)")
                units.append(row)

    # sane range on RET.BurdenFloorPct (lb-ba, KD-223)
    params = manifest.get("parameters", {}) if isinstance(manifest, dict) else {}
    floor = params.get("RET.BurdenFloorPct", {}).get("value") if "RET.BurdenFloorPct" in params else None
    ceil = params.get("RET.BurdenCeilingPct", {}).get("value", 0.50) if isinstance(params, dict) else 0.50
    if floor is not None:
        if not (0 < float(floor) < float(ceil) <= 1.0):
            errs.append(f"RET.BurdenFloorPct {floor} out of range (need 0 < floor < ceiling {ceil} <= 1.0)")
        elif not (0.20 <= float(floor) <= 0.40):
            print(f"  WARNING: RET.BurdenFloorPct {floor} is outside the usual 0.20-0.40 affordability band")

    # bucket-1 required params must be supplied — no honest national generic exists
    if isinstance(params, dict):
        for name in required:
            if name not in params:
                errs.append(f"region.json: required parameter '{name}' missing "
                            f"(no national default exists — supply your local value)")

    if errs:
        head = errs[:25]
        more = f"  (+{len(errs) - 25} more)" if len(errs) > 25 else ""
        raise BundleError("bundle validation failed:\n  - " + "\n  - ".join(head) + more)
    return buildings, units, manifest


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #

TOWNS = ("Salem", "Beverly", "Peabody", "Marblehead", "Lynn", "Danvers")


def create_base_db(label, pg=True):
    """Fresh base env; returns (db_name, conn_info).
    PG (default): template mint via migration_manager, conn_info = dict.
    SQL Server (--mssql): Gold restore + migrations, conn_info = pyodbc string."""
    cmd = [sys.executable, MIGRATION_MANAGER, "--label", label]
    if not pg:
        cmd.insert(2, "--mssql")
    out = subprocess.run(cmd, capture_output=True, text=True)
    print(out.stdout[-600:])
    if out.returncode != 0:
        raise BundleError(f"migration_manager failed:\n{out.stderr[-1000:]}")
    m = re.search(r"Test environment ready: (\S+)", out.stdout)
    if not m:
        raise BundleError("could not parse the created DB name from migration_manager")
    db = m.group(1)
    cfg = json.load(open(os.path.join(REPO, "environments", db, "db_config.json")))
    if pg:
        return db, {"host": cfg["server"], "port": cfg.get("port", 5432),
                    "dbname": db, "user": cfg["username"]}
    conn = (f"DRIVER={{{cfg['driver']}}};SERVER={cfg['server']};DATABASE={db};"
            f"Trusted_Connection=yes;Encrypt=no;")
    return db, conn


class _PgCursor:
    """psycopg cursor shim for the loader's pyodbc idioms: qmark->%s (the
    loader's SQL carries no '?' inside literals), varargs params, and
    IDENTITY_INSERT as a no-op — PG's GENERATED BY DEFAULT AS IDENTITY
    accepts explicit IDs without ceremony (sequences setval'd post-load)."""

    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, *params):
        if sql.lstrip().startswith("SET IDENTITY_INSERT"):
            return
        if len(params) == 1 and isinstance(params[0], (tuple, list)):
            params = params[0]
        self._c.execute(sql.replace("?", "%s"), params or None)

    def executemany(self, sql, rows):
        self._c.executemany(sql.replace("?", "%s"), rows)

    @property
    def rowcount(self):
        return self._c.rowcount


def load_region(conn_info, buildings, units, manifest, bundle_name, defaults, pg=False):
    if pg:
        import psycopg
        cx = psycopg.connect(**conn_info, autocommit=False)
        cur = _PgCursor(cx.cursor())
    else:
        cx = pyodbc.connect(conn_info, autocommit=False)
        cur = cx.cursor()
    try:
        # replace the universe (Units first — FK to Properties; sim FKs are empty in a fresh DB)
        cur.execute("DELETE FROM reference.Units")
        cur.execute("DELETE FROM reference.Properties")

        cur.execute("SET IDENTITY_INSERT reference.Properties ON")
        b_state = manifest["region"].get("locale", {}).get("state")
        cur.executemany(
            "INSERT INTO reference.Properties "
            "(PropertyID, Source, PropertyAddress, State, Town, PropertyStyle, YearBuilt, TotalUnits, BasePrice) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(int(r["property_id"]), "PublicRecords", r.get("address") or None,
              (r.get("state") or b_state or None), (r.get("town") or None), r.get("property_style") or None,
              (int(r["year_built"]) if r["year_built"] else None),
              (int(r["total_units"]) if r.get("total_units") else None),
              float(r["base_price"])) for r in buildings])
        cur.execute("SET IDENTITY_INSERT reference.Properties OFF")

        # Engine keys are ours: use a provided integer unit_id, else generate one. UnitNumber now
        # holds the adopter's free-form LABEL (nvarchar) directly — the (PropertyID, UnitNumber)
        # compliance join key. property_id stays the adopter-supplied building join key.
        next_uid = max([int(r["unit_id"]) for r in units if (r.get("unit_id") or "").strip()], default=0) + 1
        u_rows = []
        for r in units:
            if (r.get("unit_id") or "").strip():
                uid = int(r["unit_id"])
            else:
                uid = next_uid
                next_uid += 1
            u_rows.append((uid, int(r["property_id"]), (r.get("unit_number") or "").strip(), int(r["beds"]),
                           (float(r["baths"]) if r.get("baths") else 1.0),
                           (float(r["base_rent"]) if r.get("base_rent") else float(r["adjusted_rent"])),
                           float(r["adjusted_rent"])))
        cur.execute("SET IDENTITY_INSERT reference.Units ON")
        cur.executemany(
            "INSERT INTO reference.Units "
            "(UnitID, PropertyID, UnitNumber, Beds, Baths, BaseRent, AdjustedRent) VALUES (?,?,?,?,?,?,?)",
            u_rows)
        cur.execute("SET IDENTITY_INSERT reference.Units OFF")

        # region parameters -> ParameterRegistryDefault (overwrite in place)
        applied, unknown = 0, []
        for key, spec in manifest["parameters"].items():
            if "." not in key:
                continue
            cat, name = key.split(".", 1)
            val = spec["value"] if isinstance(spec, dict) else spec
            sval = str(int(val)) if isinstance(val, float) and val.is_integer() else str(val)
            cur.execute("UPDATE reference.ParameterRegistryDefault SET Value=? "
                        "WHERE Category=? AND Name=?", sval, cat, name)
            if cur.rowcount == 0:
                unknown.append(key)
            else:
                applied += 1

        # bucket-2: fill any national default the bundle did NOT supply (+ warn), so no
        # shipped (Salem) value ever survives silently for an omitted region parameter
        national_used = []
        present = set(manifest["parameters"].keys())
        for name, spec in bucket2_defaults(defaults).items():
            if name in present:
                continue
            cat, pname = name.split(".", 1)
            val = spec["value"]
            sval = str(int(val)) if isinstance(val, float) and val.is_integer() else str(val)
            cur.execute("UPDATE reference.ParameterRegistryDefault SET Value=? "
                        "WHERE Category=? AND Name=?", sval, cat, pname)
            if cur.rowcount:
                national_used.append((name, spec.get("tier", "?")))

        # provenance (DDL is the one structurally per-engine statement here)
        if pg:
            cur.execute("""CREATE TABLE IF NOT EXISTS dbo._region_provenance
                           (RegionName varchar(200), Provenance varchar(1000),
                            Buildings int, Units int, Params int, ImportedAtUtc timestamp)""")
        else:
            cur.execute("""IF OBJECT_ID('dbo._region_provenance') IS NULL
                           CREATE TABLE dbo._region_provenance
                           (RegionName nvarchar(200), Provenance nvarchar(1000),
                            Buildings int, Units int, Params int, ImportedAtUtc datetime2)""")
        cur.execute("INSERT INTO dbo._region_provenance VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                    manifest["region"].get("name", bundle_name),
                    manifest["region"].get("provenance", "")[:1000],
                    len(buildings), len(units), applied)

        # PG: explicit-ID loads leave identity sequences behind — position them
        # past the loaded max so any future insert can't collide
        if pg:
            for tbl, col in (("reference.Properties", "PropertyID"),
                             ("reference.Units", "UnitID")):
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{tbl.lower()}', '{col.lower()}'), "
                    f"(SELECT COALESCE(MAX({col}), 0) + 1 FROM {tbl}), false)")
        cx.commit()
        return applied, unknown, national_used
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


def main():
    ap = argparse.ArgumentParser(description="Load a Liberty Bee Region bundle into a fresh DB.")
    ap.add_argument("--bundle", required=True, help="path to the region bundle folder")
    ap.add_argument("--label", default="region", help="ephemeral-DB label (default: region)")
    ap.add_argument("--validate-only", action="store_true", help="validate the bundle and stop")
    ap.add_argument("--pg", action="store_true",
                    help="deprecated no-op: PostgreSQL is the default")
    ap.add_argument("--mssql", action="store_true",
                    help="load into a SQL Server env (pre-cutover legacy path)")
    args = ap.parse_args()
    use_pg = not args.mssql

    defaults = load_defaults()
    print(f"Validating bundle: {args.bundle}")
    try:
        buildings, units, manifest = validate_bundle(args.bundle, required_params(defaults))
    except BundleError as e:
        print(f"FAIL — {e}")
        sys.exit(2)
    print(f"OK — {len(buildings)} buildings, {len(units)} units, {len(manifest['parameters'])} params")
    if args.validate_only:
        return

    db, conn = create_base_db(args.label, pg=use_pg)
    print(f"Loading region into {db} …")
    applied, unknown, national_used = load_region(
        conn, buildings, units, manifest, os.path.basename(args.bundle.rstrip("/\\")), defaults,
        pg=use_pg)
    print(f"OK — universe replaced ({len(buildings)}/{len(units)}); {applied} parameters applied")
    if national_used:
        print(f"  WARNING: {len(national_used)} region parameter(s) absent from the bundle were filled")
        print(f"  with GENERIC NATIONAL DEFAULTS (see regiondata/defaults.json / regiondata/README.md; replace with local):")
        for nm, tier in national_used[:20]:
            print(f"    - {nm}  [{tier}]")
        if len(national_used) > 20:
            print(f"    (+{len(national_used) - 20} more)")
    if unknown:
        print(f"  note: {len(unknown)} manifest params not found in the registry (skipped): {unknown[:8]}")
    print(f"\nRegion loaded into: {db}")
    print(f"Round-trip gate: python app/src/master_test_runner.py --env {db} --regression")


if __name__ == "__main__":
    main()
