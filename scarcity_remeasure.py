"""scarcity_remeasure.py — recompute the retention scarcity-share decomposition on clean HEAD.

Runs projection 206 across 3 retention configs x 8 seeds, EACH IN ITS OWN fresh-from-Gold database
(so the run-isolation leak cannot contaminate it — unlike the original single-DB measurement).
Composed annual turnover per config; scarcity share = (deal_only - full) / (base - full).

  python scarcity_remeasure.py            # 8 seeds (default)
  python scarcity_remeasure.py --seeds 4  # quick 4-seed pass
"""
import argparse, os, subprocess, re, sys, pyodbc
from dateutil.relativedelta import relativedelta

REPO = os.path.dirname(os.path.abspath(__file__))   # ships at the repo root
DRV  = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
SERVER = os.environ.get("LB_SQL_SERVER", "localhost")
OUT = None   # optional report file, set via --out
CONFIGS = {"base": ("0", "0"), "deal_only": ("1", "0"), "full": ("1", "0.5")}

def log(s):
    if OUT:
        with open(OUT, "a") as f: f.write(s + "\n")
    print(s, flush=True)
def conn(db):
    return pyodbc.connect(f"DRIVER={{{DRV}}};SERVER={SERVER};DATABASE={db};Trusted_Connection=yes", autocommit=True)
def fresh_env(label):
    p = subprocess.run([sys.executable, "environmentscripts/migration_manager.py", "--label", label],
                       cwd=REPO, capture_output=True, text=True, timeout=900)
    m = re.search(r"Test environment ready:\s*(\S+)", p.stdout); return m.group(1) if m else None
def set_bg(db, beta, gamma):
    c = conn(db).cursor()
    c.execute("UPDATE reference.ParameterRegistry SET Value=? WHERE Category='RET' AND Name='DiscountSensitivityBeta' AND ProjectionID IS NULL", beta)
    c.execute("UPDATE reference.ParameterRegistry SET Value=? WHERE Category='RET' AND Name='ScarcitySensitivityGamma' AND ProjectionID IS NULL", gamma)
def run_sim(db, seed):
    subprocess.run([sys.executable, "app/src/simulation.py", "--env", db, "--projection-id", "206",
                    "--seed", str(seed), "--months", "240"], cwd=REPO, capture_output=True, text=True, timeout=900)
def turnover(db):
    c = conn(db).cursor()
    row = c.execute("SELECT TOP 1 RunID, CAST(StartDate AS DATE) FROM simulation.Run ORDER BY RunID").fetchone()
    if not row: return (0, 0.0)
    rid, start = row; horizon = start + relativedelta(months=240)
    tt = 0; ty = 0.0
    for s, t in c.execute("""SELECT CAST(l.LeaseStartDate AS DATE), CAST(x.TerminationDate AS DATE)
        FROM simulation.Lease l LEFT JOIN simulation.LeaseTermination x ON x.RunID=l.RunID AND x.LeaseID=l.LeaseID
        WHERE l.RunID=?""", (rid,)).fetchall():
        end = min(t, horizon) if t else horizon
        m = max((end.year - s.year) * 12 + (end.month - s.month), 0); ty += m / 12.0
        if t is not None: tt += 1
    return (tt, ty)

def main():
    global OUT
    ap = argparse.ArgumentParser(); ap.add_argument("--seeds", type=int, default=8); ap.add_argument("--out", default=None); a = ap.parse_args()
    OUT = a.out
    if OUT: open(OUT, "w").close()
    log("=== SCARCITY re-measure, clean HEAD, FRESH DB PER RUN ===")
    log(f"proj 206, {a.seeds} seeds x 3 configs; scarcity share = (deal_only - full)/(base - full)  [site: ~46%]")
    res = {}
    for cfg, (b, g) in CONFIGS.items():
        tt = 0; ty = 0.0
        for seed in range(1, a.seeds + 1):
            db = fresh_env(f"scar_{cfg}_{seed}")
            if not db:
                log(f"  !! env build failed for {cfg}/{seed}"); continue
            set_bg(db, b, g); run_sim(db, seed)
            t, y = turnover(db); tt += t; ty += y
        res[cfg] = 100 * tt / ty if ty else 0.0
        log(f"  {cfg:10s} (beta={b} gamma={g}): turnover {res[cfg]:.2f}%  ({tt} term / {ty:.0f} tenant-yrs)")
    base, deal, full = res["base"], res["deal_only"], res["full"]
    log("")
    log(f"  deal contribution     (base -> deal_only): {base - deal:.2f}pp")
    log(f"  scarcity contribution (deal_only -> full): {deal - full:.2f}pp")
    if base - full:
        log(f"  SCARCITY SHARE = {100 * (deal - full) / (base - full):.1f}%   [site: ~46%]")
    log("=== DONE ===")

if __name__ == "__main__":
    main()
