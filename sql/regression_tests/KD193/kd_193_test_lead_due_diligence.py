"""
KD-193 - lead assessment at due diligence: store-and-forward regression gates.

The fix: the lead risk assessment (doc-review -> hazard -> +/-band abatement,
the SAME chain/knobs/cutoff as the realized spawn) runs PRE-CLOSING at the
inspection stage; the foreseen abatement cost is persisted on
PropertyAcquisitionAttempt (AssessedLeadCost / LeadAssessed, V00068) and the
post-closing LEAD_ABATEMENT READS it (never re-draws). A lead-bearing property
can be withdrawn at negotiation on carryability (deployable cash after the
KD-042 floor goes negative); lead is excluded from the 8% repair-quality test.

T1  assess_property_lead is a PURE draw (no DB): post-cutoff / docs-present ->
    (0,0); a forced hazard draw returns assess = base + per-unit*(n-1) exactly
    and abatement at the unit-scaled center (factor 1.0) within +/-band; the
    same seed is reproducible (store-and-forward's source is deterministic).
T2  store-and-forward EXACTNESS (the lb-ba non-negotiable) - every closed,
    lead-bearing attempt (AcquisitionSucceeded=1, LeadAssessed=1,
    AssessedLeadCost>0) has a LEAD_ABATEMENT whose CostEstimate == the assessed
    cost, to the cent. A re-draw would diverge; a double-spawn would duplicate.
T3  no post-closing RE-ASSESSMENT - a property whose winning attempt is
    LeadAssessed=1 spawns ZERO post-closing LEAD_RISK_ASSESSMENT / LEAD_DOC_REVIEW
    rows (the pre-closing assessment replaced the chain; Keight #8 - keeps
    reachability meaningful rather than trivially satisfied by dead rows).
T4  withdrawal TARGETING - every LEAD_CARRYABILITY_WITHDRAWAL step belongs to an
    attempt with AssessedLeadCost>0 (a lead-free property is never withdrawn on
    lead grounds).
T5  RNG stream isolation (Keight #3) - the lead stream sits at offset +5000, a
    full 1000 clear of closing (+4000). Prove no collision is reachable: the
    per-run attempt-id spread is < 1000, so no two pipeline stages can seed on
    the same value.
T6  reachability + no-limbo under KD-193 (folds Keight #8) - every lead-bearing
    closing has a matching LEAD_ABATEMENT, and no acquired building is left
    offline.

T2/T3/T6 are structural: they hold whether or not the populated run happened to
close a lead-bearing property (a longer run only adds coverage). The at-scale
mission-throughput + zero-blind-commit-death proof lives in the committed
census (scratch/kd193_census.py -> kd_193_census_results.md).
"""

import argparse
import os
import random
import sys
from decimal import Decimal

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep
PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _attach(env):
    from database_manager import DatabaseManager
    from compliance_manager import ComplianceManager
    from event_logger import EventLogger
    from fund_manager import FundManager
    db = DatabaseManager(ENV_BASE + env + os.sep + "db_config.json")
    row = db.execute_query("SELECT RunID FROM simulation.Run ORDER BY RunID DESC OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY")
    if not row:
        raise RuntimeError("no run in env - run the suite with --populate first")
    run_id = int(row[0][0])
    el = EventLogger(db)
    el.set_run_id(run_id)
    # real FundManager per the F-01 fail-loud guard; these gates never pay a
    # work item, so no ledger writes occur.
    cm = ComplianceManager(db, el, run_id,
                           fund_manager=FundManager(db, el), random_seed=99)
    return db, cm, run_id


class _SeqRng:
    """Deterministic stand-in: returns a fixed sequence of draws so a specific
    chain branch (docs-present / hazard) can be forced without a live seed."""
    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def random(self):
        v = self.seq[self.i]
        self.i += 1
        return v


def t1_pure_assessment(cm) -> bool:
    zero = Decimal('0.00')
    units = 4

    # post-cutoff: no lead cost, no draws consumed
    post = cm.assess_property_lead(units, 1990, _SeqRng([0.0, 0.0, 0.5]))
    post_ok = post == (zero, zero)

    # docs present (doc-review PASS): first draw >= fail_chance -> (0,0)
    docs = cm.assess_property_lead(units, 1950, _SeqRng([0.999]))
    docs_ok = docs == (zero, zero)

    # forced hazard: doc-review FAIL (0.0), risk-assessment FAIL (0.0),
    # band factor 0.5 -> exactly center. Assess = base + per-unit*(n-1).
    assess, abate = cm.assess_property_lead(units, 1950, _SeqRng([0.0, 0.0, 0.5]))
    expect_assess = (Decimal(str(cm.lead_assess_cost_base))
                     + Decimal(str(cm.lead_assess_cost_per_unit)) * (units - 1)
                     ).quantize(Decimal('0.01'))
    center = (Decimal(str(cm.lead_abate_cost_base))
              + Decimal(str(cm.lead_abate_cost_per_unit)) * (units - 1)
              ).quantize(Decimal('0.01'))
    hazard_ok = assess == expect_assess and abate == center

    # reproducible: same live seed -> identical result (store-and-forward source
    # must be deterministic)
    r1 = cm.assess_property_lead(units, 1950, random.Random(4242))
    r2 = cm.assess_property_lead(units, 1950, random.Random(4242))
    repro_ok = r1 == r2

    ok = post_ok and docs_ok and hazard_ok and repro_ok
    print(f"  T1 pure assessment: post-cutoff (0,0)={post_ok}, docs-present (0,0)={docs_ok}, "
          f"hazard assess {assess}=={expect_assess} + center {abate}=={center} ={hazard_ok}, "
          f"reproducible={repro_ok} [{PASS if ok else FAIL}]")
    return ok


