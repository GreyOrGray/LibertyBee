"""
Property Acquisition Manager - Economic optimization with pre-acquisition pipeline

This module implements Liberty Bee's complete property acquisition workflow:
1. CSF protection and grace period validation
2. Budget calculation
3. Income-to-price ratio optimization for property selection
4. Pre-acquisition pipeline:
   - Offer generation and seller response
   - Professional inspection
   - Negotiation and concessions
   - Closing process
5. Property purchase transaction and ownership transfer

Key Features:
- CSF grace period: First 6 months bypass CSF requirement to enable initial revenue
- Economic optimization: Select properties by best income-to-price ratio (not random)
- CSF protection: After month 6, acquisitions only allowed when CSF meets target
- Realistic pre-acquisition pipeline with offers, inspections, negotiations, closing
- Seeded RNG for reproducible results (SEED_OFFSET +1000)
- Complete audit trail via event logging
- PropertyAcquisitionAttempt tracking for all attempts (success and failure)

Usage:
- Use PropertyAcquisitionManager.evaluate_and_acquire() to attempt property purchase
- Returns acquired property details or None if no suitable property found
"""

import logging
import random
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from configuration_loader import ConfigurationLoader, ProjectionConfig
from fund_manager import FundManager
from event_logger import EventLogger, EventType, EntityType, ActionType


@dataclass
class PropertyCandidate:
    """Property candidate for acquisition with calculated metrics"""
    property_id: int
    address: str
    list_price: Decimal
    unit_count: int
    total_annual_rent: Decimal
    income_to_price_ratio: float
    vacancy_adjusted_rent: Decimal


@dataclass(frozen=True)
class AcquisitionGateBases:
    """KD-042 (ratified design): the acquisition gate's TWO OpEx bases
    plus the one-time onboarding lump — one consistent snapshot.

    - ``owned_opex``: the standard expected basis on OWNED units/properties only.
      Feeds the CSF reserve earmark and nothing else on the gate side (ratified:
      the reserve target is defined on properties OWNED and is never
      forward-priced — threading pro-forma into ``_reserve_earmark`` would
      silently violate that ruling, which is why the gate carries two values).
    - ``pro_forma_opex``: owned + the marginal expected carrying (set-wise
      f(owned+pipeline) − f(owned): per-unit buckets, exterior, E[maintenance],
      E[turnover], staffing step) of every ``IsActive = 1`` acquisition
      attempt (membership rule: intent-committed, not CashHold-held) — plus
      the candidate under consideration when re-gating (the post-commitment
      test). Feeds the Rule 2/3 floor and the acquisition budget.
    - ``onboarding_lump``: Σ over the same set of E[closing costs +
      due-diligence repair + compliance onboarding chains (incl. lead)].
      Added to the required floor ONCE — never multiplied by
      CashFloorMonths (ratified).

    Built by ``Simulation.compute_acquisition_gate_bases`` (the single
    source of truth); the gate never derives its own floor inputs.
    """
    owned_opex: Decimal
    pro_forma_opex: Decimal
    onboarding_lump: Decimal
    pipeline_count: int
    pipeline_units: int
    pipeline_properties: int

    @property
    def pipeline_marginal(self) -> Decimal:
        return self.pro_forma_opex - self.owned_opex


@dataclass
class AcquisitionParameters:
    """Acquisition pipeline configuration parameters"""
    # Offer strategy
    offer_strategy_below_ask_pct: float
    offer_strategy_at_ask_pct: float
    offer_strategy_premium_pct: float
    offer_strategy_below_ask_weight: float
    offer_strategy_at_ask_weight: float
    offer_strategy_premium_weight: float

    # Seller response
    seller_acceptance_probability: float
    seller_counter_probability: float
    seller_rejection_probability: float
    seller_counter_increase_pct: float

    # Inspection
    inspection_clean_probability: float
    inspection_minor_probability: float
    inspection_moderate_probability: float
    inspection_major_probability: float
    inspection_cost_min: Decimal
    inspection_cost_max: Decimal

    # Negotiation
    lb_withdrawal_threshold_pct: float
    price_reduction_probability: float
    seller_repairs_probability: float
    no_concessions_probability: float

    # Closing
    closing_delay_days_min: int
    closing_delay_days_max: int
    closing_costs_pct: float
    seller_repair_delay_days_min: int
    seller_repair_delay_days_max: int

    # Capacity management
    max_concurrent_pipelines_base: int
    max_concurrent_pipelines_ramp_multiplier: float

    # Stage timeouts
    timeout_offer_response_max_days: int
    timeout_inspection_scheduling_max_days: int
    timeout_negotiation_max_days: int
    timeout_closing_max_days: int

    # Stage delays
    seller_response_delay_days_min: int
    seller_response_delay_days_max: int
    inspection_scheduling_delay_min: int
    inspection_scheduling_delay_max: int
    negotiation_delay_days_min: int
    negotiation_delay_days_max: int


