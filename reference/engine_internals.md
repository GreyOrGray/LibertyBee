# Liberty Bee — Engine Internals (how a run actually executes)

**Audience:** a developer who needs to know *what runs, in what order, reading and writing what* — without reverse-engineering `simulation.py`. Companion to [`users_guide.md`](users_guide.md) (the readable "how it works"), [`data_dictionary.md`](data_dictionary.md) (the tables), [`parameter_reference.md`](parameter_reference.md) (the knobs), and [`architecture_overview.md`](architecture_overview.md) (the static code map).

> **Build status:** users_guide v2 (#141), Pass 1. The run model + the turn-by-turn loop below are verified against [`simulation.py`](../../app/src/simulation.py) as of V00059. Still owed in this doc: the full RNG-stream catalog and the per-table ledger/master column detail (→ `data_dictionary.md`, Pass 3).

---

## 1. What a "run" is

A **run** is one 240-month (20-year) simulation of one funding scenario under one random seed. It is the atomic unit of the Monte Carlo: the headline outputs are survival rates *across many runs*.

- **Identity:** every run gets a `RunID` (and a `BatchID` grouping a sweep). **Every** simulation table is keyed by `RunID` first — the compound primary key `(RunID, …)` is what keeps thousands of concurrent runs isolated in one database. A query that forgets `RunID` reads across runs; the schema makes `RunID` the leading key everywhere to make that mistake loud.
- **Events:** state changes are logged as **events** with a monotonic `EventID`; ledger rows carry `(RunID, EventID)`. See §5.
- **Reproducibility:** a run is a pure function of `(projection_id, seed, engine SHA, reference data)`. Same inputs → **byte-identical** 240-month result (this is the canary gate — the frozen 1.0 baseline reproduces to the exact final dollar). The seed threads into every stochastic draw; nothing reads wall-clock time.
- **The inflation path is drawn up front.** Before the month loop starts, [`generate_inflation_schedule()`](../../app/src/simulation.py#L516) builds the entire 240-month, per-category regime path (the #48 Markov chain) into `simulation.InflationSchedule`. The macro "weather" for the whole run is therefore fixed at month 0 and deterministic given the seed — every downstream module reads the same schedule.

## 2. Setup (before the loop) — [`run_simulation()`](../../app/src/simulation.py#L578)

In strict order; any failure aborts the run:

1. [`initialize_simulation()`](../../app/src/simulation.py#L250) — create the `Run` row, instantiate and wire all managers, set `run_id` on each.
2. [`generate_inflation_schedule()`](../../app/src/simulation.py#L516) — the whole-run regime path (§1).
3. [`initialize_funds_and_staff()`](../../app/src/simulation.py#L456) — seed starting **Cash**, compute the initial **CSF** target from expected OpEx, hire the initial staff, transfer the reserve into CSF.
4. [`initialize_property_market()`](../../app/src/simulation.py#L530) — price the frozen listing catalog into the run's market (`property_market_manager.initialize_market`).

## 3. The turn-by-turn loop

Two nested loops: **outer = months** (`while current_month <= end_date`), **inner = days** (`while current_day <= month_end`). Most subsystems are called **every day** and self-gate internally (e.g. rent reductions only act on the 1st); a few fire only at month boundaries. Order matters — it is the engine's causal spine.

### 3a. Every day, in this exact order

| # | Call | Module | Does |
|---|---|---|---|
| 1 | [`process_daily_market`](../../app/src/simulation.py#L623) | property_market_manager | Advance the property market (Listed → OtherBuyerOwned → back, independent of LB). |
| 2 | [`compute_monthly_opex_breakdown`](../../app/src/simulation.py#L631) | simulation (helper) | Recompute the **expected** monthly OpEx (deterministic buckets + expected maintenance). Feeds the acquisition gate, CSF target, and top-up. Recomputed daily by design. |
| 3 | [`process_daily_pipelines`](../../app/src/simulation.py#L635) | property_acquisition_manager | Advance every in-flight acquisition (offer → response → inspection → negotiation → closing). |
| 4 | [`process_daily_compliance`](../../app/src/simulation.py#L638) | compliance_manager | Advance post-purchase compliance work (lead-paint etc.) toward rentable. |
| 5 | [`process_daily_tenant_onboarding`](../../app/src/simulation.py#L641) | tenant_manager | Execute move-ins whose start date is today. |
| 6 | [`process_monthly_rent_reductions`](../../app/src/simulation.py#L646) | rent_reduction_manager | **Self-gates to the 1st.** Advance tenure-based reductions **before** collection, so the month bills at the reduced effective rent. |
| 7 | [`process_daily_rent_collection`](../../app/src/simulation.py#L651) | rent_collection_manager | Collect due rent; assign ON_TIME / LATE / MISSED; accrue TCS on on-time; apply late fees. |
| 8 | [`process_evictions`](../../app/src/simulation.py#L666) | eviction_manager | File at the 3rd consecutive MISSED; execute 2 months after filing. |
| 9 | [`process_lease_lifecycle`](../../app/src/simulation.py#L673) | lease_renewal_manager | Lease-end pre-roll: landlord non-renewal (payment-history-gated), else renew/voluntary-exit; monthly early-break hazard. |
| 10 | [`process_pending_settlements`](../../app/src/simulation.py#L684) | security_deposit_manager | Settle deposits whose settlement date has arrived. |
| 11 | [`process_daily_turnover`](../../app/src/simulation.py#L693) | turnover_manager | Advance make-ready work orders on vacated units. |
| 12 | [`check_for_new_opportunities`](../../app/src/simulation.py#L706) | property_acquisition_manager | Maybe **start** a new acquisition — gated by the reserve-first rule (passes `monthly_opex` + `last_csf_committed_target`). |

### 3b. Conditional (date-gated) steps, same day, after the daily block

| When | Call | Does |
|---|---|---|
| Annual (raise day, seeded Dec 31) | [`process_annual_raises`](../../app/src/simulation.py#L714) | Apply staff raises. |
| Payroll days (15th **and** month-end) | [payroll block](../../app/src/simulation.py#L717) | Compute bi-monthly payroll = `Σ(base+benefits)/24`. **Halt check** (§4). Else [`process_bi_monthly_payroll`](../../app/src/simulation.py#L751) via the protected path. |
| Month-end | [non-payroll OpEx](../../app/src/simulation.py#L765) | Draw the month's **realized** maintenance ([`draw_monthly_events`](../../app/src/simulation.py#L766): routine + fat-tailed major) + turnover make-ready + deterministic buckets → one [`process_expense_protected`](../../app/src/simulation.py#L788) call. **Halt check** if not fully covered (§4). |
| Month-end | [CSF top-up](../../app/src/simulation.py#L836) | `get_reserve_months` (the #97 taper curve on **property** count) → [`process_csf_topup`](../../app/src/simulation.py#L843); record `last_csf_committed_target` for next month's gate. Fires **last** so next month's day-1 acquisition gate sees the post-top-up reserve. |
| Month-end | [TCS expiry sweep](../../app/src/simulation.py#L876) | Forfeit remaining credit for households past the 24-month post-exit window. |
| At cadence | [`snapshot_manager.capture`](../../app/src/simulation.py#L892) | End-of-day snapshot at cadence boundaries. |

Then `current_day += 1`. At month end: progress print every 12 months, `current_month += 1 month`.

### 3c. Termination — [lines 911–921](../../app/src/simulation.py#L911)

- **Died:** `failed_month` set → [`halt_run(run_id, halt_notes)`](../../app/src/simulation.py#L918) — the `Run` row gets `Status='HALTED'` + the break-time death certificate.
- **Survived the loop:** [`complete_run`](../../app/src/simulation.py#L921).
- Either way: a final snapshot + summary.

## 4. Two different "death" definitions — don't conflate them

- **The engine's operative halt** (what stops a run mid-flight): **`Cash + CSF < bi-monthly payroll`** on a payroll day ([L729](../../app/src/simulation.py#L729)), **or** month-end recurring OpEx not fully covered by `Cash + CSF` ([L804](../../app/src/simulation.py#L804)). Either sets `failed_month`, captures a halt-date snapshot, writes a `HALTED` certificate, and breaks.
- **The survival criterion of record** (how a run is *scored* for the S-curve): **positive `Cash + CSF` at month 240 AND a full 240-month ledger span.** `Status='HALTED'` is the cross-check, the span rule is authoritative.

These are not the same test. The operative halt is *narrower* — a run can limp past a payroll day yet still be scored non-surviving on the span/positive rule. See [`business_rules_current.md`](business_rules_current.md) §6 (KD-033) and [`failure_modes.md`](failure_modes.md).

## 5. Protected obligations, ledger, and reproducibility

- **The protected-expense path** ([`fund_manager.process_expense_protected`](../../app/src/fund_manager.py)) is how *survival obligations* — payroll and recurring OpEx — draw money: **Cash first, CSF as backstop**. It is a distinct code path, not a flag on the generic spender; that structural separation is what makes CSF a real protected reserve rather than a number that can be casually spent (see [`business_rules_current.md`](business_rules_current.md) §4). Growth/acquisition never uses it.
- **Ledger vs Master:** the **ledger** is the append-only event time-series (`(RunID, EventID)` per row, never updated in place); **master** tables hold current entity state, updated in place, no `EventID`. Every state change logs a ledger event; the masters are the fold of that history. (Table-by-table detail → [`data_dictionary.md`](data_dictionary.md).)
- **RNG streams:** the run seed derives a **dedicated stream per concern** (`random.Random(run_seed + offset)`) so unrelated subsystems never share draw order — the one exception being modules that deliberately borrow a caller's stream. The verified catalog:

  | Concern | Seed | Module |
  |---|---|---|
  | Inflation regime (rent/opex/property) | `run_seed + 30930` | inflation_engine |
  | Wage growth | `run_seed + 30931` | inflation_engine (2nd pass) |
  | Maintenance events | `run_seed + 30910` | maintenance_event_manager |
  | Lease renewal decisions | `run_seed + 30901` | lease_renewal_manager |
  | Security deposit | `run_seed + 30602` | security_deposit_manager |
  | Employee hire salary | `run_seed + role_id*1000 + employee_id` (per hire) | employee_manager |
  | Applicant slate | string-hash of `f"{run_seed}_vacancy_{vid}_date_{ordinal}_applicant_{i}"` | tenant_manager |
  | Property market (init shuffle + daily) | `random_seed` (passed per call) | property_market_manager |
  | Household income | **none — borrows tenant_manager's rng** (invariant #8) | income_model |

  Wage growth rides the *same* #48 regime path as rent (it reads each month's already-drawn regime, then draws on stream `30931`), so the same seed yields an identical market whether or not incomes vary — the property that let Phase 1.8 change incomes without disturbing the frozen market. **The alignment invariant (#8):** stochastic draws are consumed **unconditionally** — a fixed draw-count per step regardless of branch outcomes or parameter values — so a stream stays byte-aligned even when a knob changes which branch is taken. Zero-unit / zero-employee months skip their draws entirely (an explicit stream-preservation guarantee for portfolios that grow at different dates). Nothing reads the global `random` module (the one historical offender, inflation, was moved to a dedicated stream).

## 6. Module reference (all 26)

The engine is ~20 manager objects wired together by `simulation.py`. Each owns a slice of state (a set of tables) and is called either from the daily loop (§3) or by another manager. Format below, per module: **what it owns**, its **entry points**, the **tables** it reads/writes, the **knob categories** it consumes (full per-knob detail → [`parameter_reference.md`](parameter_reference.md); full per-table detail → [`data_dictionary.md`](data_dictionary.md)), its **collaborators**, and any **behavioral caveat** (a V1 simplification a developer must know — pure code-hygiene nits are tracked separately in the build's findings list, not here). Line refs are into `app/src/`.

### 6.1 Orchestration & infrastructure

**`simulation.py` — the orchestrator.** Owns the `Simulation` class that constructs and wires all ~20 managers, runs the daily/monthly loop, and drives the month-end financial sequence (payroll → OpEx → CSF top-up). Entry points: `__init__` (wire the always-on managers), `initialize_simulation` (create the run + the per-run managers), `initialize_funds_and_staff`, `generate_inflation_schedule`, `initialize_property_market`, `compute_monthly_opex_breakdown` (the single source of truth for expected OpEx), `run_simulation` (§3), `main` (the CLI). It is the top of the call graph — nothing calls into it. Reads `simulation.PropertyUnits` for counts; writes only `simulation.Run.SnapshotCadence` directly (everything else is delegated). Consumes the resolved `ProjectionConfig`, not the registry directly. *Caveat:* the day-0 CSF target uses a `$175,000` bootstrap payroll literal that doesn't track staffing knobs (audit F-21, `:463`) — self-corrects at first hire; month-end non-payroll OpEx is one aggregate protected charge, no per-bucket routing (`:761`).

**`run_manager.py` — run lifecycle.** Owns the `simulation.Run` row: `create_run` (assign next `RunID`, INSERT as `CREATED`), `start_run` (→`RUNNING`), `halt_run(notes)` (→`HALTED` + `CompletedAt` + the death certificate), `complete_run` (→`COMPLETED`), `get_run_info`. Reads/writes `simulation.Run`. Called only by `simulation.py`. No knobs directly.

**`event_logger.py` — the ledger writer.** The centralized writer to `simulation.Event`; defines the engine-wide `EventType`/`EntityType`/`ActionType` taxonomy. Entry points: `set_run_id`, `log_event` (computes next per-run `EventID` via `MAX(EventID)+1 WHERE RunID=?`), plus domain wrappers (`log_fund_event`, `log_acquisition_event`, `log_module_event`, `log_database_event`, `log_error`), `clear_events`. Reads `simulation.Run` (StartDate) + `simulation.Event` (max id); writes `simulation.Event`. Consumed by 15 managers; note it is **not a singleton** — `inflation_engine` constructs its own second instance writing the same table.

**`snapshot_manager.py` — trajectory capture.** Writes periodic point-in-time aggregates to `simulation.RunSnapshot` at cadence boundaries. `should_capture` (is today a cadence boundary?), `capture` (idempotent gather+insert), `capture_final` (force-capture at a halt date). Reads across `FundLedger`/`Properties`/`PropertyUnits`/`Lease`/`Employees`/`LeaseTermination`/`TurnoverWorkOrder`/`Event`; writes `simulation.RunSnapshot`. Cadence passed in from `SIM.SnapshotCadence`.

**`backfill_snapshots.py` — offline snapshot rebuild.** Standalone CLI (not called by the engine) that replays `SnapshotManager`'s aggregation to reconstruct `RunSnapshot` rows (tagged `BACKFILL`) for a finished run, using each run's last `Event` date as its terminal boundary.

**`configuration_loader.py` — projection → config.** Loads one projection's full `ProjectionConfig` (~60 fields) from the registry and builds the derived RR tier list. Sole real entry point: `load_projection(projection_id)`. Delegates all reads to `ParameterRegistry`; writes nothing. Fail-loud, with one documented exception: a half-NULL RR tier is dropped with a warning rather than raising.

**`parameter_registry.py` — the knob resolver.** The single read path for the parameter store, which since **V00071** is three tables: `reference.Projection` (identity — a projection exists because a row here says so, and it carries `Description`/`Kind`/`ScenarioTag`), `reference.ParameterRegistryDefault` (defaults, keyed `(Category, Name)`) and `reference.ParameterRegistryDefined` (per-projection overrides, `ProjectionID NOT NULL` with an FK). `load(projection_id)` unions the two value tables and resolves **override-wins-over-default** per `(Category,Name)`, and also loads the entity (`projection_exists`, `projection_name`, `projection_kind`, `projection_scenario_tag`); `load_globals()` reads only the defaults and is deliberately projection-blind — a per-projection override is invisible through it, so a knob meant to vary by projection must be read via `load()`. Typed accessors (`get_decimal/int/float/str/date`, `get_category`, `has`) coerce by each row's own `DataType` and **raise on any missing key — no code-side defaults** (the deliberate fail-loud design). Reads `reference.ParameterRegistry`; writes nothing.

**`database_manager.py` — the SQL primitive layer.** Owns pyodbc connection handling (incl. `SET QUOTED_IDENTIFIER ON` per connection) and the four executors every module uses: `execute_query`, `execute_non_query`, `execute_insert_returning`, `execute_scalar`. Holds no SQL of its own — callers supply it. Loads its connection config from a JSON file, not the registry.

**`master_test_runner.py` — the validation harness.** CLI that runs the promotion gate: optional DB cleanup → integration smoke (or the tracked regression suite, `--regression`) → event-log analysis → PASS/FAIL summary. Subprocess-invokes `simulation.py` and `run_suite.py`. Reads `simulation.Run`/`Event`/`InflationSchedule`.

**`debug_tracer.py` — optional call/DB tracer.** Zero-invasive `sys.settrace` call tracer + an explicit `log_db_call` hook (fired by `DatabaseManager`), both to a JSONL file. Activated only via `--debug`. Touches no tables.

### 6.2 Money & macro

**`fund_manager.py` — the buckets + the protected path.** Owns the append-only `simulation.FundLedger` and every money movement: `initialize_funds`, `get_reserve_months` (the #97 taper curve `floor + (peak−floor)·√(N0/N)`), `get_csf_target` (`monthly_opex × reserve_months`), `process_csf_topup` (the reserve ratchet — never sweeps CSF down), **`process_expense_protected`** (Cash-first / CSF-fallback for survival obligations; returns `fully_covered` — the OpEx/payroll halt hinges on it), `process_expense`/`process_income` (unprotected), `get_fund_balances`, plus the escrow (deposit-liability) and CashHold (offer-reservation) sub-ledgers. Reads/writes only `simulation.FundLedger` (append-only; no UPDATE/DELETE anywhere). Takes CSF/FIN knob *values* as args (resolved upstream), reads no registry itself. Injected into 6 managers; `property_acquisition_manager` also calls its reserve/target/balance reads for the acquisition gate. *Note:* `escrow` and `CashHold` are deliberately excluded from `total_balance`.

**`inflation_engine.py` — the macro weather generator.** One-shot, pre-simulation generator of `simulation.InflationSchedule`, and the sole reader of it for the cumulative OpEx factor. `generate_schedule` orchestrates; internally `_generate_monthly_rates` runs the **5-state #48 Markov chain** (Normal / Surge / Normalization / DownturnFinancial / DownturnShock — that tuple order is a reproducibility contract) drawing 3 Gaussians + 1 transition uniform per month (negative rates intended, no `max(0)` clamp); `_apply_wage_rates` is the Phase-1.8 second pass on stream `30931`; `get_cumulative_opex_factor` is the single compounding point (KD-028) so OpEx charge, CSF target, and cash floor all agree. Static mode = flat annual/12, no draws. Reads/writes `simulation.InflationSchedule`; reads `INF.*`/`INC.*` via its own registry load. Only `simulation.py` holds it.

**`maintenance_event_manager.py` — lumpy maintenance.** Pure-compute (no DB), base-year dollars. Two bases: `expected_monthly_routine_cost`/`expected_monthly_major_cost` (deterministic — the smooth basis for the CSF target/gate) and `draw_monthly_events` (the realized month-end draw: routine count via Negative-Binomial Gamma-Poisson mixture, major via Poisson with fat-tailed lognormal severity — one bad month can eat ~$14k). This stream *is* the capital-replacement budget by design (no separate smoothed reserve line, so a bad year can't be averaged away). Seed `+30910`; zero-unit months consume no RNG. Reads `MAINT.*` off the config; only `simulation.py` calls it. The caller applies the inflation factor at charge time.

### 6.3 Property side

**`property_acquisition_manager.py` — the buy pipeline + the gate.** Owns economic candidate scoring, the acquisition gate, and the multi-stage pipeline (offer → seller response → inspection → negotiation → closing) through to writing the property/units into the portfolio. Entry points: `process_daily_pipelines` (advance in-flight attempts), `check_for_new_opportunities` (start one?), `can_acquire_property` (the gate: Rule 1 reserve-first / genuine-draw CSF latch with a 6-month grace, Rule 2 cash-floor, Rule 3 deployable-cash-after-earmark; KD #99), `get_acquisition_budget`, `calculate_income_to_price_ratio` (the yield score — **uses `reference.Units.AdjustedRent`**, per the #44/KD-040 work), `add_acquired_property_to_portfolio` (copies `reference.Units` → `simulation.PropertyUnits`, persisting `BaseRent`). Reads `reference.AcquisitionParameters`/`Properties`/`Units`, `simulation.PropertyAcquisitionAttempt`/`PropertyMarket`/`PropertyUnits`; writes the attempt/step ledger + `PropertyMarket` transitions + `Properties`/`PropertyUnits` on close. Knobs: the dedicated `reference.AcquisitionParameters` table + `ACQ.*`/`CSF.GracePeriodMonths`/`PROP.RampPeriodMonths`. Collaborates with `FundManager` (offer reservations, `use_reserved_funds`, reserve/target reads) and `EmployeeManager` (post-close staffing check, KD-027). *Caveat:* inspection scheduling is a placeholder that advances immediately (`:1615`); closing costs are now actually debited (KD-035 fix).

**`compliance_manager.py` — acquired → rentable.** Owns the post-purchase compliance lifecycle (inspections + remediation work items) that gates a unit from "compliance in progress" to `Available`. `process_daily_compliance` is the sole driver (detect close triggers → advance scheduled items → resolve in-progress items → check milestones); `_check_and_emit_unit_ready` is where `PropertyUnits.UnitStatus` flips to `Available`; `is_building_online`/`is_unit_rentable` are the gating queries. Reads `reference.ComplianceParameters`/`Properties`/`Units`, `simulation.PropertyAcquisitionAttempt`; writes the compliance attempt/work-item/step tables + the `PropertyUnits` status flip. Requires `FundManager` (fail-loud) — **compliance costs draw Cash only, never CSF** (F-01 ruling). *Caveats:* V1 is acquisition-triggered only (no periodic/turnover compliance); remediation always succeeds (no failure path).

**`property_market_manager.py` — the market state machine.** Owns `simulation.PropertyMarket` as a manual SCD-2 versioned table. `initialize_market` (zero-day assignment to Listed/Available/OtherBuyerOwned via seeded shuffle + pricing); `process_daily_market` (returns → new listings → listed outcomes, each seasonal/inflation-adjusted). The market cycles **independently of LB** — a crash just means cheaper listings. Reads `reference.Properties` + `simulation.InflationSchedule`; writes `simulation.PropertyMarket`. Heavy `MARKET.*` consumer (init %s, hold curves, days-on-market, per-month seasonal multipliers). Prices via base × cumulative-inflation × monthly seasonal knob.

**`turnover_manager.py` — make-ready.** Owns the fixed 5-step work-order sequence (INSPECT → CLEAN → PAINT → RESTORATION → FINAL_INSPECTION) that turns a vacated unit `Turnover` → `Available`. `trigger_turnover` (reads the settled `LeaseDeposit` to decide if restoration is needed; **hard-fails if settlement state is missing** — a strict ordering contract with `security_deposit_manager`), `process_daily_turnover` (state machine: complete due items, start ready ones gated on lower steps being done), `_finalize_turnover`. Reads `Lease`/`TurnoverWorkOrder`/`LeaseDeposit`/`PropertyUnits`; writes `TurnoverWorkOrder` + the `PropertyUnits` status. `TIMING.*` durations. Called by `eviction_manager` and `lease_renewal_manager` (trigger) + `simulation.py` (daily advance).

### 6.4 Tenant side

**`tenant_manager.py` — applicants → housed.** Owns vacancy detection, deterministic applicant-slate generation, the three-gate screen, first-qualified selection, and materialization of Household/Person/Lease on selection. Entry points: `process_daily_tenant_onboarding` (daily driver), `_check_and_create_new_vacancies`, `_generate_candidate_slate`/`_generate_applicant` (income via `IncomeModel`, RNG seeded per `run_seed_vacancy_date_applicant`), the three gates — `_check_prescreen_affordability` (self-selection at `QUAL.PreScreenRentToIncomeRatio`), `_check_bedroom_fit` (the #118 "2 per bedroom + 1"), `_check_income_qualification` (the 30% rule at `QUAL.MaxRentToIncomeRatio`) — `_evaluate_candidates` (first qualified wins), `create_lease`/`_materialize_*`, and **`_compute_inflation_adjusted_rent`** (`BaseRent × (1−below_market) × Π(1+RentRate)`, computed **once per fill attempt** and reused by screen + insert so screen-rent == signed-rent by construction — the #154 fix). Reads `PropertyUnits`/`Vacancy`/`Lease`/`Run`/`InflationSchedule` + `reference.FirstName`/`LastName`; writes `Vacancy`/`ApplicantEvaluation`/`Household`/`Person`/`Lease` + the `PropertyUnits` status. Knobs: `TNT.*`, `QUAL.*`, `LEASE.StandardLeaseDurationMonths`. Delegates deposit/TCS init to their managers. *Caveat:* bedroom-fit is max-occupancy only, no age awareness (#127).

**`income_model.py` — the sum-of-earners generator.** Sole method `generate_household_income(household_type, adult_count, rng, target_date)` → `(monthly, primary_band)`. Household income = sum of independently-drawn earners by type (SINGLE incl. a fixed-income non-earner path at `INC.SingleNonEarnerProb`; COUPLE/FAMILY primary + Bernoulli second earner with `CoupleSameBandProb` correlation; ROOMMATES N independent, top-band-dampened, r=0). Bands B1–B4 uniform × time-varying median, **B5 log-uniform** to `TailCapMultiple` (~4×, "never CEO-ratio"). `cumulative_wage_factor` compounds `Π(1 + WageGrowthRate × bandMult)` off `InflationSchedule`. Reads `simulation.InflationSchedule` only; writes nothing. All `INC.*`. **Owns no RNG** — uses the caller's (invariant #8). Constructed by, and called only from, `tenant_manager`. Replaced the retired static `reference.IncomeBand` (the frozen-income artifact, KD-018).

**`tenant_credit_manager.py` — TCS end-to-end.** Owns the Tenant Credit System across `simulation.TenantCreditLedger` (append-only) + `TenantCreditBalance` (materialized). Entry points: `initialize_household` (idempotent zero-balance MERGE), `process_credit_accrual` (ON_TIME **and** deposit-funded → `min(basis × rate, cap headroom)`; partial-accrue to land exactly at the `$15k` cap), `should_routine_redeem`/`should_hardship_redeem` (eligibility + RNG roll vs the household's stored probability; hardship needs good standing), `process_credit_redemption` (ROUTINE/HARDSHIP redeem full rent, `FINAL_MONTH_EXIT` redeems `min(balance, rent)`), `process_credit_expiry_sweep` (forfeit past the 24-mo post-exit window), `process_credit_forfeiture_eviction`, `mark_household_exited`/`mark_household_reentered` (the portability clock). Reads `Lease`/`Household`/`MonthlyPaymentStatus`/`LeaseTermination` + its two tables; writes its two tables + `Event`. `TCS.*`. Injected into tenant/rent/eviction/lease managers; the monthly sweep is driven from `simulation.py`.

### 6.5 Lease, rent & staffing

**`rent_collection_manager.py` — the monthly bill.** Owns daily collection: `process_daily_rent_collection` (day-1 init → daily attempt → month-end MISSED finalization). Day-1 `_initialize_monthly_payment_status` rolls `CanPayThisMonth` once/lease and, if TCS is wired, may route to redemption instead of a bill. `_collect_payment` applies **Priority 1 current rent → 2 FIFO arrears → 3 late fee**, writes `RentCollection` + `MonthlyPaymentStatus`, updates `Lease.ConsecutiveMissedPayments`, and fires deposit-installment + TCS-accrual. Reads/writes `MonthlyPaymentStatus`/`RentCollection`/`Lease` (+ `LeaseTermination` read). `PAY.*` via config. *Caveat (important):* in the current daily wiring, **`LATE` is structurally never written** — a can-pay lease is always collected on day 1, inside the grace window; the code path exists but is unreachable (a documented V1 assumption).

**`lease_renewal_manager.py` — renew / exit / non-renewal.** Owns the lease-end pre-roll + the early-break hazard. `process_lease_lifecycle` (day-1 pre-roll of RENEW / VOLUNTARY_EXIT / LANDLORD_NONRENEWAL for leases ending this month → day-1 early-break roll → materialize decisions on `LeaseEndDate`). Landlord non-renewal is payment-history-gated (`LEASE.LandlordNonRenewalProbPct` only if ≥`LateMonthThreshold` LATE-or-**MISSED** months — the 3.10.2 fix, since LATE is never written); otherwise `LEASE.RenewalRatePct` renew vs voluntary exit; monthly `LEASE.EarlyBreakProbMonthly`. Two RNG streams: caller's (early break) + `+30901` (renewal). Writes `Lease` + `LeaseTermination`/`LeaseTerminationLedger`. Collaborates with deposit/turnover/TCS managers for the exit.

**`eviction_manager.py` — the last resort.** Deterministic (not probabilistic): `check_eviction_status` files at `ConsecutiveMissedPayments >= LEASE.EvictionMissedThreshold` (3); `execute_eviction` fires `LEASE.EvictionExecutionDelayMonths` (2) later — terminates the lease, forfeits TCS, charges the flat `LEASE.EvictionCost` ($1,500) via the **protected** path, writes off arrears. Unit goes `Pending_Move_Out` at filing. Reads `Lease`/`LeaseTermination`; writes `Lease`/`LeaseTermination`/`LeaseTerminationLedger` + the `PropertyUnits` status. *Caveat (mission-relevant):* **no cure period** — once filed, execution fires on schedule regardless of whether the tenant caught up in the interim (the KD-031 tension; deposit/rent continue during the window but nothing here re-checks).

**`security_deposit_manager.py` — deposit lifecycle.** Owns FULL-vs-INSTALLMENT funding + the two-step settlement (outcome at termination, cash after a delay). `initialize_deposit_for_lease` (roll `DEP.InstallmentProbability`), `process_installment_payment` (remaining-balance-aware final installment), `settle_deposit_at_termination` (FORFEITURE if unfunded/immediate, else damage-assessed PENDING), `process_pending_settlements` (daily executor), `is_deposit_funded` (the TCS accrual gate). Reads/writes `LeaseDeposit`/`LeaseDepositLedger` + updates `LeaseTermination` (KD-038 fix). `DEP.*`; RNG `+30602`. *Caveat:* a never-funded deposit forfeits with nothing to return (immediate, by design); the installment-partial case is the KD-032 tension.

**`rent_reduction_manager.py` — tenure discounts.** Sole method `process_monthly_rent_reductions` **self-gates to the 1st** and advances `Lease.CumulativeRentReductionPct`/`EffectiveMonthlyRent` when a lease crosses a new `RR.*` tenure tier (canonical 36mo→5%, 72mo→+5%, 120mo→+10%, 20% cumulative max; reductions are off the original signed rent, never compounding). Reads/writes `simulation.Lease` only; consumes the pre-built RR tier list. Runs **before** collection in the daily loop so the month bills at the reduced rate.

**`employee_manager.py` — staffing & payroll.** Owns the employee lifecycle. `calculate_staffing_needs`/`check_staffing_needs` (maintenance = `properties // STAFF.MaintCrossoverProperties` — 0 FTE below the crossover is the *design*, contracted-out, not a gap; admin = `max(STAFF.BaseAdminCount, ceil(units/threshold))` — the KD-030 `max` fix), `hire_employee` (salary via a per-hire seeded RNG `run_seed + role_id*1000 + employee_id`, the KD-039 fix for the old global-RNG poisoning), `process_bi_monthly_payroll` (half-monthly gross+benefits via the **protected** path — the cash floor never blocks payroll), `process_annual_raises` (cash-cushion-tiered, clamped to `reference.EmployeeRole.SalaryCap`, KD-029). Reads `Properties`/`Units`/`Employees`/`EmployeeRole`/`Run`; writes `Employees` + `Payroll`. `STAFF.*`. Called by `simulation.py` (setup, payroll, raises) + `property_acquisition_manager` (post-close hire, KD-027).

---

*Verified against `simulation.py` @ V00059 and the six-module source inventory, 2026-07-06. Line links are to the promoted engine on this branch's base.*
