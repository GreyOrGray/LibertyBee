"""
Phase 3.10.3 — `get_fund_balances` Tiebreaker Invariant Test.

Background: pre-3.10.3 `fund_manager.get_fund_balances` selected `TOP 1 ... ORDER BY
LedgerDate DESC` with no secondary tiebreaker. On any (RunID, LedgerDate) with > 1
FundLedger row, the row returned was implementation-defined. The fix appends
`, EventID DESC` so the latest-event-of-the-day is returned deterministically.

Two tests:

  T1  For every (RunID, LedgerDate) with > 1 FundLedger row in the verification
      run, the row that the FIXED predicate would return (MAX(EventID) per day)
      matches what `get_fund_balances` actually returns on a probe at that date.

  T2  No (RunID, LedgerDate) appears in the run with > 1 row but distinct balance
      values at the same EventID — i.e., the tiebreaker is well-defined: there is
      exactly one MAX(EventID) row per (RunID, LedgerDate).

Usage:
    python sql/regression_tests/Phase3_10_3/phase_3_10_3_test_get_fund_balances_tiebreaker.py \
        --env <db> [--run-id N] [--assert]
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_PROMOTED = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
if os.path.basename(_REPO_ROOT_PROMOTED) == 'scratch':
    REPO_ROOT = os.path.dirname(_REPO_ROOT_PROMOTED)
    APP_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
else:
    REPO_ROOT = _REPO_ROOT_PROMOTED
    APP_SRC = os.path.join(REPO_ROOT, 'app', 'src')

# Phase 3.10.3 pre-promotion validation: --scratch arg forces import from
# scratch/app/src/, used to verify the fix works before scratch -> app promotion.
if '--scratch' in sys.argv:
    APP_SRC = os.path.join(REPO_ROOT, 'scratch', 'app', 'src')
    sys.argv.remove('--scratch')

sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
SKIP = '\033[93mSKIP\033[0m'


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _resolve_run_id(db, override) -> int:
    if override is not None:
        return int(override)
    row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
    if not row or row[0][0] is None:
        raise SystemExit("No runs found in simulation.Run; cannot pick a RunID.")
    return int(row[0][0])


# ---------------------------------------------------------------------------
# T1 — get_fund_balances returns the MAX(EventID) row per (RunID, LedgerDate)
# ---------------------------------------------------------------------------

def t1_returns_latest_event_of_day(db, run_id: int) -> bool:
    """For each (RunID, LedgerDate) with > 1 FundLedger row in the verification
    run, probe `get_fund_balances` at that date and assert the returned balances
    match the row with the maximum EventID on that date."""
    from fund_manager import FundManager
    fm = FundManager(db_manager=db, event_logger=None)

    # Pick a sample of dates that have multi-row days to keep the test bounded.
    dates_with_dups = db.execute_query(
        """
        SELECT LedgerDate
        FROM simulation.FundLedger
        WHERE RunID = ?
        GROUP BY LedgerDate
        HAVING COUNT(*) > 1
        ORDER BY LedgerDate
        OFFSET 0 ROWS FETCH FIRST 25 ROWS ONLY
        """,
        (run_id,),
    )
    if not dates_with_dups:
        print(f"  {SKIP} T1: no multi-row days in run — invariant not exercised")
        return True

    fm.set_run_id(run_id) if hasattr(fm, 'set_run_id') else None

    mismatches = 0
    examples = []
    for (d,) in dates_with_dups:
        expected = db.execute_query(
            """
            SELECT CashBalance, CSFBalance, EIPBalance, EscrowBalance, EventID
            FROM simulation.FundLedger
            WHERE RunID = ? AND LedgerDate = ?
            ORDER BY EventID DESC
            OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """,
            (run_id, d),
        )[0]

        actual = fm.get_fund_balances(run_id, d)

        # Compare cash / csf / eip / escrow with small float tolerance
        eps = 0.005
        diffs = [
            ("Cash", float(actual.cash_balance), float(expected[0])),
            ("CSF",  float(actual.csf_balance),  float(expected[1])),
            ("EIP",  float(actual.eip_balance),  float(expected[2])),
            ("Esc",  float(actual.escrow_balance), float(expected[3])),
        ]
        if any(abs(a - e) > eps for _, a, e in diffs):
            mismatches += 1
            if len(examples) < 3:
                examples.append((d, [(n, a, e) for n, a, e in diffs if abs(a - e) > eps]))

    if mismatches > 0:
        print(f"  {FAIL} T1: {mismatches}/{len(dates_with_dups)} sampled multi-row "
              f"days returned non-MAX(EventID) balances. Examples: {examples}")
        return False
    print(f"  {PASS} T1: {len(dates_with_dups)} sampled multi-row days return the "
          f"MAX(EventID) balances (tiebreaker is deterministic)")
    return True


# ---------------------------------------------------------------------------
# T2 — MAX(EventID) per (RunID, LedgerDate) is well-defined (no ties)
# ---------------------------------------------------------------------------

def t2_max_eventid_unique_per_day(db, run_id: int) -> bool:
    """EventID is monotonically increasing within a run, so MAX(EventID) per
    (RunID, LedgerDate) must yield exactly one row. This is a schema invariant
    that the tiebreaker depends on."""
    violations = db.execute_query(
        """
        WITH per_day_max AS (
            SELECT LedgerDate, MAX(EventID) AS MaxEvt
            FROM simulation.FundLedger
            WHERE RunID = ?
            GROUP BY LedgerDate
        )
        SELECT COUNT(*) FROM per_day_max p
        JOIN simulation.FundLedger f
          ON f.RunID = ? AND f.LedgerDate = p.LedgerDate AND f.EventID = p.MaxEvt
        GROUP BY p.LedgerDate, p.MaxEvt
        HAVING COUNT(*) > 1
        """,
        (run_id, run_id),
    )
    if violations and len(violations) > 0:
        print(f"  {FAIL} T2: {len(violations)} (LedgerDate, MAX(EventID)) pairs map "
              f"to multiple FundLedger rows — schema invariant broken")
        return False

    multi_row_days = db.execute_query(
        """
        SELECT COUNT(*) FROM (
            SELECT LedgerDate FROM simulation.FundLedger
            WHERE RunID = ? GROUP BY LedgerDate HAVING COUNT(*) > 1
        ) z
        """,
        (run_id,),
    )[0][0]

    if multi_row_days == 0:
        print(f"  {SKIP} T2: no multi-row days in run; invariant trivially holds")
        return True
    print(f"  {PASS} T2: MAX(EventID) per day is unique across "
          f"{multi_row_days} multi-row days")
    return True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', required=True, help='Test database name')
    parser.add_argument('--run-id', type=int, default=None)
    parser.add_argument('--assert', dest='assert_mode', action='store_true',
                        help='Exit 1 on any FAIL (CI mode)')
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(_config_path_for(args.env))
    run_id = _resolve_run_id(db, args.run_id)
    print(f"\n=== Phase 3.10.3 — get_fund_balances tiebreaker "
          f"(env={args.env}, RunID={run_id}) ===\n")

    results = [
        t1_returns_latest_event_of_day(db, run_id),
        t2_max_eventid_unique_per_day(db, run_id),
    ]
    n_pass = sum(1 for r in results if r)
    print(f"\nResults: {n_pass}/{len(results)} passed\n")

    if args.assert_mode and n_pass < len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
