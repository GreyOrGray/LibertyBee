"""
KD-043 - lead chain regression gates (reachability, clearance, calibration).

The fix: role-based inspection classification (LEAD_ABATEMENT becomes
reachable), TRANSITIVE clearance (a failed doc review clears when its spawn
chain terminates cleared - the deadlock the classification fix alone would
have created), unit-scaled lead costs at severity multiplier 1.0, and a
jurisdiction cutoff knob honored identically by spawn and gate.

T1  classification - _is_inspection keys on REMEDIATION_MAPPING (role), not
    names: LEAD_RISK_ASSESSMENT is an inspection; remediations are not.
T2  cutoff knob - _is_pre_lead_cutoff: sentinel 0 disables (precedence over
    unknown-YearBuilt); cutoff bounds respected.
T3  lead cost arithmetic - 4-unit assessment = base + 3*per-unit exactly;
    abatement draws inside the unit-scaled +/-band; severity multiplier
    provably absent (MAJOR would have tripled it).
T4  gate honors the knobs - expected_onboarding_compliance_cost matches the
    closed form for pre-cutoff; excludes lead post-cutoff; prices ZERO lead
    when the sentinel disables the chain.
T5  clearance across ALL THREE terminal branches (the lb-ba blocker gate) -
    synthetic chains in the env DB: doc-FAILED+RA-PASSED clears;
    doc-FAILED+RA-FAILED+abatement-COMPLETED clears (grandchild!);
    doc-FAILED+RA-SCHEDULED still blocks. Plus one-hop value-neutrality:
    hab-FAILED+remediation-COMPLETED still clears.
T6  no-limbo (vs the populated baseline run) - every acquired building
    reached BuildingOnlineDate; and if any RA failed, abatement rows exist.

T5 inserts synthetic ComplianceWorkItem rows and deletes them in finally.
"""

import argparse
import os
import sys
from datetime import date
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


def t1_classification(cm) -> bool:
    ok = (cm._is_inspection('LEAD_RISK_ASSESSMENT') is True
          and cm._is_inspection('LEAD_DOC_REVIEW') is True
          and cm._is_inspection('UNIT_SMOKE_CO_INSPECTION') is True
          and cm._is_inspection('BUILDING_HABITABILITY_INSPECTION') is True
          and cm._is_inspection('LEAD_ABATEMENT') is False
          and cm._is_inspection('UNIT_REMEDIATION') is False
          and cm._is_inspection('DUE_DILIGENCE_REMEDIATION') is False)
    print(f"  T1 role-based classification [{PASS if ok else FAIL}]")
    return ok


def t2_cutoff_knob(cm) -> bool:
    saved = cm.lead_cutoff_year
    try:
        cm.lead_cutoff_year = 1978
        enabled = (cm._is_pre_lead_cutoff(1950) is True
                   and cm._is_pre_lead_cutoff(1990) is False
                   and cm._is_pre_lead_cutoff(None) is True)   # unknown = pre-cutoff
        cm.lead_cutoff_year = 0
        disabled = (cm._is_pre_lead_cutoff(1950) is False
                    and cm._is_pre_lead_cutoff(None) is False)  # sentinel precedence
        cm.lead_cutoff_year = 1900
        moved = (cm._is_pre_lead_cutoff(1890) is True
                 and cm._is_pre_lead_cutoff(1950) is False)
    finally:
        cm.lead_cutoff_year = saved
    ok = enabled and disabled and moved
    print(f"  T2 cutoff knob + sentinel precedence: enabled={enabled} "
          f"disabled={disabled} moved={moved} [{PASS if ok else FAIL}]")
    return ok


class _FakeItem:
    def __init__(self, property_id, work_item_id=999901):
        self.PropertyID = property_id
        self.UnitID = None
        self.WorkItemID = work_item_id
        self.ScheduledDate = date(2025, 6, 1)


def _four_unit_property(db):
    row = db.execute_query(
        """SELECT p.PropertyID FROM reference.Properties p
           WHERE (SELECT COUNT(*) FROM reference.Units u
                  WHERE u.PropertyID = p.PropertyID) = 4
           ORDER BY p.PropertyID
           OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY""")
    return int(row[0][0]) if row else None


