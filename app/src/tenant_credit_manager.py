"""
Tenant Credit Manager - Credit Accrual + Credit Redemption

Purpose: Manages the Tenant Credit System (TCS) — a non-cash loyalty reward
program that accrues 5% of on-time rent payments to a per-household credit
balance, and lets households redeem those credits to offset rent payments
(routine path) or prevent a missed payment (hardship path).

Key Responsibilities:
  Credit Accrual:
- Initialize zero-balance row at household onboarding (idempotent)
- Process credit accrual on ON_TIME rent collections (subject to deposit-funded gate)
- Enforce cap on CurrentBalance (partial-accrue to land exactly at cap)
- Maintain ledger (source of truth) + materialized balance table in lock-step
- Log all accrual events to simulation.Event under the TENANT_CREDIT lane

  Credit Redemption:
- Draw per-household redemption probability from a household-type-weighted
  triangular distribution at onboarding (FAMILY/COUPLE/SINGLE/ROOMMATES bands)
- Routine redemption: when household can pay this month, optionally use credit
- Hardship redemption: when household cannot pay this month but entered in
  good standing with sufficient balance, use credit to prevent MISSED status
- Enforce annual limit (1 month rent max per calendar year)
- Maintain AnnualRedemptionUsed + LastRedemptionYear on TenantCreditBalance

Design Principles:
- Compound primary keys (RunID, CreditTransactionID) and (RunID, HouseholdID)
- No IDENTITY columns — manual ID assignment, always reseeded from DB MAX()
- Cap enforced on CurrentBalance, not LifetimeAccrued (Gray 2026-05-16)
- LifetimeAccrued still tracked for audit/reporting
- Accrual basis = AmountCollected − ArrearsAmount − LateFeeAmount
- ON_TIME-only eligibility for accrual; deposit_funded gate required
- Redemption is full month or nothing; calendar year annual limit
- REDEMPTION ledger rows have negative Amount; BalanceAfter stays positive
- Non-cash: neither accrual nor redemption moves money in/out of Cash, CSF, EIP, Escrow

Out of Scope (deferred):
- Portability between units
- 24-month expiry / forfeiture-on-exit
- Annual probability drift (deferred behavioral enhancement; see plan)
- Partial-month redemption
- Redemption against existing arrears or late fees
"""

import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Any

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from event_logger import EventLogger, EventType, EntityType, ActionType


# Transaction types written to simulation.TenantCreditLedger.
TXN_ACCRUAL = "ACCRUAL"
TXN_REDEMPTION = "REDEMPTION"
TXN_FORFEITURE = "FORFEITURE"


# ============================================================================
# Household-type redemption probability bands.
#
# APPROVED BASELINE ASSUMPTIONS FOR IMPLEMENTATION AND
# REGRESSION TESTING. NOT FINAL POLICY CLAIMS; FUTURE CALIBRATION MAY
# ADJUST THESE AFTER SIMULATION ANALYSIS.
#
# Behavioral framing:
#   FAMILY / COUPLE   → skew saver (preserve credits for stability)
#   SINGLE / ROOMMATES → skew relief-oriented (use credits near-term)
#
# These per-type bands are now registry-backed
# (TCS.RedemptionBand_<TYPE>_<Min|Center|Max>); TenantCreditManager builds the
# dict once from the registry (see household_type_bands) and passes it in here,
# replacing the former hardcoded module-level HOUSEHOLD_TYPE_BANDS.
# ============================================================================


def draw_household_redemption_probability(
    household_type: str,
    onboarding_rng: random.Random,
    bands: dict,
    fallback_min: Decimal,
    fallback_center: Decimal,
    fallback_max: Decimal,
) -> Decimal:
    """
    Draw a household's TCSRedemptionProbability from a bounded triangular
    distribution keyed on HouseholdType.

    `bands` maps household type -> {"min","center","max"} (registry-backed,
    built once in TenantCreditManager.__init__).

    Falls back to the global Min/Default/Max guardrails if the type isn't
    one of FAMILY/COUPLE/SINGLE/ROOMMATES (defensive — should never fire in
    normal operation since tenant_manager.py constrains the enum).

    Returns Decimal quantized to 4 decimal places.
    """
    band = bands.get(
        household_type,
        {"min": fallback_min, "center": fallback_center, "max": fallback_max},
    )
    sample = onboarding_rng.triangular(
        float(band["min"]), float(band["max"]), float(band["center"])
    )
    return Decimal(str(sample)).quantize(Decimal("0.0001"))


@dataclass
class AccrualResult:
    """Outcome of a single process_credit_accrual call."""
    accrued_amount: Decimal       # Actual amount written to the ledger (may be 0)
    new_balance: Decimal          # CurrentBalance after the accrual
    credit_transaction_id: Optional[int]  # None if no accrual occurred
    reason_no_accrual: Optional[str] = None  # Set if accrued_amount == 0


@dataclass
class RedemptionResult:
    """Outcome of a single process_credit_redemption call."""
    redeemed_amount: Decimal      # Amount written to the ledger (negative magnitude)
    new_balance: Decimal          # CurrentBalance after the redemption
    credit_transaction_id: Optional[int]
    path: Optional[str]           # 'ROUTINE' | 'HARDSHIP' | 'FINAL_MONTH_EXIT'
    reason_no_redemption: Optional[str] = None  # Set if redeemed_amount == 0


@dataclass
class ForfeitureResult:
    """Outcome of a single FORFEITURE event (eviction or expiry)."""
    forfeited_amount: Decimal     # Amount written to the ledger (negative magnitude)
    new_balance: Decimal          # Always 0 after forfeiture
    credit_transaction_id: Optional[int]  # None when balance was already 0
    forfeiture_type: str          # 'EVICTION' | 'EXPIRY'


