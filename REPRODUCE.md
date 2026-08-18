# Reproducing the Liberty Bee record

Every published number traces to a corpus of simulated runs, and every run reproduces from a
seed. This document is the skeptic's path: restore the record, verify what generated it, and
re-run any part of it yourself. Setup first: [SETUP.md](SETUP.md).

> **[AT FREEZE — placeholders resolved when the v3 corpus ships]:** corpus dump asset name,
> sha256, total run count, and the exact public commit the corpus cites. Until then this
> document describes the mechanism; the release notes carry the numbers.

## What the record is

The v3 corpus: **[AT FREEZE: N] twenty-year (240-month) simulated runs** across 16 funding
rungs (projections 200–209, 300–305) and the deep-discount ladders, generated on PostgreSQL
from the clean-provenance Salem universe (MassGIS parcels + Census ACS — see
`regiondata/README.md`), by the published engine at commit **[AT FREEZE: sha]**, with zero
override flags.

## 1. Restore the corpus (the dataset)

Download `libertybee_v3_corpus.dump` **[AT FREEZE: exact asset name]** from the release,
verify its sha256 against the release notes, then:

```
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe"   -h localhost -U libertybee -w libertybee_v3_baseline
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -h localhost -U libertybee -w -d libertybee_v3_baseline libertybee_v3_corpus.dump
```

The whole record is now queryable — run-level results in `v1.run_summary`, full per-run detail
in the other `v1.*` tables:

```sql
SELECT Rung, COUNT(*), AVG(CASE WHEN Survived = 1 THEN 1.0 ELSE 0 END) AS survival
FROM v1.run_summary GROUP BY Rung ORDER BY Rung;
```

## 2. Verify provenance (what generated this?)

```
python reproduction_gate.py --corpus libertybee_v3_baseline --provenance-only
```

Checks the corpus's own stamps: the scenario, the harness commit (a public commit you can
check out), a clean-tree flag, and per-rung documentation coverage. A corpus generated from a
modified or unpublished tree is permanently marked and fails here — by design.

## 3. Re-run sampled cells (does it reproduce?)

```
python reproduction_gate.py --corpus libertybee_v3_baseline
```

Samples cells, re-runs each **from your checkout** in a freshly minted database, and compares
against the stored results. Determinism is exact — a surviving cell must match its final
figure to the penny; a failed cell must die in the same month. **Expect:**
`GATE PASSED: all N sampled cells reproduce from HEAD.`

## 4. Re-run any single cell by hand

Pick any row of `v1.run_summary`; its (ProjectionID, Seed) is the complete recipe:

```
python environmentscripts/migration_manager.py --label myrepro
python app/src/simulation.py --env <the_minted_env> --projection-id <P> --months 240 --seed <S>
```

Compare the final summary against the stored row. Same seed ⇒ same run, to the penny.

## 5. Regenerate at any scale

The corpus machinery ships in this repo (`create_corpus.py`, `regenerate_corpus.py` — see
SETUP.md §8). Regenerate a rung, a ladder, or the entire corpus; the in-flight checks and
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
