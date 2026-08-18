# Liberty Bee — Setup

Everything runs on PostgreSQL and Python. Written to be **executed top to bottom as a test** —
every step ends with a command whose output tells you it worked.

## 1. Prerequisites

- Windows 10/11, Python 3.12, git.
- Python deps: `pip install -r app/requirements.txt` then `pip install "psycopg[binary]"`.
- Clone the repo; all commands below run from the repo root.

## 2. PostgreSQL 18

1. Install **PostgreSQL 18 (x64)** via the EDB installer from postgresql.org/download/windows.
   Take the defaults (port 5432), skip Stack Builder, remember the superuser password.
   **Do not performance-tune anything** — stock configuration is deliberate: every published
   number was produced on defaults.
2. Create the engine role (pgAdmin 4 → any database → Query Tool — pgAdmin installs with EDB):
   ```sql
   CREATE ROLE libertybee LOGIN CREATEDB PASSWORD '<pick-a-password>';
   ```
   `CREATEDB`, no superuser — the tooling mints and drops ephemeral databases, nothing more.
3. Password file — create `%APPDATA%\postgresql\pgpass.conf` containing exactly one line:
   ```
   localhost:5432:*:libertybee:<that-password>
   ```
   Every client (psql, pg_restore, the Python tooling) reads this automatically. No password
   ever goes in a config file — the tooling refuses configs that carry one.
4. Optional lockdown: in `postgresql.conf` (the data directory), set
   `listen_addresses = 'localhost'`, then restart the `postgresql-x64-18` service.
5. **Verify:**
   ```
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U libertybee -d postgres -w -c "SELECT current_user, version();"
   ```
   Expect your role name and `PostgreSQL 18.x`. `-w` fails loudly instead of prompting if
   pgpass isn't wired.

## 3. Restore the seed databases (release assets)

Download from the release: `libertybee_gold_<version>.dump` (the engine's reference database)
and `libertybee_salem_gold_<version>.dump` (the corpus base — the clean-provenance Salem
universe). Verify each file's sha256 against the release notes, then:

```
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe"    -h localhost -U libertybee -w libertybee_gold
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe"  -h localhost -U libertybee -w -d libertybee_gold libertybee_gold_<version>.dump
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe"    -h localhost -U libertybee -w libertybee_salem_gold
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe"  -h localhost -U libertybee -w -d libertybee_salem_gold libertybee_salem_gold_<version>.dump
```

(The full schema is also readable in the open: `environmentscripts/pg_schema.sql` and
`corpus_schema_pg.sql`.)

## 4. Ephemeral environments (the daily workflow)

```
python environmentscripts/migration_manager.py --label mytest
```

Mints `libertybee_test_<NNNN>_mytest` from the gold template (~1 second), stamps provenance,
writes `environments/<name>/db_config.json`. Also: `--list` (with provenance) and
`--drop <name> --yes` (dry-run without `--yes`; refuses anything outside
`libertybee_test_*` — the gold is untouchable here).

The simulator accepts your **label alone** when it's unambiguous — `--env mytest` resolves to
the full minted name (and tells you); ambiguous or unknown names list the candidates.

> Seeing `source database "libertybee_gold" is being accessed by other users`? PostgreSQL
> can't copy a template while anything is connected to it — usually pgAdmin. Right-click the
> database in pgAdmin → **Disconnect from database**, and re-run.

## 5. Run a simulation

```
python app/src/simulation.py --env <the_minted_env> --projection-id 200 --months 240 --seed 12345
```

**Expect (the anchor):** `Cash burned: $4,012,108.96`, `Final annual payroll: $287,500.0000` —
to the penny. If you see these numbers, your install reproduces the published engine exactly.

## 6. Validate against the record

> **Needs the released corpus first** — the record's database, restored from its release-asset
> dump (REPRODUCE.md §1). If you haven't restored it yet, skip this step and come back.

```
python reproduction_gate.py --corpus <restored_corpus_db>
```

How it works: the restored corpus contains every published run's results **and its own
provenance stamps** (which public commit generated it, from a clean tree). The gate first
verifies those stamps, then picks sample cells — a (funding rung, seed) pair each — mints a
fresh throwaway database, re-runs each cell's full 240-month simulation **from your checkout**,
and compares your outcome to the stored one. Determinism is exact, so the bar is exact: a
surviving cell must match its final figure to the penny; a failed one must die in the same
month. **Expect:** `GATE PASSED: all N sampled cells reproduce from HEAD.` If it passes, your
machine just independently regenerated part of the published record — that's the whole trust
story, and REPRODUCE.md §3–§5 shows how to take it as far as you like.

## 7. Import a region

```
python region_importer.py --bundle regiondata/bundles/massachusetts/salem --label myregion
```

Validates the bundle fail-loud, mints a fresh env, replaces the universe, applies the region's
parameters (omitted market params fall back to cited national defaults, warned by name), stamps
provenance. **Expect:** `OK — universe replaced (2256/5823); 68 parameters applied`. Build your
own bundle: see `regiondata/README.md`.

## 8. The cockpit — all of the above, in your browser

```
pip install -r cockpit/requirements.txt
python cockpit/app.py
```

Open **http://127.0.0.1:5000**: mint environments, tune town knobs and parameter sets (every
knob shows where its number comes from; canonical sets are read-only — clone to experiment),
run simulations with live progress, and read results. Local only; everything it does, the
commands above also do. Details: `cockpit/README.md`.

## 9. Corpus tooling (sweep operators)

```
python create_corpus.py --corpus <name>                # the corpus schema, verified
$env:LB_PG_TEMPLATE = 'libertybee_salem_gold'               # workers mint from the corpus base
python regenerate_corpus.py --corpus <name> --scenario standard --seeds 1-N --rungs 200-209,300-305 --workers 4
```

Corpora of record are generated from a **clean checkout of a published commit** — the sweep
refuses modified or unpublished trees by design, and any override permanently marks the corpus
as not-a-record. A corpus is a durable artifact: take a `pg_dump` after each sweep.

## 10. Change the model's assumptions

Every assumption is a named, documented knob — see **REPRODUCE.md §6** for the two ways to
turn them (a quick registry edit in a disposable environment, or a durable parameter-override
bundle), and `reference/parameter_reference.md` for what each knob means and how far to
trust it.

## Configuration — env-var overrides (all optional)

| var | default | meaning |
|---|---|---|
| `LB_PG_HOST` / `LB_PG_PORT` / `LB_PG_USER` | localhost / 5432 / libertybee | PG connection |
| `LB_PG_TEMPLATE` | libertybee_gold | the template envs/workers mint from |
