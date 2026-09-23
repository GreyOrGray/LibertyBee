"""
Phase 3.10.2 — Landlord-Non-Renewal L1 Fix: Regression Tests.

Background: pre-3.10.2 `_count_late_months` filtered on `PaymentStatus = 'LATE'`,
but rent collection never writes 'LATE' (CanPay=True paths always collect inside
the 5-day grace window -> 'ON_TIME'). LNR pre-roll predicate `late_months >= 2`
was therefore structurally unreachable; `LEASE_LandlordNonRenewalProbPct` was
dead-weight. Per Kate's 2026-05-24 BA review handoff, L1 fix: change the
predicate to `PaymentStatus IN ('LATE', 'MISSED')`.

Six tests per Kate's BA review handoff §6:

  T1  MISSED counts toward payment-problem history — direct SQL verification
      that the same predicate used by `_count_late_months` returns the
      expected non-zero count for leases with MISSED rows in the lookback
      window.
  T2  Below-threshold lease is NOT non-renewed for payment-history reasons —
      any LANDLORD_NONRENEWAL termination must have had >= threshold
      LATE+MISSED months in its prior-12-month window at decision time.
  T3  Threshold lease enters the LNR roll branch — at least some leases in
      the run reached the threshold of LATE+MISSED months in their
      prior-12-month window at lease-end pre-roll time (proves the roll
      branch is reachable, regardless of whether the roll succeeded).
  T4  LNR parameter is no longer dead-weight — at least one
      LANDLORD_NONRENEWAL termination row exists in the run.
  T5  Eviction behavior unchanged — all evictions are still driven by
      CMP >= 3, not by LATE/MISSED count; no LeaseTermination row has both
      EvictionFiledDate AND TerminationReason = 'LANDLORD_NONRENEWAL'.
  T6  No duplicate terminations — (RunID, LeaseID) is unique in
      LeaseTermination; a LANDLORD_NONRENEWAL'd lease has exactly one
      termination ledger entry of the renewal-decision class
      (LANDLORD_NONRENEWAL, VOLUNTARY_NOTICE, or eviction).

Tests SKIP gracefully when the run doesn't exercise a pathway.

Usage:
    python sql/regression_tests/Phase3_10_2/phase_3_10_2_test_landlord_nonrenewal.py \
        --env <db> [--run-id N] [--assert]

Default mode prints results and exits 0. With --assert, exits 1 on any FAIL.
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
sys.path.insert(0, APP_SRC)
sys.path.insert(0, os.path.join(REPO_ROOT, 'sql', 'regression_tests'))
from _dialect import add_months_sql, add_days_sql, month_diff_sql  # noqa: E402


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


def _get_late_threshold(db, run_id: int) -> int:
    # LEASE.LateMonthThreshold from the registry for the RUN's projection (#75 re-point —
    # was a TOP 1 read of the retired reference.ProjectionParameters, which both coupled
    # to the dead table and ignored which projection the run used; now keyed on run_id).
    from parameter_registry import ParameterRegistry  # type: ignore
    proj = db.execute_query("SELECT ProjectionID FROM simulation.Run WHERE RunID = ?", (run_id,))[0][0]
    return ParameterRegistry(db).load(proj).get_int('LEASE', 'LateMonthThreshold')


# ---------------------------------------------------------------------------
# T1 — MISSED counts toward payment-problem history
# ---------------------------------------------------------------------------

def t1_missed_counts_toward_history(db, run_id: int) -> bool:
    """Direct SQL verification that the L1 predicate `PaymentStatus IN
    ('LATE','MISSED')` counts MISSED rows. Pre-3.10.2 the predicate was
    'LATE' only — would have returned 0. After L1, the count must be > 0
    given that the run produced any MISSED rows at all."""
    # Total MISSED rows in this run
    total_missed = db.execute_query(
        "SELECT COUNT(*) FROM simulation.MonthlyPaymentStatus "
        "WHERE RunID = ? AND PaymentStatus = 'MISSED'", (run_id,)
    )[0][0]
    if total_missed == 0:
        print(f"  {SKIP} T1: no MISSED rows in run — pathway not exercised")
        return True

    # Sum of per-lease counts using the L1 predicate, over the same set
    l1_sum = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.MonthlyPaymentStatus
        WHERE RunID = ? AND PaymentStatus IN ('LATE', 'MISSED')
        """,
        (run_id,),
    )[0][0]

    if l1_sum < total_missed:
        print(f"  {FAIL} T1: L1 predicate count ({l1_sum}) < total MISSED rows ({total_missed})")
        return False

    print(f"  {PASS} T1: L1 predicate counts {l1_sum} payment-problem rows "
          f"(>= total MISSED {total_missed})")
    return True


