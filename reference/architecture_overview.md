# Liberty Bee — Architecture Overview (V0.2)

**Status:** ✅ Canon (X.5.3) — extracted 2026-06-09 from the V1 Code Walk inventory (`docs/v_0_2/inventory/`: `00_walk.md`, `INDEX.md`, 23 module docs, `code_db_index.json`) by a fact-only sweep.
**Scope:** What the engine *is* — modules, execution flow, data-layer pattern, entry points, current drift state. Rules live in [`business_rules_current.md`](business_rules_current.md); the why in [`concept_and_philosophy.md`](concept_and_philosophy.md); per-module depth in `docs/v_0_2/inventory/modules/`.

> The Code Walk this was extracted from is a point-in-time V0.1 snapshot. The **KD-027/028/029 bundle is now promoted** to `app/src` and the policy surface is **registry-driven** (Phase 1.2.3, migrations through V00043); **KD-030 is fixed** (1.2.4a). The module notes below are updated to current state; a publication-grade re-baseline is still owed (1.2.4c).

---

## 1. Shape of the system

One CLI invocation = one complete simulation run: `python app/src/simulation.py --env <db> --projection-id <N> --seed <S> --months <M>` (default 240 months ≈ 7,300 **daily** ticks — the loop is daily, with monthly/quarterly/annual hooks). `--debug` enables the tracer.

**17 managers** orchestrated by `simulation.py` (7 eager at construction, 10 lazy at init), 4 infrastructure modules, 3 standalone tools. All randomness derives from the **run seed** (never RunID — Phase 3.10.4); per-manager RNGs use seed offsets (renewal +30901, vacancy 30708, …) or content-hash seeding (compliance work items, order-independent by design).

## 2. Module catalog

### Orchestration / infrastructure
| Module | Purpose |
|---|---|
| `simulation.py` | Entry point + orchestrator — the only place that knows global sequencing. Hosts the KD-027 staffing re-trigger (post-acquisition `check_staffing_needs`) + the KD-028 month-end OpEx charge (both promoted, 1.2.2). |
| `database_manager.py` | All DB access (pyodbc, trusted auth, per-call connections); 4 primitives incl. `execute_insert_returning` for event causal chains. |
| `event_logger.py` | Canonical event emission — every manager action → 1 `simulation.Event` row (~50K+/run); `parent_event_id`/`causal_tag` chains. |
| `configuration_loader.py` | Read-side config: `load_projection()` → `ProjectionConfig`, read from the three-table parameter store (**`reference.Projection`** + **`ParameterRegistryDefault`** + **`ParameterRegistryDefined`**; read-once + fail-loud, EAV since 1.2.3b, split at V00071). Projection **existence** is a row in `reference.Projection` — previously it was inferred from the presence of a `SIM.ProjectionName` parameter row, which conflated identity with configuration. The legacy `reference.ProjectionParameters` wide table was **dropped at V00070**; `load_employee_roles()` → `EmployeeRole`. **Not a *single* gateway** — `employee_manager` also reads `reference.EmployeeRole` directly (`BaseSalary`/`SalaryCap`, the raise/cap path; 03 Pass-2). GRT_* RESERVED (KD-022). |
| `run_manager.py` | `simulation.Run` lifecycle; `RunType` = Simulation \| ComponentTest. |
| Tools | `master_test_runner.py` (~76 regression tests/11 suites, pre-promotion gate); `debug_tracer.py`; `backfill_snapshots.py`. |

