"""
Eviction Manager - Deterministic eviction logic based on payment history

This module implements Liberty Bee's eviction system:
1. Check for eviction triggers (3 consecutive missed payments)
2. File eviction notices (Month N when trigger occurs)
3. Execute evictions (Month N+2, 2-month delay)
4. Charge eviction costs ($1,500 from Cash)
5. Write off arrears at termination

Key Features:
- Deterministic eviction based on payment history (no probability)
- 3 consecutive missed payments → eviction filed
- 2-month delay between filing and execution
- Complete audit trail via event logging and LeaseTerminationLedger
- Integration with FundManager for eviction costs

Business Rules:
- Trigger: 3 consecutive months of non-payment (not paid in full by month end)
- Timeline: Month N (3rd miss) → EVICTION_FILED, Month N+2 → EVICTION_EXECUTED
- Costs: Flat $1,500 eviction cost (paid from Cash)
- Arrears: Unpaid rent written off at termination (logged as arrears_at_exit)

Usage:
- Use EvictionManager.process_evictions() in daily simulation loop
- Checks all active leases for eviction status
- Files and executes evictions according to timeline
"""

import logging
from typing import List, Dict, Optional
from datetime import date
from dateutil.relativedelta import relativedelta
from decimal import Decimal

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from fund_manager import FundManager
from event_logger import EventLogger, EventType, EntityType, ActionType
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from security_deposit_manager import SecurityDepositManager
    from turnover_manager import TurnoverManager
    from tenant_credit_manager import TenantCreditManager


