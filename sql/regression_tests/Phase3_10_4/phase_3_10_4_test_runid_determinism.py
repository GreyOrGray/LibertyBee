"""
Phase 3.10.4 — RunID Determinism: Regression Tests.

Codifies the invariant Kate locked 2026-05-25:

    RunID is identity metadata, not a simulation input.
    Two sims with the same seed + projection produce bit-identical end states
    regardless of which RunID they got assigned or what other runs preceded
    them on the same DB.

The bug history: 3.10.3A measurement work surfaced that seed=99 on the same DB
produced different end states between RunID 1 and RunID 2 (16% divergence on
final Cash+CSF+EIP). Root cause: two managers seeded ephemeral RNGs with
expressions that included `self.run_id`:

  1. property_acquisition_manager.py — 7 sites
     `random.Random(self.run_id + self.SEED_OFFSET + <salt> + attempt_id)`
     → fixed to use `self.random_seed` instead.

  2. compliance_manager.py:154 — hash-based RNG
     `hash_input = f"{self.run_id}|{property_id}|..."`
     → fixed to use `self.random_seed`; __init__ extended to accept seed.

Three tests per Kate's BA review §10:

  T1 Same-DB cross-RunID determinism
     Two consecutive sims on the same DB with the same seed + projection
     produce identical end state. This is the primary invariant.

  T2 Fresh-DB RunID 1 stability (control)
     The same seed + projection on two different fresh DBs produce identical
     RunID 1 end state. Proves the test infrastructure works and the fix
     hasn't introduced cross-DB drift.

  T3 Different seeds DO differ
     Two different seeds on the same DB (or two fresh DBs) produce different
     end states. Proves the test isn't a no-op (always-true regardless of
     the fix).

The runner expects the operator to have populated each DB with the relevant
sims before invoking. T1 needs same-DB pairs; T2 needs the same seed on
two distinct DBs; T3 needs distinct seeds.

Usage:
    python sql/regression_tests/Phase3_10_4/phase_3_10_4_test_runid_determinism.py \\
        --env <db> [--t2-control-env <other_db>] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import json
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
sys.path.insert(0, APP_SRC)

ENV_BASE = os.path.join(REPO_ROOT, 'environments') + os.sep

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
SKIP = '\033[93mSKIP\033[0m'


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _read_runs(db) -> list:
    """Return list of {run_id, random_seed, projection_id} for runs in this DB."""
    rows = db.execute_query(
        """
        SELECT RunID, RandomSeed, ProjectionID
        FROM simulation.Run
        ORDER BY RunID
        """
    )
    return [{'run_id': int(r[0]), 'random_seed': int(r[1]), 'projection_id': int(r[2])} for r in rows]


def _end_state(db, run_id: int) -> dict:
    """Compact end-state fingerprint for a RunID. The bag of fields we expect
    to match bit-for-bit when the invariant holds."""
    final_total = db.execute_query(
        """
        SELECT CashBalance + CSFBalance + EIPBalance
        FROM simulation.FundLedger
        WHERE RunID = ?
        ORDER BY LedgerDate DESC, EventID DESC
        OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
        """,
        (run_id,),
    )
    leases = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease WHERE RunID = ?", (run_id,)
    )[0][0]
    accruals = db.execute_query(
        "SELECT COUNT(*) FROM simulation.TenantCreditLedger "
        "WHERE RunID = ? AND TransactionType = 'ACCRUAL'", (run_id,)
    )[0][0]
    renewals = db.execute_query(
        "SELECT COUNT(*) FROM simulation.LeaseTerminationLedger "
        "WHERE RunID = ? AND TxnType = 'LEASE_RENEWED'", (run_id,)
    )[0][0]
    terms = db.execute_query(
        "SELECT COUNT(*) FROM simulation.LeaseTermination WHERE RunID = ?", (run_id,)
    )[0][0]
    fme = db.execute_query(
        "SELECT COUNT(*) FROM simulation.TenantCreditLedger "
        "WHERE RunID = ? AND TransactionType = 'REDEMPTION' "
        "AND Notes LIKE '%FINAL_MONTH_EXIT%'", (run_id,)
    )[0][0]
    return {
        'final_total_cents': int(round(float(final_total[0][0]) * 100)) if final_total and final_total[0][0] is not None else 0,
        'leases': int(leases),
        'accruals': int(accruals),
        'renewals': int(renewals),
        'terminations': int(terms),
        'fme_redemptions': int(fme),
    }


def _states_equal(a: dict, b: dict) -> bool:
    return all(a[k] == b[k] for k in a.keys())


def _state_diff(a: dict, b: dict) -> str:
    diffs = [f"{k}: {a[k]} vs {b[k]}" for k in a.keys() if a[k] != b[k]]
    return '; '.join(diffs) if diffs else 'no diff'


# ---------------------------------------------------------------------------
# T1 — Same-DB cross-RunID determinism
# ---------------------------------------------------------------------------

def t1_same_db_cross_runid(db) -> bool:
    """Find any pair of runs on this DB with the same (seed, projection_id)
    and assert their end states match bit-for-bit."""
    runs = _read_runs(db)
    if len(runs) < 2:
        print(f"  {SKIP} T1: fewer than 2 runs in this DB — pre-populate with "
              f"two sims at the same seed+projection before running")
        return True

    # Group by (seed, projection); find a group with >= 2 runs
    from collections import defaultdict
    by_key = defaultdict(list)
    for r in runs:
        by_key[(r['random_seed'], r['projection_id'])].append(r['run_id'])
    pairs = [(k, v) for k, v in by_key.items() if len(v) >= 2]
    if not pairs:
        print(f"  {SKIP} T1: no (seed, projection) appears twice — pre-populate "
              f"with two sims at the same seed+projection")
        return True

    (seed, proj), run_ids = pairs[0]
    states = [_end_state(db, rid) for rid in run_ids]
    ok = all(_states_equal(states[0], s) for s in states[1:])
    if ok:
        print(f"  {PASS} T1: seed={seed} projection={proj} produced bit-identical "
              f"end state across {len(run_ids)} RunIDs {run_ids}: "
              f"final_total_cents={states[0]['final_total_cents']:,}, "
              f"leases={states[0]['leases']}, accruals={states[0]['accruals']}, "
              f"renewals={states[0]['renewals']}")
        return True

    print(f"  {FAIL} T1: seed={seed} projection={proj} runs {run_ids} diverged. "
          f"State 1: {json.dumps(states[0])}. "
          f"Diffs vs run {run_ids[1]}: {_state_diff(states[0], states[1])}")
    return False


# ---------------------------------------------------------------------------
# T2 — Fresh-DB RunID 1 stability (control)
# ---------------------------------------------------------------------------

def t2_fresh_db_stability(db, control_env: str) -> bool:
    """Compare RunID 1 end state on this DB with RunID 1 of the same
    (seed, projection) on a control fresh DB. Should match."""
    if not control_env:
        print(f"  {SKIP} T2: no --t2-control-env supplied; cannot verify fresh-DB "
              f"stability. Skipping (T1 covers the primary invariant)")
        return True

    runs = _read_runs(db)
    if not any(r['run_id'] == 1 for r in runs):
        print(f"  {SKIP} T2: no RunID 1 in this DB")
        return True
    base = next(r for r in runs if r['run_id'] == 1)
    base_state = _end_state(db, 1)

    from database_manager import DatabaseManager
    control_db = DatabaseManager(_config_path_for(control_env))
    control_runs = _read_runs(control_db)
    matching = [r for r in control_runs
                if r['random_seed'] == base['random_seed']
                and r['projection_id'] == base['projection_id']]
    if not matching:
        print(f"  {SKIP} T2: control DB has no run with seed={base['random_seed']} "
              f"projection={base['projection_id']}")
        return True
    control_state = _end_state(control_db, matching[0]['run_id'])

    if _states_equal(base_state, control_state):
        print(f"  {PASS} T2: seed={base['random_seed']} projection={base['projection_id']} "
              f"matches across primary DB RunID 1 and {control_env[-12:]} RunID "
              f"{matching[0]['run_id']}: final_total_cents={base_state['final_total_cents']:,}")
        return True

    print(f"  {FAIL} T2: cross-DB drift for seed={base['random_seed']} "
          f"projection={base['projection_id']}: {_state_diff(base_state, control_state)}")
    return False


# ---------------------------------------------------------------------------
# T3 — Different seeds DO differ
# ---------------------------------------------------------------------------

def t3_different_seeds_differ(db) -> bool:
    """Find two runs with different seeds on the same projection; assert
    their end states differ. Proves the test framework isn't a no-op."""
    runs = _read_runs(db)
    if len(runs) < 2:
        print(f"  {SKIP} T3: fewer than 2 runs in this DB")
        return True

    # Find any pair with same projection but different seeds
    pair = None
    for i, r in enumerate(runs):
        for s in runs[i+1:]:
            if r['projection_id'] == s['projection_id'] and r['random_seed'] != s['random_seed']:
                pair = (r, s); break
        if pair:
            break
    if not pair:
        print(f"  {SKIP} T3: no two runs share a projection with distinct seeds")
        return True

    state_a = _end_state(db, pair[0]['run_id'])
    state_b = _end_state(db, pair[1]['run_id'])
    if not _states_equal(state_a, state_b):
        print(f"  {PASS} T3: seed={pair[0]['random_seed']} vs seed={pair[1]['random_seed']} "
              f"(both projection={pair[0]['projection_id']}) produced different states. "
              f"Diff: {_state_diff(state_a, state_b)}")
        return True

    print(f"  {FAIL} T3: seed={pair[0]['random_seed']} and seed={pair[1]['random_seed']} "
          f"produced IDENTICAL state — suggests the simulator isn't actually using "
          f"the seed (or the state fingerprint is too coarse)")
    return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', required=True, help='Test database name')
    parser.add_argument('--t2-control-env', default=None,
                        help='Optional second DB for T2 fresh-DB stability check')
    parser.add_argument('--assert', dest='assert_mode', action='store_true',
                        help='Exit 1 on any FAIL (CI mode)')
    args = parser.parse_args()

    from database_manager import DatabaseManager
    db = DatabaseManager(_config_path_for(args.env))
    print(f"\n=== Phase 3.10.4 — RunID Determinism (env={args.env}) ===\n")

    results = [
        t1_same_db_cross_runid(db),
        t2_fresh_db_stability(db, args.t2_control_env),
        t3_different_seeds_differ(db),
    ]
    n_pass = sum(1 for r in results if r)
    print(f"\nResults: {n_pass}/{len(results)} passed\n")

    if args.assert_mode and n_pass < len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
