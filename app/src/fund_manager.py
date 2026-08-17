"""
Fund Manager - Community Stability Fund (CSF) Management

This module implements the three-bucket fund structure for Liberty Bee:
1. Cash - Operating funds for all expenses and property acquisition
2. CSF - Community Stability Fund (separate reserve bucket)
3. EIP - Experimental Initiatives Program (after successful years)

Key Logic:
- CSF must be maintained at target level: reserve_months(properties) of
  expected OpEx (curve — peak 12 tapering to floor 4 via sqrt(N0/N);
  replaced the flat FIN_OperatingReserveMonths=12 at V00049)
- Cash → CSF transfers when CSF drops below target
- Property acquisition only allowed when CSF target is maintained
- EIP allocation starts after FIN_EIPStartYear years of operation

Usage:
- Use FundManager.initialize_funds() to set up initial fund allocation
- Use FundManager.process_expense() to handle all expense payments
- Use FundManager.get_fund_balances() for current fund status
- Acquisition gating lives in PropertyAcquisitionManager (expected-basis CSF gate)
"""

import logging
import math
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal

from database_manager import DatabaseManager
from event_logger import EventLogger


@dataclass
class FundBalances:
    """Current fund balances

    Note: escrow_balance is a LIABILITY (deposits held in escrow),
    NOT an asset. It is NOT included in total_balance.

    CashHold (in-flight offer reservations) is also EXCLUDED from
    total_balance — a DELIBERATE conservative choice, not a
    correctness statement: the balance is only genuinely-active
    reservations, and excluding it can only understate wealth, never flatter.
    Whether a net-worth-complete measure should include it is a separate
    future ruling. The halt test is unaffected (it uses cash + csf only).
    """
    cash_balance: Decimal
    csf_balance: Decimal
    eip_balance: Decimal
    escrow_balance: Decimal  # Liability - NOT included in total
    total_balance: Decimal

    def __post_init__(self):
        """Calculate total balance (excludes escrow liability)"""
        self.total_balance = self.cash_balance + self.csf_balance + self.eip_balance