### Money & macro
| Module | Purpose |
|---|---|
| `fund_manager.py` | Owns the buckets (Cash + CSF live; EIP scaffolded-unused ⚠️KD-022) + CashHold + Escrow. Every monetary action = one typed method = one `FundLedger` row (most-written table). Balances = `TOP 1 ORDER BY LedgerDate DESC, EventID DESC`. `process_csf_topup` (month-end, full-deficit); `process_expense_protected` (Cash-first, CSF-fallback — the protected-obligation path, §4 of the rules). |
| `inflation_engine.py` | One-shot pre-sim generation of the 240-row `InflationSchedule`; V1 locked `Static` (rent 3 / OpEx 2.5 / property 3 / general 2% annual ÷ 12); dormant during the loop. |
| `employee_manager.py` | Staffing (registry-driven, MIXED driver #100: maintenance per-PROPERTY — 0 FTE below `MaintCrossoverProperties=15`, fully contracted; admin per-UNIT = `max(base, ceil(units/threshold))` post-KD-030) + bi-monthly payroll (15th + last, salary÷24, via protected path) + annual cash-reserve-tiered raises (Dec 31). KD-027 fixed (`check_staffing_needs` re-triggers post-acquisition); KD-029 fixed (raise tiers 4/3/2.5/2% + `SalaryCap` clamp + honest denominator). |
| `maintenance_event_manager.py` | #100: seeded (offset 30910) monthly maintenance-event draws — negative-binomial routine counts (Gamma-Poisson mixture) × lognormal severity; low-rate Poisson major events × fat-tail lognormal (carries capital replacement — no separate reserves line, lb-ba Q6 "lumpy events"). Expected-value methods feed the EXPECTED OpEx basis (CSF target/gate); realized draws feed the single month-end protected charge. |

### Property side
| Module | Purpose |
|---|---|
| `property_market_manager.py` | Market pool: seeds 1 row per `reference.Properties` (1,673 props / 3,847 units), daily price drift, probabilistic by-other sales, `MarketStatus` machine. |
| `property_acquisition_manager.py` | Growth engine: 3-rule gate (+6-mo CSF grace), Offer→Response→Inspection→Negotiation→Closing state machine, CashHold reservations, tiered concurrent-pipeline cap (month-1/3/12 ramp), scoring `income/list_price` w/ below-market + vacancy adjustments. |
| `compliance_manager.py` | Post-acquisition inspections/remediation (`ComplianceAttempt`/`WorkItem`/`Step`); gates units `Compliance_In_Progress` until clear; costs are REAL Cash debits via `process_expense` (Cash-only, never CSF — F-01 fix 2026-07-03; the pre-fix engine computed but never charged them, ~$476k/240-mo). Tables are V1_ACTIVE_WORKER_ONLY (not in the central `v1.*` extract — documented reviewability gap, not a bug). |

> **Property universe / data provenance:** `reference.Properties` (1,673) + `reference.Units` (3,847) are **real scraped listings**, **multi-family only (2–8 units) by design** — single-family / condos / townhouses are discovered upstream but excluded at the clean-load step (the engine models multi-family operations: per-unit rent, per-unit staffing & maintenance). Built offline by the parked scraper (Zillow tile-discovery → Redfin enrichment → local-LLM unit reconciliation → load → pricing); the engine **never scrapes at runtime** — it reads these frozen reference tables (carried in the `LibertyBeeGold` backup). Full case: [`realestate/README.md`](../../../realestate/README.md). **Geography, honestly (audit F-19, DB-verified):** scraping *targeted* 6 Salem-area MA towns, but the loaded catalog carries **no per-row City/State/Zip (100% NULL)**; where the address parses to a town it is **Salem 1,671 / Peabody 2** — treat this as a **Salem catalog**, not a 6-town sample. Known raw-catalog quirks a fork should know: **22 units carry discounted rents** (the frozen V0.2 inventory doc's “no discounted units” claim is wrong on this point), a handful of 6BR/8BR outlier units sit beyond the usual tier range, and 4 units record zero baths.

### Tenant side
| Module | Purpose |
|---|---|
| `tenant_manager.py` | Inflow: vacancy detection → daily applicant slates → 3-stage qualification (pre-screen / bedroom-fit / 30% rule) → Household/Person/Lease creation. New-lease rent `BaseRent × (1−10%) × cum_inflation`. |
| `rent_collection_manager.py` | Day-1 `MonthlyPaymentStatus` + payability roll (0.02), daily collection w/ grace + late fees, month-end MISSED finalization; updates `Lease.ConsecutiveMissedPayments` (THE eviction trigger). |
| `lease_renewal_manager.py` | Lease-end pre-roll (−30d): LNR check → **retention-modulated** voluntary exit (Phase 1.10, via `retention_model.py`; replaces the flat 80/20 coin-flip); monthly early-break hazard (0.25%). |
| `retention_model.py` | Phase 1.10 (KD-041/#167): voluntary-exit prob = `clamp(base_exit·(1−β·effective_discount)·(1−γ·external_scarcity), floor_exit, base_exit)` — deal (below-market+tenure) × regional difficulty-of-leaving (vacancy × affordability). **Owns no RNG** — `lease_renewal_manager` draws the single `renewal_rng.random()` and compares. Reads `InflationSchedule` (rent/wage factors + regime vacancy) once per pre-roll batch. |
| `eviction_manager.py` | Deterministic eviction at 3 consecutive MISSED (file month-end +3, execute +2 mo, $1,500 Cash). |
| `turnover_manager.py` | 5-work-item turnover orders; `Turnover → Available`; EventID back-fill ("Option B") sequencing. |
| `rent_reduction_manager.py` | Tenure tiers → `EffectiveMonthlyRent = MonthlyRent × (1 − CumulativeRentReductionPct)` (cumulative-off-original, unit-scoped clock). **Canon = 3 tiers** (m36 5% / m72 +5% / m120 +10% → 20% max). ✅ **KD #104 resolved** (Phase 1.4, V00051): the unintended 4th-tier artifact retired entirely; slots 4+5 uniformly absent-reserved (absence = inactive). |
| `security_deposit_manager.py` | Deposit lifecycle: collection→escrow, settlement at termination (30-day delayed return), forfeit paths. |
| `tenant_credit_manager.py` | TCS: ON_TIME accrual (deposit-funded gate, **current-balance cap — refillable**, $15K), routine/hardship/FME redemption, param-governed portability (`TCS.PortabilityYears`=2 → 24 mo, KD-023 fixed), eviction forfeiture. |
| `snapshot_manager.py` | Quarterly `RunSnapshot` aggregates (Mar/Jun/Sep/Dec). |

*(3 legacy modules — `onboarding_manager`, `data_loader_corrected`, `database_schema` — are ORPHAN_ARCHIVED in `app/src_legacy/`.)*

## 3. Execution flow

- **Init:** parse args → instantiate managers → create `Run` row → load config.
- **Pre-loop:** `initialize_funds_and_staff()` (fund seed + core hires) → generate full inflation schedule → seed property market → start run. *(KD-027 fixed: staffing also re-triggers post-acquisition in the daily loop, not only here.)*
- **Daily loop (order matters):** market tick → compliance → acquisition pipelines/opportunities → tenant processing → rent collection (1st) → lease renewal (−30d window) → eviction → turnover → rent reduction.
- **Monthly:** payroll 15th + last; pipeline-cap tier checks. **Quarterly:** snapshot. **Annual:** cash-tiered raises (Dec 31); CSF target recompute.
- **End:** loop to `end_date` (or bankruptcy halt) → close `Run`.
- **Sweep mode only** (`scratch/analysis/phase_3_10_6_sweep_driver.py`): atomic central extract — 12 `INSERT…SELECT`s from the worker DB into `v1.*` (tagged Rung, Seed) → drop worker. Not all tables extract (compliance trio, payroll, deposits, etc. stay worker-only).

## 4. Data-layer pattern

- **Ledger vs state:** append-only ledgers (`FundLedger`, `TenantCreditLedger`, `LeaseDepositLedger`, `LeaseTerminationLedger`, `ComplianceStep`, `RentCollection`, `Payroll`, `Event`, …) vs in-place state tables (`Lease`, `PropertyUnits`, `TenantCreditBalance`, `MonthlyPaymentStatus`, attempt tables).
- **Event chain:** every action logs a `simulation.Event`; ledger rows carry `event_id`. (`ParentEventID` was dropped at V00054 — audit F-13: no caller ever wrote it, so there never was a parent-chain in practice; causality lives in `CausalTag` + metadata.)
- **Run isolation:** every simulation table keyed by RunID; engine-managed per-run IDs (no IDENTITY) for Attempt/WorkItem/Lease/Household/etc. *(Known divergences: TCS tables KD-005/006/007 break the pattern.)*
- **Audit-trail columns (written, not yet read — deliberate):** `Event.EntityType/EntityID/Currency`, `TurnoverWorkOrder.DamageWithheldAmount`, four `RunSnapshot` month-metrics, and `InflationSchedule.GeneralRate/ScenarioType/ScenarioPhase/Notes` carry real data with no in-engine reader — they exist for post-hoc analysis/BI (audit F-13: "build the reader or document" — documented here). `RunSnapshot.EvictionsCumulative` + `TurnoverWorkOrder.WasEviction` are live eviction-visibility channels that read 0 on any seed where no eviction fires — empty ≠ dead (Pass-5 erratum).
- **Worker/central split:** worker DBs hold full `simulation.*`; the central baseline DB holds the tagged `v1.*` subset.

## 5. Entry points

| | |
|---|---|
| `app/src/simulation.py` | run one simulation |
| `app/src/master_test_runner.py` | regression suite (`--env <db> --clean`) |
| `environmentscripts/migration_manager.py` | create ephemeral test DB (restore Gold → migrate → `db_config.json`) |
| `scratch/analysis/phase_3_10_6_sweep_driver.py` | Monte Carlo sweep + central extract |

## 6. Engine promotion + drift state

The scratch-as-shadow model is **retired** (X.4.5); engine code lives in `app/src/` on branches, promoted via PR. The **KD-027/028/029 bundle is promoted** (Phase 1.2.2; migrations through **V00043**) and the full policy surface is **registry-driven + fail-loud** (1.2.3); **KD-030 is fixed** (1.2.4a). `scratch/app/src/` is **kept as a comparison safety-net** through the re-baseline and is **deleted in 1.2.5** (the X.4.5-deferred `--scratch` removal rides that step) — it is no longer a promotion source. The 588-sim V1 baseline remains **🟡 SUPERSEDED** pending the publication re-baseline (1.2.4c → `gold-v0.3`); the model's closing behavior under honest costs is what that re-baseline characterizes (early finding: the $8M rung survives at the corrected 2-admin staffing).

---
*Sources: `docs/v_0_2/inventory/` (V1 Code Walk). Per-module detail: `inventory/modules/app/src/<module>.md`. Schema depth: Technical Bibles + `Bible_Drift_Log.md` (with the caveat in business_rules §Sources).*
