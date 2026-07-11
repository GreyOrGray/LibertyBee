"""
Security Deposit Management Module for Liberty Bee Housing Simulation

Purpose: Manages security deposit collection, escrow accounting, and settlement

Key Responsibilities:
- Initialize deposit records at lease creation
- Process full payment or installment payments
- Track deposit funding status (deposit_funded gate for TCS)
- Maintain complete deposit ledger (audit trail)
- Manage deposit escrow accounting (separate from operating funds)
- Settle deposits at lease termination

Design Principles:
- No IDENTITY columns - manual ID assignment for run isolation
- Compound primary keys (RunID, LeaseID) and (RunID, LedgerID)
- Event logging integration for all deposit transactions
- Ledger is source of truth, LeaseDeposit.DepositEscrowedAmount is cached balance
- Behavioral determinism (same seed -> same deposit method and damage decisions)

Scope:
- Deposit amount = FirstMonthRent at lease signing (calculated once, never changes)
- Payment options: FULL (immediate) or INSTALLMENT (4 monthly payments) - immutable
- Monthly obligation = rent + deposit_installment (atomic payment, no separate grace period)
- TCS credit gate: deposit_funded AND month_index >= 5
- deposit_funded = (DepositEscrowedAmount >= DepositRequiredAmount)
- Early termination before fully funded -> forfeit all installments

Scope (added):
- settle_deposit_at_termination(): called by eviction/renewal managers at termination
- process_pending_settlements(): called daily in simulation.py
- Two-step model: outcome decided at termination, cash movement after DEP_SettlementDelayDays
- Forfeiture (unfunded): immediate (SettlementDueDate = termination_date)
- Eviction-biased damage probability vs voluntary/non-renewal probability
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, List, Any
from enum import Enum
import random

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from event_logger import EventLogger, EventType, EntityType, ActionType
from fund_manager import FundManager


class DepositMethod(Enum):
    """Deposit payment method (chosen at lease signing, immutable)"""
    FULL = "FULL"
    INSTALLMENT = "INSTALLMENT"


class DepositStatus(Enum):
    """Deposit lifecycle status"""
    ACTIVE = "ACTIVE"
    REFUNDED = "REFUNDED"
    FORFEITED = "FORFEITED"
    APPLIED_TO_DAMAGES = "APPLIED_TO_DAMAGES"


class TxnType(Enum):
    """Deposit ledger transaction types"""
    FULL_PAYMENT = "FULL_PAYMENT"
    INSTALLMENT_PAYMENT = "INSTALLMENT_PAYMENT"
    REFUND = "REFUND"
    FORFEITURE = "FORFEITURE"
    DAMAGE_WITHHOLD = "DAMAGE_WITHHOLD"


# Termination types that use eviction-biased damage probability
EVICTION_TERMINATION_TYPES = {"EVICTION"}


@dataclass
class DepositRecord:
    """Represents deposit state for a lease"""
    lease_id: int
    deposit_required_amount: Decimal
    deposit_escrowed_amount: Decimal
    deposit_method: DepositMethod
    installments_planned: int
    installments_paid_count: int
    deposit_funded_date: Optional[date]
    status: DepositStatus


@dataclass
class LedgerEntry:
    """Represents a deposit ledger transaction"""
    ledger_id: int
    lease_id: int
    txn_date: date
    txn_type: TxnType
    amount: Decimal
    balance_after: Decimal
    event_id: Optional[int]
    notes: Optional[str]


@dataclass
class SettlementResult:
    """Result of a processed deposit settlement"""
    lease_id: int
    settlement_date: date
    outcome: str
    escrowed_amount: Decimal
    damage_amount: Decimal
    return_amount: Decimal


class SecurityDepositManager:
    """Manages security deposit collection, escrow accounting, and settlement"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_logger: EventLogger,
        fund_manager: FundManager,
        run_id: int,
        seed: int,
        # DEP_ settlement parameters — via reference.ParameterRegistry / ProjectionConfig
        eviction_damage_prob: float = 0.30,
        voluntary_damage_prob: float = 0.10,
        damage_min_percent: float = 0.10,
        damage_max_percent: float = 0.50,
        settlement_delay_days: int = 30,
        logger: Any = None
    ):
        self.db = db_manager
        self.event_logger = event_logger
        self.fund_manager = fund_manager
        self.run_id = run_id
        self.logger = logger
        self.rng = random.Random(seed + 30602)  # offset

        self.eviction_damage_prob = eviction_damage_prob
        self.voluntary_damage_prob = voluntary_damage_prob
        self.damage_min_percent = damage_min_percent
        self.damage_max_percent = damage_max_percent
        self.settlement_delay_days = settlement_delay_days

        # Counters for ID assignment
        self._next_ledger_id = 1

        # deposit installment params from the registry (DEP.*,
        # global) — were hardcoded (P=0.70 INSTALLMENT vs FULL; 4 installments).
        # Read-once, fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.installment_probability = self.registry.get_float('DEP', 'InstallmentProbability')
        self.installment_count = self.registry.get_int('DEP', 'InstallmentCount')

    def initialize_deposit_for_lease(
        self,
        lease_id: int,
        monthly_rent: Decimal,
        lease_signed_date: date
    ) -> DepositRecord:
        """
        Initialize deposit record when lease is created.

        Business Rules:
        - DepositRequiredAmount = FirstMonthRent at lease signing
        - Tenant chooses FULL or INSTALLMENT (simulated via probability)
        - Choice is immutable once made
        """
        deposit_required_amount = monthly_rent

        deposit_method = (
            DepositMethod.INSTALLMENT
            if self.rng.random() < self.installment_probability
            else DepositMethod.FULL
        )

        if self.event_logger:
            init_event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=lease_signed_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.CREATE,
                metadata=f"Deposit initialized: Method={deposit_method.value}, Required=${deposit_required_amount:.2f}"
            )
        else:
            init_event_id = None

        insert_query = """
            INSERT INTO simulation.LeaseDeposit (
                RunID, LeaseID, DepositRequiredAmount, DepositEscrowedAmount,
                DepositMethod, InstallmentsPlanned, InstallmentsPaidCount,
                DepositFundedDate, Status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                lease_id,
                deposit_required_amount,
                Decimal("0.00"),
                deposit_method.value,
                self.installment_count,
                0,
                None,
                DepositStatus.ACTIVE.value
            )
        )

        if deposit_method == DepositMethod.FULL:
            self._process_full_payment(
                lease_id=lease_id,
                deposit_required_amount=deposit_required_amount,
                payment_date=lease_signed_date
            )

        return DepositRecord(
            lease_id=lease_id,
            deposit_required_amount=deposit_required_amount,
            deposit_escrowed_amount=(
                deposit_required_amount if deposit_method == DepositMethod.FULL else Decimal("0.00")
            ),
            deposit_method=deposit_method,
            installments_planned=4,
            installments_paid_count=(1 if deposit_method == DepositMethod.FULL else 0),
            deposit_funded_date=(lease_signed_date if deposit_method == DepositMethod.FULL else None),
            status=DepositStatus.ACTIVE
        )

    def _process_full_payment(
        self,
        lease_id: int,
        deposit_required_amount: Decimal,
        payment_date: date
    ) -> None:
        """Process full deposit payment at lease signing."""
        self._create_ledger_entry(
            lease_id=lease_id,
            txn_date=payment_date,
            txn_type=TxnType.FULL_PAYMENT,
            amount=deposit_required_amount,
            balance_after=deposit_required_amount,
            notes=f"Full deposit payment: ${deposit_required_amount:.2f}"
        )

        update_query = """
            UPDATE simulation.LeaseDeposit
            SET DepositEscrowedAmount = ?,
                DepositFundedDate = ?,
                InstallmentsPaidCount = 1,
                UpdatedAt = GETDATE()
            WHERE RunID = ? AND LeaseID = ?
        """
        self.db.execute_non_query(
            update_query,
            (deposit_required_amount, payment_date, self.run_id, lease_id)
        )

        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=payment_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.FINANCIAL,
            amount=deposit_required_amount,
            metadata="DEPOSIT/FULL_PAYMENT"
        )

        self.fund_manager.deposit_to_escrow(
            run_id=self.run_id,
            deposit_amount=deposit_required_amount,
            ledger_date=payment_date,
            lease_id=lease_id,
            event_id=event_id
        )

        if self.logger:
            self.logger.info(
                f"Full deposit payment processed: LeaseID={lease_id}, "
                f"Amount=${deposit_required_amount:.2f}, Date={payment_date}"
            )

    def process_installment_payment(
        self,
        lease_id: int,
        payment_date: date
    ) -> Optional[Decimal]:
        """
        Process monthly deposit installment payment.

        Returns:
            Installment amount if payment processed, None if deposit already funded
        """
        query = """
            SELECT
                DepositRequiredAmount,
                DepositEscrowedAmount,
                DepositMethod,
                InstallmentsPaidCount,
                DepositFundedDate
            FROM simulation.LeaseDeposit
            WHERE RunID = ? AND LeaseID = ?
        """
        result = self.db.execute_query(query, (self.run_id, lease_id))

        if not result:
            if self.logger:
                self.logger.error(f"Deposit record not found for LeaseID={lease_id}")
            return None

        (
            deposit_required_amount,
            deposit_escrowed_amount,
            deposit_method,
            installments_paid_count,
            deposit_funded_date
        ) = result[0]

        if deposit_funded_date is not None:
            return None

        if deposit_method != DepositMethod.INSTALLMENT.value:
            if self.logger:
                self.logger.warning(
                    f"Attempted installment payment for FULL deposit method: LeaseID={lease_id}"
                )
            return None

        # Use remaining-balance-aware installment logic so the final installment
        # pays the exact remaining amount. Avoids residual-penny accumulation
        # under arbitrary monthly_rent values (rent inflation, future rent paths)
        # that would otherwise overflow InstallmentsPaidCount past 4.
        installments_planned = Decimal(self.installment_count)
        remaining_balance = deposit_required_amount - deposit_escrowed_amount
        remaining_installments = int(installments_planned) - installments_paid_count

        if remaining_installments == 1:
            installment_amount = remaining_balance
        else:
            installment_amount = (deposit_required_amount / installments_planned).quantize(Decimal("0.01"))

        new_escrow_balance = deposit_escrowed_amount + installment_amount
        new_installment_count = installments_paid_count + 1
        is_now_funded = new_escrow_balance >= deposit_required_amount
        funded_date = payment_date if is_now_funded else None

        update_query = """
            UPDATE simulation.LeaseDeposit
            SET DepositEscrowedAmount = ?,
                InstallmentsPaidCount = ?,
                DepositFundedDate = ?,
                UpdatedAt = GETDATE()
            WHERE RunID = ? AND LeaseID = ?
        """
        self.db.execute_non_query(
            update_query,
            (new_escrow_balance, new_installment_count, funded_date, self.run_id, lease_id)
        )

        self._create_ledger_entry(
            lease_id=lease_id,
            txn_date=payment_date,
            txn_type=TxnType.INSTALLMENT_PAYMENT,
            amount=installment_amount,
            balance_after=new_escrow_balance,
            notes=f"Installment {new_installment_count}/{self.installment_count}: ${installment_amount:.2f}"
        )

        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=payment_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.FINANCIAL,
            amount=installment_amount,
            metadata=f"DEPOSIT/INSTALLMENT_PAYMENT: Installment {new_installment_count}/{self.installment_count}"
        )

        self.fund_manager.deposit_to_escrow(
            run_id=self.run_id,
            deposit_amount=installment_amount,
            ledger_date=payment_date,
            lease_id=lease_id,
            event_id=event_id
        )

        if self.logger:
            funded_status = "FUNDED" if is_now_funded else f"{new_installment_count}/{self.installment_count} paid"
            self.logger.info(
                f"Installment payment processed: LeaseID={lease_id}, "
                f"Amount=${installment_amount:.2f}, Status={funded_status}"
            )

        return installment_amount

    # ============================================================
    # Settlement at Termination
    # ============================================================

    def settle_deposit_at_termination(
        self,
        run_id: int,
        lease_id: int,
        termination_date: date,
        termination_type: str
    ) -> None:
        """
        Record settlement outcome at lease termination.

        Two-step model: outcome is decided now, cash movement happens on SettlementDueDate.

        Business Rules:
        - Unfunded deposit: outcome = FORFEITURE, SettlementDueDate = termination_date (immediate)
        - Funded deposit: assess damages, set PENDING, SettlementDueDate = termination_date + delay
        - EVICTION uses eviction_damage_prob; all others use voluntary_damage_prob

        Args:
            run_id: Simulation run ID (should match self.run_id)
            lease_id: Lease being terminated
            termination_date: Date of termination
            termination_type: One of EVICTION, VOLUNTARY, EARLY_BREAK, LANDLORD_NONRENEWAL
        """
        query = """
            SELECT DepositRequiredAmount, DepositEscrowedAmount, DepositFundedDate, SettlementStatus
            FROM simulation.LeaseDeposit
            WHERE RunID = ? AND LeaseID = ?
        """
        result = self.db.execute_query(query, (self.run_id, lease_id))

        if not result:
            if self.logger:
                self.logger.warning(f"No deposit record found for LeaseID={lease_id} at termination")
            return

        deposit_required_amount, deposit_escrowed_amount, deposit_funded_date, settlement_status = result[0]
        deposit_required_amount = Decimal(str(deposit_required_amount))
        deposit_escrowed_amount = Decimal(str(deposit_escrowed_amount))

        # Skip if already settled (defensive guard)
        if settlement_status is not None:
            return

        is_fully_funded = deposit_funded_date is not None

        if not is_fully_funded:
            # Unfunded: immediate forfeiture via same-day processor
            settlement_outcome = "FORFEITURE"
            settlement_due_date = termination_date
            damage_amount = Decimal("0.00")
        else:
            # Funded: assess damages, defer settlement
            settlement_outcome, damage_amount = self._assess_damages(
                termination_type=termination_type,
                deposit_required_amount=deposit_required_amount,
                deposit_escrowed_amount=deposit_escrowed_amount
            )
            settlement_due_date = termination_date + timedelta(days=self.settlement_delay_days)

        update_query = """
            UPDATE simulation.LeaseDeposit
            SET SettlementStatus = 'PENDING',
                SettlementDueDate = ?,
                DamageAmount = ?,
                SettlementOutcome = ?,
                UpdatedAt = GETDATE()
            WHERE RunID = ? AND LeaseID = ?
        """
        self.db.execute_non_query(
            update_query,
            (settlement_due_date, damage_amount, settlement_outcome, self.run_id, lease_id)
        )

        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=termination_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.COMPLETE,
            metadata=(
                f"DEPOSIT/SETTLEMENT_QUEUED: Type={termination_type}, "
                f"Outcome={settlement_outcome}, DueDate={settlement_due_date}, "
                f"Escrowed=${deposit_escrowed_amount:.2f}, Damage=${damage_amount:.2f}"
            )
        )

        if self.logger:
            self.logger.info(
                f"Deposit settlement queued: LeaseID={lease_id}, "
                f"Outcome={settlement_outcome}, DueDate={settlement_due_date}"
            )

    def process_pending_settlements(
        self,
        run_id: int,
        current_date: date
    ) -> List[SettlementResult]:
        """
        Process all deposits whose SettlementDueDate has arrived.

        Called daily in simulation.py. Returns list of settlements processed.

        Args:
            run_id: Simulation run ID
            current_date: Current simulation date

        Returns:
            List of SettlementResult for each deposit processed
        """
        query = """
            SELECT LeaseID, DepositRequiredAmount, DepositEscrowedAmount,
                   SettlementOutcome, DamageAmount
            FROM simulation.LeaseDeposit
            WHERE RunID = ? AND SettlementStatus = 'PENDING' AND SettlementDueDate <= ?
        """
        rows = self.db.execute_query(query, (self.run_id, current_date))

        results = []
        for row in rows:
            lease_id = row[0]
            deposit_required_amount = Decimal(str(row[1]))
            escrowed_amount = Decimal(str(row[2]))
            settlement_outcome = row[3]
            damage_amount = Decimal(str(row[4])) if row[4] is not None else Decimal("0.00")

            if settlement_outcome == "FORFEITURE":
                result = self._process_forfeiture(
                    lease_id=lease_id,
                    settlement_date=current_date,
                    escrowed_amount=escrowed_amount
                )
            elif settlement_outcome == "FULL_RETURN":
                result = self._process_full_return(
                    lease_id=lease_id,
                    settlement_date=current_date,
                    escrowed_amount=escrowed_amount
                )
            elif settlement_outcome == "PARTIAL_RETURN":
                result = self._process_partial_return(
                    lease_id=lease_id,
                    settlement_date=current_date,
                    escrowed_amount=escrowed_amount,
                    damage_amount=damage_amount
                )
            else:
                if self.logger:
                    self.logger.error(
                        f"Unknown settlement outcome '{settlement_outcome}' for LeaseID={lease_id}"
                    )
                continue

            # Record the deposit outcome on the canonical LeaseTermination
            # row. These columns were schema-defaulted at INSERT and never updated,
            # so deposit forfeiture/withholding was unmeasurable from LeaseTermination
            # (the ledger carried the events; the summary table read zero).
            if settlement_outcome == "FORFEITURE":
                withheld_amount = escrowed_amount
            elif settlement_outcome == "PARTIAL_RETURN":
                withheld_amount = damage_amount
            else:
                withheld_amount = Decimal("0.00")
            self.db.execute_non_query(
                """
                UPDATE simulation.LeaseTermination
                SET DepositForfeited = ?, DepositWithheldAmount = ?
                WHERE RunID = ? AND LeaseID = ?
                """,
                (1 if settlement_outcome == "FORFEITURE" else 0,
                 withheld_amount, self.run_id, lease_id),
            )

            results.append(result)

        return results

    def _assess_damages(
        self,
        termination_type: str,
        deposit_required_amount: Decimal,
        deposit_escrowed_amount: Decimal
    ) -> tuple:
        """
        Assess damage probability and amount for a fully-funded deposit at termination.

        Uses seeded RNG for determinism. Damage is based on deposit_required_amount
        (not escrowed), capped at escrowed balance.

        Returns:
            (settlement_outcome: str, damage_amount: Decimal)
        """
        damage_prob = (
            self.eviction_damage_prob
            if termination_type in EVICTION_TERMINATION_TYPES
            else self.voluntary_damage_prob
        )

        roll = self.rng.random()
        if roll >= damage_prob:
            return "FULL_RETURN", Decimal("0.00")

        # Damage occurred: calculate amount
        damage_fraction = Decimal(str(
            self.rng.uniform(self.damage_min_percent, self.damage_max_percent)
        ))
        raw_damage = (deposit_required_amount * damage_fraction).quantize(Decimal("0.01"))

        # Cap damage at escrowed amount
        damage_amount = min(raw_damage, deposit_escrowed_amount)

        if damage_amount >= deposit_escrowed_amount:
            return "FORFEITURE", deposit_escrowed_amount

        return "PARTIAL_RETURN", damage_amount

    def _process_forfeiture(
        self,
        lease_id: int,
        settlement_date: date,
        escrowed_amount: Decimal
    ) -> SettlementResult:
        """
        Process deposit forfeiture: escrowed funds move from escrow to Cash as income.

        Called for: unfunded deposits at termination, or fully-damaged funded deposits.
        """
        if escrowed_amount > Decimal("0.00"):
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=settlement_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.FINANCIAL,
                amount=escrowed_amount,
                metadata=f"DEPOSIT/FORFEITURE: ${escrowed_amount:.2f} released from escrow to Cash"
            )
            self.fund_manager.refund_from_escrow(
                run_id=self.run_id,
                refund_amount=escrowed_amount,
                ledger_date=settlement_date,
                lease_id=lease_id,
                event_id=event_id
            )
            self.fund_manager.process_income(
                run_id=self.run_id,
                income_amount=escrowed_amount,
                ledger_date=settlement_date,
                income_type="Deposit Forfeiture"
            )

            new_balance = Decimal("0.00")
            self._create_ledger_entry(
                lease_id=lease_id,
                txn_date=settlement_date,
                txn_type=TxnType.FORFEITURE,
                amount=-escrowed_amount,
                balance_after=new_balance,
                notes=f"Deposit forfeited: ${escrowed_amount:.2f} to Cash"
            )

        self.db.execute_non_query(
            """
            UPDATE simulation.LeaseDeposit
            SET SettlementStatus = 'COMPLETED', Status = ?, UpdatedAt = GETDATE()
            WHERE RunID = ? AND LeaseID = ?
            """,
            (DepositStatus.FORFEITED.value, self.run_id, lease_id)
        )

        return SettlementResult(
            lease_id=lease_id,
            settlement_date=settlement_date,
            outcome="FORFEITURE",
            escrowed_amount=escrowed_amount,
            damage_amount=escrowed_amount,
            return_amount=Decimal("0.00")
        )

    def _process_full_return(
        self,
        lease_id: int,
        settlement_date: date,
        escrowed_amount: Decimal
    ) -> SettlementResult:
        """
        Process full deposit return: escrowed funds returned to tenant (escrow liability cleared).
        """
        if escrowed_amount > Decimal("0.00"):
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=settlement_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.FINANCIAL,
                amount=escrowed_amount,
                metadata=f"DEPOSIT/FULL_RETURN: ${escrowed_amount:.2f} returned to tenant"
            )
            self.fund_manager.refund_from_escrow(
                run_id=self.run_id,
                refund_amount=escrowed_amount,
                ledger_date=settlement_date,
                lease_id=lease_id,
                event_id=event_id
            )

            self._create_ledger_entry(
                lease_id=lease_id,
                txn_date=settlement_date,
                txn_type=TxnType.REFUND,
                amount=-escrowed_amount,
                balance_after=Decimal("0.00"),
                notes=f"Full deposit return: ${escrowed_amount:.2f}"
            )

        self.db.execute_non_query(
            """
            UPDATE simulation.LeaseDeposit
            SET SettlementStatus = 'COMPLETED', Status = ?, UpdatedAt = GETDATE()
            WHERE RunID = ? AND LeaseID = ?
            """,
            (DepositStatus.REFUNDED.value, self.run_id, lease_id)
        )

        return SettlementResult(
            lease_id=lease_id,
            settlement_date=settlement_date,
            outcome="FULL_RETURN",
            escrowed_amount=escrowed_amount,
            damage_amount=Decimal("0.00"),
            return_amount=escrowed_amount
        )

    def _process_partial_return(
        self,
        lease_id: int,
        settlement_date: date,
        escrowed_amount: Decimal,
        damage_amount: Decimal
    ) -> SettlementResult:
        """
        Process partial return: damages withheld, remainder returned to tenant.

        Damage withheld becomes Cash income. Remainder clears as escrow refund.
        """
        damage_withheld = min(damage_amount, escrowed_amount)
        return_amount = escrowed_amount - damage_withheld

        # Release full escrow liability
        if escrowed_amount > Decimal("0.00"):
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=settlement_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.FINANCIAL,
                amount=escrowed_amount,
                metadata=(
                    f"DEPOSIT/PARTIAL_RETURN: Withheld=${damage_withheld:.2f}, "
                    f"Returned=${return_amount:.2f}"
                )
            )
            self.fund_manager.refund_from_escrow(
                run_id=self.run_id,
                refund_amount=escrowed_amount,
                ledger_date=settlement_date,
                lease_id=lease_id,
                event_id=event_id
            )

        # Damage amount goes to Cash as income
        if damage_withheld > Decimal("0.00"):
            self.fund_manager.process_income(
                run_id=self.run_id,
                income_amount=damage_withheld,
                ledger_date=settlement_date,
                income_type="Deposit Damage Withhold"
            )
            self._create_ledger_entry(
                lease_id=lease_id,
                txn_date=settlement_date,
                txn_type=TxnType.DAMAGE_WITHHOLD,
                amount=-damage_withheld,
                balance_after=return_amount,
                notes=f"Damage withheld: ${damage_withheld:.2f}"
            )

        if return_amount > Decimal("0.00"):
            self._create_ledger_entry(
                lease_id=lease_id,
                txn_date=settlement_date,
                txn_type=TxnType.REFUND,
                amount=-return_amount,
                balance_after=Decimal("0.00"),
                notes=f"Partial return to tenant: ${return_amount:.2f}"
            )

        self.db.execute_non_query(
            """
            UPDATE simulation.LeaseDeposit
            SET SettlementStatus = 'COMPLETED', Status = ?, UpdatedAt = GETDATE()
            WHERE RunID = ? AND LeaseID = ?
            """,
            (DepositStatus.APPLIED_TO_DAMAGES.value, self.run_id, lease_id)
        )

        return SettlementResult(
            lease_id=lease_id,
            settlement_date=settlement_date,
            outcome="PARTIAL_RETURN",
            escrowed_amount=escrowed_amount,
            damage_amount=damage_withheld,
            return_amount=return_amount
        )

    # ============================================================
    # Shared helpers
    # ============================================================

    def _create_ledger_entry(
        self,
        lease_id: int,
        txn_date: date,
        txn_type: TxnType,
        amount: Decimal,
        balance_after: Decimal,
        notes: Optional[str] = None
    ) -> int:
        """Create deposit ledger entry. Returns LedgerID."""
        # Always query MAX from DB so the counter survives manager re-instantiation
        result = self.db.execute_query(
            "SELECT ISNULL(MAX(LedgerID), 0) + 1 FROM simulation.LeaseDepositLedger WHERE RunID=?",
            (self.run_id,)
        )
        ledger_id = result[0][0] if result else 1
        self._next_ledger_id = ledger_id + 1

        if self.event_logger:
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=txn_date,
                entity_type=EntityType.LEASE,
                entity_id=lease_id,
                action_type=ActionType.FINANCIAL,
                amount=amount,
                metadata=f"Deposit {txn_type.value}: ${amount:.2f}, Balance=${balance_after:.2f}"
            )
        else:
            event_id = None

        insert_query = """
            INSERT INTO simulation.LeaseDepositLedger (
                RunID, LedgerID, LeaseID, TxnDate, TxnType,
                Amount, BalanceAfter, EventID, Notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                ledger_id,
                lease_id,
                txn_date,
                txn_type.value,
                amount,
                balance_after,
                event_id,
                notes
            )
        )

        return ledger_id

    def is_deposit_funded(self, lease_id: int) -> bool:
        """Check if deposit is fully funded (TCS credit gate check)."""
        query = """
            SELECT
                CASE
                    WHEN DepositEscrowedAmount >= DepositRequiredAmount THEN 1
                    ELSE 0
                END AS IsFunded
            FROM simulation.LeaseDeposit
            WHERE RunID = ? AND LeaseID = ?
        """
        result = self.db.execute_query(query, (self.run_id, lease_id))

        if not result:
            return False

        return bool(result[0][0])