def t2_store_and_forward_exact(db, run_id) -> bool:
    rows = db.execute_query(
        """SELECT paa.PropertyID, paa.AssessedLeadCost, cwi.CostEstimate
           FROM simulation.PropertyAcquisitionAttempt paa
           JOIN simulation.ComplianceWorkItem cwi
             ON cwi.RunID = paa.RunID AND cwi.PropertyID = paa.PropertyID
                AND cwi.WorkType = 'LEAD_ABATEMENT'
           WHERE paa.RunID = ? AND paa.AcquisitionSucceeded = 1
             AND paa.LeadAssessed = 1 AND paa.AssessedLeadCost > 0""",
        (run_id,))
    n = len(rows)
    mismatches = [r for r in rows
                  if abs(Decimal(str(r[1])) - Decimal(str(r[2]))) > Decimal('0.005')]
    ok = len(mismatches) == 0
    print(f"  T2 store-and-forward exactness: {n} lead-bearing closings, "
          f"{len(mismatches)} cost mismatches (assessed != abated) [{PASS if ok else FAIL}]")
    for r in mismatches[:3]:
        print(f"      property {r[0]}: assessed {r[1]} != abated {r[2]}")
    return ok


def t3_no_post_closing_reassessment(db, run_id) -> bool:
    n = int(db.execute_query(
        """SELECT COUNT(*)
           FROM simulation.ComplianceWorkItem cwi
           JOIN simulation.PropertyAcquisitionAttempt paa
             ON cwi.RunID = paa.RunID AND cwi.PropertyID = paa.PropertyID
           WHERE cwi.RunID = ? AND paa.AcquisitionSucceeded = 1 AND paa.LeadAssessed = 1
             AND cwi.WorkType IN ('LEAD_RISK_ASSESSMENT', 'LEAD_DOC_REVIEW')""",
        (run_id,))[0][0])
    ok = n == 0
    print(f"  T3 no post-closing re-assessment: {n} stray LEAD_RISK_ASSESSMENT/LEAD_DOC_REVIEW "
          f"rows on assessed properties (must be 0) [{PASS if ok else FAIL}]")
    return ok


def t4_withdrawal_targeting(db, run_id) -> bool:
    n = int(db.execute_query(
        """SELECT COUNT(*)
           FROM simulation.PropertyAcquisitionStep pas
           JOIN simulation.PropertyAcquisitionAttempt paa
             ON pas.RunID = paa.RunID AND pas.AttemptID = paa.AttemptID
           WHERE pas.RunID = ? AND pas.StepType = 'LEAD_CARRYABILITY_WITHDRAWAL'
             AND (paa.AssessedLeadCost IS NULL OR paa.AssessedLeadCost <= 0)""",
        (run_id,))[0][0])
    total = int(db.execute_query(
        "SELECT COUNT(*) FROM simulation.PropertyAcquisitionStep "
        "WHERE RunID = ? AND StepType = 'LEAD_CARRYABILITY_WITHDRAWAL'", (run_id,))[0][0])
    ok = n == 0
    print(f"  T4 withdrawal targeting: {total} lead-carryability withdrawals, "
          f"{n} on lead-free properties (must be 0) [{PASS if ok else FAIL}]")
    return ok


def t5_rng_stream_isolation(db, run_id) -> bool:
    row = db.execute_query(
        "SELECT MIN(AttemptID), MAX(AttemptID), COUNT(*) "
        "FROM simulation.PropertyAcquisitionAttempt WHERE RunID = ?", (run_id,))[0]
    if row[0] is None:
        print(f"  T5 RNG stream isolation: no attempts in run [{PASS}] (vacuous)")
        return True
    spread = int(row[1]) - int(row[0])
    ok = spread < 1000
    print(f"  T5 RNG stream isolation: {row[2]} attempts, id spread {spread} < 1000 "
          f"(lead +5000 vs closing +4000 can't collide) [{PASS if ok else FAIL}]")
    return ok


def t6_reachability_no_limbo(db, run_id) -> bool:
    missing = int(db.execute_query(
        """SELECT COUNT(*)
           FROM simulation.PropertyAcquisitionAttempt paa
           WHERE paa.RunID = ? AND paa.AcquisitionSucceeded = 1
             AND paa.LeadAssessed = 1 AND paa.AssessedLeadCost > 0
             AND NOT EXISTS (SELECT 1 FROM simulation.ComplianceWorkItem cwi
                             WHERE cwi.RunID = paa.RunID AND cwi.PropertyID = paa.PropertyID
                               AND cwi.WorkType = 'LEAD_ABATEMENT')""",
        (run_id,))[0][0])
    n_stuck = int(db.execute_query(
        """SELECT COUNT(*) FROM simulation.ComplianceAttempt ca
           WHERE ca.RunID = ? AND ca.ScopeType = 'BUILDING'
             AND ca.BuildingOnlineDate IS NULL""", (run_id,))[0][0])
    ok = missing == 0 and n_stuck == 0
    print(f"  T6 reachability + no-limbo: {missing} lead-bearing closings without a "
          f"LEAD_ABATEMENT, {n_stuck} buildings offline (both must be 0) [{PASS if ok else FAIL}]")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--assert", dest="run_assert", action="store_true")
    args = ap.parse_args()

    print(f"KD-193 lead-at-due-diligence regression gates (env={args.env})")
    db, cm, run_id = _attach(args.env)
    results = [
        t1_pure_assessment(cm),
        t2_store_and_forward_exact(db, run_id),
        t3_no_post_closing_reassessment(db, run_id),
        t4_withdrawal_targeting(db, run_id),
        t5_rng_stream_isolation(db, run_id),
        t6_reachability_no_limbo(db, run_id),
    ]
    passed = sum(results)
    print(f"KD193: {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
