"""
KD-042 (#172, ratified R1-R8) - forward-priced acquisition gate regression gates.

The fix: the gate's cash floor prices the PRO-FORMA org (owned + expected
carrying of every IsActive=1 pipeline + the candidate under consideration)
plus a one-time onboarding lump (closing + due-diligence repair + compliance
chains incl. the pre-1978 lead chain), while the CSF earmark keeps the OWNED
basis. These gates pin the ruling-level invariants:

T1  0/0 byte-identity - compute_monthly_opex_breakdown(day) is IDENTICAL to
    (day, 0, 0): the owned basis did not move (value-neutrality of the
    generalization; every non-gate consumer sees pre-KD-042 numbers).
T2  Marginal direction + owned-count reporting - extras raise .total;
    owned_units/owned_properties fields still report OWNED counts.
T3  V-B earmark-basis invariance - inserting synthetic IsActive=1 attempts
    leaves bases.owned_opex (the CSF earmark input, R3) unchanged while
    pro_forma_opex and the lump rise. The Q3 trap regression: threading
    pro-forma into the earmark would move owned_opex here.
T4  V-C lead expectation - a pre-1978 property's expected onboarding
    compliance cost strictly exceeds a post-1978 one's (the delta IS the
    lead chain, the s128 killer); unknown YearBuilt prices as pre-1978.
T5  V-D floor decomposition - _required_cash_floor = pro_forma x
    CashFloorMonths + lump added ONCE (a multiplied lump fails the exact
    equality).
T6  V-F refusal auditability - a forced Rule-2 block's reason string
    decomposes owned / pipeline-marginal / lump to the dollar.
T7  M2 handoff canary (vs the populated baseline run) - every successful
    attempt's property is in PropertyUnits, and no at-rest IsActive=1
    attempt sits on an owned property (overlap is a transient within the
    closing call only, never a persisted state).

T1-T3, T6-T7 read the suite's populated baseline run; T3 inserts synthetic
attempt rows and deletes them in a finally block (cleanup guaranteed).
"""

import argparse
import os
import sys
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _attach(env):
    """Build a Simulation attached READ-ONLY to the latest run in the env
    (the suite's populated baseline) - mirrors main()'s lazy global binding,
    then the initialize steps the gate-basis path needs, WITHOUT creating a
    run or writing anything."""
    import simulation as sim_mod
    from database_manager import DatabaseManager
    from fund_manager import FundManager
    from run_manager import RunManager
    from configuration_loader import ConfigurationLoader
    from inflation_engine import InflationEngine
    from employee_manager import EmployeeManager
    from event_logger import EventLogger
    from property_market_manager import PropertyMarketManager
    from property_acquisition_manager import PropertyAcquisitionManager
    from compliance_manager import ComplianceManager
    from maintenance_event_manager import MaintenanceEventManager

    for _n, _m in [
        ("DatabaseManager", DatabaseManager), ("FundManager", FundManager),
        ("RunManager", RunManager), ("ConfigurationLoader", ConfigurationLoader),
        ("InflationEngine", InflationEngine), ("EmployeeManager", EmployeeManager),
        ("EventLogger", EventLogger), ("PropertyMarketManager", PropertyMarketManager),
        ("PropertyAcquisitionManager", PropertyAcquisitionManager),
        ("ComplianceManager", ComplianceManager),
    ]:
        setattr(sim_mod, _n, _m)

    cfg = ENV_BASE + env + os.sep + "db_config.json"
    sim = sim_mod.Simulation(env=env, configfile=cfg, projection_id=206, random_seed=99)

    row = sim.db.execute_query(
        "SELECT RunID, ProjectionID FROM simulation.Run ORDER BY RunID DESC OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY")
    if not row:
        raise RuntimeError("no run in env - run the suite with --populate first")
    sim.run_id = int(row[0][0])
    sim.config = sim.config_loader.load_projection(int(row[0][1]))

    sim.event_logger.set_run_id(sim.run_id)
    sim.property_acquisition_manager.set_run_id(sim.run_id)
    sim.property_acquisition_manager.set_config(sim.config)
    sim.property_acquisition_manager.set_gate_basis_provider(
        sim.compute_acquisition_gate_bases)
    sim.compliance_manager = ComplianceManager(
        sim.db, sim.event_logger, sim.run_id,
        fund_manager=sim.fund_manager, random_seed=99)
    sim.maintenance_event_manager = MaintenanceEventManager(
        config=sim.config, run_seed=99)
    return sim


