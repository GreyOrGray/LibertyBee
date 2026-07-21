"""
Configuration Loader - Load simulation parameters from database

This module loads projection configurations, employee roles, and other
reference data needed to initialize and run simulations. It acts as the
bridge between raw database tables and structured configuration objects.

Usage:
- Use ConfigurationLoader.load_projection() to get simulation parameters
- Use ConfigurationLoader.load_employee_roles() to get staffing data
- Use ConfigurationLoader.load_properties() to get available properties
"""

import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry, ParameterRegistryError

@dataclass
class RentReductionTier:
    """One active tenure-based rent-reduction tier.

    A tier fires once a household reaches `months` of continuous occupancy in
    its current unit, contributing `pct` to the lease's cumulative reduction.
    Reductions are cumulative off the original signed rent (sum of crossed-tier
    pcts), never compounded.
    """
    tier_number: int
    months: int
    pct: Decimal


@dataclass
class ProjectionConfig:
    """Simulation projection configuration, loaded fail-loud from reference.ParameterRegistry."""
    projection_id: int
    projection_name: str
    start_date: date
    end_date: date
    starting_funds: Decimal
    property_type: str
    # OpEx realism (V00048): itemized $/unit/yr buckets replace
    # the retired AnnualStaticOpEx + AnnualOpExPerUnit aggregate.
    property_tax_per_unit: Decimal
    insurance_per_unit: Decimal
    utilities_owner_per_unit: Decimal

    # CSF reserve curve (V00049): reserve_months(properties) =
    # floor + (peak−floor)×√(N0/N), capped at peak — replaces the retired flat
    # FIN.OperatingReserveMonths=12. N0 is a DECLARED decision (swept 8/12/15).
    csf_reserve_months_peak: int
    csf_reserve_months_floor: int
    csf_reserve_n0_properties: int

    # Inflation rates
    general_inflation_rate: float
    rent_inflation_rate: float
    opex_inflation_rate: float
    property_inflation_rate: float

    # Staff raise rates
    raise_pct_min: float
    raise_pct_8mo: float
    raise_pct_10mo: float
    raise_pct_12mo: float

    # payroll/raise calendar + snapshot cadence —
    # were hardcoded 15 / Dec 31 / 'QUARTERLY' at the simulation call sites.
    payroll_midmonth_day: int
    raise_month: int
    raise_day_of_month: int
    snapshot_cadence: str

    # Inflation regime/mode params (V00050) are read inside
    # inflation_engine via ParameterRegistry (fail-loud) — ~65 rows, the
    # manager-reads-own-params idiom. The old ScenarioType/Volatility/weight
    # machinery is retired; the 4 base rates above remain (Static-mode basis).

    # Tenant Credit System parameters
    tenant_credit_rate: float
    credit_cap: Decimal

    # TCS Redemption parameters (six global guardrails;
    # household-type bands are registry rows TCS.RedemptionBand_* read in
    # tenant_credit_manager (registry-backed))
    tcs_redemption_prob_min: Decimal
    tcs_redemption_prob_default: Decimal
    tcs_redemption_prob_max: Decimal
    tcs_redemption_hardship_floor: Decimal
    tcs_redemption_hardship_boost: Decimal
    tcs_redemption_hardship_cap: Decimal

    # Rent reduction schedule (generic 5-tier read).
    # Active tiers only, sorted ascending by month threshold. Empty list is a
    # valid all-NULL no-op schedule.
    rent_reduction_tiers: List[RentReductionTier]

    # Staffing parameters. Mixed model (Gray 2026-07-02): admin scales
    # per-UNIT (unchanged); maintenance is contracted below a per-PROPERTY
    # crossover (0 FTE), in-house tech(s) + reduced contract share above it.
    maint_crossover_properties: int
    maint_tech_reduced_contract_pct: Decimal
    units_per_admin_early: int
    units_per_admin_late: int
    early_admin_years: int

    # event-driven maintenance streams (MAINT.*, V00048). Turnover +
    # exterior are deterministic charges; routine (negative-binomial counts)
    # and major (Poisson counts) draw lognormal severities per event.
    turnover_cost_base: Decimal
    exterior_per_property: Decimal
    routine_requests_per_unit_year: float
    routine_dispersion: float
    routine_cost_mean: float
    routine_cost_sigma: float
    major_event_rate_per_unit_year: float
    major_event_cost_mean: float
    major_event_cost_sigma: float

    # Property acquisition parameters. vacancy_rate_base: Salem-calibrated
    # steady-state rate (PROP.VacancyRateBase) — feeds acquisition income scoring;
    # the fill-duration fluctuation reads it via TenantManager's globals load.
    acquisition_pct: float
    vacancy_rate_base: float
    below_market_rent_pct: float

    # CSF Recurring Top-Up parameters. The CASH
    # floor moved to FIN.CashFloorMonths at V00049 (it governs
    # unrestricted Cash, not the CSF protected reserve).
    csf_topup_fraction_per_month: Decimal
    cash_floor_months: int

    # Deposit settlement parameters. Registry-sourced:
    # these were inline reference.ProjectionParameters reads in simulation.py.
    dep_eviction_damage_prob: float
    dep_voluntary_damage_prob: float
    dep_damage_min_percent: float
    dep_damage_max_percent: float
    dep_settlement_delay_days: int

    # Payment / late-fee parameters
    pay_base_fail_prob_monthly: float
    pay_grace_period_days: int
    pay_late_fee_percent: float

    # Lease renewal parameters
    lease_early_break_prob_monthly: float
    lease_renewal_rate_pct: float
    lease_landlord_nonrenewal_prob_pct: float
    lease_late_month_threshold: int

    # Retention model parameters (RET category).
    # base_exit (the zero-discount annual voluntary-exit) = 1 - lease_renewal_rate_pct/100
    # (RenewalRatePct reinterpreted). Evidence: evidence_base.md §10.
    ret_discount_sensitivity_beta: float      # beta — good-deal retention sensitivity
    ret_scarcity_sensitivity_gamma: float     # gamma — "nowhere to go" sensitivity
    ret_floor_exit_annual: float              # floor_exit — life-event minimum
    ret_vacancy_ref_pct: float                # vac_ref — balanced-market vacancy normalizer
    ret_mover_regional_vacancy_pct: float     # regional (North Shore) vacancy a mover faces — NOT LB's Salem-local rate
    ret_burden_ceiling_pct: float             # burden_ceiling — unaffordable-rent line
    ret_form_is_logistic: int                 # 0=linear (default), 1=logistic (validation sweep)


