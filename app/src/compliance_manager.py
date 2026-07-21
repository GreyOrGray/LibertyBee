"""
Compliance Manager - Property & Unit Compliance System

Manages compliance attempts and work items for acquisition, turnover, and periodic triggers.

Key Design Principles:
- Work items are source of truth (dates/costs flow from work items)
- Append-only ledgers for complete audit trails
- Trigger-based architecture (acquisition/turnover/periodic)
- Deterministic RNG via stable hashing per work item
- Cost timing: inspection paid on resolution, remediation paid on start

Architecture:
- ComplianceAttempt: Single compliance cycle (building or unit level)
- ComplianceWorkItem: Actual actions (inspections/remediation)
- ComplianceStep: Verbose narrative ledger (append-only)
- ComplianceParameters: Work type configuration (cached in memory)
"""

from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass
from decimal import Decimal
from datetime import date, timedelta
import hashlib
import struct

from database_manager import DatabaseManager
from event_logger import EventLogger
from parameter_registry import ParameterRegistry


@dataclass
class ComplianceParameterConfig:
    """In-memory cache of compliance parameters from reference.ComplianceParameters"""
    work_type: str
    cost_min: Decimal
    cost_max: Decimal
    duration_min_days: int
    duration_max_days: int
    fail_chance_base: Decimal
    pass_chance_override: Optional[Decimal]
    contractor_lag_min_days: int
    contractor_lag_max_days: int
    is_blocking_default: bool
    scope_type: str  # 'BUILDING' | 'UNIT'
    description: str


