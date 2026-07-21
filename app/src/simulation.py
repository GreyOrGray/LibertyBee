"""
Liberty Bee Housing Simulation - End-to-End Simulation Engine

This is the main simulation execution engine that runs complete end-to-end
simulations of the Liberty Bee housing model. It integrates all system
components and runs monthly loops for the specified duration.

Parameters:
- --months: Simulation duration (default: 240 for 20 years)
- --projection-id: Which projection scenario to use (default: 206, the $8M released baseline)
- --seed: Random seed for reproducibility (default: 12345)
- --iterations: Number of simulation runs (default: 1)

Usage:
    python simulation.py --months 6 --seed 12345
    python simulation.py --months 240 --projection-id 206 --iterations 10
"""

import logging
import argparse
import random
from dataclasses import dataclass
from decimal import Decimal
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
import os
import sys
from pathlib import Path
from typing import Optional, TextIO
from event_logger import EventLogger, EventType, EntityType, ActionType


@dataclass(frozen=True)
class MonthlyOpExBreakdown:
    """Itemized monthly_opex breakdown (EXPECTED basis).

    Returned by Simulation.compute_monthly_opex_breakdown(). Carries forward
    the monthly_opex contract:
      - .total for acquisition gate / CSF target / CSF top-up. .total is
        the EXPECTED monthly cost — deterministic buckets plus the expected
        value of the maintenance event streams — NOT a month's realized draws.
        A realized basis would let one bad boiler month permanently ratchet
        the CSF target through the latch; reserve targets are sized on
        expected cost, shocks are what the reserve absorbs.
      - The month-end RECURRING_NONPAYROLL_OPEX charge uses the deterministic
        buckets + the REALIZED event draws (see the month-end block) and does
        NOT include payroll (bi-monthly via separate path; double-bill guard).
      - .inflation_factor is cumulative Π(1+OpExRate) from sim start to
        current_day (must compound, not single-month rate).
    Buckets (V00048; evidence_base §4d/§5): property tax, insurance, owner
    utilities (per-unit); exterior/grounds (per-PROPERTY); expected routine +
    major maintenance (event streams); expected turnover make-ready (derived
    from the LEASE renewal/early-break params — no new unfitted rate param;
    evictions excluded as emergent, so the estimate is conservative-low).
    expected_routine already includes the in-house-tech contract reduction
    when a Maintenance Crew FTE is active.
    """
    payroll: Decimal
    property_tax: Decimal
    insurance: Decimal
    utilities: Decimal
    exterior: Decimal
    expected_routine: Decimal
    expected_major: Decimal
    expected_turnover: Decimal
    inflation_factor: Decimal
    owned_units: int
    owned_properties: int
    maintenance_techs: int
    total: Decimal

    @property
    def deterministic_buckets(self) -> Decimal:
        """The non-stochastic monthly charge components (tax + insurance +
        utilities + exterior) — realized == expected for these."""
        return (self.property_tax + self.insurance + self.utilities
                + self.exterior).quantize(Decimal('0.01'))

# Ensure we're running from app directory for proper imports and file access
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))  # this script's dir (app/src)
APP_DIR = os.path.dirname(SCRIPT_DIR)  # app/
PROJECT_ROOT = os.path.dirname(APP_DIR)  # repo root (for scratch/debug_logs output)

# NOTE: Application module imports are deferred to after sys.path setup in main().


