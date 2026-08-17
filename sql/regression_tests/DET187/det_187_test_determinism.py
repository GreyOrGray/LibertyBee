"""
#187 — invariant-#8 determinism gates (the RNG-order hardening).

Background: same-seed runs on byte-identical DBs diverged (LeaseID-49 renewal
flip) because sequential RNG draws were mapped to entities in query row order
with no ORDER BY. The fix pins ORDER BY on the 8 root queries. These gates
make invariant #8 a TESTED property:

  T1  Negative control (the gate can go RED): running the renewal pre-roll
      with the imposed ORDER BY reversed (DESC, forced in-process — not left
      to the optimizer) produces DIFFERENT decisions than ASC for the same
      seeded stream on a multi-lease month. Proves the gate detects exactly
      the #187 defect class. A determinism test that cannot be shown to fail
      on the bug is not a test.
  T2  Same-plan double run: two full sims, same seed, same env -> normalized
      event streams byte-equal. Fast smoke. What this does NOT prove:
      plan-change immunity (the b1/b2 divergence would have PASSED this on
      the unhardened engine — that is exactly why T1 and T3 exist).
  T3  Perturbed-plan run: a third sim with throwaway nonclustered indexes on
      the root tables' order-driving columns (the #186 trigger, generalized)
      -> stream equal to T2's. If the optimizer ignores the throwaway
      indexes (possible on future SQL Server versions), the test WARNS that
      the perturbation didn't bite rather than false-failing; T1 remains the
      load-bearing negative control.

Self-contained: creates its own ephemeral env (LibertyBee_Test_DET187_gate,
destructive reset each run) so its extra runs never disturb the suite's
shared baseline (other gates resolve MAX(RunID)).

Usage:
    python sql/regression_tests/DET187/det_187_test_determinism.py --env <db> [--assert]
    (--env names the SUITE env; used only to locate environments/ — the gate
     builds its own env beside it.)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
APP_SRC = os.path.join(REPO_ROOT, "app", "src")
sys.path.insert(0, APP_SRC)
sys.path.insert(0, os.path.join(REPO_ROOT, "sql", "regression_tests"))
from _dialect import is_pg  # noqa: E402

ENV_BASE = os.path.join(REPO_ROOT, "environments") + os.sep

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

GATE_ENV = "LibertyBee_Test_DET187_gate"
SIM_MONTHS = "24"
SIM_SEED = "777"
CONTROL_SEEDS = [11, 23, 47, 61, 89, 101, 131, 149, 173, 197, 211, 233]
CONTROL_MONTHS = 3  # try the top-N multi-lease-end months

THROWAWAY_INDEXES = [
    ("simulation.Lease", "IX_tmp187_lease_enddate",
     "(RunID, LeaseEndDate, LeaseStatus) INCLUDE (RenewalDecision)"),
    ("simulation.PropertyMarket", "IX_tmp187_market_status",
     "(RunID, MarketStatus, ExpirationDate)"),
    ("simulation.Vacancy", "IX_tmp187_vacancy_open",
     "(RunID, VacancyEndDate)"),
]


def _sim(env: str, seed: str) -> int:
    r = subprocess.run(
        [sys.executable, os.path.join(APP_SRC, "simulation.py"),
         "--env", env, "--projection-id", "206",
         "--months", SIM_MONTHS, "--seed", seed],
        capture_output=True, text=True)
    return r.returncode


def _build_env() -> bool:
    r = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "environmentscripts", "migration_manager.py"),
         "--envname", GATE_ENV],
        capture_output=True, text=True)
    return r.returncode == 0


def _stream(db, run_id: int):
    """Normalized event stream: everything except RunID and wall-clock LoggedAt.
    Two run-identity leaks are masked so cross-run comparison sees content:
    - Metadata prose referencing its own run number ('... for run 2') -> 'run N';
    - EntityID for run-singleton entity types (RUN, INFLATION — their entity IS
      the run) -> 'R'. Other entity types keep EntityID untouched: per-run
      numbering restarts at 1, so same-seed runs produce identical ids, and
      masking e.g. LEASE ids that merely equal the run number would hide real
      divergences."""
    import re as _re
    rows = db.execute_query(
        """
        SELECT MonthIndex, CAST(EffectiveDate AS DATE), EventType, EntityType,
               EntityID, ActionType, Amount, Metadata
        FROM simulation.Event WHERE RunID = ? ORDER BY EventID
        """, (run_id,))
    pat = _re.compile(r"\b[Rr]un\s+%d\b" % run_id)
    out = []
    for r in rows:
        entity_id = r[4]
        if r[3] in ("RUN", "INFLATION") and entity_id == run_id:
            entity_id = "R"
        meta = pat.sub("run N", r[7]) if r[7] else r[7]
        out.append((r[0], r[1], r[2], r[3], entity_id, r[5], r[6], meta))
    return out


def _first_diff(s1, s2):
    for i, (a, b) in enumerate(zip(s1, s2)):
        if a != b:
            return i, a, b
    if len(s1) != len(s2):
        return min(len(s1), len(s2)), "len=%d" % len(s1), "len=%d" % len(s2)
    return None


def t1_negative_control(db) -> bool:
    """Reversed ORDER BY on the pre-roll must change decisions (gate goes RED
    on the defect). Runs against the gate env's RunID 1."""
    from lease_renewal_manager import LeaseRenewalManager
    from event_logger import EventLogger
    from configuration_loader import ConfigurationLoader
    from retention_model import RetentionModel
    import random as _random

    run_row = db.execute_query(
        "SELECT RunID, ProjectionID FROM simulation.Run ORDER BY RunID OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY")
    run_id, proj_id = int(run_row[0][0]), int(run_row[0][1])

    # the months with the most leases ending (>=2 needed for an order effect)
    months = db.execute_query(
        f"""
        SELECT YEAR(LeaseEndDate), MONTH(LeaseEndDate), COUNT(*)
        FROM simulation.Lease WHERE RunID = ?
        GROUP BY YEAR(LeaseEndDate), MONTH(LeaseEndDate)
        HAVING COUNT(*) >= 2
        ORDER BY COUNT(*) DESC, YEAR(LeaseEndDate), MONTH(LeaseEndDate)
        OFFSET 0 ROWS FETCH FIRST {CONTROL_MONTHS} ROWS ONLY
        """, (run_id,))
    if not months:
        print(f"  T1 negative control: no multi-lease-end month in the gate run [{FAIL}]")
        return False

    config = ConfigurationLoader(db).load_projection(proj_id)

    def build_manager(seed):
        el = EventLogger(db)
        el.set_run_id(run_id)
        mgr = LeaseRenewalManager(db, el, _random.Random(seed + 1), run_seed=seed)
        # mirror simulation.py's config wiring (thresholds live on the manager)
        mgr._early_break_prob = config.lease_early_break_prob_monthly
        mgr._renewal_rate = config.lease_renewal_rate_pct
        mgr._landlord_nonrenewal_prob = config.lease_landlord_nonrenewal_prob_pct
        mgr._late_month_threshold = config.lease_late_month_threshold
        mgr.retention_model = RetentionModel(
            db,
            base_exit=1.0 - config.lease_renewal_rate_pct / 100.0,
            beta=config.ret_discount_sensitivity_beta,
            gamma=config.ret_scarcity_sensitivity_gamma,
            floor_exit=config.ret_floor_exit_annual,
            vac_ref=config.ret_vacancy_ref_pct,
            burden_ceiling=config.ret_burden_ceiling_pct,
            burden_floor=config.ret_burden_floor_pct,
            regional_vacancy_rate=config.ret_mover_regional_vacancy_pct,
            form_is_logistic=config.ret_form_is_logistic,
        )
        return mgr

    # the pre-roll's imposed key (post-#187 STOP re-key) and its forced reversal
    KEY = "ORDER BY u.PropertyID, u.UnitID"
    KEY_REV = "ORDER BY u.PropertyID DESC, u.UnitID DESC"

    ok = False
    detail = ""
    tried = []
    for y, m, n in [(int(r[0]), int(r[1]), int(r[2])) for r in months]:
        roll_date = date(y, m, 1)
        affected = [int(r[0]) for r in db.execute_query(
            """
            SELECT LeaseID FROM simulation.Lease
            WHERE RunID = ? AND YEAR(LeaseEndDate) = ? AND MONTH(LeaseEndDate) = ?
            """, (run_id, y, m))]
        in_list = ",".join(str(l) for l in affected)

        def snapshot():
            return {int(r[0]): (r[1], r[2]) for r in db.execute_query(
                f"SELECT LeaseID, RenewalDecision, RenewalDecidedDate "
                f"FROM simulation.Lease WHERE RunID = ? AND LeaseID IN ({in_list})",
                (run_id,))}

        def reset(saved):
            for lid, (dec, dd) in saved.items():
                db.execute_non_query(
                    "UPDATE simulation.Lease SET RenewalDecision = ?, RenewalDecidedDate = ? "
                    "WHERE RunID = ? AND LeaseID = ?", (dec, dd, run_id, lid))

        def clear():
            db.execute_non_query(
                f"UPDATE simulation.Lease SET RenewalDecision = NULL, RenewalDecidedDate = NULL "
                f"WHERE RunID = ? AND LeaseID IN ({in_list})", (run_id,))

        def decisions():
            return {int(r[0]): r[1] for r in db.execute_query(
                f"SELECT LeaseID, RenewalDecision FROM simulation.Lease "
                f"WHERE RunID = ? AND LeaseID IN ({in_list})", (run_id,))}

        saved = snapshot()
        mixes = []
        try:
            for seed in CONTROL_SEEDS:
                clear()
                build_manager(seed)._pre_roll_renewal_decisions(run_id, roll_date)
                asc = decisions()

                clear()
                mgr = build_manager(seed)
                orig = mgr.db.execute_query

                def reversed_query(sql, params=None, _orig=orig):
                    if "RenewalDecision IS NULL" in sql and KEY in sql:
                        sql = sql.replace(KEY, KEY_REV)
                    return _orig(sql, params) if params is not None else _orig(sql)

                mgr.db.execute_query = reversed_query
                try:
                    mgr._pre_roll_renewal_decisions(run_id, roll_date)
                finally:
                    mgr.db.execute_query = orig
                desc = decisions()

                mixes.append(sorted(asc.values(), key=str))
                if asc != desc:
                    diffs = [l for l in asc if asc[l] != desc[l]]
                    detail = (f"{y}-{m:02d} ({n} leases), seed {seed}: "
                              f"{len(diffs)} decision(s) flipped on reversal (leases {diffs})")
                    ok = True
                    break
        finally:
            reset(saved)
        tried.append(f"{y}-{m:02d}(n={n}, mixes={mixes[:2]})")
        if ok:
            break

    if not ok:
        detail = (f"no flip across {len(CONTROL_SEEDS)} seeds x {len(tried)} months "
                  f"— control cannot demonstrate the defect; tried: {'; '.join(tried)}")

    print(f"  T1 negative control (reversed ORDER BY must change outcomes): "
          f"{detail} [{PASS if ok else FAIL}]")
    return ok


