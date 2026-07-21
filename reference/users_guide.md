# Liberty Bee — User's Guide (the "How", in real terms)

**Audience:** anyone who wants to understand what this simulation actually does, why it does it that way, and how to tune it — including tuning it for a market that isn't Salem, MA. This guide is the readable spine; when you need the exhaustive detail it links **down** to three reference docs:

- [`engine_internals.md`](engine_internals.md) — the turn-by-turn engine (what runs, in what order), the reproducibility model, and a per-module reference for all 26 modules.
- [`data_dictionary.md`](data_dictionary.md) — every one of the 53 tables: purpose, keys, owner, readers.
- [`parameter_reference.md`](parameter_reference.md) — every one of the 322 knobs, with its basis label and reader.

Other companions: [`concept_and_philosophy.md`](concept_and_philosophy.md) (the why), [`business_rules_current.md`](business_rules_current.md) (the exact rules), [`evidence_base.md`](evidence_base.md) (where every calibrated number comes from), [`architecture_overview.md`](architecture_overview.md) (the code map), [`failure_modes.md`](failure_modes.md) (how the model dies — every failure path deliberately forced and verified, with recipes to do it yourself).

**The one-paragraph version:** Liberty Bee simulates 20 years (240 months) of a nonprofit that buys small multifamily buildings, rents them ~10% below market, never raises a sitting tenant's rent, cuts rent as tenants stay longer, and tries not to go broke doing it. The engine runs this movie thousands of times with different random draws (Monte Carlo) and asks: *does this survive?* Every behavioral number is a **knob** — a row in `reference.ParameterRegistry` — so the model can be re-tuned for another city without touching code.

**How to read a knob entry — the four basis labels.** Every behavioral number is a knob (a `reference.ParameterRegistry` row), and every knob carries a *real-world meaning*, a *default*, and a **basis label** that tells you **where the number comes from and how much to trust it.** The label is not decoration — it is the honesty machinery of the whole project. When someone asks "says who?" about any number, the basis label is the answer. The full per-knob catalog with labels lives in [`parameter_reference.md`](parameter_reference.md); this is how to read it.

The quick test — ask two questions of any knob:

1. **Is it a claim about the outside world?** (What vacancy runs, how fast wages grow, how often downturns hit.)
   - …and a real source pins it down → **CITED.**
   - …but the world offers no fittable number → **DECLARED.**
2. **Is it not a world-claim at all?**
   - engine plumbing (a duration, a cap, a processing threshold) → **MECHANICAL.**
   - a choice about how *this org* chooses to behave → **POLICY.**

| Label | What it is | How to trust it / treat it | Example |
|---|---|---|---|
| **CITED** | A real-world quantity grounded in a named source. | Trust it as far as its source and vintage go — both are in [`evidence_base.md`](evidence_base.md). Re-verify when the vintage ages out. Re-tuning for another city means finding *that* city's cited number. | `OPEX.PropertyTaxPerUnit` (Salem FY2026 rate × assessed value); the INC wage bands (Census B25118 / EPI). |
| **DECLARED** | A world-claim where the data genuinely can't fit a number — too few observations, no clean measurement. An **honest, disclosed judgment call**, not a hidden guess. | Treat with suspicion proportional to how much rides on it — which is why declared knobs are **sensitivity-swept**: the docs show how the result moves as you vary them. If a result only holds for one declared value, that's a finding, not a fact. | `INF.*` downturn frequency/severity (n≈2 downturns in 40 years — you cannot fit a distribution to two points). |
| **MECHANICAL** | Engine plumbing with no real-world claim behind it: a duration, a cap, a processing window, a numbering base. | Trust the default as sane engineering; change it only if you understand the machinery. It won't flatter or bias results — it just has to be internally consistent. | eviction execution delay (2 months), TCS `$15k` balance cap, application-throttle windows. |
| **POLICY** | Not a fact about the world at all — a **choice about how Liberty Bee behaves.** The mission dials. | These are *supposed* to be set by conviction, not fit to data — but state the choice openly and own its consequences (a deeper discount reaches more people *and* thins the buffer). Changing a POLICY knob models a *different org*, not a truer world. | `PROP.BelowMarketRentPct` (10% below market), the `RR.*` reduction schedule, `TCS.CreditRate`. |