class PortfolioCounts:
    """Owned-portfolio counts cache (step 0.5). simulation.PropertyUnits has
    exactly ONE insert site (add_property_to_portfolio below) and no deletes;
    status UPDATEs don't change row counts. The writer invalidates after each
    closing; readers get in-memory hits instead of the ~43k per-run
    COUNT / GROUP BY round-trips (measured). A month-end fail-loud verify in
    simulation.py guards the single-writer assumption against future write
    sites — a stale cache dies within one sim-month, loudly."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._counts = None  # (run_id, owned_units, owned_properties, owned_by_town)

    def get(self, run_id: int):
        """-> (owned_units, owned_properties, owned_by_town rows)"""
        if self._counts is None or self._counts[0] != run_id:
            row = self.db.execute_query(
                "SELECT COUNT(*), COUNT(DISTINCT PropertyID) "
                "FROM simulation.PropertyUnits WHERE RunID = ?", (run_id,))
            by_town = self.db.execute_query(
                "SELECT p.Town, COUNT(*) FROM simulation.PropertyUnits pu "
                "JOIN reference.Properties p ON p.PropertyID = pu.PropertyID "
                "WHERE pu.RunID = ? GROUP BY p.Town", (run_id,))
            self._counts = (run_id,
                            int(row[0][0]) if row else 0,
                            int(row[0][1]) if row else 0,
                            [(t, int(n)) for t, n in by_town])
        return self._counts[1], self._counts[2], self._counts[3]

    def invalidate(self):
        self._counts = None


class PropertyAcquisitionManager:
    """Manage property acquisition with economic optimization, pre-acquisition pipeline, and CSF protection"""

    # Seed offset for pre-acquisition RNG (deterministic but independent of other systems)
    SEED_OFFSET = 1000

    def __init__(self, db_manager: DatabaseManager, config_loader: ConfigurationLoader,
                 fund_manager: FundManager, event_logger: EventLogger, random_seed: int = 12345,
                 portfolio_counts: 'PortfolioCounts' = None):
        self.db = db_manager
        self.config_loader = config_loader
        self.fund_manager = fund_manager
        self.portfolio_counts = portfolio_counts or PortfolioCounts(db_manager)
        self.random_seed = random_seed
        self.rng = random.Random(random_seed + self.SEED_OFFSET)
        self.logger = logging.getLogger(__name__)
        self.event_logger = event_logger

        # CSF acquisition-gate grace + acquisition ramp window
        # from the registry — were hardcoded 6-month literals (distinct policies
        # that share a value today). Read-once, fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.csf_grace_period_months = self.registry.get_int('CSF', 'GracePeriodMonths')
        self.ramp_period_months = self.registry.get_int('PROP', 'RampPeriodMonths')

        # Negotiation-stage
        # literals + inspection repair-cost $ bands are ACQ registry knobs,
        # seeded with the exact former literals (value-neutral).
        self.counter_accept_probability = self.registry.get_float('ACQ', 'CounterAcceptProbability')
        self.counter_jitter_low_factor = self.registry.get_float('ACQ', 'CounterJitterLowFactor')
        self.counter_jitter_high_factor = self.registry.get_float('ACQ', 'CounterJitterHighFactor')
        self.counter_response_delay_days = self.registry.get_int('ACQ', 'CounterResponseDelayDays')
        self.counter_decision_timeout_days = self.registry.get_int('ACQ', 'CounterDecisionTimeoutDays')
        self.inspection_schedule_delay_days = self.registry.get_int('ACQ', 'InspectionScheduleDelayDays')
        self.repair_cost_bands = {
            'Clean': (self.registry.get_int('ACQ', 'RepairCostCleanMinUSD'),
                      self.registry.get_int('ACQ', 'RepairCostCleanMaxUSD')),
            'Minor': (self.registry.get_int('ACQ', 'RepairCostMinorMinUSD'),
                      self.registry.get_int('ACQ', 'RepairCostMinorMaxUSD')),
            'Moderate': (self.registry.get_int('ACQ', 'RepairCostModerateMinUSD'),
                         self.registry.get_int('ACQ', 'RepairCostModerateMaxUSD')),
            'Major': (self.registry.get_int('ACQ', 'RepairCostMajorMinUSD'),
                      self.registry.get_int('ACQ', 'RepairCostMajorMaxUSD')),
        }
        self.price_reduction_share_low = self.registry.get_float('ACQ', 'PriceReductionShareLow')
        self.price_reduction_share_high = self.registry.get_float('ACQ', 'PriceReductionShareHigh')

        self.run_id = None
        self.config = None  # Will be set via set_config()
        self.acquisition_params: Optional[AcquisitionParameters] = None
        # KD-042: bound Simulation.compute_acquisition_gate_bases — the gate's
        # only source of floor inputs (set via set_gate_basis_provider).
        self.gate_basis_provider = None
        # KD-027 fix: wired via set_employee_manager so post-acquisition
        # `check_staffing_needs` calls can fire after each close.
        self.employee_manager = None
        # KD-193: bound ComplianceManager.assess_property_lead_for_acquisition —
        # the pre-closing lead assessor (set via set_lead_assessor). None ⇒ no
        # lead assessment (legacy/test path; AssessedLeadCost stays NULL).
        self.lead_assessor = None

    def set_lead_assessor(self, assessor) -> None:
        """KD-193: inject the pre-closing lead assessor
        (ComplianceManager.assess_property_lead_for_acquisition). Called at
        the inspection stage: the org learns the property's actual abatement
        cost during due diligence, so it informs the carry/withdraw decision
        and is stored-and-forwarded to the post-closing abatement."""
        self.lead_assessor = assessor

    def set_gate_basis_provider(self, provider) -> None:
        """KD-042: inject the two-basis snapshot callable
        (Simulation.compute_acquisition_gate_bases). Fail-loud contract: the
        opportunity scanner refuses to run without it — a gate with no
        pro-forma basis would silently regress to the KD-042 defect."""
        self.gate_basis_provider = provider

    def expected_due_diligence_repair_cost(self) -> Decimal:
        """KD-042: E[acquisition repair cost] before the inspection draw —
        severity distribution (Clean/Minor/Moderate/Major probabilities from
        reference.AcquisitionParameters) × the uniform ACQ band means. Once a
        pipeline's inspection HAS drawn, the stored EstimatedRepairCost
        replaces this expectation (price what is known the moment it is
        known). Note the 8% withdrawal threshold truncates the realized major
        tail pre-closing, so this raw expectation sits slightly conservative
        — the permitted direction."""
        if not self.acquisition_params:
            self.load_acquisition_parameters()
        p = self.acquisition_params
        weights = {
            'Clean': Decimal(str(p.inspection_clean_probability)),
            'Minor': Decimal(str(p.inspection_minor_probability)),
            'Moderate': Decimal(str(p.inspection_moderate_probability)),
            'Major': Decimal(str(p.inspection_major_probability)),
        }
        expected = Decimal('0')
        for severity, (lo, hi) in self.repair_cost_bands.items():
            expected += weights[severity] * (Decimal(lo) + Decimal(hi)) / 2
        return expected.quantize(Decimal('0.01'))

    def set_run_id(self, run_id: int):
        """Set current run ID for acquisition operations"""
        self.run_id = run_id

    def set_config(self, config: ProjectionConfig):
        """Set projection configuration for CSF calculations"""
        self.config = config

    def set_employee_manager(self, employee_manager) -> None:
        """Wire EmployeeManager for post-acquisition staffing trigger."""
        self.employee_manager = employee_manager
        
    def load_acquisition_parameters(self) -> AcquisitionParameters:
        """Load acquisition pipeline parameters from database"""
        query = """
        SELECT
            OfferStrategyBelowAskPct, OfferStrategyAtAskPct, OfferStrategyPremiumPct,
            OfferStrategyBelowAskWeight, OfferStrategyAtAskWeight, OfferStrategyPremiumWeight,
            SellerAcceptanceProbability, SellerCounterProbability, SellerRejectionProbability,
            SellerCounterIncreasePct,
            InspectionCleanProbability, InspectionMinorProbability, InspectionModerateProbability,
            InspectionMajorProbability, InspectionCostMin, InspectionCostMax,
            LBWithdrawalThresholdPct, PriceReductionProbability, SellerRepairsProbability,
            NoConcesssProbability,
            ClosingDelayDaysMin, ClosingDelayDaysMax, ClosingCostsPct,
            SellerRepairDelayDaysMin, SellerRepairDelayDaysMax,
            MaxConcurrentPipelines_Base, MaxConcurrentPipelines_RampMultiplier,
            Timeout_OfferResponse_MaxDays, Timeout_InspectionScheduling_MaxDays,
            Timeout_Negotiation_MaxDays, Timeout_Closing_MaxDays,
            SellerResponseDelayDaysMin, SellerResponseDelayDaysMax,
            InspectionSchedulingDelayMin, InspectionSchedulingDelayMax,
            NegotiationDelayDaysMin, NegotiationDelayDaysMax
        FROM reference.AcquisitionParameters
        ORDER BY ParameterID DESC
        OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
        """

        result = self.db.execute_query(query)
        if not result:
            raise ValueError("No acquisition parameters found in database")

        row = result[0]
        self.acquisition_params = AcquisitionParameters(
            offer_strategy_below_ask_pct=float(row[0]),
            offer_strategy_at_ask_pct=float(row[1]),
            offer_strategy_premium_pct=float(row[2]),
            offer_strategy_below_ask_weight=float(row[3]),
            offer_strategy_at_ask_weight=float(row[4]),
            offer_strategy_premium_weight=float(row[5]),
            seller_acceptance_probability=float(row[6]),
            seller_counter_probability=float(row[7]),
            seller_rejection_probability=float(row[8]),
            seller_counter_increase_pct=float(row[9]),
            inspection_clean_probability=float(row[10]),
            inspection_minor_probability=float(row[11]),
            inspection_moderate_probability=float(row[12]),
            inspection_major_probability=float(row[13]),
            inspection_cost_min=Decimal(str(row[14])),
            inspection_cost_max=Decimal(str(row[15])),
            lb_withdrawal_threshold_pct=float(row[16]),
            price_reduction_probability=float(row[17]),
            seller_repairs_probability=float(row[18]),
            no_concessions_probability=float(row[19]),
            closing_delay_days_min=int(row[20]),
            closing_delay_days_max=int(row[21]),
            closing_costs_pct=float(row[22]),
            seller_repair_delay_days_min=int(row[23]),
            seller_repair_delay_days_max=int(row[24]),
            max_concurrent_pipelines_base=int(row[25]),
            max_concurrent_pipelines_ramp_multiplier=float(row[26]),
            timeout_offer_response_max_days=int(row[27]),
            timeout_inspection_scheduling_max_days=int(row[28]),
            timeout_negotiation_max_days=int(row[29]),
            timeout_closing_max_days=int(row[30]),
            seller_response_delay_days_min=int(row[31]),
            seller_response_delay_days_max=int(row[32]),
            inspection_scheduling_delay_min=int(row[33]),
            inspection_scheduling_delay_max=int(row[34]),
            negotiation_delay_days_min=int(row[35]),
            negotiation_delay_days_max=int(row[36])
        )

        return self.acquisition_params

    # ========================================
    # HELPER METHODS - State-Based Processing
    # ========================================

    def get_max_concurrent_pipelines(self, month_index: int) -> int:
        """
        Calculate maximum concurrent pipelines based on month

        First 6 months: base * ramp_multiplier (e.g., 3 * 2.0 = 6)
        After month 6: base (e.g., 3)

        Args:
            month_index: Current simulation month (1-240)

        Returns:
            Maximum concurrent acquisition pipelines allowed
        """
        if not self.acquisition_params:
            self.load_acquisition_parameters()

        if month_index <= self.ramp_period_months:
            # Ramp period - allow more concurrent acquisitions
            return int(self.acquisition_params.max_concurrent_pipelines_base *
                       self.acquisition_params.max_concurrent_pipelines_ramp_multiplier)
        else:
            # Normal operations
            return self.acquisition_params.max_concurrent_pipelines_base

    def get_active_pipeline_count(self) -> int:
        """
        Count currently active acquisition pipelines

        Returns:
            Number of active attempts (IsActive=1)
        """
        query = """
        SELECT COUNT(*)
        FROM simulation.PropertyAcquisitionAttempt
        WHERE RunID = ? AND IsActive = 1
        """

        result = self.db.execute_query(query, (self.run_id,))
        return result[0][0] if result else 0

    def get_active_attempts(self, current_date: date) -> List[Dict[str, Any]]:
        """
        Fetch all active attempts that need processing today

        Returns attempts where:
        - IsActive = 1
        - NextActionDate <= current_date OR StageTimeoutDate <= current_date

        Args:
            current_date: Current simulation date

        Returns:
            List of attempt records with all fields
        """
        query = """
        SELECT
            RunID, AttemptID, PropertyID, PipelineStage, NextActionDate,
            StageTimeoutDate, StageEnteredDate,
            OfferDate, ListingPrice, OfferAmount, OfferStrategy,
            SellerResponse, CounterAmount, LBResponseToCounter,
            InspectionDate, InspectionSeverity, InspectionCost,
            EstimatedRepairCost, NegotiationOutcome,
            AdjustedPurchasePrice, ClosingDate, FinalPurchasePrice
        FROM simulation.PropertyAcquisitionAttempt
        WHERE RunID = ?
          AND IsActive = 1
          AND (NextActionDate <= ? OR StageTimeoutDate <= ?)
        ORDER BY NextActionDate, AttemptID
        """

        result = self.db.execute_query(query, (self.run_id, current_date, current_date))

        # Convert to list of dicts for easier handling
        attempts = []
        for row in result:
            attempts.append({
                'run_id': row[0],
                'attempt_id': row[1],
                'property_id': row[2],
                'pipeline_stage': row[3],
                'next_action_date': row[4],
                'stage_timeout_date': row[5],
                'stage_entered_date': row[6],
                'offer_date': row[7],
                'listing_price': row[8],
                'offer_amount': row[9],
                'offer_strategy': row[10],
                'seller_response': row[11],
                'counter_amount': row[12],
                'lb_response_to_counter': row[13],
                'inspection_date': row[14],
                'inspection_severity': row[15],
                'inspection_cost': row[16],
                'estimated_repair_cost': row[17],
                'negotiation_outcome': row[18],
                'adjusted_purchase_price': row[19],
                'closing_date': row[20],
                'final_purchase_price': row[21]
            })

        return attempts

    def check_property_still_available(self, property_id: int, current_date: date) -> bool:
        """
        Check if property is still available on the market

        Queries PropertyMarket to see if property has gone under contract with
        another buyer, been withdrawn, or become unavailable.

        Args:
            property_id: Property to check
            current_date: Current simulation date

        Returns:
            True if property still Listed or UnderContractLB, False otherwise
        """
        query = """
        SELECT MarketStatus
        FROM simulation.PropertyMarket
        WHERE RunID = ? AND PropertyID = ? AND EffectiveDate <= ?
        ORDER BY EffectiveDate DESC
        OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
        """

        result = self.db.execute_query(query, (self.run_id, property_id, current_date))

        if not result:
            return False

        status = result[0][0]
        # Property is still available for our pipeline if Listed or under contract with us
        return status in ('Listed', 'UnderContractLB')

    def lock_property_under_contract(self, property_id: int, current_date: date) -> None:
        """
        Lock property as under contract with Liberty Bee

        Creates new PropertyMarket row with status 'UnderContractLB' and expires
        the previous 'Listed' row. This prevents other buyers from acquiring
        the property while LB's pipeline is active.

        Args:
            property_id: Property to lock
            current_date: Contract date
        """
        # Expire current Listed status
        expire_query = """
        UPDATE simulation.PropertyMarket
        SET ExpirationDate = ?
        WHERE RunID = ? AND PropertyID = ?
            AND MarketStatus = 'Listed'
            AND ExpirationDate IS NULL
        """
        self.db.execute_non_query(expire_query, (current_date, self.run_id, property_id))

        # Get property details for new row
        query = """
        SELECT CurrentListingPrice, OriginalBasePrice, DaysOnMarket, HoldPeriodDays, LastOwnerType
        FROM simulation.PropertyMarket
        WHERE RunID = ? AND PropertyID = ? AND EffectiveDate <= ?
        ORDER BY EffectiveDate DESC
        """
        result = self.db.execute_query(query, (self.run_id, property_id, current_date))

        if result:
            listing_price, base_price, days_on_market, hold_period, last_owner = result[0]

            # Get next PropertyMarketID for this run (run-specific numbering)
            id_query = """
            SELECT COALESCE(MAX(PropertyMarketID), 0) + 1
            FROM simulation.PropertyMarket
            WHERE RunID = ?
            """
            property_market_id = self.db.execute_scalar(id_query, (self.run_id,))

            # Create new UnderContractLB row
            insert_query = """
            INSERT INTO simulation.PropertyMarket
            (RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
             ExpectedReturnDate, CurrentListingPrice, OriginalBasePrice,
             DaysOnMarket, HoldPeriodDays, LastOwnerType)
            VALUES (?, ?, ?, 'UnderContractLB', ?, NULL, NULL, ?, ?, ?, ?, ?)
            """
            self.db.execute_non_query(insert_query, (
                self.run_id, property_market_id, property_id, current_date,
                listing_price, base_price, days_on_market, hold_period, last_owner
            ))

            if self.event_logger:
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.UPDATE,
                    message=f"Property {property_id} locked under contract with Liberty Bee",
                    entity_type=EntityType.PROPERTY,
                    entity_id=property_id,
                    effective_date=current_date
                )

    def unlock_property_to_market(self, property_id: int, current_date: date) -> None:
        """
        Release property back to market (contract failed/withdrawn)

        Expires the 'UnderContractLB' status and creates new 'Listed' row,
        making the property available for other buyers again.

        Args:
            property_id: Property to unlock
            current_date: Release date
        """
        # Expire current UnderContractLB status
        expire_query = """
        UPDATE simulation.PropertyMarket
        SET ExpirationDate = ?
        WHERE RunID = ? AND PropertyID = ?
            AND MarketStatus = 'UnderContractLB'
            AND ExpirationDate IS NULL
        """
        self.db.execute_non_query(expire_query, (current_date, self.run_id, property_id))

        # Get property details for new row
        query = """
        SELECT CurrentListingPrice, OriginalBasePrice, DaysOnMarket, HoldPeriodDays, LastOwnerType
        FROM simulation.PropertyMarket
        WHERE RunID = ? AND PropertyID = ? AND EffectiveDate <= ?
        ORDER BY EffectiveDate DESC
        """
        result = self.db.execute_query(query, (self.run_id, property_id, current_date))

        if result:
            listing_price, base_price, days_on_market, hold_period, last_owner = result[0]

            # Get next PropertyMarketID for this run (run-specific numbering)
            id_query = """
            SELECT COALESCE(MAX(PropertyMarketID), 0) + 1
            FROM simulation.PropertyMarket
            WHERE RunID = ?
            """
            property_market_id = self.db.execute_scalar(id_query, (self.run_id,))

            # Create new Listed row (back on market)
            insert_query = """
            INSERT INTO simulation.PropertyMarket
            (RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
             ExpectedReturnDate, CurrentListingPrice, OriginalBasePrice,
             DaysOnMarket, HoldPeriodDays, LastOwnerType)
            VALUES (?, ?, ?, 'Listed', ?, NULL, NULL, ?, ?, ?, ?, ?)
            """
            self.db.execute_non_query(insert_query, (
                self.run_id, property_market_id, property_id, current_date,
                listing_price, base_price, days_on_market, hold_period, last_owner
            ))

            if self.event_logger:
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.UPDATE,
                    message=f"Property {property_id} released back to market (contract failed)",
                    entity_type=EntityType.PROPERTY,
                    entity_id=property_id,
                    effective_date=current_date
                )

    def mark_property_acquired(self, property_id: int, final_price: Decimal, current_date: date) -> None:
        """
        Mark property as acquired by Liberty Bee

        Expires the 'UnderContractLB' status and creates new 'LBOwned' row,
        indicating successful acquisition.

        Args:
            property_id: Property acquired
            final_price: Final purchase price
            current_date: Closing date
        """
        # Expire current UnderContractLB status
        expire_query = """
        UPDATE simulation.PropertyMarket
        SET ExpirationDate = ?
        WHERE RunID = ? AND PropertyID = ?
            AND MarketStatus = 'UnderContractLB'
            AND ExpirationDate IS NULL
        """
        self.db.execute_non_query(expire_query, (current_date, self.run_id, property_id))

        # Get property details for new row
        query = """
        SELECT OriginalBasePrice, DaysOnMarket, HoldPeriodDays
        FROM simulation.PropertyMarket
        WHERE RunID = ? AND PropertyID = ? AND EffectiveDate <= ?
        ORDER BY EffectiveDate DESC
        """
        result = self.db.execute_query(query, (self.run_id, property_id, current_date))

        if result:
            base_price, days_on_market, hold_period = result[0]

            # Get next PropertyMarketID for this run (run-specific numbering)
            id_query = """
            SELECT COALESCE(MAX(PropertyMarketID), 0) + 1
            FROM simulation.PropertyMarket
            WHERE RunID = ?
            """
            property_market_id = self.db.execute_scalar(id_query, (self.run_id,))

            # Create new LBOwned row
            insert_query = """
            INSERT INTO simulation.PropertyMarket
            (RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
             ExpectedReturnDate, CurrentListingPrice, OriginalBasePrice,
             DaysOnMarket, HoldPeriodDays, LastOwnerType)
            VALUES (?, ?, ?, 'LBOwned', ?, NULL, NULL, ?, ?, ?, ?, 'LB')
            """
            self.db.execute_non_query(insert_query, (
                self.run_id, property_market_id, property_id, current_date,
                final_price, base_price, days_on_market, hold_period
            ))

            if self.event_logger:
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.COMPLETE,
                    message=f"Property {property_id} acquired by Liberty Bee for ${final_price:,}",
                    entity_type=EntityType.PROPERTY,
                    entity_id=property_id,
                    effective_date=current_date
                )

    def add_acquired_property_to_portfolio(self, property_id: int, acquisition_date: date,
                                           final_price: Decimal, event_id: Optional[int] = None) -> None:
        """
        Add acquired property to simulation portfolio

        Inserts property and its units into simulation.Properties and simulation.PropertyUnits
        tables, copying data from reference tables and setting acquisition details.

        Args:
            property_id: Property acquired
            acquisition_date: Date property was acquired
            final_price: Final purchase price
            event_id: Optional event ID to link
        """
        # Get property details from reference
        property_query = """
        SELECT PropertyAddress, PropertyStyle, TotalUnits, BasePrice
        FROM reference.Properties
        WHERE PropertyID = ?
        """
        prop_result = self.db.execute_query(property_query, (property_id,))

        if not prop_result:
            self.logger.error(f"Property {property_id} not found in reference.Properties")
            return

        address, property_type, total_units, base_price = prop_result[0]

        # Get next PropertyID for this run (run-specific numbering)
        property_id_query = """
            SELECT COALESCE(MAX(PropertyID), 0) + 1
            FROM simulation.Properties
            WHERE RunID = ?
        """
        property_id_result = self.db.execute_query(property_id_query, (self.run_id,))
        sim_property_id = property_id_result[0][0] if property_id_result else 1

        # Insert into simulation.Properties with explicit PropertyID
        # AddressID stores the reference.Properties.PropertyID for tracking
        insert_property_query = """
        INSERT INTO simulation.Properties
        (RunID, PropertyID, AddressID, ADDRESS, PROPERTYTYPE, AcquisitionDate,
         BasePrice, InflationAdjustedPrice, TotalUnits, EventID)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """

        rows_affected = self.db.execute_non_query(insert_property_query, (
            self.run_id,
            sim_property_id,  # Explicitly assigned PropertyID
            property_id,  # AddressID = reference PropertyID for tracking
            address,
            property_type,
            acquisition_date,
            base_price,
            final_price,  # InflationAdjustedPrice = what we paid
            total_units,
            event_id
        ))

        if rows_affected == 0:
            self.logger.error(f"Failed to insert property {property_id} into simulation.Properties")
            return

        # Get unit details from reference
        units_query = """
        SELECT UnitID, UnitNumber, Beds, Baths, BaseRent, AdjustedRent
        FROM reference.Units
        WHERE PropertyID = ?
        ORDER BY UnitID
        """
        units_result = self.db.execute_query(units_query, (property_id,))

        if units_result:
            # Insert units into simulation.PropertyUnits with explicit UnitID
            # PropertyID links to the simulation.Properties record we just created
            for idx, unit in enumerate(units_result, start=1):
                # Query returns: UnitID, UnitNumber, Beds, Baths, BaseRent, AdjustedRent
                # KD-012 (one rent basis): AdjustedRent — the bath-adjusted market figure
                # underwriting scored on — is carried into the run; charging and the
                # retention market anchor price off it. BaseRent stays as provenance
                # (the bedroom-median anchor).
                unit_id, unit_number, beds, baths, base_rent, adjusted_rent = unit[:6]

                # CurrentRent retired at V00058: it was a
                # set-once 0.9xBaseRent snapshot with zero independent signal; the honest
                # rent is computed at qualification/signing (AdjustedRent x discount x inflation).

                # Get next UnitID for this run (run-specific numbering)
                unit_id_query = """
                    SELECT COALESCE(MAX(UnitID), 0) + 1
                    FROM simulation.PropertyUnits
                    WHERE RunID = ?
                """
                unit_id_result = self.db.execute_query(unit_id_query, (self.run_id,))
                sim_unit_id = unit_id_result[0][0] if unit_id_result else 1

                insert_unit_query = """
                INSERT INTO simulation.PropertyUnits
                (RunID, UnitID, PropertyID, EventID, Unit, Beds, Baths, BaseRent, AdjustedRent, IsOccupied, UnitStatus)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'Compliance_In_Progress');
                """
                self.db.execute_non_query(insert_unit_query, (
                    self.run_id,
                    sim_unit_id,  # Explicitly assigned UnitID
                    sim_property_id,  # Use PropertyID from simulation.Properties
                    event_id,
                    str(unit_number),
                    beds,
                    baths,
                    base_rent,
                    adjusted_rent,
                ))

            # portfolio changed — the ONLY PropertyUnits insert site (step 0.5)
            self.portfolio_counts.invalidate()
            self.logger.info(f"Added property {property_id} with {len(units_result)} units to portfolio")
        else:
            self.logger.warning(f"No units found for property {property_id}")

        if self.event_logger:
            self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.CREATE,
                message=f"Added property {property_id} ({total_units} units) to portfolio at ${final_price:,}",
                entity_type=EntityType.PROPERTY,
                entity_id=property_id,
                effective_date=acquisition_date
            )

        # Trigger staffing scaling check after acquisition close.
        # The acquired property + its units are now visible to `check_staffing_needs`'s
        # property/unit count queries. Function is idempotent — checks current vs needed,
        # hires only the delta. "Staffing should scale because
        # the portfolio grew, not because a calendar tick occurred."
        if self.employee_manager and self.config:
            self.employee_manager.check_staffing_needs(
                self.run_id,
                acquisition_date,
                self.config.maint_crossover_properties,
                self.config.units_per_admin_early,
                self.config.units_per_admin_late,
                self.config.early_admin_years,
                self.config.start_date,
            )

    def update_attempt_state(self, attempt_id: int, pipeline_stage: str,
                            next_action_date: Optional[date],
                            stage_timeout_date: Optional[date],
                            current_date: date,
                            is_active: bool = True,
                            **additional_fields) -> None:
        """
        Update attempt state fields

        Args:
            attempt_id: Attempt to update
            pipeline_stage: New pipeline stage
            next_action_date: When next action should occur
            stage_timeout_date: When stage times out
            current_date: Current simulation date (for StageEnteredDate and LastUpdatedDate)
            is_active: Active flag
            **additional_fields: Any other fields to update (SellerResponse, etc.)
        """
        # Build dynamic UPDATE statement
        set_clause = """
            PipelineStage = ?,
            NextActionDate = ?,
            StageTimeoutDate = ?,
            IsActive = ?,
            StageEnteredDate = ?,
            LastUpdatedDate = ?
        """
        params = [pipeline_stage, next_action_date, stage_timeout_date, is_active, current_date, current_date]

        # Add any additional fields
        for field, value in additional_fields.items():
            set_clause += f", {field} = ?"
            params.append(value)

        query = f"""
        UPDATE simulation.PropertyAcquisitionAttempt
        SET {set_clause}
        WHERE RunID = ? AND AttemptID = ?
        """

        params.extend([self.run_id, attempt_id])
        self.db.execute_non_query(query, tuple(params))

    def _outstanding_reservation(self, offer_amount, counter_amount, lb_response_to_counter) -> Decimal:
        """The outstanding CashHold reservation is a
        pure function of the attempt record — the counter amount once LB accepted
        the counter (reserve_funds_for_offer + reserve_additional_funds_for_counter),
        else the original offer. The FundLedger reservation/release event stream
        remains the independent audit trail the acceptance tests reconcile against."""
        if offer_amount is None:
            return Decimal('0')
        offer = Decimal(str(offer_amount))
        if lb_response_to_counter == 'AcceptCounter' and counter_amount is not None:
            return Decimal(str(counter_amount))
        return offer

    def fail_attempt(self, attempt_id: int, failure_reason: str,
                    pipeline_stage: str, current_date: date) -> None:
        """
        Mark attempt as failed and release reserved funds

        Args:
            attempt_id: Attempt to fail
            failure_reason: Why it failed (Timeout, PropertyUnavailable, etc.)
            pipeline_stage: Final stage (Timeout, Failed, PropertyUnavailable, Withdrawn)
            current_date: Current simulation date (for event logging)
        """
        # Get attempt details (need the FULL outstanding reservation for fund release
        # — releasing only OfferAmount stranded the counter-delta whenever
        # a counter-accepted attempt later failed)
        query = """
        SELECT PropertyID, OfferAmount, CounterAmount, LBResponseToCounter
        FROM simulation.PropertyAcquisitionAttempt
        WHERE RunID = ? AND AttemptID = ?
        """
        result = self.db.execute_query(query, (self.run_id, attempt_id))
        if result:
            property_id = result[0][0]
            reserved_total = self._outstanding_reservation(result[0][1], result[0][2], result[0][3])

            # Release reserved funds (CashHold → Cash)
            if reserved_total > 0:
                self.fund_manager.release_funds_from_offer(
                    run_id=self.run_id,
                    offer_amount=reserved_total,
                    ledger_date=current_date,
                    property_id=property_id,
                    reason=failure_reason
                )

            # Check if property was under contract and unlock if so
            check_query = """
            SELECT MarketStatus
            FROM simulation.PropertyMarket
            WHERE RunID = ? AND PropertyID = ? AND EffectiveDate <= ?
            ORDER BY EffectiveDate DESC
            OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """
            status_result = self.db.execute_query(check_query, (self.run_id, property_id, current_date))
            if status_result and status_result[0][0] == 'UnderContractLB':
                self.unlock_property_to_market(property_id, current_date)

        self.update_attempt_state(
            attempt_id=attempt_id,
            pipeline_stage=pipeline_stage,
            next_action_date=None,
            stage_timeout_date=None,
            current_date=current_date,
            is_active=False,
            FailureReason=failure_reason
        )

        if self.event_logger:
            self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.FAIL,
                message=f"Acquisition failed: {failure_reason}",
                entity_type=EntityType.PROPERTY,
                entity_id=attempt_id,
                effective_date=current_date
            )
        
        self._record_acquisition_step(
                attempt_id=attempt_id,
                step_type="ACQUISITION_WITHDRAWN",
                message=f"Acquisition failed: {failure_reason}",
                effective_date=current_date,
                action=ActionType.UPDATE,
                entity_type=EntityType.PROPERTY,
                entity_id=property_id,
            )

    def _reserve_earmark(self, month_index: int, current_date: date,
                         monthly_opex: Decimal) -> Decimal:
        """Reserve-first: the pending month-end CSF top-up to earmark out
        of deployable Cash *before* growth.

        Zero during the startup grace (acquire before the reserve is funded —
        ``no homes => no tenants to protect``). After grace it is the
        inflation-step shortfall = current CSF target - at-rest CSF balance
        (>= 0); the month-end top-up will move exactly this much Cash -> CSF.
        Uses ``get_csf_target`` (the cents-quantized single source of truth) so
        the earmark and the eventual top-up agree to the cent.
        """
        if month_index <= self.csf_grace_period_months:
            return Decimal('0')
        # Months come from the taper curve on the property count — same
        # source (the PortfolioCounts cache over simulation.PropertyUnits) as
        # the OpEx breakdown, so the earmark and the month-end top-up see
        # identical curve inputs.
        _, owned_properties, _ = self.portfolio_counts.get(self.run_id)
        reserve_months = self.fund_manager.get_reserve_months(
            owned_properties,
            self.config.csf_reserve_months_peak,
            self.config.csf_reserve_months_floor,
            self.config.csf_reserve_n0_properties,
        )
        current_target = self.fund_manager.get_csf_target(monthly_opex, reserve_months)
        balances = self.fund_manager.get_fund_balances(self.run_id, current_date)
        return max(Decimal('0'), current_target - balances.csf_balance)

    def _required_cash_floor(self, bases: AcquisitionGateBases) -> Decimal:
        """KD-042 (ratified): required floor = pro-forma RECURRING carrying ×
        CashFloorMonths, plus the one-time onboarding lump added ONCE.
        The single floor definition shared by the gate and the budget
        (KD #99 same-floor guardrail — two floors drift)."""
        return (bases.pro_forma_opex * Decimal(self.config.cash_floor_months)
                + bases.onboarding_lump).quantize(Decimal('0.01'))

    @staticmethod
    def _floor_detail(bases: AcquisitionGateBases, cash_floor: Decimal) -> str:
        """V-F: the floor decomposition every pass/refusal string carries —
        auditable to the dollar."""
        return (
            f"floor ${cash_floor:,.2f} = (owned ${bases.owned_opex:,.2f} "
            f"+ pipeline-marginal ${bases.pipeline_marginal:,.2f} "
            f"[{bases.pipeline_count} attempt(s), {bases.pipeline_properties} prop / "
            f"{bases.pipeline_units} unit]) × months + lump ${bases.onboarding_lump:,.2f}"
        )

    def can_acquire_property(self, month_index: int, current_date: date,
                            bases: AcquisitionGateBases,
                            committed_target: Decimal = Decimal('0')) -> tuple[bool, str]:
        """
        KD #99 fix — reserve-first + genuine-draw latch;
        KD-042 fix — the floor is FORWARD-PRICED.

        The gate takes TWO OpEx bases (AcquisitionGateBases), not one number:
        Rules 2/3 gate against the pro-forma basis (owned + expected carrying
        of everything in-pipeline) plus the one-time onboarding lump, while
        the CSF earmark keeps the OWNED basis (ratified design — the reserve target is
        never forward-priced). Pre-KD-042 the floor was current-OpEx-only:
        at month 1 that is payroll-only, so six week-one pipelines could
        legally commit ~94% of capital (p201 s128/s131, p202 s211 — the
        living farm's first divergence findings).

          Rule 1 (6-month grace applies HERE ONLY):
            (a) LATCH — if CSF is below the *committed* target (what the last
                month-end top-up funded to), the reserve was genuinely drawn for
                survival (or the top-up was cash-starved); block growth until it
                is restored ("no murals while the spleen is on the floor").
            (b) otherwise the reserve is whole vs its commitment; only the
                inflation-step shortfall remains, and it is *earmarked* out of
                deployable Cash in Rule 3 / the budget (reserve-first — fund the
                pending top-up first, grow on the remainder). This is NOT a
                Cash+CSF blend: the shortfall is subtracted from the growth side.
          Rule 2: Cash >= required floor (no grace; floor = pro-forma × months + lump).
          Rule 3: deployable Cash (Cash - floor - reserve earmark) > 0.

        The grace period waives Rule 1 ONLY ("no homes ⇒ no tenants to
        protect") — it was never a waiver of solvency discipline; the
        forward-priced floor restores Rules 2/3's teeth during grace without
        touching the waiver.

        Args:
            bases: the two-basis snapshot from the gate-basis provider
                (Simulation.compute_acquisition_gate_bases).
            committed_target: CSF target the last month-end top-up funded to
                (passed from simulation.py). Defaults to 0 (latch inert) for
                callers without it.

        Returns:
            (allowed, reason) — Boolean and explanation string (V-F: both
            decompose the floor to the dollar)
        """
        balances = self.fund_manager.get_fund_balances(self.run_id, current_date)
        cash_floor = self._required_cash_floor(bases)
        earmark = self._reserve_earmark(month_index, current_date, bases.owned_opex)

        # Rule 1: reserve-first + genuine-draw latch (6-month grace waives it)
        if month_index <= self.csf_grace_period_months:
            csf_reason = f"CSF grace period (month {month_index}/{self.csf_grace_period_months})"
        else:
            # (a) LATCH on a genuine draw: CSF below the last committed top-up target
            if balances.csf_balance < committed_target:
                reason = (
                    f"Blocked (rule 1, genuine CSF draw): "
                    f"CSF ${balances.csf_balance:,.2f} < committed target ${committed_target:,.2f} "
                    f"— restore reserve before growth"
                )
                return False, reason
            # (b) inflation-lag only: reserve is whole vs commitment; shortfall earmarked below
            csf_reason = (
                f"CSF ${balances.csf_balance:,.2f} >= committed ${committed_target:,.2f}; "
                f"inflation-step earmark ${earmark:,.2f}"
            )

        # Rule 2: Cash floor discipline (NO grace — applies from month 1)
        if balances.cash_balance < cash_floor:
            reason = (
                f"Blocked (rule 2, pro-forma Cash floor, KD-042): "
                f"Cash ${balances.cash_balance:,.2f} < {self._floor_detail(bases, cash_floor)}"
            )
            return False, reason

        # Rule 3: deployable Cash AFTER earmarking the pending reserve top-up must be > 0
        deployable = balances.cash_balance - cash_floor - earmark
        if deployable <= 0:
            reason = (
                f"Blocked (rule 3, deployable after reserve earmark, KD-042): "
                f"Cash ${balances.cash_balance:,.2f} - floor - earmark ${earmark:,.2f} "
                f"= ${deployable:,.2f}; {self._floor_detail(bases, cash_floor)}"
            )
            return False, reason

        return True, (
            f"All gates pass: {csf_reason}; "
            f"deployable ${deployable:,.2f} (Cash ${balances.cash_balance:,.2f} "
            f"- floor - earmark ${earmark:,.2f}; {self._floor_detail(bases, cash_floor)})"
        )

    def get_acquisition_budget(self, current_date: date, config: ProjectionConfig,
                               bases: AcquisitionGateBases,
                               reserve_earmark: Decimal = Decimal('0')) -> Decimal:
        """
        KD #99 + KD-042: available acquisition budget =
        deployable Cash (above the REQUIRED floor and the pending reserve
        earmark) × acquisition_pct.

        KD #99 guardrails carried forward; KD-042: the floor is the
        shared ``_required_cash_floor`` — pro-forma recurring × months + the
        one-time lump — the exact value the gate checks (same-floor
        guardrail: the budget and the gate can never disagree). The budget
        shrinks in lockstep as pipelines stack, which is the second brake:
        a candidate the floor would refuse usually never gets offered on,
        because ``find_best_property`` can't find anything inside the
        shrunken budget.

        Args:
            current_date: Current simulation date for fund balance lookup
            config: Projection configuration (PROP_AcquisitionPct + FIN.CashFloorMonths)
            bases: the two-basis snapshot (same one the gate evaluated)
            reserve_earmark: Pending CSF top-up to reserve before growth (>= 0)

        Returns:
            Maximum amount available for property purchase. Returns 0 when Cash
            is at or below floor + earmark (gate enforces reserve before growth).
        """
        balances = self.fund_manager.get_fund_balances(self.run_id, current_date)
        cash_balance = balances.cash_balance

        # The gate's exact floor (KD #99 same-floor guardrail, now shared code)
        cash_floor = self._required_cash_floor(bases)

        # Cash balance already excludes funds in CashHold (reserved for active offers).
        # Deployable = Cash above floor AND above the earmarked pending top-up.
        deployable = max(Decimal('0'), cash_balance - cash_floor - reserve_earmark)
        budget = (deployable * Decimal(str(config.acquisition_pct))).quantize(Decimal('0.01'))

        # Don't return negative budget
        budget = max(budget, Decimal('0'))

        if self.event_logger:
            self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.VALIDATE,
                message=(
                    f"Acquisition budget calculated: ${budget:,.2f} "
                    f"({config.acquisition_pct:.1%} of ${deployable:,.2f} deployable cash "
                    f"= ${cash_balance:,.2f} cash - required floor "
                    f"- ${reserve_earmark:,.2f} reserve earmark; "
                    f"{self._floor_detail(bases, cash_floor)})"
                ),
                entity_type=EntityType.FUND,
                effective_date=current_date
            )

        return budget

    def calculate_income_to_price_ratio(self, property_data: Dict[str, Any],
                                        config: ProjectionConfig) -> float:
        """
        Calculate income-to-price ratio for a property

        Formula: (AdjustedRent × 12 × (1-vacancy) × (1-below_market)) / price

        Args:
            property_data: Dict with property_id, list_price, total_adjusted_rent
            config: Projection configuration with vacancy and below-market rates

        Returns:
            Income-to-price ratio (higher is better)
        """
        annual_rent = property_data['total_adjusted_rent'] * 12
        # KD-229 family (V00077): underwrite at the CANDIDATE's town's vacancy,
        # not the region-wide base — a high-vacancy town's property must not be
        # priced at the region's average emptiness.
        vacancy_base = config.vacancy_rate_base_by_town.get(
            property_data.get('town'), config.vacancy_rate_base)
        vacancy_factor = Decimal('1.0') - Decimal(str(vacancy_base))
        below_market_factor = Decimal('1.0') - Decimal(str(config.below_market_rent_pct))

        effective_annual_income = annual_rent * vacancy_factor * below_market_factor
        ratio = float(effective_annual_income / property_data['list_price'])

        return ratio

    def find_best_property(self, current_date: date, budget: Decimal,
                          config: ProjectionConfig) -> Optional[PropertyCandidate]:
        """
        Find the best property to acquire based on income-to-price ratio

        Args:
            current_date: Current simulation date
            budget: Maximum amount available for purchase
            config: Projection configuration

        Returns:
            PropertyCandidate with best ratio, or None if no suitable properties
        """
        # Query available properties from market
        # Exclude properties with active acquisition attempts
        query = """
        SELECT
            pm.PropertyID,
            rp.PropertyAddress,
            CAST(pm.CurrentListingPrice AS DECIMAL(12,2)) as ListPrice,
            rp.PropertyStyle,
            (SELECT COUNT(*) FROM reference.Units u WHERE u.PropertyID = rp.PropertyID) as UnitCount,
            (SELECT SUM(CAST(AdjustedRent AS DECIMAL(10,2)))
             FROM reference.Units u
             WHERE u.PropertyID = rp.PropertyID) as TotalAdjustedRent,
            rp.Town
        FROM simulation.PropertyMarket pm
        JOIN reference.Properties rp ON pm.PropertyID = rp.PropertyID
        LEFT JOIN simulation.PropertyAcquisitionAttempt paa
            ON pm.PropertyID = paa.PropertyID
            AND paa.RunID = pm.RunID
            AND paa.IsActive = 1
        WHERE pm.RunID = ?
        AND pm.MarketStatus = 'Listed'
        AND pm.ExpirationDate IS NULL  -- Current market state only
        AND pm.CurrentListingPrice <= ?
        AND paa.AttemptID IS NULL  -- Exclude properties with active attempts
        ORDER BY pm.PropertyID
        """

        try:
            results = self.db.execute_query(
                query,
                (self.run_id, budget)
            )

            if not results:
                if self.event_logger:
                    self.event_logger.log_module_event(
                        module_name="PropertyAcquisitionManager",
                        action=ActionType.VALIDATE,
                        message=f"No properties found: Budget=${budget:,.2f}",
                        entity_type=EntityType.PROPERTY,
                        effective_date=current_date
                    )
                return None

            # Calculate income-to-price ratio for each property
            candidates = []
            for row in results:
                property_data = {
                    'property_id': row[0],
                    'address': row[1],
                    'list_price': row[2],
                    'property_type': row[3],
                    'unit_count': row[4],
                    'total_adjusted_rent': row[5],
                    'town': row[6],  # V00077: underwrite at the candidate's town's vacancy
                }

                ratio = self.calculate_income_to_price_ratio(property_data, config)

                vacancy_base = config.vacancy_rate_base_by_town.get(
                    property_data['town'], config.vacancy_rate_base)
                vacancy_factor = Decimal('1.0') - Decimal(str(vacancy_base))
                below_market_factor = Decimal('1.0') - Decimal(str(config.below_market_rent_pct))
                vacancy_adjusted_rent = property_data['total_adjusted_rent'] * vacancy_factor * below_market_factor

                candidate = PropertyCandidate(
                    property_id=property_data['property_id'],
                    address=property_data['address'],
                    list_price=property_data['list_price'],
                    unit_count=property_data['unit_count'],
                    total_annual_rent=property_data['total_adjusted_rent'] * 12,
                    income_to_price_ratio=ratio,
                    vacancy_adjusted_rent=vacancy_adjusted_rent
                )
                candidates.append(candidate)

            # Select property with best (highest) income-to-price ratio
            best_property = max(candidates, key=lambda p: p.income_to_price_ratio)

            if self.event_logger:
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.VALIDATE,
                    message=f"Best property found: ID={best_property.property_id}, Ratio={best_property.income_to_price_ratio:.4f}, Price=${best_property.list_price:,.2f} ({len(candidates)} evaluated)",
                    entity_type=EntityType.PROPERTY,
                    entity_id=best_property.property_id,
                    effective_date=current_date
                )

            return best_property

        except Exception as e:
            self.logger.error(f"Failed to find best property: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"Failed to find best property",
                    exception=e,
                    context="PropertyAcquisitionManager.find_best_property"
                )
            return None

    # ============================================================================
    # PRE-ACQUISITION PIPELINE METHODS
    # ============================================================================

    def start_acquisition_attempt(self, property_id: int, listing_price: Decimal,
                                  offer_date: date) -> int:
        """
        Create new acquisition attempt record with state tracking

        Initialize with:
        - PipelineStage = 'MakingOffer'
        - IsActive = 1
        - StageEnteredDate = offer_date
        - NextActionDate = offer_date (process immediately)
        - StageTimeoutDate = NULL (no timeout for initial offer generation)
        """
        # Get next AttemptID (manual assignment, resets to 1 per RunID)
        attempt_id = self._get_next_attempt_id()

        insert_query = """
        INSERT INTO simulation.PropertyAcquisitionAttempt (
            RunID, AttemptID, PropertyID, OfferDate, ListingPrice,
            OfferAmount, OfferStrategy,
            PipelineStage, IsActive, StageEnteredDate, NextActionDate,
            StageTimeoutDate, AcquisitionSucceeded, CreatedAt, LastUpdatedDate
        )
        VALUES (?, ?, ?, ?, ?, NULL, NULL, 'MakingOffer', 1, ?, ?, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """

        self.db.execute_non_query(insert_query, (
            self.run_id, attempt_id, property_id, offer_date, listing_price,
            offer_date, offer_date
        ))

        return attempt_id

    def _get_next_attempt_id(self) -> int:
        """Get next AttemptID for this run (manual assignment, resets to 1 per run)"""
        query = """
        SELECT COALESCE(MAX(AttemptID), 0) + 1
        FROM simulation.PropertyAcquisitionAttempt
        WHERE RunID = ?
        """
        result = self.db.execute_query(query, (self.run_id,))
        return result[0][0] if result else 1

    def _record_acquisition_step(self, attempt_id: int, step_type: str, message: str,
                                 effective_date: date, amount: Optional[Decimal] = None,
                                 *, action: "ActionType" = None,
                                entity_type: "EntityType" = None,
                                entity_id: Optional[int] = None,
                                property_id: Optional[int] = None,) -> None:
        """
        Log acquisition event and record step in PropertyAcquisitionStep ledger

        This method:
        1. Logs event via event_logger (captures EventID)
        2. Inserts step record into PropertyAcquisitionStep ledger table
        3. Manually assigns StepID per attempt (sequential: 1, 2, 3, ...)

        Args:
            attempt_id: The acquisition attempt ID
            step_type: Step type (e.g., 'OFFER_MADE', 'INSPECTION_COMPLETED')
            message: Human-readable description
            effective_date: When the step occurred
            amount: Transaction amount (optional)
        """

        # Log event and capture EventID
        event_id = self.event_logger.log_acquisition_event(
            action=action,
            message=message,
            effective_date=effective_date,
            amount=amount
        )

        if event_id is None:
            self.logger.error(f"Failed to log event for step {step_type}")
            print(f"ERROR: event_id is None for step_type={step_type}")
            return

        # Get next StepID for this attempt (manual assignment, resets to 1 per attempt)
        step_id_query = """
        SELECT COALESCE(MAX(StepID), 0) + 1
        FROM simulation.PropertyAcquisitionStep
        WHERE RunID = ? AND AttemptID = ?
        """
        result = self.db.execute_query(step_id_query, (self.run_id, attempt_id))
        step_id = result[0][0] if result else 1

        # Insert into PropertyAcquisitionStep ledger with explicit StepID
        insert_query = """
        INSERT INTO simulation.PropertyAcquisitionStep (
            RunID, AttemptID, StepID, EffectiveDate, EventID, StepType, Amount, Notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        self.db.execute_non_query(insert_query, (
            self.run_id, attempt_id, step_id, effective_date, event_id, step_type, amount, message
        ))



    # ========================================
    # DAILY PIPELINE PROCESSOR - State-Based Processing
    # ========================================

    def process_daily_pipelines(self, current_date: date, month_index: int) -> None:
        """
        Main daily processor - advance all active acquisition pipelines

        Called every simulation day. For each active attempt:
        1. Check for stage timeout
        2. Check property still available
        3. If NextActionDate reached, advance to next stage

        Args:
            current_date: Current simulation date
            month_index: Current simulation month (for logging)
        """
        if not self.acquisition_params:
            self.load_acquisition_parameters()

        # Get all active attempts needing processing
        active_attempts = self.get_active_attempts(current_date)

        if not active_attempts:
            return  # No active pipelines

        if self.event_logger:
            self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.UPDATE,
                message=f"Processing {len(active_attempts)} active acquisition pipelines",
                entity_type=EntityType.PROPERTY,
                effective_date=current_date
            )

        for attempt in active_attempts:
            # Check for timeout
            if attempt['stage_timeout_date'] and current_date >= attempt['stage_timeout_date']:
                self._handle_stage_timeout(attempt, current_date)
                continue

            # Check property still available
            if not self.check_property_still_available(attempt['property_id'], current_date):
                self.fail_attempt(
                    attempt_id=attempt['attempt_id'],
                    failure_reason='Property no longer available (went under contract with other buyer)',
                    pipeline_stage='PropertyUnavailable',
                    current_date=current_date
                )
                continue

            # Process stage if action date reached
            if attempt['next_action_date'] and current_date >= attempt['next_action_date']:
                self._advance_pipeline_stage(attempt, current_date)

    def _handle_stage_timeout(self, attempt: Dict[str, Any], current_date: date) -> None:
        """
        Handle stage timeout - fail attempt with timeout reason

        Args:
            attempt: Attempt record
            current_date: Current simulation date
        """
        stage = attempt['pipeline_stage']
        timeout_reasons = {
            'AwaitingSellerResponse': 'Seller did not respond within timeout period',
            'SchedulingInspection': 'Unable to schedule inspection within timeout period',
            'Negotiating': 'Negotiation exceeded maximum duration',
            'AwaitingClosing': 'Closing process exceeded maximum duration'
        }

        reason = timeout_reasons.get(stage, f'Stage {stage} exceeded timeout')

        self.fail_attempt(
            attempt_id=attempt['attempt_id'],
            failure_reason=reason,
            pipeline_stage='Timeout',
            current_date=current_date
        )

    def _advance_pipeline_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """
        Advance pipeline to next stage based on current stage

        Args:
            attempt: Attempt record with all fields
            current_date: Current simulation date
        """
        stage = attempt['pipeline_stage']

        # Route to appropriate stage processor
        if stage == 'MakingOffer':
            self._process_offer_stage(attempt, current_date)
        elif stage == 'AwaitingSellerResponse':
            self._process_seller_response_stage(attempt, current_date)
        elif stage == 'AwaitingCounterResponse':
            self._process_counter_response_stage(attempt, current_date)
        elif stage == 'SchedulingInspection':
            self._process_inspection_scheduling_stage(attempt, current_date)
        elif stage == 'AwaitingInspection':
            self._process_inspection_stage(attempt, current_date)
        elif stage == 'Negotiating':
            self._process_negotiation_stage(attempt, current_date)
        elif stage == 'AwaitingClosing':
            self._process_closing_stage(attempt, current_date)
        else:
            # Unknown stage - should not happen
            if self.event_logger:
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.ERROR,
                    message=f"Unknown pipeline stage: {stage}",
                    entity_type=EntityType.PROPERTY,
                    entity_id=attempt['attempt_id'],
                    effective_date=current_date
                )

    # ========================================
    # STAGE PROCESSORS - State-Based Execution
    # ========================================

    def _process_offer_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """
        Process offer generation stage

        Generates offer based on strategy, records it, advances to AwaitingSellerResponse.

        Args:
            attempt: Attempt record
            current_date: Current simulation date (when offer is made)
        """
        # Generate offer (use existing logic)
        listing_price = attempt['listing_price']
        offer_result = self._generate_offer_amount(listing_price, attempt['attempt_id'])

        # Use seeded RNG for delays (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 1000 + attempt['attempt_id'])

        # Calculate seller response date
        response_delay = rng.randint(
            self.acquisition_params.seller_response_delay_days_min,
            self.acquisition_params.seller_response_delay_days_max
        )
        next_action = current_date + timedelta(days=response_delay)

        # Calculate timeout date
        timeout_date = current_date + timedelta(
            days=self.acquisition_params.timeout_offer_response_max_days
        )

        # Update attempt state
        self.update_attempt_state(
            attempt_id=attempt['attempt_id'],
            pipeline_stage='AwaitingSellerResponse',
            next_action_date=next_action,
            stage_timeout_date=timeout_date,
            current_date=current_date,
            OfferAmount=offer_result['offer_amount'],
            OfferStrategy=offer_result['strategy']
        )

        # Reserve funds in FundLedger (Cash → CashHold)
        self.fund_manager.reserve_funds_for_offer(
            run_id=self.run_id,
            offer_amount=offer_result['offer_amount'],
            ledger_date=current_date,
            property_id=attempt['property_id']
        )

        # Log event
        self._record_acquisition_step(
            attempt_id=attempt['attempt_id'],
            step_type="OFFER_MADE",
            message=f"Property {attempt['property_id']}: Offer ${offer_result['offer_amount']:,.2f} on property (list ${attempt['listing_price']}, strategy: {offer_result['strategy']})",
            effective_date=current_date,
            amount=offer_result['offer_amount'],
            action=ActionType.CREATE,
            entity_type=EntityType.PROPERTY,
            entity_id=attempt['property_id'],
        )

    def _generate_offer_amount(self, listing_price: Decimal, attempt_id: int) -> Dict[str, Any]:
        """Helper: Generate offer amount based on strategy (extracted from existing code)"""
        # Use seeded RNG (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 1000 + attempt_id)

        # Choose strategy
        roll = rng.random()
        if roll < self.acquisition_params.offer_strategy_below_ask_weight:
            strategy = 'BelowAsk'
            adjustment = rng.uniform(
                self.acquisition_params.offer_strategy_below_ask_pct * -1,
                0
            )
        elif roll < (self.acquisition_params.offer_strategy_below_ask_weight +
                     self.acquisition_params.offer_strategy_at_ask_weight):
            strategy = 'AtAsk'
            adjustment = 0
        else:
            strategy = 'Premium'
            adjustment = rng.uniform(
                0,
                self.acquisition_params.offer_strategy_premium_pct
            )

        offer_amount = listing_price * Decimal(str(1 + adjustment))

        return {
            'strategy': strategy,
            'offer_amount': offer_amount
        }

    def _process_seller_response_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """Process seller response to offer"""
        # Use seeded RNG (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 1001 + attempt['attempt_id'])

        roll = rng.random()
        if roll < self.acquisition_params.seller_acceptance_probability:
            response = 'Accepted'
            next_stage = 'SchedulingInspection'
        elif roll < (self.acquisition_params.seller_acceptance_probability +
                     self.acquisition_params.seller_counter_probability):
            response = 'Countered'
            next_stage = 'AwaitingCounterResponse'
        else:
            response = 'Rejected'
            # Fail the attempt
            self.fail_attempt(
                attempt_id=attempt['attempt_id'],
                failure_reason='Seller rejected offer',
                pipeline_stage='Failed',
                current_date=current_date
            )
            return

        # Calculate next action date
        if response == 'Accepted':
            inspection_delay = rng.randint(
                self.acquisition_params.inspection_scheduling_delay_min,
                self.acquisition_params.inspection_scheduling_delay_max
            )
            next_action = current_date + timedelta(days=inspection_delay)
            timeout_date = current_date + timedelta(
                days=self.acquisition_params.timeout_inspection_scheduling_max_days
            )

            # Accepted path - lock property under contract with LB
            self.update_attempt_state(
                attempt_id=attempt['attempt_id'],
                pipeline_stage=next_stage,
                next_action_date=next_action,
                stage_timeout_date=timeout_date,
                current_date=current_date,
                SellerResponse=response
            )

            self._record_acquisition_step(
                    attempt_id=attempt['attempt_id'],
                    step_type="OFFER_ACCEPTED",
                    message=f"Property {attempt['property_id']}: Offer ${attempt['offer_amount']:,.2f} accepted by seller",
                    effective_date=current_date,
                    amount=attempt['offer_amount'],
                    action=ActionType.UPDATE,
                    entity_type=EntityType.PROPERTY,
                    entity_id=attempt['property_id'],
                )
            self.lock_property_under_contract(attempt['property_id'], current_date)
            return
        else:  # Countered
            counter_amount = attempt['offer_amount'] * Decimal(str(1 + rng.uniform(
                self.acquisition_params.seller_counter_increase_pct * self.counter_jitter_low_factor,
                self.acquisition_params.seller_counter_increase_pct * self.counter_jitter_high_factor
            )))
            next_action = current_date + timedelta(days=self.counter_response_delay_days)
            timeout_date = current_date + timedelta(days=self.counter_decision_timeout_days)

            self.update_attempt_state(
                attempt_id=attempt['attempt_id'],
                pipeline_stage=next_stage,
                next_action_date=next_action,
                stage_timeout_date=timeout_date,
                current_date=current_date,
                SellerResponse=response,
                CounterAmount=counter_amount
            )
            
            self._record_acquisition_step(
                attempt_id=attempt['attempt_id'],
                step_type="OFFER_COUNTERED",
                message=f"Property {attempt['property_id']}: Seller countered ${attempt['offer_amount']:,.2f} -> ${counter_amount:,.2f}",
                effective_date=current_date,
                amount=attempt['offer_amount'],
                action=ActionType.UPDATE,
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
            )
            return

        

    def _process_counter_response_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """Process LB's response to counter-offer"""
        # Use seeded RNG (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 1002 + attempt['attempt_id'])

        if rng.random() < self.counter_accept_probability:
            lb_response = 'AcceptCounter'
            inspection_delay = rng.randint(
                self.acquisition_params.inspection_scheduling_delay_min,
                self.acquisition_params.inspection_scheduling_delay_max
            )
            next_action = current_date + timedelta(days=inspection_delay)
            timeout_date = current_date + timedelta(
                days=self.acquisition_params.timeout_inspection_scheduling_max_days
            )

            # Reserve additional funds for counter (counter is higher than original offer)
            offer_amount = attempt['offer_amount']
            counter_amount = attempt['counter_amount']
            additional_amount = counter_amount - offer_amount

            if additional_amount > 0:
                self.fund_manager.reserve_additional_funds_for_counter(
                    run_id=self.run_id,
                    additional_amount=additional_amount,
                    ledger_date=current_date,
                    property_id=attempt['property_id'],
                    counter_amount=counter_amount
                )

            self.update_attempt_state(
                attempt_id=attempt['attempt_id'],
                pipeline_stage='SchedulingInspection',
                next_action_date=next_action,
                stage_timeout_date=timeout_date,
                current_date=current_date,
                LBResponseToCounter=lb_response
            )
            
            self._record_acquisition_step(
                attempt_id=attempt['attempt_id'],
                step_type="COUNTER_ACCEPTED",
                message=f"Property {attempt['property_id']}: Liberty Bee accepted counter-offer ${counter_amount:,}",
                effective_date=current_date,
                amount=attempt['offer_amount'],
                action=ActionType.UPDATE,
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
            )

            # Lock property under contract with LB (counter accepted = contract formed)
            self.lock_property_under_contract(attempt['property_id'], current_date)
        else:
            # Withdraw
            self.fail_attempt(
                attempt_id=attempt['attempt_id'],
                failure_reason='Liberty Bee declined counter-offer',
                pipeline_stage='Withdrawn',
                current_date=current_date
            )

            

    def _process_inspection_scheduling_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """Placeholder: Schedule inspection (currently just advances immediately)"""
        inspection_date = current_date + timedelta(days=self.inspection_schedule_delay_days)

        self.update_attempt_state(
            attempt_id=attempt['attempt_id'],
            pipeline_stage='AwaitingInspection',
            current_date=current_date,
            next_action_date=inspection_date,
            stage_timeout_date=None,  # No timeout during inspection wait
            InspectionDate=inspection_date
        )

    def _process_inspection_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """Process inspection results"""
        # Use seeded RNG (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 2000 + attempt['attempt_id'])

        # Determine severity
        roll = rng.random()
        if roll < self.acquisition_params.inspection_clean_probability:
            severity = 'Clean'
        elif roll < (self.acquisition_params.inspection_clean_probability +
                     self.acquisition_params.inspection_minor_probability):
            severity = 'Minor'
        elif roll < (self.acquisition_params.inspection_clean_probability +
                     self.acquisition_params.inspection_minor_probability +
                     self.acquisition_params.inspection_moderate_probability):
            severity = 'Moderate'
        else:
            severity = 'Major'
        repair_min, repair_max = self.repair_cost_bands[severity]

        # Calculate costs
        inspection_cost = Decimal(str(rng.uniform(
            float(self.acquisition_params.inspection_cost_min),
            float(self.acquisition_params.inspection_cost_max)
        )))

        repair_cost = Decimal(str(rng.uniform(repair_min, repair_max)))

        # Charge inspection cost
        if self.fund_manager:
            self.fund_manager.process_expense(
                run_id=self.run_id,
                expense_amount=inspection_cost,
                ledger_date=current_date,
                expense_type=f"Property inspection (AttemptID={attempt['attempt_id']})"
            )

        # KD-193: lead risk assessment is part of due diligence. Draw the
        # ACTUAL lead outcome (doc-review → hazard → abatement ±band, dedicated
        # RNG offset +5000 — its own stream, a full 1000 clear of every other
        # pipeline stage (offer +1000, inspection +2000, negotiation +3000,
        # closing +4000) so no two stages' seeds can collide within a run's
        # attempt-ID range (Keight #3). The assessment cost is a DD cost
        # (charged now, even on later withdrawal — you pay the assessor
        # regardless). The abatement cost is FORESEEN and persisted
        # (AssessedLeadCost) → store-and-forward to the post-closing
        # LEAD_ABATEMENT, which reads it rather than re-drawing.
        #
        # When no assessor is wired (legacy/test path), AssessedLeadCost stays
        # NULL and LeadAssessed stays 0 so the post-closing doc-review fallback
        # still fires (Keight #2 — don't silently mark every attempt "clean").
        assessed_lead_cost = None
        lead_assessed = 0
        if self.lead_assessor:
            lead_assessed = 1
            lead_rng = random.Random(
                self.random_seed + self.SEED_OFFSET + 5000 + attempt['attempt_id'])
            lead_assessment_cost, assessed_lead_cost = self.lead_assessor(
                attempt['property_id'], lead_rng)
            if lead_assessment_cost > 0 and self.fund_manager:
                self.fund_manager.process_expense(
                    run_id=self.run_id,
                    expense_amount=lead_assessment_cost,
                    ledger_date=current_date,
                    expense_type=f"Lead risk assessment — due diligence (AttemptID={attempt['attempt_id']})"
                )

        # Move to negotiation
        negotiation_delay = rng.randint(
            self.acquisition_params.negotiation_delay_days_min,
            self.acquisition_params.negotiation_delay_days_max
        )
        next_action = current_date + timedelta(days=negotiation_delay)
        timeout_date = current_date + timedelta(
            days=self.acquisition_params.timeout_negotiation_max_days
        )

        self.update_attempt_state(
            attempt_id=attempt['attempt_id'],
            pipeline_stage='Negotiating',
            next_action_date=next_action,
            stage_timeout_date=timeout_date,
            current_date=current_date,
            InspectionSeverity=severity,
            InspectionCost=inspection_cost,
            EstimatedRepairCost=repair_cost,
            AssessedLeadCost=assessed_lead_cost,  # KD-193 store-and-forward (NULL if unassessed)
            LeadAssessed=lead_assessed
        )

        self._record_acquisition_step(
                attempt_id=attempt['attempt_id'],
                step_type="INSPECTION",
                message=f"Property {attempt['property_id']}: Property inspection (Severity: {severity}, Inspection Cost: {inspection_cost:,.2f}, Estimated Repair Cost: {repair_cost:,.2f})",
                effective_date=current_date,
                action=ActionType.UPDATE,
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
            )		

    def _process_negotiation_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """Process negotiation based on inspection results"""
        # Use seeded RNG (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 3000 + attempt['attempt_id'])

        repair_cost = attempt['estimated_repair_cost']
        agreed_price = attempt.get('counter_amount') or attempt['offer_amount']

        # Check withdrawal threshold — REPAIR only (deal quality). Lead is
        # deliberately EXCLUDED (KD-193 D2): ~99.6% of the stock is pre-1978,
        # so folding lead into the deal-quality gate would auto-reject the
        # exact housing the mission exists to serve. Lead is handled by the
        # carryability test below.
        repair_pct = float(repair_cost / agreed_price) if agreed_price else 0
        if repair_pct > self.acquisition_params.lb_withdrawal_threshold_pct:
            self.fail_attempt(
                attempt_id=attempt['attempt_id'],
                failure_reason=f'Repair costs too high ({repair_pct:.1%} of price)',
                pipeline_stage='Withdrawn',
                current_date=current_date
            )
            return

        # KD-193 — lead CARRYABILITY (informed choice, not a deal-quality
        # reject). The due-diligence assessment gave a KNOWN abatement cost;
        # the gate lump now prices it (D3 coherence), so if carrying the
        # pipeline INCLUDING this property's known lead drives deployable Cash
        # negative against the reserve floor, the org withdraws THIS property.
        # "Lead is a given; we test whether we can carry it" (Gray). Fires
        # only for a lead-bearing property so non-lead acquisition is unchanged.
        lead_row = self.db.execute_query(
            "SELECT AssessedLeadCost, LeadAssessed FROM simulation.PropertyAcquisitionAttempt "
            "WHERE RunID = ? AND AttemptID = ?", (self.run_id, attempt['attempt_id']))
        if (lead_row and lead_row[0][1] and lead_row[0][0] is not None
                and Decimal(str(lead_row[0][0])) > 0 and self.gate_basis_provider):
            known_lead = Decimal(str(lead_row[0][0]))
            month_index = ((current_date.year - self.config.start_date.year) * 12
                           + (current_date.month - self.config.start_date.month) + 1)
            bases = self.gate_basis_provider(current_date)
            floor = self._required_cash_floor(bases)
            earmark = self._reserve_earmark(month_index, current_date, bases.owned_opex)
            balances = self.fund_manager.get_fund_balances(self.run_id, current_date)
            deployable = balances.cash_balance - floor - earmark
            if deployable < 0:
                # Concise reason (column-bounded); the floor decomposition is
                # logged to the acquisition step record for the audit trail.
                self._record_acquisition_step(
                    attempt_id=attempt['attempt_id'],
                    step_type="LEAD_CARRYABILITY_WITHDRAWAL",
                    message=(f"Withdrew: known lead abatement ${known_lead:,.2f}, "
                             f"deployable Cash ${deployable:,.2f} < 0 after reserve "
                             f"({self._floor_detail(bases, floor)})"),
                    effective_date=current_date,
                    action=ActionType.UPDATE,
                    entity_type=EntityType.PROPERTY,
                    entity_id=attempt['property_id'],
                )
                self.fail_attempt(
                    attempt_id=attempt['attempt_id'],
                    failure_reason=f"Lead carryability: abatement ${known_lead:,.0f} exceeds carry capacity",
                    pipeline_stage='Withdrawn',
                    current_date=current_date
                )
                return

        # Negotiate outcome
        roll = rng.random()
        if roll < self.acquisition_params.price_reduction_probability:
            outcome = 'PriceReduction'
            reduction = repair_cost * Decimal(str(rng.uniform(
                self.price_reduction_share_low, self.price_reduction_share_high)))
            final_price = agreed_price - reduction
        elif roll < (self.acquisition_params.price_reduction_probability +
                     self.acquisition_params.seller_repairs_probability):
            outcome = 'SellerRepairs'
            final_price = agreed_price
        else:
            outcome = 'NoConsessions'
            final_price = agreed_price

        # Move to closing
        closing_delay = rng.randint(
            self.acquisition_params.closing_delay_days_min,
            self.acquisition_params.closing_delay_days_max
        )
        next_action = current_date + timedelta(days=closing_delay)
        timeout_date = current_date + timedelta(
            days=self.acquisition_params.timeout_closing_max_days
        )

        self.update_attempt_state(
            attempt_id=attempt['attempt_id'],
            pipeline_stage='AwaitingClosing',
            next_action_date=next_action,
            stage_timeout_date=timeout_date,
            current_date=current_date,
            NegotiationOutcome=outcome,
            AdjustedPurchasePrice=final_price
        )

        self._record_acquisition_step(
                attempt_id=attempt['attempt_id'],
                step_type="AWAITING_CLOSING",
                message=f"Property {attempt['property_id']}: Awaiting closing: (Negotion outcome: {outcome})",
                effective_date=current_date,
                action=ActionType.UPDATE,
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
            )	

    def _process_closing_stage(self, attempt: Dict[str, Any], current_date: date) -> None:
        """Process closing - complete acquisition"""
        # Use seeded RNG (property-specific via attempt_id)
        rng = random.Random(self.random_seed + self.SEED_OFFSET + 4000 + attempt['attempt_id'])

        final_price = attempt['adjusted_purchase_price']
        closing_costs = (final_price * Decimal(str(self.acquisition_params.closing_costs_pct))).quantize(Decimal('0.01'))

        # Mark as completed
        self.update_attempt_state(
            attempt_id=attempt['attempt_id'],
            pipeline_stage='Completed',
            next_action_date=None,
            stage_timeout_date=None,
            current_date=current_date,
            is_active=False,
            ClosingDate=current_date,
            FinalPurchasePrice=final_price,
            ClosingCosts=closing_costs,
            AcquisitionSucceeded=True
        )

        self._record_acquisition_step(
                attempt_id=attempt['attempt_id'],
                step_type="CLOSED",
                message=f"Property {attempt['property_id']}: Closed: (Final Price:: {final_price:,.2f}, Closing Costs: {closing_costs:,.2f}",
                effective_date=current_date,
                action=ActionType.UPDATE,
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
            )	


        # Execute fund transaction (use reserved funds from CashHold)
        # Note: Cash was already debited when offer was made (reserve_funds_for_offer)
        # The hold is the AGREED price
        # (offer, or counter when accepted) but the deal closes at final_price —
        # the price-reduction delta must return to Cash in the same motion or
        # it strands in CashHold forever (measured $45-92K per 240-mo run).
        self.fund_manager.use_reserved_funds(
            run_id=self.run_id,
            offer_amount=final_price,
            ledger_date=current_date,
            property_id=attempt['property_id']
        )
        reserved_total = self._outstanding_reservation(
            attempt['offer_amount'], attempt['counter_amount'], attempt['lb_response_to_counter'])
        unused_reservation = reserved_total - final_price
        if unused_reservation > 0:
            self.fund_manager.release_funds_from_offer(
                run_id=self.run_id,
                offer_amount=unused_reservation,
                ledger_date=current_date,
                property_id=attempt['property_id'],
                reason='Unused reservation returned at closing (final price below reserved)'
            )

        # Charge acquisition closing costs. They were computed (and stored
        # to the attempt record) but never debited from any fund, so Cash was
        # overstated by the sum of all closing costs and solvency read optimistic.
        # Charged from Cash like the other acquisition costs (e.g. inspection).
        if self.fund_manager and closing_costs > 0:
            self.fund_manager.process_expense(
                run_id=self.run_id,
                expense_amount=closing_costs,
                ledger_date=current_date,
                expense_type=f"Acquisition closing costs (PropertyID={attempt['property_id']})"
            )

        # Mark property as acquired by Liberty Bee in PropertyMarket
        self.mark_property_acquired(
            property_id=attempt['property_id'],
            final_price=final_price,
            current_date=current_date
        )

        # Log acquisition event (needed for Properties.EventID)
        acquisition_event_id = None
        if self.event_logger:
            acquisition_event_id = self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.CREATE,
                message=f"Adding Property {attempt['property_id']}: to portfolio at ${final_price:,.2f}",
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
                effective_date=current_date
            )


        # Add property to our portfolio (simulation.Properties and simulation.PropertyUnits)
        self.add_acquired_property_to_portfolio(
            property_id=attempt['property_id'],
            acquisition_date=current_date,
            final_price=final_price,
            event_id=acquisition_event_id
        )

        # Log completion
        if self.event_logger:
            self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.COMPLETE,
                message=f"Acquisition closed: ${final_price:,.2f}",
                entity_type=EntityType.PROPERTY,
                entity_id=attempt['property_id'],
                effective_date=current_date
            )

    # ========================================
    # OPPORTUNITY SCANNER - Start New Pipelines
    # ========================================

    def check_for_new_opportunities(self, current_date: date, month_index: int,
                                    config: ProjectionConfig,
                                    committed_target: Decimal = Decimal('0')) -> Optional[int]:
        """
        Check if we can start a new acquisition pipeline

        Called daily. Starts new pipeline if:
        1. Active count < max concurrent
        2. CSF reserve-first gate / grace period allows acquisition (KD #99),
           on the PRO-FORMA basis (KD-042 — in-flight pipelines priced)
        3. Budget available (after the reserve earmark)
        4. Best property found
        5. KD-042 post-commitment re-gate: the org AFTER committing to
           this candidate still clears its floor (same helper, candidate
           folded into the pipeline set — incl. its onboarding lump)

        The two OpEx bases come fresh from the gate-basis provider at gate
        time (one consistent snapshot of owned counts + pipeline set — the
        closing handoff can never open an under-count window, M2).

        Args:
            current_date: Current simulation date
            month_index: Current simulation month
            config: Projection configuration
            committed_target: CSF target the last month-end top-up funded to
                (drives the genuine-draw latch). Defaults to 0.

        Returns:
            AttemptID of new pipeline if started, None otherwise
        """
        # Check capacity
        active_count = self.get_active_pipeline_count()
        max_concurrent = self.get_max_concurrent_pipelines(month_index)

        if active_count >= max_concurrent:
            return None

        if self.gate_basis_provider is None:
            raise RuntimeError(
                "KD-042: gate_basis_provider not set — the acquisition gate "
                "cannot run without its pro-forma basis (set_gate_basis_provider)")
        bases = self.gate_basis_provider(current_date)

        # Check reserve-first gate / grace period (KD #99) on the pro-forma basis
        can_acquire, reason = self.can_acquire_property(
            month_index, current_date, bases, committed_target
        )
        if not can_acquire:
            # V-F: refusals the pipeline set caused are the KD-042 signal —
            # log those (rare, binge-window). Rule-1 latch / owned-basis
            # refusals stay silent as before (they recur daily for months).
            if (bases.pipeline_count > 0
                    and self.event_logger and "KD-042" in reason):
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.VALIDATE,
                    message=f"Acquisition blocked (pre-screen): {reason}",
                    entity_type=EntityType.FUND,
                    effective_date=current_date
                )
            return None

        # Check budget — reserve the pending CSF top-up first (KD #99 reserve-first;
        # earmark on the OWNED basis, ratified)
        reserve_earmark = self._reserve_earmark(month_index, current_date, bases.owned_opex)
        budget = self.get_acquisition_budget(current_date, config, bases, reserve_earmark)
        if budget <= 0:
            return None

        # Find best property
        best_property = self.find_best_property(current_date, budget, config)
        if not best_property:
            return None

        # KD-042 R1: post-commitment re-gate — fold the candidate (and its
        # one-time lump) into the SAME basis computation and ask "after
        # committing to THIS, does the org still clear its floor?"
        bases_with_candidate = self.gate_basis_provider(
            current_date, candidate=best_property)
        can_commit, commit_reason = self.can_acquire_property(
            month_index, current_date, bases_with_candidate, committed_target
        )
        if not can_commit:
            if self.event_logger:
                self.event_logger.log_module_event(
                    module_name="PropertyAcquisitionManager",
                    action=ActionType.VALIDATE,
                    message=(
                        f"Acquisition blocked (post-commitment re-gate, KD-042): "
                        f"candidate PropertyID={best_property.property_id} "
                        f"(${best_property.list_price:,.2f}, "
                        f"{best_property.unit_count} units) — {commit_reason}"
                    ),
                    entity_type=EntityType.PROPERTY,
                    entity_id=best_property.property_id,
                    effective_date=current_date
                )
            return None

        # Start new pipeline
        attempt_id = self.start_acquisition_attempt(
            property_id=best_property.property_id,
            listing_price=best_property.list_price,
            offer_date=current_date
        )

        if self.event_logger:
            self.event_logger.log_module_event(
                module_name="PropertyAcquisitionManager",
                action=ActionType.CREATE,
                message=f"Started new acquisition pipeline (AttemptID={attempt_id}, PropertyID={best_property.property_id}, active={active_count+1}/{max_concurrent})",
                entity_type=EntityType.PROPERTY,
                entity_id=best_property.property_id,
                effective_date=current_date
            )

        return attempt_id




