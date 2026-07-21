# Liberty Bee

A Monte-Carlo simulation of **non-extractive housing** — an operator that stays more affordable than the
market, treats tenants well, buys buildings outright with no debt, and never raises a sitting tenant's rent.
This repository is the engine, the public site, and the reference docs behind **[libertybee.org](https://libertybee.org)**.

Liberty Bee is a **public data + systems-design project**, not a product. There is no funded operator, no
buildings, no tenants — it is a rigorous, reproducible argument that the idea holds up under twenty years of
simulated market pressure, from calm to crash. Every number on the site comes out of this engine, and every
run reproduces exactly from a seed.

## What's here

- **`app/src/`** — the simulation engine (Python 3.12 + SQL Server): event-driven, seeded, deterministic
- **`docs/`** — the public site (served at libertybee.org)
- **`reference/`** — the model documented in full: the philosophy, the business rules, a plain-language user's
  guide, the engine internals, the architecture map, a **data dictionary** and **parameter reference** (for the
  shipped database + every knob), the failure modes, the cited **evidence base**, and a glossary
- **`REPRODUCE.md`** — restore the released database and verify / re-run / change any result yourself
- **Reproduction tools** (all take `--corpus <your_restored_db>`, so they run against any name you restore to):
  - `site_metrics.py` — recompute **every figure on the site** from a restored corpus, vs its published value
  - `reproduction_gate.py` — prove a restored corpus reproduces from the current engine, to the penny
  - `scarcity_remeasure.py` — the retention scarcity-share decomposition
  - `create_corpus.py` + `regenerate_corpus.py` — build an empty corpus database, then regenerate the result
    corpus (the sweep) from the seed database; every swept projection is seeded data
  - `runs_manifest.csv` — every run's settings, expected outcome, and exact command
- **`environmentscripts/migration_manager.py`** — restores the seed database from a Release asset
- **`sql/migrations/`** — empty: the seed database ships fully built. It's also the hook for *your own* changes
  (drop a numbered `.sql` file to alter assumptions — see REPRODUCE.md → "Customize it")

## Reproduce it

Don't trust it — check it. The seed database and the frozen result corpus ship as **Release** assets on this
repo. [`REPRODUCE.md`](REPRODUCE.md) walks through three levels: **verify** the published numbers against the
frozen corpus, **regenerate** them from the seed database and the engine, and **change** the assumptions and
re-run against your own city. Every figure on the site recomputes with `python site_metrics.py --corpus <db>`.

> **One run per database.** Each simulation must run against its own freshly-restored copy of the seed database
> — reusing a database across runs runs without error but produces numbers that won't line up. REPRODUCE.md
> explains why; the tools above already handle it for you.

## License

Copyright (C) 2026 Liberty Bee. Released under the **GNU Affero General Public License v3.0** (AGPL-3.0): if you
run a modified version — even as a hosted service — you must make your source available. See [`LICENSE`](LICENSE).

## Contact

Questions, corrections, or capital: **gray@libertybee.org**
