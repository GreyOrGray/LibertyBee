# Liberty Bee — Business Rules

**Status:** ✅ Canon — the current, authoritative rules, consolidated from the locked first-generation ruleset and owner rulings.
**Scope:** The *why* is in [`concept_and_philosophy.md`](concept_and_philosophy.md). Numeric parameters live in the unified EAV **`reference.ParameterRegistry`** (the canonical param store; read-once + fail-loud). The legacy `reference.ProjectionParameters` wide table is superseded as the source of truth. **The real-world evidence behind these numbers — "says who?" — is catalogued in [`evidence_base.md`](evidence_base.md).**

> **Status (release 1.0):** the policy surface is **registry-driven + fail-loud** (328 global knobs); the staffing and reserve corrections are in the current engine. The rules below reflect **current engine behavior**. The **publication-grade re-baseline has SHIPPED and is FROZEN** as the **1.0 baseline of record** (corpus V03R4, engine 0.5.0, ratified): headline survival numbers are now **canonical — see §6**. Inline **⚠️** tags point to still-open *design* questions (e.g. CSF sizing) or V2 deferrals (e.g. rent-column sourcing), **not** broken code.

---

## 1. Definitional invariants (hard rules)

These are mission-level commitments, not tunables. Violating one is a **mission breach**, not a calibration choice (see the invariants checklist in `concept_and_philosophy.md` §5):

1. **No rent increase for an existing tenant** — ever. Inflation applies to a unit's rent only at **turnover** (new tenant).
2. **Below-market rent** — LB-owned units rent at **10% below market** (`PROP_BelowMarketRentPct`); permanent policy, not a temporary subsidy.
3. **Deposits are separate** — never applied to rent / arrears / fees.
4. **Deposit forfeiture is immediate** — an **unfunded** deposit at termination forfeits with no grace period. *Why (not punitive): LB lets tenants **fund the deposit over a short period** rather than paying it up-front (a tenant accommodation); a **never-funded** deposit was never paid in, so there is nothing to return. **⚠️ This justification covers only the never-funded case:** on the installment path a **partially-funded** deposit forfeits the money already paid in — real tenant funds — on **any** early termination (eviction *and* voluntary exit); the "nothing to return" framing does not cover it. Mission-tension: tracked as a known divergence. (Deposit only — distinct from TCS forfeiture, which follows the 2-yr portability window; see §3.)*
5. **Eviction always requires restoration** — every eviction incurs a restoration cost + operational consequence; never a clean/neutral event.
6. **Mission-locked surplus** — reinvested in tenants / property / CSF / EIP; never extracted as profit.
7. **TCS is not ownership/equity** — a housing-stability credit, not savings toward ownership (home-purchase removed as a core promise, locked).
8. **Seed-reproducible** — all randomized behavior derives from the run seed (never RunID — a bit-stability lock); same seed ⇒ same run. Two seeding patterns coexist by design: per-manager offsets (e.g. vacancy 30708, renewal 30901) and **content-hash seeding** (compliance work items — order-independent); don't "normalize" one into the other.
9. **Zero-debt / outright ownership** — LB acquires properties **only** from deployable Cash; the engine has no mortgage, debt-service, or interest line (§4 "Funding source"). The structural enabler of anti-extraction: no lender demanding a return → no pressure to raise rent or cut maintenance, and LB can **absorb its mission's cost** (foregone re-pricing on flat sitting rents, the retention it now models) and simply grow slower where a leveraged below-market operator would default. Taking on debt is a mission breach, not a financing choice (`concept_and_philosophy.md` §5 #9).

---

## 2. Tenant lifecycle

### Qualification
- **30% rent-to-income** — strict qualification ceiling (`TNT_MaxRentToIncomeRatio = 0.30`). *V1 uses **rent-only** burden; utility-inclusive burden is deferred unless/until utility estimates are modeled.*
- **40% pre-screen** — behavioral self-selection (`PreScreenRentToIncomeRatio = 0.40`): optimistic households apply between 30–40%; the 30% rule is unchanged (this is realism, not policy relaxation).
- **Applicant-pool abstraction** — applicants are generated programmatically per seed at runtime; this models available demand without pre-materializing all possible applicants. Only *selected* tenants are materialized.

### Vacancy applicant funnel & selection (locked)
- **Candidate slate:** `CandidateSlateSize` candidates generated **per vacancy per day** (default **10**) — the denominator of all funnel metrics and an RNG-consumption driver.
- **5-stage funnel**, two hard invariants: `GeneratedCandidateCount = SelfScreenedCount + AppliedCount` and `AppliedCount = RejectedCount + SelectedCount`. Only **applied** (passed pre-screen) candidates count as "applicants"; **self-screened candidates are counted via Vacancy counters, NOT persisted as rejection rows.**
- **Selection = first-qualified, NO ranking:** candidates evaluated in deterministic order; the **first** applicant passing all filters is selected — no income-maximizing ranking, no random pick among qualified (mission-aligned fairness; later qualified → `EARLIER_QUALIFIED_SELECTED`). *Do not "improve" this into best-fit ranking.*
- **Bedroom-fit = max-occupancy only:** `MaxOccupants = Bedrooms × QUAL.MaxOccupantsPerBedroom + QUAL.MaxOccupantsBonus` — the industry-standard **"2 per bedroom + 1"** (studio uses its own `QUAL.MaxOccupantsStudio = 2`); defaults give studio 2, 1BR 3, 2BR 5, 3BR 7, 4BR 9. A **max**, no minimum (an early min-occupancy rule caused 0% fill and was corrected). Registry-driven (was a hardcoded `Bedrooms × 2`, a familial-status fair-housing tension — see `evidence_base.md` §3; age-aware revisit deferred). Policy, not biological constraint. **Public framing (binding):** say "industry-standard 2 per bedroom + 1" — NEVER "Keating/fair-housing/legally compliant" (Keating is a 6-factor test; we model bedroom count only).
- **Income:** sampled **within** the household's income band (not min/midpoint); pre-screen + qualification use that **sampled** monthly income. *(Band-minimums undercount applicants — locked.)*
- **Pre-screen mechanics:** applies if `rent/income ≤ 0.40` (strict `>` self-screens out); **zero/NULL income self-screens out** (no crash); runs **before** bedroom-fit and the 30% qualification.
- **Demand model** *(income realism)*: household mix **SINGLE 35 / COUPLE 30 / FAMILY 25 / ROOMMATES 10** (`TNT.HouseholdType*Weight`). **Income is generated, not banded from a static table** — the old `reference.IncomeBand` (LOW/MED/HIGH/VERY_HIGH) is **dropped**. Each household's income = the **sum of independently-drawn earners** by household type (`income_model.py`), from **%-of-median AMI-relative bands B1–B5** off `INC.EarnerMedianAnnual` (**$55K**), with **regime-coupled wage growth** (wages ride the same regime path as rent, dedicated RNG stream → same seed = identical market, only incomes vary) and **fixed-income non-earner singles** (`INC.SingleNonEarnerProb = 0.18`, to represent the SSI/SocSec floor). Qualification is **pooled** across earners. Full basis + AMI/mission-scope: [`evidence_base.md`](evidence_base.md) §9.
- **Rejection taxonomy:** `SELF_SCREENED_UNAFFORDABLE`, `INCOME_INSUFFICIENT`, `BEDROOM_FIT`, `EARLIER_QUALIFIED_SELECTED`.
- **Lease timing:** 12-month fixed term; `LeaseSignedDate` = selection date; `LeaseStartDate` = first of following month (= MoveInDate).
- **Dormant:** `MaxApplicationsPerVacancyPerWeek = 10` (DB-confirmed) is **loaded but not enforced** (not an active throttle). **Retired:** `UseConservativeIncomeEstimate` — zero code references (the sampled-income rule runs unconditionally); deleted with the 10 other orphaned registry rows in the pre-re-baseline cleanup.

