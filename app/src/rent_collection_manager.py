"""
Rent Collection Manager - Daily revenue collection from active leases

This module implements Liberty Bee's rent collection system:
Basic monthly rent collection
Late payment handling with grace periods and late fees
Obligation Settlement & Ordering with FIFO arrears tracking

Key Features:
- Daily rent collection processing (not just 1st of month)
- Grace period handling (days 1-5 = ON_TIME, no late fee)
- Late fee calculation (5% of monthly rent for payments after grace period)
- MISSED payment tracking (no payment by end of month)
- Payment state persistence (CanPayThisMonth decided once per month)
- Arrears collection (current + arrears + late fee together)
- FIFO arrears ordering (oldest arrears paid first)
- Post-termination payment support (tenants can clear arrears after lease ends)
- Complete audit trail via event logging

Core Principle:
"Ability to pay is decided once per month; timing of payment is decided day-by-day."

Payment Obligation Priority Order:
1. Current Month Rent
2. Outstanding Rent Arrears (FIFO - oldest first)
3. Late Fees

Payment Classifications:
- ON_TIME: Payment within grace period (days 1-5), no late fee
- LATE: Payment after grace period (days 6+), 5% late fee
- MISSED: No payment by end of calendar month

Post-Termination Payments:
- Tenants may clear arrears after lease termination
- Same ordering rules apply (current → arrears FIFO → late fees)
- Write-offs do NOT block payment collection (write-off = accounting action, not debt forgiveness)

Usage:
- Use RentCollectionManager.process_daily_rent_collection() in daily simulation loop
- Returns list of RentCollectionRecord objects (may be empty if no payments)
"""

import logging
import random
from typing import List, Optional
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from calendar import monthrange

from database_manager import DatabaseManager
from fund_manager import FundManager
from event_logger import EventLogger, EventType, EntityType, ActionType
from security_deposit_manager import SecurityDepositManager
from tenant_credit_manager import TenantCreditManager


@dataclass
class RentCollectionRecord:
    """Record of a single rent collection event"""
    collection_id: int
    lease_id: int
    collection_date: date
    collection_month: date
    amount_collected: Decimal
    created_event_id: int
    payment_status: Optional[str] = None  # 'ON_TIME', 'LATE', 'MISSED'
    late_fee_amount: Optional[Decimal] = None  # Late fee charged
    arrears_amount: Optional[Decimal] = None  # Arrears paid with this collection