class EvictionManager:
    """Manage deterministic eviction processing based on payment history"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        fund_manager: FundManager,
        event_logger: EventLogger,
        deposit_manager: "SecurityDepositManager" = None,
        turnover_manager: "TurnoverManager" = None,
        tenant_credit_manager: "TenantCreditManager" = None,
    ):
        self.db = db_manager
        self.fund_manager = fund_manager
        self.event_logger = event_logger
        self.deposit_manager = deposit_manager
        self.turnover_manager = turnover_manager
        # TenantCreditManager for credit-forfeiture-on-eviction
        # (forfeit at execution date). Optional for backward
        # compat with older tests that construct EvictionManager directly.
        self.tenant_credit_manager = tenant_credit_manager
        self.logger = logging.getLogger(__name__)

        # Eviction policy constants from the registry (LEASE.*,
        # global) — were class constants EVICTION_COST / CONSECUTIVE_MISSED_THRESHOLD
        # / EXECUTION_DELAY_MONTHS. Read-once, fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.eviction_cost = self.registry.get_decimal('LEASE', 'EvictionCost')
        self.consecutive_missed_threshold = self.registry.get_int('LEASE', 'EvictionMissedThreshold')
        self.execution_delay_months = self.registry.get_int('LEASE', 'EvictionExecutionDelayMonths')

    def process_evictions(self, run_id: int, current_date: date) -> Dict[str, List]:
        """
        Process all eviction activities for current date.

        Checks:
        1. New eviction filings (3 consecutive missed payments)
        2. Eviction executions (2 months after filing)

        Args:
            run_id: Current simulation run ID
            current_date: Current simulation date

        Returns:
            Dict with 'filed' and 'executed' lists of lease IDs
        """
        results = {
            'filed': [],
            'executed': []
        }

        # Check for new eviction filings
        leases_to_file = self.check_eviction_status(run_id, current_date)
        for lease_info in leases_to_file:
            self.file_eviction(
                run_id=run_id,
                lease_id=lease_info['lease_id'],
                file_date=current_date,
                arrears=lease_info['arrears']
            )
            results['filed'].append(lease_info['lease_id'])

        # Check for eviction executions (filed 2 months ago)
        leases_to_execute = self.check_pending_executions(run_id, current_date)
        for lease_info in leases_to_execute:
            self.execute_eviction(
                run_id=run_id,
                lease_id=lease_info['lease_id'],
                execution_date=current_date,
                arrears=lease_info['arrears']
            )
            results['executed'].append(lease_info['lease_id'])

        return results

    def check_eviction_status(self, run_id: int, current_date: date) -> List[Dict]:
        """
        Check all active leases for eviction triggers.

        Eviction trigger: 3 consecutive months of non-payment
        - Month 1: Rent due, not paid
        - Month 2: Rent due, not paid
        - Month 3: Rent due, not paid → Eviction filed on Month 3

        Logic:
        - Check Lease.ConsecutiveMissedPayments >= 3
        - Exclude leases already in eviction process
        - Return leases that need eviction filing TODAY

        Args:
            run_id: Current simulation run ID
            current_date: Current simulation date

        Returns:
            List of dicts with lease_id and arrears for leases needing eviction filing
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Query leases with 3+ consecutive missed payments
            # that haven't already been filed for eviction
            query = """
                SELECT
                    l.LeaseID,
                    l.ConsecutiveMissedPayments,
                    l.LastPaymentDate,
                    l.EffectiveMonthlyRent
                FROM simulation.Lease l
                WHERE l.RunID = ?
                    AND l.LeaseStatus = 'Active'
                    AND l.ConsecutiveMissedPayments >= ?
                    AND NOT EXISTS (
                        SELECT 1
                        FROM simulation.LeaseTermination lt
                        WHERE lt.RunID = l.RunID
                            AND lt.LeaseID = l.LeaseID
                    )
                    AND ? >= l.LeaseStartDate
            """

            cursor.execute(query, (run_id, self.consecutive_missed_threshold, current_date))
            rows = cursor.fetchall()

            results = []
            for row in rows:
                lease_id = row.LeaseID
                consecutive_missed = row.ConsecutiveMissedPayments
                # Arrears estimate uses current billable rent.
                monthly_rent = Decimal(str(row.EffectiveMonthlyRent))

                # Calculate arrears (consecutive missed payments * monthly rent)
                arrears = monthly_rent * consecutive_missed

                results.append({
                    'lease_id': lease_id,
                    'consecutive_missed': consecutive_missed,
                    'arrears': arrears
                })

                self.logger.info(
                    f"Eviction trigger detected - LeaseID: {lease_id}, "
                    f"ConsecutiveMissedPayments: {consecutive_missed}, "
                    f"Arrears: ${arrears:,.2f}"
                )

            return results

    def file_eviction(self, run_id: int, lease_id: int, file_date: date, arrears: Decimal) -> int:
        """
        File eviction notice for a lease.

        Actions:
        1. Log EVICTION_FILED event
        2. Insert LeaseTerminationLedger entry (EVICTION_FILED)
        3. Create LeaseTermination record with EvictionFiledDate

        Args:
            run_id: Current simulation run ID
            lease_id: Lease to file eviction for
            file_date: Date eviction is filed (Month N)
            arrears: Current arrears amount

        Returns:
            EventID of eviction filed event
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Log event
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=file_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.COMPLETE,
                amount=arrears,
                metadata=f"Eviction filed - {self.consecutive_missed_threshold} consecutive missed payments, arrears: ${arrears:,.2f}"
            )

            # Get next LedgerID for LeaseTerminationLedger
            cursor.execute("""
                SELECT ISNULL(MAX(LedgerID), 0) + 1
                FROM simulation.LeaseTerminationLedger
                WHERE RunID = ?
            """, (run_id,))
            ledger_id = cursor.fetchone()[0]

            # Insert LeaseTerminationLedger entry
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                ledger_id,
                lease_id,
                event_id,
                file_date,
                'EVICTION_FILED',
                f'Eviction filed - {self.consecutive_missed_threshold} consecutive missed payments',
                arrears,
                f'{{"arrears": {float(arrears)}, "consecutive_missed": {self.consecutive_missed_threshold}}}'
            ))

            # Calculate execution date (2 months from filing)
            execution_date = file_date + relativedelta(months=self.execution_delay_months)

            # Create LeaseTermination record
            cursor.execute("""
                INSERT INTO simulation.LeaseTermination
                (RunID, LeaseID, TerminationType, TerminationDate, TerminationReason,
                 ArrearsAtExit, EvictionFiledDate, EvictionExecutionDate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                lease_id,
                'EVICTION',
                execution_date,  # Will be executed 2 months from now
                f'Eviction filed - {self.consecutive_missed_threshold} consecutive missed payments',
                arrears,
                file_date,
                execution_date
            ))

            # Mark unit as Pending_Move_Out during eviction proceedings
            # (unit unavailable for leasing throughout the eviction window)
            cursor.execute("""
                UPDATE simulation.PropertyUnits
                SET UnitStatus = 'Pending_Move_Out'
                WHERE RunID = ? AND UnitID = (
                    SELECT UnitID FROM simulation.Lease
                    WHERE RunID = ? AND LeaseID = ?
                )
            """, (run_id, run_id, lease_id))

            conn.commit()

            self.logger.info(
                f"Eviction filed - RunID: {run_id}, LeaseID: {lease_id}, "
                f"FileDate: {file_date}, ExecutionDate: {execution_date}, "
                f"Arrears: ${arrears:,.2f}, EventID: {event_id}"
            )

            return event_id

    def check_pending_executions(self, run_id: int, current_date: date) -> List[Dict]:
        """
        Check for evictions that should be executed today.

        Logic:
        - Find LeaseTermination records with TerminationType='EVICTION'
        - Where EvictionExecutionDate = current_date
        - That haven't been executed yet (check if lease still Active)

        Args:
            run_id: Current simulation run ID
            current_date: Current simulation date

        Returns:
            List of dicts with lease_id and arrears for leases to execute
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    lt.LeaseID,
                    lt.ArrearsAtExit,
                    l.LeaseStatus
                FROM simulation.LeaseTermination lt
                INNER JOIN simulation.Lease l ON l.RunID = lt.RunID AND l.LeaseID = lt.LeaseID
                WHERE lt.RunID = ?
                    AND lt.TerminationType = 'EVICTION'
                    AND lt.EvictionExecutionDate = ?
                    AND l.LeaseStatus = 'Active'
            """

            cursor.execute(query, (run_id, current_date))
            rows = cursor.fetchall()

            results = []
            for row in rows:
                results.append({
                    'lease_id': row.LeaseID,
                    'arrears': Decimal(str(row.ArrearsAtExit))
                })

            return results

    def execute_eviction(self, run_id: int, lease_id: int, execution_date: date, arrears: Decimal) -> Dict:
        """
        Execute eviction (Month N+2 after filing).

        Actions:
        1. Terminate lease (set LeaseStatus = 'Terminated')
        2. Charge eviction cost ($1,500 from Cash via FundManager)
        3. Write off arrears (logged but not collected)
        4. Log EVICTION_EXECUTED event
        5. Insert LeaseTerminationLedger entries (EVICTION_EXECUTED, EVICTION_COST, ARREARS_WRITE_OFF)
        6. Update LeaseTermination record with final dates

        Args:
            run_id: Current simulation run ID
            lease_id: Lease to execute eviction for
            execution_date: Date eviction is executed (Month N+2)
            arrears: Final arrears amount at execution

        Returns:
            Dict with termination details
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Log EVICTION_EXECUTED event
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=execution_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.COMPLETE,
                amount=arrears,
                metadata=f"Eviction executed - Lease terminated, arrears written off: ${arrears:,.2f}, eviction cost: ${self.eviction_cost:,.2f}"
            )

            # Terminate lease
            cursor.execute("""
                UPDATE simulation.Lease
                SET LeaseStatus = 'Terminated',
                    LastPaymentDate = NULL
                WHERE RunID = ? AND LeaseID = ?
            """, (run_id, lease_id))

            # Forfeit any remaining TCS credit BEFORE the eviction
            # cost step (eviction execution = full forfeiture).
            # Done here, before the cost call, so the audit trail orders as
            # (1) lease terminated, (2) credit forfeited, (3) cost paid.
            if self.tenant_credit_manager is not None:
                cursor.execute(
                    "SELECT HouseholdID FROM simulation.Lease WHERE RunID = ? AND LeaseID = ?",
                    (run_id, lease_id)
                )
                hh_row = cursor.fetchone()
                if hh_row and hh_row[0] is not None:
                    self.tenant_credit_manager.process_credit_forfeiture_eviction(
                        household_id=hh_row[0],
                        eviction_event_id=event_id,
                        execution_date=execution_date,
                    )

            # Charge eviction cost from Cash
            # Log event for eviction cost
            eviction_cost_event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=execution_date,
                entity_type=EntityType.FUND,
                entity_id=lease_id,
                action_type=ActionType.FINANCIAL,
                amount=self.eviction_cost,
                metadata=f"Eviction cost for LeaseID {lease_id} - ${self.eviction_cost:,.2f}"
            )

            # Eviction cost is a protected obligation.
            # Once eviction execution reaches the cost step, it's a downstream
            # legal/process obligation triggered by the tenant lifecycle. CSF
            # backstops Cash if Cash alone is insufficient.
            self.fund_manager.process_expense_protected(
                run_id=run_id,
                expense_amount=self.eviction_cost,
                ledger_date=execution_date,
                expense_type='Eviction Cost'
            )

            # Get next LedgerID for LeaseTerminationLedger entries
            cursor.execute("""
                SELECT ISNULL(MAX(LedgerID), 0) + 1
                FROM simulation.LeaseTerminationLedger
                WHERE RunID = ?
            """, (run_id,))
            ledger_id = cursor.fetchone()[0]

            # Insert LeaseTerminationLedger entry: EVICTION_EXECUTED
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                ledger_id,
                lease_id,
                event_id,
                execution_date,
                'EVICTION_EXECUTED',
                f'Eviction executed - Lease terminated',
                Decimal('0.00'),  # No cash transaction for execution itself
                f'{{"lease_id": {lease_id}}}'
            ))

            ledger_id += 1

            # Insert LeaseTerminationLedger entry: EVICTION_COST
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                ledger_id,
                lease_id,
                eviction_cost_event_id,
                execution_date,
                'EVICTION_COST',
                f'Eviction cost charged to Cash',
                -self.eviction_cost,  # Negative = expense
                f'{{"eviction_cost": {float(self.eviction_cost)}}}'
            ))

            ledger_id += 1

            # Insert LeaseTerminationLedger entry: ARREARS_WRITE_OFF
            cursor.execute("""
                INSERT INTO simulation.LeaseTerminationLedger
                (RunID, LedgerID, LeaseID, EventID, TxnDate, TxnType, Description, Amount, Metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                ledger_id,
                lease_id,
                event_id,
                execution_date,
                'ARREARS_WRITE_OFF',
                f'Arrears written off at eviction - ${arrears:,.2f}',
                arrears,  # Positive = amount written off (not collected)
                f'{{"arrears_written_off": {float(arrears)}}}'
            ))

            # Update LeaseTermination record (already exists from filing)
            # No need to update - EvictionExecutionDate already set during filing

            conn.commit()

            self.logger.info(
                f"Eviction executed - RunID: {run_id}, LeaseID: {lease_id}, "
                f"ExecutionDate: {execution_date}, ArrearsWrittenOff: ${arrears:,.2f}, "
                f"EvictionCost: ${self.eviction_cost:,.2f}, EventID: {event_id}"
            )

        # Queue deposit settlement after eviction (outside conn context)
        if self.deposit_manager:
            self.deposit_manager.settle_deposit_at_termination(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=execution_date,
                termination_type="EVICTION"
            )

        # Trigger turnover workflow after settlement is committed
        if self.turnover_manager:
            self.turnover_manager.trigger_turnover(
                run_id=run_id,
                lease_id=lease_id,
                termination_date=execution_date,
                termination_type="EVICTION"
            )

        return {
            'lease_id': lease_id,
            'execution_date': execution_date,
            'arrears_written_off': arrears,
            'eviction_cost': self.eviction_cost,
            'event_id': event_id
        }