The line that matters: **CITED and DECLARED are claims about the world (and can be wrong about it); MECHANICAL and POLICY are not** — MECHANICAL is how the engine runs, POLICY is what the org chooses. Mislabeling a POLICY dial as CITED would be a quiet way to smuggle a mission choice in as if the world demanded it; the taxonomy exists to make that impossible to do by accident.

**The anti-flattery rule (read this before tuning anything):** calibrate to *your* market's honest numbers, never to the numbers that make the model survive. If Detroit's vacancy is 22%, put in 22 — a model tuned to flatter is worthless as evidence. (This is a founding tenet; see concept §"model integrity".)

---

## 1. The world in the machine

**The engine is turn-based — it lives its own life one day at a time.** A run is two nested loops: an outer loop over **240 months**, and inside each month a loop over **days**. Every simulated day, the same ordered sequence of subsystems fires — the property market cycles, in-flight purchases advance a step, compliance work ticks forward, move-ins happen, rent is collected, evictions and lease-ends are processed, turnovers advance, and the org decides whether it can afford to start buying another building. Then a few things happen only at month boundaries: payroll (twice a month), the itemized operating bill, the reserve top-up, and the credit-expiry sweep. Order matters — it's the engine's causal spine (rent reductions apply *before* collection so the month bills correctly; the reserve is topped up *last* so next month's growth decision sees the true reserve). Nothing reads the wall clock; the only inputs are the projection, the seed, the engine version, and the reference catalog — so the same seed replays the same 20 years to the cent.

Each subsystem below is one of ~20 **manager** modules, each owning a slice of state (a set of database tables). This section is the readable tour; **for the exact per-day execution order, the module-by-module reference, and the RNG-stream map, see [`engine_internals.md`](engine_internals.md) §3 (the loop) and §6 (all 26 modules).**

### 1.1 Money lives in buckets
All cash sits in named buckets with different rules (`fund_manager`):
- **Cash** — the operating account. Pays everything; funds acquisitions.
- **CSF (Community Stability Fund)** — the protected reserve. It backstops *survival obligations* (payroll, operating costs) when Cash runs short, and is **never** spent on growth, at any level, ever (mission invariant #4). Its size follows a curve — see §5.
- **EIP / Grants** — scaffolded but **not live** in the current engine (reserved for a future version; the knobs exist but nothing reads them — do not treat their behavior as shipped).
- **Escrow / CashHold** — tenant deposits held in trust; earmarks for in-flight purchases.

A month ends badly when Cash + CSF together can't cover payroll or the operating bill — that's a **simulation failure** ("the org died"), and survival rates across many runs are the headline output.

### 1.2 Properties: bought, inspected, then rented
The engine shops from a frozen catalog of **1,673 real Salem-area listings** (2–8 unit buildings, scraped once, never re-scraped at runtime). Buying is a pipeline — offer, response, inspection, negotiation, closing, then *compliance work* (this is pre-1978 Massachusetts: lead paint is the norm) — so a building produces **zero rent for months after purchase**. Time-to-revenue is a real survival variable, on purpose.

Growth is gated by three rules: the reserve must be whole vs its last commitment, Cash must stay above its floor, and only *deployable* Cash (above the floor, after the pending reserve top-up is set aside) funds offers. Translation: **the safety system gets fed before the growth engine.**

*Under the hood:* the market itself is a state machine that cycles independently of Liberty Bee (`property_market_manager` → `simulation.PropertyMarket`) — a listing can sell to someone else and come back years later, so a crash just means cheaper inventory. When LB does buy, `property_acquisition_manager` runs the pipeline as a multi-day state machine (offer → seller response → inspection → negotiation → closing), scoring candidates by yield off each unit's *bathroom-adjusted* market rent, then copies the reference unit into the run's portfolio; `compliance_manager` then drives lead-paint/habitability work items until each unit flips to rentable. One subtlety worth knowing: a property has **two IDs** — a market-pool ID and a separate run-scoped ID minted only once LB owns it. Full pipeline stages, the acquisition gate math, and the tables are in [`engine_internals.md`](engine_internals.md) §6 and [`data_dictionary.md`](data_dictionary.md).

### 1.3 Tenants: generated, screened, housed, protected
When a unit is vacant, `tenant_manager` generates a **slate** of applicant households for it (default 10 per fill-attempt), and this is where the model's realism lives. Each applicant is built in three draws: a **household type** (single / couple / family / roommates, from the Salem-renter-calibrated mix), a **composition** (adult/child counts), and — the important part — an **income**. Income is not a single number pulled from a band; it's the **sum of independently-drawn earners** (`income_model`, the Phase 1.8 rewrite): a couple is a primary earner plus a second earner who *may or may not* work (with realistic assortative-mating correlation), a single might be a fixed-income retiree, and every earner's dollars **grow over the 20 years on the same economic-weather path as rent** (so incomes and rents scissor apart honestly instead of one being frozen). The whole slate is seeded deterministically per `(vacancy, date)`, so the same run always draws the same applicants.

Screening is then three gates in order: a **self-selection** gate (people don't apply for homes over ~40% of their income), a **bedroom-fit** gate (industry-standard "2 per bedroom + 1"), and the **30% affordability rule** (rent must fit inside 30% of income — the mission's definition of affordable, not a lender's). First qualified applicant wins. *(The income model + its knobs: [`engine_internals.md`](engine_internals.md) §6 and [`parameter_reference.md`](parameter_reference.md) — categories `INC`, `TNT`, `QUAL`.)*

Once housed, the tenant-protective machinery runs:
- **New-lease rent = market rent minus ~10%** (`PROP.BelowMarketRentPct`).
- **A sitting tenant's rent never goes up. Ever.** (Invariant #1 — regression-enforced, not just documented.) Rent only reprices when a unit turns over.
- **Rent goes DOWN with tenure:** −5% at 3 years, another −5% at 6, another −10% at 10 (20% total, `RR.*`).
- **TCS (Tenant Credit System):** on-time rent quietly accrues credits (capped at $15k) a household can redeem in a hard month instead of missing rent — an earned cushion, not charity.
- Eviction exists but is deliberately last-resort mechanics: 3 consecutive missed payments file, execution ~2 months later, deposits settle per law.

### 1.4 Costs: itemized and honest (#100)
Operating costs are **itemized per-unit buckets** (property tax, insurance, owner-paid utilities — Salem-calibrated) plus **event-driven maintenance** that arrives the way real maintenance does — lumpy:
- turnover make-ready per actual move-out,
- exterior/grounds per *building* per year (landscaping, snow, gutters),
- routine repairs as random monthly counts (most units quiet, a few loud — statistically overdispersed),
- **major events** (roof, boiler, HVAC) as rare, fat-tailed draws — a single month can eat $14k. This stream *is* the capital-replacement budget; there's no separate smoothed "reserves" line precisely so the model can't hide the bad year an averaged line would smooth away.

Staffing is the mission's cost signature: **the org self-manages** (no profit property-management company — a PM's incentives are the inverse of below-market rent and rent reductions), with 2 in-house people (tenant-ops + business-side) scaling per **unit**, and maintenance **fully contracted until the portfolio reaches ~15 buildings**, then in-house techs per building. Self-management costs more at small scale than hiring a PM would. That is a *deliberate mission cost*, and the docs never call it efficiency.

### 1.5 The economic weather (#48)
Inflation isn't a flat 3% — it's **five regimes** the simulation moves between with a seeded coin-flip each month: **Normal** (~75% of months), **Surge** (2021-style rent rip), **Normalization** (the glide back down), **Downturn-Financial** (2008: property values *falling* for ~4 years while rent freezes, costs rise, vacancy jumps), and **Downturn-Shock** (2020: a short violent rent dip, usually followed by the Surge). Each regime moves rent, costs, and property values *independently* — the scissoring-apart is what stresses a landlord, not the averages. Roughly one downturn per 21 years, matching the historical record; about half of all simulated 20-year windows contain one.

Two things to hold onto: **property crashes don't hurt this org** (it never sells — a crash is cheaper acquisitions), and because rent never rises on sitting tenants, the org is *more* exposed to a rent-freeze downturn than a market landlord — surviving it anyway is the entire thesis.

### 1.6 The reserve curve (#97)
The CSF target is **months of expected operating cost**, and the number of months *tapers as the portfolio grows*: 12 months at today's ~8 buildings, gliding toward a floor of 4 via `4 + 8×√(8/N)`. Rationale: a small portfolio can genuinely see near-zero net cash flow (one bad building is a quarter of everything); a large one can't — but some shocks (a regional recession, a record winter) hit everything at once no matter the size, hence the floor. **The dollar amount still grows the whole time** — only the ratio eases. Never quote the months-taper without the dollars curve next to it.

### 1.7 Reproducibility
Same seed ⇒ identical 20-year history, to the cent, including the economic weather, every maintenance disaster, and every tenant decision (invariant #8). Every random stream has its own dedicated seed offset so subsystems can't contaminate each other — and draws are consumed *unconditionally* (a fixed count per step regardless of which branch is taken), so changing one knob doesn't shift the draw order of an unrelated subsystem. This is what makes the byte-identical canary gate possible. *(The full stream-offset catalog is in [`engine_internals.md`](engine_internals.md) §5.)*

---

## 2. The knobs

> All knobs are rows in `reference.ParameterRegistry` (global default + optional per-projection override). Reads are **fail-loud**: a missing knob crashes rather than silently defaulting. Change values with an UPDATE (experiments) or a migration (permanent).
>
> This section is the **tuner's shortlist** — the knobs you'd actually reach for, grouped by what you touch first. The **complete catalog of all 322 knobs** (every one with its basis label and reader module) is [`parameter_reference.md`](parameter_reference.md).

### 2.1 Market facts — retune these FIRST for a new market

| Knob | Real meaning | Default (Salem, MA) | Basis |
|---|---|---|---|
| `OPEX.PropertyTaxPerUnit` | Annual property tax per unit | $2,250 | CITED (Salem FY2026 rate × per-unit assessed value; re-check yearly) |
| `OPEX.InsurancePerUnit` | Annual insurance per unit | $1,400 | CITED (MA small-building proxy; low-medium confidence) |
| `OPEX.UtilitiesOwnerPerUnit` | Annual owner-paid utilities per unit | $2,000 | CITED, **conservative**: assumes owner pays heat (pre-1978 master-metered boilers). Tune to ~$1,200 only if your stock is verified separately-metered |
| `PROP.VacancyRateBase` | Steady-state vacancy rate | 1.9% | CITED (Salem ACS). **Put in your market's honest number** — 22% Detroit means 22 |
| `PROP.VacancyFluctuationBand` | Normal year-to-year vacancy wobble | ±0.5% | DECLARED. Small-fluctuation realism only — downturn stress comes from the inflation regimes, don't widen this to fake a soft market |
| `MAINT.TurnoverCostBase` | Make-ready cost per move-out (clean/paint/repair) | $2,000 | CITED (older MA stock $1.5–3k; excludes lost rent — vacancy models that) |
| `MAINT.ExteriorPerProperty` | Annual grounds/snow/exterior per building | $3,000 | CITED (Salem 2–8 unit; snow belt) |
| `MAINT.RoutineRequestsPerUnitYear` | Routine work-orders per unit per year | 5.0 | CITED (industry 4–6) |
| `MAINT.RoutineCostMean` | Typical routine repair cost | $200 | CITED |
| `MAINT.MajorEventRatePerUnitYear` | Major events (roof/boiler/HVAC) per unit-year | 0.40 | CITED (0.3–0.5). This stream IS the capital-replacement budget |
| `MAINT.MajorEventCostMean` | Typical major-event cost (fat tail above it) | $2,250 | CITED ($1.5–3k band; 99th percentile ≈ $14k at default spread) |
| `SIM.StartDate` + property catalog | When and where | 2025, Salem-area listings | Swap the `reference.Properties`/`Units` catalog to move cities |

**Distribution-shape knobs** (`MAINT.RoutineDispersion` 1.5, `RoutineCostSigma` 0.70, `MajorEventCostSigma` 1.00) are DECLARED — they set how *lumpy* maintenance is, not how expensive on average. Survival was swept across their plausible ranges (all cells survive); retune only with local claims data.

### 2.2 Mission dials — POLICY, the org's identity (change = changing what LB is)

| Knob | Real meaning | Default |
|---|---|---|
| `PROP.BelowMarketRentPct` | Discount off market rent at signing | 10% |
| `RR.First/Second/ThirdReduction*` | Tenure rent cuts: −5% @36mo, −5% @72mo, −10% @120mo | 20% lifetime max |
| `QUAL.MaxRentToIncomeRatio` | Affordability gate: rent ≤ this share of income | 30% |
| `QUAL.MaxOccupantsPerBedroom` / `Bonus` / `Studio` | Bedroom-fit standard ("2 per bedroom + 1"; studio its own cap) | 2 / +1 / 2 |
| `TCS.CreditCap`, `TCS.RedemptionBand_*` | Earned-credit ceiling ($15k) + how often household types lean on it | see registry |
| `LEASE.EvictionMissedThreshold` / `ExecutionDelayMonths` / `EvictionCost` | Eviction: 3 straight misses, ~2 months to execute, $1,500 cost | 3 / 2 / $1,500 |
| `STAFF.BaseAdminCount` | The un-outsourceable core team (tenant-ops + business-side) | 2 |
| `FIN.CashFloorMonths` | Unrestricted-Cash floor (months of OpEx) below which growth stops | 3 |

*(Tier slots 4 and 5 are absent-reserved — to add a deeper tier, seed both an `RR.<Nth>ReductionMonths` and `...Pct` row. The historical 4th-tier seeding artifact was retired at V00051/#104.)*

### 2.3 The reserve curve (#97)

| Knob | Real meaning | Default | Basis |
|---|---|---|---|
| `CSF.ReserveMonthsPeak` | Months of expected OpEx held at small scale | 12 | CITED-adjacent (nonprofit norms; = the historical flat target) |
| `CSF.ReserveMonthsFloor` | Months never tapered below (correlated shocks don't diversify) | 4 | CITED (LIHTC affordable-housing anchor) |
| `CSF.ReserveCurveN0Properties` | Building count that still holds the full peak; taper starts above | 8 | DECLARED (continuity default; swept 8/12/15 — moves the target ≤ ~1–2 months mid-scale) |
| `CSF.TopupFractionPerMonth` | How much of a reserve deficit gets refilled per month-end | 1.0 (full) | MECHANICAL |
| `CSF.GracePeriodMonths` | Startup window where the reserve gate doesn't block first acquisitions | 6 | MECHANICAL ("no homes ⇒ no tenants to protect") |

### 2.4 The economic weather (#48) — 5 regimes × 7 numbers + a transition matrix

Per regime (`INF.<Regime>_*`): annual **RentMean/RentVol**, **OpExMean/OpExVol**, **PropertyMean/PropertyVol**, and a **VacancyDelta** (percentage points added to the vacancy base while active). Defaults, all CITED-shaped but DECLARED in their exact values (evidence §7):

| Regime | Rent | OpEx | Property | Vacancy | Real-world anchor |
|---|---|---|---|---|---|
| Normal | +3.0% | +2.5% | +3.0% | — | the boring decades |
| Surge | +12% | +7% | +10% | −0.5pt | 2021–23 |
| Normalization | +4% | +4% | +3% | — | 2023–25 glide-down |
| DownturnFinancial | +0.5% | +2.5% | **−6%** | **+4pt** | 2008–12 (the survival stressor) |
| DownturnShock | **−5%** | +2.5% | +3% | +3pt | 2020 (dip → usually Surge) |

`INF.Trans_<From>_<To>` (25 rows) are **monthly move probabilities**; staying-put is the diagonal, and average regime length = 1/(1−diagonal). The load-bearing dial is **downturn frequency**: `Trans_Normal_DownturnFinancial` + `Trans_Normal_DownturnShock` = 0.004/month ≈ **one downturn per ~21 years** (DECLARED — history offers n=2 in 40 years; swept at 0.5×/1×/2×). `INF.Mode` = `Regime` (default) or `Static` (flat, for comparisons); per-projection `INF.ForcedRegime`+`StartMonth`+`DwellMonths` scripts a single archetype window for scenario runs. The 4 flat `INF.*InflationRate` knobs are the Static-mode rates.

**Tuning for another market:** regime *means* are national-macro shaped; your local market mostly enters through the market-facts table (§2.1) and the property catalog. Touch the transition matrix only to express a different macro view — and say so in your fork's evidence base.

### 2.5 Staffing scale points

| Knob | Real meaning | Default | Basis |
|---|---|---|---|
| `STAFF.UnitsPerAdmin_Early` / `_Late` (+`EarlyAdminYears`) | Units per admin before a 3rd+ hire (tighter in the first 5 years) | 25 / 50 | DECLARED (per-unit because one lease = one point of contact, regardless of household size) |
| `STAFF.MaintCrossoverProperties` | Building count that justifies the first in-house maintenance tech | 15 | CITED-adjacent (industry ~12–17 buildings; below it: zero maintenance staff, fully contracted) |
| `STAFF.MaintTechReducedContractPct` | Contracted routine spend remaining once a tech is in-house | 40% | DECLARED (tech absorbs ~60% of routine; specialty stays contracted) |
| `STAFF.BenefitsPct` | Benefits load on salary | 25% | MECHANICAL |

### 2.6 Tenant-behavior dials (mostly MECHANICAL/DECLARED)

`PAY.BaseFailProbMonthly` (2%/mo payment-failure hazard) · `LEASE.RenewalRatePct` (80% renew at term) · `LEASE.LandlordNonRenewalProbPct` (10%) · `LEASE.EarlyBreakProbMonthly` (0.25%/mo) · `TNT.HouseholdType*Weight` + `INC.*` (household mix + the sum-of-earners income model — who applies) · `TNT.CandidateSlateSize`, `TIMING.*` (application throttle, lease timing, turnover work durations) · `DEP.*` (deposit installments, damage odds, settlement timing) · `PROP.VacancyMeanDays/MaxDays` (how long refilling a unit takes: ~30 days, capped 120). These shape realism texture; retune from local data if you have it.

### 2.7 Reserved, not live (don't tune expecting effects)

`GRT.*` (grants) and `FIN.EIP*` (Experimental Initiatives Program) are scaffolding for a future version — **no engine code reads them today**. ✅ The audit ruled: the conflicting/vestigial rows (`TNT.VacancyRate`, `TNT.BaselineRenewalRate/BaselineTenancyMF/SF/TenantTurnoverRate`, `TCS.MaxRedemptionMonths/RedemptionFrequency`, `QUAL.UseConservativeIncomeEstimate`, `TIMING.LeaseStartDayOfMonth/LeaseUpWindowDays/MoveInLagDays`) were confirmed reader-less and **retired at V00052**. `LEASE.*` and `PROP.*` are authoritative.

---

## 3. Running it

```bash
# 1. Create a disposable test database (restores the Gold snapshot + applies migrations)
python environmentscripts/migration_manager.py --label my-experiment

# 2. Run one 20-year life (seeded — same seed, same history)
python app/src/simulation.py --env <LibertyBee_Test_...> --projection-id 206 --months 240 --seed 12345

# 3. Prove nothing broke (24-module regression suite)
python app/src/master_test_runner.py --env <db> --regression --clean

# Clean up
python environmentscripts/migration_manager.py --list
python environmentscripts/migration_manager.py --drop <name> --yes
```

A **projection** is a named scenario: a starting-capital level plus any per-projection knob overrides (the V1 ladder is $5M–$8M, IDs 200–206; 220–222 are stress scenarios). Survival statistics come from sweeping many seeds per projection.

## 4. Reading the results honestly

- **Survival** = Cash + CSF > 0 at month 240 — a *minimum solvency* bar, not a quality bar.
- The headline number is the **stochastic-regime** survival rate; Static and per-archetype numbers are labeled comparisons (the matrix), never the headline.
- **Never** quote a months-of-reserve taper without the absolute-dollars curve beside it.
- The expense ratio (~58% of rent at baseline) is *above* a lean private operator's 45–48% — by design, explained by below-market rents (smaller denominator) and mission self-management (deliberate cost). Presenting a lower, prettier number would be the flattery this project exists to refuse.
- If a knob change makes survival look better, ask *"says who?"* before believing it. Every load-bearing number should trace to [`evidence_base.md`](evidence_base.md) or be flagged DECLARED with a sweep.

## 5. What the model does *not* do (known limitations)

Stated plainly, because an honest model names its own edges. None of these affect the frozen 1.0 survival numbers unless noted; the full engineering list is in [`../v_0_3/phases/phase_1_9/engine_hygiene_findings.md`](../v_0_3/phases/phase_1_9/engine_hygiene_findings.md), and the mission-tension items are tracked as KDs.

- **Tenants don't have lives yet.** Hardship is a monthly probability, not a story; there are no second-order relations, no life events, no aging (ages are cosmetic, and a generated family can even have mismatched surnames). Deep per-tenant lives are the V0.4 tenant engine (#111) — the single biggest realism gap.
- **Two eviction/deposit mechanics fight the mission and are flagged, not hidden:** eviction has **no cure period** (paying up during the 2-month window doesn't stop execution — KD-031), and a partially-funded installment deposit forfeits money already paid in on any early exit (KD-032). Both are documented tensions awaiting the tenant-engine era.
- **The rent/market model is coarse:** market rent is `f(bedrooms)` plus a bathroom bump — no square footage, location, or condition. And in 1.0 the engine charges tenants off `BaseRent` while acquisition underwrites off the bathroom-adjusted `AdjustedRent` (#44), with a fractional-bath overpricing bug (KD-040) — both **acquisition-scoring-only in 1.0** (no tenant rent, no published number moves) and both fixed in V2.
- **Some things never happen in V1:** `LATE` payment status is structurally never written (grace-window collection lands ON_TIME); staff are hired but never terminated; grants and the EIP are scaffolded but unimplemented (no code reads them); compliance remediation always succeeds.
- **Vacancy is steady-state.** Cyclical soft-market stress enters *only* through the #48 inflation regimes' vacancy deltas — there's no independent vacancy-shock model. Don't widen `PROP.VacancyFluctuationBand` to fake a downturn.

*Guide vintage: **release 1.0, shipped publicly 2026-07-10** (post #100/#97/#48/#104 + the Phase-1.5 audit cleanup + Phase-1.8 income realism + Phase-1.10 tenant retention; baseline of record = the **V03R4** retention corpus, engine **0.5.0**, re-frozen 2026-07-08 — supersedes the 2026-07-06 V03R3 freeze). Registry snapshot: **328 global knobs through V00064** (322 at V00059; −1 release-hygiene V00060; +7 Phase-1.10 `RET.*` retention knobs at V00063). MARKET/ACQ/CMPL knob sections land in the v2 build-out (#141).*
