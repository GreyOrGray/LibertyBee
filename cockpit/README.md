# The cockpit — configure and run the model in your browser

A **local** web app: everything it does, the command line also does — the browser is just a
friendlier terminal. It binds to `127.0.0.1` only, talks only to your local PostgreSQL, and
never phones anywhere.

```
pip install -r cockpit/requirements.txt
python cockpit/app.py
```

Open **http://127.0.0.1:5000**. You'll want at least one environment first (SETUP.md §4), or
mint one right from the front page.

## What it gives you

- **Town knobs** — per-town local-market values (income, tax, insurance, vacancy) for an
  imported region; each property resolves by its town. Blank inherits the region-wide value.
- **Parameter sets** — every model knob, grouped and documented, each showing **where its
  number comes from**. The shipped canonical sets are **read-only**: clone one, change what
  you want, and your clone stores only the differences.
- **Run** — pick an environment, a parameter set, a seed, a horizon; watch the months tick.
  One run per environment at a time (they'd race otherwise); environments mint in a second,
  so make as many as you like. Cancel freely — environments are disposable.
- **Results** — the verdict, the monthly Cash and Community Stability Fund series, and the
  headline counts. Same seed, same parameters ⇒ the identical chart, to the penny.
- **Batch** — a handful of seeds run back-to-back with a flat-count tally. Deliberately
  modest: a few seeds is a reading, not a statistic. The published record uses 500 seeds per
  rung; `regenerate_corpus.py` is the road to that scale.

## The honesty rules it keeps

- Canonical parameter sets can't be edited — not in the UI, not by hand-crafted requests.
  Experiments happen on clones with their own IDs.
- Every knob you can change shows the evidence behind its shipped value. Overriding a sourced
  number is your right — the cockpit just makes sure it's never an accident.
- Run artifacts (logs, the run registry) stay in `cockpit/runs/`, machine-local, never
  committed.