def t3_cost_arithmetic(db, cm) -> bool:
    pid = _four_unit_property(db)
    if pid is None:
        print(f"  T3 cost arithmetic: no 4-unit reference property [{FAIL}]")
        return False
    item = _FakeItem(pid)
    _, assess = cm._calculate_remediation_params(item, 'LEAD_RISK_ASSESSMENT', 'MINOR')
    expect_assess = (Decimal(str(cm.lead_assess_cost_base))
                     + Decimal(str(cm.lead_assess_cost_per_unit)) * 3)
    assess_ok = assess == expect_assess.quantize(Decimal('0.01'))

    _, abate_minor = cm._calculate_remediation_params(item, 'LEAD_ABATEMENT', 'MINOR')
    _, abate_major = cm._calculate_remediation_params(item, 'LEAD_ABATEMENT', 'MAJOR')
    center = (Decimal(str(cm.lead_abate_cost_base))
              + Decimal(str(cm.lead_abate_cost_per_unit)) * 3)
    band = Decimal(str(cm.lead_abate_cost_band_pct))
    in_band = (center * (1 - band)) <= abate_major <= (center * (1 + band))
    # severity provably absent: same deterministic RNG stream -> identical cost
    mult_absent = abate_minor == abate_major
    ok = assess_ok and in_band and mult_absent
    print(f"  T3 cost arithmetic (4-unit): assess {assess}=={expect_assess} ok={assess_ok}, "
          f"abate {abate_major} in ±{band:.0%} of {center}={in_band}, "
          f"severity-mult absent={mult_absent} [{PASS if ok else FAIL}]")
    return ok


def t4_gate_honors_knobs(cm) -> bool:
    units = 4
    pre = cm.expected_onboarding_compliance_cost(units, 1950)
    post = cm.expected_onboarding_compliance_cost(units, 1990)

    def fail_chance(wt):
        p = cm.parameters[wt]
        if p.pass_chance_override is not None:
            return Decimal('1') - Decimal(str(p.pass_chance_override))
        return Decimal(str(p.fail_chance_base))

    assess = (Decimal(str(cm.lead_assess_cost_base))
              + Decimal(str(cm.lead_assess_cost_per_unit)) * (units - 1))
    abate = (Decimal(str(cm.lead_abate_cost_base))
             + Decimal(str(cm.lead_abate_cost_per_unit)) * (units - 1))
    expect_lead = fail_chance('LEAD_DOC_REVIEW') * (
        assess + fail_chance('LEAD_RISK_ASSESSMENT') * abate)
    closed_form_ok = abs((pre - post) - expect_lead) <= Decimal('0.02')

    saved = cm.lead_cutoff_year
    try:
        cm.lead_cutoff_year = 0
        disabled = cm.expected_onboarding_compliance_cost(units, 1950)
    finally:
        cm.lead_cutoff_year = saved
    sentinel_ok = disabled == post  # zero lead priced when disabled

    ok = closed_form_ok and sentinel_ok
    print(f"  T4 gate honors knobs: lead delta {pre - post} == closed form {expect_lead} "
          f"±0.02={closed_form_ok}, sentinel zeroes lead={sentinel_ok} [{PASS if ok else FAIL}]")
    return ok


