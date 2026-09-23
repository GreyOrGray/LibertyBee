"""
Phase 3.7.12 — Property Market BasePrice Integrity Regression Test.

Read-only assertion-mode regression test. Confirms that the BasePrice
regression repaired by migration V00025 has not drifted back into a
broken state.

Assertions:
  T1  reference.Properties rows with BasePrice IS NULL = 0
  T2  reference.Properties rows with BasePrice <= 0 = 0
  T3  Active simulation.PropertyMarket rows (any acquisition-relevant
      MarketStatus) with NULL or non-positive joined BasePrice = 0

If any assertion fails, prints the offending PropertyIDs (up to 20) and
exits 1. On success exits 0.

Companion docs:
  docs/phases/phase_3_7/phase_3_7_12_diagnostic_note.md
  docs/phases/phase_3_7/phase_3_7_12_diagnostic_ba_response.md
  sql/migrations/V00025__fix_NULL_base_prices.sql (after promotion)

Usage:
    python sql/regression_tests/Phase3_7_12/phase_3_7_12_test_baseprice_integrity.py --env <db>
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

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'

# MarketStatus values that represent a property which can enter or
# re-enter listing/acquisition logic and thus would crash _get_listing_price
# (or any downstream code reading BasePrice) on bad data.
ACQUISITION_RELEVANT_STATUSES = (
    'Available',
    'OtherBuyerOwned',
    'Listed',
    'UnderContractLB',
)


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _print_offenders(label: str, rows: list[tuple]) -> None:
    if not rows:
        return
    print(f'    Offending {label} (showing up to 20):')
    for r in rows[:20]:
        print(f'      {r}')
    if len(rows) > 20:
        print(f'      ... and {len(rows) - 20} more')


def t1_no_null_baseprice(db) -> bool:
    rows = db.execute_query(
        """
        SELECT PropertyID, Source, SourceID, PropertyAddress
        FROM reference.Properties
        WHERE BasePrice IS NULL
        ORDER BY PropertyID
        """
    )
    ok = len(rows) == 0
    print(f"  T1 reference.Properties BasePrice IS NULL: {len(rows)} (expect 0) [{PASS if ok else FAIL}]")
    if not ok:
        _print_offenders('PropertyIDs', [tuple(r) for r in rows])
    return ok


def t2_no_nonpositive_baseprice(db) -> bool:
    rows = db.execute_query(
        """
        SELECT PropertyID, BasePrice, Source, SourceID, PropertyAddress
        FROM reference.Properties
        WHERE BasePrice IS NOT NULL AND BasePrice <= 0
        ORDER BY PropertyID
        """
    )
    ok = len(rows) == 0
    print(f"  T2 reference.Properties BasePrice <= 0: {len(rows)} (expect 0) [{PASS if ok else FAIL}]")
    if not ok:
        _print_offenders('PropertyIDs', [tuple(r) for r in rows])
    return ok


def t3_no_active_listings_with_bad_baseprice(db) -> bool:
    """Active PropertyMarket rows that can enter listing/acquisition logic
    must reference reference.Properties rows with a positive BasePrice."""
    placeholders = ','.join(f"'{s}'" for s in ACQUISITION_RELEVANT_STATUSES)
    rows = db.execute_query(
        f"""
        SELECT pm.RunID, pm.PropertyID, pm.MarketStatus,
               CASE WHEN rp.PropertyID IS NULL THEN '<missing reference row>'
                    WHEN rp.BasePrice IS NULL THEN '<NULL>'
                    ELSE CAST(rp.BasePrice AS VARCHAR(40))
               END AS BasePriceState
        FROM simulation.PropertyMarket pm
        LEFT JOIN reference.Properties rp ON pm.PropertyID = rp.PropertyID
        WHERE pm.ExpirationDate IS NULL
          AND pm.MarketStatus IN ({placeholders})
          AND (rp.PropertyID IS NULL OR rp.BasePrice IS NULL OR rp.BasePrice <= 0)
        ORDER BY pm.RunID, pm.PropertyID, pm.MarketStatus
        """
    )
    ok = len(rows) == 0
    label = ', '.join(ACQUISITION_RELEVANT_STATUSES)
    print(f"  T3 active PropertyMarket ({label}) with bad BasePrice: {len(rows)} (expect 0) [{PASS if ok else FAIL}]")
    if not ok:
        _print_offenders('(RunID, PropertyID, Status, BasePrice)', [tuple(r) for r in rows])
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True, help='Environment / DB name under environments/')
    parser.add_argument('--assert', dest='assert_mode', action='store_true',
                        help='acceptance mode (module already exits 1 on any failure)')
    args = parser.parse_args()

    from database_manager import DatabaseManager  # type: ignore
    db = DatabaseManager(_config_path_for(args.env))

    print(f'Phase 3.7.12 BasePrice integrity tests against {args.env}')
    print('=' * 78)

    results = [
        t1_no_null_baseprice(db),
        t2_no_nonpositive_baseprice(db),
        t3_no_active_listings_with_bad_baseprice(db),
    ]

    print('=' * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f'Result: {passed}/{total} tests passed')
    sys.exit(0 if passed == total else 1)


if __name__ == '__main__':
    main()
