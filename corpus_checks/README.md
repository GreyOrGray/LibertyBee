# Corpus checks

Checks that run *during* a sweep, against the corpus as it fills.

A corpus sweep can take days. Without in-flight checks you find out at the end that
something was wrong from run 40 onward — and discard the whole thing. These let a
sweep notice a problem while there is still time to stop it.

Checks are optional, independently enable-able, and live here as ordinary Python
files so they can be read, edited, and added without touching the harness.

## Enabling

Either in `checks.json`:

```json
{ "enabled": ["fast_death", "acquisition_binge"], "every": 250 }
```

or on the command line, which overrides the config:

```bash
python regenerate_corpus.py --corpus MyCorpus --checks fast_death,acquisition_binge --check-every 250
python regenerate_corpus.py --corpus MyCorpus --checks all
python regenerate_corpus.py --corpus MyCorpus --checks none     # config ignored
```

`--check-every N` runs the enabled checks after every N completed runs. Checks also
run once when the sweep finishes.

## Writing one

One file per check. The filename is the check's name (`fast_death.py` → `fast_death`).
Files beginning with `_` are ignored.

Each file defines `DESCRIPTION` and a `check(cursor, ctx)` function:

```python
DESCRIPTION = "one line, shown when the check runs"

def check(cursor, ctx):
    """`cursor` is an open cursor on the corpus database.
    `ctx` carries sweep context: completed, failed, target, scenario, sweep_mode.

    Return a dict:
        summary  str   one line, always printed
        halt     bool  True to drain the sweep (in-flight runs finish, nothing new starts)
        detail   str   optional; printed only when halt is True
    """
    n = cursor.execute("SELECT COUNT(*) FROM v1.run_summary").fetchone()[0]
    return {"summary": f"{n} runs so far", "halt": False}
```

Rules the harness relies on:

- **Read-only.** A check must never write to the corpus. It runs mid-sweep, alongside
  live extracts.
- **Cheap.** It runs every N completions, competing with the sweep for the database.
  Aggregate; don't table-scan the ledger.
- **Non-fatal.** If a check raises, the harness reports it and the sweep continues.
  A broken check must never destroy a multi-day run.
- **Halt is expensive.** Returning `halt: True` stops the sweep. Reserve it for
  "continuing would generate data we will throw away."

## Shipped checks

| check | halts? | what it looks for |
|---|---|---|
| `fast_death` | no | organisations dying implausibly early — a smoke alarm for engine regressions |
| `acquisition_binge` | yes | fast deaths that do *not* match the known KD-042 over-acquisition signature, i.e. a **new** failure class |

`acquisition_binge` encodes a Liberty Bee-specific finding: KD-042 organisations
over-acquire, then die young with 5–8 properties and no evictions. That pattern is
understood and expected. A fast death *outside* it is not, and is worth stopping for.
If you are running your own scenarios, this check may not mean anything to you —
disable it.
