# Liberty Bee

A Monte-Carlo simulation of **non-extractive housing** — an operator that stays more affordable than the
market, treats tenants well, buys buildings outright with no debt, and never raises a sitting tenant's rent.
This repository is the engine, the public site, and the reference docs behind **[libertybee.org](https://libertybee.org)**.

Liberty Bee is a **public data + systems-design project**, not a product. There is no funded operator, no
buildings, no tenants — it is a rigorous, reproducible argument that the idea holds up under twenty years of
simulated market pressure, from calm to crash. Every number on the site comes out of this engine, and every
run reproduces exactly from a seed.

## What's here

- **`SETUP.md`** — start here: PostgreSQL + Python, restore the seed databases, run your first
  simulation. Written to be executed top to bottom as a test.
- **`cockpit/`** — the same model in your browser: tune documented knobs (sources shown,
  canonical sets read-only — clone to experiment), run, watch, read results. Local only.
- **`app/src/`** — the simulation engine (Python 3.12 + PostgreSQL): event-driven, seeded, deterministic
- **`docs/`** — the public site (served at libertybee.org)
- **`reference/`** — the model documented in full: the philosophy, the business rules, a plain-language user's
  guide, the engine internals, the architecture map, a **data dictionary** and **parameter reference** (for the
  shipped database + every knob), the failure modes, the cited **evidence base**, and a glossary
- **`REPRODUCE.md`** — restore the released corpus and verify / re-run / regenerate any result yourself
- **The corpus machinery** (the same code that generated the published record):
  - `reproduction_gate.py` — prove a restored corpus reproduces from the published engine, to the penny
  - `create_corpus.py` + `regenerate_corpus.py` — build an empty corpus database, then sweep it;
    provenance-stamped, refuses to masquerade as a record from a modified tree
  - `corpus_checks/` — the in-flight honesty checks that run *during* a sweep, documented and extendable
- **`regiondata/`** — the shipped region bundles (government-sourced: MassGIS + Census ACS) and the
  how-to for building a bundle from **your own** market's data
- **`region_importer.py`** — load any region bundle into a fresh database and run the model on it
- **`environmentscripts/migration_manager.py`** — mint/list/drop ephemeral databases from the seed
  template (~1 second each; every run gets a fresh one)

## Reproduce it

Don't trust it — check it. The seed databases and the frozen result corpus ship as **Release** assets on this
repo, as plain `pg_dump` files with published checksums. [`REPRODUCE.md`](REPRODUCE.md) walks through the
levels: **restore** the corpus and query every run, **verify** its provenance and re-run sampled cells with
the gate, **re-run** any single cell from its seed, and **regenerate** at any scale — or import your own
city's data and ask the same questions of your market.

> **One run per database.** Each simulation runs against its own freshly-minted database — the tooling
> handles this for you (`migration_manager.py` mints one from the template in about a second).

## License

Copyright (C) 2026 Liberty Bee. Released under the **GNU Affero General Public License v3.0** (AGPL-3.0): if you
run a modified version — even as a hosted service — you must make your source available. See [`LICENSE`](LICENSE).

## Contact

Questions, corrections, or capital: **gray@libertybee.org**
