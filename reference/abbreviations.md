# Liberty Bee — Abbreviations & Identities Key

**Status:** ✅ Canon (X.7) — the single scannable key for project acronyms, agent/role identities, and registry prefixes. Built 2026-06-18 after CSF/EIP mis-expansions caused real confusion.
**Use:** when you hit an unfamiliar LB acronym, check here first. Deep mechanics live in [`business_rules_current.md`](business_rules_current.md); this is just "what does it stand for + one line."

> ⚠️ **Trap expansions (older docs get these wrong):** **CSF = Community Stability Fund** (NOT "Capital Stabilization Fund"). **EIP = Experimental Initiatives Program** (NOT "Economic Inclusion Program" / "Equity Investment Pool" — the latter is anti-canon).

---

## Domain / model mechanisms
| Abbr | Expansion | One-line |
|---|---|---|
| **LB** | Liberty Bee | The project / the modeled housing org. |
| **CSF** | **Community Stability Fund** | Protected reserve (target 12 mo OpEx — sizing under review, #97); never growth/acquisition capital. |
| **EIP** | **Experimental Initiatives Program** | Community-mission sandbox; 5% alloc, gated, yr-6 start. ⚠️ V1_RESERVED — not implemented (KD-022). |
| **TCS** | Tenant Credit System | Household-portable housing-stability credit; ON_TIME accrual, $15K refillable cap, annual redemption. |
| **RR** | Rent Reduction | Tenure-based reduction schedule (deepens with continuous occupancy; `RR_*` params). |
| **FME** | Final-Month-Exit | TCS redemption at lease end in good standing (partial allowed; exempt from the annual limit). |
| **LNR** | Landlord Non-Renewal | Landlord declines to renew at lease end (a renewal-decision outcome). |
| **OpEx** | Operating Expenses | Payroll + static + per-unit; the `monthly_opex` that sizes CSF target + cash floor. |
| **Gold** | Gold baseline DB | `LibertyBeeGold` backup that ephemeral test DBs restore from; re-baselines tag `gold-v<n>` (next: `gold-v0.3`). |

## Registry parameter prefixes (`reference.ParameterCategory` / parameter-store categories)
`SIM` (sim metadata) · `FIN` (capital: starting funds, cash floor, EIP alloc) · `PROP` (property/vacancy/below-market discount) · `OPEX` (operating-expense baselines) · `INF` (inflation regimes, #48) · `MARKET` (property-market state machine) · `ACQ` (acquisition-pipeline negotiation) · `CMPL` (compliance work-items) · `MAINT` (maintenance cost model, #100) · `STAFF` (staffing ratios, raises, benefits) · `TNT` (applicant/tenant generation) · `INC` (sum-of-earners income model, Phase 1.8) · `QUAL` (qualification gate) · `LEASE` (lease lifecycle) · `PAY` (payment behavior) · `RR` (rent reductions) · `TCS` (credit system) · `DEP` (security deposit) · `GRT` (grants — V1_RESERVED) · `CSF` (reserve curve / top-up) · `TIMING` (operational timing). *(All 21 categories; full per-knob catalog in [`parameter_reference.md`](parameter_reference.md).)*

## Process / engineering
| Abbr | Expansion | One-line |
|---|---|---|
| **KD** | Known Divergence | A tracked code/doc divergence. Register lives on **GitHub issues** (`KNOWN_DIVERGENCES.md` is a frozen ID index). |
| **EAV** | Entity-Attribute-Value | The `ParameterRegistry` data model (one row per Category/Name/ProjectionID). |
| **BA** | Business Analyst | The spec/mission-fit review seat — the `lb-ba` subagent (Kate). Advisory; Gray ratifies. |
| **PR** | Pull request | Branch → PR → Gray merges to `master` (X.4 promotion model). |
| **FQ** | Follow-up Question | Kate/BA follow-up question (e.g. "FQ5"). |
| **V00NNN** | Migration number | Sequential SQL migration in `sql/migrations/` (current head: V00043). |

## Identities & agents
| Name | Who/what |
|---|---|
| **Gray** | Project owner — final call on scope/severity/ship; sole ratifier. |
| **Cate** | The dev / main-loop automation (implements after ratification). |
| **Kate** | BA seat → the `lb-ba` subagent (advisory, read-only). |
| **Keight** | Dev-reviewer seat → the `lb-code-review` subagent (read-only). |
| `lb-audit` | Read-only fact-lane inventory/audit worker. |
| `lb-research` | Read-only web + repo research (cited synthesis). |
| `lb-test-runner` / `lb-hooktest` | Regression-runner (drafted) / throwaway hook probe (retired). |
| **SecretAgentCate / SpecialAgentKate** | GitHub automation identities (X.4) — Cate's push/PR identity; Kate's PR-comment identity. |

## Success-metric terms (defined in `business_rules_current.md` §6)
**Survival** (FinalCash+FinalCSF > 0 @ m240 — solvency floor only) · **Reserve-cushion** (≥ $1.5M, V1) · **Strong** / **Robust** (run-set viability tiers). See §6 for the exact thresholds.

---
*Canon mechanics: [`business_rules_current.md`](business_rules_current.md) · why: [`concept_and_philosophy.md`](concept_and_philosophy.md) · architecture: [`architecture_overview.md`](architecture_overview.md). This key is expansions + identities only — not the rules themselves.*
