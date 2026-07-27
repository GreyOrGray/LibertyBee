# Liberty Bee Runbook — from zero to a verified corpus

This is the **sequential** guide: numbered steps, copy-paste commands, and the exact output you
should see at each one — taken from the run that produced the corpus of record, not invented.
The deep reference (why each mechanism exists, every flag, the customization hooks) is
[`REPRODUCE.md`](REPRODUCE.md); this file is the path through it.

Pick your depth — each level stands alone and ends with something verified:

| level | you get | time |
|---|---|---|
| [0](#level-0--setup-once) | a working setup | ~30 min |
| [1](#level-1--run-one-simulation-deterministically) | one simulation, provably deterministic | ~15 min |
| [2](#level-2--verify-the-released-corpus) | the released numbers verified on your machine | 10 min – hours (your choice of depth) |
| [3](#level-3--regenerate-a-slice-and-prove-it-matches) | a corpus slice you generated, proven identical to ours | ~1 hour |
| [4](#level-4--regenerate-the-whole-corpus) | the full corpus from scratch, verified | ~3 days (idle box) |
| [5](#level-5--change-the-assumptions) | your own model variant | yours to spend |

> **The one rule that applies everywhere:** if you use a *named* SQL Server instance
> (e.g. `localhost\SQLEXPRESS`), set `LB_SQL_SERVER` in **every console you open**, before any
> command — it is session-scoped and a reboot or new window loses it:
> ```powershell
> $env:LB_SQL_SERVER = 'localhost\SQLEXPRESS'
> ```
> On a default instance, skip this. Forgetting fails loud ("database does not exist"), not
> silently — but it costs a confused minute. (Our own operator hit this. Twice.)

---

## Level 0 — setup, once

**You need Windows.** SQL Server + trusted auth + ODBC; the tooling assumes Windows paths.

1. Install, if you don't have them:
   - **SQL Server** (Express is fine for levels 0–3; a full corpus at level 4 is ~26 GB, which
     exceeds Express's 10 GB database cap — Developer edition is free and has no cap)
   - **Python 3.12**
   - **ODBC Driver 17 for SQL Server** (18 works: set `$env:LB_SQL_DRIVER = 'ODBC Driver 18 for SQL Server'`)

2. Clone the repo **at the release tag** and install the Python deps:

   ```powershell
   git clone --branch v2.0.0 https://github.com/GreyOrGray/LibertyBee.git
   cd LibertyBee
   pip install -r app\requirements.txt
   git status
   ```

   Expected: `git status` says the working tree is **clean** (detached at the tag is fine —
   it may phrase it "Not currently on any branch"). Keep it clean: the tools stamp every
   corpus with whether the tree was modified, and that stamp is permanent (level 3 explains).

3. Download the **seed database** from the Release and verify it:

   ```powershell
   gh release download v2.0.0 --repo GreyOrGray/LibertyBee --pattern "LibertyBeeGold*.bak" --dir DBBackup\gold
   Get-FileHash DBBackup\gold\LibertyBeeGold_v0-6-1.bak -Algorithm SHA256
   ```

   Expected — the hash must equal, **in full**:

   ```
   503EEF4566DE1A0329456912502740C0AFE7E4914198EEF121A86B74908BF26C
   ```

   (No `gh`? Download it from the Releases page in a browser into `DBBackup\gold\` and hash it
   the same way.)

4. One Windows quirk to know now: the SQL Server **service account** — not your login — must be
   able to *read* any `.bak` you restore. If a restore fails with *"Operating system error 5
   (Access is denied)"*, move the file somewhere the service can read (its own data/backup
   folder always works) or grant read access.

---

## Level 1 — run one simulation, deterministically

1. Build a fresh working database from the seed:

   ```powershell
   python environmentscripts\migration_manager.py --label mytest
   ```

   Expected: a line like `Test environment ready: LibertyBee_Test_0001_mytest` (your number
   may differ). No migrations apply — the seed ships fully built.

2. Run the **anchor cell** — the exact simulation this bundle was validated against:

   ```powershell
   python app\src\simulation.py --env LibertyBee_Test_0001_mytest --projection-id 206 --seed 12345 --months 240
   ```

   Takes ~4–8 minutes (it's 20 simulated years). Expected, at the end of the output:

   ```
   Final total funds: $2,086,990.15
   ```

   That's a **$8M starting-capital** organization surviving all 240 months, to the penny.

3. Prove determinism to yourself: build a *second* fresh database (step 1 again, new label) and
   re-run the same command against it. Identical result, every time, on any machine.

   > **Why a second database?** One simulation per freshly-restored database, always. A second
   > run into a used database completes *without any error* and produces numbers that won't
   > line up — leftover state bleeds in. Every tool in this repo builds fresh databases for you;
   > just never reuse one by hand.

---

## Level 2 — verify the released corpus

The corpus of record ships as Release assets: `LibertyBee_V04R1_Standard.bak` (8,000 runs —
16 funding rungs × seeds 1–500) and `LibertyBee_V04R1_DeepDiscount25.bak` (2,400 runs — the
same ladder with rents 25% below market). Verification has three depths; each subsumes the last.

1. **Restore** the corpus you want to check (browser or `gh release download` as in level 0;
   then SSMS → *Restore Database*, or `RESTORE DATABASE`). Any database name works.

2. **Depth 1 — provenance (seconds):** can this corpus be rebuilt by a stranger at all?

   ```powershell
   python reproduction_gate.py --corpus <restored_db> --strict-provenance --provenance-only
   ```

   Expected:

   ```
   provenance: projection_parameters covers all 16 rungs
   provenance: scenario=standard  harness=<12 hex chars>  clean
   GATE PASSED: provenance only (cell re-runs skipped).
   ```

   `clean` is the load-bearing word: every run came from an unmodified public checkout, and
   the `harness=` commit is one you can `git checkout` yourself.

3. **Depth 2 — rebuild sampled cells (~1 hour for a handful):** re-run real cells from scratch
   and compare against the stored results:

   ```powershell
   python reproduction_gate.py --corpus <restored_db> --strict-provenance --cells 206:1,305:260
   ```

   Each cell builds a fresh database and runs the full simulation (~6 min/cell). Expected —
   survivors match to the penny, failed organizations to the death month:

   ```
    proj/seed      kind           corpus             HEAD  result
   206/1      survived       <amount>         <amount>  ok
   305/260      halted        death@219        death@219  ok
   GATE PASSED: all 2 sampled cells reproduce from HEAD.
   ```

   `--sample edges` instead of `--cells` re-runs the lowest and highest seed of every rung
   (32 cells ≈ 3 hours) — the exact check we ran before freezing this corpus: **64/64 across
   both corpora reproduced.**

4. **Depth 3 — query the numbers behind the site** (see `REPRODUCE.md` Path A for the survival
   curve SQL, and `site_metrics.py` to recompute every published figure at once).

---

## Level 3 — regenerate a slice and prove it matches

This is the strongest cheap check: *you* generate runs with *your* machine and prove they're
byte-identical to the released corpus. (~1 hour: 48 runs + two verifications.)

1. Create an empty corpus and note the reminder it prints:

   ```powershell
   python create_corpus.py --corpus MySlice
   ```

   Expected:

   ```
   created [MySlice] on <your server> (recovery FULL)
   [MySlice] ready on <machine>: 17 v1.* tables, AdjustedRent=yes, corpus_meta=yes
   ```

   > It's created in **FULL recovery** (a corpus is a keep-forever artifact). If your instance
   > runs a log-backup chain, take a full backup now so the chain has a base; if this is a
   > throwaway experiment, `ALTER DATABASE [MySlice] SET RECOVERY SIMPLE` is fine.

2. Run the first three seeds of the standard ladder (48 runs, ~25 min at 15 workers):

   ```powershell
   python regenerate_corpus.py --corpus MySlice --rungs 200-209,300-305 --seeds 1-3 --workers 15
   ```

   Expected: a `[n/48] rung=... seed=...: completed (...)` line per run, live checks at the
   end, and:

   ```
   [check:fast_death] 48 runs, 11 deaths (22.9%), 0 within 12mo
   [check:acquisition_binge] 0 fast deaths, 0 off the KD-042 signature (5-8 properties, 0 evictions)
   Sweep complete: 48 done (0 skipped), 0 failed in ~25 min
   ```

   (Deaths are *results*, not errors — low funding rungs are supposed to die sometimes. The
   `Failed:` count is the operational truth.)

   > **Never modify the cloned repo while generating** — not even dropping a stray file in it.
   > The tool stamps `HarnessDirty=1` into the corpus permanently, and a dirty corpus can never
   > pass strict verification, because it matches no published commit. (Pausing is fine:
   > create a `sweep.stop` file in the repo root; in-flight runs finish; delete it and re-run
   > the same command to resume — completed runs are skipped.)

3. Verify your slice's provenance (as level 2 depth 1 — expect `GATE PASSED`, with **your**
   clean checkout as the harness commit).

4. **The identity proof** — compare your slice against the restored released corpus, every
   table, every substantive column:

   ```powershell
   python corpus_diff.py --corpus-a MySlice --corpus-b <restored_release_corpus>
   ```

   Expected:

   ```
   compliance: 48 shared cells, 13 cols -> IDENTICAL
   ... (every v1 table) ...
   CORPUS DIFF PASSED: every shared cell identical across all shared tables (48 cells).
   ```

   The tool compares only cells present in both (48 of the release's 8,000 — the coverage
   note is normal) and deliberately ignores exactly three bookkeeping classes: extraction-order
   IDs, which worker database ran each cell, and wall-clock stamps. Everything else — every
   lease, payment, ledger row, simulation date — must match to the byte. When we ran this
   across all 8,000 cells of two independently-generated corpora, it did.

---

## Level 4 — regenerate the whole corpus

Same commands as level 3, bigger numbers — and the release you're comparing against was built
exactly this way, by one person, on one idle desktop:

```powershell
python regenerate_corpus.py --corpus MySlice --rungs 200-209,300-305 --seeds 1-500 --workers 15 --throttle
```

- **8,000 runs ≈ 3 days** on an idle machine; the deep-discount ladder
  (`--rungs 400-409,500-505 --seeds 1-150`, into a **separate** corpus — one scenario per
  corpus, the tool enforces it) adds ~1 day.
- `--throttle` makes the sweep yield while you use the machine (one worker + gaps) and go
  full-width after ~10 minutes of idle. It changes only *when* runs happen, never results.
- Checks run automatically every 250 completions. If one halts the sweep it exits **2** and
  says why — that's "come look," not "restart it."
- Resume after anything — pause, reboot, power loss — by re-running the same command
  (env var first, new console!). Completed runs skip.
- Finish with the full verification stack: `reproduction_gate.py --strict-provenance --sample edges`,
  then `corpus_diff.py` against the released corpus for the whole-corpus identity proof.

## Level 5 — change the assumptions

The point of all of the above: once you trust the pipeline, change what you doubt and re-run.
Drop a numbered migration (starting at `V00075`) with a one-line parameter `UPDATE` into
`sql/migrations/`, rebuild, and compare your world against ours —
[`REPRODUCE.md` → "Customize it"](REPRODUCE.md) has the recipe and
[`reference/users_guide.md`](reference/users_guide.md) maps every knob to its meaning.

---

## What proves what — the validation map

| tool / check | proves | cost |
|---|---|---|
| `Get-FileHash` vs the published sha256 | your seed is our seed | seconds |
| anchor cell (206, 12345) → **$2,086,990.15** | the engine is deterministic on your machine | minutes |
| `reproduction_gate.py --provenance-only --strict-provenance` | the corpus says what made it: one scenario, a public commit, a clean tree | seconds |
| `reproduction_gate.py --cells / --sample edges` | stored results re-run from scratch, to the penny / death-month | ~6 min per cell |
| live checks during a sweep (`corpus_checks/`) | the corpus growing right *while* it grows | free |
| `corpus_diff.py` | two corpora are the same physics — every table, every cell they share | minutes |

If any of these disagrees with what this file says it should — that's a finding, and we want
it: **gray@libertybee.org**.
