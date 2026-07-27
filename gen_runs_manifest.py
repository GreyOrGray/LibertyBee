"""gen_runs_manifest — regenerate a runs manifest from a corpus database.

The manifest is a release artifact: one row per run with its settings, outcome, and the exact
command that reproduces it. It is always generated FROM the corpus database (the source of
truth), never edited by hand — the 1.0 manifest shipped with stale rows precisely because its
generator was a throwaway script; this tool is the mechanism that replaces that mistake.

One corpus, one manifest (a corpus holds exactly one scenario):
  python gen_runs_manifest.py --corpus LibertyBee_V04R1_Standard --out runs_manifest.csv
  python gen_runs_manifest.py --corpus LibertyBee_V04R1_DeepDiscount25 --out runs_manifest_deepdiscount25.csv

Servers default to LB_SQL_SERVER, then localhost.
"""
import argparse
import csv
import os

import pyodbc

DRV = os.environ.get("LB_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
SERVER = os.environ.get("LB_SQL_SERVER", "localhost")

HEADER = ["Rung_StartingCapital", "ProjectionID", "Seed", "Months", "EngineVersion",
          "ProjectionInGold", "Survived", "FinalCash", "FinalTotal", "EvictionCount", "Command"]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--corpus", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--server", default=SERVER)
    args = p.parse_args()

    c = pyodbc.connect("DRIVER={" + DRV + "};SERVER=" + args.server + ";DATABASE="
                       + args.corpus + ";Trusted_Connection=yes").cursor()
    rows = c.execute(
        "SELECT Rung, ProjectionID, Seed, EngineVersion, Survived, FinalCash, FinalTotal, "
        "EvictionCount FROM v1.run_summary ORDER BY ProjectionID, Seed").fetchall()

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for rung, pid, seed, ev, survived, cash, total, evictions in rows:
            w.writerow([
                int(rung), pid, seed, 240, ev,
                "yes",   # every swept projection is seeded data since V00072
                1 if survived else 0,
                f"{cash:.2f}", f"{total:.2f}", evictions,
                f"python app/src/simulation.py --env <db> --projection-id {pid} "
                f"--seed {seed} --months 240",
            ])
    print(f"{args.out}: {len(rows)} rows from {args.server}/{args.corpus}")


if __name__ == "__main__":
    main()
