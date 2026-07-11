# Reproduce Liberty Bee

Every number on [libertybee.org](https://libertybee.org) comes out of this engine, and every run is
deterministic: the same `(projection, seed, engine version)` produces the identical result, to the penny.
This file shows you how to check that yourself — two ways, from least to most effort.

- **Engine version of record:** `0.5.0`
- **Frozen corpus (the 1.0 baseline of record):** `V03R4` — `Baseline` (800 runs = 16 funding rungs × 50 seeds), `Static` (100), `Cliff` (1,440)
- Both the seed database and the frozen corpus ship as **Release assets** on this repo (they're SQL Server
  backups, too big and too binary to live in git).

---

## What you need

> **Windows.** The engine runs on SQL Server + Windows trusted auth + ODBC, and the tooling uses Windows path
> conventions — so reproduction is Windows-only (use git-bash for the shell commands below).

- **SQL Server** (Express is fine) with Windows/trusted auth. The default target is `localhost`; if yours is a
  named instance (e.g. `localhost\SQLEXPRESS`), set the `LB_SQL_SERVER` env var to it.
- **Python 3.12**
- The **ODBC Driver 17 for SQL Server** (override with `LB_SQL_DRIVER` if you use 18)
- `pip install -r app/requirements.txt`

### ⚠️ First-run gotchas (the two most likely to block a first run)

- **Named SQL instance?** The default target is `localhost`. If your server is a *named* instance
  (e.g. `localhost\SQLEXPRESS`, `localhost\MYBOX`), set `LB_SQL_SERVER` before running the tools:
  ```bash
  export LB_SQL_SERVER='localhost\SQLEXPRESS'    # bash;  PowerShell:  $env:LB_SQL_SERVER='localhost\SQLEXPRESS'
  ```
  It applies to `migration_manager.py`, `simulation.py`, `reproduction_gate.py`, and
  `regenerate_corpus.py`. (`site_metrics.py` takes `--server` / `--driver` flags instead.)
- **Restore fails with "Operating system error 5 (Access is denied)"?** The SQL Server **service
  account** — not your Windows login — must be able to *read* the `.bak`. For a named instance that's a
  virtual account like `NT Service\MSSQL$INSTANCE`. Put the `.bak` in a folder that account can read
  (the SQL Server data/backup folder always works), or grant it read access to your chosen folder.

Two more, covered in detail below: **one simulation per freshly-restored database** (next section), and the
**synthetic low rungs 300–305** needing `reproduce_rungs.sql` first (Path B, step 4).

---

## ⚠️ One run per database — always restore fresh

Each simulation must run against its **own freshly-restored** copy of the seed database. Runs are tagged with
`RunID`/`BatchID`, but engine state is **not** fully isolated *within* a shared database — so if you run a
second simulation into a database that already holds a prior run, **it will complete without any error, but the
numbers won't line up** (leftover state from the earlier run bleeds into it). That's why the corpus was built
**one fresh worker database per run**, and why `migration_manager.py --label ...` mints a new database each
time. Rule of thumb: **one run, one fresh restore.** (The reproduction tools in this repo already do this for you.)

---

## Path A — verify the frozen numbers (no simulation)

Fastest way to check any chart on the site: restore the frozen corpus and query it directly.

1. Download `LibertyBee_V03R4_Baseline.bak` from this repo's latest **Release**.
2. Restore it in SQL Server (SSMS → *Restore Database*, or `RESTORE DATABASE`).
3. Query the results. Every figure on the site traces to these tables — for example, the survival curve:

   ```sql
   -- % of runs surviving 20 years, by funding rung (the S-curve on the site)
   SELECT Rung, COUNT(*) AS Seeds,
          SUM(CASE WHEN Survived = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS PctSurvived
   FROM v1.run_summary
   GROUP BY Rung
   ORDER BY Rung;
   ```

The `v1.*` schema holds the whole corpus: `run_summary`, `lease`, `lease_termination`, `property_units`,
`inflation_schedule`, `tcs_ledger`, `household`, `monthly_payment_status`. Each row is tagged `(Rung, Seed)`.

---

## Path B — regenerate it yourself (run the engine)

Prove the pipeline, not just the stored answers: restore the **seed** database and re-run a simulation.

1. Download `LibertyBeeGold.bak` from this repo's latest **Release**. By default the tooling looks for it in a
   `DBBackup/gold/` folder **at the repository root** (the folder is gitignored — create it):

   ```
   <the repo you cloned>/DBBackup/gold/LibertyBeeGold.bak
   ```

   Prefer to keep the `.bak` somewhere else? Point `LB_GOLD_BACKUP_DIR` at that folder (the folder, not the
   file) before running the next step:

   ```powershell
   # PowerShell (Windows)
   $env:LB_GOLD_BACKUP_DIR = "D:\backups\libertybee"
   ```
   ```bash
   # bash
   export LB_GOLD_BACKUP_DIR="/d/backups/libertybee"
   ```

   (SQL Server also needs read access to wherever the `.bak` lives — its service account must be able to reach
   that path.)

2. Build a fresh working database from Gold:

   ```bash
   python environmentscripts/migration_manager.py --label repro
   ```

   This restores Gold into a fresh `LibertyBee_Test_<n>_repro` database and writes its connection config.
   (Gold already carries the full schema — there are no migrations to apply.)

3. Run one simulation, deterministically:

   ```bash
   python app/src/simulation.py --env <LibertyBee_Test_..._repro> --projection-id 206 --seed 1 --months 240
   ```

   Projection **206** is the **$8M** funding rung; `--seed 1` is one of the fifty seeds used in the frozen
   corpus. Run it as many times as you like — you get the identical result every time, and it matches the
   corpus to the penny:

   | field in `v1.run_summary` | expected value |
   |---|---|
   | ProjectionID / Seed | 206 / 1 |
   | EngineVersion | 0.5.0 |
   | Survived | 1 (true) |
   | FinalCash | **$599,440.31** |
   | FinalTotal | **$1,400,778.18** |
   | EvictionCount | 0 |

   Every field should match `v1.run_summary` for `ProjectionID = 206, Seed = 1` in the corpus from Path A.
   If any differs, that's a finding — tell us.

### How do I know which settings to use?

You never have to guess. **Every run in the corpus records its own settings** — so to replay any specific run
(a particular survival, a death, one org's whole 20-year trajectory), just read them off `v1.run_summary` and
pass them back in. The three that define a run are `ProjectionID` → `--projection-id`, `Seed` → `--seed`, and
`EngineVersion` (the engine that produced it — `0.5.0`); every run uses the standard `--months 240`.

```sql
-- e.g. find a run that FAILED, then reproduce it exactly
SELECT Rung, ProjectionID, Seed, EngineVersion, Survived, FinalTotal
FROM v1.run_summary
WHERE Survived = 0
ORDER BY Rung, Seed;
```
```bash
python app/src/simulation.py --env <db> --projection-id <ProjectionID> --seed <Seed> --months 240
```

4. Reproduce any corpus cell by choosing its **funding rung** and **seed** (each rung ran across seeds **1–50**).
   Ten rungs are projections already defined inside Gold — run them directly with `--projection-id`:

   | Rung (proj. ID) | Starting capital | | Rung (proj. ID) | Starting capital |
   |---|---|---|---|---|
   | 200 | $5.0M | | 205 | $7.5M |
   | 201 | $5.5M | | 206 | $8.0M |
   | 202 | $6.0M | | 207 | $9.0M |
   | 203 | $6.5M | | 208 | $10.0M |
   | 204 | $7.0M | | 209 | $11.0M |

   The six lowest rungs — **$2.0M / $2.5M / $3.0M / $3.5M / $4.0M / $4.5M** (the under-funded + knee region),
   projection IDs **300–305** — are **not stored in Gold**; each is an exact clone of projection 206 with only
   the starting capital changed (`runs_manifest.csv` flags them with `ProjectionInGold = no`). Two ways to get them:

   - **One cell** (e.g. reproduce a specific low-rung death): apply the included **`reproduce_rungs.sql`** once
     against your fresh database — it materializes projections 300–305 — then run any of them directly, exactly
     like a stored rung, and read the result off `v1.run_summary` as in step 3:

     ```bash
     sqlcmd -S localhost -d <your_db> -i reproduce_rungs.sql          # one-time: creates projections 300–305
     python app/src/simulation.py --env <your_db> --projection-id 303 --seed 7 --months 240   # 303 = $3.5M
     ```

   - **The whole corpus**: use the included **`regenerate_corpus.py`**, which defines all sixteen rungs (cloning
     206 into 300–305 at runtime), loops them over the seeds, and extracts each result into a central corpus
     database — regenerating the full 800-run `Baseline`, then compare against the `.bak` from Path A:

     ```bash
     python regenerate_corpus.py --corpus <your_empty_corpus_db> --rungs all --seeds 1-50
     ```

---

## Customize it — test your own assumptions

The site's whole invitation is *"change the assumptions you doubt, and run it against your own city."* Here's how.

`sql/migrations/` ships **empty** — the released Gold database already carries the full schema, so there's
nothing to replay. But that folder is also the hook for *your* changes: `migration_manager` applies every `.sql`
file in it on top of Gold, in filename order, recording each in `dbo.SchemaVersion` so it's applied exactly once.

To layer a change of your own, drop a numbered SQL file in `sql/migrations/`:

```
sql/migrations/V00065__my_change.sql
```

- The **version** is the part before `__` (here `V00065`). It must not already be applied — the released Gold
  contains everything **through V00064**, so start your own at **V00065** and count up.
- Files starting with **`V`** are treated as schema/structural and run **before** files starting with **`S`**
  (seed data). Both are just SQL.

Most "assumptions" are plain parameter values, so a one-line `UPDATE` is often all you need — no Python. To make
the below-market discount deeper, or to point the market at a different city's rents and prices, update the
relevant reference/parameter rows in your migration (`reference/users_guide.md` maps the knobs to what they
mean). Then rebuild and run:

```bash
python environmentscripts/migration_manager.py --label mytest   # restores Gold, then applies your V00065+
python app/src/simulation.py --env <LibertyBee_Test_..._mytest> --projection-id 206 --seed 12345 --months 240
```

Your change is now baked into that environment; compare its results against the frozen corpus to see exactly
what it did.

---

## Why it's reproducible

Every random draw in the engine comes from a single recorded seed, isolated per run by `RunID`/`BatchID`.
There is no wall-clock, no unseeded randomness, no external call. Restore the same seed database, run the
same `(projection, seed)` on engine `0.5.0`, and you get the same run — every acquisition, every lease, every
death or survival — down to the last dollar. If you get something different, that's a finding: tell us at
**gray@libertybee.org**.