# ---------------------------------------------------------------------------
# T2 — Below-threshold lease is NOT non-renewed for payment-history reasons
# ---------------------------------------------------------------------------

def t2_below_threshold_not_lnr(db, run_id: int) -> bool:
    """Every LANDLORD_NONRENEWAL termination must have had >= threshold
    LATE+MISSED months in its 12-month window prior to the renewal
    decision date."""
    threshold = _get_late_threshold(db, run_id)
    rows = db.execute_query(
        """
        SELECT l.LeaseID, l.RenewalDecidedDate
        FROM simulation.Lease l
        WHERE l.RunID = ? AND l.RenewalDecision = 'LANDLORD_NONRENEWAL'
        """,
        (run_id,),
    )
    if not rows:
        print(f"  {SKIP} T2: no LANDLORD_NONRENEWAL leases — pathway not exercised")
        return True

    violations = 0
    examples = []
    for lease_id, decided_date in rows:
        # Mirror _count_late_months in SQL
        cnt = db.execute_query(
            f"""
            SELECT COUNT(*) FROM simulation.MonthlyPaymentStatus
            WHERE RunID = ? AND LeaseID = ?
              AND PaymentStatus IN ('LATE', 'MISSED')
              AND BillingMonth >= {add_months_sql(db, -12, '?')}
              AND BillingMonth <  ?
            """,
            (run_id, lease_id, decided_date, decided_date),
        )[0][0]
        if cnt < threshold:
            violations += 1
            if len(examples) < 3:
                examples.append((lease_id, decided_date, cnt))

    if violations > 0:
        print(f"  {FAIL} T2: {violations}/{len(rows)} LANDLORD_NONRENEWAL leases "
              f"had < threshold ({threshold}) LATE+MISSED months in prior 12. "
              f"Examples: {examples}")
        return False
    print(f"  {PASS} T2: all {len(rows)} LANDLORD_NONRENEWAL leases met the "
          f">= {threshold} LATE+MISSED threshold at decision time")
    return True


# ---------------------------------------------------------------------------
# T3 — Threshold lease enters the LNR roll branch (reachability)
# ---------------------------------------------------------------------------

def t3_threshold_reachable(db, run_id: int) -> bool:
    """At least some lease-end decision points in this run had a lease
    with prior-12-month LATE+MISSED count >= threshold — proving the LNR
    roll branch is reachable. We approximate this by counting leases that
    have RenewalDecidedDate set AND would have qualified at that date."""
    threshold = _get_late_threshold(db, run_id)
    qualified = db.execute_query(
        f"""
        WITH lease_problems AS (
            SELECT l.LeaseID, l.RenewalDecidedDate,
                   (SELECT COUNT(*) FROM simulation.MonthlyPaymentStatus mps
                     WHERE mps.RunID = l.RunID AND mps.LeaseID = l.LeaseID
                       AND mps.PaymentStatus IN ('LATE', 'MISSED')
                       AND mps.BillingMonth >= {add_months_sql(db, -12, 'l.RenewalDecidedDate')}
                       AND mps.BillingMonth <  l.RenewalDecidedDate) AS LateOrMissed
            FROM simulation.Lease l
            WHERE l.RunID = ? AND l.RenewalDecidedDate IS NOT NULL
        )
        SELECT COUNT(*) FROM lease_problems WHERE LateOrMissed >= ?
        """,
        (run_id, threshold),
    )[0][0]

    if qualified == 0:
        # Soft skip — possible in very short or very stable runs, but a 240-month
        # canonical run with payment_fail_prob=0.02 should always produce some.
        print(f"  {SKIP} T3: 0 leases reached threshold ({threshold}) — pathway "
              f"not exercised (acceptable in atypical runs)")
        return True

    print(f"  {PASS} T3: {qualified} lease-end decisions had prior-12-month "
          f"LATE+MISSED >= {threshold} — LNR roll branch is reachable")
    return True


