"""KD-193 validation census — run the mid-rungs ($5.0M-$6.5M, where the
blind-commit lead deaths appeared) on the FIXED engine and classify every
death: withdrawal-driven avoidance vs any REMAINING blind-commit-to-lead death
(the class Kate's contract requires be gone). Also reports mission throughput
(acquisitions) so a too-aggressive withdrawal shrinking the mission would show.

Runs from the KD-193 clone (this file's tree) so it exercises V00068 + the
KD-193 engine. Parallel, fresh env per run.
"""
import concurrent.futures as cf
import subprocess, sys, os, re
from pathlib import Path
import pyodbc

ROOT = Path(__file__).resolve().parents[3]           # sql/regression_tests/KD193 → clone root
SIM = ROOT / "app" / "src" / "simulation.py"
MM = ROOT / "environmentscripts" / "migration_manager.py"
os.environ["LB_SQL_SERVER"] = r"localhost\LBSANDBOX_EX01"
os.environ["LB_GOLD_BACKUP_DIR"] = r"C:\LibertyBee\DBBackup\gold"
SRV = r"localhost\LBSANDBOX_EX01"
CONN = "DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={};Trusted_Connection=yes;DATABASE={}"

RUNGS = {200: "5.0M", 201: "5.5M", 202: "6.0M", 203: "6.5M"}
SEEDS = list(range(1, 26))   # 25 seeds/rung × 4 = 100 runs
WORKERS = 8


def one(proj, seed):
    env = f"LibertyBee_Test_KD193c_{proj}_{seed}"
    r = subprocess.run([sys.executable, str(MM), "--envname", env],
                       capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        return (proj, seed, "ENVFAIL", None)
    log = subprocess.run([sys.executable, str(SIM), "--env", env,
                          "--projection-id", str(proj), "--seed", str(seed), "--months", "240"],
                         capture_output=True, text=True, cwd=str(ROOT))
    wds = len(re.findall(r"Lead carryability", log.stdout))
    c = pyodbc.connect(CONN.format(SRV, env), autocommit=True, timeout=30)
    cur = c.cursor()
    bal = cur.execute("SELECT TOP 1 CashBalance, CSFBalance FROM simulation.FundLedger "
                      "WHERE RunID=1 ORDER BY LedgerDate DESC, EventID DESC").fetchone()
    span = cur.execute("SELECT DATEDIFF(MONTH, MIN(LedgerDate), MAX(LedgerDate)) FROM simulation.FundLedger WHERE RunID=1").fetchone()[0]
    props = cur.execute("SELECT COUNT(*) FROM simulation.Properties WHERE RunID=1").fetchone()[0]
    ev = cur.execute("SELECT COUNT(*) FROM simulation.LeaseTermination WHERE RunID=1 AND TerminationType='EVICTION'").fetchone()[0]
    lead = cur.execute("SELECT COALESCE(SUM(CostEstimate),0) FROM simulation.ComplianceWorkItem WHERE RunID=1 AND WorkType='LEAD_ABATEMENT'").fetchone()[0]
    c.close()
    cash, csf = float(bal[0]), float(bal[1])
    survived = (cash + csf) > 0 and (span or 0) >= 239
    # drop the env to keep the sandbox clean
    subprocess.run([sys.executable, str(MM), "--drop", env, "--yes"], capture_output=True, text=True, cwd=str(ROOT))
    return (proj, seed, "SURV" if survived else "DIED",
            {"span": span, "props": props, "ev": ev, "lead": float(lead), "wds": wds, "ff": cash + csf})


def main():
    pairs = [(p, s) for p in RUNGS for s in SEEDS]
    results = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one, p, s): (p, s) for p, s in pairs}
        for i, f in enumerate(cf.as_completed(futs), 1):
            results.append(f.result())
            if i % 20 == 0:
                print(f"  {i}/{len(pairs)} done", flush=True)

    print("\n=== KD-193 CENSUS (fixed engine, mid-rungs) ===")
    for proj in sorted(RUNGS):
        rr = [r for r in results if r[0] == proj]
        surv = sum(1 for r in rr if r[2] == "SURV")
        wds = sum(r[3]["wds"] for r in rr if r[3])
        acq = sum(r[3]["props"] for r in rr if r[3]) / max(len(rr), 1)
        print(f"  ${RUNGS[proj]}: {surv}/{len(rr)} survived, {wds} lead-withdrawals total, {acq:.1f} avg props")
    deaths = [r for r in results if r[2] == "DIED"]
    print(f"\n  deaths: {len(deaths)} — classifying (blind-commit = fast span<=12, 0 evictions, 0 withdrawals):")
    blind = 0
    for r in deaths:
        d = r[3]
        is_blind = d and (d["span"] or 999) <= 12 and d["ev"] == 0 and d["wds"] == 0
        if is_blind: blind += 1
        print(f"    ${RUNGS[r[0]]} s{r[1]}: span {d['span']}, props {d['props']}, ev {d['ev']}, "
              f"lead ${d['lead']:,.0f}, withdrawals {d['wds']}  [{'*** BLIND-COMMIT (should be gone!)' if is_blind else 'other'}]")
    print(f"\n  REMAINING BLIND-COMMIT-TO-LEAD DEATHS: {blind} (contract requires 0)")


if __name__ == "__main__":
    main()