### Security deposit
- `DepositRequiredAmount` = **first month's rent**. Paid either **up-front (FULL)** or **funded over time (INSTALLMENT)** — a deliberate **lower barrier to entry**.
- **INSTALLMENT = 4 monthly payments**, due alongside rent in **Months 1–4** — first installments at rounded rent/4, **final installment remainder-aware (pays the exact remaining balance)** so arbitrary rent values fund precisely (locked). The FULL-vs-INSTALLMENT choice is made at lease execution and is **immutable**; pay-ahead is allowed; fully funded by end of Month 4. Funding status: `deposit_funded = (escrowed ≥ required)`.
- **The monthly obligation is atomic:** rent + that month's installment = **one payment with one grace window** — a short installment makes the **entire** payment LATE.
- Held in **escrow**, separate from Cash/CSF/EIP operating funds (invariant #3).
- **At termination:** a **funded** deposit → damage assessment → refund/withhold; an **unfunded** deposit **forfeits immediately** (invariant #4) — on the installment path **all installments paid to date are forfeited, no refund** (the escrowed partial moves to Cash as income); for a never-funded deposit there is simply nothing to return. *⚠️ Accurate as-built — but mission-tension: the partial-funding forfeiture seizes **real** tenant money and applies to **voluntary** exits too, not just eviction (a known divergence, fix horizon TBD).*
- **Damage withholding (locked):** voluntary/non-renewal exits draw damage **70% $0 / 20% $200–1K / 10% $1K–deposit**; eviction-biased **40/35/25**; uniform within bucket; **capped at the funded deposit — excess logged, not collected**. Outcome decided at termination; **cash moves after `DEP_SettlementDelayDays` (default 30)**; outcomes `FULL_RETURN / PARTIAL_RETURN / FORFEITURE`. **Forfeited deposits transfer escrow → Cash immediately as non-rent income** (normal CSF logic applies).
- ✅ **Verified (code trace 2026-06-09): settlement is damages-ONLY — arrears never touch the deposit.** `security_deposit_manager.py` contains zero arrears logic; `LeaseTermination.ArrearsAtExit` is a **record** (write-off bookkeeping by eviction/renewal managers), never an input to settlement. The earlier "arrears deducted from deposit" narrative was a wrong inference from column names. Invariant #3 holds absolutely, at exit included.
- *Deposit-insurance alternative: **stretch, not current** (owner-confirmed). It appears only in an earlier inferred narrative — the actual schema `CHECK` constraint allows only `FULL`/`INSTALLMENT`.*
- Mechanics: `simulation.LeaseDeposit`, `security_deposit_manager.py`.

### Rent over the tenancy
- **Entry:** new tenant pays `BaseRent × (1 − 10%) × cumulative_rent_inflation` — a new tenant **never inherits the prior tenant's tenure-reduced rent**.
- **Never increased** thereafter (invariant #1). Rent is **due strictly on the 1st**; **no proration ever** (mid-month signing → lease starts the 1st of the following month, no partial-month rent — locked; proration is a daily-sim stretch item).
- **Rent-reduction schedule (locked semantics):** reductions **deepen with tenure**, at milestones/percentages set by the `RR_*` projection parameters (tunable/sweepable). **Canonical schedule = 3 reductions:** 36 mo → 5%, 72 mo → +5%, 120 mo → +10% (**20% cumulative** max). ✅ **Resolved (ratified):** the unintended 4th-tier artifact (180 mo → +5% → 25%, inconsistently seeded, always excluded from published results) is **retired entirely** — all `RR.Fourth*` rows deleted; **slots 4 AND 5 are uniformly absent-reserved** (absence = inactive, the tier-builder's idiom; opt-in later = seed BOTH the Months and Pct rows). Direction rider: the correction is slightly pro-institution (leases past 180 mo tenure lose a silent 5%; thin tail; listed in the re-baseline attribution). An earlier interim baseline still carries the artifact (~715 long-tenure leases) — one more reason its numbers are directional-only. **Public framing (binding):** "corrected a seeding artifact to match the ratified 3-tier/20%-max policy" — NEVER "removed a tenant benefit" (the 25% was never promised, never published). Locked policy regardless of values:
  - **Cumulative off the original signed rent — never compounding.** `MonthlyRent` is immutable; `EffectiveMonthlyRent = MonthlyRent × (1 − CumulativeRentReductionPct)`. No separate floor — the bottom is just the tier sum.
  - **Clock = continuous occupancy of the current unit.** Renewal continues it; **moving to a different LB unit resets it** — a **deliberate, owner-confirmed asymmetry** with TCS (household-portable): TCS rewards the LB relationship, reductions reward stability in a particular home (see philosophy §4). No carryover at turnover. Occupancy-based, **not** payment-performance-based.
  - **All rent-derived calcs use effective rent** — late-fee base, TCS accrual/redemption, FME.
- *Why:* reduces displacement risk over a long tenancy, rewards rootedness (see philosophy doc).

### Payment status (locked)
- **ON_TIME** — paid on/before grace-period end. No fee. Resets the consecutive-missed counter.
- **LATE** — paid after grace but before month-end. Fee applies. Resets consecutive-missed.
- **MISSED** — no payment by month-end. No immediate fee; **increments** consecutive-missed.
- **Parameters:** grace = **5 days** (`PAY_GracePeriodDays`, rent due the 1st, grace days 1–5, no weekend/holiday adjustment); late fee = **5% of monthly rent** (`PAY_LateFeePercent`), applied only to LATE months; fee booked inside the single fund entry (`LateFeeAmount` analytic-only).
- **Eviction trigger:** **3 consecutive MISSED**. A catch-up payment resets the counter.
- ⚠️ **V1 limitation (documented, not a bug):** in normal V1 flow **LATE is never actually written** — grace-window collection lands ON_TIME, otherwise month-end MISSED. Eviction rarity is likewise a documented V1 assumption ("Outcome C — working as configured; do not tune for visibility").

### Lease lifecycle — renewal & exits (locked)
- **Lease-end pre-roll** (day 1 of the final billable month, ~30 days before end): **landlord non-renewal is checked first** — `LEASE_LandlordNonRenewalProbPct = 10%` chance **only if** payment-problem history (≥2 months `LATE` **or** `MISSED` in the rolling prior 12; `LEASE_LateMonthThreshold = 2`, DB-confirmed). **Otherwise (a retention model, not a flat coin-flip):** `voluntary_exit = clamp(base_exit·(1−β·effective_discount)·(1−γ·external_scarcity), floor_exit, base_exit)`. `effective_discount` = the tenant's current below-market + tenure deal (`1 − EffectiveMonthlyRent ÷ current market rent`; deepens with tenure — the mission mechanism, no separate loyalty knob); `external_scarcity` = the **regional** difficulty of leaving (`1 − regional_availability×affordability`, off the regime vacancy channel + reconstructed household income) — **not** LB's own occupancy. `base_exit = 1 − LEASE_RenewalRatePct/100 = 0.20` (RenewalRatePct **reinterpreted** as the market-equivalent zero-discount exit base — value unchanged from V1's 80). Bounded to `[RET.FloorExitAnnual = 5%, base_exit]`; 7 `RET.*` registry knobs. Owns no RNG — the single `renewal_rng.random()` draw is unchanged (invariant #8). *(Measured: turnover ~13.5%, mean tenure ~4.9yr, exit-hazard falls with tenure; scarcity a disclosed minority ~28% of retention — the deal is primary.)*
- **Renewal = same LeaseID, +12 months, rent unchanged** (the operational enforcement of invariant #1). Late/missed history **persists** across renewal; decision state clears.
- **Early break:** monthly hazard `LEASE_EarlyBreakProbMonthly = 0.25%` per active lease; penalty logged (`EarlyBreakPenalty`) but **no cashflow** in V1.
- **Re-lease after vacancy = NEW LeaseID** with **all lease-specific state reset** — reduced rent, reduction schedule, arrears, eviction flags, relief counters, deposit obligations.

### Eviction mechanics (locked)
- **Filed at the end of the 3rd missed month** (month-end processing); **executed 2 months after filing** (`EXECUTION_DELAY_MONTHS = 2`). Unit is `Pending_Move_Out` during proceedings; turnover starts only at execution. *⚠️ No post-filing cure: paying in full during the 2-month window does **not** halt execution — there is no rescission path (a documented gap).*
- **Flat eviction cost $1,500, paid from Cash** — plus forced restoration + ~2 months' lost income: the concrete machinery behind invariant #5.
- **Arrears window** = all months from first miss through execution (including months during proceedings), **written off at execution** — but **write-off is accounting metadata, NOT forgiveness**: post-termination payments may still settle arrears via the standard ordering; payment never restores the lease or erases MISSED history.

### Turnover (locked)
- **5 sequential work items per unit:** INSPECT 2d → CLEAN 3d → PAINT 5d → **RESTORATION** (conditional: `damage > 0 OR was_eviction`; 10–30d voluntary / 15–30d eviction) → FINAL_INSPECTION 1d (always passes in V1). Sequential within a unit, parallel across units.
- **Eviction always forces RESTORATION — even at $0 damage** (the "eviction is a wound" rule, operationalized). A unit **cannot be leased until turnover completes** (~11d voluntary / 21–31d eviction of forced vacancy).
- **Fund movement (supersedes the "absorbed in the baselines" note):** turnover make-ready is an explicit `MAINT.TurnoverCostBase` charge (~$2,000/turn base-year, OpEx-inflated), counted stateless off `TurnoverWorkOrder` `WorkSequence=1` and folded into the single month-end aggregate-protected OpEx call. The old double-counting caveat is resolved — the retired OpEx aggregate that "absorbed" turnover is gone, so the explicit charge does not stack.

### Unit states & vacancy detection (locked)
- **UnitStatus machine:** `Compliance_In_Progress → Available → Occupied → Pending_Move_Out / Turnover → Available`. Load-bearing rule: *a unit must not be `Available` until it is actually rentable.*
- **Vacancy creation** = `Available` + no active lease + no open vacancy (idempotent; deterministic `ORDER BY PropertyID, UnitID`). `VacancyStartDate = turnover_completion + 1 day`; three-date semantics `VacancyStartDate / TargetFillDate / VacancyEndDate`.

### Payment mechanic & application order (locked)
- **Payability decided once per month; timing day-by-day.** At month start, `PayableThisMonth = (rng.random() ≥ PAY_BaseFailProbMonthly)` (`PAY_BaseFailProbMonthly = 0.02`). Daily retries through the month decide *when* it lands (→ ON_TIME / LATE) but **never re-roll** that monthly probability. Load-bearing for seed-reproducibility.
- **Application order** of any payment — a single **atomic, all-or-nothing** event (no partial satisfaction): **(1) current-month rent → (2) rent arrears (oldest first) → (3) late fees.** Deposits never apply here (invariant #3).

### Eviction & relief (humane-design — 4 tiers; see philosophy doc §4)
> ⚠️ **Intended ladder — NOT wired in V1** (a documented gap). The V1 engine implements only **self-cure** (pay the full arrears+rent+late-fee bundle, all-or-nothing, before the 3rd miss) and then **deterministic eviction at 3 consecutive MISSED**. Tiers 1–3 below (payment plans, structured intervention, relief-before-eviction) do **not** exist in V1; the one partial exception — TCS hardship redemption — is locked out from the 2nd miss by a good-standing gate, and there is no post-filing cure. Retained here as the design *target*; tracked as a known divergence.
1. Temporary inability → credits / relief / payment plans / grace / CSF hardship.
2. Repeated instability w/ recovery potential → structured intervention to restore standing (not maximize penalties).
3. Persistent nonpayment w/o recovery → eviction possible, only after relief exhausted.
4. Bad-faith / destructive tenancy → separate category (humane ≠ naive).
- Every eviction → restoration process (account reconciliation, unit restoration, re-leasing, TCS handling, financial-impact logging, post-event review).

### Vacancy fill (locked)
- Duration: truncated exponential, ~30-day mean, **120-day cap, 1-day floor** (`max(1, min(int(expovariate(1/30)), 120))`), seeded RNG offset 30708. `TargetFillDate` is a hard eligibility gate — no fill before it.

---

## 3. Tenant Credit System (TCS)

TCS is **household-scoped** and **not ownership/equity** (invariant #7).

- **Accrual rate:** `TCS_CreditRate` of qualifying rent. **Canonical = 10%** ("to the spirit") — **DB-confirmed in the live rows** (the 13 per-projection rows: baseline rungs 200–209 + stress rungs 220–222); a **sweepable projection parameter** (5% = conservative sensitivity). Tune in `reference.ParameterRegistry` (the legacy `reference.ProjectionParameters` wide table is no longer read at runtime).
- **Accrual gate (when it starts):** accrual begins only once the **deposit is fully funded** (for a 4-month installment plan, the "Month 5" worked example; **no extra one-month delay** beyond funding).
- **Accrual eligibility (what earns):** accrues **only on `ON_TIME` rent actually paid**, basis = `AmountCollected − ArrearsAmount − LateFeeAmount`. A LATE catch-up, arrears, and late fees earn **nothing**.
- **Cap — CURRENT-BALANCE, refillable (NOT lifetime):** `TCS_CreditCap = $15K`/household caps the **current balance** (`headroom = cap − CurrentBalance`; final accrual is partial, landing exactly at the cap). A household that **redeems below the cap accrues again** — TCS is an *ongoing* stability benefit, not a one-time lifetime allotment. *(A lifetime cap would silently end the benefit for the longest-tenured tenants — backwards for a mission that rewards stability.)* `LifetimeAccrued` is tracked for audit/reporting only and **does not gate accrual**. *(Owner ruling, enforced in `tenant_credit_manager.py`; this **corrects prior canon text that wrongly read "lifetime-accrued"**.)* (Cap rationale: downpayment bound for a possible future ownership program + expectation/claims management — see philosophy doc.)
- **Redemption:** annual — **1 routine OR 1 hardship per calendar year**, **up to 1 month's rent** (`TCS_MaxRedemptionMonths = 1` — an **upper bound, not a floor**; whole-month for routine, no partial). *(Cap interpretation governs + supersedes an older "1-month minimum" reading — owner-confirmed.)*
- **Final-Month-Exit (FME):** at lease end, tenant in good standing — **fires deterministically (prob = 1.0) when eligible**; **hard cap = MIN(CurrentBalance, EffectiveMonthlyRent) — 1 month, period** (3-month and uncapped options explicitly rejected); partial-month balance allowed; **exempt from the annual redemption limit** — a tenant may take both a routine redemption *and* an FME in their exit year. Locked framing: **"TCS is a rent-stability credit program, not an unlimited exit cash-out benefit."** *(The later ruling governs over an earlier "consumes the year" reading — owner-confirmed.)*
- **Measurement:** the V1 TCS success criterion is "**blocked plausibly-usable credit should be low**" (4-category forfeiture decomposition; replaced the older ≤15%-forfeiture target). FME mechanisms brought tenant-reaching TCS to ~79.6% in an earlier baseline.
- **Scoping note:** household scoping is enforced **in code** — the credit tables are TenantID-keyed with no household linkage at schema level (don't "discover" a contradiction there). The TCS tables also carry documented run-isolation divergences.
- **Exit settlement ordering:** **damages settle against the security deposit ONLY — TCS may never cover damages.** Final-month rent may draw TCS only if annual-redemption-eligible with sufficient balance. *TCS is not damage insurance, not cash, not a general receivable offset.*
- **Forfeiture by exit path:**
  - **Voluntary / non-eviction exit** → credits stay redeemable through a **2-year portability window** (transfer between LB units), then forfeit. *(Param-governed by `TCS.PortabilityYears` (=2 → 24 mo) — the engine sets the expiry from the param at the `SystemExitDate` write sites instead of hardcoding 24 months.)*
  - **Eviction** → at execution, **all remaining TCS forfeits immediately — no partial, no portability window.**

---

## 4. Financial structure

- **Three buckets by design — TWO in practice (⚠️ a documented divergence):** **Cash** (operating), **CSF** (reserve), **EIP** (experimental initiatives). **V1 as-run is effectively a two-bucket model**: EIP is scaffolded but never written ($0 across all 800 baseline sims) and **Grants are unimplemented** (`grant_manager.py` doesn't exist) — both locked `V1_RESERVED_NOT_IMPLEMENTED`, deferred V2+. The EIP/grant rules below are **intent, not live behavior** — do not advertise them as shipped.
- **Starting funds:** `FIN_StartingFunds` — sweep rungs **$2.0M–$11.0M** (16 rungs: IDs 200–209 for $5–11M, plus the synthetic low-funding rungs 300–305 for $2–4.5M) — **DB-confirmed**.
- **CSF target (reserve curve, ratified):** `reserve_months(N) = 4 + (12−4)×√(8/N)` months of **expected** OpEx (itemized-OpEx basis), **N = PROPERTIES owned**, capped at the peak (N ≤ 8 holds the full 12). Params: `CSF.ReserveMonthsPeak=12` / `ReserveMonthsFloor=4` (LIHTC anchor; correlated-tail hedge) / `ReserveCurveN0Properties=8` (a **declared decision**, swept 8/12/15; re-baseline revisit). Buildings are the correlated risk pools (roof/boiler/winter roll up per-property) — **design-reasoned; sim-validation deferred** (the shock generator is per-unit). **Months taper but absolute dollars grow monotonically** — never present one without the other. **Cash floor `FIN.CashFloorMonths = 3`** (governs unrestricted Cash, not the CSF). Month-end **full-deficit top-up from Cash** (`CSF_TopupFractionPerMonth = 1.0`), fires *after* same-day obligations; **no automatic over-target sweep** (reserve ratchet) — a taper-driven target drop leaves the excess parked (reserve whole, not a raid). *(Replaced the flat `FIN_OperatingReserveMonths=12`, which ballooned in absolute dollars at scale — hoarding — and permanently capped portfolio growth after the initial deployment.)*
- **CSF model (owner ruling — this text governs):**
  The CSF is Liberty Bee's protected reserve. It is **not acquisition capital, growth capital, or a discretionary operating supplement**. Its primary function is to **preserve continuity** when unrestricted Cash is temporarily insufficient to meet protected obligations. **CSF is not permission to spend — CSF is failure protection for spending already authorized under normal rules.** It cannot justify *creating* a new obligation (a raise may not be approved *because* CSF exists), but once an obligation is validly created (e.g. a granted raise's payroll), it is protected.
  - **Two layers:** the **protected reserve layer** (at/below target — continuity only, rules below) and an **excess / above-target layer** — only exists when CSF exceeds target; may fund *approved* tenant/community/enhancement uses; **never acquisitions** (that prohibition is absolute at every layer). *(V1 engine note: no discretionary-CSF mechanism exists as-built; the excess layer is policy for when it does.)*
  - **Protected obligations** = expenses required to keep LB **legally compliant, operationally solvent, staffed, insured, habitable, and tenant-protective**: authorized payroll; required taxes, insurance, utilities; legally-required tenant payments + deposit refunds; emergency/required maintenance affecting habitability, safety, or asset preservation; baseline property operations; contracted property management (if part of the operating model); eviction/legal costs tied to tenancy enforcement or tenant protection.
  - **Non-protected (Cash-only):** acquisitions / closing costs / earnest money / acquisition due diligence; optional upgrades; community enhancements; EIP pilots; discretionary tenant benefits; marketing/branding; nonessential software/tools; expansion planning; **anything created to enable growth rather than continuity.** *(Guardrail: "baseline OpEx" must not become a costume — optional spend doesn't get protected status by being coded as OpEx.)*
  - **Draw mechanics:** protected obligation due → unrestricted Cash first (to zero) → CSF covers the shortfall → **Cash + CSF both insufficient ⇒ simulation failure**. The cash floor does **not** block protected obligations (the floor governs top-up + acquisitions only).
  - **Consequence of any draw below target:** **acquisitions AND discretionary CSF uses both close** until the reserve is restored to target — if the reserve was touched for survival, no growth *and* no enhancements ("no murals while the spleen is on the floor").
  - **Top-up:** month-end, after same-day obligations, from unrestricted Cash up to target; CSF does **not** silently absorb surplus beyond target.
  - **Mechanism rule:** protected expenses use a distinct `process_expense_protected(...)` pathway — never a generic `allow_csf_draw` flag. **Protection is a structural classification, not a parameter that can be casually flipped.** The monthly OpEx charge routes through this path — load-bearing for survival.
  - ⚠️ *Engine mapping:* V1 models OpEx as an aggregate (static + per-unit), so the protected-vs-non-protected **category split is policy-level** until discrete cost modeling exists (V2 cost engine, stretch). This classification is carried forward in the current engine; finer-grained protected-vs-non-protected cost categories remain a V2 stretch.
- **Payroll (locked):** bi-monthly on the **15th + last day of month** (salary ÷ 24); annual raises fire **Dec 31**; payroll is a **protected obligation** (above). This CSF-backstop is what makes `Survival = FinalCash + FinalCSF > 0` coherent.
- **EIP (intent — ⚠️ not implemented):** `FIN_EIPAllocationPct = 5%` of starting funds sets the **maximum initial EIP funding envelope**. Actual transfer/spend is **blocked until `FIN_EIPStartYear`** (yr 6) **and** only allowed after all housing, reserve, maintenance, tenant-benefit, and solvency gates pass. (Gated invariant — housing first.)
- **Acquisition gate — THREE independent rules** (all must pass; **not** a `Cash+CSF` sum):
  1. **Reserve-first + genuine-draw latch:** if CSF is below the **committed** target (what the last month-end top-up funded to — a genuine survival draw, or a cash-starved top-up), **block growth until the reserve is restored** (the startup grace waives this rule). Otherwise the reserve is whole vs its commitment and only the monthly **inflation-step shortfall** remains; that shortfall is **earmarked out of deployable Cash** in Rule 3 (fund the pending top-up first, grow on the remainder). This is *not* a Cash+CSF blend — the shortfall is **subtracted from the growth side**, never added to the reserve side. *(Replaces the earlier "CSF ≥ current target" check, which was unsatisfiable under inflation: the target stepped up monthly on the 1st while the top-up only reached it at month-end, so CSF was permanently one step short and growth dead-locked at ~22 units.)*
  2. **Cash ≥ cash floor** (`monthly_opex × 3`) — no grace;
  3. **deployable Cash** (`Cash − cash floor − reserve earmark`) **> 0**.
  `monthly_opex` = the **expected-cost basis** (payroll + itemized per-unit buckets [tax/insurance/utilities] + per-property exterior + E[routine] + E[major] + E[turnover]) — see §"OpEx model" / the user's guide §2.1; the retired `$95K static + $12K/unit` aggregate was removed. The month-end non-payroll charge fires as `RECURRING_NONPAYROLL_OPEX` = deterministic buckets + REALIZED event draws, in ONE protected call. Per-unit buckets count **owned units regardless of occupancy**, inflated by `INF.OpExInflationRate` (locked — do not reinterpret as flat).
- **Funding source:** acquisitions draw **ONLY from deployable Cash above the cash floor — CSF is never used, directly or indirectly, as acquisition capital.** Acquire `PROP_AcquisitionPct` of that eligible Cash. LB **never sells** (LB ownership is permanent — engine token `LBOwned`; see glossary on the dual-token cleanup).
- **Startup grace (~first 6 months):** relaxes **Rule 1 (CSF target) only** — LB can acquire before CSF is fully funded (*hit the ground running; no homes ⇒ no tenants to protect with the CSF*, owner-confirmed). Rules 2 & 3 still apply; CSF is still never spent (invariant intact). *(`property_acquisition_manager.py`.)*
- **Grants (intent — ⚠️ not implemented):** chance small **20%**, medium **10%**, large **5%** (`GRT_Chance*`), size-specific amount ranges + cooldowns *(cadence: canon says monthly, an older narrative says annual — unverifiable until a grant engine exists)*. Grants are a **growth catalyst, not a lifeline.** Grant eligibility should be gated by organizational stability/maturity where implemented; **grants received while below the operational floor must be reported separately — grant-dependent survival is NOT counted as clean viability.**

### Operating baselines (current model — the retired aggregates are history, not config)
Current model: `OPEX.PropertyTaxPerUnit=$2,250` / `InsurancePerUnit=$1,400` / `UtilitiesOwnerPerUnit=$2,000` (conservative owner-pays-heat) + the `MAINT.*` event streams; `PROP.VacancyRateBase=1.9%` ± `VacancyFluctuationBand=0.5%`; `STAFF.BenefitsPct=25%`. *(Historical V1 values — `$95K+$12K/unit` aggregate, `VacancyRateBaseline=1%`, `TNT_BaselineTenancyMF` — were retired; they appear only in earlier frozen archives and an interim baseline.)*

> **OpEx realism (SHIPPED, folded into the 1.0 baseline):** the aggregate OpEx model is **retired**. Costs are now **itemized** — per-unit property tax / insurance / owner-utilities plus the `MAINT.*` event streams (turnover make-ready, exterior/grounds per building/yr, overdispersed routine repairs, fat-tailed major events) — with **contracted-below-crossover staffing** and the CSF reserve **curve**. Both are baked into the frozen **1.0.0** baseline. *Honest caveat that survives itemization:* the expense ratio still runs **above a lean private operator's ~45–48%** — **by design, not inefficiency**: below-market rents shrink the denominator and mission self-management (no profit PM) is a deliberate cost. Present it as a mission cost, never dressed down. *(The old ~85%-of-rent figure was an earlier fixed-3-FTE small-scale artifact, retired — it survives only in earlier frozen archives.)*

### Inflation (discrete-regime Markov, per-category; supersedes "V1 = Static")
**Default = `INF.Mode='Regime'`** (the stochastic model IS the headline baseline): a seeded Markov chain over **5 regimes** — Normal / Surge / Normalization / Downturn-Financial / Downturn-Shock — each with its **own annual mean+vol per category** (rent / OpEx / property) plus a **vacancy LEVEL delta** (downturns +3–4pt), multi-month persistence via the transition-matrix diagonal. **Rates can go negative** (the old `max(0)` clamp is removed — a Downturn-Financial window produces a property factor < 1.0; regression-gated). Sitting-tenant rent is untouched by any regime (invariant #1 — schedule reprices at turnover only; regression-gated). `Static` mode retained (flat Rent 3% / OpEx 2.5% / Property 3% / General 2% annual ÷ 12) for the matrix report + two-point re-baseline attribution; per-projection `INF.ForcedRegime` scripts single-archetype scenario runs. All regime params are **declared decisions** (n=2 downturns in ~40yr), registry rows, swept — see `evidence_base.md` §7. *(The old Optimistic/Pessimistic/Recession/Volatile/Random scenario-multiplier machinery — one scalar to all categories in lockstep — is retired.)*

### Property acquisition → compliance → rentable (locked)
- **A unit is NOT rentable on acquisition day.** Acquisition runs a pipeline — **Offer → Seller response → Inspection → Negotiation → Closing → building/unit compliance → rentable** — and only then does rent flow. Time-to-revenue is a real survival variable.
- **Readiness gating:** a **unit is rentable** ⟺ its building is **ONLINE** *and* the unit's blocking work items are cleared. A **building is ONLINE** ⟺ all blocking *building* work items cleared *and* ≥1 unit rentable. "**Cleared**" = inspection `PASSED`/`COMPLETED`, **or** a `FAILED` inspection whose remediation child is `COMPLETED`. Blocking is **explicit by WorkType** (not everything blocks).
- **Due-diligence remediation severity → blocking:** acquisition-inspection severity carries into a `DUE_DILIGENCE_REMEDIATION` item — **MINOR does not block** building-online; **MODERATE / MAJOR block**.
- **MA lead reality:** ~**99.58%** of properties are pre-1978, so lead compliance is the norm. `YearBuilt ≥ 1978` skips lead; else `LEAD_DOC_REVIEW` (docs present?) → `LEAD_RISK_ASSESSMENT` → `LEAD_ABATEMENT`. *(Richer LC/LIC/no-docs lead model is deferred to a future version.)*
- **Compliance costs draw from Cash only — never CSF** (reinforces invariant #4; remediation CapEx is an operating cost).
- **Concurrent acquisitions** (configurable max) — as-built (an earlier "single active acquisition" rule was superseded).
- **Currently vacant-only:** there is **no inherited-occupancy logic** — the model does not yet acquire a property with a sitting tenant. Inherited-tenant handling is a **stretch capability**, deferred to a future version.
- **Parameters** live in **`reference.AcquisitionParameters` / `reference.ComplianceParameters`** — inspection severity **20% clean / 50% minor / 20% moderate / 10% major** and **LB withdrawal threshold = repairs > 8% of purchase price** are **DB-confirmed**.
- *Legacy `PropertyOnboarding`/`UnitOnboarding` static-onboarding design is superseded by this compliance pipeline.*

---

## 5. Staffing model (registry-driven; corrected staffing formulas; mixed scaling driver)
All `STAFF.*` params live in `reference.ParameterRegistry` (read fail-loud). **Corrected (staffing re-trigger):** `check_staffing_needs` re-triggers after each acquisition (no longer init-only), so headcount tracks the portfolio across the 240-month run.

**Mixed scaling driver:** **admin scales per UNIT** (owner-confirmed, "10 properties could be 20 units" — **stands, unchanged**; the workload unit is the lease/occupied-unit = one point of contact, not per-person); **maintenance scales per PROPERTY** — a deliberate, maintenance-only refinement for the *coordination/travel* burden (`evidence_base.md` §5: the first in-house tech tracks ~12–17 buildings, not doors). Admin is NOT affected by the property driver.
- **Maintenance:** `total_properties // MaintCrossoverProperties` with `MaintCrossoverProperties=15` — **0 FTE below the crossover** (fully contracted; the `MAINT.*` event streams carry the cost), then 1 in-house tech per crossover-properties, with routine contracted spend dropping to `MaintTechReducedContractPct=40%` (tech salary moves to payroll). Retired: `UnitsPerMaintenance` (per-unit form) + `BaseMaintenanceCount` (no day-1 maintenance floor — contracted-below-crossover is the design).
- **Admin** (corrected formula — see below): `max(BaseAdminCount=2, ceil(total_units / threshold))`, threshold = `UnitsPerAdmin_Early=25` in the first `EarlyAdminYears=5` years, else `UnitsPerAdmin_Late=50`. base-2 = 1 Administration Manager + 1 Property Manager (a true **floor**).

**Raises — CASH-RESERVE-TIERED, not tenure-based** *(the `STAFF_RaisePct*Mo` params mean months of OpEx **reserves**, not employee tenure)*: annual raises (Dec 31) pick a tier from the org's cash position. **Corrected raise tiers:** **4.0 / 3.0 / 2.5 / 2.0%** (excellent / good / thin / minimal reserves; `STAFF_RaisePct12Mo/10Mo/8Mo/Min`), with the **`SalaryCap` clamp applied** + an honest full-compensation denominator. `STAFF_BenefitsPct = 25%`.

**✅ Admin-formula fix (ratified):** the admin formula **double-counted** the fixed overhead — `admin = base_admin(2) + ceil(units/threshold)` while maintenance correctly used `max(base, ceil)`. Because `ceil(units>0) ≥ 1`, any non-empty portfolio got a 3rd admin from month ~2. Corrected to `admin = max(base_admin, ceil(units/threshold))`, mirroring maintenance: `base_admin=2` (1 Administration Manager + 1 Property Manager) is now a true **floor**, and the early/late ratio only adds a 3rd admin once units warrant it (51 units early / 101 late). At V1 scale (~22 units) this is **2 admin + 1 maintenance**; the `UnitsPerAdmin_Early/Late` ratio is *latent* below ~50 units. This flips the $8M rung dead→survives (a co-cause of the earlier closing-cost deaths). No migration (`BaseAdminCount=2` unchanged).

---

## 6. Success metrics (terminology reconciled — owner ruling)

**Two distinct axes** — both previously called "robust"; now disambiguated:

**Per-run outcomes** (one simulation, at month 240):
- **Survival** = `FinalCash + FinalCSF > 0` at month 240. *This is a **minimum solvency gate only**; reserve quality and unrestricted-cash strength are evaluated separately under Strong / Robust.* **Survival counts Cash + CSF because protected obligations may draw from both; growth capacity counts Cash only, after floors and reserve restoration — survival and expansion are different gates** (surviving ≠ "we can buy another building"). *⚠️ **Known limitation:** survival measures the **institution**, not the mission — and the engine's operative bankruptcy halt is even narrower (`Cash + CSF < bi-monthly payroll`, checked on payroll days). The Viable bar's **"no mission-rule breach"** clause is currently **unmeasured** — no per-run breach detector and no mission-outcome headline exist yet. Identifying those metrics is owed work.*

**Reporting a rung/stress death — the (a)–(d) checklist (BINDING on any public report of a dying run/rung, incl. the re-baseline):**
- **(a)** Frame the dying rung by what it IS — the most-aggressive *growth* configuration at that capital level — never as "the baseline fails." *(History: originally written for the $8M rung; superseded-by-event there — $8M survives robustly after the staffing and reserve fixes — but the rule revives for whichever rung dies under honest costs.)*
- **(b)** Name the actual cause (honest acquisition/compliance/operating cost; under-capitalization) — never vague "operating economics." *(Same supersession history as (a).)*
- **(c)** Lead with the methodology ("we corrected the instrument; the honest number is X") — never the death count. **Still mandatory.**
- **(d)** Distinguish **org-insolvency from tenant-harm**: LB's dying runs historically die *solvent in their protections* (occupancy ~95–100%, no rent hikes, no evictions) — say so, and **re-verify that classification under downturn regimes before asserting it** in the re-baseline. **Still mandatory.**
- Companion language rules: "more affordable than market / non-extractive," never "affordable housing" (regulated term LB doesn't claim); never headline a known artifact (the retired "high-inflation helps" flat-multiplier artifact is the canonical example — superseded by the regime inflation model, kept as the lesson).
- **Reserve-cushion** = `FinalCash + FinalCSF ≥ $1.5M` *(V1 fixed figure; flagged for V1.1 replacement with a dynamic multiple — e.g. 2–3× — of the operational floor)*.
- **Operational floor** ≈ combined `monthly_opex × (3 + reserve_months(N))` (3 mo Cash + the reserve-curve months, 12 at ≤8 properties tapering to 4) — a **scale-dependent** reserve-adequacy *intuition* (was the flat "×15"). The actual acquisition **gate is the 3 independent rules in §4**, not a sum.

**Run-set thresholds** (% of the Monte Carlo set — the official V1 grading bar, from philosophy doc §3):
- **Viable** ≥ **80%** of runs survive 20 yrs with **no mission-rule breach**.
- **Strong** ≥ **90%** survive + end with positive unrestricted cash + restored reserve capacity.
- **Robust** ≥ **95%** survive + grow units / maintain a healthy unit base.

Growth is **not** mandatory for baseline success; the model is **not** graded by maximum ending cash (see philosophy doc §3).

### The frozen 1.0 baseline of record — headline numbers (canonical)

*Source: the **1.0 baseline of record** (corpus V03R4 — 800 Regime + 100 Static runs, engine 0.5.0, ratified + frozen). **Framing is binding** (§6 (a)–(d) + the guardrails below): the headline is the **stochastic Regime** curve, never Static; state numbers as a **model result under Salem-area assumptions**, not launch-capital guidance.*

**★ Entry-ticket sentence (locked, public-facing):** *"Under Salem-area assumptions, this model reaches reliable 20-year survival around **$4.5M** of starting capital; below roughly **$3M** it usually fails — a result of the cost model, not fundraising guidance."* Lead $4.5M; the knee is ~$3M.

**Survival S-curve** (16 rungs × 50 seeds, every inflation regime; survival = positive Cash+CSF **and** a full 240-month span):

| Starting capital | 20-yr survival |
|---|---|
| $2.0M | 2% |
| $2.5M | 14% |
| $3.0M | **54%** (the knee) |
| $3.5M | 80% |
| $4.0M | 92% |
| **$4.5M – $11M** | **100% — every rung, every seed** |

**Survival by horizon** (under-capitalization is a *slow* death; the knee sharpens with the horizon):

| Capital | 5 yr | 10 yr | 15 yr | 20 yr |
|---|---|---|---|---|
| $2.5M | 84% | 44% | 18% | 14% |
| $3.0M | 94% | 78% | 62% | 54% |
| $3.5M | 100% | 92% | 86% | 80% |
| $4.5M + | 100% | 100% | 100% | 100% |

**How the dying orgs die (mission-critical):** 129 deaths across 800 runs, **all at ≤$4M**. **0 evictions in ANY dying run** — orgs fail on the *institutional* side (out of money for payroll/OpEx), never by evicting their way down; tenants are not harmed by the org's insolvency (survival measures the institution, not the mission). Deaths are honest liquidity failures, not regime artifacts: at ≥$5M survival is **regime-independent** in both Static and Regime modes — the killer is under-capitalization, not the inflation path.

**Reach — who Liberty Bee can and can't house** (always stated as *LB vs market*, never "X% can afford LB" alone): a below-market 2BR reaches **23.5%** of the renter-income population vs the market landlord's **20.3%** (**+3.2 pt**); by size the discount extends reach **+3–5 pt**, reaching **17–49%** of renters. The honest other side: **76.5% can't afford even a below-market 2BR at 30%.** That is the mission-scope ruling, quantified — LB is **more affordable than market, not "affordable housing"** reaching the deeply low-income (a benchmark we use, not a program we are).

**Attribution (per-direction, never netted):** the income-realistic engine reproduces the earliest pre-hygiene curve almost rung-for-rung; the hygiene-only interim was a **depressed artifact** — honest qualification applied against *frozen* incomes, now retired. The deaths that remain are **real under-capitalization** (present in both honest corpora, only below ~$4.5M) — "the capital-proof deaths were fake; honest orgs still fail when genuinely under-capitalized."

> **⚠ Known V2 rent imperfections (disclosed, do not affect these numbers):** the 1.0 engine charges tenants off `BaseRent` while acquisition underwrites on `AdjustedRent`, and the rent generator overprices fractional baths. Both are **acquisition-scoring-only** in 1.0 — no tenant rent and no published survival number moves — and are ruled for the V2 refactor (documented, not re-baselined). One even *undercharged* tenants. See the baseline-of-record write-up.

---

## 7. Glossary / terms

> Acronyms and registry prefixes have their own scannable key: [`abbreviations.md`](abbreviations.md). This glossary defines the *mechanics*; abbreviations.md is *expansions only* (and flags the CSF/EIP mis-expansion traps).

| Term | Definition |
|---|---|
| **TCS** | Tenant Credit System — non-cash, **household-scoped** housing-stability credit (not ownership). Accrues at `TCS_CreditRate` (10% canonical) of **ON_TIME** rent, once deposit-funded; **$15K current-balance cap** (refillable — redeem below cap then re-accrue; `LifetimeAccrued` is audit-only); annual redemption (1 routine OR hardship); 2-yr portability on voluntary exit; **eviction forfeits all**; FME settlement at exit. |
| **CSF** | Community Stability Fund — protected reserve, target = `reserve_months(properties)` of expected OpEx (reserve curve: 12 mo at ≤8 properties tapering to a 4-mo floor via √(8/N); §4). Two layers: protected-reserve (continuity — backstops protected obligations) + above-target excess (approved enhancements only). **Not permission to spend — failure protection for already-authorized spending.** Never acquisition/growth capital, at any layer. *(Some older docs mis-expand it "Capital Stabilization Fund" — same fund.)* |
| **EIP** | Experimental Initiatives Program — community-mission sandbox (artist residencies, historic caretaker); 5% alloc, gated, starts yr 6. **⚠️ design intent only — not implemented in V1** ($0 in all runs). *(Some older docs mis-expand it "Economic Inclusion Program" / "Equity Investment Pool" — the latter is anti-canon; Experimental Initiatives Program is correct.)* |
| **Rent-reduction schedule** | Tenure-based rent reductions that deepen over time; milestones/percentages set by the `RR_*` projection parameters (tunable). |
| **ON_TIME / LATE / MISSED** | Payment statuses (see §2). Eviction at 3 consecutive MISSED. |
| **FME** | Final-Month-Exit TCS redemption — at lease end, good standing, partial allowed, exempt from annual limit. |
| **Portability** | 2-yr post-exit window to redeem remaining TCS; forfeited after. |
| **Below-market rent** | 10% below market on LB units; permanent policy. |
| **Survival** | Per-run: FinalCash+FinalCSF > 0 at m240 — **minimum solvency gate only** (reserve/cash quality judged under Strong/Robust). |
| **Reserve-cushion** | Per-run: FinalCash+FinalCSF ≥ $1.5M (V1; V1.1→dynamic). *(formerly "robust", per-run sense)* |
| **Robust** | Run-set: ≥95% survive + grow (run-percentage sense). |
| **Operational floor** | monthly_opex × (3 + reserve_months(N)) — scale-dependent; adequacy intuition, not the gate. |
| **Ledger vs Master** | Ledger = append-only time-series (RunID→EventID→…); Master = entity tables, updated in-place, no EventID. |
| **Event** | Atomic logged action; every ledger row references an EventID; (RunID, EventID) composite key. |
| **Property market states** | **Engine lifecycle (code-verified 2026-06-09):** `Available` (neutral pool, ~94.5% at init) → `Listed` → `OtherBuyerOwned` (and back to `Available` — the market cycles independently of LB) or → `UnderContractLB` (LB pipeline reservation; falls back to `Listed` if deal dies) → **`LBOwned`** (closing; permanent). Schema CHECK allows six: those five **+ `LibertyBeeOwned`** — the conceptual token, with **no engine write site found**; dual ownership tokens = silent-query-miss risk → a planned cleanup (pick one, migrate, tighten CHECK). An older code walkthrough's `Under_Contract/Sold/Withdrawn` vocab is stale. |
| **Unit states** | `Compliance_In_Progress → Available → Occupied → Pending_Move_Out / Turnover → Available` (§2); distinct from property *market* states. |

---

## Sources & cross-refs
- *Why:* [`concept_and_philosophy.md`](concept_and_philosophy.md) · *Architecture:* [`architecture_overview.md`](architecture_overview.md) · *Evidence ("says who?"):* [`evidence_base.md`](evidence_base.md) · *Abbreviations:* [`abbreviations.md`](abbreviations.md).
- *Locked params* live in `reference.ParameterRegistry` (read-once + fail-loud). Where an older narrative and this canon disagree, **this canon governs.**
