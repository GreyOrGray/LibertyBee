# Liberty Bee — Abbreviations Key

**Status:** Reference glossary — the single scannable key for Liberty Bee acronyms and registry parameter prefixes.
**Use:** when you hit an unfamiliar Liberty Bee acronym, check here first. Deep mechanics live in [`business_rules.md`](business_rules.md); this is just "what does it stand for + one line."

> ⚠️ **Trap expansions (older docs get these wrong):** **CSF = Community Stability Fund** (NOT "Capital Stabilization Fund"). **EIP = Experimental Initiatives Program** (NOT "Economic Inclusion Program" / "Equity Investment Pool").

---

## Domain / model mechanisms
| Abbr | Expansion | One-line |
|---|---|---|
| **LB** | Liberty Bee | The project / the modeled housing org. |
| **CSF** | **Community Stability Fund** | Protected reserve (target 12 mo OpEx); never growth/acquisition capital. |
| **EIP** | **Experimental Initiatives Program** | Community-mission sandbox; 5% allocation, gated, year-6 start. Reserved for a future version — not yet implemented. |
| **TCS** | Tenant Credit System | Household-portable housing-stability credit; ON_TIME accrual, $15K refillable cap, annual redemption. |
| **RR** | Rent Reduction | Tenure-based reduction schedule (deepens with continuous occupancy; `RR_*` params). |
| **FME** | Final-Month-Exit | TCS redemption at lease end in good standing (partial allowed; exempt from the annual limit). |
| **LNR** | Landlord Non-Renewal | Landlord declines to renew at lease end (a renewal-decision outcome). |
| **OpEx** | Operating Expenses | Payroll + static + per-unit; the `monthly_opex` that sizes CSF target + cash floor. |

## Registry parameter prefixes (`reference.ParameterRegistry` categories)
`SIM` (sim metadata) · `FIN` (capital: starting funds, cash floor, EIP alloc) · `PROP` (property/vacancy/below-market discount) · `OPEX` (operating-expense baselines) · `INF` (inflation regimes) · `MARKET` (property-market state machine) · `ACQ` (acquisition-pipeline negotiation) · `CMPL` (compliance work-items) · `MAINT` (maintenance cost model) · `STAFF` (staffing ratios, raises, benefits) · `TNT` (applicant/tenant generation) · `INC` (sum-of-earners income model) · `QUAL` (qualification gate) · `LEASE` (lease lifecycle) · `RET` (retention: voluntary-exit / renewal model — discount sensitivity, scarcity, mover-market vacancy) · `PAY` (payment behavior) · `RR` (rent reductions) · `TCS` (credit system) · `DEP` (security deposit) · `GRT` (grants — reserved for a future version) · `CSF` (reserve curve / top-up) · `TIMING` (operational timing). *(All 22 categories; full per-knob catalog in [`parameter_reference.md`](parameter_reference.md).)*

## Process / engineering
| Abbr | Expansion | One-line |
|---|---|---|
| **KD** | Known Divergence | A documented divergence between the engine's behavior and its written spec. |
| **EAV** | Entity-Attribute-Value | The `ParameterRegistry` data model (one row per Category/Name/ProjectionID). |
| **V00NNN** | Migration number | Sequential SQL schema migration identifier (files applied in order). |

## Success-metric terms (defined in `business_rules.md` §6)
**Survival** (FinalCash+FinalCSF > 0 @ m240 — solvency floor only) · **Reserve-cushion** (≥ $1.5M) · **Strong** / **Robust** (run-set viability tiers). See §6 for the exact thresholds.

---
*Canon mechanics: [`business_rules.md`](business_rules.md) · why: [`concept_and_philosophy.md`](concept_and_philosophy.md) · architecture: [`architecture_overview.md`](architecture_overview.md). This key is expansions and definitions only — not the rules themselves.*