def t5_clearance_branches(db, cm, run_id) -> bool:
    """Synthetic chains; the lb-ba blocker gate."""
    pid = _four_unit_property(db)
    # FK_ComplianceWorkItem_Attempt: borrow a real AttemptID from the run
    att = db.execute_query(
        "SELECT AttemptID FROM simulation.ComplianceAttempt WHERE RunID = ? "
        "ORDER BY AttemptID OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY",
        (run_id,))
    if not att:
        print(f"  T5 clearance branches: no ComplianceAttempt in run [{FAIL}]")
        return False
    attempt_id = int(att[0][0])
    base_id = 990000
    ins = """INSERT INTO simulation.ComplianceWorkItem
             (RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
              ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
              PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata)
             VALUES (?, ?, """ + str(attempt_id) + """, ?, NULL, ?, ?, '2025-06-01', NULL, NULL,
                     0, NULL, NULL, 1, ?, NULL, NULL)"""
    rows = [
        # branch A: doc FAILED + RA PASSED child -> must clear
        (base_id + 1, 'LEAD_DOC_REVIEW', 'FAILED', None),
        (base_id + 2, 'LEAD_RISK_ASSESSMENT', 'PASSED', base_id + 1),
        # branch B: doc FAILED + RA FAILED + abatement COMPLETED grandchild -> must clear
        (base_id + 3, 'LEAD_DOC_REVIEW', 'FAILED', None),
        (base_id + 4, 'LEAD_RISK_ASSESSMENT', 'FAILED', base_id + 3),
        (base_id + 5, 'LEAD_ABATEMENT', 'COMPLETED', base_id + 4),
        # branch C: doc FAILED + RA SCHEDULED -> must still block
        (base_id + 6, 'LEAD_DOC_REVIEW', 'FAILED', None),
        (base_id + 7, 'LEAD_RISK_ASSESSMENT', 'SCHEDULED', base_id + 6),
        # one-hop value-neutrality: hab FAILED + remediation COMPLETED -> clears
        (base_id + 8, 'BUILDING_HABITABILITY_INSPECTION', 'FAILED', None),
        (base_id + 9, 'BUILDING_REMEDIATION', 'COMPLETED', base_id + 8),
    ]
    try:
        for wid, wt, status, parent in rows:
            db.execute_non_query(ins, (run_id, wid, pid, wt, status, parent))
        blocking = cm._get_blocking_building_work_items(pid)
        blocking_ids = {r.WorkItemID for r in blocking} if blocking else set()
        synthetic_blocking = blocking_ids & {r[0] for r in rows}
        a_clears = (base_id + 1) not in synthetic_blocking
        b_clears = (base_id + 3) not in synthetic_blocking
        c_blocks = (base_id + 6) in synthetic_blocking
        onehop_clears = (base_id + 8) not in synthetic_blocking
        # in-progress children never block by themselves? RA SCHEDULED (child)
        # is itself a blocking candidate at building level:
        ra_c_blocks = (base_id + 7) in synthetic_blocking
        ok = a_clears and b_clears and c_blocks and onehop_clears and ra_c_blocks
        print(f"  T5 clearance branches: docFail+raPass clears={a_clears}, "
              f"docFail+raFail+abateDone clears={b_clears} (grandchild!), "
              f"in-progress chain blocks={c_blocks}/{ra_c_blocks}, "
              f"one-hop neutral={onehop_clears} [{PASS if ok else FAIL}]")
        return ok
    finally:
        # single atomic IN-list delete: order-independent, immune to the
        # parent-FK ordering trap that once left orphans here
        ids = ",".join(str(wid) for wid, *_ in rows)
        db.execute_non_query(
            f"DELETE FROM simulation.ComplianceWorkItem WHERE RunID = ? AND WorkItemID IN ({ids})",
            (run_id,))


def t6_no_limbo(db, run_id) -> bool:
    stuck = db.execute_query(
        """SELECT COUNT(*) FROM simulation.ComplianceAttempt ca
           WHERE ca.RunID = ? AND ca.ScopeType = 'BUILDING'
             AND ca.BuildingOnlineDate IS NULL""", (run_id,))
    n_stuck = int(stuck[0][0])
    ra_failed = int(db.execute_query(
        "SELECT COUNT(*) FROM simulation.ComplianceWorkItem WHERE RunID = ? "
        "AND WorkType = 'LEAD_RISK_ASSESSMENT' AND Status = 'FAILED'", (run_id,))[0][0])
    abatements = int(db.execute_query(
        "SELECT COUNT(*) FROM simulation.ComplianceWorkItem WHERE RunID = ? "
        "AND WorkType = 'LEAD_ABATEMENT'", (run_id,))[0][0])
    reach_ok = (ra_failed == 0) or (abatements > 0)
    ok = n_stuck == 0 and reach_ok
    print(f"  T6 no-limbo + reachability (baseline run): offline buildings={n_stuck}, "
          f"RA-failed={ra_failed} -> abatements={abatements} [{PASS if ok else FAIL}]")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--assert", dest="run_assert", action="store_true")
    args = ap.parse_args()

    print(f"KD-043 lead-chain regression gates (env={args.env})")
    db, cm, run_id = _attach(args.env)
    results = [
        t1_classification(cm),
        t2_cutoff_knob(cm),
        t3_cost_arithmetic(db, cm),
        t4_gate_honors_knobs(cm),
        t5_clearance_branches(db, cm, run_id),
        t6_no_limbo(db, run_id),
    ]
    passed = sum(results)
    print(f"KD043: {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
