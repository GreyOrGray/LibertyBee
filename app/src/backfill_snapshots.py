"""
Backfill snapshots for an existing simulation run.

Synthesizes simulation.RunSnapshot rows for a given RunID by replaying the
same aggregation queries used by SnapshotManager at each cadence boundary
date between the run's StartDate and the run's actual end date (the last
event date for the run).

Backfilled snapshots are reconstructed analytics artifacts; they
share business fields with LIVE captures but are tagged SnapshotSource='BACKFILL'.

Usage:
    python app/src/backfill_snapshots.py --env <db> --run-id <N> \
        [--cadence QUARTERLY] [--clear-existing]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from database_manager import DatabaseManager  # noqa: E402
from snapshot_manager import SnapshotManager, _QUARTER_END_DAYS  # noqa: E402


def _config_path_for(env: str) -> str:
    app_dir = os.path.dirname(SCRIPT_DIR)  # app/
    return os.path.join(os.path.dirname(app_dir), 'environments', env, 'db_config.json')


def _enumerate_cadence_boundaries(start: date, end: date, cadence: str) -> list[date]:
    """List cadence-boundary dates in [start, end] inclusive."""
    out: list[date] = []
    cur = start
    while cur <= end:
        if cadence == 'QUARTERLY':
            if cur.month in (3, 6, 9, 12) and cur.day == _QUARTER_END_DAYS[cur.month]:
                out.append(cur)
        elif cadence == 'MONTHLY':
            # Last day of month
            next_month = (cur.month % 12) + 1
            next_year = cur.year + (1 if cur.month == 12 else 0)
            if (date(next_year, next_month, 1) - timedelta(days=1)) == cur:
                out.append(cur)
        elif cadence == 'ANNUAL':
            if cur.month == 12 and cur.day == 31:
                out.append(cur)
        cur += timedelta(days=1)
    return out


def _last_event_date(db: DatabaseManager, run_id: int) -> date:
    row = db.execute_query(
        "SELECT MAX(EffectiveDate) FROM simulation.Event WHERE RunID=?", (run_id,)
    )
    return row[0][0]


def _run_start_date(db: DatabaseManager, run_id: int) -> date:
    row = db.execute_query(
        "SELECT StartDate FROM simulation.Run WHERE RunID=?", (run_id,)
    )
    return row[0][0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--cadence', default='QUARTERLY',
                        choices=['MONTHLY', 'QUARTERLY', 'ANNUAL'])
    parser.add_argument('--clear-existing', action='store_true',
                        help='DELETE existing BACKFILL rows for this RunID before re-backfilling.')
    args = parser.parse_args()

    db = DatabaseManager(_config_path_for(args.env))

    start_d = _run_start_date(db, args.run_id)
    end_d = _last_event_date(db, args.run_id)
    if end_d is None:
        print(f'No events found for RunID {args.run_id}; nothing to backfill.')
        return 1

    print(f'Backfilling snapshots for RunID {args.run_id}')
    print(f'  Cadence:    {args.cadence}')
    print(f'  Date range: {start_d} -> {end_d}')

    if args.clear_existing:
        deleted = db.execute_non_query(
            "DELETE FROM simulation.RunSnapshot WHERE RunID=? AND SnapshotSource='BACKFILL'",
            (args.run_id,),
        )
        print(f'  Cleared existing BACKFILL rows for RunID {args.run_id}')

    boundaries = _enumerate_cadence_boundaries(start_d, end_d, args.cadence)
    if not boundaries:
        print('  No cadence boundaries in range; nothing to capture.')
        return 0

    mgr = SnapshotManager(db, args.run_id, cadence=args.cadence, simulation_start=start_d)
    captured = 0
    skipped = 0
    for d in boundaries:
        result = mgr.capture(d, source='BACKFILL')
        if result is None:
            skipped += 1
        else:
            captured += 1

    # Final halt-date snapshot if end_d is not a normal cadence boundary
    if end_d not in boundaries:
        result = mgr.capture(end_d, source='BACKFILL')
        if result:
            captured += 1
            print(f'  Captured halt-date snapshot at {end_d}')

    print(f'  Captured: {captured}, skipped (already exists): {skipped}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
