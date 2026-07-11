"""
Tenant Management Module for Liberty Bee Housing Simulation

Purpose: Manages tenant onboarding, vacancy tracking, lease creation, and household lifecycle.

Key Responsibilities:
- Track vacant units and create vacancy records
- Generate and evaluate applicant candidates
- Select qualified tenants and create leases
- Materialize household and person records on selection
- Maintain applicant evaluation ledger (rejection tracking)

Design Principles:
- No IDENTITY columns - manual ID assignment for run isolation
- Compound primary keys (RunID, EntityID)
- Event logging integration for all entity creation
- Infinite applicant pool abstraction (programmatic generation, no bounded population)
- Materialize-on-selection (only create Household/Person for selected tenants)
- Behavioral determinism (same seed → same outcomes)

Scope:
- Vacancy detection and tracking
- Applicant generation and evaluation
- Lease creation (12-month fixed terms)
- Household/Person materialization
- NO rent collection, proration, deposits, renewals, or moves
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any
import random

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from event_logger import EventLogger, EventType, EntityType, ActionType
from security_deposit_manager import SecurityDepositManager
from income_model import IncomeModel


@dataclass
class TenantParameters:
    """Configuration parameters for tenant placement logic"""
    candidate_slate_size: int
    max_rent_to_income_ratio: Decimal
    prescreen_rent_to_income_ratio: Decimal  # Pre-screen self-selection threshold
    standard_lease_duration_months: int

    # Bedroom-fit occupancy caps: "2 per bedroom + 1" industry standard,
    # registry-driven (QUAL.*, fail-loud). Studio has its own cap. Age-aware
    # revisit (infants uncounted; couple + child in a studio).
    max_occupants_per_bedroom: int
    max_occupants_bonus: int
    max_occupants_studio: int


@dataclass
class VacancyRecord:
    """Represents a vacant unit available for lease"""
    vacancy_id: int
    unit_id: int
    property_id: int
    vacancy_start_date: date
    bedrooms: int
    vacancy_created_event_id: int
    target_fill_date: Optional[date] = None  # hard fill-date gate


@dataclass
class ApplicantCandidate:
    """Generated applicant candidate (not yet materialized)"""
    applicant_index: int  # 0-based index in candidate slate
    household_type: str
    household_income_band: str
    adult_count: int
    child_count: int
    estimated_monthly_income: Decimal  # For qualification check


@dataclass
class HouseholdRecord:
    """Materialized household (created on lease selection)"""
    household_id: int
    household_type: str
    household_income_band: str
    adult_count: int
    child_count: int
    created_date: date
    created_event_id: int


@dataclass
class PersonRecord:
    """Materialized person within household"""
    person_id: int
    household_id: int
    first_name: str
    last_name: str
    date_of_birth: date
    is_adult: bool
    created_date: date
    created_event_id: int


@dataclass
class LeaseRecord:
    """Materialized lease agreement"""
    lease_id: int
    household_id: int
    unit_id: int
    vacancy_id: int
    lease_signed_date: date
    lease_start_date: date
    lease_end_date: date
    monthly_rent: Decimal
    lease_status: str
    signed_event_id: int


@dataclass
class VacancyEvaluationResult:
    """Result of evaluating candidate slate for a vacancy"""
    vacancy_id: int
    selected_candidate: Optional[ApplicantCandidate]  # None if no qualified candidates
    rejection_count: int  # Number of rejected candidates
    evaluation_ids: List[int]  # EvaluationIDs written to ApplicantEvaluation table


class TenantManager:
    """
    Manages tenant placement, vacancy tracking, and lease creation.

    Implementation:
    - Vacancy detection and tracking
    - Applicant generation and evaluation (infinite pool)
    - First-qualified selection logic
    - Household/Person materialization on selection
    - Lease creation (12-month fixed terms)
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_logger: EventLogger,
        run_id: int,
        run_seed: int,
        below_market_rent_pct: float,
        deposit_manager: Optional[SecurityDepositManager] = None,
        tenant_credit_manager: Optional["TenantCreditManager"] = None,
    ):
        """
        Initialize TenantManager

        Args:
            db_manager: Database manager instance
            event_logger: Event logger instance
            run_id: Current run ID
            run_seed: RNG seed for deterministic behavior
            below_market_rent_pct: Liberty Bee below-market discount, from
                ProjectionConfig.below_market_rent_pct (e.g. 0.10).
                New leases sign at BaseRent x (1 - below_market_rent_pct) x inflation.
            deposit_manager: Optional SecurityDepositManager
            tenant_credit_manager: Optional TenantCreditManager (TCS init hook)
        """
        self.db = db_manager
        self.event_logger = event_logger
        self.run_id = run_id
        self.run_seed = run_seed
        # below-market discount applied to new lease pricing. The
        # projection parameter owns this value — no hardcoded default.
        # Missing/invalid configuration fails loudly here.
        if below_market_rent_pct is None:
            raise ValueError(
                "TenantManager requires below_market_rent_pct from ProjectionConfig "
                "(no default — the projection parameter owns this value)"
            )
        self.below_market_rent_pct = Decimal(str(below_market_rent_pct))
        if not (Decimal('0') <= self.below_market_rent_pct < Decimal('1')):
            raise ValueError(
                f"below_market_rent_pct must be in [0, 1); got {self.below_market_rent_pct}"
            )
        self.deposit_manager = deposit_manager
        self.tenant_credit_manager = tenant_credit_manager  # TCS balance init hook

        # shared RNG for vacancy-fill duration. Deterministic across
        # the run because vacancy creation processes units in ORDER BY PropertyID, UnitID.
        # Offset 30708 chosen to avoid collision with 30707 (turnover) and
        # 30602 (deposit) seeded RNGs.
        self.vacancy_rng = random.Random(run_seed + 30708)

        # dedicated RNG for TCS redemption-probability assignment at
        # household onboarding. Offset 30403 (onboarding lane).
        # Consumed once per household materialization in the order this module
        # already materializes them — no separate ORDER BY needed at this site.
        self.tcs_onboarding_rng = random.Random(run_seed + 30403)

        # ID counters (manual assignment, no IDENTITY)
        self.next_household_id = 1
        self.next_person_id = 1
        self.next_lease_id = 1
        self.next_vacancy_id = 1
        self.next_evaluation_id = 1

        # Parameters (loaded from reference tables)
        self.params: Optional[TenantParameters] = None
        self.first_names: List[str] = []
        self.last_names: List[str] = []

        # unified parameter registry (globals only — tenant params
        # carry no per-projection overrides). Read-once, cached, fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()

        # renter-calibrated, time-varying, sum-of-earners
        # income model (INC.* registry params; wage growth off the SHARED
        # regime schedule). Replaces the retired static reference.IncomeBand
        # draw — the frozen-income artifact.
        self.income_model = IncomeModel(self.db, self.registry, self.run_id)

        # vacancy-duration distribution params (PROP.*, global)
        # — were the hardcoded expovariate(1/30) mean capped at 120 days in
        # _record_vacancy. Read-once, fail-loud.
        self.vacancy_mean_days = self.registry.get_int('PROP', 'VacancyMeanDays')
        self.vacancy_max_days = self.registry.get_int('PROP', 'VacancyMaxDays')

        # synthetic person ages + household composition
        # weights are TNT registry knobs, seeded with the exact former literals.
        self.adult_age_min_years = self.registry.get_int('TNT', 'AdultAgeMinYears')
        self.adult_age_max_years = self.registry.get_int('TNT', 'AdultAgeMaxYears')
        self.child_age_min_years = self.registry.get_int('TNT', 'ChildAgeMinYears')
        self.child_age_max_years = self.registry.get_int('TNT', 'ChildAgeMaxYears')
        self.family_children_weights = [
            self.registry.get_float('TNT', 'FamilyChildren0Weight'),
            self.registry.get_float('TNT', 'FamilyChildren1Weight'),
            self.registry.get_float('TNT', 'FamilyChildren2Weight'),
            self.registry.get_float('TNT', 'FamilyChildren3Weight'),
        ]
        self.roommates_adults_weights = [
            self.registry.get_float('TNT', 'RoommatesAdults2Weight'),
            self.registry.get_float('TNT', 'RoommatesAdults3Weight'),
            self.registry.get_float('TNT', 'RoommatesAdults4Weight'),
        ]

        # Salem-calibrated vacancy base + annual fluctuation
        # (PROP.VacancyRateBase / VacancyFluctuationBand, V00048). Each sim
        # calendar year draws rate_y = Base + U(-band, +band) from a dedicated
        # stream (offset 30920); fill-duration means scale by rate_y / Base so
        # realized vacancy fluctuates gently around the steady-state base.
        # Small-fluctuation realism only — through-cycle vacancy STRESS is
        # the regime schedule's job (do NOT widen this band to fake a soft
        # market). Years draw lazily in chronological order => seed-stable.
        self.vacancy_rate_base = self.registry.get_float('PROP', 'VacancyRateBase')
        self.vacancy_fluctuation_band = self.registry.get_float('PROP', 'VacancyFluctuationBand')
        if not (0 < self.vacancy_rate_base and
                0 <= self.vacancy_fluctuation_band < self.vacancy_rate_base):
            raise ValueError(
                f"PROP.VacancyFluctuationBand ({self.vacancy_fluctuation_band}) must be "
                f">= 0 and < PROP.VacancyRateBase ({self.vacancy_rate_base}) > 0 — a "
                "band reaching the base would make the yearly rate (and the scaled "
                "fill-duration mean) non-positive."
            )
        self.vacancy_fluct_rng = random.Random(run_seed + 30920)
        self._vacancy_rate_by_year: dict = {}
        # regime vacancy pressure — InflationSchedule.VacancyDelta (a
        # LEVEL delta on the yearly rate while a regime is active; downturns
        # +3-4pt, surges slightly negative). Lazy-loaded once (the schedule is
        # static post-init).
        self._vacancy_delta_schedule: Optional[list] = None

        # Load parameters on initialization
        self._load_parameters()

    def _load_parameters(self) -> None:
        """Load tenant parameters from reference tables"""

        # read TenantParameters from the unified registry (fail-loud,
        # no .get() fallbacks). MinRentToIncomeRatio (QUAL) and
        # VacancyBufferDays (TIMING) are absent from any source store; they
        # are explicit injected registry rows at their prior effective values —
        # the underlying defects are addressed later.
        reg = self.registry
        self.params = TenantParameters(
            candidate_slate_size=reg.get_int('TNT', 'CandidateSlateSize'),
            max_rent_to_income_ratio=reg.get_decimal('QUAL', 'MaxRentToIncomeRatio'),
            prescreen_rent_to_income_ratio=reg.get_decimal('QUAL', 'PreScreenRentToIncomeRatio'),
            standard_lease_duration_months=reg.get_int('LEASE', 'StandardLeaseDurationMonths'),
            # Bedroom-fit occupancy caps (parameterized 2+1; QUAL globals, fail-loud)
            max_occupants_per_bedroom=reg.get_int('QUAL', 'MaxOccupantsPerBedroom'),
            max_occupants_bonus=reg.get_int('QUAL', 'MaxOccupantsBonus'),
            max_occupants_studio=reg.get_int('QUAL', 'MaxOccupantsStudio'),
        )

        # Load FirstNames (rows are tuples: (FirstName,))
        first_name_query = "SELECT FirstName FROM reference.FirstName ORDER BY FirstName"
        first_name_rows = self.db.execute_query(first_name_query)
        self.first_names = [row[0] for row in first_name_rows]

        # Load LastNames (rows are tuples: (LastName,))
        last_name_query = "SELECT LastName FROM reference.LastName ORDER BY LastName"
        last_name_rows = self.db.execute_query(last_name_query)
        self.last_names = [row[0] for row in last_name_rows]

    def get_next_household_id(self) -> int:
        """Get next HouseholdID and increment counter"""
        household_id = self.next_household_id
        self.next_household_id += 1
        return household_id

    def get_next_person_id(self) -> int:
        """Get next PersonID and increment counter"""
        person_id = self.next_person_id
        self.next_person_id += 1
        return person_id

    def get_next_lease_id(self) -> int:
        """Get next LeaseID and increment counter"""
        lease_id = self.next_lease_id
        self.next_lease_id += 1
        return lease_id

    def get_next_vacancy_id(self) -> int:
        """Get next VacancyID and increment counter"""
        vacancy_id = self.next_vacancy_id
        self.next_vacancy_id += 1
        return vacancy_id

    def get_next_evaluation_id(self) -> int:
        """Get next EvaluationID and increment counter"""
        evaluation_id = self.next_evaluation_id
        self.next_evaluation_id += 1
        return evaluation_id

    # =========================================================================
    # Vacancy Detection and Tracking
    # =========================================================================

    def process_daily_tenant_onboarding(self, current_date: date) -> None:
        """
        Daily tenant onboarding processing

        Args:
            current_date: Current simulation date

        Steps:
        1. Detect units that became rentable today (ComplianceAttempt.UnitReadyDate)
        2. Create Vacancy records for new rentable units
        3. Get all open vacancies
        4. Attempt to fill vacancies (generate candidates, evaluate, create lease)
        """
        # Step 1: Create vacancy records for units that became rentable TODAY
        self._check_and_create_new_vacancies(current_date)

        # Step 2: Get all open vacancies
        open_vacancies = self._get_open_vacancies()

        # Step 3: Attempt to fill each vacancy
        for vacancy_record in open_vacancies:
            self._attempt_to_fill_vacancy(vacancy_record, current_date)

    def _attempt_to_fill_vacancy(self, vacancy_record: VacancyRecord, current_date: date) -> None:
        """
        Attempt to fill a vacancy with a qualified tenant.

        Args:
            vacancy_record: VacancyRecord with unit details
            current_date: Current simulation date

        Logic:
        0. hard gate: skip if current_date < TargetFillDate
        1. Generate candidate slate
        2. Evaluate candidates and select first qualified
        3. If selected, create lease and materialize household/persons
        4. If no qualified candidates, vacancy remains unfilled (retry next day)

        Note: this function processes ONE vacancy per call. The caller
        iterates `for v in open_vacancies: self._attempt_to_fill_vacancy(v, ...)`. An
        early `return` here only skips the current vacancy, not subsequent ones.
        """
        # hard fill-date gate: not eligible before TargetFillDate
        if vacancy_record.target_fill_date is not None and current_date < vacancy_record.target_fill_date:
            return

        # Step 1: Generate candidate slate
        # slate is deterministic per (vacancy, current_date).
        # Pre-fix, the seed lacked a date component, so each daily retry
        # regenerated the identical slate and infinite-rejected the same
        # applicants.
        candidate_slate = self._generate_candidate_slate(vacancy_record.vacancy_id, current_date)

        # the qualification rent IS the signing rent —
        # computed ONCE per fill attempt at current_date (= the signing date;
        # screens and signing run synchronously in this call) and reused by
        # both screens and the lease insert, so screen-rent == signed-rent is
        # an identity by construction. Pre-fix, screens used the stale month-0
        # PropertyUnits.CurrentRent (~45% under the real rent by year 20).
        qualification_rent = self._compute_inflation_adjusted_rent(
            vacancy_record.unit_id, current_date)

        # Step 2: Evaluate candidates
        evaluation_result = self._evaluate_candidates(
            vacancy_record, candidate_slate, current_date, qualification_rent)

        # Step 3: If a candidate was selected, create lease
        if evaluation_result.selected_candidate:
            self.create_lease(
                vacancy_record,
                evaluation_result.selected_candidate,
                current_date,
                monthly_rent=qualification_rent
            )

    def _check_and_create_new_vacancies(self, current_date: date) -> None:
        """
        Check for available units that need Vacancy records (unified detection).

        Args:
            current_date: Current simulation date

        Logic (unified state-based detection):
        - Find units where UnitStatus = 'Available'
        - Exclude units that already have open vacancies
        - Exclude units that have active leases
        - ORDER BY PropertyID, UnitID for deterministic RNG ordering
        - Create Vacancy record and log VACANCY_CREATED event

        This single path handles both initial lease-up (unit transitions
        Compliance_In_Progress -> Available via compliance_manager) and post-turnover
        re-leasing (turnover_manager transitions Turnover -> Available).
        UnitStatus state machine is maintained by transitions in
        property_acquisition_manager, compliance_manager, and tenant_manager.
        """
        query = """
            SELECT u.UnitID, u.PropertyID
            FROM simulation.PropertyUnits u
            WHERE u.RunID = ?
              AND u.UnitStatus = 'Available'
              AND NOT EXISTS (
                  SELECT 1 FROM simulation.Vacancy v
                  WHERE v.RunID = u.RunID
                    AND v.UnitID = u.UnitID
                    AND v.VacancyEndDate IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM simulation.Lease l
                  WHERE l.RunID = u.RunID
                    AND l.UnitID = u.UnitID
                    AND l.LeaseStatus = 'ACTIVE'
              )
            ORDER BY u.PropertyID, u.UnitID
        """
        new_vacancies = self.db.execute_query(query, (self.run_id,))

        for row in new_vacancies:
            unit_id = row[0]
            property_id = row[1]
            self._create_vacancy_record(unit_id, property_id, current_date)

    def _create_vacancy_record(self, unit_id: int, property_id: int, current_date: date) -> Optional[int]:
        """
        Create Vacancy record with seeded TargetFillDate and log VACANCY_CREATED event.

        Args:
            unit_id: UnitID from PropertyUnits
            property_id: PropertyID from PropertyUnits (for event metadata)
            current_date: Vacancy start date

        Returns:
            VacancyID of created vacancy, or None if idempotency guard skipped creation

        - Vacancy duration: max(1, min(int(rng.expovariate(1/30)), 120))
        - TargetFillDate = current_date + vacancy_days
        - Idempotency guard: skip if an open Vacancy exists for (RunID, UnitID)
        - Event metadata includes source_phase: "3.7.8"
        """
        from event_logger import EventType, EntityType, ActionType

        # Idempotency guard — race-safe double-check at INSERT time
        idempotency_check = """
            SELECT VacancyID
            FROM simulation.Vacancy
            WHERE RunID = ?
              AND UnitID = ?
              AND VacancyEndDate IS NULL
        """
        existing = self.db.execute_query(idempotency_check, (self.run_id, unit_id))
        if existing:
            return None

        # seeded truncated-exponential vacancy duration.
        # The mean scales with this year's drawn vacancy rate (base +
        # fluctuation). Plus the active inflation regime's VacancyDelta
        # (downturn +3-4pt pushes fill times out; the [1, max_days] clamp
        # bounds the scaling; the 0.0001 floor is a numerical guard on the
        # expovariate mean, not a market knob).
        effective_rate = max(
            0.0001,
            self._vacancy_rate_for_year(current_date.year)
            + self._current_vacancy_delta(current_date),
        )
        effective_mean_days = self.vacancy_mean_days * (effective_rate / self.vacancy_rate_base)
        vacancy_days = max(1, min(int(self.vacancy_rng.expovariate(1.0 / effective_mean_days)), self.vacancy_max_days))
        target_fill_date = current_date + timedelta(days=vacancy_days)

        vacancy_id = self.get_next_vacancy_id()

        # Log VACANCY_CREATED event with source_phase metadata
        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.UNIT,
            entity_id=unit_id,
            action_type=ActionType.CREATE,
            metadata=(
                f"VACANCY_CREATED: UnitID={unit_id}, PropertyID={property_id}, "
                f"TargetFillDate={target_fill_date.isoformat()}, VacancyDays={vacancy_days}, "
                f"source_phase=3.7.8, event_meaning=vacancy_record_created"
            )
        )

        # Insert vacancy record with TargetFillDate
        insert_query = """
            INSERT INTO simulation.Vacancy
            (RunID, VacancyID, UnitID, VacancyStartDate, VacancyCreatedEventID, TargetFillDate)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (self.run_id, vacancy_id, unit_id, current_date, event_id, target_fill_date)
        )

        return vacancy_id

    def _vacancy_rate_for_year(self, year: int) -> float:
        """this calendar year's vacancy rate — Base + U(-band, +band),
        drawn once per year from the dedicated 30920 stream and cached. The
        sim advances chronologically, so years are first seen in order and
        the draw sequence is seed-reproducible (invariant #8)."""
        if year not in self._vacancy_rate_by_year:
            self._vacancy_rate_by_year[year] = self.vacancy_rate_base + \
                self.vacancy_fluct_rng.uniform(-self.vacancy_fluctuation_band,
                                               self.vacancy_fluctuation_band)
        return self._vacancy_rate_by_year[year]

    def _current_vacancy_delta(self, current_date: date) -> float:
        """the active regime's vacancy LEVEL delta — the latest
        InflationSchedule row at or before current_date. Schedule is generated
        once pre-sim, so it's cached whole on first call (deterministic; no
        RNG). Static-mode runs carry all-zero deltas."""
        if self._vacancy_delta_schedule is None:
            rows = self.db.execute_query(
                "SELECT InflationDate, VacancyDelta FROM simulation.InflationSchedule "
                "WHERE RunID = ? ORDER BY InflationDate",
                (self.run_id,),
            )
            self._vacancy_delta_schedule = [(r[0], float(r[1])) for r in rows]
        delta = 0.0
        for infl_date, d in self._vacancy_delta_schedule:
            if infl_date <= current_date:
                delta = d
            else:
                break
        return delta

    def _get_open_vacancies(self) -> List[VacancyRecord]:
        """
        Get all vacancies that are still open (VacancyEndDate IS NULL).

        also returns TargetFillDate for the hard-gate check in
        _attempt_to_fill_vacancy. NULL TargetFillDate (legacy rows) means no gate.

        Returns:
            List of VacancyRecord objects with unit details
        """
        query = """
            SELECT v.VacancyID, v.UnitID, u.PropertyID, v.VacancyStartDate,
                   u.Beds, v.VacancyCreatedEventID, v.TargetFillDate
            FROM simulation.Vacancy v
            JOIN simulation.PropertyUnits u
              ON v.RunID = u.RunID AND v.UnitID = u.UnitID
            WHERE v.RunID = ?
              AND v.VacancyEndDate IS NULL
        """
        rows = self.db.execute_query(query, (self.run_id,))

        vacancy_records = []
        for row in rows:
            vacancy_records.append(VacancyRecord(
                vacancy_id=row[0],
                unit_id=row[1],
                property_id=row[2],
                vacancy_start_date=row[3],
                bedrooms=int(row[4]),  # Convert from decimal
                vacancy_created_event_id=row[5],
                target_fill_date=row[6]
            ))

        return vacancy_records

    # =========================================================================
    # Applicant Generation
    # =========================================================================

    def _get_run_seed(self) -> int:
        """Get RandomSeed for current run from simulation.Run table"""
        query = "SELECT RandomSeed FROM simulation.Run WHERE RunID = ?"
        result = self.db.execute_scalar(query, (self.run_id,))
        return result if result is not None else 0

    def _generate_candidate_slate(self, vacancy_id: int, current_date: date) -> List[ApplicantCandidate]:
        """
        Generate candidate slate for vacancy using deterministic RNG

        Args:
            vacancy_id: VacancyID to generate candidates for
            current_date: Fill-attempt date (seeds applicant RNG
                so that the same vacancy retried on different days produces a
                different deterministic slate; same vacancy + same date still
                produces the same slate)

        Returns:
            List of ApplicantCandidate objects

        Logic:
        - Generate N candidates (where N = CandidateSlateSize parameter)
        - Each candidate has deterministic attributes per (run seed, vacancy_id, current_date, applicant_index)
        - Household type sampled from weighted distribution
        - Income band sampled from weighted distribution
        - Adult/child counts determined by household type logic
        """
        slate_size = self.params.candidate_slate_size
        candidates = []

        for applicant_index in range(slate_size):
            candidate = self._generate_applicant(vacancy_id, applicant_index, current_date)
            candidates.append(candidate)

        return candidates

    def _generate_applicant(self, vacancy_id: int, applicant_index: int, current_date: date) -> ApplicantCandidate:
        """
        Generate single applicant with deterministic attributes

        Args:
            vacancy_id: VacancyID for RNG seeding
            applicant_index: Index in candidate slate (0-based)
            current_date: Fill-attempt date — widens seed key so daily retries
                of the same vacancy do not regenerate the identical slate

        Returns:
            ApplicantCandidate object with household type, income band, composition

        Determinism:
        - RNG seed: f"{run_seed}_vacancy_{vacancy_id}_date_{current_date.toordinal()}_applicant_{applicant_index}"
        - Same (run_seed, vacancy_id, date, applicant_index) → same household characteristics
        - Different date → new deterministic slate for the same vacancy
        - Engine remains fully seeded and reproducible
        """
        # Stable RNG seed for this specific applicant.
        # includes current_date.toordinal() so daily retries
        # of an open vacancy draw fresh deterministic applicants instead of
        # re-evaluating the same fixed slate forever.
        run_seed = self._get_run_seed()
        seed_string = (
            f"{run_seed}_vacancy_{vacancy_id}"
            f"_date_{current_date.toordinal()}"
            f"_applicant_{applicant_index}"
        )
        applicant_rng = random.Random(seed_string)

        # weights from the registry (TNT, global), read-once + cached
        # in self.registry — replaces 8 per-applicant scalar DB queries (fail-loud).
        household_type_weights = [
            self.registry.get_float('TNT', weight_name)
            for weight_name in [
                'HouseholdTypeSingleWeight',
                'HouseholdTypeCoupleWeight',
                'HouseholdTypeFamilyWeight',
                'HouseholdTypeRoommatesWeight'
            ]
        ]

        # Generate household type (weighted sample)
        household_type = applicant_rng.choices(
            ['SINGLE', 'COUPLE', 'FAMILY', 'ROOMMATES'],
            weights=household_type_weights
        )[0]

        # Generate household composition based on type
        composition = self._generate_household_composition(household_type, applicant_rng)

        # pooled household income = SUM of
        # independently-drawn earners (count from the composition just drawn),
        # from the renter-calibrated %-of-median bands, in CURRENT-date dollars
        # (wage growth off the shared regime schedule). Replaces the frozen
        # static-band draw. The returned band label is the PRIMARY earner's
        # band (B1..B5) — telemetry, not a qualification input.
        estimated_income, income_band = self.income_model.generate_household_income(
            household_type=household_type,
            adult_count=composition['adults'],
            rng=applicant_rng,
            target_date=current_date,
        )

        return ApplicantCandidate(
            applicant_index=applicant_index,
            household_type=household_type,
            household_income_band=income_band,
            adult_count=composition['adults'],
            child_count=composition['children'],
            estimated_monthly_income=estimated_income
        )

    def _generate_household_composition(self, household_type: str, rng: random.Random) -> Dict[str, int]:
        """
        Generate adult/child counts based on household type

        Args:
            household_type: SINGLE, COUPLE, FAMILY, or ROOMMATES
            rng: Random number generator (seeded)

        Returns:
            Dict with 'adults' and 'children' counts

        Logic:
        - SINGLE: 1 adult, 0 children
        - COUPLE: 2 adults, 0 children
        - FAMILY: 2 adults, 0-3 children (weighted: [0.1, 0.3, 0.4, 0.2])
        - ROOMMATES: 2-4 adults (weighted: [0.5, 0.3, 0.2]), 0 children
        """
        if household_type == 'SINGLE':
            return {'adults': 1, 'children': 0}
        elif household_type == 'COUPLE':
            return {'adults': 2, 'children': 0}
        elif household_type == 'FAMILY':
            adults = 2
            # Families have 0-3 children, weighted by the TNT.FamilyChildren*Weight knobs
            children = rng.choices([0, 1, 2, 3], weights=self.family_children_weights)[0]
            return {'adults': adults, 'children': children}
        elif household_type == 'ROOMMATES':
            # Roommates: 2-4 adults (TNT.RoommatesAdults*Weight knobs), no children
            adults = rng.choices([2, 3, 4], weights=self.roommates_adults_weights)[0]
            return {'adults': adults, 'children': 0}
        else:
            raise ValueError(f"Unknown household type: {household_type}")

    # (_estimate_monthly_income retired with reference.IncomeBand —
    # income generation lives in income_model.IncomeModel.)

    # =========================================================================
    # Candidate Evaluation and Selection
    # =========================================================================

    def _check_prescreen_affordability(self, candidate: ApplicantCandidate, monthly_rent: Decimal) -> bool:
        """
        Check if household would self-select to apply based on affordability

        Args:
            candidate: ApplicantCandidate with estimated monthly income
            monthly_rent: Monthly rent for the unit

        Returns:
            True if household would apply (rent affordable enough to try)
            False if household would self-screen out (rent obviously too high OR invalid income data)

        Pre-Screen Rule:
        - Households optimistically apply if rent ≤ prescreen_ratio of income
        - prescreen_ratio (default 40%) reflects human optimism and imperfect information
        - This is NOT the qualification threshold (that remains 30%)
        - This models behavioral self-selection, not policy enforcement

        Edge Cases:
        - If income is NULL/0/missing → returns False (household self-screens out)
        - This prevents division-by-zero crashes
        """
        # SAFETY: Guard against missing or zero income
        if candidate.estimated_monthly_income is None or candidate.estimated_monthly_income <= 0:
            # Invalid income data - treat as self-screened
            # (Household cannot demonstrate affordability)
            return False

        # Calculate rent-to-income ratio (safe - denominator guaranteed > 0)
        rent_to_income_ratio = monthly_rent / candidate.estimated_monthly_income

        # Household applies if ratio at or below threshold
        return rent_to_income_ratio <= self.params.prescreen_rent_to_income_ratio

    def _check_bedroom_fit(self, candidate: ApplicantCandidate, bedrooms: int) -> bool:
        """
        Check if candidate's household size fits unit bedroom count

        Args:
            candidate: ApplicantCandidate with household composition
            bedrooms: Number of bedrooms in unit

        Returns:
            True if candidate fits bedroom count policy, False otherwise

        Bedroom Fit Rule (parameterized "2 per bedroom + 1"):
        - Maximum occupancy only (no minimum)
        - MaxOccupants = bedrooms × QUAL.MaxOccupantsPerBedroom + QUAL.MaxOccupantsBonus
        - Studio (0BR) has its own cap: QUAL.MaxOccupantsStudio (no per-bedroom term/bonus)
        - Reject only if household size exceeds max capacity

        Examples (defaults 2 / 1 / 2): studio 2, 1BR 3, 2BR 5, 3BR 7, 4BR 9.

        The "2 per bedroom" benchmark is the HUD Keating rebuttable presumption, not a
        hard cap; "2+1" is the CA-statute-backed industry standard (evidence_base.md §3).
        Age-aware occupancy (infants uncounted; a couple + young child in a studio) needs
        child-age data the model doesn't track yet.
        """
        total_occupants = candidate.adult_count + candidate.child_count

        if bedrooms == 0:
            # Studio: its own cap (no per-bedroom term + bonus). Age-aware revisit.
            max_occupants = self.params.max_occupants_studio
        else:
            max_occupants = (
                bedrooms * self.params.max_occupants_per_bedroom
                + self.params.max_occupants_bonus
            )

        # Accept if household size does not exceed max capacity
        return total_occupants <= max_occupants

    def _check_income_qualification(self, candidate: ApplicantCandidate, monthly_rent: Decimal) -> bool:
        """
        Check if candidate's income qualifies for monthly rent (30% rule)

        Args:
            candidate: ApplicantCandidate with estimated monthly income
            monthly_rent: Monthly rent for the unit

        Returns:
            True if rent <= 30% of monthly income, False otherwise

        Income Qualification Rule:
        - Monthly rent must not exceed max_rent_to_income_ratio of monthly income
        - Default ratio: 0.30 (30%)
        """
        max_affordable_rent = candidate.estimated_monthly_income * self.params.max_rent_to_income_ratio
        return monthly_rent <= max_affordable_rent

    def _update_vacancy_counters(
        self,
        vacancy_id: int,
        generated_count: int,
        self_screened_count: int,
        applied_count: int,
        rejected_count: int,
        selected_count: int
    ) -> None:
        """
        Update vacancy applicant funnel counters

        Args:
            vacancy_id: VacancyID to update
            generated_count: Number of candidates generated (slate size)
            self_screened_count: Number who opted not to apply (pre-screen filter)
            applied_count: Number who submitted applications (passed pre-screen)
            rejected_count: Number of applications rejected (failed qualification)
            selected_count: Number of applications selected (lease created)

        Increments existing counters (supports daily processing):
        - GeneratedCandidateCount += generated_count
        - SelfScreenedCount += self_screened_count
        - AppliedCount += applied_count
        - RejectedCount += rejected_count
        - SelectedCount += selected_count

        Validation:
        - generated_count = self_screened_count + applied_count
        - applied_count = rejected_count + selected_count
        """
        update_query = """
            UPDATE simulation.Vacancy
            SET
                GeneratedCandidateCount = GeneratedCandidateCount + ?,
                SelfScreenedCount = SelfScreenedCount + ?,
                AppliedCount = AppliedCount + ?,
                RejectedCount = RejectedCount + ?,
                SelectedCount = SelectedCount + ?
            WHERE RunID = ? AND VacancyID = ?
        """
        self.db.execute_non_query(
            update_query,
            (
                generated_count,
                self_screened_count,
                applied_count,
                rejected_count,
                selected_count,
                self.run_id,
                vacancy_id
            )
        )

    def _record_evaluation(
        self,
        vacancy_id: int,
        candidate: ApplicantCandidate,
        outcome: str,
        evaluated_date: date,
        rejection_reason: Optional[str] = None
    ) -> int:
        """
        Record applicant evaluation in ApplicantEvaluation table

        Args:
            vacancy_id: VacancyID being evaluated
            candidate: ApplicantCandidate being evaluated
            outcome: 'SELECTED' or 'REJECTED'
            evaluated_date: Date evaluation occurred (current simulation date)
            rejection_reason: Reason for rejection (if outcome='REJECTED')

        Returns:
            EvaluationID of created record

        Note: No event logging for evaluations (too granular)
        """
        evaluation_id = self.get_next_evaluation_id()

        insert_query = """
            INSERT INTO simulation.ApplicantEvaluation
            (RunID, EvaluationID, VacancyID, ApplicantIndex, HouseholdType, HouseholdIncomeBand,
             AdultCount, ChildCount, Outcome, RejectionReason, EvaluatedDate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                evaluation_id,
                vacancy_id,
                candidate.applicant_index,
                candidate.household_type,
                candidate.household_income_band,
                candidate.adult_count,
                candidate.child_count,
                outcome,
                rejection_reason,
                evaluated_date
            )
        )

        return evaluation_id

    def _evaluate_candidates(
        self,
        vacancy_record: VacancyRecord,
        candidate_slate: List[ApplicantCandidate],
        current_date: date,
        qualification_rent: Decimal
    ) -> VacancyEvaluationResult:
        """
        Evaluate candidate slate and select first qualified applicant

        Args:
            vacancy_record: VacancyRecord with unit details (bedrooms)
            candidate_slate: List of ApplicantCandidate objects to evaluate
            current_date: Current simulation date (for EvaluatedDate)
            qualification_rent: THE rent the lease will sign at (inflation-adjusted,
                computed once by the caller; screens and
                signing share this exact Decimal)

        Returns:
            VacancyEvaluationResult with selected candidate (or None if none qualified)

        Selection Logic:
        - First-qualified applicant wins (no ranking by income)
        - Qualification checks (in order):
          1. Pre-screen affordability (self-selection: rent ≤ 40% of income)
          2. Bedroom fit (maximum occupancy only)
          3. Income qualification (30% rule: rent ≤ 30% of income)
        - Self-screened candidates: NOT recorded in ApplicantEvaluation (they never applied)
        - Actual applications: Recorded in ApplicantEvaluation (passed pre-screen)
        - All funnel metrics: Tracked in Vacancy counter columns
        """
        selected_candidate = None
        evaluation_ids = []

        # Track applicant funnel metrics
        generated_count = len(candidate_slate)
        self_screened_count = 0
        applied_count = 0
        rejected_count = 0
        selected_count = 0

        for candidate in candidate_slate:
            # Check pre-screen affordability (self-selection filter)
            # Households self-screen out if rent > 40% of income (behavioral realism)
            prescreen_passed = self._check_prescreen_affordability(candidate, qualification_rent)
            if not prescreen_passed:
                # Self-screened candidates are NOT applicants
                # Do not record in ApplicantEvaluation - they never applied
                # Track in vacancy counter only
                self_screened_count += 1
                continue

            # Candidate passed pre-screen - this is an actual application
            applied_count += 1

            # Check bedroom fit
            bedroom_fit = self._check_bedroom_fit(candidate, vacancy_record.bedrooms)
            if not bedroom_fit:
                rejected_count += 1
                eval_id = self._record_evaluation(
                    vacancy_record.vacancy_id,
                    candidate,
                    outcome='REJECTED',
                    evaluated_date=current_date,
                    rejection_reason='BEDROOM_FIT'
                )
                evaluation_ids.append(eval_id)
                continue

            # Check income qualification
            income_qualified = self._check_income_qualification(candidate, qualification_rent)
            if not income_qualified:
                rejected_count += 1
                eval_id = self._record_evaluation(
                    vacancy_record.vacancy_id,
                    candidate,
                    outcome='REJECTED',
                    evaluated_date=current_date,
                    rejection_reason='INCOME_INSUFFICIENT'
                )
                evaluation_ids.append(eval_id)
                continue

            # Candidate is qualified!
            if selected_candidate is None:
                selected_candidate = candidate
                selected_count += 1
                eval_id = self._record_evaluation(
                    vacancy_record.vacancy_id,
                    candidate,
                    outcome='SELECTED',
                    evaluated_date=current_date,
                    rejection_reason=None
                )
                evaluation_ids.append(eval_id)
            else:
                # Already selected someone, reject this candidate
                rejected_count += 1
                eval_id = self._record_evaluation(
                    vacancy_record.vacancy_id,
                    candidate,
                    outcome='REJECTED',
                    evaluated_date=current_date,
                    rejection_reason='EARLIER_QUALIFIED_SELECTED'
                )
                evaluation_ids.append(eval_id)

        # Update vacancy counters
        self._update_vacancy_counters(
            vacancy_record.vacancy_id,
            generated_count,
            self_screened_count,
            applied_count,
            rejected_count,
            selected_count
        )

        rejection_count = len([c for c in candidate_slate if c != selected_candidate])

        return VacancyEvaluationResult(
            vacancy_id=vacancy_record.vacancy_id,
            selected_candidate=selected_candidate,
            rejection_count=rejection_count,
            evaluation_ids=evaluation_ids
        )

    # =========================================================================
    # Lease Creation and Materialization
    # =========================================================================

    def create_lease(
        self,
        vacancy_record: VacancyRecord,
        selected_candidate: ApplicantCandidate,
        current_date: date,
        monthly_rent: Optional[Decimal] = None
    ) -> LeaseRecord:
        """
        Orchestrate complete lease creation process

        Args:
            vacancy_record: VacancyRecord with unit details
            selected_candidate: ApplicantCandidate who was selected
            current_date: Current simulation date (LeaseSignedDate)

        Returns:
            LeaseRecord for created lease

        Steps:
        1. Materialize household (insert simulation.Household)
        2. Materialize persons (insert simulation.Person for all household members)
        3. Create lease record (insert simulation.Lease)
        4. Close vacancy (update simulation.Vacancy with LeaseID and VacancyEndDate)
        """
        # Step 1: Materialize household
        household_id = self._materialize_household(selected_candidate, current_date)

        # Step 2: Materialize persons
        self._materialize_persons(
            household_id,
            selected_candidate.household_type,
            selected_candidate.adult_count,
            selected_candidate.child_count,
            current_date
        )

        # Step 3: Create lease record (the caller passes the SAME
        # inflation-adjusted rent the screens qualified against; None = compute
        # here, identical function/date basis, for direct callers/tests)
        lease_record = self._create_lease_record(
            household_id,
            vacancy_record,
            current_date,
            monthly_rent
        )

        # Step 4: Close vacancy
        self._close_vacancy(
            vacancy_record.vacancy_id,
            lease_record.lease_id,
            current_date
        )

        # Step 5: Initialize security deposit
        if self.deposit_manager:
            self.deposit_manager.initialize_deposit_for_lease(
                lease_id=lease_record.lease_id,
                monthly_rent=lease_record.monthly_rent,
                lease_signed_date=current_date
            )

        return lease_record

    def _materialize_household(
        self,
        candidate: ApplicantCandidate,
        current_date: date
    ) -> int:
        """
        Create Household record for selected applicant

        Args:
            candidate: ApplicantCandidate who was selected
            current_date: Current simulation date

        Returns:
            HouseholdID of created household

        Event Logging:
        - Logs HOUSEHOLD_CREATED event (EventType.SIMULATION, EntityType.HOUSEHOLD, ActionType.CREATE)
        """
        household_id = self.get_next_household_id()

        # Log HOUSEHOLD_CREATED event
        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.HOUSEHOLD,
            entity_id=household_id,
            action_type=ActionType.CREATE,
            metadata=f"Household created: {candidate.household_type}, Income Band: {candidate.household_income_band}"
        )

        # Insert household record. Persist the (already-computed)
        # monthly signing income in dollars — retention's affordability term needs
        # income at renewal time, and only the band was stored before. income(t) is
        # reconstructed as SigningMonthlyIncome × wage_growth(sign→t) via the income model
        # (no re-draw; invariant #8 safe).
        insert_query = """
            INSERT INTO simulation.Household
            (RunID, HouseholdID, HouseholdType, HouseholdIncomeBand, AdultCount, ChildCount,
             CreatedDate, CreatedEventID, SigningMonthlyIncome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                household_id,
                candidate.household_type,
                candidate.household_income_band,
                candidate.adult_count,
                candidate.child_count,
                current_date,
                event_id,
                candidate.estimated_monthly_income
            )
        )

        # initialize TCS zero-balance row at household materialization (idempotent)
        if self.tenant_credit_manager:
            self.tenant_credit_manager.initialize_household(household_id, event_id)

            # draw household-type-weighted redemption probability
            # from the per-type bands (FAMILY/COUPLE/SINGLE/ROOMMATES) using a
            # triangular distribution. Falls back to the global Min/Default/Max
            # guardrails for any unrecognized type. The bands
            # are now registry-backed (TenantCreditManager.household_type_bands).
            from tenant_credit_manager import draw_household_redemption_probability
            redemption_probability = draw_household_redemption_probability(
                household_type=candidate.household_type,
                onboarding_rng=self.tcs_onboarding_rng,
                bands=self.tenant_credit_manager.household_type_bands,
                fallback_min=self.tenant_credit_manager.redemption_prob_min,
                fallback_center=self.tenant_credit_manager.redemption_prob_default,
                fallback_max=self.tenant_credit_manager.redemption_prob_max,
            )
            self.db.execute_non_query(
                "UPDATE simulation.Household "
                "SET TCSRedemptionProbability = ?, "
                "    TCSRedemptionProbabilityUpdatedDate = ? "
                "WHERE RunID = ? AND HouseholdID = ?",
                (redemption_probability, current_date, self.run_id, household_id),
            )

        return household_id

    def _materialize_persons(
        self,
        household_id: int,
        household_type: str,
        adult_count: int,
        child_count: int,
        current_date: date
    ) -> List[int]:
        """
        Create Person records for all household members

        Args:
            household_id: HouseholdID to assign persons to
            household_type: SINGLE, COUPLE, FAMILY, or ROOMMATES
            adult_count: Number of adults in household
            child_count: Number of children in household
            current_date: Current simulation date

        Returns:
            List of PersonIDs created

        Person Generation:
        - Names selected deterministically from reference.FirstName and reference.LastName
        - DateOfBirth: Synthetic placeholders (not genealogical data)
          - Adults: Random age 22-65 (approx)
          - Children: Random age 0-17
        - IsAdult: age >= 18 (derived from DateOfBirth)

        Event Logging:
        - Logs PERSON_CREATED event for each person
        """
        person_ids = []
        total_members = adult_count + child_count

        # Use household_id as RNG seed for deterministic name selection
        name_rng = random.Random(f"{self._get_run_seed()}_household_{household_id}_names")

        for member_index in range(total_members):
            is_adult = member_index < adult_count  # First N are adults, rest are children

            # Select names deterministically
            first_name = name_rng.choice(self.first_names)
            last_name = name_rng.choice(self.last_names)

            # Generate DateOfBirth
            if is_adult:
                age_years = name_rng.randint(self.adult_age_min_years, self.adult_age_max_years)
            else:
                age_years = name_rng.randint(self.child_age_min_years, self.child_age_max_years)

            # Approximate DOB (365 days per year)
            date_of_birth = date(current_date.year - age_years, current_date.month, 1)

            # Get PersonID
            person_id = self.get_next_person_id()

            # Log PERSON_CREATED event
            event_id = self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=current_date,
                entity_type=EntityType.PERSON,
                entity_id=person_id,
                action_type=ActionType.CREATE,
                metadata=f"Person created: {first_name} {last_name}, HouseholdID={household_id}, IsAdult={is_adult}"
            )

            # Insert person record
            insert_query = """
                INSERT INTO simulation.Person
                (RunID, PersonID, HouseholdID, FirstName, LastName, DateOfBirth, IsAdult,
                 CreatedDate, CreatedEventID)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            self.db.execute_non_query(
                insert_query,
                (
                    self.run_id,
                    person_id,
                    household_id,
                    first_name,
                    last_name,
                    date_of_birth,
                    is_adult,
                    current_date,
                    event_id
                )
            )

            person_ids.append(person_id)

        return person_ids

    def _create_lease_record(
        self,
        household_id: int,
        vacancy_record: VacancyRecord,
        current_date: date,
        monthly_rent: Optional[Decimal] = None
    ) -> LeaseRecord:
        """
        Create Lease record

        Args:
            household_id: HouseholdID signing the lease
            vacancy_record: VacancyRecord with unit and rent details
            current_date: Current simulation date (LeaseSignedDate)

        Returns:
            LeaseRecord with lease details

        Lease Terms:
        - LeaseSignedDate: current_date (day candidate selected)
        - LeaseStartDate: First day of following month
        - LeaseEndDate: LeaseStartDate + standard_lease_duration_months (default 12)
        - MonthlyRent: the single-source inflation-adjusted rent
        - LeaseStatus: 'ACTIVE'

        Event Logging:
        - Logs LEASE_SIGNED event (EventType.SIMULATION, EntityType.LEASE, ActionType.CREATE)
        """
        lease_id = self.get_next_lease_id()

        # compute inflation-adjusted base rent.
        # V5 verification confirmed PropertyUnits.CurrentRent is set once at
        # acquisition (0.9 * BaseRent) and never updated. So we use the unit's
        # BaseRent and apply the cumulative rent inflation index up to current_date.
        # deposit-installment math repaired in parallel to handle arbitrary
        # rent values without overflowing InstallmentsPaidCount.
        # prefer the rent the screens already qualified against
        # (passed down from the fill attempt — identity by construction);
        # compute only for direct callers that did not supply it.
        inflation_adjusted_rent = (
            monthly_rent if monthly_rent is not None
            else self._compute_inflation_adjusted_rent(vacancy_record.unit_id, current_date)
        )

        # Calculate lease dates
        lease_signed_date = current_date

        # LeaseStartDate: First day of following month
        if current_date.month == 12:
            lease_start_date = date(current_date.year + 1, 1, 1)
        else:
            lease_start_date = date(current_date.year, current_date.month + 1, 1)

        # LeaseEndDate: LeaseStartDate + lease duration
        lease_duration_months = self.params.standard_lease_duration_months
        end_year = lease_start_date.year + (lease_start_date.month + lease_duration_months - 1) // 12
        end_month = (lease_start_date.month + lease_duration_months - 1) % 12 + 1
        lease_end_date = date(end_year, end_month, lease_start_date.day)

        # Log LEASE_SIGNED event
        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.CREATE,
            metadata=f"Lease signed: HouseholdID={household_id}, UnitID={vacancy_record.unit_id}, Rent=${inflation_adjusted_rent}/mo"
        )

        # Insert lease record.
        # a new lease starts with no earned reduction —
        # EffectiveMonthlyRent is explicitly set equal to MonthlyRent
        # (the column has no DB default, so an omission would fail loudly).
        # CumulativeRentReductionPct defaults to 0 via the column DEFAULT.
        insert_query = """
            INSERT INTO simulation.Lease
            (RunID, LeaseID, HouseholdID, UnitID, VacancyID, LeaseSignedDate, LeaseStartDate,
             LeaseEndDate, MonthlyRent, EffectiveMonthlyRent, LeaseStatus, SignedEventID)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                lease_id,
                household_id,
                vacancy_record.unit_id,
                vacancy_record.vacancy_id,
                lease_signed_date,
                lease_start_date,
                lease_end_date,
                inflation_adjusted_rent,
                inflation_adjusted_rent,
                'ACTIVE',
                event_id
            )
        )

        # transition unit to Occupied on lease creation
        unit_status_query = """
            UPDATE simulation.PropertyUnits
            SET UnitStatus = 'Occupied'
            WHERE RunID = ?
                AND UnitID = ?
        """
        self.db.execute_non_query(unit_status_query, (self.run_id, vacancy_record.unit_id))

        # If this household was previously EXITED with TCS credit
        # still in its 24-month validity window, re-entry resets the expiry
        # clock. mark_household_reentered is idempotent and only
        # fires the UPDATE for rows currently in 'EXITED' status, so this is
        # safe to call for every new lease (cheap no-op for first-time
        # tenants or households that never exited).
        if self.tenant_credit_manager is not None:
            self.tenant_credit_manager.mark_household_reentered(
                household_id=household_id,
                reentry_date=lease_start_date,
            )

        return LeaseRecord(
            lease_id=lease_id,
            household_id=household_id,
            unit_id=vacancy_record.unit_id,
            vacancy_id=vacancy_record.vacancy_id,
            lease_signed_date=lease_signed_date,
            lease_start_date=lease_start_date,
            lease_end_date=lease_end_date,
            monthly_rent=inflation_adjusted_rent,
            lease_status='ACTIVE',
            signed_event_id=event_id
        )

    def _compute_inflation_adjusted_rent(self, unit_id: int, target_date: date) -> Decimal:
        """
        Compute a new lease's rent from the unit's
        BaseRent + cumulative rent inflation (not the prior tenant's reduced rent).

        Apply the Liberty Bee below-market
        discount. New lease rent =
            BaseRent × (1 − below_market_rent_pct) × Π(1 + RentRate)
        from simulation start to target_date.

        Args:
            unit_id: UnitID to look up BaseRent for
            target_date: Date to compute the inflation factor as of

        Returns:
            Decimal monthly rent = BaseRent × (1 − below_market_rent_pct) ×
            product(1 + RentRate) for each InflationSchedule month up to
            target_date. With no inflation schedule the factor is 1.0, so the
            result is the discounted base rent.
        """
        # Read the unit's BaseRent (immutable, set at acquisition)
        base_rent_row = self.db.execute_query(
            "SELECT BaseRent FROM simulation.PropertyUnits WHERE RunID = ? AND UnitID = ?",
            (self.run_id, unit_id)
        )
        if not base_rent_row:
            raise RuntimeError(f"Cannot compute rent: UnitID {unit_id} not found for RunID {self.run_id}")
        base_rent = Decimal(str(base_rent_row[0][0]))

        # Compute cumulative compound rent inflation factor up to target_date
        # (matches the pattern used in property_market_manager for PropertyRate).
        rate_rows = self.db.execute_query(
            """
            SELECT RentRate
            FROM simulation.InflationSchedule
            WHERE RunID = ?
              AND InflationDate <= ?
            ORDER BY InflationDate
            """,
            (self.run_id, target_date)
        )

        factor = Decimal('1.0')
        for row in rate_rows:
            factor *= (Decimal('1.0') + Decimal(str(row[0])))

        # sign the lease at the Liberty Bee below-market rate, not
        # the market BaseRent. A new tenant gets the unit's standard discounted
        # rate, inflation-adjusted — never the prior tenant's tenure-reduced rent
        # (this preserves the turnover-hygiene intent).
        below_market_base = base_rent * (Decimal('1.0') - self.below_market_rent_pct)
        # quantize to cents AT THE SOURCE with SQL Server's
        # rounding (half-away-from-zero), so the rent the screens qualify
        # against, the value inserted, and the stored DECIMAL(18,2) are the
        # SAME number — the screen==sign identity holds bit-exactly end-to-end.
        return (below_market_base * factor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def _close_vacancy(
        self,
        vacancy_id: int,
        lease_id: int,
        current_date: date
    ) -> None:
        """
        Close vacancy by setting VacancyEndDate and LeaseID

        Args:
            vacancy_id: VacancyID to close
            lease_id: LeaseID that filled the vacancy
            current_date: Current simulation date (VacancyEndDate)

        Note: No event logging (vacancy closure implied by lease creation event)
        """
        update_query = """
            UPDATE simulation.Vacancy
            SET VacancyEndDate = ?,
                LeaseID = ?
            WHERE RunID = ? AND VacancyID = ?
        """
        self.db.execute_non_query(
            update_query,
            (current_date, lease_id, self.run_id, vacancy_id)
        )