@dataclass
class ExpiryResult:
    """Single household's outcome from process_credit_expiry_sweep."""
    household_id: int
    forfeited_amount: Decimal     # 0 when balance was already 0 (status flip only)
    credit_transaction_id: Optional[int]


class TenantCreditManager:
    """Manages tenant credit accrual, redemption, and lifecycle."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_logger: EventLogger,
        run_id: int,
        credit_rate: Decimal,
        credit_cap: Decimal,
        # Redemption guardrails (via reference.ParameterRegistry / ProjectionConfig)
        redemption_prob_min: Decimal = Decimal("0.0050"),
        redemption_prob_default: Decimal = Decimal("0.0200"),
        redemption_prob_max: Decimal = Decimal("0.0800"),
        hardship_floor: Decimal = Decimal("0.7500"),
        hardship_boost: Decimal = Decimal("0.7000"),
        hardship_cap: Decimal = Decimal("0.9500"),
        run_seed: Optional[int] = None,
        logger: Any = None,
    ):
        self.db = db_manager
        self.event_logger = event_logger
        self.run_id = run_id
        self.credit_rate = Decimal(str(credit_rate))   # e.g., 0.05
        self.credit_cap = Decimal(str(credit_cap))     # e.g., 15000.00
        self.logger = logger

        # TCS portability window from the registry
        # (TCS.PortabilityYears, global) — replaces the hardcoded 24-month DATEADD
        # in the (now-removed, V00041) CreditExpiryDate persisted computed column.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.portability_months = self.registry.get_int('TCS', 'PortabilityYears') * 12

        # Per-household-type TCS redemption-probability bands,
        # registry-backed (TCS.RedemptionBand_<TYPE>_<Min|Center|Max>) — replaces
        # the hardcoded module-level HOUSEHOLD_TYPE_BANDS. Built once, fail-loud.
        self.household_type_bands = {
            htype: {
                'min':    self.registry.get_decimal('TCS', f'RedemptionBand_{htype}_Min'),
                'center': self.registry.get_decimal('TCS', f'RedemptionBand_{htype}_Center'),
                'max':    self.registry.get_decimal('TCS', f'RedemptionBand_{htype}_Max'),
            }
            for htype in ('FAMILY', 'COUPLE', 'SINGLE', 'ROOMMATES')
        }

        # Redemption guardrails + dedicated RNG
        self.redemption_prob_min     = Decimal(str(redemption_prob_min))
        self.redemption_prob_default = Decimal(str(redemption_prob_default))
        self.redemption_prob_max     = Decimal(str(redemption_prob_max))
        self.hardship_floor          = Decimal(str(hardship_floor))
        self.hardship_boost          = Decimal(str(hardship_boost))
        self.hardship_cap            = Decimal(str(hardship_cap))
        # Dedicated RNG for redemption decisions. Seed offset 30802.
        # Existing offsets in use: 30602 (deposit), 30707 (turnover), 30708 (vacancy fill),
        # 30403 (TCS onboarding probability draw — lives in tenant_manager.py).
        # If run_seed is None we fall back to a non-seeded RNG (test convenience only).
        self.redemption_rng = (
            random.Random(run_seed + 30802) if run_seed is not None else random.Random()
        )

        # Counter is always reseeded from DB on each insert (survives re-instantiation).
        self._next_credit_transaction_id = 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize_household(self, household_id: int, event_id: Optional[int] = None) -> None:
        """
        Insert a zero-balance row for a household. Idempotent — safe to call
        twice. Hook point: called from onboarding_manager when a Household is
        materialized.

        Every active household should appear in TCS reporting from
        day one, even if they have not yet earned credit.
        """
        # New TCS balance rows default to ExpiryStatus = 'ACTIVE'
        # so the lifecycle state machine has a defined starting point.
        merge_query = """
            MERGE simulation.TenantCreditBalance AS target
            USING (SELECT ? AS RunID, ? AS HouseholdID) AS source
                ON target.RunID = source.RunID AND target.HouseholdID = source.HouseholdID
            WHEN NOT MATCHED THEN
                INSERT (RunID, HouseholdID, CurrentBalance, LifetimeAccrued,
                        LifetimeRedeemed, LastAccrualDate, LastUpdatedEventID,
                        ExpiryStatus)
                VALUES (source.RunID, source.HouseholdID, 0, 0, 0, NULL, ?, 'ACTIVE');
        """
        self.db.execute_non_query(
            merge_query,
            (self.run_id, household_id, event_id),
        )

        if self.logger:
            self.logger.debug(
                f"TCS balance initialized (idempotent): HouseholdID={household_id}"
            )

    def process_credit_accrual(
        self,
        lease_id: int,
        collection_id: int,
        amount_collected: Decimal,
        arrears_amount: Decimal,
        late_fee_amount: Decimal,
        payment_status: str,
        deposit_funded: bool,
        transaction_date: date,
        event_id: int,
    ) -> AccrualResult:
        """
        Calculate, record, and persist credit accrual for a single
        RentCollection row.

        Eligibility (both must hold):
          - payment_status == 'ON_TIME'
          - deposit_funded == True

        Basis:
          - basis = amount_collected − arrears_amount − late_fee_amount
          - accrual = basis * credit_rate, rounded to cents

        Cap (Gray 2026-05-16):
          - Cap on CurrentBalance at credit_cap (e.g., $15,000)
          - Partial-accrue to land exactly at the cap if needed
          - LifetimeAccrued tracked for audit but does not gate accrual

        Returns:
            AccrualResult capturing the outcome (may be a zero-accrual result
            with a reason string).
        """
        # 1. Eligibility
        if payment_status != "ON_TIME":
            return AccrualResult(
                accrued_amount=Decimal("0.00"),
                new_balance=self._get_balance(self._lookup_household_id(lease_id)) or Decimal("0.00"),
                credit_transaction_id=None,
                reason_no_accrual="payment_status != ON_TIME",
            )

        if not deposit_funded:
            return AccrualResult(
                accrued_amount=Decimal("0.00"),
                new_balance=self._get_balance(self._lookup_household_id(lease_id)) or Decimal("0.00"),
                credit_transaction_id=None,
                reason_no_accrual="deposit_not_funded",
            )

        # 2. Resolve household
        household_id = self._lookup_household_id(lease_id)
        if household_id is None:
            if self.logger:
                self.logger.warning(
                    f"TCS accrual: no Household for LeaseID={lease_id}; skipping"
                )
            return AccrualResult(
                accrued_amount=Decimal("0.00"),
                new_balance=Decimal("0.00"),
                credit_transaction_id=None,
                reason_no_accrual="no_household_for_lease",
            )

        # 3. Compute basis and proposed accrual
        basis = Decimal(str(amount_collected)) - Decimal(str(arrears_amount)) - Decimal(str(late_fee_amount))
        if basis <= 0:
            # Atomic payment was 100% arrears+fees — no current-month rent component.
            # (Unusual for an ON_TIME row but defensive.)
            return AccrualResult(
                accrued_amount=Decimal("0.00"),
                new_balance=self._get_balance(household_id) or Decimal("0.00"),
                credit_transaction_id=None,
                reason_no_accrual="basis_non_positive",
            )

        proposed = (basis * self.credit_rate).quantize(Decimal("0.01"))

        # 4. Cap enforcement against CurrentBalance
        current_balance, lifetime_accrued, lifetime_redeemed = (
            self._get_balance_row(household_id)
        )
        headroom = self.credit_cap - current_balance
        if headroom <= 0:
            return AccrualResult(
                accrued_amount=Decimal("0.00"),
                new_balance=current_balance,
                credit_transaction_id=None,
                reason_no_accrual="cap_reached",
            )

        accrued = min(proposed, headroom).quantize(Decimal("0.01"))
        if accrued <= 0:
            return AccrualResult(
                accrued_amount=Decimal("0.00"),
                new_balance=current_balance,
                credit_transaction_id=None,
                reason_no_accrual="accrued_rounds_to_zero",
            )

        # 5. Insert ledger row + upsert balance (single logical transaction)
        new_balance = current_balance + accrued
        new_lifetime_accrued = lifetime_accrued + accrued

        credit_txn_id = self._insert_ledger_row(
            household_id=household_id,
            lease_id=lease_id,
            collection_id=collection_id,
            transaction_date=transaction_date,
            txn_type=TXN_ACCRUAL,
            amount=accrued,
            balance_after=new_balance,
            event_id=event_id,
            notes=(
                f"Accrual on basis ${basis:.2f} at rate {self.credit_rate}; "
                f"cap headroom was ${headroom:.2f}"
            ),
        )

        self._upsert_balance(
            household_id=household_id,
            new_current_balance=new_balance,
            new_lifetime_accrued=new_lifetime_accrued,
            lifetime_redeemed=lifetime_redeemed,
            last_accrual_date=transaction_date,
            last_updated_event_id=event_id,
        )

        # 6. Log TCS event
        self._log_accrual_event(
            household_id=household_id,
            accrued=accrued,
            new_balance=new_balance,
            transaction_date=transaction_date,
        )

        if self.logger:
            self.logger.debug(
                f"TCS accrual: HouseholdID={household_id}, accrued=${accrued:.2f}, "
                f"balance ${current_balance:.2f} → ${new_balance:.2f} "
                f"(cap=${self.credit_cap:.2f})"
            )

        return AccrualResult(
            accrued_amount=accrued,
            new_balance=new_balance,
            credit_transaction_id=credit_txn_id,
            reason_no_accrual=None,
        )

    def get_balance(self, household_id: int) -> Decimal:
        """Current credit balance for a household. Returns 0 if no row."""
        return self._get_balance(household_id) or Decimal("0.00")

    def get_lifetime_accrued(self, household_id: int) -> Decimal:
        """Total ever accrued for a household. Audit-only."""
        _, lifetime_accrued, _ = self._get_balance_row(household_id)
        return lifetime_accrued

    # ------------------------------------------------------------------
    # Public API — redemption
    # ------------------------------------------------------------------

    def should_routine_redeem(
        self,
        household_id: int,
        lease_id: int,
        monthly_rent: Decimal,
        current_date: date,
        deposit_funded: bool,
    ) -> bool:
        """
        Routine path eligibility check + RNG roll. Called when the household
        CAN pay this month — they may choose to redeem credit instead.

        Returns True iff every gate passes AND the household's
        TCSRedemptionProbability roll fires this month.
        """
        if not self._redemption_eligible(
            household_id, lease_id, monthly_rent, current_date, deposit_funded,
            require_good_standing=True,
        ):
            return False

        household_prob = self._get_household_redemption_probability(household_id)
        roll = self.redemption_rng.random()
        return Decimal(str(roll)) < household_prob

    def should_hardship_redeem(
        self,
        household_id: int,
        lease_id: int,
        monthly_rent: Decimal,
        current_date: date,
        deposit_funded: bool,
    ) -> bool:
        """
        Hardship path eligibility check + RNG roll. Called when the household
        CANNOT pay this month (payment_fail_prob roll fired) — they may use
        credit to prevent the month becoming MISSED.

        Same gates as routine PLUS strict good-standing requirement (must have
        entered the month with no prior unresolved arrears). Uses boosted
        probability per the hardship formula:

            hardship_probability = min(cap, max(base + boost, floor))
        """
        if not self._redemption_eligible(
            household_id, lease_id, monthly_rent, current_date, deposit_funded,
            require_good_standing=True,
        ):
            return False

        base_prob = self._get_household_redemption_probability(household_id)
        hardship_prob = min(
            self.hardship_cap,
            max(base_prob + self.hardship_boost, self.hardship_floor),
        )
        roll = self.redemption_rng.random()
        return Decimal(str(roll)) < hardship_prob

    def process_credit_redemption(
        self,
        household_id: int,
        lease_id: int,
        collection_id: Optional[int],
        monthly_rent: Decimal,
        transaction_date: date,
        event_id: int,
        path: str,
    ) -> RedemptionResult:
        """
        Apply a full-month-rent redemption.

        Inserts a `simulation.TenantCreditLedger` row with `TransactionType =
        'REDEMPTION'` and `Amount = -monthly_rent` (negative sign convention
        matches SecurityDepositManager REFUND/FORFEITURE/DAMAGE_WITHHOLD).
        Updates `simulation.TenantCreditBalance`:
          - CurrentBalance -= monthly_rent
          - LifetimeRedeemed += monthly_rent
          - AnnualRedemptionUsed = monthly_rent (lazy-reset if new year)
          - LastRedemptionYear = current year
          - LastUpdatedEventID = event_id

        Logs a TENANT_CREDIT event with metadata
        `TENANT_CREDIT/REDEMPTION_<PATH>: ...`.

        Caller is responsible for ensuring the eligibility check passed (via
        should_routine_redeem / should_hardship_redeem). This method does NOT
        re-check; it executes the redemption.

        Returns RedemptionResult.
        """
        assert path in ("ROUTINE", "HARDSHIP", "FINAL_MONTH_EXIT"), f"Invalid redemption path: {path}"

        monthly_rent = Decimal(str(monthly_rent)).quantize(Decimal("0.01"))

        current_balance, lifetime_accrued, lifetime_redeemed = (
            self._get_balance_row(household_id)
        )

        # FINAL_MONTH_EXIT may redeem
        # partially when balance < monthly_rent. ROUTINE / HARDSHIP remain
        # full-month-or-nothing (eligibility guarantees balance >= monthly_rent
        # upstream).
        if path == "FINAL_MONTH_EXIT":
            redemption_amount = min(current_balance, monthly_rent)
        else:
            redemption_amount = monthly_rent

        new_balance = current_balance - redemption_amount
        new_lifetime_redeemed = lifetime_redeemed + redemption_amount
        current_year = transaction_date.year

        # Compose the audit string to reflect partial vs full-month.
        is_partial = (path == "FINAL_MONTH_EXIT" and redemption_amount < monthly_rent)
        amount_descriptor = (
            f"partial-month settlement of ${redemption_amount:.2f} "
            f"(< one month's rent ${monthly_rent:.2f})"
            if is_partial
            else f"one month's rent ${redemption_amount:.2f}"
        )

        # Insert REDEMPTION ledger row (negative amount)
        credit_txn_id = self._insert_ledger_row(
            household_id=household_id,
            lease_id=lease_id,
            collection_id=collection_id,
            transaction_date=transaction_date,
            txn_type=TXN_REDEMPTION,
            amount=-redemption_amount,
            balance_after=new_balance,
            event_id=event_id,
            notes=(
                f"Redemption ({path}) {amount_descriptor}; "
                f"balance ${current_balance:.2f} → ${new_balance:.2f}"
            ),
        )

        # Update balance row — full state including annual tracking
        self._update_balance_for_redemption(
            household_id=household_id,
            new_current_balance=new_balance,
            lifetime_accrued=lifetime_accrued,
            new_lifetime_redeemed=new_lifetime_redeemed,
            redemption_amount=redemption_amount,
            current_year=current_year,
            transaction_date=transaction_date,
            event_id=event_id,
        )

        # Log to TCS audit lane
        self._log_redemption_event(
            household_id=household_id,
            redeemed_amount=redemption_amount,
            new_balance=new_balance,
            transaction_date=transaction_date,
            path=path,
        )

        if self.logger:
            self.logger.debug(
                f"TCS redemption ({path}): HouseholdID={household_id}, "
                f"redeemed=${redemption_amount:.2f} (rent=${monthly_rent:.2f}, "
                f"partial={is_partial}), balance ${current_balance:.2f} → "
                f"${new_balance:.2f}"
            )

        return RedemptionResult(
            redeemed_amount=redemption_amount,
            new_balance=new_balance,
            credit_transaction_id=credit_txn_id,
            path=path,
            reason_no_redemption=None,
        )

    # ------------------------------------------------------------------
    # Lifecycle (Portability & Expiry)
    # ------------------------------------------------------------------

    def mark_household_exited(self, household_id: int, exit_date: date) -> None:
        """
        Mark a household as having exited the LB system (non-eviction).

        Called by lease_renewal_manager when the household's last active LB
        lease ends. Sets `SystemExitDate = exit_date` and `ExpiryStatus =
        'EXITED'`, and sets `CreditExpiryDate = exit_date + TCS.PortabilityYears*12
        months` (previously a persisted DATEADD(24mo) computed column).

        Idempotent: if already EXITED, this is a no-op overwrite with the
        same data. Terminal states (EXPIRED, FORFEITED) are NOT overwritten.
        """
        self.db.execute_non_query(
            """
            UPDATE simulation.TenantCreditBalance
                SET SystemExitDate = ?,
                    ExpiryStatus = 'EXITED',
                    CreditExpiryDate = DATEADD(MONTH, ?, ?)
            WHERE RunID = ?
              AND HouseholdID = ?
              AND ExpiryStatus IN ('ACTIVE', 'EXITED')
            """,
            (exit_date, self.portability_months, exit_date, self.run_id, household_id),
        )

    def mark_household_reentered(self, household_id: int, reentry_date: date) -> None:
        """
        Mark a previously-EXITED household as having re-entered LB (signed a
        new lease before its portability-window expiry hit).

        The portability clock resets on re-entry. SystemExitDate
        and CreditExpiryDate are cleared; ExpiryStatus returns to 'ACTIVE'.

        If the household is already ACTIVE (no exit clock running), this is a
        no-op. Terminal states (EXPIRED, FORFEITED) are NOT reset — those
        credits are already gone.
        """
        self.db.execute_non_query(
            """
            UPDATE simulation.TenantCreditBalance
                SET SystemExitDate = NULL,
                    ExpiryStatus = 'ACTIVE',
                    CreditExpiryDate = NULL
            WHERE RunID = ?
              AND HouseholdID = ?
              AND ExpiryStatus = 'EXITED'
            """,
            (self.run_id, household_id),
        )

    def process_credit_forfeiture_eviction(
        self,
        household_id: int,
        eviction_event_id: int,
        execution_date: date,
    ) -> ForfeitureResult:
        """
        Forfeit all remaining TCS credit for a household whose lease was just
        evicted (eviction execution date is the trigger).

        Writes one FORFEITURE ledger row when balance > 0, logs a
        TENANT_CREDIT/FORFEITURE_EVICTION event, and flips ExpiryStatus to
        the terminal 'FORFEITED' state.

        If balance is already 0 (household had no credit), this still flips
        ExpiryStatus to 'FORFEITED' for terminal-state consistency, but writes
        no ledger row.
        """
        row = self._get_balance_row(household_id)
        if row is None or row[0] is None:
            # No balance row — household never accrued. Nothing to forfeit.
            return ForfeitureResult(
                forfeited_amount=Decimal("0.00"),
                new_balance=Decimal("0.00"),
                credit_transaction_id=None,
                forfeiture_type="EVICTION",
            )

        current_balance, lifetime_accrued, lifetime_redeemed = row
        forfeited_amount = current_balance if current_balance > 0 else Decimal("0.00")
        credit_txn_id: Optional[int] = None

        if forfeited_amount > 0:
            credit_txn_id = self._insert_ledger_row(
                household_id=household_id,
                lease_id=None,
                collection_id=None,
                transaction_date=execution_date,
                txn_type=TXN_FORFEITURE,
                amount=-forfeited_amount,
                balance_after=Decimal("0.00"),
                event_id=eviction_event_id,
                notes=(
                    f"FORFEITURE_EVICTION: HouseholdID={household_id} "
                    f"balance ${current_balance:.2f} → $0.00 at eviction execution"
                ),
            )

        # Update balance: zero out current, terminal status, audit timestamp
        self.db.execute_non_query(
            """
            UPDATE simulation.TenantCreditBalance
                SET CurrentBalance = 0,
                    ExpiryStatus = 'FORFEITED',
                    LastUpdatedEventID = ?
            WHERE RunID = ? AND HouseholdID = ?
            """,
            (eviction_event_id, self.run_id, household_id),
        )

        self._log_forfeiture_event(
            household_id=household_id,
            forfeited_amount=forfeited_amount,
            transaction_date=execution_date,
            forfeiture_type="EVICTION",
        )

        if self.logger:
            self.logger.debug(
                f"TCS forfeiture (EVICTION): HouseholdID={household_id}, "
                f"forfeited=${forfeited_amount:.2f}"
            )

        return ForfeitureResult(
            forfeited_amount=forfeited_amount,
            new_balance=Decimal("0.00"),
            credit_transaction_id=credit_txn_id,
            forfeiture_type="EVICTION",
        )

    def process_credit_expiry_sweep(
        self,
        sweep_date: date,
        sweep_event_id: int,
    ) -> list:
        """
        Monthly sweep: forfeit credit for EXITED households whose
        CreditExpiryDate <= sweep_date (month-end cadence with
        exact-date eligibility).

        For each such household:
          - If CurrentBalance > 0: insert FORFEITURE ledger row + log
            TENANT_CREDIT/FORFEITURE_EXPIRY event, set CurrentBalance = 0.
          - Always flip ExpiryStatus to terminal 'EXPIRED'.

        Returns list[ExpiryResult] — one entry per household swept.
        """
        eligible = self.db.execute_query(
            """
            SELECT HouseholdID, CurrentBalance
            FROM simulation.TenantCreditBalance
            WHERE RunID = ?
              AND ExpiryStatus = 'EXITED'
              AND CreditExpiryDate <= ?
            """,
            (self.run_id, sweep_date),
        )

        results: list = []
        if not eligible:
            return results

        for household_id, current_balance in eligible:
            current_balance = Decimal(str(current_balance or 0)).quantize(Decimal("0.01"))
            credit_txn_id: Optional[int] = None
            forfeited_amount = current_balance if current_balance > 0 else Decimal("0.00")

            if forfeited_amount > 0:
                credit_txn_id = self._insert_ledger_row(
                    household_id=household_id,
                    lease_id=None,
                    collection_id=None,
                    transaction_date=sweep_date,
                    txn_type=TXN_FORFEITURE,
                    amount=-forfeited_amount,
                    balance_after=Decimal("0.00"),
                    event_id=sweep_event_id,
                    notes=(
                        f"FORFEITURE_EXPIRY: HouseholdID={household_id} "
                        f"balance ${current_balance:.2f} → $0.00 at 24-month expiry"
                    ),
                )

            self.db.execute_non_query(
                """
                UPDATE simulation.TenantCreditBalance
                    SET CurrentBalance = 0,
                        ExpiryStatus = 'EXPIRED',
                        LastUpdatedEventID = ?
                WHERE RunID = ? AND HouseholdID = ?
                """,
                (sweep_event_id, self.run_id, household_id),
            )

            self._log_forfeiture_event(
                household_id=household_id,
                forfeited_amount=forfeited_amount,
                transaction_date=sweep_date,
                forfeiture_type="EXPIRY",
            )

            results.append(ExpiryResult(
                household_id=household_id,
                forfeited_amount=forfeited_amount,
                credit_transaction_id=credit_txn_id,
            ))

        if self.logger:
            total_forfeited = sum((r.forfeited_amount for r in results), Decimal("0"))
            self.logger.info(
                f"TCS expiry sweep on {sweep_date}: {len(results)} households "
                f"swept, ${total_forfeited:.2f} total forfeited"
            )

        return results

    def is_final_month_exit_eligible(
        self,
        household_id: int,
        lease_id: int,
        monthly_rent: Decimal,
        exit_date: date,
        deposit_funded: bool,
    ) -> bool:
        """
        Public eligibility check for final-month exit redemption — mirrors the
        `should_routine_redeem` / `should_hardship_redeem` pattern.

        Caller is responsible for confirming conditions 1 (non-
        eviction exit) and 2 (household is leaving LB), typically via the
        `Lease.RenewalDecision` field set by `lease_renewal_manager`'s
        pre-roll.

        This method covers the remaining conditions:
          3. ~~No prior TCS redemption in this calendar year~~
             exempt for FINAL_MONTH_EXIT. Routine/hardship still
             gated by annual limit; exit redemption is a settlement action
             and bypasses it.
          4. ~~CurrentBalance >= monthly_rent~~
             relaxed to CurrentBalance > 0. Redemption amount becomes
             MIN(balance, monthly_rent) (partial-month at exit).
          5. Lease in good standing (no prior unresolved MISSED months) — KEPT
          + defensive: lease not currently in eviction

        Final-month exit redemption is deterministic when eligible (P=1.0) —
        no RNG draw here.
        """
        return self._redemption_eligible(
            household_id=household_id,
            lease_id=lease_id,
            monthly_rent=monthly_rent,
            current_date=exit_date,
            deposit_funded=deposit_funded,
            require_good_standing=True,
            path="FINAL_MONTH_EXIT",
        )

    def process_final_month_exit_redemption(
        self,
        household_id: int,
        lease_id: int,
        collection_id: Optional[int],
        monthly_rent: Decimal,
        exit_date: date,
        event_id: int,
        deposit_funded: bool,
    ) -> Optional[RedemptionResult]:
        """
        Apply TCS to the final-month rent of a household exiting in good
        standing.

        Eligibility:
          1. Non-eviction exit (caller verifies via lease.RenewalDecision)
          2. Household leaving the LB system (caller verifies)
          3. ~~Annual-limit~~ — EXEMPT for FINAL_MONTH_EXIT
          4. CurrentBalance > 0 (partial-month allowed)
          5. Obligation is rent (caller guarantees)
          + good-standing (require_good_standing=True) — KEPT

        The redemption amount is MIN(CurrentBalance, monthly_rent), computed
        downstream in process_credit_redemption when path='FINAL_MONTH_EXIT'.

        Final-month exit redemption is deterministic (P=1.0) when eligible —
        no RNG draw. FME does NOT count toward the
        annual redemption limit; routine/hardship same-year is still permitted.

        Returns RedemptionResult on success, None if not eligible (caller
        falls back to normal rent collection).
        """
        if not self._redemption_eligible(
            household_id=household_id,
            lease_id=lease_id,
            monthly_rent=monthly_rent,
            current_date=exit_date,
            deposit_funded=deposit_funded,
            require_good_standing=True,
            path="FINAL_MONTH_EXIT",
        ):
            return None

        return self.process_credit_redemption(
            household_id=household_id,
            lease_id=lease_id,
            collection_id=collection_id,
            monthly_rent=monthly_rent,
            transaction_date=exit_date,
            event_id=event_id,
            path="FINAL_MONTH_EXIT",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _lookup_household_id(self, lease_id: int) -> Optional[int]:
        """Resolve HouseholdID from LeaseID for the current run."""
        result = self.db.execute_query(
            "SELECT HouseholdID FROM simulation.Lease WHERE RunID = ? AND LeaseID = ?",
            (self.run_id, lease_id),
        )
        if not result:
            return None
        return result[0][0]

    def _get_balance(self, household_id: int) -> Optional[Decimal]:
        """Return CurrentBalance, or None if no row exists."""
        if household_id is None:
            return None
        result = self.db.execute_query(
            "SELECT CurrentBalance FROM simulation.TenantCreditBalance "
            "WHERE RunID = ? AND HouseholdID = ?",
            (self.run_id, household_id),
        )
        if not result:
            return None
        return Decimal(str(result[0][0]))

    def _get_balance_row(self, household_id: int) -> tuple:
        """Return (CurrentBalance, LifetimeAccrued, LifetimeRedeemed); zeros if no row."""
        result = self.db.execute_query(
            "SELECT CurrentBalance, LifetimeAccrued, LifetimeRedeemed "
            "FROM simulation.TenantCreditBalance "
            "WHERE RunID = ? AND HouseholdID = ?",
            (self.run_id, household_id),
        )
        if not result:
            return (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        row = result[0]
        return (
            Decimal(str(row[0])),
            Decimal(str(row[1])),
            Decimal(str(row[2])),
        )

    def _insert_ledger_row(
        self,
        household_id: int,
        lease_id: int,
        collection_id: Optional[int],
        transaction_date: date,
        txn_type: str,
        amount: Decimal,
        balance_after: Decimal,
        event_id: int,
        notes: Optional[str],
    ) -> int:
        """Insert a TenantCreditLedger row and return the new CreditTransactionID."""
        # Reseed counter from DB (survives manager re-instantiation)
        result = self.db.execute_query(
            "SELECT ISNULL(MAX(CreditTransactionID), 0) + 1 "
            "FROM simulation.TenantCreditLedger WHERE RunID = ?",
            (self.run_id,),
        )
        credit_txn_id = result[0][0] if result else 1
        self._next_credit_transaction_id = credit_txn_id + 1

        insert_query = """
            INSERT INTO simulation.TenantCreditLedger (
                RunID, CreditTransactionID, HouseholdID, TransactionDate,
                TransactionType, Amount, BalanceAfter, RelatedCollectionID,
                RelatedLeaseID, CreatedEventID, Notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                credit_txn_id,
                household_id,
                transaction_date,
                txn_type,
                amount,
                balance_after,
                collection_id,
                lease_id,
                event_id,
                notes,
            ),
        )
        return credit_txn_id

    def _upsert_balance(
        self,
        household_id: int,
        new_current_balance: Decimal,
        new_lifetime_accrued: Decimal,
        lifetime_redeemed: Decimal,
        last_accrual_date: date,
        last_updated_event_id: int,
    ) -> None:
        """
        Upsert a TenantCreditBalance row. Used by accrual; future
        REDEMPTION/FORFEITURE will have their own update paths.
        """
        # Defensive invariant — if the balance row is in
        # 'EXITED' state and an accrual writes (a positive flow that should
        # only happen for a household with an active lease), auto-flip to
        # 'ACTIVE'. This closes any ordering window where an accrual fires
        # before mark_household_reentered is called. Terminal states
        # ('EXPIRED', 'FORFEITED') are intentionally preserved — those
        # credits are already gone and the audit signal stays.
        merge_query = """
            MERGE simulation.TenantCreditBalance AS target
            USING (SELECT ? AS RunID, ? AS HouseholdID) AS source
                ON target.RunID = source.RunID AND target.HouseholdID = source.HouseholdID
            WHEN MATCHED THEN
                UPDATE SET
                    CurrentBalance = ?,
                    LifetimeAccrued = ?,
                    LifetimeRedeemed = ?,
                    LastAccrualDate = ?,
                    LastUpdatedEventID = ?,
                    ExpiryStatus = CASE WHEN target.ExpiryStatus = 'EXITED'
                                        THEN 'ACTIVE'
                                        ELSE target.ExpiryStatus END,
                    SystemExitDate = CASE WHEN target.ExpiryStatus = 'EXITED'
                                          THEN NULL
                                          ELSE target.SystemExitDate END,
                    CreditExpiryDate = CASE WHEN target.ExpiryStatus = 'EXITED'
                                            THEN NULL
                                            ELSE target.CreditExpiryDate END
            WHEN NOT MATCHED THEN
                INSERT (RunID, HouseholdID, CurrentBalance, LifetimeAccrued,
                        LifetimeRedeemed, LastAccrualDate, LastUpdatedEventID)
                VALUES (source.RunID, source.HouseholdID, ?, ?, ?, ?, ?);
        """
        self.db.execute_non_query(
            merge_query,
            (
                self.run_id, household_id,
                # UPDATE clause
                new_current_balance, new_lifetime_accrued, lifetime_redeemed,
                last_accrual_date, last_updated_event_id,
                # INSERT clause
                new_current_balance, new_lifetime_accrued, lifetime_redeemed,
                last_accrual_date, last_updated_event_id,
            ),
        )

    def _log_accrual_event(
        self,
        household_id: int,
        accrued: Decimal,
        new_balance: Decimal,
        transaction_date: date,
    ) -> None:
        """Log an accrual to simulation.Event under the TENANT_CREDIT lane."""
        if not self.event_logger:
            return
        # Use the new TENANT_CREDIT ActionType (separate audit lane)
        # plus a "TENANT_CREDIT/ACCRUAL: ..." metadata prefix matching the
        # codebase domain-event convention.
        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=transaction_date,
            entity_type=EntityType.HOUSEHOLD,
            entity_id=household_id,
            action_type=ActionType.TENANT_CREDIT,
            amount=accrued,
            metadata=(
                f"TENANT_CREDIT/ACCRUAL: HouseholdID={household_id}, "
                f"accrued=${accrued:.2f}, balance_after=${new_balance:.2f}"
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers — redemption
    # ------------------------------------------------------------------

    def _redemption_eligible(
        self,
        household_id: int,
        lease_id: int,
        monthly_rent: Decimal,
        current_date: date,
        deposit_funded: bool,
        require_good_standing: bool,
        path: Optional[str] = None,
    ) -> bool:
        """
        Combined eligibility check for routine + hardship + FINAL_MONTH_EXIT.

        Path-specific tuning:
          - For ROUTINE / HARDSHIP (path None or those values): balance must be
            >= monthly_rent (full-month redemption), AND annual-limit applies.
          - For FINAL_MONTH_EXIT: balance must be > 0 (partial-month allowed at
            exit), AND the annual-limit is EXEMPT (final-month exit is a
            settlement action, not normal redemption).
          - Good-standing (when require_good_standing=True) and lease-in-eviction
            gates apply uniformly.

        Gates:
          - deposit_funded (passed in by caller)
          - Balance:  > 0 for FINAL_MONTH_EXIT;  >= monthly_rent otherwise
          - Annual-limit:  skipped for FINAL_MONTH_EXIT;  enforced otherwise
          - require_good_standing → no unresolved prior MISSED rows
          - Lease not in eviction
        """
        if not deposit_funded:
            return False

        current_balance, _, _ = self._get_balance_row(household_id)

        # Balance threshold is path-dependent.
        if path == "FINAL_MONTH_EXIT":
            if current_balance <= 0:
                return False
        else:
            if current_balance < monthly_rent:
                return False

        # Annual-limit is EXEMPT for FINAL_MONTH_EXIT.
        # Routine/hardship paths still gated by the calendar-year limit.
        if path != "FINAL_MONTH_EXIT":
            annual_row = self.db.execute_query(
                "SELECT LastRedemptionYear FROM simulation.TenantCreditBalance "
                "WHERE RunID = ? AND HouseholdID = ?",
                (self.run_id, household_id),
            )
            if annual_row and annual_row[0][0] is not None:
                if int(annual_row[0][0]) >= current_date.year:
                    return False  # Already redeemed in this calendar year

        if require_good_standing:
            current_billing_month = date(current_date.year, current_date.month, 1)
            if not self._entered_in_good_standing(lease_id, current_billing_month):
                return False

        if self._lease_in_eviction(lease_id):
            return False

        return True

    def _get_household_redemption_probability(self, household_id: int) -> Decimal:
        """
        Read the household's TCSRedemptionProbability. Falls back to
        redemption_prob_default if no row (defensive — shouldn't happen
        post-V00027 since the column has NOT NULL DEFAULT).
        """
        result = self.db.execute_query(
            "SELECT TCSRedemptionProbability FROM simulation.Household "
            "WHERE RunID = ? AND HouseholdID = ?",
            (self.run_id, household_id),
        )
        if not result or result[0][0] is None:
            return self.redemption_prob_default
        return Decimal(str(result[0][0]))

    def _entered_in_good_standing(self, lease_id: int, current_billing_month: date) -> bool:
        """
        True iff the lease has no unresolved MISSED MonthlyPaymentStatus rows
        for billing months strictly before current_billing_month.

        "Unresolved" means PaymentStatus = 'MISSED' AND ActualPaymentDate IS NULL.
        Matches the arrears definition.
        """
        result = self.db.execute_query(
            """
            SELECT COUNT(*)
            FROM simulation.MonthlyPaymentStatus
            WHERE RunID = ? AND LeaseID = ?
              AND BillingMonth < ?
              AND PaymentStatus = 'MISSED'
              AND ActualPaymentDate IS NULL
            """,
            (self.run_id, lease_id, current_billing_month),
        )
        return result[0][0] == 0 if result else True

    def _lease_in_eviction(self, lease_id: int) -> bool:
        """
        True iff the lease has a filed-but-not-yet-executed eviction.

        Eviction state is tracked in `simulation.LeaseTermination`:
          - EVICTION_FILED: EvictionFiledDate is set
          - EVICTION_EXECUTED: EvictionExecutionDate is set
        A lease is "in eviction" between filing and execution — credit
        redemption should not fire during that window.
        """
        result = self.db.execute_query(
            """
            SELECT COUNT(*)
            FROM simulation.LeaseTermination
            WHERE RunID = ? AND LeaseID = ?
              AND EvictionFiledDate IS NOT NULL
              AND EvictionExecutionDate IS NULL
            """,
            (self.run_id, lease_id),
        )
        return result[0][0] > 0 if result else False

    def _update_balance_for_redemption(
        self,
        household_id: int,
        new_current_balance: Decimal,
        lifetime_accrued: Decimal,
        new_lifetime_redeemed: Decimal,
        redemption_amount: Decimal,
        current_year: int,
        transaction_date: date,
        event_id: int,
    ) -> None:
        """
        Update TenantCreditBalance for a redemption. AnnualRedemptionUsed
        lazy-resets when LastRedemptionYear < current_year (no Jan 1 batch job).
        """
        update_query = """
            UPDATE simulation.TenantCreditBalance
            SET CurrentBalance = ?,
                LifetimeRedeemed = ?,
                AnnualRedemptionUsed = ?,
                LastRedemptionYear = ?,
                LastUpdatedEventID = ?
            WHERE RunID = ? AND HouseholdID = ?
        """
        # Lazy reset: AnnualRedemptionUsed becomes just this redemption's amount
        # (it's the first redemption in current_year for this household; the
        # eligibility check guaranteed LastRedemptionYear < current_year).
        self.db.execute_non_query(
            update_query,
            (
                new_current_balance,
                new_lifetime_redeemed,
                redemption_amount,   # AnnualRedemptionUsed = this redemption's amount
                current_year,
                event_id,
                self.run_id, household_id,
            ),
        )

    def _log_redemption_event(
        self,
        household_id: int,
        redeemed_amount: Decimal,
        new_balance: Decimal,
        transaction_date: date,
        path: str,
    ) -> None:
        """
        Log a redemption to simulation.Event under the TENANT_CREDIT lane.
        Metadata format: "TENANT_CREDIT/REDEMPTION_<ROUTINE|HARDSHIP>: ...".
        """
        if not self.event_logger:
            return
        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=transaction_date,
            entity_type=EntityType.HOUSEHOLD,
            entity_id=household_id,
            action_type=ActionType.TENANT_CREDIT,
            amount=-redeemed_amount,   # negative to mirror ledger sign
            metadata=(
                f"TENANT_CREDIT/REDEMPTION_{path}: HouseholdID={household_id}, "
                f"redeemed=${redeemed_amount:.2f}, balance_after=${new_balance:.2f}"
            ),
        )

    def _log_forfeiture_event(
        self,
        household_id: int,
        forfeited_amount: Decimal,
        transaction_date: date,
        forfeiture_type: str,
    ) -> None:
        """
        Log a forfeiture to simulation.Event under the TENANT_CREDIT lane.

        Metadata format:
          TENANT_CREDIT/FORFEITURE_EVICTION: ...
          TENANT_CREDIT/FORFEITURE_EXPIRY: ...

        Both share the same TransactionType in the ledger ('FORFEITURE') per
        the V00026 enum; the subtype distinction lives here in the event lane.
        """
        if not self.event_logger:
            return
        assert forfeiture_type in ("EVICTION", "EXPIRY"), (
            f"Invalid forfeiture_type: {forfeiture_type}"
        )
        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=transaction_date,
            entity_type=EntityType.HOUSEHOLD,
            entity_id=household_id,
            action_type=ActionType.TENANT_CREDIT,
            amount=-forfeited_amount,
            metadata=(
                f"TENANT_CREDIT/FORFEITURE_{forfeiture_type}: "
                f"HouseholdID={household_id}, forfeited=${forfeited_amount:.2f}"
            ),
        )