class FundManager:
    """Manage three-bucket fund structure with CSF requirements"""

    def __init__(self, db_manager: DatabaseManager, event_logger: Optional[EventLogger] = None):
        self.db = db_manager
        self.event_logger = event_logger
        self.logger = logging.getLogger(__name__)



    def initialize_funds(self, run_id: int, starting_funds: Decimal,
                        csf_target: Decimal, ledger_date: date) -> int:
        """Initialize fund balances with proper CSF allocation

        Args:
            run_id: Simulation run ID
            starting_funds: Total starting capital
            csf_target: Required CSF reserve amount
            ledger_date: Date for fund initialization

        Returns:
            Event ID for fund initialization
        """
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="INITIALIZE",
                message=f"Initializing funds: ${starting_funds:,} total, CSF target ${csf_target:,}",
                effective_date=ledger_date,
                amount=starting_funds
            )
        else:
            event_id = None

        # Initial allocation strategy:
        # 1. Fund CSF to target level first
        # 2. Remaining funds go to Cash for operations
        # 3. EIP starts at $0 (funded later)

        initial_csf = min(csf_target, starting_funds)
        initial_cash = starting_funds - initial_csf
        initial_eip = Decimal('0')

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO simulation.FundLedger
                (RunID, LedgerDate, CashDebit, CashCredit, CashBalance,
                 CSFDebit, CSFCredit, CSFBalance, EIPDebit, EIPCredit, EIPBalance,
                 CashHoldDebit, CashHoldCredit, CashHoldBalance,
                 EscrowDebit, EscrowCredit, EscrowBalance, EventID)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, ledger_date,
                Decimal('0'),  # CashDebit
                initial_cash,  # CashCredit
                initial_cash,  # CashBalance
                Decimal('0'),  # CSFDebit
                initial_csf,   # CSFCredit
                initial_csf,   # CSFBalance
                Decimal('0'),  # EIPDebit
                initial_eip,   # EIPCredit
                initial_eip,   # EIPBalance
                Decimal('0'),  # CashHoldDebit
                Decimal('0'),  # CashHoldCredit
                Decimal('0'),  # CashHoldBalance
                Decimal('0'),  # EscrowDebit
                Decimal('0'),  # EscrowCredit
                Decimal('0'),  # EscrowBalance (deposit liability)
                event_id
            ))

            conn.commit()

        self.logger.info(f"Funds initialized: Cash=${initial_cash:,}, CSF=${initial_csf:,}, EIP=${initial_eip:,}")
        return event_id

    def get_csf_target(self, monthly_opex: Decimal, reserve_months) -> Decimal:
        """Calculate CSF target based on operating expenses.

        single source of truth for cents-based CSF target. Callers
        may pass raw, sub-cent, or pre-quantized monthly_opex — all converge to
        the same cents-quantized target. This prevents the rounding mismatch
        where process_csf_topup (quantized) funded CSF to a different value
        than can_acquire_property (unquantized) checked against — a one-cent
        delta was blocking acquisitions for entire years.

        reserve_months may be the int peak or the Decimal curve value
        from get_reserve_months (both quantize identically here).
        """
        monthly_opex_q = Decimal(str(monthly_opex)).quantize(Decimal("0.01"))
        return (monthly_opex_q * Decimal(str(reserve_months))).quantize(Decimal("0.01"))

    def get_reserve_months(self, owned_properties: int, peak_months: int,
                           floor_months: int, n0_properties: int) -> Decimal:
        """CSF reserve curve:

            reserve_months(N) = floor + (peak − floor) × √(N0 / N),  N = properties

        capped at peak — N ≤ N0 (including the empty pre-acquisition portfolio)
        holds the full peak; the taper only exists above N0. N counts
        PROPERTIES (buildings are the correlated risk pools — roof/boiler/
        winter; per-unit costs roll up inside the building's buffer). The
        result is quantized to 4dp so the acquisition-gate earmark and the
        month-end top-up compute cent-identical targets from the same inputs
        (the contract). Absolute dollars grow monotonically under
        this curve even as months taper — never present one without the other.
        """
        if owned_properties <= n0_properties:
            return Decimal(peak_months)
        months = Decimal(floor_months) + \
            (Decimal(peak_months) - Decimal(floor_months)) * \
            Decimal(str(math.sqrt(n0_properties / owned_properties)))
        return months.quantize(Decimal("0.0001"))

    def get_fund_balances(self, run_id: int, as_of_date: date) -> FundBalances:
        """Get current fund balances as of specified date.

        Note: escrow_balance is a LIABILITY (deposits held), NOT included in total.

        ORDER BY includes `EventID DESC` as a secondary tiebreaker so
        that same-day rows resolve to the latest event of the day deterministically.
        Matches the convention already used at fund_manager.py:811-814.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT CashBalance, CSFBalance, EIPBalance, EscrowBalance
                FROM simulation.FundLedger
                WHERE RunID = ? AND LedgerDate <= ?
                ORDER BY LedgerDate DESC, EventID DESC
                OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """, (run_id, as_of_date))

            result = cursor.fetchone()
            if not result:
                return FundBalances(
                    cash_balance=Decimal('0'),
                    csf_balance=Decimal('0'),
                    eip_balance=Decimal('0'),
                    escrow_balance=Decimal('0'),
                    total_balance=Decimal('0')
                )

            return FundBalances(
                cash_balance=Decimal(str(result[0])),
                csf_balance=Decimal(str(result[1])),
                eip_balance=Decimal(str(result[2])),
                escrow_balance=Decimal(str(result[3])),  # liability
                total_balance=Decimal('0')  # Will be calculated in __post_init__
            )

    def transfer_to_csf(self, run_id: int, transfer_amount: Decimal,
                       ledger_date: date, reason: str) -> int:
        """Transfer funds from Cash to CSF"""
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="TRANSFER_TO_CSF",
                message=f"Cash to CSF transfer: ${transfer_amount:,} ({reason})",
                effective_date=ledger_date,
                amount=transfer_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_debit=transfer_amount,  # Money leaving Cash
            csf_credit=transfer_amount,  # Money entering CSF
            event_id=event_id
        )

        self.logger.info(f"Transferred ${transfer_amount:,} from Cash to CSF: {reason}")
        return event_id

    def process_csf_topup(
        self,
        run_id: int,
        as_of_date: date,
        monthly_opex: Decimal,
        reserve_months: Decimal,  # the curve value (or the int peak)
        topup_fraction: Decimal,
        cash_floor_months: int,
    ) -> dict:
        """
        Recurring CSF top-up (reserve ratchet).

        Called from simulation.py's monthly orchestration, AFTER rent
        collection settles and BEFORE the acquisition gate is evaluated
        for the month.

        Algorithm:
            target = monthly_opex * reserve_months
            floor  = monthly_opex * cash_floor_months
            if csf >= target:
                no-op (reserve ratchet — hold excess, do NOT sweep back)
            else:
                deficit       = target - csf
                proposed_move = deficit * topup_fraction
                cash_available = max(0, cash - floor)
                actual_move   = min(proposed_move, cash_available)
                if actual_move > 0:
                    write balanced FundLedger row + log event

        Returns a dict describing the outcome:
            {
                'topup_amount': Decimal,
                'csf_before': Decimal, 'csf_after': Decimal,
                'cash_before': Decimal, 'cash_after': Decimal,
                'csf_target': Decimal, 'cash_floor': Decimal,
                'reason': str,
            }

        No RNG involved — fully deterministic given state.
        """
        # get_csf_target is now the single source of truth for
        # cents-based CSF target — no need to pre-quantize monthly_opex here.
        # cash_floor's own .quantize() handles its result; topup_fraction is
        # normalized to Decimal once for the proposed-move calc below.
        topup_fraction = Decimal(str(topup_fraction))

        balances = self.get_fund_balances(run_id, as_of_date)
        csf_before = balances.csf_balance
        cash_before = balances.cash_balance

        csf_target = self.get_csf_target(monthly_opex, reserve_months)
        cash_floor = (Decimal(str(monthly_opex)) * Decimal(cash_floor_months)).quantize(Decimal("0.01"))

        result = {
            'topup_amount': Decimal("0.00"),
            'csf_before': csf_before,
            'csf_after': csf_before,
            'cash_before': cash_before,
            'cash_after': cash_before,
            'csf_target': csf_target,
            'cash_floor': cash_floor,
            'reason': '',
        }

        # Reserve ratchet: if CSF is already at or above target, do nothing.
        if csf_before >= csf_target:
            result['reason'] = 'CSF_AT_OR_ABOVE_TARGET'
            return result

        deficit = (csf_target - csf_before).quantize(Decimal("0.01"))
        proposed = (deficit * topup_fraction).quantize(Decimal("0.01"))
        cash_available = max(Decimal("0.00"), (cash_before - cash_floor).quantize(Decimal("0.01")))
        actual = min(proposed, cash_available)

        if actual <= 0:
            # Cash at or below floor — skip this month. No FundLedger row.
            result['reason'] = 'CSF_TOPUP_SKIPPED_CASH_FLOOR'
            return result

        # Execute the top-up: balanced Cash → CSF transfer.
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="CSF_TOPUP",
                message=(
                    f"CSF_TOPUP: monthly_opex=${monthly_opex:,.2f}, "
                    f"reserve_months={reserve_months}, csf_target=${csf_target:,.2f}, "
                    f"csf_before=${csf_before:,.2f}, cash_before=${cash_before:,.2f}, "
                    f"proposed=${proposed:,.2f}, actual=${actual:,.2f}, "
                    f"cash_floor=${cash_floor:,.2f}"
                ),
                effective_date=as_of_date,
                amount=actual,
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=as_of_date,
            cash_debit=actual,   # Money leaving Cash
            csf_credit=actual,   # Money entering CSF
            event_id=event_id,
        )

        result['topup_amount'] = actual
        result['csf_after'] = csf_before + actual
        result['cash_after'] = cash_before - actual
        result['reason'] = 'CSF_TOPUP_EXECUTED'

        self.logger.debug(
            f"CSF top-up: target=${csf_target:,.2f} csf_before=${csf_before:,.2f} "
            f"-> csf_after=${csf_before + actual:,.2f} (moved ${actual:,.2f}; "
            f"deficit was ${deficit:,.2f}, floor ${cash_floor:,.2f})"
        )
        return result

    def process_expense_protected(
        self,
        run_id: int,
        expense_amount: Decimal,
        ledger_date: date,
        expense_type: str,
    ) -> dict:
        """
        Process a protected operating obligation (payroll,
        eviction cost, etc.) with Cash-first, CSF-fallback semantics.

        Algorithm:
          1. Pay from Cash first, down to zero if needed.
          2. If Cash insufficient, draw the shortfall from CSF.
          3. If Cash + CSF still insufficient, return a partial result
             (caller decides whether to trigger SIMULATION FAILURE).

        The Cash floor does NOT block protected expenses — the floor governs
        top-up and acquisition discipline only.

        FundLedger row layout:
          - Fully Cash-covered: a regular CashDebit row (delegated to
            process_expense to preserve existing audit semantics).
          - Cash + CSF split:   one balanced row with CashDebit AND CSFDebit.
          - CSF-only (Cash = 0): one row with CSFDebit only.

        Event lane:
          - Fully Cash-covered: FUND/<expense_type> via process_expense.
          - Any CSF draw:       FUND/CSF_DRAW with full metadata.

        Returns dict:
            {
                'cash_paid':       Decimal,
                'csf_drawn':       Decimal,
                'cash_after':      Decimal,
                'csf_after':       Decimal,
                'fully_covered':   bool,   # True iff cash + CSF >= amount
                'event_id':        Optional[int],
            }
        """
        expense_amount = Decimal(str(expense_amount)).quantize(Decimal("0.01"))
        balances = self.get_fund_balances(run_id, ledger_date)
        cash_before = balances.cash_balance
        csf_before = balances.csf_balance

        cash_paid = min(expense_amount, cash_before).quantize(Decimal("0.01"))
        shortfall = (expense_amount - cash_paid).quantize(Decimal("0.01"))

        # Fast path: fully Cash-covered. Delegate to existing process_expense
        # so the event lane and FundLedger semantics match unprotected expenses
        # (a CSF_DRAW event would be misleading when no CSF was actually drawn).
        if shortfall <= 0:
            event_id = self.process_expense(run_id, expense_amount, ledger_date, expense_type)
            return {
                'cash_paid':     expense_amount,
                'csf_drawn':     Decimal("0.00"),
                'cash_after':    cash_before - expense_amount,
                'csf_after':     csf_before,
                'fully_covered': True,
                'event_id':      event_id,
            }

        # CSF draw branch: Cash was insufficient. Draw the shortfall from CSF.
        csf_drawn = min(shortfall, csf_before).quantize(Decimal("0.01"))
        # If even CSF can't fully cover, we still do the partial draw and
        # report fully_covered=False; caller decides on SIMULATION FAILURE.
        actually_paid = cash_paid + csf_drawn

        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="CSF_DRAW",
                message=(
                    f"FUND/CSF_DRAW: expense_type={expense_type}, "
                    f"expense_amount=${expense_amount:.2f}, "
                    f"cash_before=${cash_before:.2f}, csf_before=${csf_before:.2f}, "
                    f"cash_paid=${cash_paid:.2f}, csf_drawn=${csf_drawn:.2f}, "
                    f"cash_after=${cash_before - cash_paid:.2f}, "
                    f"csf_after=${csf_before - csf_drawn:.2f}, "
                    f"reason=Cash insufficient for protected obligation"
                ),
                effective_date=ledger_date,
                amount=expense_amount,
            )
        else:
            event_id = None

        # Single balanced FundLedger row covering both Cash and CSF debits.
        # _update_fund_balances accepts independent debit values for each bucket.
        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_debit=cash_paid,
            csf_debit=csf_drawn,
            event_id=event_id,
        )

        self.logger.warning(
            f"CSF draw for protected {expense_type}: ${expense_amount:.2f} obligation; "
            f"paid ${cash_paid:.2f} from Cash + ${csf_drawn:.2f} from CSF; "
            f"fully_covered={actually_paid >= expense_amount}"
        )

        return {
            'cash_paid':     cash_paid,
            'csf_drawn':     csf_drawn,
            'cash_after':    cash_before - cash_paid,
            'csf_after':     csf_before - csf_drawn,
            'fully_covered': actually_paid >= expense_amount,
            'event_id':      event_id,
        }

    def process_expense(self, run_id: int, expense_amount: Decimal,
                       ledger_date: date, expense_type: str) -> int:
        """Process expense payment from Cash fund"""
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="EXPENSE",
                message=f"{expense_type}: ${expense_amount:,}",
                effective_date=ledger_date,
                amount=expense_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_debit=expense_amount,  # Money leaving Cash
            event_id=event_id
        )

        self.logger.info(f"Processed {expense_type}: ${expense_amount:,}")
        return event_id

    def process_income(self, run_id: int, income_amount: Decimal,
                      ledger_date: date, income_type: str) -> int:
        """Process income receipt into Cash fund"""
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="INCOME",
                message=f"{income_type}: ${income_amount:,}",
                effective_date=ledger_date,
                amount=income_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_credit=income_amount,  # Money entering Cash
            event_id=event_id
        )

        self.logger.info(f"Processed {income_type}: ${income_amount:,}")
        return event_id

    def deposit_to_escrow(self, run_id: int, deposit_amount: Decimal,
                         ledger_date: date, lease_id: int, event_id: Optional[int] = None) -> None:
        """
        Deposit funds into escrow (security deposit collection).

        Security deposits are liabilities held in escrow.
        They are NOT operating funds and NOT included in total fund balance.

        Args:
            run_id: Simulation run ID
            deposit_amount: Amount to deposit into escrow
            ledger_date: Date for ledger entry
            lease_id: Lease ID for tracking (for logging only)
            event_id: Optional event ID (if already logged by caller)
        """
        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            escrow_credit=deposit_amount,  # Money entering Escrow (liability increase)
            event_id=event_id
        )

        self.logger.info(f"Deposited ${deposit_amount:,} to escrow for Lease {lease_id}")

    def refund_from_escrow(self, run_id: int, refund_amount: Decimal,
                          ledger_date: date, lease_id: int, event_id: Optional[int] = None) -> None:
        """
        Refund funds from escrow (security deposit return).

        Refunding a deposit reduces the escrow liability balance.

        Args:
            run_id: Simulation run ID
            refund_amount: Amount to refund from escrow
            ledger_date: Date for ledger entry
            lease_id: Lease ID for tracking (for logging only)
            event_id: Optional event ID (if already logged by caller)
        """
        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            escrow_debit=refund_amount,  # Money leaving Escrow (liability decrease)
            event_id=event_id
        )

        self.logger.info(f"Refunded ${refund_amount:,} from escrow for Lease {lease_id}")

    def process_property_acquisition(self, run_id: int, property_id: int, purchase_price: Decimal,
                                    ledger_date: date, description: str) -> bool:
        """
        Process property acquisition payment from Cash fund

        Args:
            run_id: Simulation run ID
            property_id: Property being acquired
            purchase_price: Amount to pay for property
            ledger_date: Date of acquisition
            description: Description for ledger/logging

        Returns:
            True if transaction successful, False otherwise
        """
        try:
            if self.event_logger:
                event_id = self.event_logger.log_fund_event(
                    action="PROPERTY_ACQUISITION",
                    message=f"{description} (Property {property_id}): ${purchase_price:,}",
                    effective_date=ledger_date,
                    amount=purchase_price
                )
            else:
                event_id = None

            self._update_fund_balances(
                run_id=run_id,
                ledger_date=ledger_date,
                cash_debit=purchase_price,  # Money leaving Cash to buy property
                event_id=event_id
            )

            self.logger.info(f"Processed property acquisition: Property {property_id} for ${purchase_price:,}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to process property acquisition: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"Failed to process property acquisition for property {property_id}",
                    exception=e,
                    context="FundManager.process_property_acquisition"
                )
            return False

    def reserve_funds_for_offer(self, run_id: int, offer_amount: Decimal,
                                ledger_date: date, property_id: int) -> int:
        """
        Reserve funds for property offer (move from Cash to CashHold)

        Transaction: CashDebit + CashHoldCredit
        Result: Cash↓, CashHold↑

        Args:
            run_id: Simulation run ID
            offer_amount: Amount being offered
            ledger_date: Date of offer
            property_id: Property being offered on

        Returns:
            Event ID for reservation transaction
        """
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="RESERVE_FUNDS",
                message=f"Reserved ${offer_amount:,} for offer on Property {property_id}",
                effective_date=ledger_date,
                amount=offer_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_debit=offer_amount,        # Money leaving Cash
            cash_hold_credit=offer_amount,  # Money entering CashHold
            event_id=event_id
        )

        self.logger.info(f"Reserved ${offer_amount:,} for Property {property_id} offer")
        return event_id

    def reserve_additional_funds_for_counter(self, run_id: int, additional_amount: Decimal,
                                            ledger_date: date, property_id: int,
                                            counter_amount: Decimal) -> int:
        """
        Reserve additional funds when accepting counter-offer

        When seller counters at higher price and we accept, we need to reserve
        the additional amount beyond the original offer.

        Transaction: CashDebit + CashHoldCredit (for the additional amount only)
        Result: Cash↓, CashHold↑

        Args:
            run_id: Simulation run ID
            additional_amount: Additional funds to reserve (counter - offer)
            ledger_date: Date of counter acceptance
            property_id: Property ID
            counter_amount: Total counter offer amount (for logging)

        Returns:
            Event ID for reservation transaction
        """
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="RESERVE_ADDITIONAL_FUNDS",
                message=f"Reserved additional ${additional_amount:,} for Property {property_id} counter (total: ${counter_amount:,})",
                effective_date=ledger_date,
                amount=additional_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_debit=additional_amount,        # Money leaving Cash
            cash_hold_credit=additional_amount,  # Money entering CashHold
            event_id=event_id
        )

        self.logger.info(f"Reserved additional ${additional_amount:,} for Property {property_id} counter")
        return event_id

    def release_funds_from_offer(self, run_id: int, offer_amount: Decimal,
                                 ledger_date: date, property_id: int, reason: str) -> int:
        """
        Release reserved funds back to Cash (offer failed/withdrawn)

        Transaction: CashHoldDebit + CashCredit
        Result: CashHold↓, Cash↑

        Args:
            run_id: Simulation run ID
            offer_amount: Amount to release
            ledger_date: Date of release
            property_id: Property offer failed/withdrawn
            reason: Why funds are being released

        Returns:
            Event ID for release transaction
        """
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="RELEASE_FUNDS",
                message=f"Released ${offer_amount:,} from Property {property_id} offer ({reason})",
                effective_date=ledger_date,
                amount=offer_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_hold_debit=offer_amount,  # Money leaving CashHold
            cash_credit=offer_amount,      # Money entering Cash
            event_id=event_id
        )

        self.logger.info(f"Released ${offer_amount:,} from Property {property_id}: {reason}")
        return event_id

    def use_reserved_funds(self, run_id: int, offer_amount: Decimal,
                          ledger_date: date, property_id: int) -> int:
        """
        Use reserved funds for completed acquisition (remove from CashHold)

        Transaction: CashHoldDebit only
        Result: CashHold↓ (money already left Cash when reserved)

        Args:
            run_id: Simulation run ID
            offer_amount: Amount to use
            ledger_date: Date of acquisition
            property_id: Property being acquired

        Returns:
            Event ID for use transaction
        """
        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="USE_RESERVED_FUNDS",
                message=f"Used ${offer_amount:,} reserved funds for Property {property_id} acquisition",
                effective_date=ledger_date,
                amount=offer_amount
            )
        else:
            event_id = None

        self._update_fund_balances(
            run_id=run_id,
            ledger_date=ledger_date,
            cash_hold_debit=offer_amount,  # Money leaving CashHold (already left Cash)
            event_id=event_id
        )

        self.logger.info(f"Used ${offer_amount:,} reserved funds for Property {property_id}")
        return event_id

    def _update_fund_balances(self, run_id: int, ledger_date: date,
                             cash_debit: Decimal = Decimal('0'), cash_credit: Decimal = Decimal('0'),
                             csf_debit: Decimal = Decimal('0'), csf_credit: Decimal = Decimal('0'),
                             eip_debit: Decimal = Decimal('0'), eip_credit: Decimal = Decimal('0'),
                             cash_hold_debit: Decimal = Decimal('0'), cash_hold_credit: Decimal = Decimal('0'),
                             escrow_debit: Decimal = Decimal('0'), escrow_credit: Decimal = Decimal('0'),
                             event_id: Optional[int] = None):
        """
        Record fund balance transaction in append-only ledger.

        Each call creates a new ledger entry with the transaction and updated balances.
        Follows ledger append-only pattern - never updates existing entries.

        Added escrow_debit/escrow_credit for deposit liability tracking.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get most recent balances (any date <= current date)
            cursor.execute("""
                SELECT CashBalance, CSFBalance, EIPBalance, CashHoldBalance, EscrowBalance
                FROM simulation.FundLedger
                WHERE RunID = ? AND LedgerDate <= ?
                ORDER BY LedgerDate DESC, EventID DESC
                OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """, (run_id, ledger_date))

            prev_result = cursor.fetchone()
            if prev_result:
                prev_cash = Decimal(str(prev_result[0]))
                prev_csf = Decimal(str(prev_result[1]))
                prev_eip = Decimal(str(prev_result[2]))
                prev_cash_hold = Decimal(str(prev_result[3]))
                prev_escrow = Decimal(str(prev_result[4]))
            else:
                prev_cash = prev_csf = prev_eip = prev_cash_hold = prev_escrow = Decimal('0')

            # Calculate new balances from transaction
            new_cash_balance = prev_cash + cash_credit - cash_debit
            new_csf_balance = prev_csf + csf_credit - csf_debit
            new_eip_balance = prev_eip + eip_credit - eip_debit
            new_cash_hold_balance = prev_cash_hold + cash_hold_credit - cash_hold_debit
            new_escrow_balance = prev_escrow + escrow_credit - escrow_debit

            # Always INSERT - append-only ledger pattern
            cursor.execute("""
                INSERT INTO simulation.FundLedger
                (RunID, EventID, LedgerDate, CashDebit, CashCredit, CashBalance,
                 CSFDebit, CSFCredit, CSFBalance, EIPDebit, EIPCredit, EIPBalance,
                 CashHoldDebit, CashHoldCredit, CashHoldBalance,
                 EscrowDebit, EscrowCredit, EscrowBalance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, event_id, ledger_date,
                cash_debit, cash_credit, new_cash_balance,
                csf_debit, csf_credit, new_csf_balance,
                eip_debit, eip_credit, new_eip_balance,
                cash_hold_debit, cash_hold_credit, new_cash_hold_balance,
                escrow_debit, escrow_credit, new_escrow_balance
            ))

            conn.commit()
