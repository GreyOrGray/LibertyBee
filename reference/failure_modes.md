# Modeling Failure — How Liberty Bee Dies, and How We Proved It

**Status:** ✅ Canon (public bundle). First written 2026-07-03 from the Phase-1.5b coverage probes.

A survival claim is only as credible as the failure machinery behind it. If the simulation *can't* die — or its death paths have never actually executed — then "survived 20 years" is a tautology, not a finding. This document shows the failure model in real terms: what the machinery is, why it's built this way, what happened when we deliberately forced every path, and exactly how you can force them yourself.

Everything below is a **labeled diagnostic**: runs on throwaway databases with knobs deliberately pushed outside their calibrated ranges. None of these numbers are survival statistics — headline numbers come only from the stochastic baseline sweep. What these runs prove is that the machinery *works*.

---

## 1. The failure model, in real terms

Liberty Bee dies the way a real nonprofit operator dies: **it can no longer meet a protected obligation.** The hierarchy:

1. **Unprotected expenses** (compliance/remediation, acquisition costs, general OpEx) pay from Cash — and they are charged **when incurred, even if Cash can't cover them**. Cash is allowed to go negative. A negative Cash balance is *a liquidity fact pending inflows*, not a death sentence — real operators run payables ahead of receivables too.
2. **Protected obligations** (payroll — people get paid, period) pay from Cash first; if Cash is exhausted, they **draw down the Community Stability Fund (CSF)**. That is what the reserve is *for*: the CSF's job in a crisis is to spend itself keeping commitments whole.
3. **Death** = the combined position (Cash + CSF) can no longer cover a protected obligation. The simulation halts with a `SIMULATION FAILURE` record and writes a final off-cadence snapshot at the halt date, so the death state is fully queryable.

Two design consequences worth stating plainly:

- **Unprotected expenses never touch the CSF.** Even with Cash millions of dollars negative, the protected reserve is not raided to cover discretionary costs. We verified this at −$2.2M Cash: zero CSF draws (§3.3).
- **"Died solvent in protections"** is a real, observable end-state: an institution whose CSF paid staff to nearly the last dollar before the halt. That's the failure mode the design *prefers* — commitments honored all the way down.

Tenant-side adverse events (missed payments → arrears → eviction after 3 consecutive misses, deposit damage withholding, credit forfeiture) are part of the same honesty: the model doesn't pretend hardship away, it routes it through tenant-protective mechanisms (grace, TCS redemptions) and records what happens when they're exhausted.

## 2. Why the paths never fire on healthy runs — and why that's not enough

At calibrated knobs, 240-month runs are institutionally healthy: across the reference seed and five fresh smoke seeds, minimum Cash never dropped below ~$188K, so the CSF backstop, the negative-Cash allowance, and the halt had **never executed outside unit tests**. Evictions are similarly rare by design — at a 2%/month payment-failure rate and a 3-consecutive-miss threshold, the expected number of eviction filings is ~0.1 per 240-month run (we observed 2 natural evictions in one of six runs, which is the math working, not a gap).

Untested code guarding your most important claims is a risk. So before the V0.3 baseline, we forced every path.

## 3. What we forced, and what it showed (2026-07-03, engine at the Phase-1.5b stack)

### 3.1 Starvation → CSF backstop → halt (`seed 606001`)
Starting funds cut $8M → $700K; everything else at calibrated values. The institution can't afford property, so it burns payroll with no revenue:
- Cash reached $0.00 at month ~26.
- **Payroll then drew the CSF 24 times — $220,777 over 12 months — down to a final $2,991.** Staff were paid a full year past Cash exhaustion.
- At the next payroll ($9,700 bi-monthly) the combined test failed by $6,709 → `SIMULATION FAILURE` (2028-02-29) + forced halt-date snapshot, off-cadence, written correctly.
- **Showed:** the backstop, the halt, and the halt-snapshot all work; the run *died solvent in protections*.

### 3.2 Cost shock → negative Cash → the honest death (`seed 808001`)
Due-diligence cost multiplier pushed 0.85 → 40× (absurd on purpose); normal funds. First acquisitions land $0.7M–$1.4M remediation charges:
- Cash was driven to **−$2,228,642** across 20 ledger rows — the run continued; negative Cash alone does not halt.
- **CSF draws during the entire excursion: zero.** The protected reserve was never raided for unprotected costs, even at −$2.2M.
- The run died only when the *pre-existing* combined test found Cash + CSF < payroll — a $2.2M hole against a $185K reserve is institutional death, and the model says so honestly.
- **Showed:** charge-anyway semantics, the negative-Cash allowance, the protected-reserve invariant under extreme distress, and that no *new* halt condition was smuggled in.

