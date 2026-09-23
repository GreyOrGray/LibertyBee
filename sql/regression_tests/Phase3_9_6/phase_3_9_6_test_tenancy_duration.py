"""
Phase 3.9.6 — Tenancy Duration & Exit Path Correctness: Regression Tests.

Outcome-level black-box tests for the two BA-approved fixes:

  3.9.6.2 — _execute_renewal now clears RenewalDecision + RenewalDecidedDate
            on extension, so the next pre-roll cycle picks the lease up.
  3.9.6.3 — _check_early_breaks is gated on current_date.day == 1, so the
            documented 0.25% MONTHLY hazard fires monthly, not daily.

Eight tests per Kate's BA review (phase_3_9_6_ba_review_and_fix_authorization.md §7):

  T1  Renewal clears decision state — no LEASE_RENEWED ledger entry leaves a
      stale RenewalDecidedDate older than the renewal itself.
  T2  Second-cycle pre-roll fires — renewed leases that survive >= 14 months
      receive a fresh RenewalDecidedDate after the renewal.
  T3  Lease-end voluntary exits can occur after month 12 — observed at 24+ mo
      in a normal 240-month run.
  T4  No same-cycle double-roll — within a single 12-month lease cycle, no
      lease has two distinct RenewalDecidedDate values.
  T5  Phase 3.9.5 final-month exit redemption remains intact — every clean
      eligible exit (early-break or lease-end) that meets all five conditions
      still produces a FINAL_MONTH_EXIT REDEMPTION.
  T6  Early-break cadence invariant — ALL early-break TerminationDates have
      day == 1 (mechanical proof of monthly cadence, per Kate's §7 precision
      requirement).
  T7  No duplicate termination rows — (RunID, LeaseID) is unique in
      LeaseTermination.
  T8  Tenure distribution sanity check — lease-end voluntary exits are not
      all concentrated at month 12.

Tests SKIP gracefully when the run doesn't exercise a pathway (e.g. no
renewals fired in a very short sim).

Usage:
    python sql/regression_tests/Phase3_9_6/phase_3_9_6_test_tenancy_duration.py --env <db> [--run-id N] [--assert]

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


# ---------------------------------------------------------------------------
# T1 — Renewal clears decision state
# ---------------------------------------------------------------------------

def t1_renewal_clears_decision_state(db, run_id: int) -> bool:
    """For every LEASE_RENEWED ledger entry, the lease's stored
    RenewalDecidedDate must not be older than the renewal TxnDate. (After
    fix, _execute_renewal clears it; subsequent pre-rolls set it fresh.)"""
    violations = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.LeaseTerminationLedger ltl
        JOIN simulation.Lease l ON l.RunID = ltl.RunID AND l.LeaseID = ltl.LeaseID
        WHERE ltl.RunID = ? AND ltl.TxnType = 'LEASE_RENEWED'
          AND l.RenewalDecidedDate IS NOT NULL
          AND l.RenewalDecidedDate < ltl.TxnDate
        """,
        (run_id,),
    )
    total = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.LeaseTerminationLedger
        WHERE RunID = ? AND TxnType = 'LEASE_RENEWED'
        """,
        (run_id,),
    )
    n_renewals = total[0][0] if total else 0
    n_viol = violations[0][0] if violations else 0
    if n_renewals == 0:
        print(f"  T1 Renewal clears decision state: no LEASE_RENEWED entries [{SKIP}]")
        return True
    ok = n_viol == 0
    print(f"  T1 Renewal clears decision state: {n_renewals} renewals, "
          f"{n_viol} with stale RenewalDecidedDate (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T2 — Second-cycle pre-roll fires
# ---------------------------------------------------------------------------

def t2_second_cycle_preroll_fires(db, run_id: int) -> bool:
    """A lease that renewed at month 12 and survives long enough should
    receive a fresh RenewalDecidedDate after the renewal. We look for
    long-tenured leases (LeaseEndDate >= LeaseStartDate + 14 months) and
    verify the RenewalDecidedDate (if set) is at or after the second
    pre-roll cycle's earliest possible date."""
    rows = db.execute_query(
        f"""
        SELECT COUNT(*),
               SUM(CASE WHEN l.RenewalDecidedDate IS NULL
                         OR l.RenewalDecidedDate >= {add_months_sql(db, 12, 'l.LeaseStartDate')}
                        THEN 1 ELSE 0 END) AS fresh_or_null
        FROM simulation.Lease l
        WHERE l.RunID = ?
          AND {month_diff_sql(db, 'l.LeaseStartDate', 'l.LeaseEndDate')} >= 14
        """,
        (run_id,),
    )
    if not rows or rows[0][0] is None or rows[0][0] == 0:
        print(f"  T2 Second-cycle pre-roll fires: no long-tenured renewed "
              f"leases in this run [{SKIP}]")
        return True
    n_total, n_fresh = rows[0][0] or 0, rows[0][1] or 0
    ok = (n_total == n_fresh)
    print(f"  T2 Second-cycle pre-roll fires: {n_fresh}/{n_total} "
          f"long-tenured renewed leases have fresh/NULL RenewalDecidedDate "
          f"[{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T3 — Lease-end voluntary exits can occur after month 12
# ---------------------------------------------------------------------------

def t3_lease_end_exits_after_month_12(db, run_id: int) -> bool:
    """Voluntary lease-end exits should appear at 24, 36, 48+ months once
    the renewal-state bug is fixed."""
    rows = db.execute_query(
        f"""
        SELECT
          SUM(CASE WHEN {month_diff_sql(db, 'l.LeaseStartDate', 't.TerminationDate')} > 12
                   THEN 1 ELSE 0 END) AS after12,
          COUNT(*) AS total
        FROM simulation.LeaseTermination t
        JOIN simulation.Lease l ON l.RunID = t.RunID AND l.LeaseID = t.LeaseID
        WHERE t.RunID = ? AND t.TerminationReason = 'Tenant voluntary exit at lease end'
        """,
        (run_id,),
    )
    n_after = rows[0][0] or 0 if rows else 0
    n_total = rows[0][1] or 0 if rows else 0
    if n_total == 0:
        print(f"  T3 Lease-end voluntary exits after month 12: no lease-end "
              f"voluntary exits in this run [{SKIP}]")
        return True
    ok = n_after > 0
    print(f"  T3 Lease-end voluntary exits after month 12: {n_after}/{n_total} "
          f"exits after month 12 (expected > 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T4 — No same-cycle double-roll
# ---------------------------------------------------------------------------

def t4_no_same_cycle_double_roll(db, run_id: int) -> bool:
    """The pre-roll guard `WHERE RenewalDecision IS NULL` must still prevent
    a lease from being re-rolled within the same lease cycle. We can't
    directly observe pre-roll attempts, but at any sim instant a lease has
    exactly one RenewalDecidedDate (the most recent pre-roll) — and that
    date must fall within the lease's current cycle window
    [LeaseEndDate - 12 months, LeaseEndDate]."""
    violations = db.execute_query(
        f"""
        SELECT COUNT(*) FROM simulation.Lease
        WHERE RunID = ?
          AND RenewalDecidedDate IS NOT NULL
          AND (RenewalDecidedDate < {add_months_sql(db, -12, 'LeaseEndDate')}
               OR RenewalDecidedDate > LeaseEndDate)
        """,
        (run_id,),
    )
    n_viol = violations[0][0] if violations else 0
    ok = n_viol == 0
    print(f"  T4 No same-cycle double-roll: {n_viol} leases with "
          f"RenewalDecidedDate outside current cycle (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T5 — Phase 3.9.5 final-month exit redemption remains intact
# ---------------------------------------------------------------------------

def t5_phase_3_9_5_exit_redemption_intact(db, run_id: int) -> bool:
    """Both lease-end and early-break clean exits with an eligible balance
    must still produce FINAL_MONTH_EXIT redemptions. Regression cover for
    the 3.9.5 fix.

    Phase 3.10.2 L1 fix made landlord-non-renewal a live exit pathway that
    also produces FME redemptions, so 'lease-end' must include LNR exits.
    Per the 3.9.5 wiring at lease_renewal_manager._execute_landlord_nonrenewal,
    LNR exits fire process_final_month_exit_redemption identically to
    voluntary lease-end exits.
    """
    n_fme = db.execute_query(
        """
        SELECT COUNT(*) FROM simulation.TenantCreditLedger
        WHERE RunID = ? AND TransactionType = 'REDEMPTION'
          AND Notes LIKE '%FINAL_MONTH_EXIT%'
        """,
        (run_id,),
    )
    n_early = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.LeaseTermination t
          ON t.RunID = tcl.RunID AND t.LeaseID = tcl.RelatedLeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND t.TerminationReason LIKE '%early break%'
        """,
        (run_id,),
    )
    n_leaseend = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.TenantCreditLedger tcl
        JOIN simulation.LeaseTermination t
          ON t.RunID = tcl.RunID AND t.LeaseID = tcl.RelatedLeaseID
        WHERE tcl.RunID = ? AND tcl.TransactionType = 'REDEMPTION'
          AND tcl.Notes LIKE '%FINAL_MONTH_EXIT%'
          AND (t.TerminationReason = 'Tenant voluntary exit at lease end'
               OR t.TerminationReason LIKE '%landlord%non%renew%')
        """,
        (run_id,),
    )
    total = n_fme[0][0] if n_fme else 0
    eb = n_early[0][0] if n_early else 0
    le = n_leaseend[0][0] if n_leaseend else 0
    if total == 0:
        print(f"  T5 Phase 3.9.5 exit redemption intact: no FINAL_MONTH_EXIT "
              f"redemptions in this run [{SKIP}]")
        return True
    ok = (eb + le) == total
    print(f"  T5 Phase 3.9.5 exit redemption intact: {total} FINAL_MONTH_EXIT, "
          f"{eb} early-break + {le} lease-end (incl LNR) = {eb + le} "
          f"(expected {total}) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T6 — Early-break cadence invariant (precision)
# ---------------------------------------------------------------------------

def t6_early_break_cadence_invariant(db, run_id: int) -> bool:
    """Kate's precision requirement (§7): prove the monthly cadence directly,
    not just observe lower totals. Under the day-1 gate, every early-break
    TerminationDate must have day == 1. A lone non-day-1 early-break would
    indicate the cadence gate is leaking."""
    rows = db.execute_query(
        """
        SELECT
          SUM(CASE WHEN DAY(t.TerminationDate) = 1 THEN 1 ELSE 0 END) AS day1,
          COUNT(*) AS total
        FROM simulation.LeaseTermination t
        WHERE t.RunID = ? AND t.TerminationReason LIKE '%early break%'
        """,
        (run_id,),
    )
    if not rows or rows[0][1] is None or rows[0][1] == 0:
        print(f"  T6 Early-break cadence invariant: no early-break "
              f"terminations in this run [{SKIP}]")
        return True
    day1 = rows[0][0] or 0
    total = rows[0][1] or 0
    ok = (day1 == total)
    print(f"  T6 Early-break cadence invariant (day == 1 for ALL early "
          f"breaks): {day1}/{total} on day 1 (expected equal) "
          f"[{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T7 — No duplicate termination rows
# ---------------------------------------------------------------------------

def t7_no_duplicate_termination_rows(db, run_id: int) -> bool:
    """(RunID, LeaseID) must be unique in LeaseTermination — a lease can
    only terminate once."""
    rows = db.execute_query(
        """
        SELECT COUNT(*) FROM (
          SELECT LeaseID, COUNT(*) AS c
          FROM simulation.LeaseTermination
          WHERE RunID = ?
          GROUP BY LeaseID
          HAVING COUNT(*) > 1
        ) x
        """,
        (run_id,),
    )
    n_dup = rows[0][0] if rows else 0
    ok = n_dup == 0
    print(f"  T7 No duplicate termination rows: {n_dup} LeaseIDs with > 1 "
          f"termination (expected 0) [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# T8 — Tenure distribution sanity check
# ---------------------------------------------------------------------------

_T8_MIN_SAMPLE = 20      # a distribution check needs volume to be meaningful
_T8_DEGENERACY_PCT = 95.0  # ceiling: ~100% at month 12 == renewal never fires (the bug)


def t8_tenure_distribution_sanity(db, run_id: int) -> bool:
    """Lease-end voluntary exits should not be GROSSLY concentrated at the first
    lease-end (month 12) — that signals "renewal never fires" (everyone exits at their
    first lease-end). Scenario-agnostic (#106): the exact month-12 share is noise- and
    horizon-dominated (it ranges legitimately from ~35% at the default renewal rate to
    ~90% under a low-renewal stress scenario, and is small-N noisy when few voluntary
    lease-end exits occur), so this is a soft DEGENERACY guard, not a renewal-rate check:
      - SKIP when there are too few lease-end voluntary exits to judge a distribution
        (< _T8_MIN_SAMPLE) — avoids small-N false signals (e.g. high-eviction stress,
        where most exits are evictions, not lease-end);
      - otherwise require month-12 share < _T8_DEGENERACY_PCT (a renewal-never-fires bug
        drives it to ~100%; legitimate low-renewal bunching tops out near 90%).
    The PRECISE "renewals actually fire" assertion is T3 (lease-end exits after month 12 > 0).

    (The previous fixed "< 90%" threshold false-failed the low-renewal stress projection
    222, where ~90% at month 12 is correct behaviour.)"""
    rows = db.execute_query(
        f"""
        SELECT
          SUM(CASE WHEN {month_diff_sql(db, 'l.LeaseStartDate', 't.TerminationDate')} = 12
                   THEN 1 ELSE 0 END) AS at12,
          COUNT(*) AS total
        FROM simulation.LeaseTermination t
        JOIN simulation.Lease l ON l.RunID = t.RunID AND l.LeaseID = t.LeaseID
        WHERE t.RunID = ? AND t.TerminationReason = 'Tenant voluntary exit at lease end'
        """,
        (run_id,),
    )
    total = (rows[0][1] or 0) if rows else 0
    if total < _T8_MIN_SAMPLE:
        print(f"  T8 Tenure distribution sanity: only {total} lease-end voluntary exits "
              f"(< {_T8_MIN_SAMPLE}) — too few to judge a distribution [{SKIP}]")
        return True
    at12 = rows[0][0] or 0
    pct_at12 = 100.0 * at12 / total
    ok = pct_at12 < _T8_DEGENERACY_PCT
    print(f"  T8 Tenure distribution sanity: {at12}/{total} exits at month 12 "
          f"({pct_at12:.1f}%, expected < {_T8_DEGENERACY_PCT:.0f}% — gross-degeneracy guard) "
          f"[{PASS if ok else FAIL}]")
    return ok


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
    print(f"\n=== Phase 3.9.6 — Tenancy Duration & Exit Path Correctness "
          f"(env={args.env}, RunID={run_id}) ===\n")

    results = [
        t1_renewal_clears_decision_state(db, run_id),
        t2_second_cycle_preroll_fires(db, run_id),
        t3_lease_end_exits_after_month_12(db, run_id),
        t4_no_same_cycle_double_roll(db, run_id),
        t5_phase_3_9_5_exit_redemption_intact(db, run_id),
        t6_early_break_cadence_invariant(db, run_id),
        t7_no_duplicate_termination_rows(db, run_id),
        t8_tenure_distribution_sanity(db, run_id),
    ]
    n_pass = sum(1 for r in results if r)
    print(f"\nResults: {n_pass}/{len(results)} passed\n")

    if args.assert_mode and n_pass < len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
