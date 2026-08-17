# Phase 3.7.9 — Integration Testing: Regression Artifacts

**Phase:** 3.7.9 — Integration Testing (closeout of Phase 3.7)
**Promotion date:** 2026-05-14
**Production code:** No new production code — Phase 3.7.9 is formalization of existing behavior

---

## Contents

| File | Purpose |
|---|---|
| `phase_3_7_9_test_lifecycle.py` | 8 query-based tests covering lease counts, termination records, turnover terminal states, hard fill-date gate, deposit installment math, deposit funding, state-machine coverage, and inflation visibility |
| `../Phase3_7_9_Integration_Tests.sql` | 5 SQL projections (IDs 169-173) — Voluntary lifecycle, Eviction lifecycle, Mixed long horizon, Deterministic replay, Stress edge cases |

---

## How To Re-Run

```bash
# Create an ephemeral test database
python environmentscripts/migration_manager.py

# Apply Phase 3.7.9 projections
sqlcmd -S localhost -d <test_db> -i sql/regression_tests/Phase3_7_9_Integration_Tests.sql

# Run the primary acceptance simulation (60 months on projection 171)
python app/src/master_test_runner.py --env <test_db> --months 60 \
    --projection-id 171 --seed 99 --clean

# Run the lifecycle acceptance tests
python sql/regression_tests/Phase3_7_9/phase_3_7_9_test_lifecycle.py --env <test_db>
```

Expected output: `Result: 8/8 tests passed`.

**Note on simulation horizon:** the simulation may halt before the requested 60 months due to portfolio cash dynamics. This is expected — cash depletion is realistic behavior, not a Phase 3.7 bug. The lifecycle tests are designed to be valid against whatever horizon the simulation actually reaches.

---

## Deterministic Replay Test

To verify the determinism guarantee:

```bash
# Create two ephemeral DBs
python environmentscripts/migration_manager.py  # -> DB_A
python environmentscripts/migration_manager.py  # -> DB_B

# Apply projections to both
sqlcmd -S localhost -d <DB_A> -i sql/regression_tests/Phase3_7_9_Integration_Tests.sql
sqlcmd -S localhost -d <DB_B> -i sql/regression_tests/Phase3_7_9_Integration_Tests.sql

# Run identical simulation on both
python app/src/master_test_runner.py --env <DB_A> --months 60 --projection-id 172 --seed 99 --clean
python app/src/master_test_runner.py --env <DB_B> --months 60 --projection-id 172 --seed 99 --clean

# Diff behavior-relevant state via cross-database SQL EXCEPT queries
# (see phase_3_7_acceptance_summary.md or the original session log for the query)
```

Expected: 0 row differences across Lease, Vacancy, LeaseDeposit, and TurnoverWorkOrder when projecting to behavior-relevant columns.

---

## Coverage Matrix

See `docs/phases/phase_3_7/phase_3_7_9_coverage_matrix.md` for the 20-cell map across termination type × deposit method × damage state × vacancy fill timing, plus deterministic replay, long-horizon drift, multi-cohort, and state-machine completeness cells.

---

## Annual / Pre-Investor Smoke Test

Per Kate's BA guidance: *"Run manually before major milestone releases or investor-facing analysis refreshes."*

```bash
python app/src/master_test_runner.py --env <test_db> --months 240 \
    --projection-id 171 --seed 99 --clean
```

This is optional and NOT part of the automated acceptance suite. Use it before any externally-facing deliverable to validate long-horizon stability.

---

## Related Phase Documentation

- `docs/phases/phase_3_7/phase_3_7_9_integration_testing_implementation_plan.md` — plan
- `docs/phases/phase_3_7/phase_3_7_9_integration_testing_BA_answer.md` — BA decisions
- `docs/phases/phase_3_7/phase_3_7_9_coverage_matrix.md` — scenario coverage
- `docs/phases/phase_3_7/phase_3_7_9_post_implementation_report.md` — final results
- `docs/phases/phase_3_7/phase_3_7_acceptance_summary.md` — canonical Phase 3.7 acceptance reference

---

**Maintained as part of Phase 3.7.9 acceptance surface. Do not delete without explicit BA sign-off.**
