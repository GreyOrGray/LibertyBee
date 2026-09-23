# Phase 3.7.10 — Simulation Snapshot Instrumentation: Regression Artifacts

**Phase:** 3.7.10 — Simulation Snapshot Instrumentation (bridge from Phase 3.7 closeout to Phase 3.8)
**Promotion date:** 2026-05-14
**Production code:** `app/src/snapshot_manager.py`, `app/src/backfill_snapshots.py`, modifications to `app/src/simulation.py`

---

## Contents

| File | Purpose |
|---|---|
| `phase_3_7_10_test_snapshots.py` | 9 query-based tests covering snapshot existence, monotonicity (date, month index, cumulative counts), unit consistency, fund balance consistency, rate bounds, and backfill parity context |

---

## How To Re-Run

```bash
# Create ephemeral test database
python environmentscripts/migration_manager.py

# Run a simulation with snapshot capture enabled (snapshots fire automatically)
python app/src/simulation.py --env <test_db> --months 36 --projection-id 167 --seed 99

# Optionally backfill an existing run (synthesizes BACKFILL rows from event log + state)
python app/src/backfill_snapshots.py --env <test_db> --run-id <N>

# Run the snapshot tests
python sql/regression_tests/Phase3_7_10/phase_3_7_10_test_snapshots.py --env <test_db>
```

Expected output: `Result: 9/9 tests passed`.

---

## What's In `simulation.RunSnapshot`

Each row represents end-of-day state at a cadence boundary (default: calendar quarter-end). Fields cover:

**Fund balances:** `CashBalance`, `CSFBalance`, `EIPBalance`, `EscrowBalance`, `TotalFundBalance`
**Portfolio:** `PropertiesOwned`, `UnitsTotal`, `UnitsOccupied`, `UnitsVacant`, `UnitsInTurnover`, `OccupancyRate`, `VacancyRate`, `ActiveLeases`
**Monthly aggregates** (for the month containing SnapshotDate): `MonthRentCollected`, `MonthOpEx`, `MonthPayroll`
**Cumulative:** `TerminationsCumulative`, `EvictionsCumulative`, `TurnoversCompleted`
**Staff:** `EmployeesActive`
**Provenance:** `SnapshotCadence`, `SnapshotSource` (LIVE | BACKFILL), `SnapshotCreatedAt`

PK is `(RunID, SnapshotDate)` — at most one snapshot per (run, date).

---

## Quarterly Cadence (Default)

Snapshots fire on calendar quarter-ends:

| Quarter | SnapshotDate |
|---|---|
| Q1 | March 31 |
| Q2 | June 30 |
| Q3 | September 30 |
| Q4 | December 31 |

A 20-year (240-month) run produces ~80 quarterly snapshots — compact enough for direct CSV export and plot-friendly for trajectory analysis.

For halted runs: if the run halts between cadence boundaries, an additional final halt-date snapshot is captured per BA §6.

---

## Backfill

The `backfill_snapshots.py` script reconstructs snapshot rows for an existing RunID without re-running the simulation. Useful for analyzing Phase 3.7.7 / 3.7.8 / 3.7.9 historical runs.

Backfilled rows are tagged `SnapshotSource='BACKFILL'`. Business fields match what a LIVE capture would produce — only `SnapshotCreatedAt` and `SnapshotSource` differ.

```bash
python app/src/backfill_snapshots.py --env <db> --run-id <N> --cadence QUARTERLY [--clear-existing]
```

---

## Query Patterns

**Plot cash trajectory:**
```sql
SELECT SnapshotDate, MonthIndex, CashBalance, TotalFundBalance
FROM simulation.RunSnapshot
WHERE RunID = @run_id
ORDER BY MonthIndex;
```

**Compare two runs:**
```sql
SELECT a.SnapshotDate, a.MonthIndex,
       a.TotalFundBalance AS Run1Total, b.TotalFundBalance AS Run2Total,
       a.ActiveLeases AS Run1Leases, b.ActiveLeases AS Run2Leases
FROM simulation.RunSnapshot a
JOIN simulation.RunSnapshot b ON a.SnapshotDate = b.SnapshotDate
WHERE a.RunID = @run1 AND b.RunID = @run2
ORDER BY a.MonthIndex;
```

**Find first negative-cash quarter:**
```sql
SELECT TOP 1 SnapshotDate, MonthIndex, CashBalance
FROM simulation.RunSnapshot
WHERE RunID = @run_id AND CashBalance < 0
ORDER BY MonthIndex;
```

---

## Related Phase Documentation

- `docs/phases/phase_3_7/phase_3_7_10_snapshot_instrumentation_implementation_plan.md`
- `docs/phases/phase_3_7/phase_3_7_10_snapshot_instrumentation_questions.md`
- `docs/phases/phase_3_7/phase_3_7_10_snapshot_instrumentation_ba_answers.md`
- `docs/phases/phase_3_7/phase_3_7_10_post_implementation_report.md`
- `docs/phases/phase_3_7/phase_3_7_acceptance_summary.md` (will be updated to reference snapshots)

---

**Maintained as part of Phase 3.7.10 acceptance surface. Do not delete without explicit BA sign-off.**