def t2_double_run(db) -> tuple:
    rc2 = _sim(GATE_ENV, SIM_SEED)
    rc3 = _sim(GATE_ENV, SIM_SEED)
    if rc2 != 0 or rc3 != 0:
        print(f"  T2 double run: sim rc={rc2}/{rc3} [{FAIL}]")
        return False, None
    runs = [int(r[0]) for r in db.execute_query(
        "SELECT RunID FROM simulation.Run ORDER BY RunID")]
    r2, r3 = runs[-2], runs[-1]
    s2, s3 = _stream(db, r2), _stream(db, r3)
    diff = _first_diff(s2, s3)
    ok = diff is None and len(s2) > 0
    print(f"  T2 same-plan double run (seed {SIM_SEED}, {SIM_MONTHS}mo x2): "
          f"{len(s2)} events, {'identical' if ok else f'FIRST DIFF at {diff[0]}: {diff[1]} vs {diff[2]}'} "
          f"[{PASS if ok else FAIL}]")
    return ok, (r2, s2)


def t3_perturbed_plan(db, reference) -> bool:
    if reference is None:
        print(f"  T3 perturbed plan: skipped (T2 failed) [{FAIL}]")
        return False
    _, ref_stream = reference
    created = []
    try:
        for tbl, name, cols in THROWAWAY_INDEXES:
            # NONCLUSTERED is T-SQL-only; PG's plain CREATE INDEX is the same
            # thing (all PG indexes are secondary structures)
            nonclustered = "" if is_pg(db) else "NONCLUSTERED "
            db.execute_non_query(f"CREATE {nonclustered}INDEX {name} ON {tbl} {cols}")
            created.append((tbl, name))
        rc = _sim(GATE_ENV, SIM_SEED)
        if rc != 0:
            print(f"  T3 perturbed plan: sim rc={rc} [{FAIL}]")
            return False
        run4 = int(db.execute_query(
            "SELECT MAX(RunID) FROM simulation.Run")[0][0])
        s4 = _stream(db, run4)
        diff = _first_diff(ref_stream, s4)
        ok = diff is None
        if is_pg(db):
            # planner instrumentation is engine-specific; the determinism
            # assertion itself (the stream diff) runs fully on PG
            bite = f" (usage instrumentation is SQL Server-specific; T1 carries the proof on PG)"
        else:
            used = db.execute_query(
                """
                SELECT SUM(COALESCE(u.user_seeks,0) + COALESCE(u.user_scans,0))
                FROM sys.indexes i
                LEFT JOIN sys.dm_db_index_usage_stats u
                  ON u.object_id = i.object_id AND u.index_id = i.index_id
                 AND u.database_id = DB_ID()
                WHERE i.name LIKE 'IX_tmp187%'
                """)[0][0] or 0
            bite = "" if used > 0 else f" ({WARN}: throwaway indexes unused — perturbation didn't bite; T1 carries the proof)"
        print(f"  T3 perturbed-plan run: {'identical to T2' if ok else f'FIRST DIFF at {diff[0]}: {diff[1]} vs {diff[2]}'}"
              f"{bite} [{PASS if ok else FAIL}]")
        return ok
    finally:
        for tbl, name in created:
            if is_pg(db):
                schema = tbl.split(".", 1)[0]
                db.execute_non_query(f"DROP INDEX {schema}.{name}")
            else:
                db.execute_non_query(f"DROP INDEX {name} ON {tbl}")


