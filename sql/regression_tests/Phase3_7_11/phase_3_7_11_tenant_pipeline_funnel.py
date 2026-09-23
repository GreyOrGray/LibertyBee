"""
Phase 3.7.11 — Tenant Pipeline Funnel Diagnostic.

Read-only diagnostic that walks the funnel Kate requested in the
2026-05-15 market-context note:

    Units acquired
    -> units compliance-ready / Available
    -> Vacancy rows created
    -> vacancies reaching TargetFillDate
    -> fill attempts made (= ApplicantEvaluation rows / slate)
    -> candidate slates generated (Vacancy.GeneratedCandidateCount)
    -> qualified applicants found (Vacancy.SelectedCount > 0)
    -> leases created

Output is per-RunID and per-UnitID so we can identify where the 19
persistently vacant units in projection-13 RunID 3 drop out.

This is a diagnostic, not a regression test. No assertions, no exit code
beyond 0. Companion docs:
    docs/phases/phase_3_7/phase_3_7_11_tenant_pipeline_scale_implementation_plan.md
    docs/phases/phase_3_7/phase_3_7_11_market_context_note_for_cate.md

Usage:
    python sql/regression_tests/Phase3_7_11/phase_3_7_11_tenant_pipeline_funnel.py --env <db> [--run-id N]
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def funnel_overall(db, run_id: int) -> None:
    print(f'\n--- Overall funnel for RunID={run_id} ---')
    rows = db.execute_query(
        """
        SELECT
          (SELECT COUNT(*) FROM simulation.PropertyUnits WHERE RunID=?) AS UnitsAcquired,
          (SELECT COUNT(*) FROM simulation.PropertyUnits WHERE RunID=? AND UnitStatus='Available') AS UnitsAvailableNow,
          (SELECT COUNT(*) FROM simulation.PropertyUnits WHERE RunID=? AND UnitStatus='Occupied') AS UnitsOccupiedNow,
          (SELECT COUNT(DISTINCT UnitID) FROM simulation.Vacancy WHERE RunID=?) AS UnitsWithAnyVacancy,
          (SELECT COUNT(*) FROM simulation.Vacancy WHERE RunID=?) AS VacanciesTotal,
          (SELECT COUNT(*) FROM simulation.Vacancy WHERE RunID=? AND AppliedCount>0) AS VacanciesWithApplications,
          (SELECT COUNT(*) FROM simulation.Vacancy WHERE RunID=? AND SelectedCount>0) AS VacanciesWithSelection,
          (SELECT COUNT(*) FROM simulation.Vacancy WHERE RunID=? AND LeaseID IS NOT NULL) AS VacanciesFilled,
          (SELECT COUNT(DISTINCT UnitID) FROM simulation.Lease WHERE RunID=?) AS UnitsEverLeased,
          (SELECT COUNT(*) FROM simulation.Lease WHERE RunID=?) AS LeasesCreated,
          (SELECT COUNT(*) FROM simulation.ApplicantEvaluation WHERE RunID=?) AS ApplicantEvalRows,
          (SELECT COUNT(*) FROM simulation.ApplicantEvaluation WHERE RunID=? AND Outcome='SELECTED') AS EvalSelected,
          (SELECT COUNT(*) FROM simulation.ApplicantEvaluation WHERE RunID=? AND Outcome='REJECTED') AS EvalRejected
        """,
        tuple([run_id] * 13)
    )
    r = rows[0]
    print(f'  Units acquired              : {r[0]:>10}')
    print(f'  Units Available (now)       : {r[1]:>10}')
    print(f'  Units Occupied (now)        : {r[2]:>10}')
    print(f'  Distinct units w/ vacancy   : {r[3]:>10}')
    print(f'  Vacancies created (total)   : {r[4]:>10}')
    print(f'  Vacancies w/ applications   : {r[5]:>10}')
    print(f'  Vacancies w/ selection      : {r[6]:>10}')
    print(f'  Vacancies filled (LeaseID)  : {r[7]:>10}')
    print(f'  Distinct units ever leased  : {r[8]:>10}')
    print(f'  Leases created              : {r[9]:>10}')
    print(f'  ApplicantEvaluation rows    : {r[10]:>10}')
    print(f'    of which SELECTED         : {r[11]:>10}')
    print(f'    of which REJECTED         : {r[12]:>10}')


def funnel_rejection_breakdown(db, run_id: int) -> None:
    print(f'\n--- Rejection-reason breakdown for RunID={run_id} ---')
    rows = db.execute_query(
        """
        SELECT RejectionReason, COUNT(*) AS Cnt
        FROM simulation.ApplicantEvaluation
        WHERE RunID=? AND Outcome='REJECTED'
        GROUP BY RejectionReason
        ORDER BY 2 DESC
        """,
        (run_id,)
    )
    total = sum(int(r[1]) for r in rows) or 1
    for reason, cnt in rows:
        pct = 100.0 * int(cnt) / total
        print(f'  {(reason or "(null)"):<30} {int(cnt):>10}  {pct:>5.1f}%')


def funnel_per_unit(db, run_id: int) -> None:
    print(f'\n--- Per-unit funnel for RunID={run_id} ---')
    print(f'  {"UnitID":>6} {"Beds":>4} {"Rent":>9}  {"Status":<10} '
          f'{"Vacs":>4} {"Apps":>6} {"Sel":>4} {"Leases":>6} {"Eval":>6} '
          f'{"%IncRej":>7} {"%BedRej":>7}')
    rows = db.execute_query(
        """
        WITH unit_lease AS (
          SELECT UnitID, COUNT(*) AS LeasesEver
          FROM simulation.Lease WHERE RunID=? GROUP BY UnitID
        ),
        unit_vac AS (
          SELECT UnitID,
                 COUNT(*) AS Vacs,
                 SUM(AppliedCount) AS Apps,
                 SUM(SelectedCount) AS Sel
          FROM simulation.Vacancy WHERE RunID=? GROUP BY UnitID
        ),
        unit_eval AS (
          SELECT v.UnitID,
                 COUNT(*) AS EvalTotal,
                 SUM(CASE WHEN ae.RejectionReason='INCOME_INSUFFICIENT' THEN 1 ELSE 0 END) AS RejInc,
                 SUM(CASE WHEN ae.RejectionReason='BEDROOM_FIT' THEN 1 ELSE 0 END) AS RejBed
          FROM simulation.ApplicantEvaluation ae
          INNER JOIN simulation.Vacancy v ON ae.RunID=v.RunID AND ae.VacancyID=v.VacancyID
          WHERE ae.RunID=? GROUP BY v.UnitID
        )
        SELECT pu.UnitID, pu.Beds, pu.BaseRent, pu.UnitStatus,
               COALESCE(uv.Vacs,0), COALESCE(uv.Apps,0), COALESCE(uv.Sel,0),
               COALESCE(ul.LeasesEver,0),
               COALESCE(ue.EvalTotal,0), COALESCE(ue.RejInc,0), COALESCE(ue.RejBed,0)
        FROM simulation.PropertyUnits pu
        LEFT JOIN unit_lease ul ON pu.UnitID=ul.UnitID
        LEFT JOIN unit_vac uv ON pu.UnitID=uv.UnitID
        LEFT JOIN unit_eval ue ON pu.UnitID=ue.UnitID
        WHERE pu.RunID=?
        ORDER BY COALESCE(ul.LeasesEver,0) DESC, pu.BaseRent
        """,
        (run_id, run_id, run_id, run_id)
    )
    for r in rows:
        uid, beds, rent, status = r[0], r[1], r[2], r[3]
        vacs, apps, sel, leases = int(r[4]), int(r[5]), int(r[6]), int(r[7])
        evtotal, rejinc, rejbed = int(r[8]), int(r[9]), int(r[10])
        pct_inc = (100.0 * rejinc / evtotal) if evtotal else 0.0
        pct_bed = (100.0 * rejbed / evtotal) if evtotal else 0.0
        print(f'  {uid:>6} {float(beds):>4.1f} {float(rent):>9.2f}  {status:<10} '
              f'{vacs:>4} {apps:>6} {sel:>4} {leases:>6} {evtotal:>6} '
              f'{pct_inc:>6.1f}% {pct_bed:>6.1f}%')


def funnel_slate_repetition_signal(db, run_id: int) -> None:
    """Highlight the per-vacancy slate determinism signal.

    For each open vacancy, the same N applicants are regenerated daily.
    Symptom: very high ApplicantEvaluation row counts per UnitID with zero
    selections, where the eval count is roughly slate_size * days_vacant.
    """
    print(f'\n--- Slate-determinism signal for RunID={run_id} ---')
    print('  Vacancies still open at end of run that have >0 evaluations and 0 selections:')
    rows = db.execute_query(
        """
        SELECT v.VacancyID, v.UnitID, v.VacancyStartDate, v.TargetFillDate,
               v.GeneratedCandidateCount, v.AppliedCount, v.SelectedCount,
               (SELECT COUNT(*) FROM simulation.ApplicantEvaluation ae
                WHERE ae.RunID=v.RunID AND ae.VacancyID=v.VacancyID) AS EvalRows
        FROM simulation.Vacancy v
        WHERE v.RunID=? AND v.VacancyEndDate IS NULL AND v.SelectedCount=0
        ORDER BY EvalRows DESC
        """,
        (run_id,)
    )
    if not rows:
        print('  (none — all vacancies either closed or had at least one selection)')
        return
    print(f'  {"VacID":>6} {"UnitID":>6} {"Started":>12} {"TargetFill":>12} '
          f'{"Slate":>6} {"Apps":>6} {"Sel":>4} {"EvalRows":>8}')
    for r in rows:
        print(f'  {r[0]:>6} {r[1]:>6} {str(r[2]):>12} {str(r[3]):>12} '
              f'{int(r[4]):>6} {int(r[5]):>6} {int(r[6]):>4} {int(r[7]):>8}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True, help='Environment / DB name under environments/')
    parser.add_argument('--run-id', type=int, default=None,
                        help='Specific RunID to analyze (default: all runs)')
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    db = DatabaseManager(_config_path_for(args.env))

    print(f'Phase 3.7.11 tenant pipeline funnel against {args.env}')
    print('=' * 78)

    if args.run_id is not None:
        run_ids = [args.run_id]
    else:
        rows = db.execute_query("SELECT DISTINCT RunID FROM simulation.Vacancy ORDER BY RunID")
        run_ids = [int(r[0]) for r in rows]

    for rid in run_ids:
        print(f'\n========== RunID = {rid} ==========')
        funnel_overall(db, rid)
        funnel_rejection_breakdown(db, rid)
        funnel_per_unit(db, rid)
        funnel_slate_repetition_signal(db, rid)

    print('=' * 78)
    print('Done.')


if __name__ == '__main__':
    main()