class TeeOutput:
    """
    Redirect output to both console and file.

    This class acts as a "tee" - writing to both stdout and a log file
    simultaneously when --debug-log flag is enabled.
    """
    def __init__(self, file_path: str):
        self.terminal = sys.stdout
        self.log = open(file_path, 'w', encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


def print_error_box(message: str):
    """
    Print error message with visual separators for visibility.

    Makes errors stand out in console output so they don't get buried.
    """
    border = "=" * 80
    print(f"\n{border}")
    print(f"ERROR: {message}")
    print(f"{border}\n")


class Simulation:
    """Run complete end-to-end simulation"""

    def __init__(self, env: str, configfile: str, projection_id: int = 206, random_seed: int = 12345,
                 months: Optional[int] = None):
        self.configfile = configfile
        self.db = DatabaseManager(self.configfile)
        self.event_logger = EventLogger(self.db)
        self.fund_manager = FundManager(self.db, self.event_logger)
        self.config_loader = ConfigurationLoader(self.db)
        self.run_manager = RunManager(self.db, self.config_loader)
        self.inflation_engine = InflationEngine(self.db, self.config_loader)
        self.employee_manager = EmployeeManager(self.db, self.fund_manager, self.event_logger)
        self.property_market_manager = PropertyMarketManager(self.db, self.event_logger, {})
        self.property_acquisition_manager = PropertyAcquisitionManager(self.db, self.config_loader, self.fund_manager, self.event_logger, random_seed)
        self.compliance_manager = None  # Initialized after run_id is set
        self.tenant_manager = None  # Initialized after run_id is set
        self.rent_collection_manager = None  # Initialized after run_id is set

        self.projection_id = projection_id
        self.random_seed = random_seed
        self.months_override = months
        self.run_id = None
        self.config = None
        # CSF target the last month-end top-up committed to — the
        # genuine-draw latch reference for the acquisition gate. Updated each
        # month-end top-up; 0 until the first one (grace covers months 1-6).
        self.last_csf_committed_target = Decimal('0')
        self.logger = logging.getLogger(__name__)

    def compute_monthly_opex_breakdown(self, current_day: date,
                                       extra_units: int = 0,
                                       extra_properties: int = 0) -> MonthlyOpExBreakdown:
        """Single source of truth for monthly_opex (expected basis).

        Composes payroll (from EmployeeManager) + itemized per-unit buckets
        (tax/insurance/utilities) + per-property exterior + the EXPECTED value
        of the routine/major maintenance event streams, all inflated via the
        cumulative OpEx inflation factor. Owned units are counted from
        simulation.PropertyUnits regardless of occupancy;
        properties as DISTINCT PropertyID from the same table (same source, no
        count drift). Realized event draws happen in the month-end block only.

        KD-042: ``extra_units`` / ``extra_properties`` fold a
        hypothetical addition (the in-pipeline set) into every count-driven
        bucket — the pro-forma marginal is f(owned+extra) − f(owned) of THIS
        function, so any nonlinearity is priced by the same code the owned
        basis runs. Defaults of 0/0 are byte-identical to the pre-KD-042
        behavior (regression-gated). Payroll stays actual-employees; the
        pro-forma staffing STEP is priced separately in
        compute_acquisition_gate_bases. The ``owned_units`` /
        ``owned_properties`` fields always report OWNED counts, whatever
        extras were passed.
        """
        employees = self.employee_manager.get_active_employees(self.run_id, current_day)
        payroll = sum((emp.base_salary + emp.benefits_cost) / 12 for emp in employees)
        payroll_q = Decimal(str(payroll)).quantize(Decimal('0.01'))
        maint_techs = self.employee_manager.count_employees_by_role(employees, "Maintenance Crew")

        counts_row = self.db.execute_query(
            "SELECT COUNT(*), COUNT(DISTINCT PropertyID) "
            "FROM simulation.PropertyUnits WHERE RunID = ?",
            (self.run_id,),
        )
        owned_units = int(counts_row[0][0]) if counts_row else 0
        owned_properties = int(counts_row[0][1]) if counts_row else 0
        basis_units = owned_units + extra_units
        basis_properties = owned_properties + extra_properties

        inflation_factor = self.inflation_engine.get_cumulative_opex_factor(
            self.run_id, current_day
        )

        def monthly_per_unit(annual: Decimal) -> Decimal:
            return ((annual * Decimal(basis_units) / Decimal(12))
                    * inflation_factor).quantize(Decimal('0.01'))

        property_tax = monthly_per_unit(self.config.property_tax_per_unit)
        insurance = monthly_per_unit(self.config.insurance_per_unit)
        utilities = monthly_per_unit(self.config.utilities_owner_per_unit)
        exterior = ((self.config.exterior_per_property * Decimal(basis_properties)
                     / Decimal(12)) * inflation_factor).quantize(Decimal('0.01'))

        # Expected event-stream costs (base-year) -> inflated. Routine spend
        # drops to the reduced contract share once an in-house tech is active
        # (the tech's salary shows up in payroll instead); major/specialty
        # work stays fully contracted. (Pro-forma note: the tech count is the
        # ACTUAL headcount even when extras are passed — if the pipeline would
        # step a first tech, the basis prices full contract share AND the
        # step's salary; slightly conservative-high, the permitted direction.)
        expected_routine_base = Decimal(str(
            self.maintenance_event_manager.expected_monthly_routine_cost(basis_units)))
        if maint_techs > 0:
            expected_routine_base *= self.config.maint_tech_reduced_contract_pct
        expected_routine = (expected_routine_base * inflation_factor).quantize(Decimal('0.01'))
        expected_major = (Decimal(str(
            self.maintenance_event_manager.expected_monthly_major_cost(basis_units)))
            * inflation_factor).quantize(Decimal('0.01'))

        # Expected turnover make-ready: derived from the
        # engine's own lease params — per unit-month, a lease turns if the
        # year-end renewal fails (tenant declines OR landlord non-renews,
        # spread over the 12-mo term) or it breaks early. Evictions are
        # emergent (pay-fail driven) and excluded — the estimate is
        # deliberately conservative-low, never flattering-high.
        # RenewalRatePct is now the market-equivalent base_exit CEILING —
        # retention modulates actual voluntary exit BELOW it (measured ~9.7-13.5% vs the
        # 0.20 base), so this over-estimates turnover. Conservative for the OpEx/CSF
        # reserve (never flattering); revisit at re-baseline if the reserve is materially high.
        renewal_survive = (self.config.lease_renewal_rate_pct / 100.0) * \
            (1.0 - self.config.lease_landlord_nonrenewal_prob_pct / 100.0)
        expected_turns_per_unit_month = (1.0 - renewal_survive) / 12.0 + \
            self.config.lease_early_break_prob_monthly
        expected_turnover = (
            Decimal(str(expected_turns_per_unit_month)) * Decimal(basis_units)
            * self.config.turnover_cost_base * inflation_factor
        ).quantize(Decimal('0.01'))

        total = (payroll_q + property_tax + insurance + utilities + exterior
                 + expected_routine + expected_major
                 + expected_turnover).quantize(Decimal('0.01'))

        return MonthlyOpExBreakdown(
            payroll=payroll_q,
            property_tax=property_tax,
            insurance=insurance,
            utilities=utilities,
            exterior=exterior,
            expected_routine=expected_routine,
            expected_major=expected_major,
            expected_turnover=expected_turnover,
            inflation_factor=inflation_factor,
            owned_units=owned_units,
            owned_properties=owned_properties,
            maintenance_techs=maint_techs,
            total=total,
        )

    def compute_acquisition_gate_bases(self, current_day: date, candidate=None):
        """KD-042 (ratified design): one consistent snapshot of the
        acquisition gate's TWO OpEx bases + the one-time onboarding lump.

        - owned basis: compute_monthly_opex_breakdown as-is — feeds the CSF
          earmark ONLY (the reserve target is never forward-priced, by ratified design).
        - pro-forma basis: the same breakdown at (owned + pipeline) counts —
          the set-wise marginal prices threshold crossings the per-property
          sum would miss — plus the expected STAFFING STEP (the staffing
          formula evaluated at combined vs owned counts, priced at expected
          hire cost, since salaries are only drawn at hire time).
        - onboarding lump (full ratified content): per in-pipeline property,
          closing costs + due-diligence repair (ACTUAL EstimatedRepairCost
          once its inspection has drawn, else the severity-weighted ACQ-band
          expectation) + the expected compliance onboarding chains including
          the pre-cutoff LEAD chain (post-closing, unit-scaled,
          un-withdrawable — KD-043: reachable and calibrated; the gate
          derives from the same knobs as the realized spawn, so pricing
          and realization move together by construction).

        Membership: every ``IsActive = 1`` attempt — priced from the
        moment intent exists, before CashHold lands. Owned counts and the
        pipeline set are read in the same call, so the closing handoff can
        never leave a property out of BOTH bases (M2: overlap allowed, gap
        never). ``candidate`` folds one more property in for the R1
        post-commitment re-gate.
        """
        from property_acquisition_manager import AcquisitionGateBases

        owned = self.compute_monthly_opex_breakdown(current_day)

        pam = self.property_acquisition_manager
        if not pam.acquisition_params:
            pam.load_acquisition_parameters()
        closing_pct = Decimal(str(pam.acquisition_params.closing_costs_pct))
        expected_dd_repair = pam.expected_due_diligence_repair_cost()

        rows = self.db.execute_query(
            """SELECT paa.PropertyID, paa.ListingPrice, paa.OfferAmount,
                      paa.CounterAmount, paa.LBResponseToCounter,
                      paa.EstimatedRepairCost, rp.YearBuilt,
                      (SELECT COUNT(*) FROM reference.Units u
                       WHERE u.PropertyID = paa.PropertyID) AS UnitCount,
                      paa.AssessedLeadCost, paa.LeadAssessed
               FROM simulation.PropertyAcquisitionAttempt paa
               JOIN reference.Properties rp ON rp.PropertyID = paa.PropertyID
               WHERE paa.RunID = ? AND paa.IsActive = 1""",
            (self.run_id,),
        )

        pipeline = []
        for r in rows:
            # Committed price: the outstanding-reservation rule (offer /
            # accepted counter), else the listing price pre-offer.
            reserved = pam._outstanding_reservation(r[2], r[3], r[4])
            price = Decimal(str(reserved)) if reserved and reserved > 0 else Decimal(str(r[1]))
            repair = Decimal(str(r[5])) if r[5] is not None else expected_dd_repair
            # KD-193 (D3 coherence): once the property's lead is assessed at
            # due diligence, price its onboarding lump at the KNOWN actual lead,
            # not E[lead] — mirrors EstimatedRepairCost refining once drawn.
            known_lead = Decimal(str(r[8])) if (r[9] and r[8] is not None) else None
            pipeline.append({
                'units': int(r[7]), 'price': price, 'repair': repair,
                'year_built': int(r[6]) if r[6] is not None else None,
                'known_lead': known_lead,
            })

        if candidate is not None:
            yb_row = self.db.execute_query(
                "SELECT YearBuilt FROM reference.Properties WHERE PropertyID = ?",
                (candidate.property_id,))
            year_built = int(yb_row[0][0]) if yb_row and yb_row[0][0] is not None else None
            pipeline.append({
                'units': int(candidate.unit_count),
                'price': Decimal(str(candidate.list_price)),
                'repair': expected_dd_repair,
                'year_built': year_built,
                'known_lead': None,  # candidate not yet assessed → E[lead]
            })

        pipeline_units = sum(p['units'] for p in pipeline)
        pipeline_properties = len(pipeline)

        if pipeline_properties == 0:
            return AcquisitionGateBases(
                owned_opex=owned.total, pro_forma_opex=owned.total,
                onboarding_lump=Decimal('0.00'), pipeline_count=0,
                pipeline_units=0, pipeline_properties=0)

        pro_forma = self.compute_monthly_opex_breakdown(
            current_day, extra_units=pipeline_units,
            extra_properties=pipeline_properties)

        # Expected staffing step: the SAME formula at combined vs owned counts
        # (never vs current headcount — pending owned-driven hires belong to
        # check_staffing_needs, and the step must be zero at zero pipeline).
        # Admin steps price at Administration Manager's expectation: the hire
        # loop's alternation index resets per call, and threshold crossings
        # arrive one at a time, so the next admin hire beyond the base-2
        # floor is Administration Manager in practice (also the
        # pricier band, the conservative direction).
        em = self.employee_manager
        years_operating = (current_day - self.config.start_date).days / 365.25
        formula_args = (self.config.maint_crossover_properties,
                        self.config.units_per_admin_early,
                        self.config.units_per_admin_late,
                        self.config.early_admin_years, em.base_admin_count)
        maint_owned, admin_owned = em.staffing_formula(
            owned.owned_properties, owned.owned_units, years_operating, *formula_args)
        maint_comb, admin_comb = em.staffing_formula(
            owned.owned_properties + pipeline_properties,
            owned.owned_units + pipeline_units, years_operating, *formula_args)
        staffing_step = Decimal('0')
        if maint_comb > maint_owned:
            staffing_step += (Decimal(maint_comb - maint_owned)
                              * em.expected_annual_role_cost("Maintenance Crew"))
        if admin_comb > admin_owned:
            staffing_step += (Decimal(admin_comb - admin_owned)
                              * em.expected_annual_role_cost("Administration Manager"))
        staffing_step /= 12

        pro_forma_opex = (pro_forma.total + staffing_step).quantize(Decimal('0.01'))

        lump = Decimal('0')
        for p in pipeline:
            lump += (p['price'] * closing_pct).quantize(Decimal('0.01'))
            lump += p['repair']
            lump += self.compliance_manager.expected_onboarding_compliance_cost(
                p['units'], p['year_built'], known_lead_cost=p.get('known_lead'))

        return AcquisitionGateBases(
            owned_opex=owned.total,
            pro_forma_opex=pro_forma_opex,
            onboarding_lump=lump.quantize(Decimal('0.01')),
            pipeline_count=len(pipeline),
            pipeline_units=pipeline_units,
            pipeline_properties=pipeline_properties)

    def count_turnovers_started(self, month_start: date, month_end: date) -> int:
        """Turns whose make-ready began in [month_start, month_end] —
        one WorkSequence=1 row per turnover (its ScheduledStartDate is the
        termination date). Stateless so replays/reruns can't double-charge."""
        row = self.db.execute_query(
            "SELECT COUNT(*) FROM simulation.TurnoverWorkOrder "
            "WHERE RunID = ? AND WorkSequence = 1 "
            "AND ScheduledStartDate BETWEEN ? AND ?",
            (self.run_id, month_start, month_end),
        )
        return int(row[0][0]) if row else 0

    def initialize_simulation(self) -> bool:
        """Initialize simulation run and components"""
        try:
            # Create simulation run
            self.run_id = self.run_manager.create_run(
                projection_id=self.projection_id,
                random_seed=self.random_seed,
                run_type="Simulation"
            )
            self.config = self.config_loader.load_projection(self.projection_id)

            if not self.run_id or not self.config:
                error_msg = "Failed to initialize simulation - could not create run or load projection"
                self.logger.error(error_msg)
                print_error_box(error_msg)
                sys.exit(1)  # Exit immediately on critical error

            # Override end_date if months specified
            if self.months_override is not None:
                original_end = self.config.end_date
                self.config.end_date = self.config.start_date + relativedelta(months=self.months_override)
                print(f"Duration override: {self.months_override} months (end date: {original_end} -> {self.config.end_date})")

            # Set up event logging for this run
            self.event_logger.set_run_id(self.run_id)

            # Set up property acquisition manager for this run
            self.property_acquisition_manager.set_run_id(self.run_id)
            self.property_acquisition_manager.set_config(self.config)
            # Wire EmployeeManager so check_staffing_needs
            # fires after each property close (post-acquisition trigger).
            self.property_acquisition_manager.set_employee_manager(self.employee_manager)
            # KD-042: the gate's two-basis snapshot provider — the gate
            # computes NOTHING itself (single source of truth lives here).
            self.property_acquisition_manager.set_gate_basis_provider(
                self.compute_acquisition_gate_bases)

            # Initialize compliance manager for this run
            self.compliance_manager = ComplianceManager(self.db, self.event_logger, self.run_id, fund_manager=self.fund_manager, random_seed=self.random_seed)

            # KD-193: wire the pre-closing lead assessor (compliance owns the
            # knobs + YearBuilt/unit sources; PAM calls it at due diligence).
            self.property_acquisition_manager.set_lead_assessor(
                self.compliance_manager.assess_property_lead_for_acquisition)

            # Deposit settlement params come from the registry via ProjectionConfig.

            # Initialize security deposit manager for this run
            from security_deposit_manager import SecurityDepositManager
            self.security_deposit_manager = SecurityDepositManager(
                db_manager=self.db,
                event_logger=self.event_logger,
                fund_manager=self.fund_manager,
                run_id=self.run_id,
                seed=self.random_seed,
                eviction_damage_prob=self.config.dep_eviction_damage_prob,
                voluntary_damage_prob=self.config.dep_voluntary_damage_prob,
                damage_min_percent=self.config.dep_damage_min_percent,
                damage_max_percent=self.config.dep_damage_max_percent,
                settlement_delay_days=self.config.dep_settlement_delay_days
            )

            # Initialize tenant credit manager for this run
            from tenant_credit_manager import TenantCreditManager
            self.tenant_credit_manager = TenantCreditManager(
                db_manager=self.db,
                event_logger=self.event_logger,
                run_id=self.run_id,
                credit_rate=Decimal(str(self.config.tenant_credit_rate)),
                credit_cap=self.config.credit_cap,
                # Redemption guardrails + seed
                redemption_prob_min=self.config.tcs_redemption_prob_min,
                redemption_prob_default=self.config.tcs_redemption_prob_default,
                redemption_prob_max=self.config.tcs_redemption_prob_max,
                hardship_floor=self.config.tcs_redemption_hardship_floor,
                hardship_boost=self.config.tcs_redemption_hardship_boost,
                hardship_cap=self.config.tcs_redemption_hardship_cap,
                run_seed=self.random_seed,
                logger=self.logger if hasattr(self, "logger") else None,
            )

            # Initialize tenant manager for this run
            try:
                self.tenant_manager = TenantManager(
                    db_manager=self.db,
                    event_logger=self.event_logger,
                    run_id=self.run_id,
                    run_seed=self.random_seed,
                    below_market_rent_pct=self.config.below_market_rent_pct,
                    deposit_manager=self.security_deposit_manager,
                    tenant_credit_manager=self.tenant_credit_manager,
                )
            except Exception as e:
                print(f"ERROR: TenantManager initialization failed: {e}")
                import traceback
                traceback.print_exc()
                raise

            # Initialize rent collection manager for this run
            # Create seeded RNG for deterministic payment failures
            payment_rng = random.Random(self.random_seed)

            # Payment / late-fee params come from the registry via ProjectionConfig.


            self.rent_collection_manager = RentCollectionManager(
                db_manager=self.db,
                fund_manager=self.fund_manager,
                event_logger=self.event_logger,
                deposit_manager=self.security_deposit_manager,
                tenant_credit_manager=self.tenant_credit_manager,
                rng=payment_rng,
                payment_fail_prob=self.config.pay_base_fail_prob_monthly,
                grace_period_days=self.config.pay_grace_period_days,
                late_fee_percent=self.config.pay_late_fee_percent
            )
            self.rent_collection_manager.set_run_id(self.run_id)

            # Initialize rent reduction manager for this run.
            # Applies the tenure-based RR_* schedule to active leases; must run
            # before rent collection so the month is billed at effective rent.
            from rent_reduction_manager import RentReductionManager
            self.rent_reduction_manager = RentReductionManager(
                db_manager=self.db,
                event_logger=self.event_logger,
                run_id=self.run_id,
                rent_reduction_tiers=self.config.rent_reduction_tiers,
                logger=self.logger if hasattr(self, "logger") else None,
            )

            # Initialize turnover manager for this run
            from turnover_manager import TurnoverManager
            self.turnover_manager = TurnoverManager(
                db_manager=self.db,
                event_logger=self.event_logger,
                run_id=self.run_id,
                seed=self.random_seed,
            )

            # Initialize maintenance event draws for this run.
            # Dedicated seeded stream (offset 30910); draws fire at month-end.
            from maintenance_event_manager import MaintenanceEventManager
            self.maintenance_event_manager = MaintenanceEventManager(
                config=self.config,
                run_seed=self.random_seed,
            )

            # Initialize snapshot manager for this run (cadence is
            # SIM.SnapshotCadence — seeded QUARTERLY)
            from snapshot_manager import SnapshotManager
            self.snapshot_manager = SnapshotManager(
                db_manager=self.db,
                run_id=self.run_id,
                cadence=self.config.snapshot_cadence,
            )
            # Persist cadence on simulation.Run
            self.db.execute_non_query(
                "UPDATE simulation.Run SET SnapshotCadence=? WHERE RunID=?",
                (self.config.snapshot_cadence, self.run_id),
            )

            # Initialize eviction manager for this run
            self.eviction_manager = EvictionManager(
                db_manager=self.db,
                fund_manager=self.fund_manager,
                event_logger=self.event_logger,
                deposit_manager=self.security_deposit_manager,
                turnover_manager=self.turnover_manager,
                tenant_credit_manager=self.tenant_credit_manager,  # TCS forfeiture on eviction
            )

            # Lease renewal params come from the registry via ProjectionConfig.

            # Initialize lease renewal manager for this run
            # Create separate seeded RNG for lease decisions (deterministic)
            lease_rng = random.Random(self.random_seed + 1)  # Different seed offset from payment RNG

            self.lease_renewal_manager = LeaseRenewalManager(
                db_manager=self.db,
                event_logger=self.event_logger,
                rng=lease_rng,
                run_seed=self.random_seed,  # dedicated renewal_rng (offset 30901)
            )

            # Pass parameters to manager
            self.lease_renewal_manager._early_break_prob = self.config.lease_early_break_prob_monthly
            self.lease_renewal_manager._renewal_rate = self.config.lease_renewal_rate_pct
            self.lease_renewal_manager._landlord_nonrenewal_prob = self.config.lease_landlord_nonrenewal_prob_pct
            self.lease_renewal_manager._late_month_threshold = self.config.lease_late_month_threshold
            # Wire deposit manager for settlement at termination
            self.lease_renewal_manager.deposit_manager = self.security_deposit_manager
            # Wire turnover manager for turnover trigger at termination
            self.lease_renewal_manager.turnover_manager = self.turnover_manager
            # Wire TCS manager for exit-detection (mark_household_exited)
            self.lease_renewal_manager.tenant_credit_manager = self.tenant_credit_manager

            # Wire the retention model. base_exit (the
            # zero-discount annual voluntary-exit) = 1 - RenewalRatePct/100 —
            # RenewalRatePct reinterpreted. Owns no RNG; the manager draws.
            from retention_model import RetentionModel
            self.lease_renewal_manager.retention_model = RetentionModel(
                self.db,
                base_exit=1.0 - self.config.lease_renewal_rate_pct / 100.0,
                beta=self.config.ret_discount_sensitivity_beta,
                gamma=self.config.ret_scarcity_sensitivity_gamma,
                floor_exit=self.config.ret_floor_exit_annual,
                vac_ref=self.config.ret_vacancy_ref_pct,
                burden_ceiling=self.config.ret_burden_ceiling_pct,
                regional_vacancy_rate=self.config.ret_mover_regional_vacancy_pct,
                form_is_logistic=self.config.ret_form_is_logistic,
                logger=getattr(self, 'logger', None),
            )

            # Start the run
            self.run_manager.start_run(self.run_id)

            print(f"Initialized simulation run {self.run_id}")
            print(f"Projection: {self.config.projection_name} (ID: {self.projection_id})")
            print(f"Random seed: {self.random_seed}")
            print(f"Period: {self.config.start_date} to {self.config.end_date}")
            print(f"Starting funds: ${self.config.starting_funds:,}")

            return True

        except Exception as e:
            error_msg = f"Simulation initialization failed: {e}"
            self.logger.error(error_msg)
            print_error_box(error_msg)
            import traceback
            traceback.print_exc()
            sys.exit(1)  # Exit immediately on critical error

    def initialize_funds_and_staff(self) -> bool:
        """Set up initial funds and hire core team"""
        try:
            # Calculate CSF target based on expected payroll (2-FTE core
            # team, no day-1 maintenance hire — maintenance is contracted below
            # the property crossover): 2 * ~$70K + 25% benefits = ~$175K annual.
            # Transient bootstrap only — recomputed from actual hires immediately
            # below. NOTE: this literal does NOT track
            # STAFF.BaseAdminCount / salary bands / BenefitsPct; a fork changing
            # those knobs gets a one-time slightly-off day-0 target that
            # self-corrects at first hire.
            estimated_annual_payroll = Decimal('175000')
            monthly_opex = estimated_annual_payroll / 12
            # Pre-acquisition portfolio (0 properties <= N0) holds the full peak.
            csf_target = self.fund_manager.get_csf_target(
                monthly_opex, self.config.csf_reserve_months_peak)

            # Initialize funds
            starting_funds = Decimal(str(self.config.starting_funds))
            self.fund_manager.initialize_funds(
                self.run_id, starting_funds, csf_target, self.config.start_date
            )

            # Hire core team
            self.employee_manager.check_staffing_needs(
                self.run_id, self.config.start_date,
                self.config.maint_crossover_properties, self.config.units_per_admin_early,
                self.config.units_per_admin_late, self.config.early_admin_years,
                self.config.start_date
            )

            # Get actual payroll for CSF recalculation
            employees = self.employee_manager.get_active_employees(self.run_id, self.config.start_date)
            actual_annual_payroll = sum(emp.base_salary + emp.benefits_cost for emp in employees)
            actual_monthly_opex = actual_annual_payroll / 12
            actual_csf_target = self.fund_manager.get_csf_target(
                actual_monthly_opex, self.config.csf_reserve_months_peak)

            print(f"\nInitial Setup:")
            print(f"  Employees hired: {len(employees)}")
            print(f"  Annual payroll: ${actual_annual_payroll:,}")
            print(f"  Monthly payroll: ${actual_monthly_opex:,}")
            print(f"  CSF target: ${actual_csf_target:,}")

            # Check if CSF adjustment needed
            balances = self.fund_manager.get_fund_balances(self.run_id, self.config.start_date)
            if balances.csf_balance < actual_csf_target:
                deficit = actual_csf_target - balances.csf_balance
                self.fund_manager.transfer_to_csf(
                    self.run_id, deficit, self.config.start_date,
                    "Adjust CSF for actual payroll"
                )
                print(f"  CSF adjusted: +${deficit:,}")

            return True

        except Exception as e:
            self.logger.error(f"Funds and staff initialization failed: {e}")
            return False

    def generate_inflation_schedule(self) -> bool:
        """Generate monthly inflation schedule for this run"""
        try:
            self.inflation_engine.generate_schedule(
                self.run_id,
                self.projection_id,
                self.random_seed
            )
            return True

        except Exception as e:
            self.logger.error(f"Inflation schedule generation failed: {e}")
            return False

    def initialize_property_market(self) -> bool:
        """Initialize property market with all properties assigned states"""
        try:
            self.property_market_manager.initialize_market(self.run_id, self.random_seed, self.config.start_date)
            return True

        except Exception as e:
            self.logger.error(f"Property market initialization failed: {e}")
            return False

    def process_annual_raises(self, current_date: date) -> bool:
        """Process annual raises based on cash position and inflation.

        The raise tier check now uses the
        honest monthly_opex (payroll + non-payroll recurring), computed via
        `compute_monthly_opex_breakdown`, instead of the prior payroll-only
        denominator. Combined with the SalaryCap clamp inside the employee
        manager and lower raise rate values (via V00037 migration), this
        addresses the runaway-compensation mechanism.
        """
        try:
            if current_date.month != 12:
                return True  # Only process in December

            employees = self.employee_manager.get_active_employees(self.run_id, current_date)
            if not employees:
                return True

            print(f"\n=== Annual Raises ({current_date.year}) ===")

            # Compute honest monthly_opex breakdown
            # so the raise tier check sees real cash burn, not just payroll.
            breakdown = self.compute_monthly_opex_breakdown(current_date)

            total_raise_cost = self.employee_manager.process_annual_raises(
                self.run_id, current_date,
                self.config.raise_pct_min, self.config.raise_pct_8mo,
                self.config.raise_pct_10mo, self.config.raise_pct_12mo,
                honest_monthly_opex=breakdown.total,
            )

            print(f"Total annual raise cost: ${total_raise_cost:,}")
            return True

        except Exception as e:
            self.logger.error(f"Annual raises processing failed: {e}")
            return False

    def run_simulation(self) -> bool:
        """Run complete end-to-end simulation"""
        try:
            print("STARTING LIBERTY BEE SIMULATION")
            print("=" * 60)

            if not self.initialize_simulation():
                return False

            # Generate inflation schedule FIRST (pre-simulation, month 0)
            print("\n=== Generating Inflation Schedule (Pre-Simulation) ===")
            if not self.generate_inflation_schedule():
                return False
            print("Inflation schedule generated")

            # Now initialize funds and staff (first simulation events, month 1)
            if not self.initialize_funds_and_staff():
                return False

            print("\n=== Initializing Property Market ===")
            if not self.initialize_property_market():
                return False
            print("Property market initialized with pricing")

            # Simulation loop: daily events within monthly iterations
            current_month = self.config.start_date
            end_date = self.config.end_date
            month_count = 0
            failed_month = None
            halt_notes = None  # set at whichever halt site fires

            print(f"\n=== SIMULATION LOOP (DAILY EVENTS) ===")
            print(f"Processing {self.config.start_date} to {self.config.end_date}")

            while current_month <= end_date:
                month_count += 1
                month_start = current_month
                month_end = (current_month + relativedelta(months=1)) - timedelta(days=1)

                # Daily loop within this month
                current_day = month_start
                while current_day <= month_end and current_day <= end_date:
                    day_of_month = current_day.day

                    # Process property market daily (every day)
                    self.property_market_manager.process_daily_market(self.run_id, current_day, self.random_seed)

                    # Unified monthly_opex via helper.
                    # Replaces the prior payroll-only sum at this site. Consumers
                    # (acquisition gate, CSF target, CSF top-up) all see
                    # breakdown.total which now includes static + per-unit OpEx
                    # (cumulative-inflated). Recomputed daily to match prior
                    # behavior pattern.
                    opex_breakdown = self.compute_monthly_opex_breakdown(current_day)
                    monthly_opex = opex_breakdown.total

                    # Advance all active acquisition pipelines
                    self.property_acquisition_manager.process_daily_pipelines(current_day, month_count)

                    # Process compliance system (daily)
                    self.compliance_manager.process_daily_compliance(current_day)

                    # Process tenant onboarding (daily)
                    self.tenant_manager.process_daily_tenant_onboarding(current_day)

                    # Process tenure-based rent reductions.
                    # Self-gates to the 1st of the month; runs BEFORE rent
                    # collection so the month bills at the reduced effective rent.
                    reductions_advanced = self.rent_reduction_manager.process_monthly_rent_reductions(current_day)
                    if reductions_advanced:
                        print(f"  {current_day}: Rent reductions advanced for {reductions_advanced} lease(s)")

                    # Process rent collection (daily)
                    rent_collections = self.rent_collection_manager.process_daily_rent_collection(current_day)
                    if rent_collections:
                        total_rent = sum(c.amount_collected for c in rent_collections)
                        on_time_payments = [c for c in rent_collections if c.payment_status == 'ON_TIME']
                        late_payments = [c for c in rent_collections if c.payment_status == 'LATE']

                        if on_time_payments:
                            on_time_total = sum(c.amount_collected for c in on_time_payments)
                            print(f"  {current_day}: ON_TIME - {len(on_time_payments)} leases (${on_time_total:,.2f})")
                        if late_payments:
                            late_total = sum(c.amount_collected for c in late_payments)
                            total_late_fees = sum(c.late_fee_amount for c in late_payments)
                            print(f"  {current_day}: LATE - {len(late_payments)} leases (${late_total:,.2f}, late fees: ${total_late_fees:,.2f})")

                    # Process evictions (check daily for filings and executions)
                    eviction_results = self.eviction_manager.process_evictions(self.run_id, current_day)
                    if eviction_results['filed']:
                        print(f"  {current_day}: Filed evictions for {len(eviction_results['filed'])} leases: {eviction_results['filed']}")
                    if eviction_results['executed']:
                        print(f"  {current_day}: Executed evictions for {len(eviction_results['executed'])} leases: {eviction_results['executed']}")

                    # Process lease renewals and voluntary exits
                    lifecycle_results = self.lease_renewal_manager.process_lease_lifecycle(self.run_id, current_day)
                    if lifecycle_results['early_breaks']:
                        print(f"  {current_day}: Early breaks: {len(lifecycle_results['early_breaks'])} leases")
                    if lifecycle_results['renewals']:
                        print(f"  {current_day}: Renewed: {len(lifecycle_results['renewals'])} leases")
                    if lifecycle_results['voluntary_exits']:
                        print(f"  {current_day}: Voluntary exits: {len(lifecycle_results['voluntary_exits'])} leases")
                    if lifecycle_results['landlord_nonrenewals']:
                        print(f"  {current_day}: Landlord non-renewals: {len(lifecycle_results['landlord_nonrenewals'])} leases")

                    # Process pending deposit settlements
                    settlement_results = self.security_deposit_manager.process_pending_settlements(
                        run_id=self.run_id,
                        current_date=current_day
                    )
                    if settlement_results:
                        for sr in settlement_results:
                            print(f"  {current_day}: Deposit settled LeaseID={sr.lease_id} outcome={sr.outcome} escrowed=${sr.escrowed_amount:.2f}")

                    # Process daily turnover workflow
                    turnover_results = self.turnover_manager.process_daily_turnover(
                        run_id=self.run_id,
                        current_date=current_day
                    )
                    if turnover_results:
                        starts = [t for t in turnover_results if t.event == 'started']
                        completes = [t for t in turnover_results if t.event == 'completed']
                        if starts:
                            print(f"  {current_day}: Turnover work started: {len(starts)} items")
                        if completes:
                            print(f"  {current_day}: Turnover work completed: {len(completes)} items")

                    # Check if we can start a new acquisition pipeline.
                    # KD-042: no monthly_opex passed — the gate pulls a fresh
                    # two-basis snapshot (owned + pro-forma + lump) from
                    # compute_acquisition_gate_bases at gate time.
                    new_attempt_id = self.property_acquisition_manager.check_for_new_opportunities(
                        current_day, month_count, self.config,
                        self.last_csf_committed_target
                    )
                    if new_attempt_id:
                        print(f"  {current_day}: Started new acquisition pipeline (Attempt {new_attempt_id})")

                    # Process annual raises (STAFF.RaiseMonth/RaiseDayOfMonth — seeded Dec 31)
                    if current_day.month == self.config.raise_month and current_day.day == self.config.raise_day_of_month:
                        self.process_annual_raises(current_day)

                    # Process bi-monthly payroll (STAFF.PayrollMidMonthDay — seeded 15th — and last day of month)
                    if day_of_month == self.config.payroll_midmonth_day or current_day == month_end:
                        # Check fund status before payroll
                        balances = self.fund_manager.get_fund_balances(self.run_id, current_day)
                        employees = self.employee_manager.get_active_employees(self.run_id, current_day)

                        # Calculate bi-monthly payroll (half of monthly amount)
                        bi_monthly_payroll = sum((emp.base_salary + emp.benefits_cost) / 24 for emp in employees)

                        # Payroll is a protected obligation. CSF may
                        # backstop Cash if Cash alone is insufficient. SIMULATION
                        # FAILURE now requires Cash + CSF combined to be short.
                        if balances.cash_balance + balances.csf_balance < bi_monthly_payroll:
                            print(f"\nSIMULATION FAILURE - {current_day}")
                            print(f"Cash balance: ${balances.cash_balance:,}")
                            print(f"CSF balance:  ${balances.csf_balance:,}")
                            print(f"Bi-monthly payroll: ${bi_monthly_payroll:,}")
                            print(f"Combined shortfall: ${bi_monthly_payroll - (balances.cash_balance + balances.csf_balance):,}")
                            failed_month = month_count
                            # Capture the death certificate NOW —
                            # post-loop balances differ from the break-time state.
                            halt_notes = (
                                f"HALTED month {month_count}: Cash+CSF "
                                f"${balances.cash_balance + balances.csf_balance:,.2f} < "
                                f"bi-monthly payroll ${bi_monthly_payroll:,.2f}")
                            # Force a final halt-date snapshot
                            try:
                                self.snapshot_manager.capture_final(current_day, source='LIVE')
                            except Exception as snap_err:
                                print(f"  WARNING: halt-date snapshot capture failed: {snap_err}")
                            break

                        # Process bi-monthly payroll (employee_manager now uses
                        # process_expense_protected — CSF backstops Cash if needed)
                        actual_payroll = self.employee_manager.process_bi_monthly_payroll(self.run_id, current_day)

                    # Month-end non-payroll OpEx charge.
                    # Fires AFTER bi-monthly payroll and BEFORE the CSF top-up.
                    # The charge = deterministic buckets
                    # (tax/insurance/utilities/exterior, from the breakdown) +
                    # REALIZED maintenance event draws + turnover make-ready for
                    # turns started this month — all folded into ONE
                    # process_expense_protected call so the itemized model still
                    # produces a single protected obligation with a single
                    # Cash-first/CSF-fallback decision (aggregate-
                    # protected, no per-bucket routing this phase). Payroll is
                    # excluded (bi-monthly path; double-bill guard). SIMULATION
                    # FAILURE if Cash + CSF combined cannot cover the obligation.
                    if current_day == month_end:
                        draw = self.maintenance_event_manager.draw_monthly_events(
                            opex_breakdown.owned_units
                        )
                        routine_realized = Decimal(str(draw.routine_cost))
                        if opex_breakdown.maintenance_techs > 0:
                            # In-house tech absorbs the non-contracted share of
                            # routine work (salary already in payroll).
                            routine_realized *= self.config.maint_tech_reduced_contract_pct
                        major_realized = Decimal(str(draw.major_cost))

                        month_start = month_end.replace(day=1)
                        turns = self.count_turnovers_started(month_start, month_end)
                        turnover_makeready = self.config.turnover_cost_base * turns

                        events_inflated = (
                            (routine_realized + major_realized + turnover_makeready)
                            * opex_breakdown.inflation_factor
                        ).quantize(Decimal('0.01'))
                        nonpayroll_opex = (
                            opex_breakdown.deterministic_buckets + events_inflated
                        ).quantize(Decimal('0.01'))
                        if nonpayroll_opex > 0:
                            opex_result = self.fund_manager.process_expense_protected(
                                run_id=self.run_id,
                                expense_amount=nonpayroll_opex,
                                ledger_date=current_day,
                                expense_type='RECURRING_NONPAYROLL_OPEX',
                            )
                            print(
                                f"  {current_day}: Recurring non-payroll OpEx ${nonpayroll_opex:,.2f} "
                                f"(tax ${opex_breakdown.property_tax:,.2f} + ins ${opex_breakdown.insurance:,.2f} "
                                f"+ util ${opex_breakdown.utilities:,.2f} + ext ${opex_breakdown.exterior:,.2f} "
                                f"+ maint[routine {draw.routine_count}x=${routine_realized:,.2f} "
                                f"major {draw.major_count}x=${major_realized:,.2f} "
                                f"turns {turns}x=${turnover_makeready:,.2f}] @ "
                                f"{opex_breakdown.owned_units}u/{opex_breakdown.owned_properties}p, "
                                f"infl_factor {opex_breakdown.inflation_factor:.4f})"
                            )
                            if not opex_result['fully_covered']:
                                print(f"\nSIMULATION FAILURE - {current_day}")
                                print(f"Recurring non-payroll OpEx: ${nonpayroll_opex:,.2f}")
                                print(
                                    f"Cash paid: ${opex_result['cash_paid']:,.2f}, "
                                    f"CSF drawn: ${opex_result['csf_drawn']:,.2f}"
                                )
                                print("Cash + CSF combined was insufficient for recurring OpEx")
                                failed_month = month_count
                                # Capture the death certificate at THIS
                                # halt site too (hotfix: the first wiring only
                                # covered the payroll halt; this path crashed
                                # every honest OpEx death with UnboundLocalError)
                                halt_notes = (
                                    f"HALTED month {month_count}: recurring OpEx "
                                    f"${nonpayroll_opex:,.2f} not fully covered "
                                    f"(cash ${opex_result['cash_paid']:,.2f} + CSF "
                                    f"${opex_result['csf_drawn']:,.2f})")
                                try:
                                    self.snapshot_manager.capture_final(current_day, source='LIVE')
                                except Exception as snap_err:
                                    print(f"  WARNING: halt-date snapshot capture failed: {snap_err}")
                                break

                    # Month-end CSF top-up (reserve ratchet).
                    # Fires LAST in the day's processing so it sees post-obligation balances.
                    # Day-1 acquisition gate next month sees the post-top-up CSF state.
                    # Reserve months come from the taper curve on the
                    # portfolio's PROPERTY count (same source as the OpEx breakdown) —
                    # the target can now move DOWN as N grows; the latch also
                    # steps down with it (a taper-drop reads as "reserve whole", not
                    # a draw — the excess stays parked, no over-target sweep exists).
                    if current_day == month_end:
                        reserve_months = self.fund_manager.get_reserve_months(
                            opex_breakdown.owned_properties,
                            self.config.csf_reserve_months_peak,
                            self.config.csf_reserve_months_floor,
                            self.config.csf_reserve_n0_properties,
                        )
                        topup_result = self.fund_manager.process_csf_topup(
                            run_id=self.run_id,
                            as_of_date=current_day,
                            monthly_opex=monthly_opex,
                            reserve_months=reserve_months,
                            topup_fraction=self.config.csf_topup_fraction_per_month,
                            cash_floor_months=self.config.cash_floor_months,
                        )
                        # Record the committed reserve target so next month's
                        # acquisition gate can tell a genuine draw (CSF below this)
                        # from the harmless inflation-step lag (reserve-first + latch).
                        self.last_csf_committed_target = topup_result['csf_target']
                        if topup_result['topup_amount'] > 0:
                            print(
                                f"  {current_day}: CSF top-up ${topup_result['topup_amount']:,.2f} "
                                f"(csf ${topup_result['csf_before']:,.2f} -> ${topup_result['csf_after']:,.2f}, "
                                f"target ${topup_result['csf_target']:,.2f})"
                            )

                        # Monthly TCS expiry sweep.
                        # Runs after the CSF top-up so balance/expiry bookkeeping
                        # all settles together on month-end. Forfeits remaining
                        # credit for households whose 24-month post-exit window
                        # has elapsed.
                        if self.tenant_credit_manager is not None:
                            sweep_event_id = self.event_logger.log_event(
                                event_type=EventType.SIMULATION,
                                effective_date=current_day,
                                entity_type=EntityType.RUN,
                                entity_id=self.run_id,
                                action_type=ActionType.TENANT_CREDIT,
                                metadata=f"TENANT_CREDIT/EXPIRY_SWEEP: Monthly sweep ran on {current_day}",
                            )
                            expiry_results = self.tenant_credit_manager.process_credit_expiry_sweep(
                                sweep_date=current_day,
                                sweep_event_id=sweep_event_id,
                            )
                            if expiry_results:
                                n_forfeited = len(expiry_results)
                                total_forfeited = sum(
                                    (r.forfeited_amount for r in expiry_results),
                                    Decimal("0"),
                                )
                                print(
                                    f"  {current_day}: TCS expiry sweep — {n_forfeited} households, "
                                    f"${total_forfeited:,.2f} forfeited"
                                )

                    # Capture end-of-day snapshot at cadence boundaries (after daily events)
                    if self.snapshot_manager.should_capture(current_day):
                        self.snapshot_manager.capture(current_day, source='LIVE')

                    # Move to next day
                    current_day += timedelta(days=1)

                # Break if failed during daily loop
                if failed_month:
                    break

                # Progress reporting (every 12 months)
                if month_count % 12 == 0:
                    year = month_count // 12
                    updated_balances = self.fund_manager.get_fund_balances(self.run_id, month_end)
                    print(f"Year {year}: Cash=${updated_balances.cash_balance:,}, CSF=${updated_balances.csf_balance:,}")

                # Move to next month
                current_month += relativedelta(months=1)

            # Simulation complete
            if failed_month:
                print(f"\nSimulation ended in failure at month {failed_month}")
                # Honest death certificate — HALTED + break-time
                # shortfall on the Run row (was: "just mark as completed").
                if halt_notes is None:  # any future halt site that forgets to set it
                    halt_notes = f"HALTED month {failed_month}: protected obligation unmet (details not captured)"
                self.run_manager.halt_run(self.run_id, halt_notes)
            else:
                print(f"\nSimulation completed successfully! Processed {month_count} months")
                self.run_manager.complete_run(self.run_id)

            # Final summary (use last processed month-end date)
            final_date = current_month - relativedelta(months=1) if failed_month else month_end
            final_balances = self.fund_manager.get_fund_balances(self.run_id, final_date)
            final_employees = self.employee_manager.get_active_employees(self.run_id, final_date)
            final_annual_payroll = sum(emp.base_salary + emp.benefits_cost for emp in final_employees)

            print(f"\n=== FINAL SUMMARY ===")
            print(f"Months processed: {month_count}")
            print(f"Final date: {final_date}")
            print(f"Final cash: ${final_balances.cash_balance:,}")
            print(f"Final CSF: ${final_balances.csf_balance:,}")
            print(f"Final total funds: ${final_balances.total_balance:,}")
            print(f"Employees: {len(final_employees)}")
            print(f"Final annual payroll: ${final_annual_payroll:,}")
            print(f"Cash burned: ${self.config.starting_funds - final_balances.total_balance:,}")

            return True

        except Exception as e:
            error_msg = f"Simulation failed: {e}"
            self.logger.error(error_msg)
            print_error_box(error_msg)  # Visual error separator
            import traceback
            traceback.print_exc()
            return False


def main():
    """Main entry point with argument parsing"""
    parser = argparse.ArgumentParser(
        description="Liberty Bee Housing Simulation - End-to-End Simulation Engine"
    )
    parser.add_argument(
        "--env",
        type=str,
        help="Environment to use"
    )
    parser.add_argument(
        "--months",
        type=int,
        default=None,
        help="Override simulation duration in months (default: use projection end_date, typically 240 months)"
    )
    parser.add_argument(
        "--projection-id",
        type=int,
        default=206,
        help="Projection scenario ID to use (default: 206, the $8M released baseline)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=12345,
        help="Random seed for reproducibility (default: 12345)"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of simulation runs to execute (default: 1)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help="Write all output to debug log file in environments/<env>/debug_<timestamp>.log"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Trace every project function call and DB operation to scratch/debug_logs/ (JSONL)"
    )

    args = parser.parse_args()

    if not args.env:
        print(f"No environment supplied")
        sys.exit()
    env_dir = os.path.dirname(APP_DIR) + "\\environments\\" + args.env  # Go up one level to app directory
    configfile = env_dir + "\\db_config.json"

    # Set up debug logging if requested
    tee_output = None
    if args.debug_log:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_log_path = os.path.join(env_dir, f"debug_{timestamp}.log")
        tee_output = TeeOutput(debug_log_path)
        sys.stdout = tee_output
        print(f"[debug] Debug logging enabled: {debug_log_path}")
    
    # Run from app/src — the single source of truth (the --scratch shadow was retired)
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    print(f"[app] using src dir {APP_DIR}")

    # NOW import application modules (after sys.path is set up)
    # Use global to make these available to the Simulation class defined at module level
    global DatabaseManager, FundManager, RunManager, ConfigurationLoader
    global InflationEngine, EmployeeManager, EventLogger
    global PropertyMarketManager, PropertyAcquisitionManager, ComplianceManager, TenantManager, RentCollectionManager, EvictionManager, LeaseRenewalManager
    global DebugTracer

    from database_manager import DatabaseManager
    from fund_manager import FundManager
    from run_manager import RunManager
    from configuration_loader import ConfigurationLoader
    from inflation_engine import InflationEngine
    from employee_manager import EmployeeManager
    from event_logger import EventLogger
    from property_market_manager import PropertyMarketManager
    from property_acquisition_manager import PropertyAcquisitionManager
    from compliance_manager import ComplianceManager
    from tenant_manager import TenantManager
    from rent_collection_manager import RentCollectionManager
    from eviction_manager import EvictionManager
    from lease_renewal_manager import LeaseRenewalManager
    from debug_tracer import DebugTracer

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Set up debug tracer if requested
    tracer = None
    if args.debug:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        project_root = Path(PROJECT_ROOT)
        log_dir = project_root / "scratch" / "debug_logs"
        log_path = log_dir / f"debug_sim_{args.env}_{timestamp}_seed{args.seed}.jsonl"
        tracer = DebugTracer(output_path=log_path, project_root=project_root)
        tracer.start()

    # Run iterations
    results = []
    for iteration in range(1, args.iterations + 1):
        print(f"\n{'=' * 80}")
        if args.iterations > 1:
            print(f"ITERATION {iteration} of {args.iterations}")
            print("=" * 80)

        # Use different seed for each iteration
        iteration_seed = args.seed + (iteration - 1) if args.iterations > 1 else args.seed

        simulation = Simulation(
            env = args.env,
            configfile = configfile,
            projection_id=args.projection_id,
            random_seed=iteration_seed,
            months=args.months
        )
        if tracer:
            simulation.db.set_debug_tracer(tracer)
        success = simulation.run_simulation()
        results.append({
            'iteration': iteration,
            'seed': iteration_seed,
            'success': success,
            'run_id': simulation.run_id
        })

        if not success:
            print(f"\nERROR Iteration {iteration} failed!")
        else:
            print(f"\nOK Iteration {iteration} completed successfully! Run ID: {simulation.run_id}")

    # Summary for multiple iterations
    if args.iterations > 1:
        print(f"\n{'=' * 80}")
        print("MULTI-ITERATION SUMMARY")
        print("=" * 80)
        successful = sum(1 for r in results if r['success'])
        print(f"Total iterations: {args.iterations}")
        print(f"Successful: {successful}")
        print(f"Failed: {args.iterations - successful}")
        print(f"\nRun IDs:")
        for r in results:
            status = "OK" if r['success'] else "FAIL"
            print(f"  Iteration {r['iteration']} (seed {r['seed']}): Run {r['run_id']} [{status}]")

    if tracer:
        tracer.stop()

    # Close debug log file if opened
    if tee_output:
        print(f"[debug] Debug log closed")  # Print before restoring stdout
        sys.stdout = tee_output.terminal  # Restore original stdout
        tee_output.close()

    # Exit code based on overall success
    all_successful = all(r['success'] for r in results)
    sys.exit(0 if all_successful else 1)


if __name__ == "__main__":
    main()