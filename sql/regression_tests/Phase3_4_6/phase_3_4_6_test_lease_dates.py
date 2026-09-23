"""
Phase 3.4.6 — Lease-date-math regression invariants (read-only).

Validates the lease-date contract (`tenant_manager._calculate_lease_dates`) on the
run's ACTUAL leases — no destructive setup. Closes a real gap: no existing suite
module asserts lease dates (Phase3_9_6 covers renewal/termination dates, not the
date arithmetic of creation).

Contract:
  - LeaseStartDate is the first day of a month (start = first-of-next-month after signing).
  - LeaseEndDate   is the first day of a month.
  - term = months(start -> end) is a positive multiple of the registry
    LEASE.StandardLeaseDurationMonths for the run's projection: the initial term is
    one standard term, and each renewal extends the lease's end date by exactly one
    more (so a tenancy renewed k times shows (k+1) x standard). Scenario-agnostic —
    the standard term is read from config, never hardcoded 12.

Scoped to the latest RunID. Ported from the date-math intent of the V0.1 source
scratch/test_lease_creation.py (whose original form DELETEd + rebuilt households/
leases — not suitable against a populated baseline); here it is a pure invariant
check on real data. (#106)

Usage:
    python sql/regression_tests/Phase3_4_6/phase_3_4_6_test_lease_dates.py --env <db> --assert
"""
from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))  # PhaseX -> regression_tests -> sql -> repo
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + "db_config.json"


def _months_between(a, b) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, help="Environment / DB name under environments/")
    parser.add_argument("--assert", dest="assert_mode", action="store_true",
                        help="acceptance mode (module exits 1 on any failure)")
    parser.add_argument("--run-id", type=int, default=None,
                        help="RunID to validate (default: latest run)")
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    from parameter_registry import ParameterRegistry  # type: ignore

    db = DatabaseManager(_config_path_for(args.env))

    run_id = args.run_id
    if run_id is None:
        row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
        run_id = row[0][0] if row and row[0][0] is not None else 1

    proj_row = db.execute_query("SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,))
    projection_id = proj_row[0][0] if proj_row else None

    # Scenario-agnostic: the expected term comes from the registry for this run's projection.
    term = ParameterRegistry(db).load(projection_id).get_int('LEASE', 'StandardLeaseDurationMonths')

    leases = db.execute_query(
        """
        SELECT LeaseID, LeaseStartDate, LeaseEndDate
        FROM simulation.Lease
        WHERE RunID = ? AND LeaseStartDate IS NOT NULL AND LeaseEndDate IS NOT NULL
        """,
        (run_id,),
    )

    print(f"Phase 3.4.6 lease-date invariants against {args.env} "
          f"(RunID {run_id}, projection {projection_id}, configured term {term} mo)")
    print("=" * 78)

    results = []

    # I0 — there are leases to validate
    has_leases = len(leases) > 0
    print(f"  I0 leases present: {len(leases)} leases on RunID {run_id} (expect >0)  "
          f"[{PASS if has_leases else FAIL}]")
    results.append(has_leases)

    # I1 — LeaseStartDate is the first of a month
    bad_start = [lid for lid, s, e in leases if s.day != 1]
    ok1 = not bad_start
    print(f"  I1 start = first-of-month: {len(bad_start)} of {len(leases)} violations (expect 0)  "
          f"[{PASS if ok1 else FAIL}]")
    results.append(ok1)

    # I2 — LeaseEndDate is the first of a month
    bad_end = [lid for lid, s, e in leases if e.day != 1]
    ok2 = not bad_end
    print(f"  I2 end = first-of-month: {len(bad_end)} of {len(leases)} violations (expect 0)  "
          f"[{PASS if ok2 else FAIL}]")
    results.append(ok2)

    # I3 — every lease term is a positive multiple of the standard term (initial term =
    #      standard; each renewal extends the end date by one more standard term).
    lease_terms = [(lid, _months_between(s, e)) for lid, s, e in leases]
    bad_term = [(lid, t) for lid, t in lease_terms if t <= 0 or t % term != 0]
    ok3 = not bad_term
    distinct = sorted({t for _, t in lease_terms})
    print(f"  I3 term is a positive multiple of {term} mo (initial + renewal extensions): "
          f"{len(bad_term)} of {len(leases)} violations (expect 0); observed terms {distinct}  "
          f"[{PASS if ok3 else FAIL}]")
    for lid, t in bad_term[:5]:
        print(f"    LeaseID {lid}: term {t} mo (not a multiple of {term})")
    results.append(ok3)

    print("=" * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Result: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