### 3.3 Payment-failure stress → the eviction chain at volume (`seed 555001`)
Monthly payment-failure probability 2% → 20% (10×):
- 27 evictions across 164 terminations; 135 eviction-turnover work orders; $164,796 in deposit damage withholdings.
- TCS redemptions rose 276 → 396 — the tenant-protective machinery visibly working *harder* under stress, as designed.
- The institution survived (final $1.52M, minimum Cash +$215,959): mass arrears alone, at 10× calibration, do not kill a portfolio of this shape.
- On a **normal-knob** seed (137842), the chain also fired naturally: 2 evictions, both deposit-withholding branches exercised ($11,302 withheld / $0 no-damage), credit forfeiture at execution, `EvictionsCumulative` counting correctly.

### 3.4 Pipeline timeouts (`seeds 707001/707002`)
Stage timeouts are structurally unreachable at calibrated values (sampled delays are always shorter), so we inverted them:
- Offer-response timeout at 1 day: **343 timeouts fired**, and reserved funds returned to Cash **exactly** — end-of-run CashHold $0.00.
- Closing timeout at 1 day: **146 timeouts fired**, including 50 after accepted counter-offers.

### 3.5 The probes caught two real bugs — which is the point
Forcing rare paths is also how you find what hides on them. Both findings predate the probes and are tracked for pre-baseline fixes:
- **#148:** the due-diligence severity→duration map never matches (case mismatch), so every DD remediation collapses to the 7–21-day band — Major work should take 60–180 days. Flattery-direction.
- **#149:** offer reservations leak: the price-reduction delta strands at closing and the counter-offer delta strands on post-counter failure — $45–92K per normal 240-month run, attributed **to the cent** by the timeout probe ($1,170,517.23 stranded == Σ(counter − offer) across 53 forced failures, exactly).

A validation exercise that finds nothing should make you suspicious. This one found two.

## 4. How you can do this too

All of it is knob-driven — no code edits. Recipe (Windows, trusted auth; adjust names):

```bash
# 1. Mint a THROWAWAY database (never run diagnostics on a baseline env)
python environmentscripts/migration_manager.py --label mystress

# 2. Push a knob outside its calibrated range (examples used above)
sqlcmd -S localhost -d LibertyBee_Test_<NNN>_mystress -E -Q \
  "UPDATE reference.ParameterRegistryDefined SET Value='700000.00' WHERE ProjectionID=206 AND Category='FIN' AND Name='StartingFunds'"    # starvation
# ...or Value='0.20' for PAY.BaseFailProbMonthly (eviction stress)
# ...or Value='40.0' for CMPL.DueDiligenceCostMultBase (cost shock / negative Cash)
# ...or UPDATE reference.AcquisitionParameters SET Timeout_Closing_MaxDays=1 (timeout inversion)

# 3. Run, with a seed you record
python app/src/simulation.py --env LibertyBee_Test_<NNN>_mystress --projection-id 206 --months 240 --seed <seed>
```

Then interrogate the death (or survival) — the queries we used:

```sql
-- Did the CSF backstop fire? (protected draws)
SELECT COUNT(*), SUM(CSFDebit) FROM simulation.FundLedger WHERE RunID=@r AND CSFDebit > 0;
-- Negative-Cash excursion?
SELECT COUNT(*), MIN(CashBalance) FROM simulation.FundLedger WHERE RunID=@r AND CashBalance < 0;
-- Halt state: the last snapshot is the forced halt-date snapshot
SELECT TOP 1 * FROM simulation.RunSnapshot WHERE RunID=@r ORDER BY SnapshotDate DESC;
-- Eviction chain
SELECT TerminationType, COUNT(*) FROM simulation.LeaseTermination WHERE RunID=@r GROUP BY TerminationType;
-- Stranded reservations (should equal in-flight holds; see #149)
SELECT TOP 1 CashHoldBalance FROM simulation.FundLedger WHERE RunID=@r ORDER BY LedgerDate DESC, EventID DESC;
```

**Rules of the road:** label every diagnostic run as such; never mix its numbers into baseline statistics; reset or drop the database afterward; and if a same-seed rerun ever disagrees with itself, STOP and preserve the database (see the determinism protocol in the Phase-1.5 record).

## 5. Framing guardrails (binding)

- A negative Cash balance is reported as **a liquidity fact pending inflows** — never as a solvency failure claim. Solvency failure has one definition here: combined Cash + CSF cannot meet a protected obligation.
- Reserve adequacy is always stated in **months AND dollars**, never months alone.
- Diagnostic-run outcomes are never survival headlines. Headlines come from the stochastic baseline sweep only.
- "Died solvent in protections" describes the institution's conduct toward its commitments, not a softening of the death. The run still died; we say so.

---

*Companion docs: [`users_guide.md`](users_guide.md) (all knobs, meanings, tuning), [`architecture_overview.md`](architecture_overview.md) (module map, ledger pattern), [`concept_and_philosophy.md`](concept_and_philosophy.md) (why honesty-over-flattery governs). Probe evidence and run records: `docs/v_0_3/phases/phase_1_5/README.md` + issues #148/#149.*
