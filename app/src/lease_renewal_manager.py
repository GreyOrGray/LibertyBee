"""
Lease Renewal Manager - Voluntary exits, renewals, and non-renewal decisions

This module implements Liberty Bee's lease lifecycle system:
1. Early break probability (0.25% monthly during active lease)
2. Lease-end renewal decisions (80% renew, 20% voluntary exit)
3. Landlord non-renewal (10% if ≥2 late payment months - future dependency)
4. Lease renewal extends LeaseEndDate by 12 months (same LeaseID, no rent inflation)

Key Features:
- Deterministic behavior via seeded RNG
- Renewal extends existing lease (doesn't create new LeaseID)
- Voluntary exits create LeaseTermination records
- Complete audit trail via event logging and LeaseTerminationLedger
- Landlord non-renewal logic exists but inactive

Business Rules:
- Early break: 0.25% probability per month (rare event)
- Renewal rate: 80% at lease end (20% exit voluntarily)
- Landlord non-renewal: 10% if tenant has ≥2 late payment months in prior 12 months
- Renewal: Keeps same LeaseID, extends LeaseEndDate by 12 months, NO rent inflation
- Rent inflation only applies on turnover, not renewal

Usage:
- Use LeaseRenewalManager.process_lease_lifecycle() in daily simulation loop
- Checks all active leases for early breaks and lease-end decisions
"""

import logging
import random
from typing import List, Dict, Optional
from datetime import date
from dateutil.relativedelta import relativedelta
from decimal import Decimal

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from event_logger import EventLogger, EventType, EntityType, ActionType
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from security_deposit_manager import SecurityDepositManager
    from turnover_manager import TurnoverManager
    from tenant_credit_manager import TenantCreditManager


