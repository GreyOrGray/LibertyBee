"""
Phase 3.9.2 — CSF Target Rounding Fix Regression Tests.

Covers Kate's required test list R1-R4 from
phase_3_9_2_rounding_fix_handoff.md §6.

  R1  get_csf_target quantizes consistently — raw and pre-quantized
      monthly_opex must produce the same cents-based target.
  R2  Top-up and acquisition gate agree — for every CSF_TOPUP event in the
      canonical run, the resulting CSFBalance must equal the target
      computed by get_csf_target. No off-by-cent gap can exist post-fix.
  R3  No full-year penny-gate silence — over the canonical post-fix run,
      no day exhibits a "penny gap" where 0 < (csf_target - csf_balance)
      <= $0.10. (Real underfunding in dollars is allowed; rounding-induced
      false underfunding in cents is forbidden.)
  R4  Phase 3.9.1 guardrails intact — confirm the rounding fix did not
      weaken any 3.9.1 discipline rule.

Usage:
    python scratch/sql/regression_tests/Phase3_9_2/phase_3_9_2_test_csf_target_rounding.py --env <db> [--run-id N] [--assert]

R1 is a pure unit test and runs regardless of --env. R2-R4 read against the
specified DB and RunID.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from decimal import Decimal
from datetime import date

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

PENNY_THRESHOLD = Decimal("0.10")


def _config_path_for(env: str) -> str:
    return ENV_BASE + env + os.sep + 'db_config.json'


def _resolve_run_id(db, override) -> int:
    if override is not None:
        return int(override)
    row = db.execute_query("SELECT MAX(RunID) FROM simulation.Run")
    if not row or row[0][0] is None:
        raise SystemExit("No runs found in simulation.Run; cannot pick a RunID.")
    return int(row[0][0])


def _new_fund_manager_stub():
    """Build a FundManager without invoking DB-dependent __init__, for unit
    tests of pure methods."""
    from fund_manager import FundManager
    fm = FundManager.__new__(FundManager)
    fm.logger = None
    return fm


def _parse_target_from_topup_metadata(meta: str) -> Decimal | None:
    """Extract csf_target=$X,XXX.XX from a CSF_TOPUP event message."""
    m = re.search(r"csf_target=\$([\d,]+\.\d+)", meta or "")
    if not m:
        return None
    return Decimal(m.group(1).replace(",", ""))


def _parse_monthly_opex_from_topup_metadata(meta: str) -> Decimal | None:
    """Extract monthly_opex=$X,XXX.XX from a CSF_TOPUP event message."""
    m = re.search(r"monthly_opex=\$([\d,]+\.\d+)", meta or "")
    if not m:
        return None
    return Decimal(m.group(1).replace(",", ""))


def _parse_reserve_months_from_topup_metadata(meta: str) -> Decimal | None:
    """Extract reserve_months=X[.XXXX] from a CSF_TOPUP event message.

    #97 (Phase 1.2): reserve months come from the taper curve on the property
    count, so the recompute must use the run's OWN per-event value from the
    metadata — a hardcoded 12 false-fails as soon as the portfolio passes
    N0 properties. The event has carried this field since Phase 3.9.1.
    """
    m = re.search(r"reserve_months=([\d]+(?:\.\d+)?)", meta or "")
    if not m:
        return None
    return Decimal(m.group(1))


# ---------------------------------------------------------------------------
# R1 — unit: get_csf_target quantizes consistently
# ---------------------------------------------------------------------------

def r1_get_csf_target_quantizes_consistently() -> bool:
    """The exact 2043 bug scenario: monthly_opex = $104,625.17083... × 12
    must produce $1,255,502.04 (not $1,255,502.05). Raw and pre-quantized
    input must yield identical output."""
    fm = _new_fund_manager_stub()

    cases = [
        # (label, monthly_opex, reserve_months, expected_target)
        ("2043 bug-trigger value (raw sub-cent)", Decimal("104625.17083"), 12, Decimal("1255502.04")),
        ("2043 bug-trigger value (pre-quantized)", Decimal("104625.17"),   12, Decimal("1255502.04")),
        ("Plain integer dollars",                 Decimal("100000"),        12, Decimal("1200000.00")),
        ("Sub-cent with 3:1 reserve months",      Decimal("104625.171083"), 3,  Decimal("313875.51")),
        ("Edge — extra precision",                Decimal("104625.179999"), 12, Decimal("1255502.16")),
    ]

    all_ok = True
    for label, monthly_opex, reserve_months, expected in cases:
        actual = fm.get_csf_target(monthly_opex, reserve_months)
        ok = actual == expected
        all_ok = all_ok and ok
        status = PASS if ok else FAIL
        print(f"  R1 {label}: get_csf_target(${monthly_opex}, {reserve_months}) = ${actual} (expected ${expected}) [{status}]")

    # Equivalence check: raw and pre-quantized must agree
    raw = fm.get_csf_target(Decimal("104625.17083"), 12)
    pre = fm.get_csf_target(Decimal("104625.17"), 12)
    eq_ok = raw == pre
    all_ok = all_ok and eq_ok
    print(f"  R1 raw == pre-quantized: ${raw} == ${pre} [{PASS if eq_ok else FAIL}]")

    return all_ok


# ---------------------------------------------------------------------------
# R2 — integration: top-up and gate agree on csf_target
# ---------------------------------------------------------------------------

def r2_topup_and_gate_agree(db, run_id: int) -> bool:
    """The csf_target reported in each CSF_TOPUP event's metadata must be
    rounding-consistent with a fresh get_csf_target recompute, and the
    post-event CSFBalance must never EXCEED that target.

    Post-#99 (reserve-first) a month-end top-up can be cash-limited, so the
    CSFBalance may sit genuinely BELOW the committed target by dollars — that is
    not a defect (R3 proves no sub-cent rounding gap exists). The pre-#99 strict
    "balance == target" assumption is therefore relaxed to "balance <= target".
    """
    fm = _new_fund_manager_stub()

    rows = db.execute_query(
        """
        SELECT e.EventID, e.EffectiveDate, e.Metadata, fl.CSFBalance
        FROM simulation.Event e
        JOIN simulation.FundLedger fl
          ON fl.EventID = e.EventID AND fl.RunID = e.RunID
        WHERE e.RunID = ?
          AND e.Metadata LIKE '%FUND/CSF_TOPUP%'
          AND fl.CSFCredit > 0
        ORDER BY e.EventID
        """,
        (run_id,),
    )

    if not rows:
        print(f"  R2 No CSF_TOPUP events found in RunID={run_id} [{SKIP}]")
        return True

    mismatches = 0
    n_checked = 0
    for event_id, eff_date, metadata, csf_balance in rows:
        n_checked += 1
        target_from_meta = _parse_target_from_topup_metadata(metadata)
        opex_from_meta = _parse_monthly_opex_from_topup_metadata(metadata)
        months_from_meta = _parse_reserve_months_from_topup_metadata(metadata)
        if target_from_meta is None or opex_from_meta is None or months_from_meta is None:
            continue
        # Recompute target with the FIXED method using the same opex + the
        # event's own curve months (#97 — no hardcoded 12).
        target_fixed = fm.get_csf_target(opex_from_meta, months_from_meta)
        # Both views of "target" should agree, and CSFBalance should match
        balance_dec = Decimal(str(csf_balance))
        ok = (
            target_fixed == target_from_meta      # reported target is rounding-consistent
            and balance_dec <= target_from_meta   # never over-funded; any gap is a genuine
                                                   # cash-limited shortfall (post-#99 reserve-first),
                                                   # never a sub-cent rounding gap (R3 enforces that)
        )
        if not ok:
            mismatches += 1
            if mismatches <= 3:
                print(
                    f"  R2 MISMATCH @ Event {event_id} {eff_date}: "
                    f"balance=${balance_dec} meta_target=${target_from_meta} "
                    f"fixed_target=${target_fixed}"
                )

    ok = mismatches == 0
    print(f"  R2 Top-up and gate agree on csf_target: {n_checked} events checked, {mismatches} mismatches [{PASS if ok else FAIL}]")
    return ok


# ---------------------------------------------------------------------------
# R3 — integration: no penny-gate silence
# ---------------------------------------------------------------------------

def r3_no_penny_gate_silence(db, run_id: int) -> bool:
    """For every CSF_TOPUP event window in the run, scan all daily
    FundLedger rows in that window and check for "penny gaps":
    0 < (csf_target - csf_balance) <= $0.10. Such a gap is the signature
    of rounding-induced false underfunding and should NEVER occur post-fix.

    Real underfunding (a draw that brought CSF down by hundreds or more
    until the next top-up) is allowed and not flagged.
    """
    # Get every CSF_TOPUP event with its parsed target + the opex that drove it
    topups = db.execute_query(
        """
        SELECT e.EventID, e.EffectiveDate, e.Metadata
        FROM simulation.Event e
        WHERE e.RunID = ?
          AND e.Metadata LIKE '%FUND/CSF_TOPUP%'
        ORDER BY e.EffectiveDate
        """,
        (run_id,),
    )

    if not topups:
        print(f"  R3 No CSF_TOPUP events found in RunID={run_id} [{SKIP}]")
        return True

    # For each top-up, the implied target governs all days until the next top-up.
    # Walk windows and look for daily CSFBalance values that fall in the penny-gap zone.
    fm = _new_fund_manager_stub()
    penny_gap_days = 0
    n_windows = 0

    for i, (event_id, eff_date, metadata) in enumerate(topups):
        target = _parse_target_from_topup_metadata(metadata)
        opex = _parse_monthly_opex_from_topup_metadata(metadata)
        months = _parse_reserve_months_from_topup_metadata(metadata)
        if target is None or opex is None or months is None:
            continue
        # End of window = next top-up's date, or end of run if last
        if i + 1 < len(topups):
            window_end = topups[i + 1][1]  # next event's EffectiveDate
        else:
            window_end_row = db.execute_query(
                "SELECT MAX(LedgerDate) FROM simulation.FundLedger WHERE RunID = ?",
                (run_id,),
            )
            window_end = window_end_row[0][0] if window_end_row and window_end_row[0][0] else eff_date

        # Re-verify target with the FIXED method (R3 specifically checks no penny gap
        # under the fixed contract; #97 — the event's own curve months, not 12).
        target_fixed = fm.get_csf_target(opex, months)

        # Sample daily CSFBalance values in this window
        daily = db.execute_query(
            """
            SELECT LedgerDate, MIN(CSFBalance), MAX(CSFBalance)
            FROM simulation.FundLedger
            WHERE RunID = ? AND LedgerDate >= ? AND LedgerDate < ?
            GROUP BY LedgerDate
            """,
            (run_id, eff_date, window_end),
        )

        n_windows += 1
        for ledger_date, min_csf, max_csf in daily:
            min_dec = Decimal(str(min_csf))
            # The penny gap test: balance was BELOW target by an amount <= $0.10 AND > 0
            gap = target_fixed - min_dec
            if Decimal("0") < gap <= PENNY_THRESHOLD:
                penny_gap_days += 1

    ok = penny_gap_days == 0
    print(
        f"  R3 No penny-gate silence: {n_windows} top-up windows, "
        f"{penny_gap_days} penny-gap days (0 < gap <= ${PENNY_THRESHOLD}) [{PASS if ok else FAIL}]"
    )
    return ok


# ---------------------------------------------------------------------------
# R4 — guardrails intact (3.9.1 discipline rules)
# ---------------------------------------------------------------------------

def r4_guardrails_intact(db, run_id: int) -> bool:
    """Structural sanity check that the rounding fix did not weaken the five
    Phase 3.9.1 discipline rules:

      1. No acquisition if CSF is genuinely below target (except grace period)
      2. No acquisition if Cash is below Cash floor
      3. No acquisition if deployable Cash after floor is insufficient
      4. No CSF draw funds acquisition-related costs
      5. No automatic CSF over-target sweep

    Probes each rule via SQL signatures that would change if the fix had
    inadvertently broken them. Full enforcement is covered by re-running the
    3.9.1 regression suite separately; this test catches obvious regressions.
    """
    all_ok = True

    # Rule 1: CSF gate. Verify the gate method still exists and the
    # PropertyAcquisitionManager imports it correctly.
    try:
        from property_acquisition_manager import PropertyAcquisitionManager
        import inspect
        src = inspect.getsource(PropertyAcquisitionManager.can_acquire_property)
        # Post-#99 the gate is reserve-first + genuine-draw latch (committed_target),
        # not the old "check_csf_target" call; Rule 1 is still grace-gated.
        rule_1_ok = "committed_target" in src and "grace" in src.lower()
        all_ok = all_ok and rule_1_ok
        print(f"  R4.1 can_acquire_property still has the CSF reserve latch (committed_target) + grace [{PASS if rule_1_ok else FAIL}]")
    except Exception as e:
        print(f"  R4.1 inspection failed: {e} [{FAIL}]")
        all_ok = False

    # Rule 2 + 3: Cash floor + deployable cash. Verify the gate code path
    # mentions cash floor.
    try:
        rule_23_ok = "cash_floor" in src.lower() or "cash floor" in src.lower()
        all_ok = all_ok and rule_23_ok
        print(f"  R4.2 can_acquire_property still references cash floor [{PASS if rule_23_ok else FAIL}]")
    except NameError:
        all_ok = False

    # Rule 4: No CSF draw funds acquisitions. Verify process_csf_topup
    # exists and that no FundLedger row has CSFDebit > 0 with a metadata
    # referencing acquisitions in this run.
    rows = db.execute_query(
        """
        SELECT COUNT(*)
        FROM simulation.FundLedger fl
        JOIN simulation.Event e ON e.EventID = fl.EventID AND e.RunID = fl.RunID
        WHERE fl.RunID = ?
          AND fl.CSFDebit > 0
          AND e.Metadata LIKE '%ACQUISITION%'
        """,
        (run_id,),
    )
    n_violations = rows[0][0] if rows else 0
    rule_4_ok = n_violations == 0
    all_ok = all_ok and rule_4_ok
    print(f"  R4.3 No CSF draw funds acquisition: {n_violations} violations [{PASS if rule_4_ok else FAIL}]")

    # Rule 5: No CSF over-target sweep. Verify process_csf_topup has the
    # reserve ratchet guard (returns early if CSF >= target).
    try:
        from fund_manager import FundManager
        src_topup = inspect.getsource(FundManager.process_csf_topup)
        rule_5_ok = "CSF_AT_OR_ABOVE_TARGET" in src_topup
        all_ok = all_ok and rule_5_ok
        print(f"  R4.4 process_csf_topup still has reserve ratchet guard [{PASS if rule_5_ok else FAIL}]")
    except Exception as e:
        print(f"  R4.4 inspection failed: {e} [{FAIL}]")
        all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', required=False, help='DB name under environments/ (omit to run R1 only)')
    parser.add_argument('--run-id', default=None, help='RunID to test (default: latest)')
    parser.add_argument('--assert', dest='assert_mode', action='store_true',
                        help='Exit 1 on any FAIL')
    args = parser.parse_args()

    print('Phase 3.9.2 CSF Target Rounding Fix regression')
    print('=' * 78)

    results = []

    # R1 always runs (pure unit)
    print('R1 — get_csf_target quantizes consistently (unit):')
    results.append(r1_get_csf_target_quantizes_consistently())

    if args.env:
        from database_manager import DatabaseManager  # type: ignore
        db = DatabaseManager(_config_path_for(args.env))
        run_id = _resolve_run_id(db, args.run_id)
        print(f'\nIntegration tests against {args.env} (RunID={run_id}):')
        print('R2 — Top-up and gate agree on csf_target:')
        results.append(r2_topup_and_gate_agree(db, run_id))
        print('R3 — No penny-gate silence:')
        results.append(r3_no_penny_gate_silence(db, run_id))
        print('R4 — 3.9.1 guardrails intact:')
        results.append(r4_guardrails_intact(db, run_id))
    else:
        print('\n(no --env supplied; R2/R3/R4 skipped — pass --env to run integration checks)')

    print('=' * 78)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f'Result: {passed}/{total} tests passed')

    if args.assert_mode and passed != total:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
