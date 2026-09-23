"""
Phase 3.7.11 — Tenant Pipeline Occupancy-Health Regression Test.

Outcome-level black-box test of the post-fix tenant pipeline. Covers Kate's
required metric list (Kate review §Regression Artifact Requirements):

  - average occupancy rate
  - peak occupancy rate
  - occupied unit count by snapshot
  - distinct UnitIDs ever leased
  - vacancies created
  - vacancies filled
  - leases per UnitID
  - perpetually vacant units
  - applicant evaluations by UnitID
  - selections by UnitID
  - rejection breakdown by reason

Two modes:
  default      — descriptive report, exit 0 always
  --assert     — B+D acceptance mode, exit 1 if either fails

B+D acceptance (per Kate):
  B  average occupancy across the run >= 60%
  D  lease creation scales with portfolio size; practical floor =
       >= 12 of UnitsAcquired units have at least one lease

Lives permanently under sql/regression_tests/Phase3_7_11/ as a retained
regression artifact; not promoted into app/src/.

Companion artifacts:
  scratch/sql/regression_tests/Phase3_7_11/phase_3_7_11_tenant_pipeline_funnel.py
  scratch/test_applicant_generation.py
  docs/phases/phase_3_7/phase_3_7_11_2_implementation_plan.md

Usage:
    python sql/regression_tests/Phase3_7_11/phase_3_7_11_test_occupancy_health.py --env <db> [--run-id N] [--assert]
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'

OCCUPANCY_FLOOR_B = Decimal('0.60')
DISTINCT_LEASED_FLOOR_D = 12  # Kate's practical floor; expected post-fix value 22-23


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _format_pct(x) -> str:
    if x is None:
        return '   n/a'
    return f'{float(x) * 100:6.2f}%'


def collect_metrics(db, run_id: int) -> dict:
    """Pull the metric set Kate listed for this run.

    Returns a dict of named metrics. The caller decides display + assertion.
    """
    m: dict = {'run_id': run_id}

    row = db.execute_query(
        "SELECT COUNT(*) FROM simulation.PropertyUnits WHERE RunID=?",
        (run_id,)
    )
    m['units_acquired'] = int(row[0][0])

    row = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.RunSnapshot WHERE RunID=?
        """,
        (run_id,)
    )
    m['snapshot_count'] = int(row[0][0])

    if m['snapshot_count'] > 0:
        row = db.execute_query(
            """
            SELECT
              AVG(CAST(OccupancyRate AS DECIMAL(10,6))),
              MAX(OccupancyRate),
              MAX(UnitsOccupied)
            FROM simulation.RunSnapshot WHERE RunID=?
            """,
            (run_id,)
        )
        m['avg_occupancy_rate'] = row[0][0]
        m['peak_occupancy_rate'] = row[0][1]
        m['peak_units_occupied'] = int(row[0][2] or 0)
    else:
        m['avg_occupancy_rate'] = None
        m['peak_occupancy_rate'] = None
        m['peak_units_occupied'] = 0

    row = db.execute_query(
        """
        SELECT
          (SELECT COUNT(*) FROM simulation.Vacancy WHERE RunID=?),
          (SELECT COUNT(*) FROM simulation.Vacancy WHERE RunID=? AND LeaseID IS NOT NULL),
          (SELECT COUNT(*) FROM simulation.Lease WHERE RunID=?),
          (SELECT COUNT(DISTINCT UnitID) FROM simulation.Lease WHERE RunID=?)
        """,
        (run_id, run_id, run_id, run_id)
    )
    m['vacancies_created'] = int(row[0][0])
    m['vacancies_filled'] = int(row[0][1])
    m['leases_created'] = int(row[0][2])
    m['distinct_units_ever_leased'] = int(row[0][3])

    # Per-unit lease counts (only units with at least one lease)
    rows = db.execute_query(
        """
        SELECT UnitID, COUNT(*) AS LeaseCnt
        FROM simulation.Lease WHERE RunID=?
        GROUP BY UnitID ORDER BY LeaseCnt DESC, UnitID
        """,
        (run_id,)
    )
    m['leases_per_unit'] = [(int(r[0]), int(r[1])) for r in rows]

    # Perpetually vacant units = acquired but never leased
    rows = db.execute_query(
        """
        SELECT pu.UnitID, pu.Beds, pu.BaseRent
        FROM simulation.PropertyUnits pu
        WHERE pu.RunID=?
          AND NOT EXISTS (
            SELECT 1 FROM simulation.Lease l
            WHERE l.RunID=pu.RunID AND l.UnitID=pu.UnitID
          )
        ORDER BY pu.UnitID
        """,
        (run_id,)
    )
    m['perpetually_vacant_units'] = [(int(r[0]), float(r[1]), float(r[2])) for r in rows]

    # Applicant evaluations + selections by UnitID
    rows = db.execute_query(
        """
        SELECT v.UnitID,
               COUNT(*) AS EvalRows,
               SUM(CASE WHEN ae.Outcome='SELECTED' THEN 1 ELSE 0 END) AS Sel
        FROM simulation.ApplicantEvaluation ae
        INNER JOIN simulation.Vacancy v ON ae.RunID=v.RunID AND ae.VacancyID=v.VacancyID
        WHERE ae.RunID=?
        GROUP BY v.UnitID ORDER BY EvalRows DESC, v.UnitID
        """,
        (run_id,)
    )
    m['evals_by_unit'] = [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    # Rejection breakdown
    rows = db.execute_query(
        """
        SELECT RejectionReason, COUNT(*) AS Cnt
        FROM simulation.ApplicantEvaluation
        WHERE RunID=? AND Outcome='REJECTED'
        GROUP BY RejectionReason ORDER BY Cnt DESC
        """,
        (run_id,)
    )
    m['rejection_breakdown'] = [((r[0] or '(null)'), int(r[1])) for r in rows]

    # Occupancy-by-snapshot trajectory (for display)
    rows = db.execute_query(
        """
        SELECT MonthIndex, SnapshotDate, UnitsOccupied, UnitsTotal, OccupancyRate
        FROM simulation.RunSnapshot
        WHERE RunID=?
        ORDER BY SnapshotDate
        """,
        (run_id,)
    )
    m['occupancy_trajectory'] = [
        (int(r[0]), str(r[1]), int(r[2] or 0), int(r[3] or 0), r[4])
        for r in rows
    ]

    return m


def report_metrics(m: dict) -> None:
    print(f"\n========== RunID = {m['run_id']} — Occupancy Health Report ==========")
    print(f"\nUnits acquired                 : {m['units_acquired']}")
    print(f"Snapshot rows                  : {m['snapshot_count']}")
    print(f"Average occupancy rate         : {_format_pct(m['avg_occupancy_rate'])}")
    print(f"Peak occupancy rate            : {_format_pct(m['peak_occupancy_rate'])}")
    print(f"Peak units occupied            : {m['peak_units_occupied']}")
    print(f"Vacancies created (total)      : {m['vacancies_created']}")
    print(f"Vacancies filled (LeaseID set) : {m['vacancies_filled']}")
    print(f"Leases created                 : {m['leases_created']}")
    print(f"Distinct units ever leased     : {m['distinct_units_ever_leased']}")
    print(f"Perpetually vacant units       : {len(m['perpetually_vacant_units'])}")

    if m['occupancy_trajectory']:
        print(f"\n  Occupancy trajectory (first 12 / last 6 snapshots):")
        head = m['occupancy_trajectory'][:12]
        tail = m['occupancy_trajectory'][-6:] if len(m['occupancy_trajectory']) > 12 else []
        for mi, sd, occ, tot, rate in head:
            print(f"    M{mi:>3}  {sd}  Occ={occ:>3}/{tot:<3} ({_format_pct(rate)})")
        if tail:
            print(f"    ...")
            for mi, sd, occ, tot, rate in tail:
                print(f"    M{mi:>3}  {sd}  Occ={occ:>3}/{tot:<3} ({_format_pct(rate)})")

    if m['leases_per_unit']:
        print(f"\n  Leases per UnitID ({len(m['leases_per_unit'])} of {m['units_acquired']} units have >=1 lease):")
        for uid, cnt in m['leases_per_unit']:
            print(f"    Unit {uid:>3}: {cnt} lease(s)")

    if m['perpetually_vacant_units']:
        print(f"\n  Perpetually vacant units (never leased):")
        for uid, beds, rent in m['perpetually_vacant_units']:
            print(f"    Unit {uid:>3}: {beds:.1f}br @ ${rent:.2f}")

    if m['evals_by_unit']:
        print(f"\n  Applicant evaluations + selections by UnitID:")
        for uid, ev, sel in m['evals_by_unit']:
            print(f"    Unit {uid:>3}: {ev:>6} evals, {sel:>3} selected")

    if m['rejection_breakdown']:
        total = sum(c for _, c in m['rejection_breakdown']) or 1
        print(f"\n  Rejection-reason breakdown:")
        for reason, cnt in m['rejection_breakdown']:
            print(f"    {reason:<30} {cnt:>10}  {100.0*cnt/total:>5.1f}%")


def assert_b_plus_d(m: dict) -> list[tuple[str, bool, str]]:
    """Apply B+D acceptance. Returns list of (label, passed, detail)."""
    results: list[tuple[str, bool, str]] = []

    if m['avg_occupancy_rate'] is None:
        results.append(('B  avg occupancy >= 60%', False,
                        'no snapshot rows — cannot evaluate'))
    else:
        avg = Decimal(str(m['avg_occupancy_rate']))
        passed = avg >= OCCUPANCY_FLOOR_B
        results.append((
            'B  avg occupancy >= 60%', bool(passed),
            f'avg = {float(avg)*100:.2f}% (floor = {float(OCCUPANCY_FLOOR_B)*100:.0f}%)'
        ))

    distinct = m['distinct_units_ever_leased']
    acquired = m['units_acquired']
    passed_d = distinct >= DISTINCT_LEASED_FLOOR_D
    results.append((
        f'D  distinct units leased >= {DISTINCT_LEASED_FLOOR_D}',
        bool(passed_d),
        f'distinct = {distinct} of {acquired} acquired (floor = {DISTINCT_LEASED_FLOOR_D})'
    ))

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True)
    parser.add_argument('--run-id', type=int, default=None,
                        help='Specific RunID (default: all runs with snapshots)')
    parser.add_argument('--assert', dest='assertion_mode', action='store_true',
                        help='Apply B+D acceptance; exit 1 if any check fails')
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    db = DatabaseManager(_config_path_for(args.env))

    print(f'Phase 3.7.11 occupancy-health test against {args.env}')
    print('=' * 78)

    if args.run_id is not None:
        run_ids = [args.run_id]
    else:
        # Default to the LATEST run (the one just populated), consistent with the
        # other suite modules. The occupancy floors are calibrated for a mature
        # run; a leftover short/smoke run in the DB would spuriously fail them.
        # Pass --run-id to target a specific run.
        row = db.execute_query("SELECT MAX(RunID) FROM simulation.PropertyUnits")
        run_ids = [int(row[0][0])] if row and row[0][0] is not None else []

    all_pass = True
    for rid in run_ids:
        m = collect_metrics(db, rid)
        report_metrics(m)
        if args.assertion_mode:
            print(f"\n  B+D acceptance checks:")
            for label, passed, detail in assert_b_plus_d(m):
                mark = PASS if passed else FAIL
                print(f"    {label:<40} {detail}  [{mark}]")
                if not passed:
                    all_pass = False

    print('\n' + '=' * 78)
    if args.assertion_mode:
        print(f"Overall: {'PASS' if all_pass else 'FAIL'}")
        sys.exit(0 if all_pass else 1)
    else:
        print("(descriptive mode — exit 0)")
        sys.exit(0)


if __name__ == '__main__':
    main()
