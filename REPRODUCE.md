# Reproduce Liberty Bee

Every number on [libertybee.org](https://libertybee.org) comes out of this engine, and every run is
deterministic: the same `(projection, seed, engine version)` produces the identical result, to the penny.
This file shows you how to check that yourself — two ways, from least to most effort.

- **Engine version of record:** `0.6.0`
- **Corpus of record:** the **v2 corpus is being generated now**, from exactly this bundle; its `.bak`s land on
  the Release page when it freezes. Until then, the numbers on the site are from **Release v1.0** (engine
  `0.5.0`, corpus `V03R4` — `Baseline` 800 runs, `Static` 100, `Cliff` 1,440). The two engines are different
  models (v2 folds in a cohort of behavioral fixes), so **v1.0 numbers reproduce under the v1.0 bundle, not
  this one** — to verify them, use Release v1.0's assets and the copy of this file frozen at the `v1.0` tag.
- The seed database (and, once frozen, the corpus) ship as **Release assets** on this repo (they're SQL Server
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
  It applies to `migration_manager.py`, `simulation.py`, `reproduction_gate.py`, `create_corpus.py`, and
  `regenerate_corpus.py`. (`site_metrics.py` takes `--server` / `--driver` flags instead.)
- **Restore fails with "Operating system error 5 (Access is denied)"?** The SQL Server **service
  account** — not your Windows login — must be able to *read* the `.bak`. For a named instance that's a
  virtual account like `NT Service\MSSQL$INSTANCE`. Put the `.bak` in a folder that account can read
  (the SQL Server data/backup folder always works), or grant it read access to your chosen folder.

One more, covered in detail below: **one simulation per freshly-restored database** (next section).

---

## ⚠️ One run per database — always restore fresh

Each simulation must run against its **own freshly-restored** copy of the seed database. Runs are tagged with
`RunID`/`BatchID`, but engine state is **not** fully isolated *within* a shared database — so if you run a
second simulation into a database that already holds a prior run, **it will complete without any error, but the
numbers won't line up** (leftover state from the earlier run bleeds into it). That's why the corpus was built
**one fresh worker database per run**, and why `migration_manager.py --label ...` mints a new database each
time. Rule of thumb: **one run, one fresh restore.** (The reproduction tools in this repo already do this for you.)

---

## Path A — verify frozen numbers (no simulation)

Fastest way to check any chart on the site: restore a frozen corpus and query it directly.

1. Download a corpus `.bak` from the **Release that produced it**. Today that means
   `LibertyBee_V03R4_Baseline.bak` from **Release v1.0** — the corpus behind the site's current numbers.
   (When the v2 corpus freezes, its `.bak`s will sit on the latest Release instead, and this step will point
   there.)
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

1. Download the seed database — the `LibertyBeeGold_v0-6-0.bak` asset on this repo's **v2 pre-release**
   (`v2.0.0-rc1`; it moves to the main v2 release when the corpus freezes). By default the tooling restores
   the most recent `.bak` it finds in a `DBBackup/gold/` folder **at the repository root** (the folder is
   gitignored — create it):

   ```
   <the repo you cloned>/DBBackup/gold/LibertyBeeGold_v0-6-0.bak
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

2. Build a fresh working database from the seed:

   ```bash
   python environmentscripts/migration_manager.py --label repro
   ```

   This restores the seed into a fresh `LibertyBee_Test_<n>_repro` database and writes its connection config.
   (The seed already carries the full schema — there are no migrations to apply.)

3. Run one simulation, deterministically:

   ```bash
   python app/src/simulation.py --env <LibertyBee_Test_..._repro> --projection-id 206 --seed 12345 --months 240
   ```

   Projection **206** is the **$8M** funding rung. Run it as many times as you like — you get the identical
   result every time:

   | field | expected value |
   |---|---|
   | ProjectionID / Seed | 206 / 12345 |
   | EngineVersion | 0.6.0 |
   | Outcome | survives all 240 months |
   | Final total funds | **$2,086,990.15** |

   `(206, 12345)` is the anchor cell this bundle was validated against before release. When the v2 corpus
   freezes you'll be able to check **any** cell the same way against its `v1.run_summary` — and if anything
   here doesn't match on your machine, that's a finding: tell us.

### How do I know which settings to use?

You never have to guess. **Every run in a corpus records its own settings** — so to replay any specific run
(a particular survival, a death, one org's whole 20-year trajectory), just read them off `v1.run_summary` and
pass them back in. The three that define a run are `ProjectionID` → `--projection-id`, `Seed` → `--seed`, and
`EngineVersion` — the engine that produced it, which must match the engine you run (this bundle: `0.6.0`;
Release v1.0's corpus rows say `0.5.0` and reproduce under that bundle). Every run uses the standard
`--months 240`.

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

4. Reproduce any corpus cell by choosing its **funding rung** and **seed**. Every rung is a stored projection
   in the seed database — there is nothing to set up first. The standard ladder:

   | Rung (proj. ID) | Starting capital | | Rung (proj. ID) | Starting capital |
   |---|---|---|---|---|
   | 300 | $2.0M | | 202 | $6.0M |
   | 301 | $2.5M | | 203 | $6.5M |
   | 302 | $3.0M | | 204 | $7.0M |
   | 303 | $3.5M | | 205 | $7.5M |
   | 304 | $4.0M | | 206 | $8.0M |
   | 305 | $4.5M | | 207 | $9.0M |
   | 200 | $5.0M | | 208 | $10.0M |
   | 201 | $5.5M | | 209 | $11.0M |

   (The deep-discount scenario ships as its own ladder over the same capitals — projections **400–409** and
   **500–505** — see *Scenarios* below.)

   - **One cell** (e.g. reproduce a specific low-rung death) — run it directly, and read the result off the
     run output as in step 3:

     ```bash
     python app/src/simulation.py --env <your_db> --projection-id 303 --seed 7 --months 240   # 303 = $3.5M
     ```

   - **The whole corpus**: build an empty corpus database, then fill it. `create_corpus.py` applies
     `corpus_schema.sql` (the `v1.*` schema of record) to a brand-new database; `regenerate_corpus.py` runs the
     projections you name over the seeds you name and extracts each result into that corpus. Every projection is
     seeded data (from the migration chain); the tool reads its funding amount and scenario from that data.

     ```bash
     python create_corpus.py     --corpus MyCorpus
     python regenerate_corpus.py --corpus MyCorpus --rungs 200-209,300-305 --seeds 1-50
     ```

     `--rungs` is required and takes a mixed list and ranges (`200-209,300-305` is the published standard
     ladder — the sixteen rungs above). There is no default set: the recipe states exactly what ran. Expect
     this to take a while — each run is a full 240-month simulation, and this is 800 of them. Add
     `--workers N` to widen parallelism, and `--throttle` if you want it to yield the machine back while
     you're using it (throttling changes only *when* runs are scheduled, never their results).

     Interrupt it safely with a `sweep.stop` file next to the script: in-flight runs finish and extract, and
     nothing new starts. Re-invoking resumes — runs already in the corpus are skipped.

### Scenarios — one corpus per scenario

A scenario is an affordability assumption, and **a corpus holds exactly one**. You don't select it with a flag;
you name the projections that carry it, and the tool reads their scenario from the seeded data:

| scenario | projections | meaning |
|---|---|---|
| `standard` | `200-209,300-305` | the seed database's own below-market rent (10%) — the published baseline |
| `deep-discount-25` | `400-409,500-505` | rents set 25% below market |

```bash
python regenerate_corpus.py --corpus MyDeepDiscount --rungs 400-409,500-505 --seeds 1-50
```

The named projections must all share one scenario, or the tool refuses — a corpus blending two populations
would look entirely normal and be silently wrong. The resolved scenario is recorded in `v1.corpus_meta` on
first write and enforced on resume. `--scenario <name>` is optional and only *asserts* the resolved value (a
guard against naming the wrong projections); it does not select anything. To sweep both scenarios, build two
corpora.

### What produced a corpus

`v1.corpus_meta` also records the engine version, the harness commit, and whether that checkout had
uncommitted changes:

```sql
SELECT Scenario, EngineVersion, HarnessCommit, HarnessDirty, StartedUTC FROM v1.corpus_meta;
```

`HarnessDirty = 1` means the corpus was generated from a modified working tree and therefore cannot be
reproduced from any published commit — useful for a throwaway experiment, disqualifying for a corpus of
record. The tool refuses to generate from a dirty tree unless you explicitly pass `--allow-dirty`.

### Verifying a corpus is reproducible, not merely re-runnable

`reproduction_gate.py` does two things, in order:

1. **Provenance check** — can this corpus be rebuilt by someone else at all? Confirms every funding rung
   present has a matching `v1.projection_parameters` row, that the corpus holds exactly one scenario, and
   that no run came from an uncommitted tree. The rung check makes the corpus **self-describing**: a
   corpus outlives any particular seed database, so every run's exact parameter set must be readable from
   the corpus alone — without it, results become uninterpretable the moment the seed moves on.
2. **Cell reproduction** — rebuilds a sample of `(rung, seed)` cells from scratch and compares against the
   corpus: survivors to the penny, failed organisations to the death-month.

```bash
python reproduction_gate.py --corpus <your_corpus_db> --sample edges
python reproduction_gate.py --corpus <your_corpus_db> --provenance-only   # fast; no re-runs
```

Add `--strict-provenance` to also require `v1.corpus_meta` (only meaningful for corpora built after
provenance stamping existed — the 1.0 corpora pre-date it and will warn instead).

Passing the numbers but failing provenance is a real and useful distinction: it means the corpus is
*correct* but not *handable* — nobody else could rebuild it.

### Watching a long sweep

A full sweep takes days, so `corpus_checks/` runs checks against the corpus as it fills — every 250
completions by default. Two ship: `fast_death` (reports the death profile) and `acquisition_binge` (halts
if organisations start dying in a pattern we don't recognise). A halt drains the sweep cleanly and exits
**2**, and `corpus_regen_status.txt` records it — so an unattended run that stops for a real reason doesn't
look like a successful one. See `corpus_checks/README.md` to write your own or disable ours.

---

## Customize it — test your own assumptions

The site's whole invitation is *"change the assumptions you doubt, and run it against your own city."* Here's how.

`sql/migrations/` ships **empty** — the released seed database already carries the full schema, so there's
nothing to replay. But that folder is also the hook for *your* changes: `migration_manager` applies every `.sql`
file in it on top of the seed, in filename order, recording each in `dbo.SchemaVersion` so it's applied exactly once.

To layer a change of your own, drop a numbered SQL file in `sql/migrations/`:

```
sql/migrations/V00074__my_change.sql
```

- The **version** is the part before `__` (here `V00074`). It must not already be applied — the released seed
  database contains everything **through V00073**, so start your own at **V00074** and count up.
- Files starting with **`V`** are treated as schema/structural and run **before** files starting with **`S`**
  (seed data). Both are just SQL.

Most "assumptions" are plain parameter values, so a one-line `UPDATE` is often all you need — no Python. To make
the below-market discount deeper, or to point the market at a different city's rents and prices, update the
relevant parameter rows in your migration — defaults live in `reference.ParameterRegistryDefault`, per-projection
overrides in `reference.ParameterRegistryDefined` (`reference/users_guide.md` maps the knobs to what they
mean). Then rebuild and run:

```bash
python environmentscripts/migration_manager.py --label mytest   # restores the seed, then applies your V00074+
python app/src/simulation.py --env <LibertyBee_Test_..._mytest> --projection-id 206 --seed 12345 --months 240
```

Your change is now baked into that environment; compare its results against the frozen corpus to see exactly
what it did.

---

## Why it's reproducible

Every random draw in the engine comes from a single recorded seed, isolated per run by `RunID`/`BatchID`.
There is no wall-clock, no unseeded randomness, no external call. Restore the same seed database, run the
same `(projection, seed)` on engine `0.6.0`, and you get the same run — every acquisition, every lease, every
death or survival — down to the last dollar. If you get something different, that's a finding: tell us at
**gray@libertybee.org**.
