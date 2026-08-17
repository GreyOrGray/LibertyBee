# Liberty Bee — PostgreSQL Setup (from scratch)

The open-stack setup: everything below runs on PostgreSQL with zero proprietary components.
Written to be **executed top to bottom as a test** — every step ends with a command whose output
tells you it worked. (SETUP.md remains the SQL Server guide until the corpus cutover, when this
file replaces it and the SQL Server path is archived — cutover scope in
`docs/v_0_3/phases/phase_1_12/corpus_supersede_plan.md`.)

> **Current limitation (pre-cutover):** step 3's engine-gold build cuts the PG schema **from a
> live SQL Server database**, so a brand-new machine still needs the SQL Server side once. The
> source-free gold build (from committed `environmentscripts/pg_schema.sql` + a reference-data
> dump) is a tracked cutover deliverable. On a machine where `libertybee_gold` already exists
> (or was restored from a dump), skip step 3.

## 1. Prerequisites

- Windows 10/11, Python 3.12, git.
- Python deps: `pip install -r app/requirements.txt` then `pip install "psycopg[binary]"`.
- Clone the repo; all commands below run from the repo root.

## 2. PostgreSQL 18

1. Install **PostgreSQL 18 (x64)** via the EDB installer from postgresql.org/download/windows.
   Take the defaults (port 5432), skip Stack Builder, remember the superuser password.
   **Do not performance-tune anything** — stock configuration is deliberate: the honest
   cross-engine benchmark and the adopter story both depend on "install it, take the defaults,
   it works."
2. Create the engine role (pgAdmin 4 → any database → Query Tool — pgAdmin installed with EDB):
   ```sql
   CREATE ROLE libertybee LOGIN CREATEDB PASSWORD '<pick-a-password>';
   ```
   `CREATEDB`, no superuser — the tooling mints and drops ephemeral databases, nothing more.
3. Password file — create `%APPDATA%\postgresql\pgpass.conf` containing exactly one line:
   ```
   localhost:5432:*:libertybee:<that-password>
   ```
   Every client (psql, pg_dump, psycopg) reads this automatically. **No password ever goes in a
   config file or the repo** — the tooling refuses configs that carry one.
4. Optional lockdown: in `C:\Program Files\PostgreSQL\18\data\postgresql.conf` (NOT
   pgpass.conf), uncomment and set `listen_addresses = 'localhost'`, then restart the
   `postgresql-x64-18` service.
5. **Verify:**
   ```
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U libertybee -d postgres -w -c "SELECT current_user, version();"
   ```
   Expect your role name and `PostgreSQL 18.x`. `-w` means it fails loudly instead of prompting
   if pgpass isn't wired.

## 3. Build the engine gold (one-time; needs SQL Server pre-cutover)

```
python environmentscripts/pg_schema_cut.py --env <a_migrated_sqlserver_env> --build-gold
```

Cuts the schema from the SQL Server catalog, ports the reference data, positions identity
sequences, and parity-audits every table. **Expect:** `PARITY AUDIT` all `[OK ]`, then
`libertybee_gold built`. Rebuild deliberately with `--rebuild` — it refuses to overwrite
silently.

## 4. Ephemeral environments (the daily workflow)

```
python environmentscripts/migration_manager.py --pg --label mytest
```

Mints `libertybee_test_<NNNN>_mytest` from the gold template (~1 second), applies any pending
migrations, stamps provenance, writes `environments/<name>/db_config.json` (backend `psycopg`,
no password field). Also: `--pg --list` (with provenance), `--pg --drop <name> --yes` (dry-run
without `--yes`; refuses anything outside `libertybee_test_*` — the gold is untouchable here).

## 5. Run a simulation

```
python app/src/simulation.py --env <the_minted_env> --projection-id 200 --months 240 --seed 12345
```

**Expect (the anchor):** `Cash burned: $4,012,108.96`, `Final annual payroll: $287,500.0000` —
penny-identical to the SQL Server engine, ~105 s.

## 6. Run the regression suite

```
python app/src/master_test_runner.py --env <the_minted_env> --regression
```

**Expect:** `Overall Result: OK ALL TESTS PASSED` (~4 min; a few gates print
"SKIP on PostgreSQL — planner-instrumentation gate is engine-specific" — that is honest
disclosure, not failure). Note: never run two suites concurrently — the determinism gate's
self-built env is a singleton.

## 7. Import a region

```
python region_importer.py --bundle regiondata/bundles/massachusetts/salem --label myregion --pg
```

Validates the bundle fail-loud, mints a fresh env, replaces the universe, applies the region's
parameters (omitted market params fall back to cited national defaults, warned by name), stamps
provenance. **Expect:** `OK — universe replaced (2256/5823); 68 parameters applied`. This is the
clean-provenance MassGIS+ACS Salem bundle — the corpus-of-record source. (The frozen V2
reproduction fixture lives at `regiondata/fixtures/salem_reference_v2`; importing it and running
step 6 reproduces the V2 record.)

## 8. Corpus tooling (sweep operators)

```
python create_corpus.py --corpus <name> --pg                # 17 v1.* tables, verified
python regenerate_corpus.py --corpus <name> --scenario standard --seeds 1-N --rungs 200 --workers 2 --pg
python reproduction_gate.py --corpus <name> --pg            # provenance + sampled cell re-runs
```

Corpora of record are generated from a **promoted checkout with a clean tree** — the sweep
refuses the dev tree and dirty trees by design (`--allow-*` overrides exist for smoke tests and
mark the corpus permanently). A corpus is a durable artifact: take a `pg_dump` after each sweep.
Worker templates: set `LB_PG_TEMPLATE` to a corpus-base template (e.g. a promoted region import:
`migration_manager --pg --envname <imported_env> --promote-template <template_name>`).

## Configuration — env-var overrides (all optional)

| var | default | meaning |
|---|---|---|
| `LB_PG_HOST` / `LB_PG_PORT` / `LB_PG_USER` | localhost / 5432 / libertybee | PG connection |
| `LB_PG_TEMPLATE` | libertybee_gold | the template envs/workers mint from |
| `LB_CORPUS_BACKEND` | pyodbc | corpus-harness engine (`--pg` flags override per-invocation) |
