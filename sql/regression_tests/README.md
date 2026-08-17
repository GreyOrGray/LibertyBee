# Regression Test Suite

Behavioral regression tests for the Liberty Bee simulation engine — unit assertions
(`get_csf_target` rounding, …) + integration assertions read against a simulated run
(TCS accrual/redemption/portability, CSF reserve discipline, deposit settlement,
turnover, vacancy, rent reductions, determinism, snapshots, …).

Each test module exposes a `--env <db> --assert` CLI that exits **0** on all-pass / **1**
on any failure. The suite **runner** (`run_suite.py`) populates an ephemeral DB with a
baseline simulation, discovers every test module, runs each `--assert`, and reports a
pass/fail summary.

---

## How to run

```bash
# 1. mint an ephemeral DB (Gold + migrations)
python environmentscripts/migration_manager.py --label regr

# 2. run the whole suite (populates with a baseline sim, then runs every module)
python sql/regression_tests/run_suite.py --env <LibertyBee_Test_...> --populate \
    --projection-id 206 --months 60 --seed 99

# run a single module directly (R1-style unit checks run without --env)
python sql/regression_tests/Phase3_9_2/phase_3_9_2_test_csf_target_rounding.py --env <db> --assert
```

The runner exits non-zero if any module fails, so it can gate a promotion once green.

### Stress sweep (registry-native stress projections, #106)

Beyond the baseline (206), the suite runs against three **registry-native stress
projections** (seeded by `sql/migrations/V00046`) that exercise the eviction /
deposit-settlement / turnover paths the baseline barely touches:

```bash
# each runs the FULL suite against one stress projection (clean-room per run)
python app/src/master_test_runner.py --env <db> --clean --regression --reg-projection 220  # pay-fail / eviction
python app/src/master_test_runner.py --env <db> --clean --regression --reg-projection 221  # deposit damage
python app/src/master_test_runner.py --env <db> --clean --regression --reg-projection 222  # voluntary turnover
```

| Proj | Override | Stresses | Suite result |
|---|---|---|---|
| 220 | `PAY.BaseFailProbMonthly` → 0.40 | arrears, evictions, eviction-turnover, forfeiture | **21/22** — only `Phase3_8_3` T5, which correctly catches **[#120](https://github.com/GreyOrGray/LibertyBeeDev/issues/120)** (a real TCS eviction-forfeiture bug). Expected tripwire until #120 is fixed. |
| 221 | `DEP.*DamageProbability` → 1.0 | deposit damage / PARTIAL_RETURN | 22/22 |
| 222 | `LEASE.RenewalRatePct` → 20 | voluntary turnover + re-leasing churn | 22/22 |

A stress sweep that is always green isn't stressing anything — its value is surfacing
real findings (e.g. #120). Do **not** weaken a correct test to force a stress projection
green; file the finding instead.

---

## Current state (V0.2 — 2026-06-23)

Built in V0.1 (Phase 3.x), **rehabilitated for the V0.2 engine** under
[#106](https://github.com/GreyOrGray/LibertyBeeDev/issues/106).

**✅ 22/22 test modules green** — clean-room validated on a fresh single-run DB
(proj 206, 60 mo, seed 99), and wired into `master_test_runner --regression` as the
automated promotion gate. The rehabilitation brought every module green, and a key
finding: **every failure was a stale TEST, not an engine bug** — the engine was correct
in all cases. Fixes followed one principle: **scenario-agnostic** — derive expectations
from the run's actual config / horizon, never hardcode a scenario snapshot.
(`phase_3_7_11_tenant_pipeline_funnel.py` is a diagnostic report with no pass/fail and is
excluded from the suite.)

The 19 rehabilitated modules were joined by **3 ported from `scratch/` onboarding tests**
(closing real gaps the outcome-level modules missed):
- `Phase3_4_4` applicant-generation determinism (RNG seed includes current_date — the 3.7.11.2 fix);
- `Phase3_4_5` candidate-qualification gates (bedroom-fit max = bedrooms×2 / studio 2; 30%-income rule) — pins **current** behaviour; the bedroom cap is flagged as a mission-tension / fair-housing concern in [#118](https://github.com/GreyOrGray/LibertyBeeDev/issues/118) (rejects larger families from smaller units), so Q1 is a tripwire, not an endorsement;
- `Phase3_4_6` lease-date invariants (start/end first-of-month; term a positive multiple of the registry standard term — initial + renewal extensions), a read-only invariant on real leases (the V0.1 source rebuilt leases destructively).

Representative fixes:
- post-#99 reserve-first CSF semantics — CSF may sit below the committed target (`Phase3_9_2` R2);
- the run's ACTUAL horizon (`MAX(BillingMonth)`) vs the projection's configured 240-mo end (`Phase3_9_3` R3);
- balance-capped redemptions are legitimately < a month's rent (`Phase3_9_3` R9);
- `properties_per_maintenance`→`units_per_maintenance` rename (`Phase3_9_3` E1);
- long-horizon #99 facets reframed as scenario-agnostic gate/growth invariants (`Phase3_9_1` D8/D9);
- `--assert` CLI standardization + latest-run scoping (the Phase 3.7 set);
- state-machine-*exercised* vs end-state-*diversity* (`Phase3_7_9` L7).

**Done (#106 follow-ups — closes #106):**
- ✅ **Wired `run_suite.py` into `master_test_runner.py`** as the automated promotion gate
  (`master_test_runner --env <db> --clean --regression`) — PR #117.
- ✅ **Triaged the 3 scratch onboarding tests** — all three covered real gaps and were ported
  (see `Phase3_4_4/4_5/4_6` above); none discarded.
- ✅ **Registry-native stress projections** (the "Stress sweep" above) — replaced the dead V0.1
  SQL seeders (which `INSERT`ed projections 157–173 into the retired `reference.ProjectionParameters`
  wide table) with `V00046` registry overrides (220/221/222). Along the way the sweep exposed and
  fixed two latent couplings: **3 modules still read `reference.ProjectionParameters`** (`Phase3_8_1`,
  `Phase3_9_4`, `Phase3_10_2`) → re-pointed to the registry (#75 — they crashed on registry-native
  projections + would break at the V0.3 ProjectionParameters drop); and **`Phase3_9_6` T8** was a
  scenario-dependent threshold → reframed as a small-N-skipping gross-degeneracy guard. The sweep
  also surfaced **#120** (a real TCS eviction-forfeiture bug; `Phase3_8_3` T5).

**Still pending:**
- The per-test sections in [INDEX.md](INDEX.md) are **V0.1-era** (stale `How to Run` paths);
  this README is the current authority.

---

## Adding a test

1. New module under `sql/regression_tests/PhaseX_Y/`, exposing `--env <db> --assert`
   (exit 1 on any fail), printing a final `Result: <n>/<total> tests passed` line.
2. Keep test projections in the **registry** (`reference.ParameterRegistry`), not the
   legacy `reference.ProjectionParameters` wide table.
3. The runner auto-discovers it — no registration needed.
