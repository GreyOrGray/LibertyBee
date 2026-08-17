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

import corpus_conn

REPO = os.path.dirname(os.path.abspath(__file__))   # this script ships at the repo root
def conn(db):
    return corpus_conn.connect(db, autocommit=True)

def fresh_env(label):
    cmd = [sys.executable, "environmentscripts/migration_manager.py", "--label", label]
    if corpus_conn.is_pg():
        cmd.insert(2, "--pg")  # template-minted PG env (the D5 worker path)
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=900)
    m = re.search(r"Test environment ready:\s*(\S+)", p.stdout)
    return m.group(1) if m else None

def seed_has_projection(db, proj):
    c = conn(db)
    row = c.cursor().execute("SELECT 1 FROM reference.Projection WHERE ProjectionID = ?", proj).fetchone()
    c.close()
    return row is not None

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
    dm = cc.fetchone()[0]
    if dm is None or dm <= 0:
        # KD-046: a cell whose only payment-status rows are the -1 sentinels (a run
        # can die before any realized payment month). Fall back to the ledger span,
        # the same measure the fast-death checks use.
        cc.execute(f"SELECT {corpus_conn.month_diff_sql('MIN(LedgerDate)', 'MAX(LedgerDate)')} + 1 "
                   f"FROM v1.fund_ledger WHERE Rung=? AND Seed=?", r[2], seed)
        dm = cc.fetchone()[0]
    return ("halted", str(dm))

def provenance_check(cc, strict=False):
    """Can this corpus be handed to someone else and rebuilt? Returns a failure list.

    Reproducing sampled cells proves the NUMBERS are right. It does not prove the
    corpus is self-describing, and a corpus that cannot say what produced it is
    not a reproducible artifact no matter how well its cells re-run. Three checks:

      1. v1.projection_parameters covers every rung present. This makes the corpus
         self-describing: a corpus outlives any particular seed database, so every
         run's exact parameter set must be readable from the corpus alone.
         This shipped broken once; it is a gate now, not a note.
      2. Exactly one scenario. A corpus blending two affordability populations
         looks completely normal and is silently wrong.
      3. No dirty-tree generation. HarnessDirty=1 means the corpus came from a
         modified working tree and matches no published commit.
    """
    failures = []

    cc.execute("SELECT DISTINCT ProjectionID FROM v1.run_summary")
    rungs_present = {r[0] for r in cc.fetchall()}
    try:
        cc.execute("SELECT DISTINCT ProjectionID FROM v1.projection_parameters")
        rungs_documented = {r[0] for r in cc.fetchall()}
    except corpus_conn.db_errors():
        rungs_documented = set()
    undocumented = sorted(rungs_present - rungs_documented)
    if undocumented:
        failures.append(
            f"v1.projection_parameters is missing {len(undocumented)} of "
            f"{len(rungs_present)} rungs present in the corpus: {undocumented}. "
            f"Runs on those rungs cannot be reproduced from the published bundle.")
    else:
        print(f"  provenance: projection_parameters covers all {len(rungs_present)} rungs")

    try:
        cc.execute("SELECT Scenario, HarnessCommit, HarnessDirty FROM v1.corpus_meta")
        meta = cc.fetchall()
    except corpus_conn.db_errors():
        meta = None

    if not meta:
        msg = ("v1.corpus_meta is absent or empty — the corpus does not record what "
               "generated it (pre-dates provenance stamping).")
        if strict:
            failures.append(msg)
        else:
            print(f"  provenance: WARNING — {msg}")
        return failures

    scenarios = sorted({m[0] for m in meta})
    if len(scenarios) > 1:
        failures.append(f"corpus mixes {len(scenarios)} scenarios {scenarios} — "
                        f"a corpus must hold exactly one.")
    if any(m[2] for m in meta):
        failures.append("corpus was generated from a DIRTY working tree "
                        "(HarnessDirty=1) — it matches no published commit.")
    commits = sorted({(m[1] or "unknown")[:12] for m in meta})
    if not failures:
        print(f"  provenance: scenario={scenarios[0]}  harness={','.join(commits)}  clean")
    return failures


def parse_cells(spec, cc):
    if spec == "edges":   # lowest + highest seed actually present, per projection
        cc.execute("SELECT ProjectionID, MIN(Seed), MAX(Seed) FROM v1.run_summary "
                   "GROUP BY ProjectionID ORDER BY ProjectionID")
        out = []
        for p, lo, hi in cc.fetchall():
            out.append((p, lo))
            if hi != lo:
                out.append((p, hi))
        return out
    out = []
    for tok in spec.split(","):
        p, s = tok.split(":")
        out.append((int(p), int(s)))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--sample", default="edges", help="'edges' (min+max seed per proj) or use --cells")
    ap.add_argument("--cells", default=None, help="explicit 'proj:seed,proj:seed'")
    ap.add_argument("--strict-provenance", action="store_true",
                    help="treat a missing v1.corpus_meta as failure (use for any corpus "
                         "generated after provenance stamping existed)")
    ap.add_argument("--pg", action="store_true",
                    help="the corpus (and the rebuild envs) live on PostgreSQL")
    ap.add_argument("--provenance-only", action="store_true",
                    help="run only the provenance checks and skip cell re-runs (fast)")
    args = ap.parse_args()
    if args.pg:
        corpus_conn.set_backend("psycopg")

    cc = conn(args.corpus).cursor()
    print(f"Reproduction gate: corpus={args.corpus} [{corpus_conn.backend()}]")
    prov_failures = provenance_check(cc, strict=args.strict_provenance)
    if prov_failures:
        print("\nGATE FAILED (provenance):")
        for f in prov_failures:
            print(f"  - {f}")
        sys.exit(1)
    if args.provenance_only:
        print("\nGATE PASSED: provenance only (cell re-runs skipped).")
        sys.exit(0)

    cells = parse_cells(args.cells, cc) if args.cells else parse_cells(args.sample, cc)
    print(f"  cells={len(cells)}")
    print(f"{'proj/seed':>10} {'kind':>9} {'corpus':>16} {'HEAD':>16}  result")

    failures = []
    for proj, seed in cells:
        exp = corpus_expect(cc, proj, seed)
        if exp is None:
            print(f"{proj}/{seed:<4} {'MISSING':>9} — cell not in corpus"); failures.append((proj, seed, "missing")); continue
        db = fresh_env(f"gate{proj}_{seed}")
        if db and not seed_has_projection(db, proj):
            print(f"{proj}/{seed:<4} {'NO-PROJ':>9} — projection not in the seed database; "
                  f"this corpus needs the seed that defined it")
            failures.append((proj, seed, "projection not in seed database")); continue
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
