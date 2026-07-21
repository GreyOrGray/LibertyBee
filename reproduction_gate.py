"""Reproduction gate — re-run a sample of a frozen corpus from HEAD and confirm each cell reproduces.

For each (projection, seed) cell it builds a fresh-from-Gold database, runs the committed engine, and
compares against the corpus:
  * survivors  -> Final total funds must match to the penny
  * halted orgs -> death-month (MAX MonthIndex) must match exactly

Exit code 0 if every sampled cell reproduces; 1 if any drifts. This is the check whose ABSENCE let a
mid-calibration corpus (V03R4 projection 206) reach the release doorstep. Run it before any corpus is
frozen, cited, or shipped.

Usage:
  python reproduction_gate.py --corpus <your_restored_corpus_db> --sample edges
  python reproduction_gate.py --corpus <your_restored_corpus_db> --cells 206:1,206:50,203:1
"""
import argparse, os, re, subprocess, sys
import pyodbc

REPO = os.path.dirname(os.path.abspath(__file__))   # this script ships at the repo root
DRV  = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
SERVER = os.environ.get("LB_SQL_SERVER", "localhost")
RUNGS_SQL = os.path.join(REPO, "reproduce_rungs.sql")   # synthetic-rung setup (300-305)
STORED = set(range(200, 210))

def conn(db):
    return pyodbc.connect(f"DRIVER={{{DRV}}};SERVER={SERVER};DATABASE={db};Trusted_Connection=yes")

def fresh_env(label):
    p = subprocess.run([sys.executable, "environmentscripts/migration_manager.py", "--label", label],
                       cwd=REPO, capture_output=True, text=True, timeout=900)
    m = re.search(r"Test environment ready:\s*(\S+)", p.stdout)
    return m.group(1) if m else None

def apply_rungs(db):
    c = conn(db); c.autocommit = True
    c.cursor().execute(open(RUNGS_SQL, encoding="utf-8").read()); c.close()

def head_run(db, proj, seed):
    p = subprocess.run([sys.executable, "app/src/simulation.py", "--env", db,
                        "--projection-id", str(proj), "--seed", str(seed), "--months", "240"],
                       cwd=REPO, capture_output=True, text=True, timeout=900)
    out = p.stdout + p.stderr
    ft = re.findall(r"Final total funds:\s*\$([0-9,]+\.\d+)", out)
    hm = re.findall(r"HALTED month (\d+)", out)
    return (ft[-1].replace(",", "") if ft else None, hm[-1] if hm else None)

def corpus_expect(cc, proj, seed):
    # run_summary carries both ProjectionID and Rung; monthly_payment_status is keyed by Rung.
    cc.execute("SELECT Survived, FinalTotal, Rung FROM v1.run_summary WHERE ProjectionID=? AND Seed=?", proj, seed)
    r = cc.fetchone()
    if not r:
        return None
    if r[0]:
        return ("survived", f"{r[1]:.2f}")
    cc.execute("SELECT MAX(MonthIndex) FROM v1.monthly_payment_status WHERE Rung=? AND Seed=?", r[2], seed)
    return ("halted", str(cc.fetchone()[0]))

def parse_cells(spec, cc):
    if spec == "edges":   # seed 1 + seed 50 for every projection present
        cc.execute("SELECT DISTINCT ProjectionID FROM v1.run_summary ORDER BY ProjectionID")
        projs = [row[0] for row in cc.fetchall()]
        return [(p, s) for p in projs for s in (1, 50)]
    out = []
    for tok in spec.split(","):
        p, s = tok.split(":")
        out.append((int(p), int(s)))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--sample", default="edges", help="'edges' (seed 1+50 per proj) or use --cells")
    ap.add_argument("--cells", default=None, help="explicit 'proj:seed,proj:seed'")
    args = ap.parse_args()

    cc = conn(args.corpus).cursor()
    cells = parse_cells(args.cells, cc) if args.cells else parse_cells(args.sample, cc)
    print(f"Reproduction gate: corpus={args.corpus}  cells={len(cells)}")
    print(f"{'proj/seed':>10} {'kind':>9} {'corpus':>16} {'HEAD':>16}  result")

    failures = []
    for proj, seed in cells:
        exp = corpus_expect(cc, proj, seed)
        if exp is None:
            print(f"{proj}/{seed:<4} {'MISSING':>9} — cell not in corpus"); failures.append((proj, seed, "missing")); continue
        db = fresh_env(f"gate{proj}_{seed}")
        if db and proj not in STORED:
            apply_rungs(db)
        ft, hm = head_run(db, proj, seed) if db else (None, None)
        kind, val = exp
        if kind == "survived":
            got, ok = (ft or "ERR"), (ft == val)
        else:
            got, ok = (f"death@{hm}"), (str(hm) == val)
            val = f"death@{val}"
        print(f"{proj}/{seed:<4} {kind:>9} {val:>16} {got:>16}  {'ok' if ok else 'DRIFT'}")
        if not ok:
            failures.append((proj, seed, f"corpus={val} head={got}"))

    print()
    if failures:
        print(f"GATE FAILED: {len(failures)}/{len(cells)} cells did not reproduce -> {failures}")
        sys.exit(1)
    print(f"GATE PASSED: all {len(cells)} sampled cells reproduce from HEAD.")
    sys.exit(0)

if __name__ == "__main__":
    main()
