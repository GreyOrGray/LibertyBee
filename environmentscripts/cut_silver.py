"""Cut a Silver baseline — a candidate replacement for Gold.

WHY THIS EXISTS

Gold has always been hand-cut. `workflow_current.md` describes the re-baseline as
prose ("build a clean env, strip simulation.* run data, snapshot it") and someone
followed it. That is the same untracked-procedure problem that made the V2 corpus
unpublishable, one level up: the seed database every run depends on was produced
by steps that existed only in a doc and someone's memory.

This makes the cut a tracked, verifiable, repeatable artifact-producing step.

WHAT SILVER IS

    Gold + the full migration chain, with simulation run data stripped.

It is a RELEASE CANDIDATE for Gold, not Gold itself. The lifecycle Gray set out:

    dev  = Gold + migrations + validation
    cut  -> Silver
    Silver = what sweeps restore from
    Silver validated -> Silver becomes the new Gold

Cutting Silver before a sweep means the sweep's input is one immutable, checksummed
file rather than "Gold plus a chain someone remembered to apply". An artifact can be
verified by hash; a procedure can only be verified by reading it and trusting it ran.

USAGE

    python environmentscripts/cut_silver.py --label v0-4-rc1
    python environmentscripts/cut_silver.py --label v0-4-rc1 --force   # overwrite

Writes <out-dir>/LibertyBeeSilver_<label>.bak plus a .manifest.json recording the
commit, the applied migration chain, row counts and the backup's SHA-256 — so the
question "what is in this Silver?" is answerable from tracked inputs alone.
"""

import argparse
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys

import pyodbc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_MANAGER = os.path.join(REPO, "environmentscripts", "migration_manager.py")
MIGRATIONS_DIR = os.path.join(REPO, "sql", "migrations")

SQL_SERVER = os.environ.get("LB_SQL_SERVER", "localhost")
SQL_DRIVER = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
CONN_TMPL = ("DRIVER={{" + SQL_DRIVER + "}};SERVER=" + SQL_SERVER
             + ";Trusted_Connection=yes;DATABASE={}")

# Default beside Gold; the SQL Server service account must be able to WRITE here,
# which is a stricter requirement than the read access a restore needs.
DEFAULT_OUT = os.environ.get(
    "LB_SILVER_BACKUP_DIR", os.path.join(REPO, "DBBackup", "silver"))

BUILD_DB = "LibertyBee_Test_SilverCut"


def conn(db):
    return pyodbc.connect(CONN_TMPL.format(db), timeout=60, autocommit=True)


