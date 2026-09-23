# Reproducing the Liberty Bee record

Every published number traces to a corpus of simulated runs, and every run reproduces from a
seed. This document is the skeptic's path: restore the record, verify what generated it, and
re-run any part of it yourself. Setup first: [SETUP.md](SETUP.md).

## What the record is

**89,800 twenty-year (240-month) simulated runs across 44 released corpora** — three
compensation bases × nine regions, generated on PostgreSQL by the published engine
(**0.6.0**, public commit **`71b8b81`**), with zero override flags. The bases:

- **declared** (`libertybee_v3_*`) — the original founder-subsidy pay basis, plus its
  deep-discount ladders;
- **fw** (`libertybee_fw_*`) — fair wages: each region's own metro median hire-in with
  75th-percentile career caps (BLS OEWS May 2025; see `fw_build.py`);
- **fwrd** (`libertybee_fwrd_*`) — fair wages plus the leaner tenant deal the site
  headlines (5% signing, 3/3/5% tenure reductions, 10% credit).

The regions: Salem 2026 (the record), the 2025 Salem vintage (kept as the historical
comparison), six North Shore towns, Tacoma, and San Francisco — each universe built from
government data (MassGIS + ACS; Pierce County; DataSF — see `regiondata/README.md` and
`regiondata/adapters/`). Each corpus is a full funding ladder (up to 23 levels, $2M–$20M)
at up to 500 seeds per rung. The headline corpus is **`libertybee_fwrd_salem2026`**
(23 rungs × 500 seeds = 11,500 runs — the $7M clean floor on the front page).

## 1. Restore a corpus (the dataset)

Each corpus ships as `libertybee_<basis>_<region>.dump` on the release, with its sha256 in
the matching `SHA256SUMS_*.txt`. Verify, then restore — the headline corpus as the example:

```
Get-FileHash libertybee_fwrd_salem2026.dump -Algorithm SHA256   # compare to SHA256SUMS_corpora_fwrd.txt
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe"   -h localhost -U libertybee -w libertybee_fwrd_salem2026
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -h localhost -U libertybee -w -d libertybee_fwrd_salem2026 libertybee_fwrd_salem2026.dump
```

> **Split volumes.** Three dumps exceed the release-asset size limit and ship as numbered
> parts (`<name>.dump.part1`, `.part2`, …): `libertybee_fwrd_salem2026`,
> `libertybee_v3_baseline`, `libertybee_v3_salem2026`. Reassemble before restoring —
> `cmd /c copy /b "x.dump.part1"+"x.dump.part2" "x.dump"` — then verify the assembled
> file's sha256 as above (the checksums are for the whole dumps).

The whole record is now queryable — run-level results in `v1.run_summary`, full per-run detail
in the other `v1.*` tables (in the fw/fwrd corpora, `Rung` is the starting capital in dollars):

```sql
SELECT Rung, COUNT(*), AVG(CASE WHEN Survived = 1 THEN 1.0 ELSE 0 END) AS survival
FROM v1.run_summary GROUP BY Rung ORDER BY Rung;
```

## 2. Verify provenance (what generated this?)

```
python reproduction_gate.py --corpus libertybee_fwrd_salem2026 --provenance-only
```

Checks the corpus's own stamps: the scenario, the harness commit (a public commit you can
check out), a clean-tree flag, and per-rung documentation coverage. A corpus generated from a
modified or unpublished tree is permanently marked and fails here — by design.

## 3. Re-run sampled cells (does it reproduce?)

Every corpus was swept from its own seed template — `libertybee_<region>_<basis>_gold`,
shipped alongside the corpora as release assets (restore one like any other dump). Point
the gate's re-runs at the **matching** template, then run it:

```
$env:LB_PG_TEMPLATE = 'libertybee_salem2026_fwrd_gold'
python reproduction_gate.py --corpus libertybee_fwrd_salem2026
```

Samples cells, re-runs each **from your checkout** in a freshly minted database, and compares
against the stored results. Determinism is exact — a surviving cell must match its final
figure to the penny; a failed cell must die in the same month. **Expect:**
`GATE PASSED: all N sampled cells reproduce from HEAD.`

> **A wall of `DRIFT` (or `NO-PROJ`) instead?** That is the gate telling you the template
> doesn't match — your re-runs simulated a different compensation basis or ladder than the
> corpus was swept from, so every cell differs. Set `LB_PG_TEMPLATE` to the corpus's own
> gold (fw/fwrd corpora pair with `libertybee_<region>_fw_gold` / `_fwrd_gold`; declared
> corpora with `libertybee_<region>_gold`; the release notes list every pairing) and
> re-run.

## 4. Re-run any single cell by hand

Pick any row of `v1.run_summary`; its (ProjectionID, Seed) is the complete recipe — minted
from the corpus's matching template (§3), which `migration_manager.py` also honors:

```
$env:LB_PG_TEMPLATE = 'libertybee_salem2026_fwrd_gold'
python environmentscripts/migration_manager.py --label myrepro
python app/src/simulation.py --env <the_minted_env> --projection-id <P> --months 240 --seed <S>
```

Compare the final summary against the stored row. Same seed ⇒ same run, to the penny.

## 5. Regenerate at any scale

The corpus machinery ships in this repo (`create_corpus.py`, `regenerate_corpus.py` — see
SETUP.md §9). Regenerate a rung, a ladder, or the entire corpus; the in-flight checks and
provenance stamps run for you exactly as they ran for us.

## 6. Change the assumptions (turn the knobs)

Every modeling assumption is a named parameter in the database's registry —
[`reference/parameter_reference.md`](reference/parameter_reference.md) documents all of them,
and the [user's guide](reference/users_guide.md) explains how far to trust each one (a **CITED**
knob is grounded in a named real-world source; a **MECHANICAL** one is engine plumbing). Two
ways to turn them:

**Quick experiment** — edit the registry in a minted environment and re-run. Environments are
disposable, so nothing you break matters:

```sql
-- e.g. deepen the below-market discount to 25%:
UPDATE reference.parameterregistrydefault
SET Value = '0.25'
WHERE Category = 'PROP' AND Name = 'BelowMarketRentPct';
```

```
python app/src/simulation.py --env <that_env> --projection-id 200 --months 240 --seed 12345
```

Same seed, changed assumption — the difference in outcomes is the assumption's effect.

**Durable variant** — copy a shipped bundle, add or override any registry parameter in its
`region.json` `parameters` map, and import it. Your variant becomes a named, provenance-stamped
database; unknown parameter names are reported and skipped, so typos can't silently do nothing:

```
python region_importer.py --bundle my-salem-variant --label myvariant
```

Sweep it with the corpus tooling (§5) and you've produced your own survival ladder under your
own assumptions — the same pipeline, end to end, that produced the published record.

## The reproduction contract

- **Engine determinism is cross-checked**: seeded RNG + integer/decimal arithmetic; the event
  stream itself is deterministic (explicit processing order everywhere the database could
  otherwise choose).
- **The corpus cites its commit**; the commit is public; the gate re-runs from it. There is no
  step where you have to trust us.
- If you find a cell that does not reproduce, that is a finding — please open an issue.