class ComplianceManager:
    """
    Manages property and unit compliance system with work items and gating logic.

    v1 Scope: Acquisition-triggered compliance only (defer turnover/periodic)
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_logger: EventLogger,
        run_id: int,
        fund_manager,
        random_seed: int = 12345,
    ):
        """
        Initialize ComplianceManager and load compliance parameters into memory.

        Args:
            db_manager: Database manager instance
            event_logger: Event logger instance
            run_id: Current simulation run ID
            fund_manager: FundManager — compliance/remediation costs are REAL
                cash outflows (canon: "Compliance
                costs draw from Cash only — never CSF"). Required, fail-loud:
                the TODO left ~$476k/240-mo of work items paid on
                paper but never debited.
            random_seed: Reproducibility seed — used to derive the
                stable per-work-item hash so the same seed produces the same
                inspection outcomes regardless of RunID. Must be the user-provided
                --seed value, not derived from run_id.
        """
        self.db = db_manager
        self.event_logger = event_logger
        self.run_id = run_id
        self.fund_manager = fund_manager
        self.random_seed = random_seed
        if self.fund_manager is None:
            raise ValueError("ComplianceManager requires a FundManager: "
                             "compliance costs must reach the ledger.")

        # In-memory cache of compliance parameters (loaded once at initialization)
        self.parameters: Dict[str, ComplianceParameterConfig] = {}

        # Load compliance parameters from database
        self._load_compliance_parameters()

        # Schedule offsets,
        # due-diligence duration/cost sampling, and remediation severity policy
        # are CMPL registry knobs, seeded with the exact former literals
        # (value-neutral). Read-once, fail-loud.
        registry = ParameterRegistry(self.db).load_globals()
        self.offset_doc_review_days = registry.get_int('CMPL', 'ScheduleOffsetDocReviewDays')
        self.offset_smoke_co_days = registry.get_int('CMPL', 'ScheduleOffsetSmokeCoDays')
        self.offset_habitability_days = registry.get_int('CMPL', 'ScheduleOffsetHabitabilityDays')
        self.offset_due_diligence_days = registry.get_int('CMPL', 'ScheduleOffsetDueDiligenceDays')
        self.dd_duration_bands = {
            'MINOR': (registry.get_int('CMPL', 'DueDiligenceMinorDurationMinDays'),
                      registry.get_int('CMPL', 'DueDiligenceMinorDurationMaxDays')),
            'MODERATE': (registry.get_int('CMPL', 'DueDiligenceModerateDurationMinDays'),
                         registry.get_int('CMPL', 'DueDiligenceModerateDurationMaxDays')),
            'MAJOR': (registry.get_int('CMPL', 'DueDiligenceMajorDurationMinDays'),
                      registry.get_int('CMPL', 'DueDiligenceMajorDurationMaxDays')),
        }
        # CLEAN shares the MINOR band explicitly (Clean-with-
        # repair-cost is a live branch and must never block building-online).
        self.dd_duration_bands['CLEAN'] = self.dd_duration_bands['MINOR']
        self.dd_cost_mult_base = registry.get_float('CMPL', 'DueDiligenceCostMultBase')
        self.dd_cost_mult_span = registry.get_float('CMPL', 'DueDiligenceCostMultSpan')
        self.unit_remediation_minor_pct = registry.get_float('CMPL', 'UnitRemediationMinorPct')
        self.building_remediation_moderate_pct = registry.get_float('CMPL', 'BuildingRemediationModeratePct')
        self.severity_multipliers = {
            'MINOR': registry.get_float('CMPL', 'SeverityMultMinor'),
            'MODERATE': registry.get_float('CMPL', 'SeverityMultModerate'),
            'MAJOR': registry.get_float('CMPL', 'SeverityMultMajor'),
        }

        # KD-043 lead knobs (all-aspects-knobbed regime, ratified). The lead
        # chain is jurisdiction-specific: the cutoff year gates the chain
        # (sentinel 0 disables it entirely — no-lead-law jurisdictions and
        # new-build portfolios alike); costs are unit-scaled per the cited
        # anchors (evidence_base §11) with the severity multiplier fixed at
        # 1.0 for lead items — base + per-unit IS the full job (the model
        # prices the full-deleading pole; a severity gradient on it is a
        # vestige of habitability remediation).
        self.lead_cutoff_year = registry.get_int('CMPL', 'LeadCutoffYearBuilt')
        self.lead_assess_cost_base = registry.get_float('CMPL', 'LeadAssessmentCostBase')
        self.lead_assess_cost_per_unit = registry.get_float('CMPL', 'LeadAssessmentCostPerUnit')
        self.lead_abate_cost_base = registry.get_float('CMPL', 'LeadAbatementCostBase')
        self.lead_abate_cost_per_unit = registry.get_float('CMPL', 'LeadAbatementCostPerUnit')
        self.lead_abate_cost_band_pct = registry.get_float('CMPL', 'LeadAbatementCostBandPct')

    def _property_unit_count(self, property_id: int) -> int:
        """Unit count for lead cost scaling — the SAME source the acquisition
        gate's expectation uses (reference.Units by PropertyID), so gate and
        realized draws can never scale from different counts (KD-043)."""
        row = self.db.execute_query(
            "SELECT COUNT(*) FROM reference.Units WHERE PropertyID = ?",
            (property_id,))
        return int(row[0][0]) if row and row[0] else 1

    def _is_pre_lead_cutoff(self, year_built) -> bool:
        """KD-043: the lead chain fires iff the building predates the
        jurisdiction cutoff. Sentinel `LeadCutoffYearBuilt = 0` disables the
        chain entirely and TAKES PRECEDENCE over the unknown-YearBuilt
        default (ratified precedence). Unknown YearBuilt counts as
        pre-cutoff (conservative) only when the chain is enabled."""
        if self.lead_cutoff_year <= 0:
            return False
        return year_built is None or year_built < self.lead_cutoff_year

    def _load_compliance_parameters(self) -> None:
        """
        Load compliance parameters from reference.ComplianceParameters into memory.

        This is called once at initialization to avoid repeated database queries.
        """
        query = """
            SELECT
                WorkType,
                CostMin,
                CostMax,
                DurationMinDays,
                DurationMaxDays,
                FailChanceBase,
                PassChanceOverride,
                ContractorLagMinDays,
                ContractorLagMaxDays,
                IsBlockingDefault,
                ScopeType,
                Description
            FROM reference.ComplianceParameters
            ORDER BY WorkType
        """

        rows = self.db.execute_query(query)

        for row in rows:
            config = ComplianceParameterConfig(
                work_type=row.WorkType,
                cost_min=row.CostMin,
                cost_max=row.CostMax,
                duration_min_days=row.DurationMinDays,
                duration_max_days=row.DurationMaxDays,
                fail_chance_base=row.FailChanceBase,
                pass_chance_override=row.PassChanceOverride,
                contractor_lag_min_days=row.ContractorLagMinDays,
                contractor_lag_max_days=row.ContractorLagMaxDays,
                is_blocking_default=bool(row.IsBlockingDefault),
                scope_type=row.ScopeType,
                description=row.Description
            )
            self.parameters[row.WorkType] = config

        print(f"[ComplianceManager] Loaded {len(self.parameters)} work type parameters")

    def expected_onboarding_compliance_cost(self, unit_count: int,
                                            year_built: Optional[int],
                                            known_lead_cost: Optional[Decimal] = None) -> Decimal:
        """KD-042: expected one-time compliance cost of onboarding ONE
        acquired property — the probability-weighted average of exactly the
        work-item chains acquisition close spawns (see
        initiate_property_compliance / REMEDIATION_MAPPING):

          building:  BUILDING_HABITABILITY_INSPECTION → BUILDING_REMEDIATION
                       (ModeratePct × 1.5 | else × 3.0)
                     LEAD_DOC_REVIEW ($0) → LEAD_RISK_ASSESSMENT ($base+per-unit)
                       → LEAD_ABATEMENT ($base+per-unit, ±band, ×1.0) — pre-cutoff (knobbed) only
          per unit:  UNIT_SMOKE_CO_INSPECTION → …_REMEDIATION (always MINOR)
                     UNIT_HABITABILITY_INSPECTION → UNIT_REMEDIATION
                       (MinorPct × 1.0 | else × 1.5)

        Base-year dollars (compliance charges are not inflation-adjusted in
        the engine — the expectation matches what will actually be drawn).
        Remediation costs use the uniform band MEAN; inspection items use
        cost_min, matching the CostEstimate the spawn writes. Unknown
        Lead terms derive from the SAME knobs the realized spawn uses
        (KD-043: the cutoff sentinel is honored identically — a disabled
        jurisdiction prices zero lead; unknown YearBuilt counts as
        pre-cutoff only when the chain is enabled; unit-scaled assessment
        + abatement at multiplier 1.0), so gate pricing and realization
        move together by construction. Used ONLY by the pro-forma
        acquisition gate — realized draws are untouched.
        """
        def band_mean(work_type: str) -> Decimal:
            p = self.parameters[work_type]
            return (Decimal(str(p.cost_min)) + Decimal(str(p.cost_max))) / 2

        def fail_chance(work_type: str) -> Decimal:
            p = self.parameters[work_type]
            if p.pass_chance_override is not None:
                return Decimal('1') - Decimal(str(p.pass_chance_override))
            return Decimal(str(p.fail_chance_base))

        mult = {k: Decimal(str(v)) for k, v in self.severity_multipliers.items()}

        # Building habitability chain (every property)
        bldg_rem_sev = (Decimal(str(self.building_remediation_moderate_pct)) * mult['MODERATE']
                        + (1 - Decimal(str(self.building_remediation_moderate_pct))) * mult['MAJOR'])
        expected = (Decimal(str(self.parameters['BUILDING_HABITABILITY_INSPECTION'].cost_min))
                    + fail_chance('BUILDING_HABITABILITY_INSPECTION')
                    * band_mean('BUILDING_REMEDIATION') * bldg_rem_sev)

        # Lead chain. KD-193 (D3 coherence): if the property's lead has been
        # ASSESSED at due diligence, price it at the KNOWN actual
        # (known_lead_cost) — the assessment cost is already sunk pre-closing
        # (a DD cost), so only the foreseen ABATEMENT remains in the onboarding
        # lump. Otherwise (un-assessed candidate), fall back to the E[lead]
        # expectation — unit-scaled assessment + abatement at ×1.0, cutoff/
        # sentinel identical to the realized spawn (KD-043 move-together).
        if known_lead_cost is not None:
            expected += Decimal(str(known_lead_cost))
        elif self._is_pre_lead_cutoff(year_built):
            assess = (Decimal(str(self.lead_assess_cost_base))
                      + Decimal(str(self.lead_assess_cost_per_unit)) * max(0, unit_count - 1))
            abate_center = (Decimal(str(self.lead_abate_cost_base))
                            + Decimal(str(self.lead_abate_cost_per_unit)) * max(0, unit_count - 1))
            expected += fail_chance('LEAD_DOC_REVIEW') * (
                assess + fail_chance('LEAD_RISK_ASSESSMENT') * abate_center)

        # Per-unit chains
        e_smoke = (Decimal(str(self.parameters['UNIT_SMOKE_CO_INSPECTION'].cost_min))
                   + fail_chance('UNIT_SMOKE_CO_INSPECTION')
                   * band_mean('UNIT_SMOKE_CO_REMEDIATION') * mult['MINOR'])
        unit_rem_sev = (Decimal(str(self.unit_remediation_minor_pct)) * mult['MINOR']
                        + (1 - Decimal(str(self.unit_remediation_minor_pct))) * mult['MODERATE'])
        e_unit_hab = (Decimal(str(self.parameters['UNIT_HABITABILITY_INSPECTION'].cost_min))
                      + fail_chance('UNIT_HABITABILITY_INSPECTION')
                      * band_mean('UNIT_REMEDIATION') * unit_rem_sev)
        expected += Decimal(unit_count) * (e_smoke + e_unit_hab)

        return expected.quantize(Decimal('0.01'))

    def assess_property_lead(self, unit_count: int, year_built: Optional[int], rng):
        """KD-193: pre-closing lead assessment — the ACTUAL draw (not the
        E[lead] average) of the lead-chain outcome for ONE property, using the
        SAME knobs, cutoff, and probabilities as
        expected_onboarding_compliance_cost / the realized spawn (full-chain
        fidelity: doc-review P(no docs)=0.85 → hazard P=0.55 → abatement ±band).

        Returns (assessment_cost, abatement_cost):
          assessment_cost — the risk-assessment due-diligence cost INCURRED
            (0 if post-cutoff or docs present; the org pays the assessor only
            when it commissions one, i.e. when doc-review fails).
          abatement_cost  — the FORESEEN full abatement cost (0 if post-cutoff /
            docs present / no hazard; else base + per-unit, ±band). This is the
            store-and-forward value: persisted on the attempt at inspection and
            READ by the post-closing LEAD_ABATEMENT (never re-drawn — the
            realized abatement RNG seeds on WorkItemID/ScheduledDate, absent
            pre-closing).

        rng: a caller-owned, property-seeded random.Random (dedicated offset —
        NOT the content-hash work-item stream). Draw order (doc → hazard →
        band) is fixed so the outcome is seed-reproducible.
        """
        zero = Decimal('0.00')
        if not self._is_pre_lead_cutoff(year_built):
            return zero, zero

        def fail_chance(work_type: str) -> float:
            p = self.parameters[work_type]
            if p.pass_chance_override is not None:
                return 1.0 - float(p.pass_chance_override)
            return float(p.fail_chance_base)

        # LEAD_DOC_REVIEW — docs present (pass) ⇒ no lead cost.
        if rng.random() >= fail_chance('LEAD_DOC_REVIEW'):
            return zero, zero
        # no docs ⇒ commission the risk assessment (DD cost incurred now).
        assess = (Decimal(str(self.lead_assess_cost_base))
                  + Decimal(str(self.lead_assess_cost_per_unit)) * max(0, unit_count - 1)
                  ).quantize(Decimal('0.01'))
        # LEAD_RISK_ASSESSMENT — no hazard (pass) ⇒ assessment only, no abatement.
        if rng.random() >= fail_chance('LEAD_RISK_ASSESSMENT'):
            return assess, zero
        # hazard ⇒ foresee the full abatement cost (unit-scaled center ± band, ×1.0).
        center = (Decimal(str(self.lead_abate_cost_base))
                  + Decimal(str(self.lead_abate_cost_per_unit)) * max(0, unit_count - 1))
        band = Decimal(str(self.lead_abate_cost_band_pct))
        factor = (Decimal('1') - band) + Decimal(str(rng.random())) * 2 * band
        return assess, (center * factor).quantize(Decimal('0.01'))

    def assess_property_lead_for_acquisition(self, property_id: int, rng):
        """KD-193 provider entry (wired to PropertyAcquisitionManager's lead
        assessor). Fetches YearBuilt + unit count for `property_id` from the
        SAME sources the gate expectation and the realized spawn use
        (reference.Properties.YearBuilt, `_property_unit_count`), then runs the
        pre-closing assessment. Returns (assessment_cost, abatement_cost).
        `rng` is the caller's dedicated property-seeded Random."""
        row = self.db.execute_query(
            "SELECT YearBuilt FROM reference.Properties WHERE PropertyID = ?", (property_id,))
        year_built = row[0][0] if row else None
        unit_count = self._property_unit_count(property_id)
        return self.assess_property_lead(unit_count, year_built, rng)

    def _is_inspection(self, work_type: str) -> bool:
        """KD-043 reachability fix: an inspection is any work type that can
        SPAWN a child on failure — i.e. a key of REMEDIATION_MAPPING. Role,
        not name: the old string-match ('INSPECTION' in name, or the
        LEAD_DOC_REVIEW special case) silently excluded LEAD_RISK_ASSESSMENT,
        which therefore always resolved COMPLETED and never rolled its
        declared fail chance — LEAD_ABATEMENT was unreachable dead code
        (KD-043; zero abatement rows in any corpus before this fix). Future
        chain extensions inherit correct classification by construction."""
        return work_type in self.REMEDIATION_MAPPING

    def _get_work_item_rng(
        self,
        property_id: int,
        unit_id: Optional[int],
        work_type: str,
        scheduled_date: date,
        work_item_id: int
    ) -> float:
        """
        Generate deterministic RNG value for a work item using stable hashing.

        This ensures that work item outcomes (pass/fail, cost, duration) are:
        - Deterministic (same seed → same outcome)
        - Stable (re-running doesn't change outcome)
        - Independent (each work item has its own RNG)

        Args:
            property_id: Property ID
            unit_id: Unit ID (None for building-level work items)
            work_type: Work type string
            scheduled_date: Scheduled date for work item
            work_item_id: Work item ID (ensures uniqueness)

        Returns:
            Float in range [0.0, 1.0)
        """
        # Create stable hash from work item identity
        # Hash input must NOT include self.run_id — that made RunID
        # a causal simulation input. Use self.random_seed (the user --seed) so
        # the hash is identity-stable across RunIDs.
        hash_input = f"{self.random_seed}|{property_id}|{unit_id or 0}|{work_type}|{scheduled_date.isoformat()}|{work_item_id}"
        hash_bytes = hashlib.sha256(hash_input.encode()).digest()

        # Convert first 8 bytes to uint64, then normalize to [0.0, 1.0)
        uint64_value = struct.unpack('>Q', hash_bytes[:8])[0]
        return uint64_value / (2**64)

    def _get_next_attempt_id(self) -> int:
        """
        Get next AttemptID for this run (manual assignment, no IDENTITY).

        Returns:
            Next available AttemptID for this run
        """
        query = """
            SELECT ISNULL(MAX(AttemptID), 0) + 1 AS NextAttemptID
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
        """
        result = self.db.execute_query(query, (self.run_id,))
        return result[0].NextAttemptID if result else 1

    def _get_next_work_item_id(self) -> int:
        """
        Get next WorkItemID for this run (manual assignment, no IDENTITY).

        Returns:
            Next available WorkItemID for this run
        """
        query = """
            SELECT ISNULL(MAX(WorkItemID), 0) + 1 AS NextWorkItemID
            FROM simulation.ComplianceWorkItem
            WHERE RunID = ?
        """
        result = self.db.execute_query(query, (self.run_id,))
        return result[0].NextWorkItemID if result else 1

    def _get_next_step_id(self) -> int:
        """
        Get next StepID for this run (manual assignment, no IDENTITY).

        Returns:
            Next available StepID for this run
        """
        query = """
            SELECT ISNULL(MAX(StepID), 0) + 1 AS NextStepID
            FROM simulation.ComplianceStep
            WHERE RunID = ?
        """
        result = self.db.execute_query(query, (self.run_id,))
        return result[0].NextStepID if result else 1

    def process_daily_compliance(self, current_date: date) -> None:
        """
        Main entry point for daily compliance processing.

        This is called once per day from the simulation loop and handles:
        1. Trigger detection (acquisition close, unit turnover, periodic)
        2. Work item advancement (scheduled → in_progress → passed/failed/completed)
        3. Remediation spawning (inspection failures)
        4. Gating logic (building online, unit ready)
        5. Event logging and step population

        Args:
            current_date: Current simulation date
        """
        # DEBUG: Confirm daily compliance is being called

        # Detect acquisition close triggers
        self._detect_acquisition_close_triggers(current_date)

        # Work item advancement
        self._advance_scheduled_work_items(current_date)
        self._resolve_in_progress_work_items(current_date)

        # Add remediation spawning logic
        # Add gating logic and milestone events

    def _detect_acquisition_close_triggers(self, current_date: date) -> None:
        """
        Detect properties that closed acquisition on current_date and create compliance attempts.

        Queries PropertyAcquisitionAttempt for Status='CLOSED' with CloseDate=current_date
        that don't already have a compliance attempt.

        Args:
            current_date: Current simulation date
        """
        # ORDER BY is an invariant-#8 guard (#187): on a same-day multi-close,
        # loop order assigns AttemptID/WorkItemID blocks, and WorkItemID is a
        # component of the work-item content-hash seed — so the order must not
        # ride plan row order. AttemptID = the clustered-PK trailing column.
        query = """
            SELECT
                paa.AttemptID AS AcquisitionAttemptID,
                paa.PropertyID,
                paa.ClosingDate,
                paa.InspectionSeverity,
                paa.EstimatedRepairCost,
                p.YearBuilt
            FROM simulation.PropertyAcquisitionAttempt paa
            INNER JOIN reference.Properties p ON paa.PropertyID = p.PropertyID
            WHERE paa.RunID = ?
                AND paa.AcquisitionSucceeded = 1
                AND paa.ClosingDate = ?
                AND NOT EXISTS (
                    SELECT 1
                    FROM simulation.ComplianceAttempt ca
                    WHERE ca.RunID = paa.RunID
                        AND ca.PropertyID = paa.PropertyID
                        AND ca.ScopeType = 'BUILDING'
                        AND ca.TriggerType = 'ACQUISITION_CLOSE'
                )
            ORDER BY paa.AttemptID
        """

        properties = self.db.execute_query(query, (self.run_id, current_date))

        # DEBUG

        for prop in properties:
            # Create building-level compliance attempt
            self._create_building_compliance_attempt(
                prop.PropertyID,
                current_date,
                prop.YearBuilt,
                prop.InspectionSeverity,
                prop.EstimatedRepairCost
            )

    def _create_building_compliance_attempt(
        self,
        property_id: int,
        close_date: date,
        year_built: int,
        inspection_severity: Optional[str],
        estimated_repair_cost: Optional[Decimal]
    ) -> int:
        """
        Create building-level compliance attempt on acquisition close.

        Args:
            property_id: Property ID
            close_date: Acquisition close date
            year_built: Property year built (for lead compliance)
            inspection_severity: Acquisition inspection severity (CLEAN/MINOR/MODERATE/MAJOR)
            estimated_repair_cost: Acquisition estimated repair cost

        Returns:
            AttemptID for the created compliance attempt
        """
        attempt_id = self._get_next_attempt_id()

        # Insert compliance attempt
        insert_query = """
            INSERT INTO simulation.ComplianceAttempt (
                RunID, AttemptID, PropertyID, ScopeType, TriggerType, UnitID,
                StartDate, CompletedDate, IsActive, NextActionDate,
                BuildingOnlineDate, UnitReadyDate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        self.db.execute_non_query(
            insert_query,
            (
                self.run_id,
                attempt_id,
                property_id,
                'BUILDING',
                'ACQUISITION_CLOSE',
                None,  # UnitID is NULL for building-level
                close_date,
                None,  # CompletedDate
                1,  # IsActive
                close_date + timedelta(days=1),  # NextActionDate (first work items start tomorrow)
                None,  # BuildingOnlineDate (milestone, set when building goes online)
                None   # UnitReadyDate (not used for building-level attempts)
            )
        )

        # Record compliance step
        self._record_compliance_step(
            attempt_id,
            None,  # No work item yet
            'COMPLIANCE_STARTED',
            close_date,
            f"Building compliance started for Property {property_id} on acquisition close"
        )

        print(f"  {close_date}: Created building compliance attempt (Property {property_id}, Attempt {attempt_id})")

        # Create initial building-level work items
        self._create_initial_building_work_items(
            attempt_id,
            property_id,
            close_date,
            year_built
        )

        # Create due diligence remediation if needed
        if estimated_repair_cost and estimated_repair_cost > 0:
            self._create_due_diligence_remediation(
                attempt_id,
                property_id,
                close_date,
                inspection_severity,
                estimated_repair_cost
            )

        # Create unit-level compliance attempts and work items for all units
        self._create_unit_compliance_attempts(property_id, close_date)

        return attempt_id

    def _create_initial_building_work_items(
        self,
        attempt_id: int,
        property_id: int,
        close_date: date,
        year_built: int
    ) -> None:
        """
        Create initial building-level work items on acquisition close.

        Work items created:
        - LEAD_DOC_REVIEW (pre-1978 only)
        - BUILDING_HABITABILITY_INSPECTION (all properties)

        Args:
            attempt_id: Compliance attempt ID
            property_id: Property ID
            close_date: Acquisition close date
            year_built: Property year built
        """
        # Check if pre-1978 (lead compliance required)
        # KD-043: cutoff is a jurisdiction knob (CMPL.LeadCutoffYearBuilt,
        # sentinel 0 disables), no longer a hardcoded 1978 literal.
        is_pre_1978 = self._is_pre_lead_cutoff(year_built)

        # Lead chain — KD-193 store-and-forward. If the property's lead was
        # ASSESSED at due diligence (the normal path once the assessor is
        # wired), REALIZE the stored outcome: the doc-review + hazard
        # determination and the assessment cost already ran pre-closing, so
        # post-closing we do ONLY the abatement WORK at the assessed cost
        # (occupancy-blocking preserved; cost READ not drawn; no re-charge).
        # If un-assessed (legacy / no assessor wired), fall back to the
        # original post-closing doc-review chain.
        if is_pre_1978:
            assessed = self._lookup_assessed_lead(property_id)
            if assessed is not None:
                lead_assessed, abate_cost = assessed
                if lead_assessed and abate_cost is not None and Decimal(str(abate_cost)) > 0:
                    self._spawn_realized_lead_abatement(
                        attempt_id, property_id, close_date, Decimal(str(abate_cost)))
                # assessed clean (docs present / no hazard) → no lead work
            else:
                self._spawn_lead_doc_review_chain(attempt_id, property_id, close_date)

        # BUILDING_HABITABILITY_INSPECTION (all properties)
        hab_params = self.parameters['BUILDING_HABITABILITY_INSPECTION']
        work_item_id = self._get_next_work_item_id()
        scheduled_date = close_date + timedelta(days=self.offset_habitability_days)

        insert_query = """
            INSERT INTO simulation.ComplianceWorkItem (
                RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
                ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
                PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        self.db.execute_non_query(
            insert_query,
            (
                self.run_id, work_item_id, attempt_id, property_id, None,
                'BUILDING_HABITABILITY_INSPECTION', 'SCHEDULED', scheduled_date, None, None,
                hab_params.cost_min, None, None, 0, None, None, None
            )
        )

        print(f"    Created BUILDING_HABITABILITY_INSPECTION (WorkItem {work_item_id}, scheduled {scheduled_date})")

    def _lookup_assessed_lead(self, property_id: int):
        """KD-193 store-and-forward source: the pre-closing lead assessment for
        the WINNING acquisition attempt of this property. Returns
        (lead_assessed: bool, abatement_cost: Decimal|None), or None when no
        assessed attempt exists (legacy / no assessor wired → the original
        post-closing chain runs)."""
        row = self.db.execute_query(
            "SELECT LeadAssessed, AssessedLeadCost FROM simulation.PropertyAcquisitionAttempt "
            "WHERE RunID = ? AND PropertyID = ? AND AcquisitionSucceeded = 1 AND LeadAssessed = 1",
            (self.run_id, property_id))
        if not row:
            return None
        return bool(row[0][0]), row[0][1]

    def _spawn_realized_lead_abatement(self, attempt_id: int, property_id: int,
                                       close_date: date, cost: Decimal) -> None:
        """KD-193: the post-closing LEAD_ABATEMENT that REALIZES the
        due-diligence assessment. CostEstimate = the assessed cost (READ, never
        re-drawn — assessed == abated). Duration IS drawn here: the
        occupancy-delay is post-closing work, not part of the pre-closing cost
        decision. As a standalone remediation it pays its CostEstimate on start
        and blocks the building until COMPLETED (occupancy channel preserved).
        The doc-review + risk-assessment determination + assessment cost already
        ran pre-closing, so they are NOT re-spawned / re-charged here."""
        abate_params = self.parameters['LEAD_ABATEMENT']
        work_item_id = self._get_next_work_item_id()
        scheduled_date = close_date + timedelta(days=self.offset_doc_review_days)
        rng_dur = self._get_work_item_rng(
            property_id, None, 'LEAD_ABATEMENT_DURATION', scheduled_date, work_item_id)
        duration_days = int(abate_params.duration_min_days
                            + rng_dur * (abate_params.duration_max_days - abate_params.duration_min_days))
        self.db.execute_non_query(
            """INSERT INTO simulation.ComplianceWorkItem (
                 RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
                 ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
                 PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            # Severity='MAJOR' matches the legacy chain-spawned LEAD_ABATEMENT
            # (via _spawn_remediation_from_failed_inspection) so the frozen corpus
            # stores one consistent value for the same real-world event. Purely a
            # stored label here — the cost is the READ assessed value, not a
            # severity-derived recompute, and KD-043 set lead's severity multiplier
            # to 1.0. Clearance never branches on Severity for LEAD_ABATEMENT.
            (self.run_id, work_item_id, attempt_id, property_id, None,
             'LEAD_ABATEMENT', 'SCHEDULED', scheduled_date, None, None,
             cost.quantize(Decimal('0.01')), None, None, duration_days, None, 'MAJOR', None))
        print(f"    Created LEAD_ABATEMENT (realized from DD assessment, WorkItem "
              f"{work_item_id}, cost ${cost:,.2f}, scheduled {scheduled_date})")

    def _spawn_lead_doc_review_chain(self, attempt_id: int, property_id: int,
                                     close_date: date) -> None:
        """The original post-closing lead chain (LEAD_DOC_REVIEW → …). KD-193
        legacy fallback for un-assessed properties (no assessor wired — tests /
        legacy). Behavior unchanged from pre-KD-193."""
        lead_doc_params = self.parameters['LEAD_DOC_REVIEW']
        work_item_id = self._get_next_work_item_id()
        scheduled_date = close_date + timedelta(days=self.offset_doc_review_days)
        rng_value = self._get_work_item_rng(
            property_id, None, 'LEAD_DOC_REVIEW', scheduled_date, work_item_id)
        duration_days = int(lead_doc_params.duration_min_days
                            + rng_value * (lead_doc_params.duration_max_days - lead_doc_params.duration_min_days))
        self.db.execute_non_query(
            """INSERT INTO simulation.ComplianceWorkItem (
                 RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
                 ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
                 PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, work_item_id, attempt_id, property_id, None,
             'LEAD_DOC_REVIEW', 'SCHEDULED', scheduled_date, None, None,
             Decimal('0.00'), None, None, duration_days, None, None, None))
        print(f"    Created LEAD_DOC_REVIEW (WorkItem {work_item_id}, scheduled {scheduled_date})")

    def _create_due_diligence_remediation(
        self,
        attempt_id: int,
        property_id: int,
        close_date: date,
        inspection_severity: Optional[str],
        estimated_repair_cost: Decimal
    ) -> None:
        """
        Create due diligence remediation work item from acquisition inspection.

        Args:
            attempt_id: Compliance attempt ID
            property_id: Property ID
            close_date: Acquisition close date
            inspection_severity: Acquisition inspection severity (CLEAN/MINOR/MODERATE/MAJOR)
            estimated_repair_cost: Acquisition estimated repair cost
        """
        work_item_id = self._get_next_work_item_id()
        scheduled_date = close_date + timedelta(days=self.offset_due_diligence_days)

        # The acquisition pipeline supplies title-case
        # severities ('Major') while compliance keys are uppercase — normalize
        # ONCE here and use the normalized value for both the band lookup and
        # the stored Severity (the MODERATE/MAJOR blocking SQL must not depend
        # on case-insensitive collation). CLEAN maps to
        # the MINOR band explicitly (live branch, must not block).
        severity_norm = (inspection_severity or 'CLEAN').upper()
        duration_min, duration_max = self.dd_duration_bands.get(
            severity_norm, self.dd_duration_bands['MINOR'])

        # Sample duration from range
        rng_value = self._get_work_item_rng(
            property_id, None, 'DUE_DILIGENCE_REMEDIATION',
            scheduled_date, work_item_id
        )
        duration_days = int(duration_min + rng_value * (duration_max - duration_min))

        # Sample cost multiplier: Base + [0, Span] from the CMPL knobs
        rng_value_cost = self._get_work_item_rng(
            property_id, None, f'DUE_DILIGENCE_REMEDIATION_COST',
            scheduled_date, work_item_id
        )
        cost_multiplier = Decimal(str(self.dd_cost_mult_base + rng_value_cost * self.dd_cost_mult_span))
        cost_estimate = estimated_repair_cost * cost_multiplier

        insert_query = """
            INSERT INTO simulation.ComplianceWorkItem (
                RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
                ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
                PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        self.db.execute_non_query(
            insert_query,
            (
                self.run_id, work_item_id, attempt_id, property_id, None,
                'DUE_DILIGENCE_REMEDIATION', 'SCHEDULED', scheduled_date, None, None,
                cost_estimate, None, None, duration_days, None, severity_norm, None
            )
        )

        print(f"    Created DUE_DILIGENCE_REMEDIATION (WorkItem {work_item_id}, severity {severity_norm}, ${cost_estimate:,.2f}, {duration_days} days)")

    def _create_initial_unit_work_items(
        self,
        attempt_id: int,
        property_id: int,
        close_date: date
    ) -> None:
        """
        Create initial unit-level work items for all units in property.

        Work items created per unit:
        - UNIT_SMOKE_CO_INSPECTION (scheduled close_date + 1)
        - UNIT_HABITABILITY_INSPECTION (scheduled close_date + 2)

        Args:
            attempt_id: Compliance attempt ID (building-level, but we track units separately)
            property_id: Property ID
            close_date: Acquisition close date
        """
        # Get all units for this property
        query = """
            SELECT UnitID
            FROM reference.Units
            WHERE PropertyID = ?
            ORDER BY UnitID
        """

        units = self.db.execute_query(query, (property_id,))

        for unit in units:
            unit_id = unit.UnitID

            # UNIT_SMOKE_CO_INSPECTION
            smoke_params = self.parameters['UNIT_SMOKE_CO_INSPECTION']
            work_item_id = self._get_next_work_item_id()
            scheduled_date = close_date + timedelta(days=self.offset_smoke_co_days)

            insert_query = """
                INSERT INTO simulation.ComplianceWorkItem (
                    RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
                    ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
                    PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            self.db.execute_non_query(
                insert_query,
                (
                    self.run_id, work_item_id, attempt_id, property_id, unit_id,
                    'UNIT_SMOKE_CO_INSPECTION', 'SCHEDULED', scheduled_date, None, None,
                    smoke_params.cost_min, None, None, 0, None, None, None
                )
            )

            # UNIT_HABITABILITY_INSPECTION
            hab_params = self.parameters['UNIT_HABITABILITY_INSPECTION']
            work_item_id = self._get_next_work_item_id()
            scheduled_date = close_date + timedelta(days=self.offset_habitability_days)

            self.db.execute_non_query(
                insert_query,
                (
                    self.run_id, work_item_id, attempt_id, property_id, unit_id,
                    'UNIT_HABITABILITY_INSPECTION', 'SCHEDULED', scheduled_date, None, None,
                    hab_params.cost_min, None, None, 0, None, None, None
                )
            )

        print(f"    Created {len(units) * 2} unit-level work items ({len(units)} units)")

    def _create_unit_compliance_attempts(
        self,
        property_id: int,
        close_date: date
    ) -> None:
        """
        Create unit-level compliance attempts and work items for all units in property.

        For each unit:
        1. Create UNIT-level ComplianceAttempt
        2. Create initial unit work items (smoke/CO, habitability)

        Args:
            property_id: Property ID
            close_date: Acquisition close date
        """
        # Get all units for this property
        query = """
            SELECT UnitID
            FROM reference.Units
            WHERE PropertyID = ?
            ORDER BY UnitID
        """
        units = self.db.execute_query(query, (property_id,))

        for unit_row in units:
            unit_id = unit_row.UnitID

            # Create unit-level ComplianceAttempt
            attempt_id = self._get_next_attempt_id()

            insert_attempt = """
                INSERT INTO simulation.ComplianceAttempt (
                    RunID, AttemptID, PropertyID, ScopeType, TriggerType, UnitID,
                    StartDate, CompletedDate, IsActive, NextActionDate,
                    BuildingOnlineDate, UnitReadyDate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            self.db.execute_non_query(
                insert_attempt,
                (
                    self.run_id,
                    attempt_id,
                    property_id,
                    'UNIT',
                    'ACQUISITION_CLOSE',
                    unit_id,
                    close_date,
                    None,  # CompletedDate
                    1,  # IsActive
                    close_date + timedelta(days=1),  # NextActionDate
                    None,  # BuildingOnlineDate (not used for unit-level)
                    None   # UnitReadyDate (milestone, set when unit becomes rentable)
                )
            )

            # Create unit work items for this unit
            self._create_unit_work_items(attempt_id, property_id, unit_id, close_date)

        print(f"    Created {len(units)} unit-level compliance attempts ({len(units) * 2} work items)")

    def _create_unit_work_items(
        self,
        attempt_id: int,
        property_id: int,
        unit_id: int,
        close_date: date
    ) -> None:
        """
        Create initial unit-level work items for a single unit.

        Work items created:
        - UNIT_SMOKE_CO_INSPECTION (1-day turnaround)
        - UNIT_HABITABILITY_INSPECTION (2-day turnaround)

        Args:
            attempt_id: Unit-level compliance attempt ID
            property_id: Property ID
            unit_id: Unit ID
            close_date: Acquisition close date
        """
        insert_query = """
            INSERT INTO simulation.ComplianceWorkItem (
                RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType, Status,
                ScheduledDate, StartDate, CompletedDate, CostEstimate, ActualCost,
                PaidDate, DurationDays, ParentWorkItemID, Severity, Metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        # UNIT_SMOKE_CO_INSPECTION
        smoke_params = self.parameters['UNIT_SMOKE_CO_INSPECTION']
        work_item_id = self._get_next_work_item_id()
        scheduled_date = close_date + timedelta(days=self.offset_smoke_co_days)

        self.db.execute_non_query(
            insert_query,
            (
                self.run_id, work_item_id, attempt_id, property_id, unit_id,
                'UNIT_SMOKE_CO_INSPECTION', 'SCHEDULED', scheduled_date, None, None,
                smoke_params.cost_min, None, None, 0, None, None, None
            )
        )

        # UNIT_HABITABILITY_INSPECTION
        hab_params = self.parameters['UNIT_HABITABILITY_INSPECTION']
        work_item_id = self._get_next_work_item_id()
        scheduled_date = close_date + timedelta(days=self.offset_habitability_days)

        self.db.execute_non_query(
            insert_query,
            (
                self.run_id, work_item_id, attempt_id, property_id, unit_id,
                'UNIT_HABITABILITY_INSPECTION', 'SCHEDULED', scheduled_date, None, None,
                hab_params.cost_min, None, None, 0, None, None, None
            )
        )

    def _record_compliance_step(
        self,
        attempt_id: int,
        work_item_id: Optional[int],
        step_type: str,
        effective_date: date,
        notes: str
    ) -> None:
        """
        Record compliance step in append-only ledger.

        Args:
            attempt_id: Compliance attempt ID
            work_item_id: Work item ID (None for attempt-level steps)
            step_type: Step type (e.g., 'COMPLIANCE_STARTED', 'WORK_ITEM_STARTED')
            effective_date: Effective date for this step
            notes: Narrative notes
        """
        step_id = self._get_next_step_id()

        # Create event for this step
        from event_logger import ActionType, EntityType

        event_id = self.event_logger.log_module_event(
            module_name="ComplianceManager",
            action=ActionType.UPDATE,  # All compliance steps are updates
            message=notes,
            entity_type=EntityType.PROPERTY,
            entity_id=None,  # Will be added when we have a better entity tracking system
            effective_date=effective_date
        )

        # Insert step
        insert_query = """
            INSERT INTO simulation.ComplianceStep (
                RunID, StepID, AttemptID, WorkItemID, StepType, EffectiveDate, Notes, EventID
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        self.db.execute_non_query(
            insert_query,
            (self.run_id, step_id, attempt_id, work_item_id, step_type, effective_date, notes, event_id)
        )

    # ========================================================================
    # Work Item Advancement
    # ========================================================================

    def _advance_scheduled_work_items(self, current_date: date) -> None:
        """
        Advance work items from SCHEDULED to IN_PROGRESS or directly to resolution.

        Same-day items (DurationDays = 0):
        - Inspections: SCHEDULED → PASSED/FAILED (same day)
        - Remediation: SCHEDULED → COMPLETED (same day)

        Multi-day items (DurationDays > 0):
        - SCHEDULED → IN_PROGRESS on ScheduledDate

        Args:
            current_date: Current simulation date
        """
        query = """
            SELECT WorkItemID, AttemptID, PropertyID, UnitID, WorkType,
                   ScheduledDate, DurationDays, CostEstimate
            FROM simulation.ComplianceWorkItem
            WHERE RunID = ? AND Status = 'SCHEDULED' AND ScheduledDate <= ?
            ORDER BY ScheduledDate, WorkItemID
        """

        work_items = self.db.execute_query(query, (self.run_id, current_date))

        if not work_items:
            return


        for item in work_items:
            # Determine if same-day or multi-day
            is_same_day = (item.DurationDays is None or item.DurationDays == 0)

            if is_same_day:
                # Same-day items: resolve immediately
                self._resolve_same_day_work_item(item, current_date)
            else:
                # Multi-day items: transition to IN_PROGRESS
                self._start_multi_day_work_item(item, current_date)

    def _start_multi_day_work_item(self, item, current_date: date) -> None:
        """
        Start a multi-day work item (SCHEDULED → IN_PROGRESS).

        For remediation, pay cost on start day.

        Args:
            item: Work item row
            current_date: Current simulation date
        """
        # Update status to IN_PROGRESS
        update_query = """
            UPDATE simulation.ComplianceWorkItem
            SET Status = 'IN_PROGRESS', StartDate = ?
            WHERE RunID = ? AND WorkItemID = ?
        """

        self.db.execute_non_query(update_query, (current_date, self.run_id, item.WorkItemID))

        print(f"    {current_date}: Work item {item.WorkItemID} ({item.WorkType}) started (IN_PROGRESS)")

        # Determine if inspection or remediation
        is_inspection = self._is_inspection(item.WorkType)

        if not is_inspection:
            # Remediation: pay cost on start day
            self._pay_work_item_cost(item.WorkItemID, item.CostEstimate, current_date, item.WorkType, item.PropertyID)

        # Record step
        self._record_compliance_step(
            item.AttemptID,
            item.WorkItemID,
            'WORK_ITEM_STARTED',
            current_date,
            f"{item.WorkType} started for Property {item.PropertyID}" +
            (f", Unit {item.UnitID}" if item.UnitID else "")
        )

    def _resolve_same_day_work_item(self, item, current_date: date) -> None:
        """
        Resolve a same-day work item (SCHEDULED → PASSED/FAILED/COMPLETED).

        Inspections: resolve to PASSED or FAILED based on FailChanceBase/PassChanceOverride
        Remediation: resolve to COMPLETED (always succeeds in v1)

        For inspections, pay cost on resolution day.

        Args:
            item: Work item row
            current_date: Current simulation date
        """
        # Determine if inspection or remediation
        is_inspection = self._is_inspection(item.WorkType)

        if is_inspection:
            # Inspection: determine PASSED or FAILED
            outcome = self._determine_inspection_outcome(item)
        else:
            # Remediation: always COMPLETED
            outcome = 'COMPLETED'

        # Update status
        update_query = """
            UPDATE simulation.ComplianceWorkItem
            SET Status = ?, StartDate = ?, CompletedDate = ?
            WHERE RunID = ? AND WorkItemID = ?
        """

        self.db.execute_non_query(
            update_query,
            (outcome, current_date, current_date, self.run_id, item.WorkItemID)
        )

        print(f"    {current_date}: Work item {item.WorkItemID} ({item.WorkType}) resolved ({outcome})")

        # Pay cost (inspections paid on resolution day)
        if is_inspection:
            self._pay_work_item_cost(item.WorkItemID, item.CostEstimate, current_date, item.WorkType, item.PropertyID)
        else:
            # Remediation cost already paid on start day (but same-day, so pay now)
            self._pay_work_item_cost(item.WorkItemID, item.CostEstimate, current_date, item.WorkType, item.PropertyID)

        # Record step
        step_type = f'INSPECTION_{outcome}' if is_inspection else 'REMEDIATION_COMPLETED'
        self._record_compliance_step(
            item.AttemptID,
            item.WorkItemID,
            step_type,
            current_date,
            f"{item.WorkType} {outcome.lower()} for Property {item.PropertyID}" +
            (f", Unit {item.UnitID}" if item.UnitID else "")
        )

        # Spawn remediation if inspection failed
        if is_inspection and outcome == 'FAILED':
            self._spawn_remediation_from_failed_inspection(item, current_date)

        # Check milestone gates after work item resolution
        self._check_all_milestones(item.PropertyID, current_date)

    def _resolve_in_progress_work_items(self, current_date: date) -> None:
        """
        Resolve work items from IN_PROGRESS to PASSED/FAILED/COMPLETED.

        Query work items with Status=IN_PROGRESS where current_date >= StartDate + DurationDays.

        Args:
            current_date: Current simulation date
        """
        query = """
            SELECT WorkItemID, AttemptID, PropertyID, UnitID, WorkType,
                   ScheduledDate, StartDate, DurationDays, CostEstimate
            FROM simulation.ComplianceWorkItem
            WHERE RunID = ? AND Status = 'IN_PROGRESS'
              AND DATEADD(day, DurationDays, StartDate) <= ?
            ORDER BY StartDate, WorkItemID
        """

        work_items = self.db.execute_query(query, (self.run_id, current_date))

        if not work_items:
            return


        for item in work_items:
            # Determine if inspection or remediation
            is_inspection = self._is_inspection(item.WorkType)

            if is_inspection:
                # Inspection: determine PASSED or FAILED
                outcome = self._determine_inspection_outcome(item)
            else:
                # Remediation: always COMPLETED
                outcome = 'COMPLETED'

            # Update status
            update_query = """
                UPDATE simulation.ComplianceWorkItem
                SET Status = ?, CompletedDate = ?
                WHERE RunID = ? AND WorkItemID = ?
            """

            self.db.execute_non_query(
                update_query,
                (outcome, current_date, self.run_id, item.WorkItemID)
            )

            print(f"    {current_date}: Work item {item.WorkItemID} ({item.WorkType}) completed ({outcome})")

            # Pay cost (inspections paid on resolution day)
            if is_inspection:
                self._pay_work_item_cost(item.WorkItemID, item.CostEstimate, current_date, item.WorkType, item.PropertyID)

            # Record step
            step_type = f'INSPECTION_{outcome}' if is_inspection else 'REMEDIATION_COMPLETED'
            self._record_compliance_step(
                item.AttemptID,
                item.WorkItemID,
                step_type,
                current_date,
                f"{item.WorkType} {outcome.lower()} for Property {item.PropertyID}" +
                (f", Unit {item.UnitID}" if item.UnitID else "")
            )

            # Spawn remediation if inspection failed
            if is_inspection and outcome == 'FAILED':
                self._spawn_remediation_from_failed_inspection(item, current_date)

            # Check milestone gates after work item resolution
            self._check_all_milestones(item.PropertyID, current_date)

    def _determine_inspection_outcome(self, item) -> str:
        """
        Determine inspection outcome (PASSED or FAILED) using deterministic RNG.

        Uses FailChanceBase or PassChanceOverride from parameters.

        Args:
            item: Work item row

        Returns:
            'PASSED' or 'FAILED'
        """
        params = self.parameters[item.WorkType]

        # Get pass probability
        if params.pass_chance_override is not None:
            pass_chance = float(params.pass_chance_override)
        else:
            pass_chance = 1.0 - float(params.fail_chance_base)

        # Get deterministic RNG value
        rng_value = self._get_work_item_rng(
            item.PropertyID,
            item.UnitID,
            item.WorkType,
            item.ScheduledDate,
            item.WorkItemID
        )

        # Determine outcome
        if rng_value < pass_chance:
            return 'PASSED'
        else:
            return 'FAILED'

    def _pay_work_item_cost(self, work_item_id: int, cost: Decimal, current_date: date,
                            work_type: str, property_id: int) -> None:
        """
        Pay work item cost: persist ActualCost/PaidDate AND debit Cash.

        rulings:
        per work item, at PaidDate (= current_date — when cash actually
        leaves), Cash-only unprotected path (canon: never CSF; Cash may ride
        negative like every unprotected expense — the honest cost lands and
        the next protected checkpoint reads the lower buffer), compound
        expense string with the INSPECTION/REMEDIATION split so the fund
        stream can answer "what did compliance honesty cost — diligence vs
        repair" without a second query. $0 items (e.g. LEAD_DOC_REVIEW)
        persist their row but skip the ledger (no empty rows).
        """
        # Update ComplianceWorkItem with actual cost and paid date
        update_query = """
            UPDATE simulation.ComplianceWorkItem
            SET ActualCost = ?, PaidDate = ?
            WHERE RunID = ? AND WorkItemID = ?
        """

        self.db.execute_non_query(
            update_query,
            (cost, current_date, self.run_id, work_item_id)
        )

        if cost > 0:
            is_inspection = self._is_inspection(work_type)
            kind = 'INSPECTION' if is_inspection else 'REMEDIATION'
            self.fund_manager.process_expense(
                run_id=self.run_id,
                expense_amount=cost,
                ledger_date=current_date,
                expense_type=(
                    f"COMPLIANCE_{kind}: {work_type} "
                    f"(PropertyID={property_id}, WorkItemID={work_item_id})"
                ),
            )

    # ============================================================================
    # Remediation Spawning
    # ============================================================================

    # Remediation mapping: inspection failure → remediation work type
    REMEDIATION_MAPPING = {
        'LEAD_DOC_REVIEW': 'LEAD_RISK_ASSESSMENT',  # Special case: spawn assessment
        'LEAD_RISK_ASSESSMENT': 'LEAD_ABATEMENT',   # Spawn abatement
        'UNIT_SMOKE_CO_INSPECTION': 'UNIT_SMOKE_CO_REMEDIATION',
        'UNIT_HABITABILITY_INSPECTION': 'UNIT_REMEDIATION',
        'BUILDING_HABITABILITY_INSPECTION': 'BUILDING_REMEDIATION',
    }

    def _spawn_remediation_from_failed_inspection(
        self,
        failed_item,
        current_date: date
    ) -> None:
        """
        Spawn remediation work item when inspection fails.

        Steps:
        1. Map failed inspection → remediation work type
        2. Get parameters from ComplianceParameters
        3. Assign severity (deterministic RNG)
        4. Calculate duration (sample from min/max based on severity)
        5. Calculate cost (base cost × severity multiplier × RNG)
        6. Sample contractor lag (3-14 days RNG)
        7. Create work item with ParentWorkItemID = failed inspection

        Args:
            failed_item: Failed inspection work item
            current_date: Current simulation date (inspection failed date)
        """
        # Check if this inspection spawns remediation
        if failed_item.WorkType not in self.REMEDIATION_MAPPING:
            # No remediation for this work type
            return

        remediation_type = self.REMEDIATION_MAPPING[failed_item.WorkType]

        # Get parameters for remediation work type
        if remediation_type not in self.parameters:
            print(f"      WARNING: No parameters for remediation type {remediation_type}")
            return

        # Assign severity using deterministic RNG
        severity = self._assign_remediation_severity(failed_item, remediation_type)

        # Calculate duration and cost based on severity
        duration_days, cost_estimate = self._calculate_remediation_params(
            failed_item,
            remediation_type,
            severity
        )

        # Sample contractor lag (3-14 days RNG)
        contractor_lag = self._sample_contractor_lag(failed_item, remediation_type)

        # Calculate scheduled date (failed date + contractor lag)
        scheduled_date = current_date + timedelta(days=contractor_lag)

        # Get next work item ID
        work_item_id = self._get_next_work_item_id()

        # Insert remediation work item
        insert_query = """
            INSERT INTO simulation.ComplianceWorkItem (
                RunID, WorkItemID, AttemptID, PropertyID, UnitID, WorkType,
                ScheduledDate, DurationDays, CostEstimate, Severity,
                Status, ParentWorkItemID
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', ?)
        """

        self.db.execute_non_query(insert_query, (
            self.run_id,
            work_item_id,
            failed_item.AttemptID,
            failed_item.PropertyID,
            failed_item.UnitID,
            remediation_type,
            scheduled_date,
            duration_days,
            cost_estimate,
            severity,
            failed_item.WorkItemID  # ParentWorkItemID
        ))

        print(f"      -> Spawned {remediation_type} (WorkItem {work_item_id}) " +
              f"for {current_date} + {contractor_lag} days = {scheduled_date}, " +
              f"Severity={severity}, Duration={duration_days} days, Cost=${cost_estimate:,.2f}")

        # Record step
        self._record_compliance_step(
            failed_item.AttemptID,
            work_item_id,
            'REMEDIATION_SPAWNED',
            current_date,
            f"{remediation_type} spawned from failed {failed_item.WorkType} " +
            f"for Property {failed_item.PropertyID}" +
            (f", Unit {failed_item.UnitID}" if failed_item.UnitID else "") +
            f" (Severity={severity}, +{contractor_lag} days contractor lag)"
        )

    def _assign_remediation_severity(self, failed_item, remediation_type: str) -> str:
        """
        Assign severity for spawned remediation work item using deterministic RNG.

        Severity distribution by work type (splits are CMPL registry knobs;
        seeded 50/50 and 60/40):
        - UNIT_SMOKE_CO_REMEDIATION: always MINOR
        - UNIT_REMEDIATION: MINOR (UnitRemediationMinorPct) | else MODERATE
        - BUILDING_REMEDIATION: MODERATE (BuildingRemediationModeratePct) | else MAJOR
        - LEAD_ABATEMENT: always MAJOR

        Args:
            failed_item: Failed inspection work item
            remediation_type: Remediation work type

        Returns:
            Severity string ('MINOR' | 'MODERATE' | 'MAJOR')
        """
        # Get deterministic RNG value
        rng_value = self._get_work_item_rng(
            failed_item.PropertyID,
            failed_item.UnitID,
            remediation_type,  # Use remediation type for RNG (not inspection type)
            failed_item.ScheduledDate,
            failed_item.WorkItemID
        )

        # Assign severity based on work type
        if remediation_type == 'UNIT_SMOKE_CO_REMEDIATION':
            return 'MINOR'
        elif remediation_type == 'LEAD_ABATEMENT':
            return 'MAJOR'
        elif remediation_type == 'UNIT_REMEDIATION':
            # MINOR (50%) | MODERATE (50%)
            return 'MINOR' if rng_value < self.unit_remediation_minor_pct else 'MODERATE'
        elif remediation_type == 'BUILDING_REMEDIATION':
            # MODERATE (60%) | MAJOR (40%)
            return 'MODERATE' if rng_value < self.building_remediation_moderate_pct else 'MAJOR'
        elif remediation_type == 'LEAD_RISK_ASSESSMENT':
            # Lead risk assessment is inspection, not remediation (default MINOR)
            return 'MINOR'
        else:
            # Default: MINOR
            return 'MINOR'

    def _calculate_remediation_params(
        self,
        failed_item,
        remediation_type: str,
        severity: str
    ) -> Tuple[int, Decimal]:
        """
        Calculate duration and cost for remediation based on severity.

        Severity multipliers: CMPL.SeverityMult* registry knobs
        (seeded MINOR 1.0x / MODERATE 1.5x / MAJOR 3.0x).

        Args:
            failed_item: Failed inspection work item
            remediation_type: Remediation work type
            severity: Severity string ('MINOR' | 'MODERATE' | 'MAJOR').
                IGNORED for LEAD_RISK_ASSESSMENT / LEAD_ABATEMENT — lead
                cost is unit-scaled at multiplier 1.0, not severity-scaled
                (KD-043).

        Returns:
            Tuple of (duration_days, cost_estimate)
        """
        params = self.parameters[remediation_type]

        # KD-043 (ratified): lead items are UNIT-SCALED at multiplier 1.0 —
        # base + per-unit IS the full job (anchored to the MassHousing GTLO
        # ladder; evidence_base §11). Assessment is deterministic
        # (base + per-unit); abatement draws a uniform ±band around its
        # unit-scaled center. Durations keep the ComplianceParameters bands
        # (the migration folded the former ×3 severity effect into the
        # LEAD_ABATEMENT duration band itself, so effective blocking time is
        # unchanged — the occupancy-delay channel is real and priced).
        if remediation_type in ('LEAD_RISK_ASSESSMENT', 'LEAD_ABATEMENT'):
            units = self._property_unit_count(failed_item.PropertyID)
            rng_duration = self._get_work_item_rng(
                failed_item.PropertyID, failed_item.UnitID,
                f"{remediation_type}_DURATION", failed_item.ScheduledDate,
                failed_item.WorkItemID)
            duration_days = params.duration_min_days + int(
                rng_duration * (params.duration_max_days - params.duration_min_days))
            if remediation_type == 'LEAD_RISK_ASSESSMENT':
                cost = Decimal(str(self.lead_assess_cost_base)) + \
                    Decimal(str(self.lead_assess_cost_per_unit)) * max(0, units - 1)
            else:
                center = Decimal(str(self.lead_abate_cost_base)) + \
                    Decimal(str(self.lead_abate_cost_per_unit)) * max(0, units - 1)
                rng_cost = self._get_work_item_rng(
                    failed_item.PropertyID, failed_item.UnitID,
                    f"{remediation_type}_COST", failed_item.ScheduledDate,
                    failed_item.WorkItemID)
                band = Decimal(str(self.lead_abate_cost_band_pct))
                factor = (Decimal('1') - band) + Decimal(str(rng_cost)) * 2 * band
                cost = center * factor
            return duration_days, cost.quantize(Decimal('0.01'))

        # Severity multipliers from the CMPL knobs (unknown severity -> MINOR)
        multiplier = self.severity_multipliers.get(severity, self.severity_multipliers['MINOR'])

        # Get two RNG values (one for duration, one for cost)
        rng_duration = self._get_work_item_rng(
            failed_item.PropertyID,
            failed_item.UnitID,
            f"{remediation_type}_DURATION",
            failed_item.ScheduledDate,
            failed_item.WorkItemID
        )

        rng_cost = self._get_work_item_rng(
            failed_item.PropertyID,
            failed_item.UnitID,
            f"{remediation_type}_COST",
            failed_item.ScheduledDate,
            failed_item.WorkItemID
        )

        # Calculate duration (sample from min/max, then apply severity multiplier)
        base_duration = params.duration_min_days + int(
            rng_duration * (params.duration_max_days - params.duration_min_days)
        )
        duration_days = int(base_duration * multiplier)

        # Calculate cost (sample from min/max, then apply severity multiplier)
        base_cost_range = params.cost_max - params.cost_min
        base_cost = params.cost_min + (base_cost_range * Decimal(str(rng_cost)))
        cost_estimate = base_cost * Decimal(str(multiplier))

        return duration_days, cost_estimate

    def _sample_contractor_lag(self, failed_item, remediation_type: str) -> int:
        """
        Sample contractor lag (3-14 days) using deterministic RNG.

        Args:
            failed_item: Failed inspection work item
            remediation_type: Remediation work type

        Returns:
            Contractor lag in days (3-14)
        """
        params = self.parameters[remediation_type]

        # Get RNG value
        rng_value = self._get_work_item_rng(
            failed_item.PropertyID,
            failed_item.UnitID,
            f"{remediation_type}_LAG",
            failed_item.ScheduledDate,
            failed_item.WorkItemID
        )

        # Sample from ContractorLagMinDays to ContractorLagMaxDays
        lag_range = params.contractor_lag_max_days - params.contractor_lag_min_days
        contractor_lag = params.contractor_lag_min_days + int(rng_value * lag_range)

        return contractor_lag

    # ========================================================================
    # Gating Logic & Milestone Events
    # ========================================================================

    def _get_blocking_building_work_items(
        self,
        property_id: int
    ) -> List:
        """
        Get all blocking building-level work items that are not yet cleared.

        A work item is "cleared" when (KD-043 clearance fix):
        - Status = 'PASSED' (inspection passed)
        - Status = 'COMPLETED' (remediation completed)
        - Status = 'FAILED' AND its spawn chain terminates cleared — i.e. ANY
          descendant (recursive) has Status PASSED or COMPLETED. In a linear
          spawn chain only the terminal node can be PASSED/COMPLETED, so this
          is exactly "the chain resolved": a failed LEAD_DOC_REVIEW clears
          when its LEAD_RISK_ASSESSMENT passes (no hazard) OR when the chain's
          LEAD_ABATEMENT completes. The pre-KD-043 rule (direct child
          COMPLETED only) deadlocked two-stage chains — it was accidentally
          satisfied by the mis-classification KD-043's reachability fix
          removes. Value-neutral for every one-hop chain (a COMPLETED direct
          remediation is a COMPLETED descendant).

        Special case (DUE_DILIGENCE_REMEDIATION):
        - Only blocks if Severity >= 'MODERATE'
        - MINOR severity does NOT block building online

        Returns:
            List of blocking work items not yet cleared
        """
        query = """
            WITH descendants AS (
                SELECT root.WorkItemID AS RootID, child.WorkItemID, child.Status
                FROM simulation.ComplianceWorkItem root
                JOIN simulation.ComplianceWorkItem child
                  ON child.RunID = root.RunID
                 AND child.ParentWorkItemID = root.WorkItemID
                WHERE root.RunID = ? AND root.PropertyID = ?
                  AND root.UnitID IS NULL  -- only building-level roots are probed
                UNION ALL
                SELECT d.RootID, c.WorkItemID, c.Status
                FROM simulation.ComplianceWorkItem c
                JOIN descendants d ON c.ParentWorkItemID = d.WorkItemID
                WHERE c.RunID = ?
            )
            SELECT wi.*
            FROM simulation.ComplianceWorkItem wi
            WHERE wi.RunID = ?
                AND wi.PropertyID = ?
                AND wi.UnitID IS NULL  -- Building-level only
                AND wi.Status NOT IN ('PASSED', 'COMPLETED')  -- Not yet cleared
                AND NOT EXISTS (
                    -- KD-043: cleared when the spawn chain terminates cleared
                    SELECT 1 FROM descendants d
                    WHERE d.RootID = wi.WorkItemID
                        AND d.Status IN ('PASSED', 'COMPLETED')
                )
                AND (
                    -- Normal blocking items: block unconditionally
                    wi.WorkType != 'DUE_DILIGENCE_REMEDIATION'
                    OR
                    -- DUE_DILIGENCE_REMEDIATION: only block if severity >= MODERATE
                    (wi.WorkType = 'DUE_DILIGENCE_REMEDIATION' AND wi.Severity IN ('MODERATE', 'MAJOR'))
                )
        """
        results = self.db.execute_query(
            query, (self.run_id, property_id, self.run_id, self.run_id, property_id))
        return results if results else []

    def _get_blocking_unit_work_items(
        self,
        property_id: int,
        unit_id: int
    ) -> List:
        """
        Get all blocking unit-level work items that are not yet cleared.

        Clearance semantics identical to the building-level query (KD-043):
        PASSED / COMPLETED, or a FAILED inspection whose spawn chain
        terminates cleared (recursive descendant PASSED or COMPLETED).

        Returns:
            List of blocking work items not yet cleared
        """
        query = """
            WITH descendants AS (
                SELECT root.WorkItemID AS RootID, child.WorkItemID, child.Status
                FROM simulation.ComplianceWorkItem root
                JOIN simulation.ComplianceWorkItem child
                  ON child.RunID = root.RunID
                 AND child.ParentWorkItemID = root.WorkItemID
                WHERE root.RunID = ? AND root.PropertyID = ? AND root.UnitID = ?
                UNION ALL
                SELECT d.RootID, c.WorkItemID, c.Status
                FROM simulation.ComplianceWorkItem c
                JOIN descendants d ON c.ParentWorkItemID = d.WorkItemID
                WHERE c.RunID = ?
            )
            SELECT wi.*
            FROM simulation.ComplianceWorkItem wi
            WHERE wi.RunID = ?
                AND wi.PropertyID = ?
                AND wi.UnitID = ?
                AND wi.Status NOT IN ('PASSED', 'COMPLETED')  -- Not yet cleared
                AND NOT EXISTS (
                    -- KD-043: cleared when the spawn chain terminates cleared
                    SELECT 1 FROM descendants d
                    WHERE d.RootID = wi.WorkItemID
                        AND d.Status IN ('PASSED', 'COMPLETED')
                )
        """
        results = self.db.execute_query(
            query,
            (self.run_id, property_id, unit_id, self.run_id,
             self.run_id, property_id, unit_id))
        return results if results else []

    def is_building_online(
        self,
        property_id: int
    ) -> bool:
        """
        Check if building is online (ready for units to become rentable).

        Building is online when:
        1. All blocking building-level work items are cleared
        2. At least one unit is rentable (has all unit work items cleared)

        Returns:
            True if building is online, False otherwise
        """
        # Check if any blocking building-level work items remain
        blocking_building_items = self._get_blocking_building_work_items(property_id)
        if blocking_building_items:
            for item in blocking_building_items:
                print(f"            - WorkItem {item.WorkItemID}: {item.WorkType} (Status={item.Status})")
            return False

        # Check if at least one unit is rentable
        # Get all units for this property from ComplianceAttempt
        query = """
            SELECT DISTINCT UnitID
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
                AND PropertyID = ?
                AND UnitID IS NOT NULL
        """
        units = self.db.execute_query(query, (self.run_id, property_id))

        if not units:
            return False  # No units found


        # Check if at least one unit has no blocking items
        for unit_row in units:
            unit_id = unit_row.UnitID
            blocking_unit_items = self._get_blocking_unit_work_items(property_id, unit_id)
            if not blocking_unit_items:
                # This unit is rentable -> building can be online
                return True

        return False  # No units are rentable yet

    def is_unit_rentable(
        self,
        property_id: int,
        unit_id: int
    ) -> bool:
        """
        Check if unit is rentable.

        Unit is rentable when:
        1. Building is online (building-level items cleared AND >=1 unit rentable)
        2. All unit-level blocking work items are cleared

        Returns:
            True if unit is rentable, False otherwise
        """
        # First check if building is online
        if not self.is_building_online(property_id):
            return False

        # Check if this specific unit has any blocking items
        blocking_unit_items = self._get_blocking_unit_work_items(property_id, unit_id)
        return len(blocking_unit_items) == 0

    def _check_and_emit_building_online(
        self,
        property_id: int,
        current_date: date
    ) -> None:
        """
        Check if building is online and emit BUILDING_ONLINE event if not already emitted.

        Uses BuildingOnlineDate for idempotency.

        Steps:
        1. Check if building is online
        2. Query building ComplianceAttempt: if BuildingOnlineDate IS NULL, proceed
        3. Emit BUILDING_ONLINE event
        4. UPDATE ComplianceAttempt SET BuildingOnlineDate = current_date
        5. Log ComplianceStep
        """
        # Check if building is online
        is_online = self.is_building_online(property_id)
        if not is_online:
            return

        # Get building-level ComplianceAttempt (UnitID IS NULL)
        query = """
            SELECT AttemptID, BuildingOnlineDate
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
                AND PropertyID = ?
                AND UnitID IS NULL
        """
        result = self.db.execute_query(query, (self.run_id, property_id))

        if not result:
            print(f"      WARNING: No building-level ComplianceAttempt found for PropertyID {property_id}")
            return

        building_attempt = result[0]

        # Check if already emitted (idempotency via BuildingOnlineDate)
        if building_attempt.BuildingOnlineDate is not None:
            return  # Already emitted

        # Emit BUILDING_ONLINE event using EventType.SIMULATION
        from event_logger import EventType, EntityType, ActionType

        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.PROPERTY,
            entity_id=property_id,
            action_type=ActionType.COMPLETE,
            metadata=f'BUILDING_ONLINE: PropertyID {property_id} - all building-level gates cleared, at least one unit rentable'
        )

        # Check if event logging failed
        if event_id is None:
            print(f"      ERROR: Failed to log BUILDING_ONLINE event for PropertyID {property_id}")
            return

        # Update ComplianceAttempt BuildingOnlineDate
        update_query = """
            UPDATE simulation.ComplianceAttempt
            SET BuildingOnlineDate = ?
            WHERE RunID = ?
                AND AttemptID = ?
        """
        self.db.execute_non_query(update_query, (current_date, self.run_id, building_attempt.AttemptID))

        # Log ComplianceStep
        step_id = self._get_next_step_id()
        insert_step = """
            INSERT INTO simulation.ComplianceStep (
                RunID, StepID, AttemptID, WorkItemID, StepType, EffectiveDate, EventID, Notes
            )
            VALUES (?, ?, ?, NULL, 'BUILDING_ONLINE', ?, ?, ?)
        """
        notes = f'Building online: all building-level gates cleared, at least one unit rentable'
        self.db.execute_non_query(insert_step, (
            self.run_id, step_id, building_attempt.AttemptID,
            current_date, event_id, notes
        ))

        print(f"      -> BUILDING_ONLINE event emitted for PropertyID {property_id}")

    def _check_and_emit_unit_ready(
        self,
        property_id: int,
        unit_id: int,
        current_date: date
    ) -> None:
        """
        Check if unit is rentable and emit UNIT_READY event if not already emitted.

        Uses UnitReadyDate for idempotency.

        Steps:
        1. Check if unit is rentable (building online + unit items cleared)
        2. Query ComplianceAttempt for this unit: if UnitReadyDate IS NULL, proceed
        3. Emit UNIT_READY event
        4. UPDATE ComplianceAttempt SET UnitReadyDate = current_date
        5. Log ComplianceStep
        """
        # Check if unit is rentable
        if not self.is_unit_rentable(property_id, unit_id):
            return

        # Get unit-level ComplianceAttempt
        query = """
            SELECT AttemptID, UnitReadyDate
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
                AND PropertyID = ?
                AND UnitID = ?
        """
        result = self.db.execute_query(query, (self.run_id, property_id, unit_id))

        if not result:
            print(f"      WARNING: No ComplianceAttempt found for PropertyID {property_id}, UnitID {unit_id}")
            return

        unit_attempt = result[0]

        # Check if already emitted (idempotency via UnitReadyDate)
        if unit_attempt.UnitReadyDate is not None:
            return  # Already emitted

        # Emit UNIT_READY event using EventType.SIMULATION
        from event_logger import EventType, EntityType, ActionType

        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.UNIT,
            entity_id=unit_id,
            action_type=ActionType.COMPLETE,
            metadata=f'UNIT_READY: PropertyID {property_id}, UnitID {unit_id} - building online, all unit-level gates cleared'
        )

        # Check if event logging failed
        if event_id is None:
            print(f"      ERROR: Failed to log UNIT_READY event for PropertyID {property_id}, UnitID {unit_id}")
            return

        # Update ComplianceAttempt UnitReadyDate
        update_query = """
            UPDATE simulation.ComplianceAttempt
            SET UnitReadyDate = ?
            WHERE RunID = ?
                AND AttemptID = ?
        """
        self.db.execute_non_query(update_query, (current_date, self.run_id, unit_attempt.AttemptID))

        # Transition unit to Available now that it is rentable.
        # NOTE: compliance_manager works in reference ID space (property_id and
        # unit_id are reference IDs from reference.Properties/reference.Units),
        # but simulation.PropertyUnits uses simulation-local UnitID. Bridge via
        # simulation.Properties.AddressID (= reference PropertyID) and the
        # PropertyUnits.Unit string (= reference UnitNumber).
        unit_status_query = """
            UPDATE u
            SET u.UnitStatus = 'Available'
            FROM simulation.PropertyUnits u
            JOIN simulation.Properties p
              ON u.RunID = p.RunID AND u.PropertyID = p.PropertyID
            JOIN reference.Units ru
              ON ru.PropertyID = p.AddressID
             AND CAST(ru.UnitNumber AS NVARCHAR(50)) = u.Unit
            WHERE u.RunID = ?
              AND ru.UnitID = ?
        """
        self.db.execute_non_query(unit_status_query, (self.run_id, unit_id))

        # Log ComplianceStep
        step_id = self._get_next_step_id()
        insert_step = """
            INSERT INTO simulation.ComplianceStep (
                RunID, StepID, AttemptID, WorkItemID, StepType, EffectiveDate, EventID, Notes
            )
            VALUES (?, ?, ?, NULL, 'UNIT_READY', ?, ?, ?)
        """
        notes = f'Unit ready: building online, all unit-level gates cleared'
        self.db.execute_non_query(insert_step, (
            self.run_id, step_id, unit_attempt.AttemptID,
            current_date, event_id, notes
        ))

        print(f"      -> UNIT_READY event emitted for PropertyID {property_id}, UnitID {unit_id}")

    def _check_and_emit_compliance_completed(
        self,
        property_id: int,
        current_date: date
    ) -> None:
        """
        Check if all compliance gates are satisfied and emit COMPLIANCE_COMPLETED event.

        Criteria:
        - Building is ONLINE (no blocking building items uncleared)
        - All units are RENTABLE (no blocking unit items uncleared)

        Mark ComplianceAttempt.IsActive = 0, CompletedDate = current_date
        """
        # Check if building is online
        if not self.is_building_online(property_id):
            return

        # Get all units for this property
        query = """
            SELECT DISTINCT UnitID, AttemptID, CompletedDate, IsActive
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
                AND PropertyID = ?
                AND UnitID IS NOT NULL
        """
        units = self.db.execute_query(query, (self.run_id, property_id))

        if not units:
            return  # No units found

        # Check if all units are rentable
        all_units_ready = True
        for unit_row in units:
            unit_id = unit_row.UnitID
            if not self.is_unit_rentable(property_id, unit_id):
                all_units_ready = False
                break

        if not all_units_ready:
            return  # Not all units are ready yet

        # Get building-level ComplianceAttempt
        building_query = """
            SELECT AttemptID, CompletedDate, IsActive
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
                AND PropertyID = ?
                AND UnitID IS NULL
        """
        building_result = self.db.execute_query(building_query, (self.run_id, property_id))

        if not building_result:
            print(f"      WARNING: No building-level ComplianceAttempt found for PropertyID {property_id}")
            return

        building_attempt = building_result[0]

        # Check if already completed (idempotency via CompletedDate)
        if building_attempt.CompletedDate is not None or building_attempt.IsActive == 0:
            return  # Already completed

        # Emit COMPLIANCE_COMPLETED event using EventType.SIMULATION
        from event_logger import EventType, EntityType, ActionType

        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.PROPERTY,
            entity_id=property_id,
            action_type=ActionType.COMPLETE,
            metadata=f'COMPLIANCE_COMPLETED: PropertyID {property_id} - building online, all units rentable'
        )

        # Check if event logging failed
        if event_id is None:
            print(f"      ERROR: Failed to log COMPLIANCE_COMPLETED event for PropertyID {property_id}")
            return

        # Mark building-level ComplianceAttempt as completed
        update_query = """
            UPDATE simulation.ComplianceAttempt
            SET CompletedDate = ?,
                IsActive = 0
            WHERE RunID = ?
                AND AttemptID = ?
        """
        self.db.execute_non_query(update_query, (current_date, self.run_id, building_attempt.AttemptID))

        # Mark all unit-level ComplianceAttempts as completed
        for unit_row in units:
            self.db.execute_non_query(update_query, (current_date, self.run_id, unit_row.AttemptID))

        # Log ComplianceStep for building
        step_id = self._get_next_step_id()
        insert_step = """
            INSERT INTO simulation.ComplianceStep (
                RunID, StepID, AttemptID, WorkItemID, StepType, EffectiveDate, EventID, Notes
            )
            VALUES (?, ?, ?, NULL, 'COMPLIANCE_COMPLETED', ?, ?, ?)
        """
        notes = f'Compliance completed: building online, all units rentable'
        self.db.execute_non_query(insert_step, (
            self.run_id, step_id, building_attempt.AttemptID,
            current_date, event_id, notes
        ))

        print(f"      -> COMPLIANCE_COMPLETED event emitted for PropertyID {property_id}")

    def _check_all_milestones(
        self,
        property_id: int,
        current_date: date
    ) -> None:
        """
        Check and emit all milestone events for a property.

        Called after any work item status changes to check if new milestones reached.

        Order matters:
        1. Check BUILDING_ONLINE first (required for units to be rentable)
        2. Check UNIT_READY for each unit
        3. Check COMPLIANCE_COMPLETED (all units ready)
        """

        # 1. Check if building should be marked online
        self._check_and_emit_building_online(property_id, current_date)

        # 2. Check if any units should be marked ready
        query = """
            SELECT DISTINCT UnitID
            FROM simulation.ComplianceAttempt
            WHERE RunID = ?
                AND PropertyID = ?
                AND UnitID IS NOT NULL
        """
        units = self.db.execute_query(query, (self.run_id, property_id))

        if units:
            for unit_row in units:
                self._check_and_emit_unit_ready(property_id, unit_row.UnitID, current_date)

        # 3. Check if compliance is fully completed
        self._check_and_emit_compliance_completed(property_id, current_date)