class LeaseRenewalManager:
    """Manage voluntary lease exits, renewals, and non-renewal decisions"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_logger: EventLogger,
        rng: random.Random,
        run_seed: Optional[int] = None,
    ):
        """
        Initialize LeaseRenewalManager.

        Args:
            db_manager: Database manager instance
            event_logger: Event logger instance
            rng: Seeded random number generator (from simulation.py) — used for
                 early-break rolls.
            run_seed: Run seed for the dedicated renewal_rng (offset
                      30901). Optional for backward compatibility; falls
                      back to non-seeded Random() if absent (tests only).
        """
        self.db = db_manager
        self.event_logger = event_logger
        self.logger = logging.getLogger(__name__)
        self.rng = rng

        # lease renewal extension length from the registry
        # (LEASE.RenewalMonths, global) — was class const LEASE_RENEWAL_MONTHS.
        # Distinct from the initial-term LEASE.LeaseDurationMonths. Fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.lease_renewal_months = self.registry.get_int('LEASE', 'RenewalMonths')

        # dedicated renewal-decision RNG. Seeded independently from other
        # managers so future refactors that add/remove RNG calls elsewhere
        # don't reshuffle renewal outcomes. Offset 30901 — matches the
        # tenant_credit_manager.py pattern (redemption_rng = run_seed + 30802,
        # tcs_onboarding_rng = run_seed + 30403).
        self.renewal_rng = (
            random.Random(run_seed + 30901) if run_seed is not None else random.Random()
        )

        # Parameters (set by simulation.py after initialization)
        self._early_break_prob = None
        self._renewal_rate = None
        self._landlord_nonrenewal_prob = None
        self._late_month_threshold = None

        # deposit manager wired in by simulation.py
        self.deposit_manager: "SecurityDepositManager" = None

        # turnover manager wired in by simulation.py
        self.turnover_manager: "TurnoverManager" = None

        # tenant credit manager wired in by simulation.py for
        # exit-detection (calls mark_household_exited on non-eviction lease end
        # when the household has no remaining active LB lease).
        self.tenant_credit_manager: "TenantCreditManager" = None

        # retention model — computes the voluntary-exit
        # probability from the tenant's deal (below-market + tenure) and the regional
        # difficulty of leaving. Wired by simulation.py after config load. Owns no
        # RNG; this manager draws the single renewal_rng.random() and compares.
        self.retention_model = None

    def process_lease_lifecycle(self, run_id: int, current_date: date) -> Dict[str, List[int]]:
        """
        Main entry point - checks daily for lease lifecycle events.

        Processes (in order):
        1. (Day 1 only) Pre-roll renewal decisions for leases whose
           LeaseEndDate falls within the current calendar month. Persists
           outcome to Lease.RenewalDecision so the
           same-day rent-collection cycle can read it.
        2. Early breaks (voluntary exit during active lease)
        3. Lease-end materialization (reads pre-rolled RenewalDecision,
           dispatches to the appropriate _execute_* method)

        Args:
            run_id: Current simulation run ID
            current_date: Current simulation date

        Returns:
            Dict with lists of lease IDs:
            - 'early_breaks': Leases with early break this day
            - 'renewals': Leases renewed this day
            - 'voluntary_exits': Leases voluntarily exited this day
            - 'landlord_nonrenewals': Leases non-renewed by landlord this day
        """
        results = {
            'early_breaks': [],
            'renewals': [],
            'voluntary_exits': [],
            'landlord_nonrenewals': []
        }

        # pre-roll renewal decisions on day 1 of each month.
        # Must run BEFORE materialization in case a lease has
        # LeaseEndDate == day 1 (rare but possible).
        if current_date.day == 1:
            self._pre_roll_renewal_decisions(run_id, current_date)

        # gate the early-break
        # call on day 1, mirroring the pre-roll. The configured
        # `LEASE_EarlyBreakProbMonthly` is a MONTHLY probability; rolling it
        # every day inflated the effective hazard ~19× (4.7%/lease-month
        # observed vs 0.25% intended). Keeping the gate at the orchestration
        # layer here keeps the monthly intent visible alongside the pre-roll.
        early_breaks = []
        if current_date.day == 1:
            early_breaks = self._check_early_breaks(run_id, current_date)
        results['early_breaks'] = early_breaks

        # Materialize lease-end decisions (reads pre-rolled state)
        lease_end_results = self._materialize_lease_end_decisions(run_id, current_date)
        results['renewals'] = lease_end_results['renewals']
        results['voluntary_exits'] = lease_end_results['voluntary_exits']
        results['landlord_nonrenewals'] = lease_end_results['landlord_nonrenewals']

        return results

    # ============================================================
    # INTERNAL QUERY METHODS
    # ============================================================

    def _check_early_breaks(self, run_id: int, current_date: date) -> List[int]:
        """
        Check all active leases for early break probability.

        Business Rule: 0.25% monthly probability of voluntary early break

        Args:
            run_id: Current run ID
            current_date: Current date

        Returns:
            List of lease IDs that early broke today
        """
        # Query active leases not already terminated
        query = """
            SELECT l.LeaseID
            FROM simulation.Lease l
            WHERE l.RunID = ?
                AND l.LeaseStatus = 'Active'
                AND ? >= l.LeaseStartDate
                AND ? < l.LeaseEndDate
                AND NOT EXISTS (
                    SELECT 1 FROM simulation.LeaseTermination lt
                    WHERE lt.RunID = l.RunID AND lt.LeaseID = l.LeaseID
                )
        """

        leases = self.db.execute_query(query, (run_id, current_date, current_date))

        early_breaks = []
        for (lease_id,) in leases:
            # Roll early break probability for each active lease
            if self.rng.random() < self._early_break_prob:
                early_breaks.append(lease_id)
                self._execute_early_break(run_id, lease_id, current_date)

        return early_breaks

    def _pre_roll_renewal_decisions(self, run_id: int, current_date: date) -> None:
        """
        Pre-roll renewal/exit decisions.

        On day 1 of each month, roll renewal/exit decisions for every lease
        whose LeaseEndDate falls within the current calendar month AND that
        has not already been pre-rolled (RenewalDecision IS NULL). Persist the
        outcome to `simulation.Lease.RenewalDecision` and `RenewalDecidedDate`.

        The lease still terminates on `LeaseEndDate`; only the *decision*
        moves earlier. `_materialize_lease_end_decisions` reads the persisted
        decision and dispatches to the existing _execute_* methods without
        re-rolling.

        Uses `self.renewal_rng` (seed offset 30901) — independent
        from `self.rng` (which still handles early-break rolls).
        """
        # Compute end-of-month bound for the query
        next_month = current_date.replace(day=28) + relativedelta(days=4)
        eom = next_month - relativedelta(days=next_month.day)

        query = """
            SELECT l.LeaseID, l.EffectiveMonthlyRent,
                   u.BaseRent, h.SigningMonthlyIncome, CAST(h.CreatedDate AS DATE)
            FROM simulation.Lease l
            JOIN simulation.PropertyUnits u ON u.RunID = l.RunID AND u.UnitID = l.UnitID
            JOIN simulation.Household h ON h.RunID = l.RunID AND h.HouseholdID = l.HouseholdID
            WHERE l.RunID = ?
              AND l.LeaseStatus = 'Active'
              AND l.LeaseEndDate >= ?
              AND l.LeaseEndDate <= ?
              AND l.RenewalDecision IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM simulation.LeaseTermination lt
                  WHERE lt.RunID = l.RunID AND lt.LeaseID = l.LeaseID
              )
        """
        leases = self.db.execute_query(query, (run_id, current_date, eom))

        if not leases:
            return

        # retention-aware renewal. Build the per-date context once
        # (rent/wage factors + regional vacancy are lease-independent), then modulate
        # each lease's voluntary-exit threshold. RNG discipline unchanged: exactly one
        # renewal_rng.random() per renew/exit decision (invariant).
        ctx = self.retention_model.build_date_context(run_id, current_date)

        for (lease_id, eff_rent, base_rent, signing_income, created_date) in leases:
            late_months = self._count_late_months(run_id, lease_id, current_date)

            decision: str
            if late_months >= self._late_month_threshold and \
                    self.renewal_rng.random() < (self._landlord_nonrenewal_prob / 100.0):
                decision = 'LANDLORD_NONRENEWAL'
            else:
                exit_prob = self.retention_model.voluntary_exit_prob(
                    ctx, effective_rent=eff_rent, base_rent=base_rent,
                    signing_income=signing_income, income_reference_date=created_date)
                if self.renewal_rng.random() < exit_prob:
                    decision = 'VOLUNTARY_EXIT'
                else:
                    decision = 'RENEW'

            self.db.execute_non_query(
                """
                UPDATE simulation.Lease
                    SET RenewalDecision = ?,
                        RenewalDecidedDate = ?
                WHERE RunID = ? AND LeaseID = ?
                """,
                (decision, current_date, run_id, lease_id),
            )

        if self.logger:
            self.logger.info(
                f"Pre-rolled renewal decisions for {len(leases)} leases ending in "
                f"{current_date.strftime('%Y-%m')}"
            )

    def _compute_voluntary_exit_prob(self, run_id: int, lease_id: int, current_date: date) -> float:
        """Single-lease retention exit probability (defensive inline path).
        The batch pre-roll builds the date context once and loops; this fetches one
        lease's inputs for the rare mid-month case where the pre-roll didn't fire."""
        row = self.db.execute_query(
            """
            SELECT l.EffectiveMonthlyRent, u.BaseRent, h.SigningMonthlyIncome, CAST(h.CreatedDate AS DATE)
            FROM simulation.Lease l
            JOIN simulation.PropertyUnits u ON u.RunID = l.RunID AND u.UnitID = l.UnitID
            JOIN simulation.Household h ON h.RunID = l.RunID AND h.HouseholdID = l.HouseholdID
            WHERE l.RunID = ? AND l.LeaseID = ?
            """,
            (run_id, lease_id),
        )
        if not row:
            return self.retention_model.base_exit  # no inputs -> the market-equivalent base
        eff_rent, base_rent, signing_income, created_date = row[0]
        ctx = self.retention_model.build_date_context(run_id, current_date)
        return self.retention_model.voluntary_exit_prob(
            ctx, effective_rent=eff_rent, base_rent=base_rent,
            signing_income=signing_income, income_reference_date=created_date)

    def _materialize_lease_end_decisions(self, run_id: int, current_date: date) -> Dict[str, List[int]]:
        """
        Read the pre-rolled `RenewalDecision` for each lease
        ending today and dispatch to the appropriate _execute_* method.

        Does NOT roll the renewal RNG. If a lease somehow reaches its
        LeaseEndDate without a pre-rolled decision (edge case: very short
        lease created mid-month), this method defensively rolls inline using
        `self.renewal_rng` to preserve backward compatibility.

        Business Rules (unchanged):
        - Landlord non-renewal: 10% if tenant has ≥2 late months
        - Renewal: 80%
        - Voluntary exit: 20%
        """
        query = """
            SELECT l.LeaseID, l.RenewalDecision
            FROM simulation.Lease l
            WHERE l.RunID = ?
                AND l.LeaseStatus = 'Active'
                AND l.LeaseEndDate = ?
                AND NOT EXISTS (
                    SELECT 1 FROM simulation.LeaseTermination lt
                    WHERE lt.RunID = l.RunID AND lt.LeaseID = l.LeaseID
                )
        """
        leases = self.db.execute_query(query, (run_id, current_date))

        results = {
            'renewals': [],
            'voluntary_exits': [],
            'landlord_nonrenewals': []
        }

        for lease_id, renewal_decision in leases:
            # Defensive: if pre-roll didn't fire (e.g., lease created mid-month),
            # roll inline now. This should be rare on standard 12-month leases.
            if renewal_decision is None:
                late_months = self._count_late_months(run_id, lease_id, current_date)
                if late_months >= self._late_month_threshold and \
                        self.renewal_rng.random() < (self._landlord_nonrenewal_prob / 100.0):
                    renewal_decision = 'LANDLORD_NONRENEWAL'
                else:
                    # retention-aware voluntary decision (rare inline path).
                    exit_prob = self._compute_voluntary_exit_prob(run_id, lease_id, current_date)
                    if self.renewal_rng.random() < exit_prob:
                        renewal_decision = 'VOLUNTARY_EXIT'
                    else:
                        renewal_decision = 'RENEW'
                # Persist the late-rolled decision for audit trail
                self.db.execute_non_query(
                    """
                    UPDATE simulation.Lease
                        SET RenewalDecision = ?,
                            RenewalDecidedDate = ?
                    WHERE RunID = ? AND LeaseID = ?
                    """,
                    (renewal_decision, current_date, run_id, lease_id),
                )

            # Dispatch to the existing _execute_* methods (unchanged behavior)
            if renewal_decision == 'LANDLORD_NONRENEWAL':
                results['landlord_nonrenewals'].append(lease_id)
                self._execute_landlord_nonrenewal(run_id, lease_id, current_date)
            elif renewal_decision == 'RENEW':
                results['renewals'].append(lease_id)
                self._execute_renewal(run_id, lease_id, current_date)
            elif renewal_decision == 'VOLUNTARY_EXIT':
                results['voluntary_exits'].append(lease_id)
                self._execute_voluntary_exit(run_id, lease_id, current_date)
            else:
                self.logger.warning(
                    f"Lease {lease_id} has invalid RenewalDecision={renewal_decision} "
                    f"on LeaseEndDate {current_date}; skipping"
                )

        return results

    def _count_late_months(self, run_id: int, lease_id: int, current_date: date) -> int:
        """
        Count payment-problem months in prior 12 months.

        Counts MonthlyPaymentStatus.PaymentStatus IN ('LATE', 'MISSED').

        The original predicate looked only at 'LATE'. The
        rent-collection code-path never writes 'LATE' (CanPay=True paths
        always collect inside the 5-day grace window → 'ON_TIME'), so the
        prior predicate was structurally unreachable and the
        landlord-nonrenewal mechanism was dead. The fix counts 'MISSED' as
        the real
        payment-problem signal alongside 'LATE'. 'LATE' is retained for
        forward-compatibility with future late-payment-behavior phases
        ("richer LATE semantics may be revisited").

        Args:
            run_id: Current run ID
            lease_id: Lease to check
            current_date: Current date

        Returns:
            Count of prior-12-month MonthlyPaymentStatus rows with
            PaymentStatus in ('LATE', 'MISSED').
        """
        twelve_months_ago = current_date - relativedelta(months=12)

        query = """
            SELECT COUNT(*)
            FROM simulation.MonthlyPaymentStatus
            WHERE RunID = ?
              AND LeaseID = ?
              AND PaymentStatus IN ('LATE', 'MISSED')
              AND BillingMonth >= ?
              AND BillingMonth < ?
        """
        result = self.db.execute_scalar(query, (run_id, lease_id, twelve_months_ago, current_date))

        return result if result is not None else 0

    # ============================================================
    # INTERNAL ACTION METHODS
    # ============================================================

    def _execute_early_break(self, run_id: int, lease_id: int, break_date: date) -> None:
        """
        Execute early lease break (voluntary exit during active lease).

        Creates:
        - LeaseTermination record (TerminationType='VOLUNTARY')
        - LeaseTerminationLedger entry (TxnType='VOLUNTARY_NOTICE')
        - Event log entry

        Args:
            run_id: Current run ID
            lease_id: Lease to terminate
            break_date: Date of early break
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Get current lease info (also fetch HouseholdID
            #    for the exit-redemption call below)
            cursor.execute("""
                SELECT EffectiveMonthlyRent, ConsecutiveMissedPayments, HouseholdID
                FROM simulation.Lease
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            row = cursor.fetchone()
            if not row:
                self.logger.error(f"Lease {lease_id} not found for early break")
                return

            monthly_rent = Decimal(str(row[0]))
            consecutive_missed = row[1]
            household_id = row[2]
            arrears = monthly_rent * consecutive_missed

            # 2. Log event (moved ahead of the LeaseStatus update so the
            #    exit redemption fires while the lease is still
            #    active and carries this event_id as its trigger).
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=break_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.COMPLETE,
                amount=arrears,
                metadata=f"Early break - voluntary exit during lease, arrears: ${arrears:,.2f}"
            )

            # 3. Final-month exit redemption for early-break exits
            #    Mirrors the eviction-
            #    forfeiture pattern at eviction_manager.py — the termination
            #    author makes the TCS call directly. Does NOT consult
            #    Lease.RenewalDecision: early-break exits never have a
            #    pre-rolled decision. Five-condition eligibility is enforced
            #    inside the credit manager (non-eviction, leaving-LB,
            #    no-prior-redemption-this-year, balance >= rent, good standing).
            #    Not-eligible returns None and we proceed with normal exit.
            if self.tenant_credit_manager is not None and household_id is not None:
                deposit_funded = (
                    self.deposit_manager.is_deposit_funded(lease_id)
                    if self.deposit_manager else True
                )
                self.tenant_credit_manager.process_final_month_exit_redemption(
                    household_id=household_id,
                    lease_id=lease_id,
                    collection_id=None,
                    monthly_rent=monthly_rent,
                    exit_date=break_date,
                    event_id=event_id,
                    deposit_funded=deposit_funded,
                )

            # 4. Update Lease status
            cursor.execute("""
                UPDATE simulation.Lease
                SET LeaseStatus = 'Terminated'
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            # 5. Create LeaseTermination record
            cursor.execute("""
                INSERT INTO simulation.LeaseTermination
                (RunID, LeaseID, TerminationType, TerminationDate, TerminationReason, ArrearsAtExit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                run_id, lease_id, 'VOLUNTARY', break_date,
                'Tenant early break - voluntary exit during active lease', arrears
            ))

            # 6. Create LeaseTerminationLedger entry
            ledger_id = self._get_next_ledger_id(run_id)
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, ledger_id, lease_id, event_id, break_date, 'VOLUNTARY_NOTICE',
                'Tenant early break - voluntary exit during active lease', arrears,
                f'{{"arrears": {float(arrears)}, "early_break": true}}'
            ))

            conn.commit()
            self.logger.info(f"Early break - LeaseID {lease_id}, arrears: ${arrears:,.2f}")

        # Queue deposit settlement
        if self.deposit_manager:
            self.deposit_manager.settle_deposit_at_termination(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=break_date,
                termination_type="EARLY_BREAK"
            )

        # Trigger turnover workflow
        if self.turnover_manager:
            self.turnover_manager.trigger_turnover(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=break_date,
                termination_type="EARLY_BREAK"
            )

        # TCS lifecycle — if this is the household's last active
        # LB lease, start the 24-month expiry clock.
        self._mark_household_exited_if_last_lease(run_id, lease_id, break_date)

    def _execute_renewal(self, run_id: int, lease_id: int, renewal_date: date) -> None:
        """
        Execute lease renewal (extend LeaseEndDate by 12 months).

        Business Rule: Renewal keeps same LeaseID, rent unchanged (no inflation).
        Rent inflation only applies on turnover, not renewal.

        Creates:
        - LeaseTerminationLedger entry (TxnType='LEASE_RENEWED' for audit trail)
        - Event log entry
        - Updates Lease.LeaseEndDate

        Args:
            run_id: Current run ID
            lease_id: Lease to renew
            renewal_date: Date of renewal (current LeaseEndDate)
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Calculate new end date (12 months forward)
            new_end_date = renewal_date + relativedelta(months=self.lease_renewal_months)

            # 2. Update Lease table — extend end date AND clear renewal decision state.
            # The pre-existing version updated
            # only LeaseEndDate. Combined with the pre-roll's
            # `WHERE RenewalDecision IS NULL` filter, renewed leases were permanently
            # excluded from future pre-rolls — the stale 'RENEW' decision was reused
            # at every subsequent boundary, causing auto-renewal forever. Clearing
            # RenewalDecision + RenewalDecidedDate here lets the next cycle's
            # pre-roll pick the lease up again.
            cursor.execute("""
                UPDATE simulation.Lease
                SET LeaseEndDate = ?,
                    RenewalDecision = NULL,
                    RenewalDecidedDate = NULL
                WHERE RunID = ? AND LeaseID = ?
            """, (new_end_date, run_id, lease_id))

            # 3. Log event
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=renewal_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.UPDATE,
                metadata=f"Lease renewed - extended to {new_end_date}"
            )

            # 4. Create LeaseTerminationLedger entry (audit trail only, no LeaseTermination)
            ledger_id = self._get_next_ledger_id(run_id)
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, ledger_id, lease_id, event_id, renewal_date, 'LEASE_RENEWED',
                f'Lease renewed - extended to {new_end_date}', Decimal('0.00'),
                f'{{"old_end_date": "{renewal_date}", "new_end_date": "{new_end_date}"}}'
            ))

            conn.commit()
            self.logger.info(f"Lease {lease_id} renewed - new end date: {new_end_date}")

    def _execute_voluntary_exit(self, run_id: int, lease_id: int, exit_date: date) -> None:
        """
        Execute voluntary exit at lease end (tenant decides not to renew).

        Creates:
        - LeaseTermination record (TerminationType='VOLUNTARY')
        - LeaseTerminationLedger entry (TxnType='VOLUNTARY_NOTICE')
        - Event log entry

        Args:
            run_id: Current run ID
            lease_id: Lease to terminate
            exit_date: Date of exit (LeaseEndDate)
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Get current lease info (also fetch HouseholdID
            #    for the exit-redemption call below)
            cursor.execute("""
                SELECT EffectiveMonthlyRent, ConsecutiveMissedPayments, HouseholdID
                FROM simulation.Lease
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            row = cursor.fetchone()
            if not row:
                self.logger.error(f"Lease {lease_id} not found for voluntary exit")
                return

            monthly_rent = Decimal(str(row[0]))
            consecutive_missed = row[1]
            household_id = row[2]
            arrears = monthly_rent * consecutive_missed

            # 2. Log event (moved ahead of the LeaseStatus update so the
            #    exit redemption fires while the lease is still
            #    active and carries this event_id as its trigger).
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=exit_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.COMPLETE,
                amount=arrears,
                metadata=f"Voluntary exit - lease ended, arrears: ${arrears:,.2f}"
            )

            # 3. Final-month exit redemption for lease-end
            #    voluntary exits. Mirrors
            #    the early-break wiring and the eviction-forfeiture pattern in
            #    eviction_manager.py — the termination author calls the TCS
            #    path directly, not the rent_collection branch (which was
            #    silently broken by the pre-roll/materialization same-day race
            #    and has been removed). Five-condition eligibility is enforced
            #    inside the credit manager.
            if self.tenant_credit_manager is not None and household_id is not None:
                deposit_funded = (
                    self.deposit_manager.is_deposit_funded(lease_id)
                    if self.deposit_manager else True
                )
                self.tenant_credit_manager.process_final_month_exit_redemption(
                    household_id=household_id,
                    lease_id=lease_id,
                    collection_id=None,
                    monthly_rent=monthly_rent,
                    exit_date=exit_date,
                    event_id=event_id,
                    deposit_funded=deposit_funded,
                )

            # 4. Update Lease status
            cursor.execute("""
                UPDATE simulation.Lease
                SET LeaseStatus = 'Terminated'
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            # 5. Create LeaseTermination record
            cursor.execute("""
                INSERT INTO simulation.LeaseTermination
                (RunID, LeaseID, TerminationType, TerminationDate, TerminationReason, ArrearsAtExit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                run_id, lease_id, 'VOLUNTARY', exit_date,
                'Tenant voluntary exit at lease end', arrears
            ))

            # 6. Create LeaseTerminationLedger entry
            ledger_id = self._get_next_ledger_id(run_id)
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, ledger_id, lease_id, event_id, exit_date, 'VOLUNTARY_NOTICE',
                'Tenant voluntary exit at lease end', arrears,
                f'{{"arrears": {float(arrears)}}}'
            ))

            conn.commit()
            self.logger.info(f"Voluntary exit - LeaseID {lease_id}, arrears: ${arrears:,.2f}")

        # Queue deposit settlement
        if self.deposit_manager:
            self.deposit_manager.settle_deposit_at_termination(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=exit_date,
                termination_type="VOLUNTARY"
            )

        # Trigger turnover workflow
        if self.turnover_manager:
            self.turnover_manager.trigger_turnover(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=exit_date,
                termination_type="VOLUNTARY"
            )

        # TCS lifecycle — start 24-month expiry clock if this is
        # the household's last active LB lease.
        self._mark_household_exited_if_last_lease(run_id, lease_id, exit_date)

    def _execute_landlord_nonrenewal(self, run_id: int, lease_id: int, exit_date: date) -> None:
        """
        Execute landlord non-renewal (landlord decides not to renew due to late payments).

        Business Rule: 10% probability if tenant has ≥2 late payment months in prior 12 months.

        Creates:
        - LeaseTermination record (TerminationType='NON_RENEWAL_LANDLORD')
        - LeaseTerminationLedger entry (TxnType='NON_RENEWAL_LANDLORD')
        - Event log entry

        Args:
            run_id: Current run ID
            lease_id: Lease to terminate
            exit_date: Date of non-renewal (LeaseEndDate)
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Get current lease info (also fetch HouseholdID
            #    for the exit-redemption call below)
            cursor.execute("""
                SELECT EffectiveMonthlyRent, ConsecutiveMissedPayments, HouseholdID
                FROM simulation.Lease
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            row = cursor.fetchone()
            if not row:
                self.logger.error(f"Lease {lease_id} not found for landlord non-renewal")
                return

            monthly_rent = Decimal(str(row[0]))
            consecutive_missed = row[1]
            household_id = row[2]
            arrears = monthly_rent * consecutive_missed

            # 2. Log event (moved ahead of LeaseStatus update so the
            #    exit redemption fires while the lease is still active).
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=exit_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.COMPLETE,
                amount=arrears,
                metadata=f"Landlord non-renewal - tenant late payment history, arrears: ${arrears:,.2f}"
            )

            # 3. Final-month exit redemption for landlord-non-
            #    renewal exits. Mirrors
            #    the early-break and voluntary-exit wiring. Note that good-
            #    standing eligibility (no prior unresolved MISSED months) is
            #    likely to fail here — landlord-non-renewal triggers on late
            #    payment history — but the check is uniform and the credit
            #    manager will correctly return None when ineligible.
            if self.tenant_credit_manager is not None and household_id is not None:
                deposit_funded = (
                    self.deposit_manager.is_deposit_funded(lease_id)
                    if self.deposit_manager else True
                )
                self.tenant_credit_manager.process_final_month_exit_redemption(
                    household_id=household_id,
                    lease_id=lease_id,
                    collection_id=None,
                    monthly_rent=monthly_rent,
                    exit_date=exit_date,
                    event_id=event_id,
                    deposit_funded=deposit_funded,
                )

            # 4. Update Lease status
            cursor.execute("""
                UPDATE simulation.Lease
                SET LeaseStatus = 'Terminated'
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            # 5. Create LeaseTermination record
            cursor.execute("""
                INSERT INTO simulation.LeaseTermination
                (RunID, LeaseID, TerminationType, TerminationDate, TerminationReason, ArrearsAtExit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                run_id, lease_id, 'NON_RENEWAL_LANDLORD', exit_date,
                'Landlord non-renewal - tenant late payment history', arrears
            ))

            # 6. Create LeaseTerminationLedger entry
            ledger_id = self._get_next_ledger_id(run_id)
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, ledger_id, lease_id, event_id, exit_date, 'NON_RENEWAL_LANDLORD',
                'Landlord non-renewal - tenant late payment history', arrears,
                f'{{"arrears": {float(arrears)}}}'
            ))

            conn.commit()
            self.logger.info(f"Landlord non-renewal - LeaseID {lease_id}, arrears: ${arrears:,.2f}")

        # Queue deposit settlement
        if self.deposit_manager:
            self.deposit_manager.settle_deposit_at_termination(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=exit_date,
                termination_type="LANDLORD_NONRENEWAL"
            )

        # Trigger turnover workflow
        if self.turnover_manager:
            self.turnover_manager.trigger_turnover(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=exit_date,
                termination_type="LANDLORD_NONRENEWAL"
            )

        # TCS lifecycle — start 24-month expiry clock if this is
        # the household's last active LB lease.
        self._mark_household_exited_if_last_lease(run_id, lease_id, exit_date)

    def _mark_household_exited_if_last_lease(
        self,
        run_id: int,
        terminating_lease_id: int,
        exit_date: date,
    ) -> None:
        """
        TCS lifecycle hook.

        Called by the non-eviction _execute_* methods after the lease has been
        marked Terminated. If the household has no other active LB lease,
        starts the household's 24-month TCS expiry clock via
        TenantCreditManager.mark_household_exited().

        Eviction-driven exits do NOT call this — eviction forfeits credit
        immediately (handled by eviction_manager → process_credit_forfeiture_eviction).

        Defensive: if tenant_credit_manager isn't wired (older tests, partial
        sim init), silently no-op.
        """
        if self.tenant_credit_manager is None:
            return

        # Look up the household for the terminating lease
        row = self.db.execute_query(
            "SELECT HouseholdID FROM simulation.Lease WHERE RunID = ? AND LeaseID = ?",
            (run_id, terminating_lease_id),
        )
        if not row or row[0][0] is None:
            return
        household_id = row[0][0]

        # Does this household have any OTHER active LB lease?
        # Use LeaseStatus = 'Active' AND NOT EXISTS terminated record to mirror
        # the check used elsewhere in this manager.
        other_active = self.db.execute_query(
            """
            SELECT COUNT(*)
            FROM simulation.Lease l
            WHERE l.RunID = ?
              AND l.HouseholdID = ?
              AND l.LeaseID <> ?
              AND l.LeaseStatus = 'Active'
              AND NOT EXISTS (
                  SELECT 1 FROM simulation.LeaseTermination lt
                  WHERE lt.RunID = l.RunID AND lt.LeaseID = l.LeaseID
              )
            """,
            (run_id, household_id, terminating_lease_id),
        )
        n_other_active = other_active[0][0] if other_active else 0

        if n_other_active == 0:
            # This was the household's last active lease — start expiry clock
            self.tenant_credit_manager.mark_household_exited(household_id, exit_date)

    # ============================================================
    # UTILITY METHODS
    # ============================================================

    def _get_next_ledger_id(self, run_id: int) -> int:
        """
        Get next available LedgerID for LeaseTerminationLedger (per-run counter).

        Args:
            run_id: Current run ID

        Returns:
            Next available LedgerID
        """
        query = """
            SELECT ISNULL(MAX(LedgerID), 0) + 1
            FROM simulation.LeaseTerminationLedger
            WHERE RunID = ?
        """
        result = self.db.execute_query(query, (run_id,))
        return result[0][0] if result else 1