def t4_eviction_batch_order(db) -> bool:
    """A4 pin: even at 10x payment-failure stress no run produces a same-day
    multi-eviction batch (V1b: 30 evictions, 0 collisions), so the A4 ORDER BY
    cannot be exercised end-to-end. This gate pins it directly: two synthetic
    same-day EVICTION terminations on real Active leases must come back from
    check_pending_executions in ascending LeaseID order (and both must come
    back). Synthetic rows deleted in finally."""
    from eviction_manager import EvictionManager
    from fund_manager import FundManager
    from event_logger import EventLogger
    from datetime import date as _date

    run_row = db.execute_query(
        "SELECT RunID FROM simulation.Run ORDER BY RunID OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY")
    run_id = int(run_row[0][0])
    leases = [int(r[0]) for r in db.execute_query(
        """
        SELECT l.LeaseID FROM simulation.Lease l
        WHERE l.RunID = ? AND l.LeaseStatus = 'Active'
          AND NOT EXISTS (SELECT 1 FROM simulation.LeaseTermination lt
                          WHERE lt.RunID = l.RunID AND lt.LeaseID = l.LeaseID)
        ORDER BY l.LeaseID DESC
        OFFSET 0 ROWS FETCH FIRST 2 ROWS ONLY
        """, (run_id,))]
    if len(leases) < 2:
        print(f"  T4 eviction batch order: <2 eligible Active leases in the gate run [{FAIL}]")
        return False
    exec_date = _date(2030, 6, 15)  # any date; the query matches on equality
    ok = False
    try:
        for lid in leases:
            db.execute_non_query(
                """
                INSERT INTO simulation.LeaseTermination
                    (RunID, LeaseID, TerminationType, TerminationDate,
                     EvictionExecutionDate, ArrearsAtExit)
                VALUES (?, ?, 'EVICTION', ?, ?, 0)
                """, (run_id, lid, exec_date, exec_date))
        el = EventLogger(db)
        el.set_run_id(run_id)
        em = EvictionManager(db, FundManager(db, el), el)
        rows = em.check_pending_executions(run_id, exec_date)
        got = [int(r["lease_id"]) for r in rows]
        ok = got == sorted(leases)
        print(f"  T4 eviction batch order (A4 pin, synthetic 2-row batch): "
              f"returned {got}, expect ascending {sorted(leases)} [{PASS if ok else FAIL}]")
    finally:
        db.execute_non_query(
            f"DELETE FROM simulation.LeaseTermination WHERE RunID = ? AND "
            f"LeaseID IN ({','.join(str(l) for l in leases)}) AND TerminationType = 'EVICTION' "
            f"AND EvictionExecutionDate = ?", (run_id, exec_date))
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)  # suite env; unused except convention
    parser.add_argument("--run-id", default=None)  # harness parity; unused
    parser.add_argument("--assert", dest="assert_mode", action="store_true")
    args = parser.parse_args()

    print(f"#187 determinism gates — own env {GATE_ENV} (suite env {args.env} untouched)")
    print("=" * 78)

    if not _build_env():
        print(f"  env build FAILED [{FAIL}]")
        sys.exit(1)
    if _sim(GATE_ENV, SIM_SEED) != 0:
        print(f"  seed run FAILED [{FAIL}]")
        sys.exit(1)

    from database_manager import DatabaseManager
    db = DatabaseManager(ENV_BASE + GATE_ENV + os.sep + "db_config.json")

    r1 = t1_negative_control(db)
    r2, reference = t2_double_run(db)
    r3 = t3_perturbed_plan(db, reference)
    r4 = t4_eviction_batch_order(db)

    print("=" * 78)
    passed = sum(1 for r in (r1, r2, r3, r4) if r)
    print(f"Result: {passed}/4 gates passed")
    if args.assert_mode and passed != 4:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
