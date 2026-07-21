# Liberty Bee — Parameter Reference (328 knobs)

**Audience:** a developer tuning or auditing the model. Every behavioral number lives in the parameter store as a `(Category, Name)` row — in `reference.ParameterRegistryDefault` for the default, or `reference.ParameterRegistryDefined` keyed `(ProjectionID, Category, Name)` for a per-projection override (split at V00071). Each row now carries its own `Description` in the database (V00073 backfilled 77 that were empty, value-restating, or too terse), so this catalog and the live store should agree; the store is authoritative. Read [`users_guide.md` "the four basis labels"](users_guide.md) first — **CITED / DECLARED / MECHANICAL / POLICY** is the honesty machinery, and it's the most important column here. Tables: [`data_dictionary.md`](data_dictionary.md); code: [`engine_internals.md`](engine_internals.md); citations behind the CITED knobs: [`evidence_base.md`](evidence_base.md).

> **Build status:** users_guide v2 (#141), Pass 4. Values are the live V00059 global (`ProjectionID IS NULL`) defaults; basis labels + readers verified from evidence_base + the seed migrations + reader code via a 6-agent inventory. Per-projection *overrides* (stress projections, capital-ladder rungs) exist for some knobs and are noted where relevant but not enumerated. Knobs with no live reader are flagged **⚠ dormant** and cross-listed in [`../v_0_3/phases/phase_1_9/engine_hygiene_findings.md`](../v_0_3/phases/phase_1_9/engine_hygiene_findings.md).

**Registry mechanics:** `parameter_registry.py` resolves per-projection override → global default per `(Category, Name)`, coerces by the row's `DataType`, and **raises on any missing key — no code-side defaults**. A knob's value is only ever what the registry holds.

## Category map (22 categories, 328 knobs)

| Cat | # | Controls | Reader | Basis lean |
|---|---|---|---|---|
| INF | 66 | Inflation-regime Markov model (#48) | inflation_engine | DECLARED (anchored to CITED episodes) |
| MARKET | 60 | Property-market state machine | property_market_manager | CITED (FRED) + DECLARED |
| INC | 34 | Income model (Phase 1.8) | income_model / inflation_engine | CITED/CALIBRATED |
| TCS | 20 | Tenant Credit System | tenant_credit_manager | POLICY |
| CMPL | 17 | Compliance sampling | compliance_manager | DECLARED |
| ACQ | 16 | Acquisition negotiation | property_acquisition_manager | DECLARED |
| TNT | 16 | Applicant/household generation | tenant_manager | DECLARED + CITED (mix) |
| STAFF | 13 | Staffing & payroll | employee_manager | POLICY + CITED |
| GRT | 12 | Grants — **reserved, unimplemented** | none (⚠ KD-022) | N/A |
| LEASE | 9 | Lease lifecycle | lease_renewal / eviction | POLICY |
| RET | 7 | Tenant retention model (Phase 1.10) | retention_model / lease_renewal | DECLARED (anchored §10) |
| MAINT | 9 | Maintenance cost model | maintenance_event_manager | CITED (all) |
| TIMING | 8 | Turnover/timing durations | turnover_manager | MECHANICAL |
| DEP | 7 | Deposit lifecycle | security_deposit_manager | POLICY/DECLARED |
| PROP | 7 | Property/vacancy/discount | tenant_mgr / acquisition | mixed |
| RR | 6 | Rent-reduction schedule | rent_reduction_manager | POLICY |
| CSF | 5 | Reserve curve | fund_manager | CITED/DECLARED |
| QUAL | 5 | Qualification gates | tenant_manager | CITED/POLICY |
| FIN | 3 | Cash floor + EIP | configuration_loader | CITED + ⚠ orphaned |
| OPEX | 3 | Per-unit operating costs | simulation | CITED (Salem) |
| PAY | 3 | Payment/late-fee | rent_collection_manager | MECHANICAL |
| SIM | 2 | Sim clock/cadence | simulation | MECHANICAL |

---

## OPEX (3) — per-unit operating costs · reader: `simulation.compute_monthly_opex_breakdown`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| PropertyTaxPerUnit | $2,250/yr | **CITED** | Salem FY2026 residential rate $10.78/$1k AV × per-unit AV (evidence §4d, HIGH confidence, parcel-verified). |
| InsurancePerUnit | $1,400/yr | **CITED** | MA small-building BOP proxy $1,300–1,500/unit (evidence §4d; not Salem-segmented, +15–30%/yr since 2022). |
| UtilitiesOwnerPerUnit | $2,000/yr | **CITED** | Owner-pays-**heat** case (evidence §4d); conservative on purpose — ~99.6% pre-1978 stock may retain master-metered boilers. Modeling the ~$1,200 separately-metered case would be flattery. |

## FIN (3) — cash floor + EIP · reader: `configuration_loader`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| CashFloorMonths | 3 | **CITED** | Unrestricted-Cash floor = 3 mo OpEx (NORI nonprofit-reserve minimum, evidence §6). Renamed CSF→FIN at V00049 (namespace hygiene). |
| EIPAllocationPct | 0.05 | POLICY (design intent) | Intended 5%-of-starting-funds EIP envelope. **⚠ ORPHANED** — not read anywhere (not even loaded into `ProjectionConfig`; deeper than KD-022's text). |
| EIPStartYear | 6 | POLICY (design intent) | Year EIP spend would unlock. **⚠ ORPHANED** — same as above. |

## CSF (5) — reserve curve (#97) · reader: `fund_manager` / `property_acquisition_manager`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| ReserveMonthsPeak | 12 | **CITED/DECLARED** | Reserve peak at ≤N0 properties (NORI ">12mo needs justification"; small-portfolio value of the curve, evidence §6). |
| ReserveMonthsFloor | 4 | **CITED** | Curve floor (LIHTC 4-mo operating-reserve min, evidence §6; legibly above the 3-mo cash floor). |
| ReserveCurveN0Properties | 8 | **DECLARED** | Portfolio size holding the peak; taper `floor+(peak−floor)·√(N0/N)` begins above it. "A declared decision, swept 8/12/15" (Gray). |
| TopupFractionPerMonth | 1.0 | DECLARED | Fraction of the monthly CSF shortfall topped up per month (1.0 = full, one pass). |
| GracePeriodMonths | 6 | DECLARED | Startup grace bypassing the CSF acquisition gate. Distinct policy from `PROP.RampPeriodMonths` (shared value today). |

## PROP (7) — property/vacancy/discount · readers: `tenant_manager`, `property_acquisition_manager`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| BelowMarketRentPct | 0.10 | **POLICY** | The 10%-below-market mission dial. |
| VacancyRateBase | 0.019 | **CITED** | Salem rental vacancy 1.9% (City Housing Roadmap, evidence §4a). Steady-state only — cyclical stress is #48's job. *(Doc note: V00048 says "acquisition scoring only" but it also feeds tenant_manager fill-duration.)* |
| VacancyFluctuationBand | 0.005 | DECLARED | ± band on the seeded annual vacancy draw (small-fluctuation realism, not stress). |
| VacancyMeanDays | 30 | DECLARED | Mean of the truncated-exponential vacancy-fill duration. |
| VacancyMaxDays | 120 | DECLARED | Cap on vacancy-fill duration. |
| AcquisitionPct | 0.85 | POLICY | Fraction of deployable cash committed to acquisitions per cycle. |
| RampPeriodMonths | 6 | DECLARED | Early window allowing extra concurrent acquisition pipelines. |

## QUAL (5) — qualification gates · reader: `tenant_manager`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| MaxRentToIncomeRatio | 0.30 | **POLICY/CITED** | The 30%-rule affordability gate (standard housing-affordability practice; salience corroborated evidence §4c). |
| PreScreenRentToIncomeRatio | 0.40 | DECLARED | Applicant **self-selection** threshold — households apply only if rent ≤ 40% income. Models autonomy, **not an LB gate**: LB enforces the 30% rule; this only models who *chooses* to apply (e.g. valuing a roof here over a car/commute). Magnitude deliberately loose — could be higher (Gray 2026-07-07). |
| MaxOccupantsPerBedroom | 2 | **CITED** | The "2 per bedroom" of the industry-standard occupancy rule (evidence §3, Keating/CA §12955 de-facto standard). |
| MaxOccupantsBonus | 1 | **CITED** | The "+1" of "2 per bedroom + 1". |
| MaxOccupantsStudio | 2 | **CITED** | Studio (0BR) cap (own knob; age-aware revisit → #127). |

## DEP (7) — deposit lifecycle · reader: `security_deposit_manager`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| InstallmentProbability | 0.70 | DECLARED | P(tenant chooses INSTALLMENT vs FULL). |
| InstallmentCount | 4 | DECLARED | Installments when INSTALLMENT chosen (Months 1–4). |
| EvictionDamageProbability | 0.30 | POLICY/DECLARED | P(damage assessed) at an eviction settlement (stress override 221→1.0). |
| VoluntaryDamageProbability | 0.10 | POLICY/DECLARED | P(damage assessed) at a voluntary settlement. |
| DamageMinPercent | 0.10 | MECHANICAL | Lower bound of damage-as-fraction-of-deposit draw. |
| DamageMaxPercent | 0.50 | MECHANICAL | Upper bound of same. |
| SettlementDelayDays | 30 | MECHANICAL | Termination→settlement cash-movement delay (echoes MA §15B 30-day rule, not explicitly cited). |

## PAY (3) — payment/late-fee · reader: `rent_collection_manager`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| BaseFailProbMonthly | 0.02 | DECLARED | Monthly P(household can't pay); decided once/month (seed-reproducibility). Stress overrides 220→0.40, 221→0.10. |
| GracePeriodDays | 5 | MECHANICAL | Days after due before LATE (no weekend/holiday adj). |
| LateFeePercent | 5.0 | POLICY | Late fee % of rent (LATE months only). *(Note: LATE is structurally never written in V1 — see engine_internals §6.)* |

## SIM (2) — sim clock/cadence · reader: `simulation`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| StartDate | 2025-01-01 | MECHANICAL | Simulation clock anchor (per-projection EndDate → 20-yr horizon). |
| SnapshotCadence | QUARTERLY | MECHANICAL | RunSnapshot cadence (MONTHLY/QUARTERLY/ANNUAL). |

## RR (6) — rent-reduction schedule · reader: `rent_reduction_manager` · **all POLICY (mission dial)**
The tenure ladder, cumulative off original rent, never compounding: **36 mo → 5% · 72 mo → +5% · 120 mo → +10% (20% cumulative max)**. Knobs: `{First,Second,Third}ReductionMonths` = 36/72/120, `{First,Second,Third}ReductionPct` = 0.05/0.05/0.10. Tiers 4 & 5 are **absent-inactive** (the 4th-tier 180mo→25% artifact was retired at V00051/KD-104 — slightly pro-institution; slot 5 never seeded).

## LEASE (9) — lease lifecycle · readers: `lease_renewal_manager`, `eviction_manager` · **all POLICY/business-locked**
| Knob | Default | Meaning |
|---|---|---|
| RenewalRatePct | 80.0 | **Reinterpreted (Phase 1.10):** the market-equivalent base — `base_exit = 1 − this/100 = 0.20`. Voluntary exit at term is no longer flat; it's `RET.*`-modulated (deal + regional scarcity), bounded `[RET.FloorExitAnnual, base_exit]`. Value unchanged from V1. |
| LandlordNonRenewalProbPct | 10.0 | 10% landlord non-renewal — **only if** ≥ `LateMonthThreshold` late/missed months in prior 12. |
| LateMonthThreshold | 2 | Payment-history gate for landlord non-renewal (counts LATE+MISSED). |
| EarlyBreakProbMonthly | 0.0025 | Monthly voluntary early-break hazard (day-1 gated; a prior daily-roll bug inflated it ~19×, fixed 3.9.6.3). |
| EvictionMissedThreshold | 3 | Consecutive MISSED that trigger eviction filing. |
| EvictionExecutionDelayMonths | 2 | Filing → execution delay (~2 mo lost income). |
| EvictionCost | $1,500 | Flat eviction cost, Cash (protected path). |
| StandardLeaseDurationMonths | 12 | Initial lease term. |
| RenewalMonths | 12 | Renewal extension length (own knob, distinct from initial term). |

## RET (7) — tenant retention model (Phase 1.10, KD-041/#167) · reader: `retention_model` (wired via `lease_renewal_manager`) · **DECLARED, anchored to CITED evidence §10**
`voluntary_exit = clamp(base_exit·(1−β·effective_discount)·(1−γ·external_scarcity), floor_exit, base_exit)`. `base_exit` = `LEASE.RenewalRatePct` reinterpreted (not a RET row).
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| DiscountSensitivityBeta | 1.0 | DECLARED (DMQ ~19% move-reduction, §10b) | β — how strongly a below-market deal suppresses exit. |
| ScarcitySensitivityGamma | 0.50 | DECLARED, swept 0.25–0.75 (Sieg-Yoon lock-in §10b/c) | γ — how strongly regional scarcity suppresses exit; scarcity ≈28% of retention at this value (S6-fix). |
| FloorExitAnnual | 0.05 | DECLARED-no-source | Minimum annual voluntary exit (life events), regardless of deal/scarcity. |
| VacancyRefPct | 0.07 | DECLARED, §4a-anchored (6–8% healthy vacancy) | Balanced-market vacancy normalizer for availability. |
| MoverRegionalVacancyPct | 0.030 | DECLARED, secondary-sourced (North Shore/Essex ≈ACS5yr, §4a; pending Census verify) | Regional vacancy a leaving tenant faces — separate from `PROP.VacancyRateBase` (LB's Salem-local occupancy). |
| BurdenCeilingPct | 0.50 | DECLARED, HUD-anchored (severe rent-burden) | Rent-to-income at which the regional market is effectively unaffordable. |
| FormIsLogistic | 0 | MECHANICAL | 0=linear (shipped); 1=logistic (validation sweep only). |

## TCS (20) — Tenant Credit System · reader: `tenant_credit_manager` · **mostly POLICY**
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| CreditCap | $15,000 | POLICY | Cap on current balance (refillable). Owner ruling. |
| PortabilityYears | 2 | POLICY | Post-exit redemption window (24 mo). |
| RedemptionProbMin / Default / Max | 0.005 / 0.02 / 0.08 | POLICY | Fallback triangular bounds for the onboarding redemption-probability draw. |
| RedemptionHardshipFloor / Boost / Cap | 0.75 / 0.70 / 0.95 | POLICY | Hardship formula `min(cap, max(base+boost, floor))` — a missed-month household's boosted redemption odds. |
| RedemptionBand_&lt;TYPE&gt;_&lt;Min/Center/Max&gt; (12) | FAMILY .005/.015/.05 · COUPLE .005/.018/.06 · SINGLE .008/.025/.08 · ROOMMATES .010/.030/.08 | POLICY/DECLARED | Per-household-type triangular bands for the redemption-probability draw ("saver" vs "relief-oriented"; "approved baseline, not final policy"). |

> **`CreditRate` (per-projection; 0.10 on live rungs 200–206) — POLICY.** The set-aside rate — % of rent accrued to the tenant's credit balance. Set per projection (no global row), so it's outside the (20) global count above. **Why 10% = the floor of meaningfulness** (Gray 2026-07-07): a lower rate makes the yearly credit a rounding error (~$50 on a $1,000 rent — "not comfortable groceries for a family of three"); the mission point is a *usable* annual redemption, so too-low defeats the purpose. Sweepable (5% = conservative sensitivity leg).

## INC (34) — income model (Phase 1.8) · readers: `income_model`, `inflation_engine` (wage pass) · **CITED/CALIBRATED to evidence §9**
| Knob(s) | Default | Basis | Meaning |
|---|---|---|---|
| EarnerMedianAnnual | $55,000 | **CITED anchor / calibrated** | Month-0 median earner income; calibrated so the household mixture ≈ Salem renter median ~$58k (Census B25118). |
| BandFloorPct, BandCutoffPct_1..4, TailCapMultiple | 0.10 / 0.35 / 0.60 / 1.00 / 1.60 / 4.00 | edges DECLARED; **TailCapMultiple CITED** | B1–B5 %-of-median edges; B5 tail cap 4× = BLS CEO-vs-all-occupations ratio (rejecting the 281:1 EPI ratio as too wide). |
| BandWeight_1..5 | 27/18/21/19/15 | **CALIBRATED** (target CITED B25118) | Band sampling weights (probe-fitted to the renter income shape). |
| WageBandMult_1..5 | 1.00 (all) | MECHANICAL placeholder | Per-band wage-growth multiplier (neutral default; composable). |
| SecondEarnerProb | 0.68 | **CITED** | P(2nd adult earns) — CPS ~0.68 (BLS 2024). |
| CoupleSameBandProb | 0.25 | **CALIBRATED** (target CITED) | Assortative-mating knob → realized couple earnings r≈0.20–0.25 (Schwartz). |
| NoEarnerResidualMaxPct | 0.15 | DECLARED | Non-earning-2nd-adult residual cap. |
| RoommateTopBandDampen | 0.25 | DECLARED (directional cite) | Dampens Band4/5 for ROOMMATES (roommate formation skews lower-income; magnitude uncited). |
| SingleNonEarnerProb | 0.18 | **CALIBRATED** | P(SINGLE is fixed-income/non-earner) — closes a measured ~6pt flattering gap vs B25118 <$25k share. |
| FixedIncomeMinPct / MaxPct | 0.15 / 0.45 | **CITED** | Fixed-income draw range (≈ SSI floor $11k → SocSec avg $23k at the $55k median). |
| &lt;Regime&gt;_WageMean / _WageVol (10) | Normal 3.1%/0.8% · Surge 5.0%/1.2% · Nzn 3.5%/0.8% · DF 0.0%/1.0% · DS 1.5%/1.5% | **CALIBRATED** (anchor CITED EPI) | Per-#48-regime wage growth; tuned as a set so E[rent−wage] = +0.41 pp/yr (inside the ratified 0.3–0.5 floor, evidence §9c). A first draft at +0.10 was rejected as survival-flattering. |

## TNT (16) — applicant/household generation · reader: `tenant_manager`
| Knob(s) | Default | Basis | Meaning |
|---|---|---|---|
| HouseholdType{Single,Couple,Family,Roommates}Weight | 35/30/25/10 | **CITED** | Household-type mix — re-leveled at V00059 to Salem renter shape (~35% single; evidence §4c). |
| CandidateSlateSize | 10 | DECLARED | Applicants generated per vacancy per fill-attempt (funnel denominator + RNG driver). |
| AdultAgeMinYears / MaxYears | 22 / 65 | DECLARED | Synthetic adult-age bounds (cosmetic DOB; no behavioral feedback). |
| ChildAgeMinYears / MaxYears | 0 / 17 | DECLARED | Synthetic child-age bounds. |
| FamilyChildren0..3Weight | 0.1/0.3/0.4/0.2 | DECLARED | FAMILY child-count distribution. |
| RoommatesAdults2..4Weight | 0.5/0.3/0.2 | DECLARED | ROOMMATES adult-count distribution. |

## ACQ (16) — acquisition negotiation · reader: `property_acquisition_manager` · **all DECLARED** (code-literal lifts)
| Knob(s) | Default | Meaning |
|---|---|---|
| Counter{AcceptProbability, JitterLowFactor, JitterHighFactor, ResponseDelayDays, DecisionTimeoutDays} | 0.7 / 0.5 / 1.5 / 1 / 5 | Seller-counter response: jitter the counter, accept w.p. 0.7, delay/timeout. |
| InspectionScheduleDelayDays | 7 | Fixed offset at the scheduling stage (stacks with the `AcquisitionParameters` scheduling delay). |
| RepairCost&lt;Sev&gt;{Min,Max}USD (8) | Clean 0–2k · Minor 2k–10k · Moderate 10k–35k · Major 35k–100k | Absolute-$ repair-estimate bands by inspection severity. |
| PriceReductionShare{Low,High} | 0.3 / 0.7 | Negotiated reduction = repair_cost × U(0.3,0.7). |

## CMPL (17) — compliance sampling · reader: `compliance_manager` · **DECLARED/MECHANICAL**
| Knob(s) | Default | Meaning |
|---|---|---|
| ScheduleOffset{DocReview,SmokeCo,Habitability,DueDiligence}Days | 1/1/2/3 | Days-after-close each work-item type is scheduled. |
| DueDiligence{Minor,Moderate,Major}Duration{Min,Max}Days (6) | Minor 7–21 · Moderate 21–60 · Major 60–180 | Remediation duration bands by severity (CLEAN aliases MINOR). |
| DueDiligenceCostMult{Base,Span} | 0.85 / 0.40 | DD cost = repair_estimate × (0.85 + U(0,1)·0.40). |
| UnitRemediationMinorPct / BuildingRemediationModeratePct | 0.5 / 0.6 | Severity split for spawned remediation. |
| SeverityMult{Minor,Moderate,Major} | 1.0/1.5/3.0 | Duration+cost multiplier by severity. |

## MARKET (60) — property-market state machine · reader: `property_market_manager`
**Standalone:** `SaleSuccessRate`=0.90 **CITED** (NAR); `DailyListingBasePct`=0.015 DECLARED; `DailyListingWindowDays`=30 MECHANICAL; `DaysOnMarket_Mean/StdDev`=26.4/20 **CITED** (FRED Essex, StdDev fit to observed range); `DaysOnMarketMin/MaxDays`=3/365 DECLARED (safety clamps, wider than observed).
**Seasonal families (12 each), all CITED to FRED 2020–25 monthly:** `SeasonalMultiplier_<Mon>` (listing volume, ACTLISCOUMA; peak Oct 1.31), `DaysOnMarket_SeasonalMultiplier_<Mon>` (MEDDAYONMAR25009; slowest Dec 1.80), `PriceMultiplier_<Mon>` (MEDLISPRI25009; peak May 1.08).
**Init family (8):** `Init{Listed,Available}Pct{Mid,Jitter,Min,Max}` (Listed .015±.005 [.01,.02]; Available .04±.01 [.03,.05]) — **DECLARED** (synthetic steady-state).
**Hold-curve family (9):** `HoldLT{2,5,10}YrCumPct` .06/.20/.29 **CITED** (owner survey); `Hold{2,5,10}YrDays`/`HoldFlipMinDays`/`HoldCapDays` = calendar arithmetic MECHANICAL; `HoldTailMeanYears`=8 DECLARED.

## STAFF (13) — staffing & payroll · reader: `employee_manager`
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| BaseAdminCount | 2 | POLICY (evidence-adjacent) | Admin floor (1 Admin Mgr + 1 Property Mgr); self-management design (evidence §2). |
| MaintCrossoverProperties | 15 | **CITED** | First in-house maintenance tech at 15 properties (evidence §5 band 12–17 buildings); 0 FTE below (contracted). |
| MaintTechReducedContractPct | 0.40 | DECLARED | Routine contracted-spend remaining once a tech is in-house (~60% absorbed). |
| UnitsPerAdmin_Early / Late | 25 / 50 | DECLARED (early band cited) | Admin unit-ratio before/after `EarlyAdminYears`. |
| EarlyAdminYears | 5 | DECLARED | Cutover from early→late admin ratio. |
| BenefitsPct | 0.25 | DECLARED/POLICY | Benefits load on base salary. |
| RaiseTier{Top,Mid,Low}CushionMonths | 12/10/8 | POLICY (KD-029) | Cash-cushion thresholds for the raise tiers. |
| Raise{Month,DayOfMonth} | 12 / 31 | MECHANICAL | Annual raise fires Dec 31. |
| PayrollMidMonthDay | 15 | MECHANICAL | First bi-monthly payroll day (2nd = month-end). |

## MAINT (9) — maintenance cost model · reader: `maintenance_event_manager` · **all CITED** (evidence §5)
| Knob | Default | Basis | Meaning |
|---|---|---|---|
| RoutineRequestsPerUnitYear | 5.0 | **CITED** | Routine work-order rate λ (evidence §5c, 4–6/unit/yr). |
| RoutineCostMean | $200 | **CITED** | Mean routine severity (§5c). |
| RoutineDispersion | 1.5 | DECLARED | Negative-binomial dispersion (swept). |
| RoutineCostSigma | 0.70 | DECLARED | Routine lognormal σ (swept). |
| MajorEventRatePerUnitYear | 0.40 | **CITED** | Major-event rate λ (§5, 0.3–0.5/unit/yr) — carries capital replacement directly. |
| MajorEventCostMean | $2,250 | **CITED** | Mean major severity (§5 band $1,500–3,000). |
| MajorEventCostSigma | 1.00 | DECLARED | Fat-tail σ (99th pct ~$14k — the "bad year" the model can't average away). |
| ExteriorPerProperty | $3,000/yr | **CITED** | Exterior/grounds per building (§5b $2,500–4,500). |
| TurnoverCostBase | $2,000 | **CITED** | Make-ready hard cost per turnover (§5a $1,500–3,000, older MA stock). |

## TIMING (8) — durations · reader: `turnover_manager` · **MECHANICAL**
`TurnoverDuration{Inspect,Clean,Paint,FinalInspect}` = 2/3/5/1 days; `Restoration{Min,Max}{Eviction,Voluntary}` = Eviction 15–30, Voluntary 10–30 days (eviction always forces restoration — "a wound", POLICY-flavored). *(`MaxApplicationsPerVacancyPerWeek` was loaded-but-never-enforced — dropped at V00060.)*

## GRT (12) — grants · **⚠ RESERVED / UNIMPLEMENTED (KD-022) — no live reader for any of the 12**
`Chance{Small,Medium,Large}` = 0.20/0.10/0.05; `{Min,Max}Grant{Small,Medium,Large}` = Small $10k–50k, Medium $50k–150k, Large $150k–500k; `Cooldown{Small,Medium,Large}` = 12/24/36 mo. Schema-only design placeholders; `grant_manager.py` does not exist. See `GrantLedger` (data_dictionary §1).

## INF (66) — inflation-regime Markov model (#48) · reader: `inflation_engine` · **DECLARED, anchored to CITED episodes** (evidence §7)
**Standalone:** `Mode`=Regime (Gray-ratified default), `StartRegime`=Normal.
**Static-mode flat rates (4):** `{Rent,OpEx,Property,General}InflationRate` = 3%/2.5%/3%/2% — retained for the attribution baseline. *(`GeneralRate` is written every run but has no downstream reader — dormant.)*
**Per-regime rate params (35) — `<Regime>_{Rent,OpEx,Property}{Mean,Vol}` + `_VacancyDelta`** (annual; engine ÷12 mean, ÷√12 vol; no `max(0)` clamp — negative rates intended):

| Regime | Rent μ/σ | OpEx μ/σ | Property μ/σ | VacΔ | Anchor |
|---|---|---|---|---|---|
| Normal | 3.0/1.0% | 2.5/1.0% | 3.0/2.0% | 0 | CPI 1990-2019 ~2.45% |
| Surge | 12.0/4.0% | 7.0/2.5% | 10.0/4.0% | −0.5pt | 2021-23 rent rip; Salem +34.8% |
| Normalization | 4.0/1.5% | 4.0/1.5% | 3.0/2.0% | 0 | glide-down shape |
| DownturnFinancial | 0.5/1.0% | 2.5/1.0% | **−6.0**/3.0% | +4pt | Case-Shiller −27%, 2008-12 |
| DownturnShock | **−5.0**/3.0% | 2.5/1.5% | 3.0/2.5% | +3pt | 2020 dip + property decoupling |

**Transition matrix (25) — `Trans_<src>_<dst>`** (monthly, rows sum to 1.0; dwell = 1/(1−diag)):

| From \ To | Normal | Surge | Nzn | DF | DS |
|---|---|---|---|---|---|
| Normal | .992 | .004 | 0 | .002 | .002 |
| Surge | 0 | .945 | .055 | 0 | 0 |
| Normalization | .045 | 0 | .955 | 0 | 0 |
| DownturnFinancial | .010 | 0 | .010 | .980 | 0 |
| DownturnShock | .040 | .060 | 0 | 0 | .900 |

Dwell times: Normal ~125mo, Surge ~18mo, Nzn ~22mo, DF ~50mo, DS ~10mo. Downturn cadence ~once/21yr, reconciled to the CITED n=2-in-40yr sample. Zero cells encode "downturns start only from Normal; Surge decays only via Normalization." **Forced-regime** archetype knobs exist only as per-projection rows (not in the 66 global).

---

*Verified against the live V00059 registry + evidence_base + the seed migrations + reader code, via a 6-agent inventory, 2026-07-06. Basis labels are this reference's synthesis; the underlying citations live in evidence_base.md.*