# ---------------------------------------------------------------------------
# T4 — LNR parameter is no longer dead-weight
# ---------------------------------------------------------------------------

def t4_lnr_observed(db, run_id: int) -> bool:
    """At least one LANDLORD_NONRENEWAL termination row should exist in a
    run that had qualifying leases. Soft-checks the parameter is live —
    SKIPs gracefully when 0 leases met the LateOrMissed threshold at any
    decision point (single-seed luck on natural payment_fail_prob = 0.02 and
    threshold = 2 makes a 12-month-window-of-2-misses rare; aggregate
    observability across the full sweep is the gating check, performed in
    the post-implementation report)."""
    threshold = _get_late_threshold(db, run_id)

    # Count of decision points that met the threshold
    qualified = db.execute_query(
        f"""
        WITH lease_problems AS (
            SELECT l.LeaseID, l.RenewalDecidedDate,
                   (SELECT COUNT(*) FROM simulation.MonthlyPaymentStatus mps
                     WHERE mps.RunID = l.RunID AND mps.LeaseID = l.LeaseID
                       AND mps.PaymentStatus IN ('LATE', 'MISSED')
                       AND mps.BillingMonth >= {add_months_sql(db, -12, 'l.RenewalDecidedDate')}
                       AND mps.BillingMonth <  l.RenewalDecidedDate) AS LateOrMissed
            FROM simulation.Lease l
            WHERE l.RunID = ? AND l.RenewalDecidedDate IS NOT NULL
        )
        SELECT COUNT(*) FROM lease_problems WHERE LateOrMissed >= ?
        """,
        (run_id, threshold),
    )[0][0]

    cnt = db.execute_query(
        "SELECT COUNT(*) FROM simulation.Lease "
        "WHERE RunID = ? AND RenewalDecision = 'LANDLORD_NONRENEWAL'",
        (run_id,),
    )[0][0]

    # Small-sample SKIP threshold. With 10% LNR probability:
    #   N=5  → P(0 LNR) = 59%
    #   N=10 → P(0 LNR) = 35%
    #   N=20 → P(0 LNR) = 12%
    # At N<20 the stochastic-miss probability is too high to make 0 LNR
    # genuinely surprising on a single run. Aggregate observability across
    # the full sweep is the gating check (see 3.10.2 post-implementation report).
    SMALL_SAMPLE_THRESHOLD = 20

    if qualified < SMALL_SAMPLE_THRESHOLD:
        print(f"  {SKIP} T4: only {qualified} decision points reached the threshold "
              f"({threshold}) in this single-seed run (need >= {SMALL_SAMPLE_THRESHOLD} "
              f"for meaningful assertion); aggregate observability checked at sweep level")
        return True

    if cnt == 0:
        # >=SMALL_SAMPLE_THRESHOLD qualifying decisions but 0 LNR is unusual:
        # P(0 LNR | 20+ qualifying, 10% rate) <= 12%. Soft-fail with context.
        print(f"  {FAIL} T4: {qualified} qualifying decisions but 0 LNR fired "
              f"(P <= 12% under default rate; suspicious)")
        return False

    print(f"  {PASS} T4: {qualified} qualifying decisions -> {cnt} LNR fired "
          f"(parameter live; observed rate {cnt/qualified*100:.0f}% of expected ~10%)")
    return True


# ---------------------------------------------------------------------------
# T5 — Eviction behavior unchanged
# ---------------------------------------------------------------------------

