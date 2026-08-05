# Living-data contract — `live.json`

Consumed by the "live, not cited" section at the tail of the scroller.
Produced by the farm's tripwire-gated publish pipeline; every publish is a
human-merged PR.

## Blocks

- **`frozen`** — pre-baked constants from the frozen 1.0 corpus of record
  (`LibertyBee_V03R4_Baseline`, 800 runs, engine 0.5.0). Reference-only:
  the client may draw the locked curve and show the labeled run count from
  it. **No computed living statistic consumes this block** (enforced by the
  `frozen_isolation` + `never_blend` tripwires before every publish).
- **`living`** — everything computed, from living runs only. Segments are
  keyed `(scenario, engine_sha)` and are never merged across keys; each
  segment carries the SHA-256 of the Gold database it farms from.

## Display rules (binding on the client)

- The run counter may present frozen + living side by side, or as labeled
  arithmetic ("800 frozen baseline runs + N living runs") — never as one
  unlabeled number.
- Confidence bands render only where a rung's living `n >=
  contract.band_display_min_n`; below that, show the tally only.
- The `standard` scenario renders against the locked curve; flavor
  scenarios render in their own exploratory sub-zone, never overlaid on a
  cited number.
- Living survival rates are living-only measurements. If a living rate
  visibly departs from the locked curve, the locked number stays; the
  band shows the fuller truth with a note (the divergence tripwire will
  have blocked auto-publish first — a human decided to ship it).

## Fields

Per segment rung: `n`, `survived`, `deaths` (dead runs are always
published — no survivorship in the reporting), `survival_rate`,
`final_cash_csf.p10/p50/p90` (PERCENTILE_CONT semantics, matching the
corpus reporting). Per segment: `death_year_histogram` (fixed 20 buckets,
death year 1-20 from run span). Each rung also carries its own
`death_year_histogram` (same 20 buckets, that rung only; shipping since
2026-08-02), and the frozen `rungs` / `dd25_reference.rungs` blocks carry
matching per-rung `deaths` + `death_year_histogram` — the reference side of
the death-timing heatmaps and the publish gate's timing guard.
