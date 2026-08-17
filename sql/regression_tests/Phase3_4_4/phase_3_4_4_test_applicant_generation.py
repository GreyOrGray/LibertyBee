"""
Phase 3.4.4 — Applicant-generation mechanism regression tests (white-box).

Determinism contract for tenant_manager._generate_candidate_slate:
    same vacancy + same date      -> identical slate (slot-for-slot)
    same vacancy + different date  -> at least one slot differs
    same date    + different vacancy -> at least one slot differs
plus distribution + composition sanity.

Guards the Phase 3.7.11.2 fix that widened the applicant RNG seed key with
current_date. Pre-fix, the seed was (run_seed, vacancy_id, applicant_index) only,
so daily retries of an open vacancy regenerated the identical slate — a vacancy
whose seeded slate held no qualifier looped on the same rejects forever.

Ported into the tracked suite from scratch/test_applicant_generation.py (#106).
Outcome-level coverage lives in Phase3_7_11 (occupancy health); this is the
mechanism guard.

Usage:
    python sql/regression_tests/Phase3_4_4/phase_3_4_4_test_applicant_generation.py --env <db> --assert
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))  # PhaseX -> regression_tests -> sql -> repo
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + "db_config.json"


def _candidate_signature(c) -> tuple:
    """Stable comparable tuple for a candidate (ignores applicant_index)."""
    return (
        c.household_type,
        c.household_income_band,
        c.adult_count,
        c.child_count,
        str(c.estimated_monthly_income),
    )


def t1_same_date_reproducibility(tenant_manager) -> bool:
    """Same vacancy + same date -> slot-for-slot identical slate."""
    vacancy_id = 1
    d = date(2026, 1, 1)
    slate_a = tenant_manager._generate_candidate_slate(vacancy_id, d)
    slate_b = tenant_manager._generate_candidate_slate(vacancy_id, d)
    if len(slate_a) != len(slate_b):
        print(f"  T1 same-date reproducibility: slate sizes differ "
              f"({len(slate_a)} vs {len(slate_b)}) [{FAIL}]")
        return False
    mismatches = [
        i for i, (a, b) in enumerate(zip(slate_a, slate_b))
        if _candidate_signature(a) != _candidate_signature(b)
    ]
    ok = len(mismatches) == 0
    print(f"  T1 same-date reproducibility: {len(mismatches)} slot mismatches "
          f"across {len(slate_a)} slots (expect 0) [{PASS if ok else FAIL}]")
    return ok


def t2_cross_date_variation(tenant_manager) -> bool:
    """Same vacancy + different date -> at least one slot differs."""
    vacancy_id = 1
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    slate_d1 = tenant_manager._generate_candidate_slate(vacancy_id, d1)
    slate_d2 = tenant_manager._generate_candidate_slate(vacancy_id, d2)
    differences = [
        i for i, (a, b) in enumerate(zip(slate_d1, slate_d2))
        if _candidate_signature(a) != _candidate_signature(b)
    ]
    ok = len(differences) >= 1
    print(f"  T2 cross-date variation: {len(differences)} of {len(slate_d1)} "
          f"slots differ between D and D+1 (expect >=1) [{PASS if ok else FAIL}]")
    return ok


def t3_cross_vacancy_variation(tenant_manager) -> bool:
    """Same date + different vacancy -> at least one slot differs."""
    d = date(2026, 1, 1)
    slate_v1 = tenant_manager._generate_candidate_slate(1, d)
    slate_v2 = tenant_manager._generate_candidate_slate(2, d)
    differences = [
        i for i, (a, b) in enumerate(zip(slate_v1, slate_v2))
        if _candidate_signature(a) != _candidate_signature(b)
    ]
    ok = len(differences) >= 1
    print(f"  T3 cross-vacancy variation: {len(differences)} of {len(slate_v1)} "
          f"slots differ between vacancy 1 and vacancy 2 on same date "
          f"(expect >=1) [{PASS if ok else FAIL}]")
    return ok


def t4_distribution_sanity(tenant_manager) -> bool:
    """Loose sanity: each household type + income band appears across a sample.

    Phase 1.8 (KD-018): bands are now B1..B5 — the renter-calibrated
    %-of-median earner bands (income_model.BAND_LABELS), replacing the
    retired LOW/MEDIUM/HIGH/VERY_HIGH static bands."""
    household_counts = {'SINGLE': 0, 'COUPLE': 0, 'FAMILY': 0, 'ROOMMATES': 0}
    income_counts = {'B1': 0, 'B2': 0, 'B3': 0, 'B4': 0, 'B5': 0}
    d = date(2026, 1, 1)
    for v_id in range(1, 31):
        for offset in range(10):
            slate = tenant_manager._generate_candidate_slate(v_id, d + timedelta(days=offset))
            for c in slate:
                household_counts[c.household_type] += 1
                income_counts[c.household_income_band] += 1
    total = sum(household_counts.values()) or 1
    missing_household = [k for k, v in household_counts.items() if v == 0]
    missing_income = [k for k, v in income_counts.items() if v == 0]
    ok = len(missing_household) == 0 and len(missing_income) == 0
    print(f"  T4 distribution sanity: {total} candidates sampled — "
          f"missing household types: {missing_household or 'none'}, "
          f"missing income bands: {missing_income or 'none'} [{PASS if ok else FAIL}]")
    return ok


def t5_composition_invariants(tenant_manager) -> bool:
    """Composition rules per household type hold under the seed."""
    d = date(2026, 1, 1)
    bad = []
    for v_id in range(1, 21):
        for offset in range(5):
            slate = tenant_manager._generate_candidate_slate(v_id, d + timedelta(days=offset))
            for c in slate:
                if c.household_type == 'SINGLE':
                    if not (c.adult_count == 1 and c.child_count == 0):
                        bad.append((v_id, offset, 'SINGLE', c.adult_count, c.child_count))
                elif c.household_type == 'COUPLE':
                    if not (c.adult_count == 2 and c.child_count == 0):
                        bad.append((v_id, offset, 'COUPLE', c.adult_count, c.child_count))
                elif c.household_type == 'FAMILY':
                    if not (c.adult_count == 2 and 0 <= c.child_count <= 3):
                        bad.append((v_id, offset, 'FAMILY', c.adult_count, c.child_count))
                elif c.household_type == 'ROOMMATES':
                    if not (2 <= c.adult_count <= 4 and c.child_count == 0):
                        bad.append((v_id, offset, 'ROOMMATES', c.adult_count, c.child_count))
    ok = len(bad) == 0
    print(f"  T5 composition invariants: {len(bad)} violations (expect 0) "
          f"[{PASS if ok else FAIL}]")
    for row in bad[:3]:
        print(f"    sample violation: {row}")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, help="Environment / DB name under environments/")
    parser.add_argument("--assert", dest="assert_mode", action="store_true",
                        help="acceptance mode (module exits 1 on any failure)")
    parser.add_argument("--run-id", type=int, default=None,
                        help="RunID to anchor for the run_seed lookup (default: latest run)")
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    from event_logger import EventLogger  # type: ignore
    from tenant_manager import TenantManager  # type: ignore

    db = DatabaseManager(_config_path_for(args.env))

    run_id = args.run_id
    if run_id is None:
        row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
        run_id = row[0][0] if row and row[0][0] is not None else 1

    event_logger = EventLogger(db)
    event_logger.set_run_id(run_id)
    # run_seed for the slate RNG is read from the Run table by the manager; the
    # constructor seed + below_market_rent_pct (0.10) just satisfy the signature —
    # neither is used by _generate_candidate_slate (the only method exercised here).
    tenant_manager = TenantManager(db, event_logger, run_id, 12345, 0.10)

    print(f"Phase 3.4.4 applicant-generation mechanism tests against {args.env} (RunID {run_id})")
    print("=" * 78)

    results = [
        t1_same_date_reproducibility(tenant_manager),
        t2_cross_date_variation(tenant_manager),
        t3_cross_vacancy_variation(tenant_manager),
        t4_distribution_sanity(tenant_manager),
        t5_composition_invariants(tenant_manager),
    ]

    print("=" * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Result: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