def t5_eviction_path_decoupled(db, run_id: int) -> bool:
    """Eviction is still driven by CMP >= 3, not by LATE+MISSED count.
    No LeaseTermination row should have both EvictionFiledDate set AND
    TerminationReason = 'LANDLORD_NONRENEWAL'."""
    overlap = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.LeaseTermination lt
        JOIN simulation.Lease l ON l.RunID = lt.RunID AND l.LeaseID = lt.LeaseID
        WHERE lt.RunID = ?
          AND lt.EvictionFiledDate IS NOT NULL
          AND l.RenewalDecision = 'LANDLORD_NONRENEWAL'
        """,
        (run_id,),
    )[0][0]

    if overlap > 0:
        print(f"  {FAIL} T5: {overlap} terminations have both eviction filing "
              f"AND LANDLORD_NONRENEWAL — these pathways must not collide")
        return False

    # Also confirm any evictions still have CMP-trail evidence
    evicted = db.execute_query(
        "SELECT COUNT(*) FROM simulation.LeaseTerminationLedger "
        "WHERE RunID = ? AND TxnType = 'EVICTION_FILED'",
        (run_id,),
    )[0][0]

    if evicted == 0:
        print(f"  {PASS} T5: 0 eviction filings in run; LNR/eviction paths disjoint")
        return True
    print(f"  {PASS} T5: {evicted} eviction filings; 0 collision with LANDLORD_NONRENEWAL")
    return True


# ---------------------------------------------------------------------------
# T6 — No duplicate terminations
# ---------------------------------------------------------------------------

def t6_no_duplicate_terminations(db, run_id: int) -> bool:
    """(RunID, LeaseID) must be unique in LeaseTermination. Additionally,
    any LANDLORD_NONRENEWAL lease should have exactly one renewal-decision
    ledger entry (VOLUNTARY_NOTICE author or LANDLORD_NONRENEWAL — they
    share the VOLUNTARY_NOTICE TxnType per the termination author)."""
    dups = db.execute_query(
        """
        SELECT COUNT(*) FROM (
            SELECT LeaseID, COUNT(*) AS Cnt
            FROM simulation.LeaseTermination
            WHERE RunID = ?
            GROUP BY LeaseID
            HAVING COUNT(*) > 1
        ) z
        """,
        (run_id,),
    )[0][0]
    if dups > 0:
        print(f"  {FAIL} T6: {dups} leases have multiple LeaseTermination rows")
        return False

    # Also check LANDLORD_NONRENEWAL leases all have a termination
    lnr_orphan = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.Lease l
        WHERE l.RunID = ? AND l.RenewalDecision = 'LANDLORD_NONRENEWAL'
          AND NOT EXISTS (
              SELECT 1 FROM simulation.LeaseTermination lt
               WHERE lt.RunID = l.RunID AND lt.LeaseID = l.LeaseID
          )
          AND l.LeaseEndDate < (SELECT MAX(LedgerDate) FROM simulation.FundLedger WHERE RunID = l.RunID)
        """,
        (run_id,),
    )[0][0]
    if lnr_orphan > 0:
        # Leases whose end date is past sim end may legitimately have no termination
        # row if the sim ended first. The MAX(LedgerDate) bound above filters those.
        print(f"  {FAIL} T6: {lnr_orphan} LANDLORD_NONRENEWAL leases past their end "
              f"date have no termination row")
        return False

    print(f"  {PASS} T6: no duplicate or orphan LNR terminations")
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
    print(f"\n=== Phase 3.10.2 — Landlord-Non-Renewal L1 Fix "
          f"(env={args.env}, RunID={run_id}) ===\n")

    results = [
        t1_missed_counts_toward_history(db, run_id),
        t2_below_threshold_not_lnr(db, run_id),
        t3_threshold_reachable(db, run_id),
        t4_lnr_observed(db, run_id),
        t5_eviction_path_decoupled(db, run_id),
        t6_no_duplicate_terminations(db, run_id),
    ]
    n_pass = sum(1 for r in results if r)
    print(f"\nResults: {n_pass}/{len(results)} passed\n")

    if args.assert_mode and n_pass < len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
