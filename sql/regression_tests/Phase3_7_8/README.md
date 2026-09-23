# Phase 3.7.8 — Vacancy Creation & Re-Leasing: Regression Artifacts

**Phase:** 3.7.8 — Vacancy Creation & Re-Leasing
**Promotion date:** 2026-05-14
**Production code:** `app/src/{property_acquisition_manager,compliance_manager,tenant_manager,security_deposit_manager}.py`, `sql/migrations/V00023__add_vacancy_target_fill_date.sql`

---

## Contents

| File | Purpose |
|---|---|
| `phase_3_7_8_test_vacancy_releasing.py` | 8 targeted Python tests covering RNG distribution, reproducibility, floor/cap, idempotency, hard gate, and deposit math. Retained here (not in `app/src/`) to keep production source clean while preserving the proof surface. |

A SQL regression projection file (`Phase3_7_8_Vacancy_Tests.sql`) may be added later to exercise full-lifecycle scenarios alongside the existing Phase 3.7.5-3.7.7 projections.

---

## How To Re-Run The Python Tests

```bash
# Create or reuse an ephemeral test database
python environmentscripts/migration_manager.py

# Run a multi-month simulation against a mixed-termination projection
# (this populates the data the DB-level tests need to inspect)
python app/src/master_test_runner.py --env LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX \
    --months 36 --projection-id 167 --seed 99 --clean

# Run the targeted Phase 3.7.8 tests
python sql/regression_tests/Phase3_7_8/phase_3_7_8_test_vacancy_releasing.py \
    --env LibertyBee_Test_YYYYMMDD_HHMMSS_XXXXXX
```

Expected output: `Result: 8/8 tests passed`.

T1-T4 are pure RNG checks (don't need DB). T5, T7, T8 inspect the simulation database. T6 is a placeholder — the deterministic-ordering property is fully covered by the 36-month simulation reproducibility check, but a dedicated projection-driven test can be added if regressions appear.

---

## Two Prerequisite Repairs Bundled in Phase 3.7.8

Both were uncovered by the verification gates in the BA-approved plan and bundled into Phase 3.7.8 with explicit BA approval. Documented here so future maintainers understand the scope of changes that landed under this phase number.

### Repair 1: `PropertyUnits.UnitStatus` State Machine

**The defect:** The `CHK_PropertyUnits_UnitStatus` CHECK constraint allowed 6 values (`Compliance_In_Progress`, `Under_Contract`, `Available`, `Occupied`, `Pending_Move_Out`, `Turnover`). Only 3 were ever written by the codebase. Initial state (`Compliance_In_Progress`) and lease state (`Occupied`) were never set — units defaulted to `Available` at acquisition and stayed that way through compliance and leasing. The `UnitStatus` column was decorative for the initial lease-up lifecycle.

**Why it had to be repaired in Phase 3.7.8:** The BA-approved unified vacancy detection (`UnitStatus = 'Available' AND no open Vacancy AND no active Lease`) cannot work correctly until `UnitStatus` reflects real unit lifecycle state. Without the repair, freshly-acquired-but-not-yet-compliance-ready units would have created premature Vacancy rows.

**What changed:**
- `property_acquisition_manager.py` INSERT now writes `UnitStatus = 'Compliance_In_Progress'` explicitly. (The DEFAULT constraint remains `'Available'` but is now inert.)
- `compliance_manager._check_and_emit_unit_ready` UPDATEs `UnitStatus = 'Available'` when `UnitReadyDate` is set. Bridges ref↔sim UnitID space via `reference.Units` ↔ `simulation.Properties.AddressID` ↔ `PropertyUnits.Unit` string.
- `tenant_manager._create_lease_record` UPDATEs `UnitStatus = 'Occupied'` on lease creation.

**Authoritative BA trail:**
- `docs/phases/phase_3_7/phase_3_7_8_v2_verification_escalation.md` — escalation finding
- `docs/phases/phase_3_7/phase_3_7_8_v_2_escalation_ba_response.md` — Option A approved

---

### Repair 2: Phase 3.6 Deposit Installment Math

**The defect:** `security_deposit_manager.process_installment_payment` computed `installment_amount = monthly_rent / 4` and incremented `InstallmentsPaidCount` once per installment. The implicit assumption was that `4 × installment` (at DECIMAL(18,2) precision) equals `monthly_rent`. Under arbitrary rent values, residual cents accumulate, the deposit isn't funded after 4 installments, a 5th installment fires, and the `CHK_InstallmentsPaidCount` constraint (`<= InstallmentsPlanned = 4`) is violated.

**Why it surfaced now:** Phase 3.7.8 introduced BA §9 Outcome B rent inflation (`BaseRent × cumulative_rent_inflation_factor`). The resulting rents (e.g., $2,927.85) don't divide cleanly by 4 cents. Pre-3.7.8, all rents were `0.90 × BaseRent` of typically 4-divisible BaseRents, so the bug was latent.

**What changed:** Final installment now pays the exact remaining balance instead of a hardcoded quarter. Total still exactly equals `DepositRequiredAmount`. Business rule (`InstallmentsPlanned = 4`) unchanged.

```python
remaining_balance = deposit_required_amount - deposit_escrowed_amount
remaining_installments = 4 - installments_paid_count

if remaining_installments == 1:
    installment_amount = remaining_balance
else:
    installment_amount = (deposit_required_amount / Decimal("4")).quantize(Decimal("0.01"))
```

**Authoritative BA trail:**
- `docs/phases/phase_3_7/phase_3_7_8_rent_inflation_escalation.md` — escalation finding
- `docs/phases/phase_3_7/phase_3_7_8_rent_inflation_escalation_BA_response.md` — Option A approved

---

## Related Phase Documentation

For the complete Phase 3.7.8 paper trail, see `docs/phases/phase_3_7/`:
- `phase_3_7_8_vacancy_releasing_final_plan.md` — canonical implementation plan
- `phase_3_7_8_final_ba_handoff.md` — binding BA decisions
- `phase_3_7_8_post_implementation_report.md` — final results summary

---

**Maintained as part of Phase 3.7.8 acceptance surface. Do not delete without explicit BA sign-off.**
