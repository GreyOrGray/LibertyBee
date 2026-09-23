"""
Phase 3.4.5 — Candidate-qualification gate regression tests (bedroom-fit + 30% income).

Unit-level coverage of the two qualification gates that the outcome-level suite
(Phase3_7_11 occupancy health) only *counts* (rejection tallies) but never *validates*:

  Q1 bedroom-fit (`_check_bedroom_fit`): max occupancy only, no minimum.
       MaxOccupants = bedrooms x QUAL.MaxOccupantsPerBedroom + QUAL.MaxOccupantsBonus
       ("2 per bedroom + 1"; #118), studio (0BR) uses QUAL.MaxOccupantsStudio.
       Reject only when household size EXCEEDS capacity (boundary = fit).
  Q2 income 30%-rule (`_check_income_qualification`): rent <= income x ratio,
       ratio = registry max_rent_to_income_ratio (default 0.30).

Both gates are pure functions on tenant_manager, so the assertions are
scenario-agnostic by construction: Q1 derives expectations from the live registry
occupancy params across studio/1BR/2BR/3BR/4BR at the capacity boundary; Q2 derives
the threshold from the live registry ratio (not a hardcoded 0.30).

NOTE (test lineage): the V0.1 source (scratch/test_candidate_evaluation.py) expected
"5 occupants in a 2BR -> fits". The V1 cap (bedrooms x 2 = 4) rejected it; #118's
"2 per bedroom + 1" rule caps a 2BR at 5, so 5 fits again — this module now pins the
parameterized rule. The source's DB-writing end-to-end selection portion (T3-T5) is
intentionally NOT ported — it mutated the run; the gate logic it exercised is covered
here at the unit level.

✅ #118 RESOLVED (Phase 1.3): the old hard 2-per-bedroom cap (which rejected ordinary
larger families, e.g. 2 adults + 3 children from a 2BR — a familial-status fair-housing
tension) is replaced by the parameterized "2 per bedroom + 1" rule (QUAL.MaxOccupants*).
This module's tripwire fired as designed when the rule changed; Q1 now derives from the
live params. The deeper age-aware model (infants uncounted; couple + child in a studio)
is tracked in #127.

Ported into the tracked suite (#106).

Usage:
    python sql/regression_tests/Phase3_4_5/phase_3_4_5_test_candidate_qualification.py --env <db> --assert
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))  # PhaseX -> regression_tests -> sql -> repo
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + "db_config.json"


def _max_occupants(tm, bedrooms: int) -> int:
    """The bedroom-fit contract (#118, parameterized "2 per bedroom + 1"), derived from
    the live registry params — NOT a hardcode (mirrors Q2's live-ratio pattern)."""
    p = tm.params
    if bedrooms == 0:
        return p.max_occupants_studio
    return bedrooms * p.max_occupants_per_bedroom + p.max_occupants_bonus


def _candidate(adults: int, children: int, income: Decimal):
    from tenant_manager import ApplicantCandidate  # type: ignore
    return ApplicantCandidate(0, 'FAMILY', 'MEDIUM', adults, children, income)


def _split(occ: int):
    """Split a total occupant count into (adults, children) with >=1 adult."""
    adults = 2 if occ >= 2 else 1
    return adults, occ - adults


def q1_bedroom_fit(tm) -> bool:
    """At capacity -> fits; one over -> rejects; under -> fits, across 0..4 BR.

    Pins the parameterized "2 per bedroom + 1" rule (#118 resolved): expectations are
    derived from the live QUAL.MaxOccupants* registry params, so this stays correct as
    the params move (it does not re-hardcode the cap).
    """
    bad = []
    checked = 0
    for br in (0, 1, 2, 3, 4):
        mx = _max_occupants(tm, br)
        cases = [(mx, True), (mx + 1, False)]
        if mx - 1 >= 1:
            cases.append((mx - 1, True))
        for occ, expected in cases:
            adults, children = _split(occ)
            got = tm._check_bedroom_fit(_candidate(adults, children, Decimal('5000')), br)
            checked += 1
            if got != expected:
                bad.append((f"{br}BR", occ, f"expected {expected}", f"got {got}"))
    ok = not bad
    print(f"  Q1 bedroom-fit: {checked} boundary cases across 0-4BR "
          f"(max = bedrooms x perBR + bonus; studio own cap) — {len(bad)} mismatches (expect 0) "
          f"[{PASS if ok else FAIL}]")
    for row in bad[:5]:
        print(f"    mismatch: {row}")
    return ok


def q2_income_30pct(tm) -> bool:
    """rent == income x ratio qualifies (<=); a cent over does not; under does."""
    ratio = tm.params.max_rent_to_income_ratio
    income = Decimal('5000')
    # mirror the method's own computation so types/precision align exactly
    max_rent = income * ratio
    cases = [
        (max_rent, True, "at threshold"),
        (max_rent + Decimal('0.01'), False, "a cent over"),
        (max_rent - Decimal('1'), True, "under"),
    ]
    bad = []
    for rent, expected, label in cases:
        got = tm._check_income_qualification(_candidate(1, 0, income), rent)
        if got != expected:
            bad.append((label, str(rent), f"expected {expected}", f"got {got}"))
    ok = not bad
    print(f"  Q2 income 30%-rule (ratio={ratio}, income=${income}, max_rent=${max_rent}): "
          f"{len(bad)} mismatches (expect 0) [{PASS if ok else FAIL}]")
    for row in bad:
        print(f"    mismatch: {row}")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, help="Environment / DB name under environments/")
    parser.add_argument("--assert", dest="assert_mode", action="store_true",
                        help="acceptance mode (module exits 1 on any failure)")
    parser.add_argument("--run-id", type=int, default=None,
                        help="RunID to anchor the TenantManager params load (default: latest run)")
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
    # The seed + below_market_rent_pct (0.10) just satisfy the signature; neither is
    # used by _check_bedroom_fit / _check_income_qualification (the methods tested).
    tenant_manager = TenantManager(db, event_logger, run_id, 12345, 0.10)

    print(f"Phase 3.4.5 candidate-qualification gate tests against {args.env} (RunID {run_id})")
    print("=" * 78)

    results = [
        q1_bedroom_fit(tenant_manager),
        q2_income_30pct(tenant_manager),
    ]

    print("=" * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Result: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