class ConfigurationLoader:
    """Load simulation configuration data from database"""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.logger = logging.getLogger(__name__)
        self.event_logger = None

  
    def load_projection(self, projection_id: int) -> Optional[ProjectionConfig]:
        """Load projection configuration by ID"""
        if self.event_logger:
            from event_logger import ActionType, EntityType
            self.event_logger.log_module_event(
                module_name="ConfigurationLoader",
                action=ActionType.START,
                message=f"Loading projection configuration {projection_id}",
                entity_type=EntityType.CONFIGURATION,
                entity_id=projection_id
            )

        try:
            # load from the unified ParameterRegistry (read-once,
            # resolve override->global, coerce by DataType, FAIL LOUD on missing).
            reg = ParameterRegistry(self.db).load(projection_id)

            # Projection-existence check: SIM.ProjectionName is per-projection
            # (override-only, no global), so its absence = no such projection.
            # Preserves the prior "not found -> None" contract. A projection that
            # DOES exist but lacks a required param now fails loud (below).
            if not reg.has('SIM', 'ProjectionName'):
                self.logger.warning(f"No projection found with ID {projection_id}")
                if self.event_logger:
                    self.event_logger.log_error(
                        error_message=f"No projection found with ID {projection_id}",
                        context="ConfigurationLoader.load_projection"
                    )
                return None

            # No COALESCE, no positional row indices: every value is fetched by
            # (category, name) and coerced; a missing required param raises.
            config = ProjectionConfig(
                projection_id=projection_id,
                projection_name=reg.get_str('SIM', 'ProjectionName'),
                start_date=reg.get_date('SIM', 'StartDate'),
                end_date=reg.get_date('SIM', 'EndDate'),
                starting_funds=reg.get_decimal('FIN', 'StartingFunds'),
                property_type=reg.get_str('PROP', 'PropertyType'),
                property_tax_per_unit=reg.get_decimal('OPEX', 'PropertyTaxPerUnit'),
                insurance_per_unit=reg.get_decimal('OPEX', 'InsurancePerUnit'),
                utilities_owner_per_unit=reg.get_decimal('OPEX', 'UtilitiesOwnerPerUnit'),
                csf_reserve_months_peak=reg.get_int('CSF', 'ReserveMonthsPeak'),
                csf_reserve_months_floor=reg.get_int('CSF', 'ReserveMonthsFloor'),
                csf_reserve_n0_properties=reg.get_int('CSF', 'ReserveCurveN0Properties'),
                general_inflation_rate=reg.get_float('INF', 'GeneralInflationRate'),
                rent_inflation_rate=reg.get_float('INF', 'RentInflationRate'),
                opex_inflation_rate=reg.get_float('INF', 'OpExInflationRate'),
                property_inflation_rate=reg.get_float('INF', 'PropertyInflationRate'),
                tenant_credit_rate=reg.get_float('TCS', 'CreditRate'),
                credit_cap=reg.get_decimal('TCS', 'CreditCap'),
                tcs_redemption_prob_min=reg.get_decimal('TCS', 'RedemptionProbMin'),
                tcs_redemption_prob_default=reg.get_decimal('TCS', 'RedemptionProbDefault'),
                tcs_redemption_prob_max=reg.get_decimal('TCS', 'RedemptionProbMax'),
                tcs_redemption_hardship_floor=reg.get_decimal('TCS', 'RedemptionHardshipFloor'),
                tcs_redemption_hardship_boost=reg.get_decimal('TCS', 'RedemptionHardshipBoost'),
                tcs_redemption_hardship_cap=reg.get_decimal('TCS', 'RedemptionHardshipCap'),
                rent_reduction_tiers=self._build_rent_reduction_tiers_from_registry(reg, projection_id),
                maint_crossover_properties=reg.get_int('STAFF', 'MaintCrossoverProperties'),
                maint_tech_reduced_contract_pct=reg.get_decimal('STAFF', 'MaintTechReducedContractPct'),
                units_per_admin_early=reg.get_int('STAFF', 'UnitsPerAdmin_Early'),
                units_per_admin_late=reg.get_int('STAFF', 'UnitsPerAdmin_Late'),
                early_admin_years=reg.get_int('STAFF', 'EarlyAdminYears'),
                turnover_cost_base=reg.get_decimal('MAINT', 'TurnoverCostBase'),
                exterior_per_property=reg.get_decimal('MAINT', 'ExteriorPerProperty'),
                routine_requests_per_unit_year=reg.get_float('MAINT', 'RoutineRequestsPerUnitYear'),
                routine_dispersion=reg.get_float('MAINT', 'RoutineDispersion'),
                routine_cost_mean=reg.get_float('MAINT', 'RoutineCostMean'),
                routine_cost_sigma=reg.get_float('MAINT', 'RoutineCostSigma'),
                major_event_rate_per_unit_year=reg.get_float('MAINT', 'MajorEventRatePerUnitYear'),
                major_event_cost_mean=reg.get_float('MAINT', 'MajorEventCostMean'),
                major_event_cost_sigma=reg.get_float('MAINT', 'MajorEventCostSigma'),
                raise_pct_min=reg.get_float('STAFF', 'RaisePctMin'),
                raise_pct_8mo=reg.get_float('STAFF', 'RaisePct8Mo'),
                raise_pct_10mo=reg.get_float('STAFF', 'RaisePct10Mo'),
                raise_pct_12mo=reg.get_float('STAFF', 'RaisePct12Mo'),
                payroll_midmonth_day=reg.get_int('STAFF', 'PayrollMidMonthDay'),
                raise_month=reg.get_int('STAFF', 'RaiseMonth'),
                raise_day_of_month=reg.get_int('STAFF', 'RaiseDayOfMonth'),
                snapshot_cadence=reg.get_str('SIM', 'SnapshotCadence'),
                acquisition_pct=reg.get_float('PROP', 'AcquisitionPct'),
                vacancy_rate_base=reg.get_float('PROP', 'VacancyRateBase'),
                below_market_rent_pct=reg.get_float('PROP', 'BelowMarketRentPct'),
                csf_topup_fraction_per_month=reg.get_decimal('CSF', 'TopupFractionPerMonth'),
                cash_floor_months=reg.get_int('FIN', 'CashFloorMonths'),
                dep_eviction_damage_prob=reg.get_float('DEP', 'EvictionDamageProbability'),
                dep_voluntary_damage_prob=reg.get_float('DEP', 'VoluntaryDamageProbability'),
                dep_damage_min_percent=reg.get_float('DEP', 'DamageMinPercent'),
                dep_damage_max_percent=reg.get_float('DEP', 'DamageMaxPercent'),
                dep_settlement_delay_days=reg.get_int('DEP', 'SettlementDelayDays'),
                pay_base_fail_prob_monthly=reg.get_float('PAY', 'BaseFailProbMonthly'),
                pay_grace_period_days=reg.get_int('PAY', 'GracePeriodDays'),
                pay_late_fee_percent=reg.get_float('PAY', 'LateFeePercent'),
                lease_early_break_prob_monthly=reg.get_float('LEASE', 'EarlyBreakProbMonthly'),
                lease_renewal_rate_pct=reg.get_float('LEASE', 'RenewalRatePct'),
                lease_landlord_nonrenewal_prob_pct=reg.get_float('LEASE', 'LandlordNonRenewalProbPct'),
                lease_late_month_threshold=reg.get_int('LEASE', 'LateMonthThreshold'),
                ret_discount_sensitivity_beta=reg.get_float('RET', 'DiscountSensitivityBeta'),
                ret_scarcity_sensitivity_gamma=reg.get_float('RET', 'ScarcitySensitivityGamma'),
                ret_floor_exit_annual=reg.get_float('RET', 'FloorExitAnnual'),
                ret_vacancy_ref_pct=reg.get_float('RET', 'VacancyRefPct'),
                ret_mover_regional_vacancy_pct=reg.get_float('RET', 'MoverRegionalVacancyPct'),
                ret_burden_ceiling_pct=reg.get_float('RET', 'BurdenCeilingPct'),
                ret_form_is_logistic=reg.get_int('RET', 'FormIsLogistic'),
            )

            self.logger.info(f"Loaded projection '{config.projection_name}' ({config.start_date} to {config.end_date}) from ParameterRegistry")

            if self.event_logger:
                from event_logger import ActionType, EntityType
                self.event_logger.log_module_event(
                    module_name="ConfigurationLoader",
                    action=ActionType.COMPLETE,
                    message=f"Successfully loaded projection '{config.projection_name}' (${config.starting_funds:,}, {config.start_date} to {config.end_date})",
                    entity_type=EntityType.CONFIGURATION,
                    entity_id=projection_id
                )

            return config

        except ParameterRegistryError as e:
            # Fail-loud: a projection that exists but is missing or has a malformed
            # required parameter is a hard error — no silent None, no fallback.
            self.logger.error(f"ParameterRegistry validation failed for projection {projection_id}: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"ParameterRegistry validation failed for projection {projection_id}",
                    exception=e,
                    context="ConfigurationLoader.load_projection"
                )
            raise
        except Exception as e:
            self.logger.error(f"Failed to load projection {projection_id}: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"Failed to load projection {projection_id}",
                    exception=e,
                    context="ConfigurationLoader.load_projection"
                )
            return None

    def _build_rent_reduction_tiers_from_registry(self, reg, projection_id):
        """Build the active rent-reduction tier list from the registry.

        Pairs each ordinal's RR.*ReductionMonths / RR.*ReductionPct. Tiers absent
        from the registry (the seed excludes inactive tiers entirely — e.g. the
        5th) resolve to (None, None) and are dropped by _build_rent_reduction_tiers,
        reproducing the prior NULL-tier = inactive behavior.
        """
        rr = reg.get_category('RR')
        raw = []
        for num, prefix in [(1, 'First'), (2, 'Second'), (3, 'Third'),
                            (4, 'Fourth'), (5, 'Fifth')]:
            raw.append((num, rr.get(f'{prefix}ReductionMonths'), rr.get(f'{prefix}ReductionPct')))
        return self._build_rent_reduction_tiers(raw, projection_id)

    def _build_rent_reduction_tiers(self, raw_tiers, projection_id):
        """Build the active rent-reduction tier list.

        `raw_tiers` is a list of (tier_number, months, pct) triples straight from
        the projection row. A tier is active only when BOTH months and pct are
        non-NULL. A tier with exactly one side populated is malformed: it is
        dropped and a non-blocking warning is emitted (never
        crash projection loading over a warning). An all-NULL schedule yields an
        empty list, a valid no-op configuration. Active tiers are returned sorted
        ascending by month threshold.
        """
        active = []
        for tier_number, months, pct in raw_tiers:
            if months is None and pct is None:
                continue  # inactive tier — valid, future-reserved
            if months is None or pct is None:
                self._warn_malformed_tier(tier_number, months, pct, projection_id)
                continue
            active.append(RentReductionTier(
                tier_number=tier_number,
                months=int(months),
                pct=Decimal(str(pct)),
            ))
        active.sort(key=lambda t: t.months)
        return active

    def _warn_malformed_tier(self, tier_number, months, pct, projection_id):
        """Emit a non-blocking warning for a malformed (half-NULL) RR tier.

        Always logs through the Python logger. If an event logger is available,
        additionally records a validation event — but a failure to write that
        event must never abort projection loading.
        """
        msg = (f"Rent-reduction tier {tier_number} in projection {projection_id} "
               f"is malformed (months={months}, pct={pct}); exactly one side is "
               f"NULL. Tier ignored for simulation safety.")
        self.logger.warning(msg)
        if self.event_logger:
            try:
                from event_logger import ActionType, EntityType
                self.event_logger.log_module_event(
                    module_name="ConfigurationLoader",
                    action=ActionType.VALIDATE,
                    message=msg,
                    entity_type=EntityType.CONFIGURATION,
                    entity_id=projection_id,
                )
            except Exception as e:
                self.logger.warning(f"Could not log malformed-tier validation event: {e}")