class RentCollectionManager:
    """Manage daily rent collection from active leases with late payment handling"""

    def __init__(self, db_manager: DatabaseManager, fund_manager: FundManager,
                 event_logger: EventLogger, deposit_manager: Optional[SecurityDepositManager] = None,
                 tenant_credit_manager: Optional[TenantCreditManager] = None,
                 rng: Optional[random.Random] = None, payment_fail_prob: float = 0.02,
                 grace_period_days: int = 5, late_fee_percent: float = 5.0):
        self.db = db_manager
        self.fund_manager = fund_manager
        self.logger = logging.getLogger(__name__)
        self.event_logger = event_logger
        self.deposit_manager = deposit_manager
        self.tenant_credit_manager = tenant_credit_manager  # TCS accrual hook
        self.run_id = None
        self._next_collection_id = 1  # CollectionID counter (per-run)
        self.rng = rng if rng is not None else random.Random()  # Seeded RNG for deterministic payment failures
        self.payment_fail_prob = payment_fail_prob  # PAY_BaseFailProbMonthly — via reference.ParameterRegistry / ProjectionConfig

        # Late payment parameters
        self.grace_period_days = grace_period_days  # PAY_GracePeriodDays (default 5)
        self.late_fee_percent = late_fee_percent  # PAY_LateFeePercent (default 5.0)


    def set_run_id(self, run_id: int):
        """Set current run ID and initialize CollectionID counter

        Args:
            run_id: Simulation run ID
        """
        self.run_id = run_id

        # Initialize CollectionID counter by querying max CollectionID for this run
        query = """
            SELECT ISNULL(MAX(CollectionID), 0) + 1 AS NextCollectionID
            FROM simulation.RentCollection
            WHERE RunID = ?
        """
        result = self.db.execute_query(query, (run_id,))
        if result and len(result) > 0:
            self._next_collection_id = result[0][0]
        else:
            self._next_collection_id = 1

        self.logger.info(f"RentCollectionManager initialized for RunID {run_id}, next CollectionID: {self._next_collection_id}")

    # ========== DAILY RENT COLLECTION WITH LATE PAYMENT HANDLING ==========

    def process_daily_rent_collection(self, current_date: date) -> List[RentCollectionRecord]:
        """
        Main entry point - runs DAILY (not just day 1).

        Daily logic:
        - Day 1: Initialize monthly payment status for all active leases
        - Days 1-30: Attempt collection for unpaid leases
        - Last day of month: Finalize MISSED status for unpaid leases

        Args:
            current_date: Current simulation date

        Returns:
            List of RentCollectionRecord objects (may be empty)
        """
        current_day = current_date.day
        collections = []

        # Step 1: Initialize monthly payment status on day 1
        if current_day == 1:
            self._initialize_monthly_payment_status(current_date)

        # Step 2: Attempt collection for unpaid leases (runs every day)
        collections = self._attempt_daily_collection(current_date)

        # Step 3: Finalize MISSED status on last day of month
        if self._is_last_day_of_month(current_date):
            self._finalize_missed_payments(current_date)

        return collections

    def _initialize_monthly_payment_status(self, current_date: date) -> None:
        """
        On day 1, create MonthlyPaymentStatus records for all active leases.
        Roll payment_fail_prob ONCE to determine CanPayThisMonth.

        Args:
            current_date: First day of month (day 1)
        """
        billing_month = current_date.replace(day=1)
        active_leases = self._get_active_leases(current_date)

        self.logger.info(f"Initializing monthly payment status for {len(active_leases)} active leases on {current_date}")

        for lease in active_leases:
            lease_id = lease[0]
            # lease[1] is EffectiveMonthlyRent — the current billable
            # rent after any earned tenure reductions. Equals the original signed
            # rent for leases with no reduction. All downstream current-rent logic
            # (AmountDue, late fee, FME eligibility, TCS) uses this value.
            monthly_rent = Decimal(str(lease[1]))
            lease_end_date = lease[2]
            renewal_decision = lease[3]            # NULL until pre-roll fires
            occupied_this_month = bool(lease[4])   # OccupiedThisMonth
            # OccupiedThisMonth=0 ⇒ the lease exited in a PRIOR month and only surfaces via
            # the arrears arm — bill $0 so pre-existing arrears settle without a
            # phantom new-rent obligation (basis = collected - arrears - late = 0 ⇒ no phantom
            # TCS accrual either). Occupied months — including the eviction-execution month and
            # the voluntary/non-renewal final month — bill normally.
            obligation_due = monthly_rent if occupied_this_month else Decimal('0.00')

            # The FINAL_MONTH_EXIT detection block
            # that previously lived here has been removed. It was silently
            # broken — the lease-end pre-roll fires the same day as the
            # lease-end materialization, so on day 1 of the lease-end month
            # the RenewalDecision the predicate read was still NULL and the
            # branch was never entered for the lease-end path (the early-
            # break path was structurally unreachable here regardless). As of
            # 2026-05-22, `process_final_month_exit_redemption`
            # is now called directly inside the termination authors:
            # lease_renewal_manager._execute_voluntary_exit,
            # ._execute_landlord_nonrenewal, and ._execute_early_break.
            # `renewal_decision` / `lease_end_date` from the SELECT are now
            # unused here; the SELECT itself stays untouched to preserve the
            # determinism contract.
            redeemed = False
            redemption_path = None

            # Roll payment_fail_prob ONCE to determine payability
            # CanPay roll fires BEFORE redemption decision.
            can_pay_this_month = self.rng.random() > self.payment_fail_prob

            # Redemption decision — routine path if can_pay,
            # hardship path if can't. Determinism: redemption_rng is
            # consumed once per lease in the same stable iteration order
            # as self.rng (ORDER BY LeaseID).
            if self.tenant_credit_manager and occupied_this_month:
                deposit_funded = (
                    self.deposit_manager.is_deposit_funded(lease_id)
                    if self.deposit_manager else True
                )
                if can_pay_this_month:
                    if self.tenant_credit_manager.should_routine_redeem(
                        household_id=self._lookup_household_for_lease(lease_id),
                        lease_id=lease_id,
                        monthly_rent=monthly_rent,
                        current_date=current_date,
                        deposit_funded=deposit_funded,
                    ):
                        redeemed = True
                        redemption_path = "ROUTINE"
                else:
                    if self.tenant_credit_manager.should_hardship_redeem(
                        household_id=self._lookup_household_for_lease(lease_id),
                        lease_id=lease_id,
                        monthly_rent=monthly_rent,
                        current_date=current_date,
                        deposit_funded=deposit_funded,
                    ):
                        redeemed = True
                        redemption_path = "HARDSHIP"

            # Calculate grace period end date (5 days after month start)
            grace_period_end_date = current_date + timedelta(days=self.grace_period_days)

            # Calculate arrears (unpaid rent from previous months)
            # Arrears tracked in FIFO order via MonthlyPaymentStatus.BillingMonth ASC
            arrears = self._calculate_arrears(lease_id, current_date)

            if redeemed:
                # Write a satisfied month — REDEEMED MonthlyPaymentStatus
                # row + RentCollection row + TCS ledger REDEMPTION row.
                self._process_redemption_at_init(
                    lease_id=lease_id,
                    monthly_rent=monthly_rent,
                    can_pay_this_month=can_pay_this_month,
                    billing_month=billing_month,
                    grace_period_end_date=grace_period_end_date,
                    arrears=arrears,
                    current_date=current_date,
                    path=redemption_path,
                )
            else:
                # Existing flow: insert plain MonthlyPaymentStatus row
                insert_query = """
                    INSERT INTO simulation.MonthlyPaymentStatus
                    (RunID, LeaseID, BillingMonth, CanPayThisMonth, PaymentDueDate,
                     GracePeriodEndDate, AmountDue, ArrearsAmount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """
                self.db.execute_non_query(insert_query, (
                    self.run_id, lease_id, billing_month, can_pay_this_month,
                    billing_month, grace_period_end_date, obligation_due, arrears
                ))

                self.logger.debug(
                    f"Initialized payment status: LeaseID={lease_id}, "
                    f"CanPay={can_pay_this_month}, Rent=${monthly_rent:.2f}, "
                    f"Arrears=${arrears:.2f}"
                )

    def _attempt_daily_collection(self, current_date: date) -> List[RentCollectionRecord]:
        """
        Attempt collection for all unpaid leases.

        Logic:
        - If CanPayThisMonth = FALSE → skip (will be MISSED)
        - If CanPayThisMonth = TRUE and not yet paid → attempt collection TODAY

        Args:
            current_date: Current simulation date

        Returns:
            List of RentCollectionRecord objects for successful collections
        """
        billing_month = self._first_of_month(current_date)

        # Query unpaid leases that CAN pay this month
        # lease_data[4] feeds the late-fee base — use the current
        # billable EffectiveMonthlyRent, not the original signed MonthlyRent.
        query = """
            SELECT mps.LeaseID, mps.BillingMonth, mps.AmountDue, mps.ArrearsAmount,
                   l.EffectiveMonthlyRent, mps.GracePeriodEndDate
            FROM simulation.MonthlyPaymentStatus mps
            JOIN simulation.Lease l ON mps.RunID = l.RunID AND mps.LeaseID = l.LeaseID
            WHERE mps.RunID = ?
              AND mps.BillingMonth = ?  -- Current month only
              AND mps.CanPayThisMonth = 1  -- Tenant CAN pay this month
              AND mps.ActualPaymentDate IS NULL  -- Not yet paid
        """

        unpaid_leases = self.db.execute_query(query, (self.run_id, billing_month))

        if not unpaid_leases:
            return []

        collections = []
        for lease_data in unpaid_leases:
            # Attempt collection (no RNG roll - tenant WILL pay, just determining when)
            collection_record = self._collect_payment(current_date, lease_data)
            if collection_record:
                collections.append(collection_record)

        if collections:
            total_collected = sum(c.amount_collected for c in collections)
            on_time_count = sum(1 for c in collections if c.payment_status == 'ON_TIME')
            late_count = sum(1 for c in collections if c.payment_status == 'LATE')

            self.logger.info(
                f"Daily collection on {current_date}: {len(collections)} payments "
                f"(ON_TIME: {on_time_count}, LATE: {late_count}), "
                f"Total: ${total_collected:,.2f}"
            )

        return collections

    def _collect_payment(self, current_date: date, lease_data) -> Optional[RentCollectionRecord]:
        """
        Collect payment for a single lease.

        Obligation Priority Order (explicit, deterministic):
        1. Current Month Rent (amount_due)
        2. Outstanding Rent Arrears (arrears - FIFO ordering from MonthlyPaymentStatus)
        3. Late Fees (late_fee - calculated based on grace period)

        Determine classification based on current day:
        - Days 1-5 (grace period) → ON_TIME (no late fee)
        - Days 6+ → LATE (5% late fee)

        Args:
            current_date: Current simulation date
            lease_data: Tuple of (LeaseID, BillingMonth, AmountDue, ArrearsAmount, MonthlyRent, GracePeriodEndDate)

        Returns:
            RentCollectionRecord or None
        """
        lease_id = lease_data[0]
        billing_month = lease_data[1]
        amount_due = Decimal(str(lease_data[2]))  # Priority 1: Current Month Rent
        arrears = Decimal(str(lease_data[3]))      # Priority 2: Arrears (FIFO from MonthlyPaymentStatus)
        monthly_rent = Decimal(str(lease_data[4]))
        grace_period_end_date = lease_data[5]

        # Determine payment classification based on current day
        if current_date <= grace_period_end_date:
            # Payment within grace period → ON_TIME
            payment_status = 'ON_TIME'
            late_fee = Decimal('0.00')  # Priority 3: No late fee
        else:
            # Payment after grace period → LATE
            payment_status = 'LATE'
            late_fee = monthly_rent * Decimal(str(self.late_fee_percent / 100.0))  # Priority 3: Late fee

        # Total amount to collect (Current + Arrears FIFO + Late Fee)
        # Atomic payment: all-or-nothing (no partial satisfaction)
        total_amount = amount_due + arrears + late_fee

        # Log event
        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.FINANCIAL,
            amount=total_amount,
            metadata=f"Rent collection - {payment_status}, Rent: ${amount_due:.2f}, Arrears: ${arrears:.2f}, Late fee: ${late_fee:.2f}"
        )

        # Collect payment via FundManager
        self.fund_manager.process_income(
            run_id=self.run_id,
            income_amount=total_amount,
            ledger_date=current_date,
            income_type='RENT'
        )

        # Update MonthlyPaymentStatus for current month
        update_query = """
            UPDATE simulation.MonthlyPaymentStatus
            SET ActualPaymentDate = ?,
                PaymentStatus = ?,
                LateFeeAmount = ?
            WHERE RunID = ? AND LeaseID = ? AND BillingMonth = ?
        """
        self.db.execute_non_query(update_query, (
            current_date, payment_status, late_fee,
            self.run_id, lease_id, billing_month
        ))

        # Update old MISSED records if arrears were collected (FIFO satisfaction)
        if arrears > Decimal('0.00'):
            # Get FIFO arrears list to update old MISSED records
            arrears_fifo = self._get_arrears_fifo(lease_id, current_date)
            for (arrears_month, arrears_amount) in arrears_fifo:
                # Mark old MISSED month as satisfied (set ActualPaymentDate to current date)
                update_arrears_query = """
                    UPDATE simulation.MonthlyPaymentStatus
                    SET ActualPaymentDate = ?
                    WHERE RunID = ? AND LeaseID = ? AND BillingMonth = ?
                """
                self.db.execute_non_query(update_arrears_query, (
                    current_date, self.run_id, lease_id, arrears_month
                ))

        # Create RentCollection record (extend schema with new columns)
        collection_id = self._next_collection_id
        self._next_collection_id += 1

        insert_query = """
            INSERT INTO simulation.RentCollection
            (RunID, CollectionID, LeaseID, CollectionDate, CollectionMonth,
             AmountCollected, CreatedEventID, PaymentStatus, IsLate, LateFeeAmount,
             PaymentDueDate, ActualPaymentDate, ArrearsAmount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(insert_query, (
            self.run_id, collection_id, lease_id, current_date, billing_month,
            total_amount, event_id, payment_status,
            1 if payment_status == 'LATE' else 0, late_fee,
            billing_month, current_date, arrears
        ))

        # Update Lease payment tracking (reset ConsecutiveMissedPayments)
        self._update_payment_tracking(lease_id, current_date, payment_succeeded=True)

        # Process deposit installment payment
        if self.deposit_manager:
            installment_amount = self.deposit_manager.process_installment_payment(
                lease_id=lease_id,
                payment_date=current_date
            )
            if installment_amount:
                self.logger.debug(f"Processed deposit installment for LeaseID={lease_id}: ${installment_amount:.2f}")

        # TCS credit accrual on ON_TIME payments (subject to deposit-funded gate)
        if self.tenant_credit_manager:
            deposit_funded = (
                self.deposit_manager.is_deposit_funded(lease_id)
                if self.deposit_manager else True
            )
            self.tenant_credit_manager.process_credit_accrual(
                lease_id=lease_id,
                collection_id=collection_id,
                amount_collected=total_amount,
                arrears_amount=arrears,
                late_fee_amount=late_fee,
                payment_status=payment_status,
                deposit_funded=deposit_funded,
                transaction_date=current_date,
                event_id=event_id,
            )

        self.logger.debug(
            f"Collected payment: LeaseID={lease_id}, Status={payment_status}, "
            f"Total=${total_amount:.2f} (Rent: ${amount_due:.2f}, Arrears: ${arrears:.2f}, Late Fee: ${late_fee:.2f})"
        )

        return RentCollectionRecord(
            collection_id=collection_id,
            lease_id=lease_id,
            collection_date=current_date,
            collection_month=billing_month,
            amount_collected=total_amount,
            created_event_id=event_id,
            payment_status=payment_status,
            late_fee_amount=late_fee,
            arrears_amount=arrears
        )

    def _finalize_missed_payments(self, current_date: date) -> None:
        """
        On last day of month, mark unpaid leases as MISSED.

        Args:
            current_date: Last day of month
        """
        billing_month = self._first_of_month(current_date)

        # Find leases with unpaid obligations
        query = """
            SELECT LeaseID, AmountDue, ArrearsAmount
            FROM simulation.MonthlyPaymentStatus
            WHERE RunID = ?
              AND BillingMonth = ?
              AND ActualPaymentDate IS NULL  -- Not paid
        """
        unpaid_leases = self.db.execute_query(query, (self.run_id, billing_month))

        if not unpaid_leases:
            return

        for (lease_id, amount_due, arrears_amount) in unpaid_leases:
            # Mark as MISSED
            update_query = """
                UPDATE simulation.MonthlyPaymentStatus
                SET PaymentStatus = 'MISSED',
                    LateFeeAmount = 0.00
                WHERE RunID = ? AND LeaseID = ? AND BillingMonth = ?
            """
            self.db.execute_non_query(update_query, (self.run_id, lease_id, billing_month))

            # Update Lease payment tracking (increment ConsecutiveMissedPayments)
            self._update_payment_tracking(lease_id, current_date, payment_succeeded=False)

            # Log MISSED event
            self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=current_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.FINANCIAL,
                metadata=f"Payment MISSED - month end, Amount due: ${amount_due:.2f}, Arrears: ${arrears_amount:.2f}"
            )

        self.logger.info(
            f"Finalized MISSED payments on {current_date}: {len(unpaid_leases)} leases"
        )

    # ========== HELPER METHODS ==========

    def _get_active_leases(self, current_date: date) -> List:
        """
        Get all active leases on current_date, plus terminated leases with unpaid obligations.

        Supports post-termination payments (tenants can clear arrears after termination).

        Args:
            current_date: Current simulation date

        Returns:
            List of tuples (LeaseID, EffectiveMonthlyRent, LeaseEndDate, RenewalDecision,
            OccupiedThisMonth).
            The rent column is EffectiveMonthlyRent (current billable
            rent), not the immutable original MonthlyRent.
            OccupiedThisMonth is 1 if the lease was NOT removed
            mid-term in a prior month (eviction or early break with effective exit
            < LeaseEndDate), 0 otherwise. A 0 value means the lease surfaces via the
            arrears arm only; _initialize bills AmountDue=0 and skips the
            redemption path for these leases.
        """
        # Added ORDER BY LeaseID to eliminate latent
        # determinism drift. _initialize_monthly_payment_status consumes self.rng
        # per row, so iteration order MUST be stable across runs. Without explicit
        # ordering, SQL Server's query plan can shift when other table contents
        # change (e.g., added ~6.5K rows to simulation.Event over a
        # 240-month run, which shifted observed iteration order and produced a
        # ~$400K fund delta vs the baseline).
        # LeaseEndDate + RenewalDecision are returned so
        # _initialize_monthly_payment_status can detect "final owed month of
        # a non-renewing lease". RenewalDecision is NULL until
        # day 1 of the lease's final billable month, when lease_renewal_manager
        # pre-rolls and persists it.
        # Bill EffectiveMonthlyRent (= MonthlyRent for leases that
        # have not earned a reduction). MonthlyRent stays the immutable original
        # signed rent; current-rent calculations downstream use the effective value.
        query = """
            SELECT DISTINCT l.LeaseID, l.EffectiveMonthlyRent, l.LeaseEndDate,
                   l.RenewalDecision,
                   -- OccupiedThisMonth flag:
                   -- "occupied this month" = the lease was NOT removed mid-term in a PRIOR
                   -- month. A MID-LEASE removal — eviction (effective exit = execution date)
                   -- or an early lease break (effective exit = break date) — ends occupancy
                   -- BEFORE LeaseEndDate; from the following month on, no new rent is owed
                   -- (only pre-existing arrears settle). End-of-term exits (voluntary-at-end
                   -- / landlord non-renewal) occur AT LeaseEndDate, so their effective exit is
                   -- NOT < LeaseEndDate and OccupiedThisMonth stays 1 through their final month.
                   -- The discriminator is the DATE (effective exit < LeaseEndDate), not
                   -- LeaseStatus — gating on status alone strips the legitimate final/execution
                   -- month (early-break is recorded as TerminationType='VOLUNTARY', so type
                   -- can't distinguish it from an end-of-term exit; the date can).
                   CASE WHEN EXISTS (
                       SELECT 1 FROM simulation.LeaseTermination t
                       WHERE t.RunID = l.RunID AND t.LeaseID = l.LeaseID
                         AND COALESCE(t.EvictionExecutionDate, t.TerminationDate) < l.LeaseEndDate
                         AND COALESCE(t.EvictionExecutionDate, t.TerminationDate) < ?
                   ) THEN 0 ELSE 1 END AS OccupiedThisMonth
            FROM simulation.Lease l
            WHERE l.RunID = ?
              AND l.LeaseStartDate <= ?
              AND (
                  -- Occupied this month: within the date window AND not exited in a prior month.
                  ( l.LeaseEndDate >= ?
                    AND NOT EXISTS (
                        SELECT 1 FROM simulation.LeaseTermination t
                        WHERE t.RunID = l.RunID AND t.LeaseID = l.LeaseID
                          AND COALESCE(t.EvictionExecutionDate, t.TerminationDate) < l.LeaseEndDate
                          AND COALESCE(t.EvictionExecutionDate, t.TerminationDate) < ?
                    ) )
                  OR
                  -- Terminated leases with unpaid MISSED arrears — surface for
                  -- ARREARS settlement ONLY; _initialize bills $0 (OccupiedThisMonth=0), so no
                  -- NEW full-rent obligation is created.
                  EXISTS (
                      SELECT 1
                      FROM simulation.MonthlyPaymentStatus mps
                      WHERE mps.RunID = l.RunID
                        AND mps.LeaseID = l.LeaseID
                        AND mps.PaymentStatus = 'MISSED'
                        AND mps.ActualPaymentDate IS NULL
                  )
              )
            ORDER BY l.LeaseID
        """
        # Params in text order: CASE-exit-date, RunID, LeaseStartDate, LeaseEndDate, active-arm-exit-date.
        result = self.db.execute_query(
            query, (current_date, self.run_id, current_date, current_date, current_date)
        )
        return result

    def _calculate_arrears(self, lease_id: int, current_date: date) -> Decimal:
        """
        Calculate total arrears (unpaid rent from previous months) for a lease.

        This method returns aggregated total for backward compatibility.
        For FIFO-ordered arrears, use _get_arrears_fifo().

        Logic:
        - Query MonthlyPaymentStatus for all MISSED months before current month
        - Sum AmountDue from MISSED records

        Args:
            lease_id: Lease ID
            current_date: Current simulation date

        Returns:
            Total arrears amount
        """
        billing_month = self._first_of_month(current_date)

        query = """
            SELECT ISNULL(SUM(AmountDue), 0) AS TotalArrears
            FROM simulation.MonthlyPaymentStatus
            WHERE RunID = ?
              AND LeaseID = ?
              AND BillingMonth < ?
              AND PaymentStatus = 'MISSED'
              AND ActualPaymentDate IS NULL  -- Exclude arrears satisfied in later payments
        """
        result = self.db.execute_query(query, (self.run_id, lease_id, billing_month))

        if result and len(result) > 0:
            return Decimal(str(result[0][0]))
        else:
            return Decimal('0.00')

    def _get_arrears_fifo(self, lease_id: int, current_date: date) -> List[tuple[date, Decimal]]:
        """
        Get arrears in FIFO order (oldest first) for a lease.

        Returns list of (billing_month, amount) tuples ordered by BillingMonth ASC.

        Args:
            lease_id: Lease ID
            current_date: Current simulation date

        Returns:
            List of (billing_month, amount) tuples, oldest first
        """
        billing_month = self._first_of_month(current_date)

        query = """
            SELECT BillingMonth, AmountDue
            FROM simulation.MonthlyPaymentStatus
            WHERE RunID = ?
              AND LeaseID = ?
              AND BillingMonth < ?
              AND PaymentStatus = 'MISSED'
              AND ActualPaymentDate IS NULL  -- Exclude arrears satisfied in later payments
            ORDER BY BillingMonth ASC
        """
        result = self.db.execute_query(query, (self.run_id, lease_id, billing_month))

        if result:
            return [(row[0], Decimal(str(row[1]))) for row in result]
        else:
            return []

    def _first_of_month(self, current_date: date) -> date:
        """Get first day of month for current_date"""
        return current_date.replace(day=1)

    def _is_last_day_of_month(self, current_date: date) -> bool:
        """Check if current_date is the last day of the month"""
        last_day = monthrange(current_date.year, current_date.month)[1]
        return current_date.day == last_day

    # ========== REDEMPTION HELPERS ==========

    def _lookup_household_for_lease(self, lease_id: int) -> int:
        """Resolve HouseholdID from LeaseID for the current run."""
        result = self.db.execute_query(
            "SELECT HouseholdID FROM simulation.Lease WHERE RunID = ? AND LeaseID = ?",
            (self.run_id, lease_id),
        )
        return result[0][0] if result else None

    def _process_redemption_at_init(
        self,
        lease_id: int,
        monthly_rent: Decimal,
        can_pay_this_month: bool,
        billing_month: date,
        grace_period_end_date: date,
        arrears: Decimal,
        current_date: date,
        path: str,
    ) -> None:
        """
        Write the satisfied-month state for a redeeming lease.

        Creates three coordinated rows for a single month:
          1. simulation.MonthlyPaymentStatus — PaymentStatus='REDEEMED',
             ActualPaymentDate=current_date (excludes from `_attempt_daily_collection`
             and `_finalize_missed_payments`)
          2. simulation.RentCollection — PaymentStatus='REDEEMED', AmountCollected=0
          3. simulation.TenantCreditLedger — TransactionType='REDEMPTION',
             Amount=-monthly_rent (via process_credit_redemption)

        Plus a TENANT_CREDIT audit event from the TCS manager.
        """
        household_id = self._lookup_household_for_lease(lease_id)

        # 1. Event for the RentCollection row (FINANCIAL action type)
        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.FINANCIAL,
            amount=Decimal("0.00"),
            metadata=(
                f"RENT/REDEEMED ({path}): LeaseID={lease_id}, "
                f"HouseholdID={household_id}, MonthlyRent=${monthly_rent:.2f}"
            ),
        )

        # 2. MonthlyPaymentStatus row — satisfied at day 1
        self.db.execute_non_query(
            """
            INSERT INTO simulation.MonthlyPaymentStatus
            (RunID, LeaseID, BillingMonth, CanPayThisMonth, PaymentDueDate,
             GracePeriodEndDate, AmountDue, ArrearsAmount,
             PaymentStatus, ActualPaymentDate, LateFeeAmount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REDEEMED', ?, 0)
            """,
            (
                self.run_id, lease_id, billing_month, can_pay_this_month,
                billing_month, grace_period_end_date, monthly_rent, arrears,
                current_date,
            ),
        )

        # 3. RentCollection row — $0 collected, REDEEMED status
        collection_id = self._next_collection_id
        self._next_collection_id += 1
        self.db.execute_non_query(
            """
            INSERT INTO simulation.RentCollection
            (RunID, CollectionID, LeaseID, CollectionDate, CollectionMonth,
             AmountCollected, CreatedEventID, PaymentStatus, IsLate, LateFeeAmount,
             PaymentDueDate, ActualPaymentDate, ArrearsAmount)
            VALUES (?, ?, ?, ?, ?, 0, ?, 'REDEEMED', 0, 0, ?, ?, 0)
            """,
            (
                self.run_id, collection_id, lease_id, current_date, billing_month,
                event_id, billing_month, current_date,
            ),
        )

        # 4. TCS ledger REDEMPTION row + balance update + audit event
        self.tenant_credit_manager.process_credit_redemption(
            household_id=household_id,
            lease_id=lease_id,
            collection_id=collection_id,
            monthly_rent=monthly_rent,
            transaction_date=current_date,
            event_id=event_id,
            path=path,
        )

        self.logger.debug(
            f"Redemption ({path}): LeaseID={lease_id}, HouseholdID={household_id}, "
            f"MonthlyRent=${monthly_rent:.2f}, CanPay={can_pay_this_month}"
        )

    def _update_payment_tracking(self, lease_id: int, current_date: date, payment_succeeded: bool):
        """
        Update Lease payment tracking fields.

        Updates:
        - ConsecutiveMissedPayments (reset to 0 on success, increment on failure)
        - LastPaymentDate (update on success, leave unchanged on failure)

        Args:
            lease_id: Lease to update
            current_date: Current simulation date
            payment_succeeded: True if payment succeeded, False if failed
        """
        if payment_succeeded:
            # Reset consecutive missed counter, update last payment date
            update_query = """
                UPDATE simulation.Lease
                SET ConsecutiveMissedPayments = 0,
                    LastPaymentDate = ?
                WHERE RunID = ? AND LeaseID = ?
            """
            self.db.execute_non_query(update_query, (current_date, self.run_id, lease_id))
            self.logger.debug(f"Payment tracking updated for LeaseID={lease_id}: ConsecutiveMissedPayments=0, LastPaymentDate={current_date}")
        else:
            # Increment consecutive missed counter (do not update last payment date)
            update_query = """
                UPDATE simulation.Lease
                SET ConsecutiveMissedPayments = ConsecutiveMissedPayments + 1
                WHERE RunID = ? AND LeaseID = ?
            """
            self.db.execute_non_query(update_query, (self.run_id, lease_id))

            # Log the new value for debugging
            query = "SELECT ConsecutiveMissedPayments FROM simulation.Lease WHERE RunID = ? AND LeaseID = ?"
            result = self.db.execute_query(query, (self.run_id, lease_id))