def _eval_date(sim) -> date:
    # inside the populated run's inflation schedule
    return sim.config.start_date + relativedelta(months=2)


def t1_zero_extras_byte_identity(sim) -> bool:
    d = _eval_date(sim)
    a = sim.compute_monthly_opex_breakdown(d)
    b = sim.compute_monthly_opex_breakdown(d, 0, 0)
    ok = a == b  # frozen dataclass: field-wise equality
    print(f"  T1 0/0 byte-identity: identical={a == b} [{PASS if ok else FAIL}]")
    return ok


def t2_marginal_direction(sim) -> bool:
    d = _eval_date(sim)
    a = sim.compute_monthly_opex_breakdown(d)
    c = sim.compute_monthly_opex_breakdown(d, extra_units=10, extra_properties=2)
    ok = (c.total > a.total
          and c.owned_units == a.owned_units
          and c.owned_properties == a.owned_properties)
    print(f"  T2 marginal: total {a.total} -> {c.total} rises={c.total > a.total}, "
          f"owned fields stable={c.owned_units == a.owned_units} [{PASS if ok else FAIL}]")
    return ok


def _pickable_properties(sim, n=2):
    """Reference properties with units, not owned in this run, no attempt rows."""
    rows = sim.db.execute_query(
        """SELECT p.PropertyID,
                  (SELECT COUNT(*) FROM reference.Units u WHERE u.PropertyID = p.PropertyID)
           FROM reference.Properties p
           WHERE EXISTS (SELECT 1 FROM reference.Units u WHERE u.PropertyID = p.PropertyID)
             AND NOT EXISTS (SELECT 1 FROM simulation.PropertyUnits pu
                             WHERE pu.RunID = ? AND pu.PropertyID = p.PropertyID)
             AND NOT EXISTS (SELECT 1 FROM simulation.PropertyAcquisitionAttempt a
                             WHERE a.RunID = ? AND a.PropertyID = p.PropertyID)
           ORDER BY p.PropertyID
           OFFSET 0 ROWS FETCH FIRST ? ROWS ONLY""",
        (sim.run_id, sim.run_id, n))
    return [(int(r[0]), int(r[1])) for r in rows]


