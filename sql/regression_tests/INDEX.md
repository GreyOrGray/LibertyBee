# Regression Test Script Registry

> ⚠️ **V0.1-era reference (2026-05-14). For current status + how to run the suite, see [README.md](README.md).** The per-test `How to Run` blocks below reference now-deleted `scratch/app/src/` paths and the V0.1 SQL projection-seeder mechanism (which `INSERT`s into the legacy `reference.ProjectionParameters` wide table the engine no longer reads). The suite is being rehabilitated for V0.2 under [#106](https://github.com/GreyOrGray/LibertyBeeDev/issues/106) — current baseline is **11/20 green** via `run_suite.py`. This INDEX also predates the Phase 3.8–3.10 modules (not catalogued below). Treat the entries here as historical per-test intent, not runnable instructions.

**Purpose:** Complete catalog of regression test scripts, what they validate, and which phases they protect.

---

## Active Regression Tests

### Phase3_7_5_PayFail_Stress_Tests.sql

**Created:** 2026-01-31
**Protects:** Phase 3.7.5 (Obligation Settlement & Ordering)
**Test Projections:** 4 projections (IDs 157-160)

**What It Tests:**
- FIFO arrears ordering with multi-month catchups (up to 8 consecutive MISSED months)
- Eviction triggers and arrears write-offs
- Post-termination payment tracking
- Deterministic behavior under varied failure rates

**Test Scenarios:**

| ProjectionID | Name | PAY_BaseFailProbMonthly | Purpose |
|--------------|------|-------------------------|---------|
| 157 | Phase3.7.5_Test_PayFail20pct | 0.20 (20%) | Moderate stress, multi-month arrears |
| 158 | Phase3.7.5_Test_PayFail40pct_Evictions | 0.40 (40%) | Eviction trigger threshold |
| 159 | Phase3.7.5_Test_PayFail60pct_StressTest | 0.60 (60%) | High stress, multiple evictions |
| 160 | Phase3.7.5_Test_PayFail80pct_ExtremeStress | 0.80 (80%) | Extreme stress, 8-month catchups |

**How to Run:**
```bash
# Apply test projections
sqlcmd -S localhost -d LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX -i scratch/sql/migrations/regression_tests/Phase3_7_5_PayFail_Stress_Tests.sql

# Run 18-month simulation with Projection 160 (extreme stress)
python scratch/app/src/master_test_runner.py --env LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX --clean --months 18 --seed 12345 --projection-id 160
```

**Expected Results (Projection 160, seed 12345):**
- 2 evictions
- 8-month arrears catchup (Lease 4: $29,143.80 total)
- 39 total MISSED months
- $98,389.80 arrears collected
- All FIFO records updated correctly

**Key Validation:**
- Arrears double-counting bug fixed (old MISSED records updated when satisfied)
- Perfect FIFO ordering ($0 → $3,238 → $6,476 → ... → $25,906)
- Write-offs occur at eviction execution
- Post-termination MonthlyPaymentStatus tracking works

---

### Phase3_7_6_Deposit_Settlement_Tests.sql

**Created:** 2026-05-11
**Protects:** Phase 3.7.6 (Deposit Settlement)
**Test Projections:** 4 projections (IDs 161-164)

**What It Tests:**
- Two-step settlement model (outcome decided at termination, cash movement after delay)
- Eviction-biased vs voluntary damage probability divergence
- FULL_RETURN / PARTIAL_RETURN / FORFEITURE outcomes under forced parameter conditions
- Configurable settlement delay (`DEP_SettlementDelayDays`)
- Determinism (same seed + projection => same outcome and damage amount)

**Test Scenarios:**

| ProjectionID | Name | DEP_EvictionDamageProbability | DEP_VoluntaryDamageProbability | DEP_DamageMinPercent | DEP_DamageMaxPercent | DEP_SettlementDelayDays | Other overrides | Purpose |
|--------------|------|-------------------------------|--------------------------------|----------------------|----------------------|-------------------------|-----------------|---------|
| 161 | Phase3.7.6_Test_NoDamage | 0.0 | 0.0 | 0.10 | 0.50 | 30 | LEASE_RenewalRatePct=30 | Every funded termination -> FULL_RETURN |
| 162 | Phase3.7.6_Test_GuaranteedDamage | 1.0 | 1.0 | 0.20 | 0.30 | 30 | LEASE_RenewalRatePct=30 | Every funded termination -> PARTIAL_RETURN with damage in [20%, 30%] of required |
| 163 | Phase3.7.6_Test_EvictionBias | 1.0 | 0.0 | 0.30 | 0.40 | 30 | PAY_BaseFailProbMonthly=0.40, LEASE_RenewalRatePct=30 | Evictions -> damage; voluntary/non-renewal -> FULL_RETURN |
| 164 | Phase3.7.6_Test_ShortDelay | 0.30 | 0.10 | 0.10 | 0.50 | **1** | LEASE_RenewalRatePct=30 | Fast settlement (next day) for short-duration tests |

**How to Run:**
```bash
# Apply test projections to an ephemeral test database
sqlcmd -S localhost -d LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX -i scratch/sql/migrations/regression_tests/Phase3_7_6_Deposit_Settlement_Tests.sql

# Run simulation against one of the test projections (24 months recommended)
python scratch/app/src/simulation.py --env LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX --months 24 --seed 99 --projection-id 164
```

**Verification Queries:**
```sql
-- Summary of settlements per run
SELECT RunID, COUNT(*) AS Total,
       SUM(CASE WHEN SettlementStatus='COMPLETED' THEN 1 ELSE 0 END) AS Completed,
       SUM(CASE WHEN SettlementStatus='PENDING' THEN 1 ELSE 0 END) AS Pending
FROM simulation.LeaseDeposit
WHERE SettlementStatus IS NOT NULL
GROUP BY RunID ORDER BY RunID;

-- Breakdown by outcome (use specific RunID)
SELECT SettlementOutcome, COUNT(*) AS N, AVG(DamageAmount) AS AvgDamage
FROM simulation.LeaseDeposit
WHERE RunID=<id> AND SettlementStatus='COMPLETED'
GROUP BY SettlementOutcome;
```

**Expected Results (verified 2026-05-11, projection 164, seed 99, 24 months):**
- Simulation halts on cash shortfall around month 16 (expected — projection forces stress with PAY_BaseFailProbMonthly elevated to 0.04 effectively via renewal pressure)
- 3+ settlements complete (mix of FULL_RETURN and FORFEITURE depending on which leases reach termination)
- SettlementDueDate = TerminationDate + 1 day (validates 1-day delay)

**Key Validation:**
- Forfeiture path (unfunded at termination) produces DamageAmount=0.00 and FORFEITURE outcome
- Funded voluntary exits with DEP_VoluntaryDamageProbability=0.0 (projection 161) produce ONLY FULL_RETURN
- Funded terminations with damage probabilities=1.0 (projection 162) produce ONLY PARTIAL_RETURN
- Settlement fires after configured delay (projection 164: 1 day; projections 161-163: 30 days)

---

### Phase3_7_7_Turnover_Tests.sql

**Created:** 2026-05-12
**Protects:** Phase 3.7.7 (Turnover Compliance Trigger)
**Test Projections:** 4 projections (IDs 165-168)

**What It Tests:**
- Turnover workflow firing across all four termination types (EVICTION, VOLUNTARY, EARLY_BREAK, LANDLORD_NONRENEWAL)
- Eviction-biased RESTORATION duration (15-30) vs voluntary duration (10-30)
- Sequential work item progression within each unit
- Parallel turnovers across multiple units in the same simulation
- Unit status transitions: Pending_Move_Out (during eviction) -> Turnover -> Available

**Test Scenarios:**

| ProjectionID | Name | PAY_BaseFailProbMonthly | LEASE_RenewalRatePct | DEP_SettlementDelayDays | Purpose |
|--------------|------|-------------------------|----------------------|-------------------------|---------|
| 165 | Phase3.7.7_Test_HighEviction | 0.40 (40%) | 80 (default) | 30 | Force evictions; observe eviction-biased restoration durations |
| 166 | Phase3.7.7_Test_HighVoluntaryExit | 0.02 (default) | 20 (low) | 30 | Force voluntary exits at lease end; observe voluntary restoration ranges |
| 167 | Phase3.7.7_Test_MixedTermination | 0.20 (20%) | 30 (low) | 30 | Both eviction-driven and voluntary-driven turnovers in same run |
| 168 | Phase3.7.7_Test_ShortCycleObservable | 0.02 (default) | 30 (low) | 1 (fast) | Compact turnover cycles for shorter simulation observation |

**How to Run:**
```bash
# Apply test projections
sqlcmd -S localhost -d LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX -i scratch/sql/migrations/regression_tests/Phase3_7_7_Turnover_Tests.sql

# Run 24-month simulation with Projection 168 (fast cycle)
python scratch/app/src/simulation.py --env LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX --months 24 --seed 99 --projection-id 168
```

**Verification Queries:**
```sql
-- Turnover work order summary per run
SELECT RunID, COUNT(*) AS Total,
       SUM(CASE WHEN Status='COMPLETED' THEN 1 ELSE 0 END) AS Completed,
       SUM(CASE WHEN Status='SKIPPED' THEN 1 ELSE 0 END) AS Skipped,
       SUM(CASE WHEN Status IN ('PENDING','IN_PROGRESS') THEN 1 ELSE 0 END) AS InFlight
FROM simulation.TurnoverWorkOrder
GROUP BY RunID ORDER BY RunID;

-- Restoration durations by termination type (sanity check on eviction vs voluntary range)
SELECT TerminationType, WasEviction, COUNT(*) AS N,
       MIN(DurationDays) AS MinDays, MAX(DurationDays) AS MaxDays, AVG(DurationDays * 1.0) AS AvgDays
FROM simulation.TurnoverWorkOrder
WHERE WorkItemType='RESTORATION' AND Status<>'SKIPPED'
GROUP BY TerminationType, WasEviction;

-- Final unit status distribution (after sim run)
SELECT UnitStatus, COUNT(*) AS N
FROM simulation.PropertyUnits WHERE RunID=<id>
GROUP BY UnitStatus;
```

**Expected Results:**
- Eviction-driven turnovers: `RESTORATION.DurationDays` ∈ [15, 30]
- Voluntary-driven turnovers with no damage: `RESTORATION.Status='SKIPPED'`, `DurationDays=0`
- Voluntary-driven turnovers with damage: `DurationDays` ∈ [10, 30]
- Units that complete turnover: `UnitStatus='Available'`
- Units mid-turnover: `UnitStatus='Turnover'`
- Units in eviction proceedings: `UnitStatus='Pending_Move_Out'`

**Key Validation:**
- Sequential work item progression (NOT EXISTS gate prevents parallel work items within a unit)
- Eviction always requires RESTORATION (rule: `was_eviction or damage_withheld > 0`)
- Settlement state must exist before turnover trigger (RuntimeError if missing)
- Same seed produces identical RESTORATION durations (determinism)

---

### Phase3_7_10/ (Python regression artifact)

**Created:** 2026-05-14
**Protects:** Phase 3.7.10 (Simulation Snapshot Instrumentation)
**Format:** Python test script (not SQL).

**What It Tests:**
- Snapshot rows exist at expected quarterly boundaries
- Monotonicity of SnapshotDate, MonthIndex, TerminationsCumulative, TurnoversCompleted
- Internal consistency: UnitsOccupied + UnitsVacant + UnitsInTurnover <= UnitsTotal
- Stored derived fields: TotalFundBalance == CashBalance + CSFBalance + EIPBalance
- OccupancyRate and VacancyRate in [0,1]
- Backfill parity context (LIVE vs BACKFILL on shared keys)

**Contents:**
- `phase_3_7_10_test_snapshots.py` — 9 query-based snapshot validation tests
- `README.md` — re-run instructions, snapshot field reference, common query patterns

**How to Run:**
```bash
# Run a simulation (snapshots fire automatically at quarterly boundaries)
python app/src/simulation.py --env <test_db> --months 36 --projection-id 167 --seed 99

# Optionally backfill an existing run
python app/src/backfill_snapshots.py --env <test_db> --run-id <N>

# Run snapshot tests
python sql/regression_tests/Phase3_7_10/phase_3_7_10_test_snapshots.py --env <test_db>
```

**Expected:** `Result: 9/9 tests passed`

---

### Phase3_7_9_Integration_Tests.sql + Phase3_7_9/

**Created:** 2026-05-14
**Protects:** Phase 3.7.9 (Integration Testing) + closeout of all Phase 3.7
**Test Projections:** 5 projections (IDs 169-173)
**Companion artifact:** `Phase3_7_9/phase_3_7_9_test_lifecycle.py` (8 lifecycle acceptance tests)

**What It Tests:**
- End-to-end Phase 3.7 lifecycle: lease creation → rent → termination → deposit settlement → turnover → vacancy → re-lease
- All four termination types (EVICTION, VOLUNTARY, EARLY_BREAK, LANDLORD_NONRENEWAL)
- Both deposit methods (FULL, INSTALLMENT)
- Deterministic replay (same seed + same projection → identical behavior-relevant state across two runs)
- Long-horizon inflation drift
- Multi-cohort parallel turnovers
- State-machine completeness

**Test Scenarios:**

| ProjectionID | Name | Termination Bias | Purpose |
|--------------|------|------------------|---------|
| 169 | Phase3.7.9_Voluntary_Lifecycle | Voluntary (low fail, low renewal) | VOLUNTARY paths + mixed deposit/damage |
| 170 | Phase3.7.9_Eviction_Lifecycle | Eviction (high fail) | EVICTION paths + unfunded forfeitures |
| 171 | Phase3.7.9_Mixed_Long_Horizon | Mixed | Primary 60-month acceptance run |
| 172 | Phase3.7.9_Deterministic_Replay | Mixed (matches 171) | Replay validation across two runs |
| 173 | Phase3.7.9_Stress_Edge_Cases | Mixed + forced damage extremes | Boundary behavior + fast settlement |

**How to Run:**
```bash
# Apply projections + run primary acceptance simulation
sqlcmd -S localhost -d <test_db> -i sql/regression_tests/Phase3_7_9_Integration_Tests.sql
python app/src/master_test_runner.py --env <test_db> --months 60 \
    --projection-id 171 --seed 99 --clean

# Run lifecycle acceptance tests
python sql/regression_tests/Phase3_7_9/phase_3_7_9_test_lifecycle.py --env <test_db>
```

**Expected:** `Result: 8/8 tests passed`

See `Phase3_7_9/README.md` for the deterministic replay procedure and the optional 240-month smoke-test guidance.

---

### Phase3_7_8/ (Python regression artifact)

**Created:** 2026-05-14
**Protects:** Phase 3.7.8 (Vacancy Creation & Re-Leasing) + two bundled prerequisite repairs (UnitStatus state machine, Phase 3.6 deposit installment math)
**Format:** Python test script (not SQL). Retained outside `app/src/` per project convention of keeping test files out of production source.

**What It Tests:**
- Vacancy duration RNG distribution (truncated exponential, mean ~30, cap 120, floor 1)
- Same-seed reproducibility
- Floor/cap enforcement at the RNG layer
- DB-level idempotency (no duplicate open Vacancy rows)
- Hard fill-date gate (no fills before `TargetFillDate`)
- Phase 3.6 deposit installment math (no constraint violations under arbitrary rent values)

**Contents:**
- `phase_3_7_8_test_vacancy_releasing.py` — 8 targeted scenarios
- `README.md` — re-run instructions + documentation of the two prerequisite repairs bundled in Phase 3.7.8

**How to Run:**
```bash
# Run a multi-month simulation first to populate test DB
python app/src/master_test_runner.py --env <db_name> --months 36 --projection-id 167 --seed 99 --clean

# Run the targeted tests
python sql/regression_tests/Phase3_7_8/phase_3_7_8_test_vacancy_releasing.py --env <db_name>
```

**Expected:** `Result: 8/8 tests passed`

**SQL companion** (`Phase3_7_8_Vacancy_Tests.sql`): TBD — projection-driven scenarios deferred to post-promotion. May be added alongside this folder when written.

---

## Future Regression Tests

_Space reserved for additional regression tests as phases are completed._

**Suggested Future Tests:**
- Daily payment timing distribution (Phase 3.7.4 follow-up)
- Late fee calculation under varied grace periods
- Post-termination actual payment collection
- Phase 3.7.8 SQL projection (IDs 169-173) — companion to the Python artifact
- Tenant Credit System integration (Phase 3.8)

---

## Maintenance Guidelines

**When to Add Regression Tests:**
1. After fixing a critical bug (create test that would have caught it)
2. After implementing complex business logic (stress test edge cases)
3. When BA provides extreme scenario requirements
4. Before making architectural changes (baseline behavior validation)

**How to Maintain:**
1. Update this INDEX.md when adding new scripts
2. Document expected results for deterministic tests
3. Note which phases are protected
4. Keep test projections separate from production IDs (use 150+)

**Naming Conventions:**
- Use `PhaseX_Y_Z_Description.sql` format
- Avoid special characters (`%`, `&`, spaces)
- Use `pct` instead of `%`
- Use underscores for readability

---

**Last Updated:** 2026-05-14
**Total Scripts:** 7 (4 SQL + 3 Python regression artifact folders)