def git(*args):
    try:
        p = subprocess.run(["git", "-C", REPO, *args],
                           capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def preflight(allow_dirty):
    """A baseline generated from a modified tree cannot be tied to any published
    commit, so refuse by default — the same contract the corpus harness uses."""
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain")) if commit else False
    if dirty and not allow_dirty:
        sys.exit("REFUSING: the working tree has uncommitted changes.\n"
                 "          A Silver cut from a modified tree cannot be reproduced from\n"
                 "          any published commit. Commit first, or pass --allow-dirty for\n"
                 "          a throwaway cut (recorded as dirty in the manifest).")
    return commit, dirty


def build_env():
    print(f"building {BUILD_DB} from Gold + the full migration chain ...", flush=True)
    p = subprocess.run([sys.executable, MIGRATION_MANAGER, "--envname", BUILD_DB],
                       capture_output=True, text=True, cwd=REPO, timeout=3600)
    if p.returncode != 0:
        sys.exit("build failed:\n" + ((p.stdout or "") + (p.stderr or ""))[-2000:])
    print("  built", flush=True)


def applied_migrations():
    """Every migration on disk must be recorded as applied. A gap here means the
    Silver would carry a schema the migration chain does not describe.

    dbo.SchemaVersion is keyed by Version ('V00070'), which is the filename prefix
    before the '__'. Checked in one direction only — disk implies applied. The
    reverse does not hold and must not be asserted: Gold has migrations folded into
    the baseline whose files no longer exist on disk, and flagging those would fail
    every cut.
    """
    on_disk = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    by_version = {f.split("__", 1)[0]: f for f in on_disk}
    with conn(BUILD_DB) as c:
        applied = {r[0] for r in c.cursor().execute(
            "SELECT Version FROM dbo.SchemaVersion").fetchall()}
    missing = [f for v, f in sorted(by_version.items()) if v not in applied]
    return on_disk, sorted(applied), missing


def strip_run_data():
    """Strip simulation run data, keeping schema and reference data.

    Uses the engine's own sp_CleanSimulationEngine rather than a hand-written
    DELETE list: the procedure knows the full table set and its FK order, and a
    hand-written list would silently rot as tables are added.
    """
    print("stripping simulation run data ...", flush=True)
    with conn(BUILD_DB) as c:
        c.cursor().execute("EXEC [dbo].[sp_CleanSimulationEngine] @Type = NULL")
    print("  stripped", flush=True)


def verify_stripped():
    """Prove both halves: run data gone AND reference data intact. Checking only
    the first would happily bless a Silver that had been emptied of the reference
    rows every run depends on."""
    with conn(BUILD_DB) as c:
        cur = c.cursor()
        sim_rows = cur.execute("""
            SELECT SUM(p.rows) FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
            WHERE s.name = 'simulation'""").fetchone()[0] or 0
        ref_rows = cur.execute("""
            SELECT SUM(p.rows) FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
            WHERE s.name = 'reference'""").fetchone()[0] or 0
        projections = cur.execute(
            "SELECT COUNT(*) FROM reference.Projection").fetchone()[0]
        defaults = cur.execute(
            "SELECT COUNT(*) FROM reference.ParameterRegistryDefault").fetchone()[0]
        undescribed = cur.execute(
            "SELECT COUNT(*) FROM reference.ParameterRegistryDefault "
            "WHERE Description IS NULL OR LEN(LTRIM(RTRIM(Description))) < 30").fetchone()[0]
    return {"simulation_rows": int(sim_rows), "reference_rows": int(ref_rows),
            "projections": projections, "default_parameters": defaults,
            "undescribed_parameters": undescribed}


def backup(out_path):
    """Back up the built database, and DRAIN the statement to completion.

    BACKUP emits its progress as a sequence of result sets. pyodbc returns from
    execute() as soon as the first is available, so closing the connection there
    aborts the backup mid-flight — and raises nothing, leaving no file and no
    msdb.backupset row. It looks like it worked. Draining with nextset() is what
    actually waits for the backup to finish, and surfaces any server-side error.
    """
    print(f"backing up -> {out_path}", flush=True)
    c = conn("master")
    try:
        cur = c.cursor()
        cur.execute(f"""
            BACKUP DATABASE [{BUILD_DB}] TO DISK = ?
            WITH FORMAT, INIT, COMPRESSION, NAME = 'LibertyBee Silver baseline'
        """, (out_path,))
        while cur.nextset():
            pass
    finally:
        c.close()

    # Trust the server's own record, not the absence of an exception.
    with conn("msdb") as m:
        row = m.cursor().execute("""
            SELECT TOP 1 bs.backup_size, bmf.physical_device_name
            FROM msdb.dbo.backupset bs
            JOIN msdb.dbo.backupmediafamily bmf ON bmf.media_set_id = bs.media_set_id
            WHERE bs.database_name = ? ORDER BY bs.backup_finish_date DESC
        """, (BUILD_DB,)).fetchone()
    if row is None:
        sys.exit(f"BACKUP reported no error but msdb has no record of it.\n"
                 f"         Check that the SQL Server service account can WRITE to\n"
                 f"         {os.path.dirname(out_path)} — a restore only needs read access,\n"
                 f"         so a directory that works for Gold may still fail here.")
    if not os.path.exists(out_path):
        sys.exit(f"BACKUP recorded to {row[1]} but {out_path} is not visible from here.")
    print(f"  backed up ({int(row[0]):,} bytes uncompressed)", flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True,
                    help="version label, e.g. v0-4-rc1 (used in the filename)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--force", action="store_true", help="overwrite an existing cut")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="permit cutting from a modified tree (marked in the manifest)")
    ap.add_argument("--keep-db", action="store_true",
                    help="keep the build database for inspection")
    args = ap.parse_args()

    commit, dirty = preflight(args.allow_dirty)
    os.makedirs(args.out_dir, exist_ok=True)
    bak = os.path.join(args.out_dir, f"LibertyBeeSilver_{args.label}.bak")
    manifest_path = bak + ".manifest.json"
    if os.path.exists(bak) and not args.force:
        sys.exit(f"REFUSING: {bak} already exists. Pass --force to overwrite.")

    print(f"Silver cut: label={args.label}")
    print(f"  commit: {commit[:12] if commit else 'unknown'}"
          f"{'  *** DIRTY TREE ***' if dirty else ''}")
    print(f"  server: {SQL_SERVER}\n")

    build_env()

    on_disk, applied, missing = applied_migrations()
    if missing:
        sys.exit(f"REFUSING: {len(missing)} migration(s) on disk are not recorded as "
                 f"applied: {missing[:5]}\n          The Silver would carry a schema the "
                 f"migration chain does not describe.")
    print(f"migrations: {len(on_disk)} on disk, all recorded as applied")

    strip_run_data()
    checks = verify_stripped()
    print("\nverification:")
    for k, v in checks.items():
        print(f"  {k:<24} {v}")
    if checks["simulation_rows"] != 0:
        sys.exit("REFUSING: simulation run data survived the strip.")
    if checks["reference_rows"] == 0 or checks["projections"] == 0:
        sys.exit("REFUSING: reference data is empty — the strip took too much.")
    if checks["undescribed_parameters"] != 0:
        sys.exit(f"REFUSING: {checks['undescribed_parameters']} default parameter(s) "
                 f"lack a usable description.")

    backup(bak)
    digest = sha256(bak)

    manifest = {
        "label": args.label,
        "created_utc": datetime.datetime.utcnow().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "sql_server": SQL_SERVER,
        "harness_commit": commit,
        "harness_dirty": dirty,
        "source": "Gold + full migration chain, simulation run data stripped",
        "migrations_applied": on_disk,
        "migration_count": len(on_disk),
        "checks": checks,
        "backup_file": os.path.basename(bak),
        "backup_sha256": digest,
        "backup_bytes": os.path.getsize(bak),
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"\nsha256: {digest}")
    print(f"manifest: {manifest_path}")

    if not args.keep_db:
        subprocess.run([sys.executable, MIGRATION_MANAGER, "--drop", BUILD_DB, "--yes"],
                       capture_output=True, text=True, cwd=REPO)
        print(f"dropped {BUILD_DB}")

    print("\nSILVER CUT COMPLETE.")
    print("  This is a CANDIDATE, not Gold. Validate it (restore it, run the golden")
    print("  seeds, confirm they reproduce) before promoting it to Gold.")
    sys.exit(0)


if __name__ == "__main__":
    main()