def t3_earmark_basis_invariance(sim) -> bool:
    d = _eval_date(sim)
    base = sim.compute_acquisition_gate_bases(d)
    props = _pickable_properties(sim, 2)
    if len(props) < 2:
        print(f"  T3 earmark invariance: no free reference properties [{FAIL}]")
        return False
    next_id_row = sim.db.execute_query(
        "SELECT COALESCE(MAX(AttemptID), 0) FROM simulation.PropertyAcquisitionAttempt WHERE RunID = ?",
        (sim.run_id,))
    next_id = int(next_id_row[0][0]) + 900  # far from real ids
    inserted = []
    try:
        for i, (pid, _units) in enumerate(props):
            sim.db.execute_non_query(
                """INSERT INTO simulation.PropertyAcquisitionAttempt (
                       RunID, AttemptID, PropertyID, OfferDate, ListingPrice,
                       OfferAmount, OfferStrategy, PipelineStage, IsActive,
                       StageEnteredDate, NextActionDate, StageTimeoutDate,
                       AcquisitionSucceeded, CreatedAt, LastUpdatedDate)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, 'MakingOffer', 1, ?, ?, NULL, 0,
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (sim.run_id, next_id + i, pid, d, Decimal("500000.00"), d, d))
            inserted.append(next_id + i)

        with_pipe = sim.compute_acquisition_gate_bases(d)
        owned_stable = with_pipe.owned_opex == base.owned_opex
        pro_forma_rises = with_pipe.pro_forma_opex > base.pro_forma_opex
        lump_present = with_pipe.onboarding_lump > base.onboarding_lump
        counted = with_pipe.pipeline_count == base.pipeline_count + 2
        ok = owned_stable and pro_forma_rises and lump_present and counted
        print(f"  T3 earmark invariance (V-B/R3): owned_opex stable={owned_stable} "
              f"({base.owned_opex} -> {with_pipe.owned_opex}), pro-forma rises={pro_forma_rises}, "
              f"lump {base.onboarding_lump} -> {with_pipe.onboarding_lump}, "
              f"membership counted={counted} [{PASS if ok else FAIL}]")
        return ok
    finally:
        for aid in inserted:
            sim.db.execute_non_query(
                "DELETE FROM simulation.PropertyAcquisitionAttempt WHERE RunID = ? AND AttemptID = ?",
                (sim.run_id, aid))


def t4_lead_expectation(sim) -> bool:
    cm = sim.compliance_manager
    pre = cm.expected_onboarding_compliance_cost(4, 1950)
    post = cm.expected_onboarding_compliance_cost(4, 1990)
    unknown = cm.expected_onboarding_compliance_cost(4, None)
    lead_delta = pre - post
    ok = lead_delta > Decimal("0") and unknown == pre and post > Decimal("0")
    print(f"  T4 lead chain (V-C): pre-1978 {pre} vs post {post} "
          f"(lead delta {lead_delta} > 0={lead_delta > 0}), unknown==pre-1978={unknown == pre} "
          f"[{PASS if ok else FAIL}]")
    return ok


def t5_floor_decomposition(sim) -> bool:
    from property_acquisition_manager import AcquisitionGateBases
    pam = sim.property_acquisition_manager
    months = Decimal(sim.config.cash_floor_months)
    bases = AcquisitionGateBases(
        owned_opex=Decimal("10000.00"), pro_forma_opex=Decimal("12000.00"),
        onboarding_lump=Decimal("50000.00"), pipeline_count=1,
        pipeline_units=4, pipeline_properties=1)
    floor = pam._required_cash_floor(bases)
    expect = (Decimal("12000.00") * months + Decimal("50000.00")).quantize(Decimal("0.01"))
    wrong_if_multiplied = ((Decimal("12000.00") + Decimal("50000.00")) * months).quantize(Decimal("0.01"))
    ok = floor == expect and floor != wrong_if_multiplied
    print(f"  T5 floor decomposition (V-D): floor {floor} == pro_forma x {months} + lump "
          f"(lump once)={floor == expect} [{PASS if ok else FAIL}]")
    return ok


def t6_refusal_string(sim) -> bool:
    from property_acquisition_manager import AcquisitionGateBases
    pam = sim.property_acquisition_manager
    d = _eval_date(sim)
    owned = sim.compute_monthly_opex_breakdown(d).total
    # a floor no balance clears -> Rule 2 refusal (month 1: grace waives Rule 1)
    bases = AcquisitionGateBases(
        owned_opex=owned, pro_forma_opex=owned + Decimal("1000000000.00"),
        onboarding_lump=Decimal("123456.78"), pipeline_count=3,
        pipeline_units=12, pipeline_properties=3)
    allowed, reason = pam.can_acquire_property(1, d, bases)
    tokens = ["KD-042", "owned", "pipeline-marginal", "lump", "$123,456.78"]
    missing = [t for t in tokens if t not in reason]
    # V-A (unit leg): the pro-forma floor is TIME-INVARIANT — the same block
    # must fire POST-GRACE (month 61; Rule 1's latch passes at
    # committed_target=0, Rule 2 still refuses). The mid-sim binge window is
    # priced by the same rule as the startup one; the scenario-scale
    # demonstration rides the V2 impact sweep (all rungs, all months).
    allowed_pg, reason_pg = pam.can_acquire_property(61, d, bases)
    ok = (not allowed) and not missing and (not allowed_pg) and "KD-042" in reason_pg
    print(f"  T6 refusal auditability (V-F) + post-grace (V-A unit leg): "
          f"blocked@m1={not allowed}, blocked@m61={not allowed_pg}, "
          f"missing tokens={missing or 'none'} [{PASS if ok else FAIL}]")
    return ok


def t7_handoff_canary(sim) -> bool:
    # NB: simulation.Properties re-keys per-run — its PropertyID is run-local
    # and the REFERENCE PropertyID (what attempts carry) is stored in
    # AddressID. The handoff join must go attempts.PropertyID →
    # simulation.Properties.AddressID → PropertyUnits(sim PropertyID).
    orphan_closes = sim.db.execute_query(
        """SELECT COUNT(*) FROM simulation.PropertyAcquisitionAttempt a
           WHERE a.RunID = ? AND a.AcquisitionSucceeded = 1
             AND NOT EXISTS (SELECT 1
                             FROM simulation.Properties sp
                             JOIN simulation.PropertyUnits pu
                               ON pu.RunID = sp.RunID AND pu.PropertyID = sp.PropertyID
                             WHERE sp.RunID = a.RunID AND sp.AddressID = a.PropertyID)""",
        (sim.run_id,))
    at_rest_overlap = sim.db.execute_query(
        """SELECT COUNT(*) FROM simulation.PropertyAcquisitionAttempt a
           WHERE a.RunID = ? AND a.IsActive = 1
             AND EXISTS (SELECT 1 FROM simulation.Properties sp
                         WHERE sp.RunID = a.RunID AND sp.AddressID = a.PropertyID)""",
        (sim.run_id,))
    n_orphan = int(orphan_closes[0][0])
    n_overlap = int(at_rest_overlap[0][0])
    ok = n_orphan == 0 and n_overlap == 0
    print(f"  T7 handoff canary (M2): closed-but-unowned={n_orphan}, "
          f"at-rest active-on-owned={n_overlap} [{PASS if ok else FAIL}]")
    return ok


def t8_candidate_fold(sim) -> bool:
    """R1 riders (Keight T-1): the post-commitment re-gate path —
    compute_acquisition_gate_bases(candidate=...) must fold exactly the
    candidate into the set (counts INCLUDING pipeline_count — the C-3
    off-by-one regression) and its lump must include closing + repair
    expectation + its compliance expectation."""
    from property_acquisition_manager import PropertyCandidate
    props = _pickable_properties(sim, 1)
    if not props:
        print(f"  T8 candidate fold: no free reference property [{FAIL}]")
        return False
    pid, units = props[0]
    d = _eval_date(sim)
    cand = PropertyCandidate(
        property_id=pid, address="t8", list_price=Decimal("400000.00"),
        unit_count=units, total_annual_rent=Decimal("0"),
        income_to_price_ratio=0.0, vacancy_adjusted_rent=Decimal("0"))

    base = sim.compute_acquisition_gate_bases(d)
    with_c = sim.compute_acquisition_gate_bases(d, candidate=cand)

    pam = sim.property_acquisition_manager
    yb_row = sim.db.execute_query(
        "SELECT YearBuilt FROM reference.Properties WHERE PropertyID = ?", (pid,))
    year_built = int(yb_row[0][0]) if yb_row and yb_row[0][0] is not None else None
    expected_lump_delta = (
        (Decimal("400000.00") * Decimal(str(pam.acquisition_params.closing_costs_pct))).quantize(Decimal("0.01"))
        + pam.expected_due_diligence_repair_cost()
        + sim.compliance_manager.expected_onboarding_compliance_cost(units, year_built))

    counts_ok = (with_c.pipeline_count == base.pipeline_count + 1
                 and with_c.pipeline_properties == base.pipeline_properties + 1
                 and with_c.pipeline_units == base.pipeline_units + units)
    lump_delta = with_c.onboarding_lump - base.onboarding_lump
    lump_ok = abs(lump_delta - expected_lump_delta) <= Decimal("0.02")  # quantize slack
    pro_forma_rises = with_c.pro_forma_opex > base.pro_forma_opex
    owned_stable = with_c.owned_opex == base.owned_opex
    ok = counts_ok and lump_ok and pro_forma_rises and owned_stable
    print(f"  T8 candidate fold (R1/C-3): counts+1={counts_ok}, "
          f"lump delta {lump_delta} == expected {expected_lump_delta} ±0.02={lump_ok}, "
          f"pro-forma rises={pro_forma_rises}, owned stable={owned_stable} [{PASS if ok else FAIL}]")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--assert", dest="run_assert", action="store_true")
    args = ap.parse_args()

    print(f"KD-042 forward-gate regression gates (env={args.env})")
    sim = _attach(args.env)
    results = [
        t1_zero_extras_byte_identity(sim),
        t2_marginal_direction(sim),
        t3_earmark_basis_invariance(sim),
        t4_lead_expectation(sim),
        t5_floor_decomposition(sim),
        t6_refusal_string(sim),
        t7_handoff_canary(sim),
        t8_candidate_fold(sim),
    ]
    passed = sum(results)
    print(f"KD042: {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
