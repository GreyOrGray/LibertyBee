"""
#186 — measured index pass (V00067): regression gates.

Mirrors the Phase 3.10.3A pattern (V00031 FundLedger cover index gates):

  T1  Added index exists — IX_PropertyMarket_StatusSweep on
      simulation.PropertyMarket (V00067 ran).
  T2  Dropped indexes are absent — IX_MonthlyPaymentStatus_Late,
      IX_RentCollection_Lease, IX_RentCollection_Month,
      IX_simTenantCreditLedger_Collection (the written-never-read set;
      the Late index additionally indexed a status that never persists in
      V1 flow for a predicate retired in Phase 3.10.2).
  T3  Added index is used — a market-sweep-shaped probe query bumps
      user_seeks on IX_PropertyMarket_StatusSweep (the optimizer picks it
      for the daily MarketStatus sweeps it was built for).

Value-neutrality (indexes move access paths, not results) was proven at the
pass itself: same-seed 240-mo A/B runs with byte-identical final funds +
ledger checksum — record on issue #186. These gates only pin the schema
state and the index's live usability.

Usage:
    python sql/regression_tests/IDX186/idx_186_test_index_pass.py --env <db> [--assert]
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)
sys.path.insert(0, os.path.join(REPO_ROOT, "sql", "regression_tests"))
from _dialect import index_exists, index_user_seeks, SKIP_NOTE  # noqa: E402

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

ADDED = ("simulation.PropertyMarket", "IX_PropertyMarket_StatusSweep")
DROPPED = (
    ("simulation.MonthlyPaymentStatus", "IX_MonthlyPaymentStatus_Late"),
    ("simulation.RentCollection", "IX_RentCollection_Lease"),
    ("simulation.RentCollection", "IX_RentCollection_Month"),
    ("simulation.TenantCreditLedger", "IX_simTenantCreditLedger_Collection"),
)


def t1_added_exists(db) -> bool:
    ok = index_exists(db, *ADDED)
    print(f"  T1 added index exists ({ADDED[1]}): {ok} [{PASS if ok else FAIL}]")
    return ok


def t2_dropped_absent(db) -> bool:
    leftovers = [name for tbl, name in DROPPED if index_exists(db, tbl, name)]
    ok = not leftovers
    print(f"  T2 dropped indexes absent: {len(DROPPED) - len(leftovers)}/{len(DROPPED)} gone"
          f"{' (still present: ' + ', '.join(leftovers) + ')' if leftovers else ''} "
          f"[{PASS if ok else FAIL}]")
    return ok


def t3_added_used(db):
    def seeks():
        return index_user_seeks(db, ADDED[0], ADDED[1])

    before = seeks()
    if before is None:
        print(f"  T3 added index used: {SKIP_NOTE}")
        return None  # not counted as a gate on PG
    # the daily-sweep shape the index was built for (results unused)
    db.execute_query(
        "SELECT PropertyID, CurrentListingPrice FROM simulation.PropertyMarket "
        "WHERE RunID = ? AND MarketStatus = 'Listed' AND ExpirationDate IS NOT NULL",
        (1,))
    after = seeks()
    ok = after > before
    print(f"  T3 added index used by the sweep shape: user_seeks {before} -> {after} "
          f"[{PASS if ok else FAIL}]")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--run-id", default=None)  # accepted for harness parity; unused
    parser.add_argument("--assert", dest="assert_mode", action="store_true")
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(ENV_BASE + args.env + os.sep + "db_config.json")

    print(f"#186 index-pass gates — {args.env}")
    print("=" * 78)
    results = [t1_added_exists(db), t2_dropped_absent(db), t3_added_used(db)]
    print("=" * 78)
    counted = [r for r in results if r is not None]  # None = loud engine skip
    passed = sum(1 for r in counted if r)
    print(f"Result: {passed}/{len(counted)} gates passed"
          f"{' (' + str(len(results) - len(counted)) + ' engine-specific skipped)' if len(counted) != len(results) else ''}")
    if args.assert_mode and passed != len(counted):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
