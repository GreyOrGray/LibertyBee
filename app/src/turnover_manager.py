"""
Turnover Manager Module

Manages the unit turnover workflow that fires after a lease terminates.

Two-step model:
  1. trigger_turnover() at termination — inserts 5 work orders + transitions
     PropertyUnits.UnitStatus to 'Turnover'
  2. process_daily_turnover() runs in the daily simulation loop — advances
     work items PENDING -> IN_PROGRESS -> COMPLETED with sequence gating

When FINAL_INSPECTION completes, PropertyUnits.UnitStatus transitions to
'Available' (also called "Vacant").

Restoration requirement:
    restoration_required = damage_withheld_amount > 0 or was_eviction

Eviction-biased restoration duration:
    if was_eviction: rng.randint(15, 30)
    else:            rng.randint(10, 30)

Determinism: all RNG draws use seeded RNG with offset (seed + 30707).

Settlement state read boundary:
- DamageAmount is read from simulation.LeaseDeposit (set by
  SecurityDepositManager.settle_deposit_at_termination, committed before
  trigger_turnover is called).
- Missing or invalid settlement state hard-fails with RuntimeError in all
  environments. No production fallback.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from database_manager import DatabaseManager
from parameter_registry import ParameterRegistry
from event_logger import EventLogger, EventType, EntityType, ActionType


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

# Work item type values (must match V00022 CHECK constraint)
class WorkItemType:
    INSPECT = "INSPECT"
    CLEAN = "CLEAN"
    PAINT = "PAINT"
    RESTORATION = "RESTORATION"
    FINAL_INSPECTION = "FINAL_INSPECTION"


# Work order status values (must match V00022 CHECK constraint)
class WorkOrderStatus:
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


# Termination types that count as eviction-biased
EVICTION_TERMINATION_TYPES = {"EVICTION"}

# Turnover work-item durations (spec, locked) and the
# restoration duration ranges are now registry-backed (TIMING.*); they are read
# once into instance attributes in TurnoverManager.__init__.


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------

@dataclass
class TurnoverSummary:
    """Returned by trigger_turnover() for caller logging."""
    lease_id: int
    unit_id: int
    termination_type: str
    work_orders_created: int
    restoration_required: bool
    restoration_duration: int
    turnover_end_date: date  # scheduled completion date (FINAL_INSPECTION.ScheduledEndDate)


@dataclass
class WorkItemTransition:
    """Returned by process_daily_turnover() per row affected."""
    event: str            # 'started' or 'completed'
    work_order_id: int
    lease_id: int
    unit_id: int
    work_item_type: str


# ----------------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------------

class TurnoverManager:
    """Manages turnover workflow for units vacated by lease termination."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        event_logger: EventLogger,
        run_id: int,
        seed: int,
        logger: Any = None,
    ):
        self.db = db_manager
        self.event_logger = event_logger
        self.run_id = run_id
        self.rng = random.Random(seed + 30707)  # offset
        self.logger = logger

        # Turnover durations + restoration ranges from the
        # registry (TIMING.*, global) — were module constants. Read-once, fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.duration_inspect = self.registry.get_int('TIMING', 'TurnoverDurationInspect')
        self.duration_clean = self.registry.get_int('TIMING', 'TurnoverDurationClean')
        self.duration_paint = self.registry.get_int('TIMING', 'TurnoverDurationPaint')
        self.duration_final_inspection = self.registry.get_int('TIMING', 'TurnoverDurationFinalInspect')
        self.restoration_range_eviction = (
            self.registry.get_int('TIMING', 'RestorationMinEviction'),
            self.registry.get_int('TIMING', 'RestorationMaxEviction'),
        )
        self.restoration_range_voluntary = (
            self.registry.get_int('TIMING', 'RestorationMinVoluntary'),
            self.registry.get_int('TIMING', 'RestorationMaxVoluntary'),
        )

    # ------------------------------------------------------------------
    # Public — trigger
    # ------------------------------------------------------------------

    def trigger_turnover(
        self,
        run_id: int,
        lease_id: int,
        termination_date: date,
        termination_type: str,
    ) -> Optional[TurnoverSummary]:
        """
        Initialize a turnover workflow for a vacated unit.

        Step 0: Idempotency guard (safety rail for reruns/retries).
        Step 1: Resolve damage state from LeaseDeposit (hard-fail if missing).
        Step 2: Determine restoration requirement + duration.
        Step 3: Compute work item schedule.
        Step 4: Insert 5 work order rows + log creation events (Option B sequencing).
        Step 5: Transition PropertyUnits.UnitStatus -> 'Turnover'.
        Step 6: Log UnitTurnoverStarted event.

        Returns TurnoverSummary, or None if duplicate trigger (idempotency).
        Raises RuntimeError on missing/invalid settlement state.
        """
        # Resolve unit_id from lease
        lease_rows = self.db.execute_query(
            "SELECT UnitID FROM simulation.Lease WHERE RunID=? AND LeaseID=?",
            (self.run_id, lease_id),
        )
        if not lease_rows:
            raise RuntimeError(
                f"Lease not found: RunID={self.run_id}, LeaseID={lease_id} — cannot trigger turnover"
            )
        unit_id = lease_rows[0][0]

        # Step 0: Idempotency guard
        existing = self.db.execute_query(
            "SELECT COUNT(*) FROM simulation.TurnoverWorkOrder "
            "WHERE RunID=? AND LeaseID=? AND UnitID=?",
            (self.run_id, lease_id, unit_id),
        )
        existing_count = existing[0][0] if existing else 0
        if existing_count > 0:
            self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=termination_date,
                entity_type=EntityType.UNIT,
                entity_id=unit_id,
                action_type=ActionType.UPDATE,
                metadata=(
                    f"TURNOVER/DUPLICATE_TRIGGER_IGNORED: LeaseID={lease_id}, "
                    f"UnitID={unit_id}, TerminationType={termination_type}, "
                    f"ExistingWorkOrders={existing_count}"
                ),
            )
            if self.logger:
                self.logger.warning(
                    f"Duplicate turnover trigger ignored for LeaseID={lease_id}, UnitID={unit_id}"
                )
            return None

        # Step 1: Resolve damage state (hard-fail on missing/invalid)
        damage_withheld_amount = self._resolve_damage_amount(lease_id)

        # Step 2: Determine restoration shape
        was_eviction = termination_type in EVICTION_TERMINATION_TYPES
        restoration_required = damage_withheld_amount > Decimal("0") or was_eviction

        if restoration_required:
            r_min, r_max = (
                self.restoration_range_eviction if was_eviction
                else self.restoration_range_voluntary
            )
            restoration_duration = self.rng.randint(r_min, r_max)
        else:
            restoration_duration = 0

        # Step 3: Compute schedule
        schedule = self._compute_schedule(
            turnover_start=termination_date,
            restoration_duration=restoration_duration,
            restoration_required=restoration_required,
        )

        # Step 4: Insert work order rows + log creation events
        property_id = self._get_property_id_for_unit(unit_id)
        for work_item_type, start, end, duration, required, status, sequence in schedule:
            self._create_work_order(
                lease_id=lease_id,
                unit_id=unit_id,
                property_id=property_id,
                termination_type=termination_type,
                work_item_type=work_item_type,
                work_sequence=sequence,
                status=status,
                required_flag=required,
                scheduled_start_date=start,
                scheduled_end_date=end,
                duration_days=duration,
                was_eviction=was_eviction,
                damage_withheld_amount=damage_withheld_amount,
            )

        # Step 5: Transition unit to Turnover
        self.db.execute_non_query(
            "UPDATE simulation.PropertyUnits SET UnitStatus='Turnover' "
            "WHERE RunID=? AND UnitID=?",
            (self.run_id, unit_id),
        )

        # Step 6: Log UnitTurnoverStarted
        turnover_end_date = schedule[-1][2]  # FINAL_INSPECTION.scheduled_end_date
        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=termination_date,
            entity_type=EntityType.UNIT,
            entity_id=unit_id,
            action_type=ActionType.START,
            metadata=(
                f"UNIT/TURNOVER_STARTED: LeaseID={lease_id}, UnitID={unit_id}, "
                f"TerminationType={termination_type}, "
                f"RestorationRequired={restoration_required}, "
                f"RestorationDuration={restoration_duration}, "
                f"WasEviction={was_eviction}, "
                f"DamageWithheld={damage_withheld_amount:.2f}, "
                f"ScheduledEndDate={turnover_end_date}"
            ),
        )

        if self.logger:
            self.logger.info(
                f"Turnover triggered: LeaseID={lease_id}, UnitID={unit_id}, "
                f"TerminationType={termination_type}, Restoration={restoration_duration}d"
            )

        return TurnoverSummary(
            lease_id=lease_id,
            unit_id=unit_id,
            termination_type=termination_type,
            work_orders_created=5,
            restoration_required=restoration_required,
            restoration_duration=restoration_duration,
            turnover_end_date=turnover_end_date,
        )

    # ------------------------------------------------------------------
    # Public — daily processor
    # ------------------------------------------------------------------

    def process_daily_turnover(
        self,
        run_id: int,
        current_date: date,
    ) -> List[WorkItemTransition]:
        """
        Daily turnover state machine.

        Phase A: IN_PROGRESS -> COMPLETED for items whose ScheduledEndDate <= current_date.
                 When FINAL_INSPECTION completes, finalize the turnover.
        Phase B: PENDING -> IN_PROGRESS for items whose ScheduledStartDate <= current_date,
                 gated by NOT EXISTS check ensuring all prior-sequence items are COMPLETED/SKIPPED.

        Returns list of WorkItemTransition for caller logging.
        """
        results: List[WorkItemTransition] = []

        # Phase A — Complete IN_PROGRESS items whose end date has arrived
        completed_rows = self.db.execute_query(
            """
            SELECT TurnoverWorkOrderID, LeaseID, UnitID, WorkItemType
            FROM simulation.TurnoverWorkOrder
            WHERE RunID = ?
              AND Status = ?
              AND ScheduledEndDate <= ?
            ORDER BY UnitID, WorkSequence
            """,
            (self.run_id, WorkOrderStatus.IN_PROGRESS, current_date),
        )

        for work_order_id, lease_id, unit_id, work_item_type in completed_rows:
            self.db.execute_non_query(
                "UPDATE simulation.TurnoverWorkOrder "
                "SET Status=?, ActualEndDate=?, UpdatedAt=CURRENT_TIMESTAMP "
                "WHERE RunID=? AND TurnoverWorkOrderID=?",
                (WorkOrderStatus.COMPLETED, current_date, self.run_id, work_order_id),
            )
            self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=current_date,
                entity_type=EntityType.UNIT,
                entity_id=unit_id,
                action_type=ActionType.COMPLETE,
                metadata=(
                    f"TURNOVER/WORK_ITEM_COMPLETED: LeaseID={lease_id}, UnitID={unit_id}, "
                    f"WorkItemType={work_item_type}, TurnoverWorkOrderID={work_order_id}"
                ),
            )
            results.append(WorkItemTransition(
                event='completed',
                work_order_id=work_order_id,
                lease_id=lease_id,
                unit_id=unit_id,
                work_item_type=work_item_type,
            ))

            # FINAL_INSPECTION completion finalizes the turnover
            if work_item_type == WorkItemType.FINAL_INSPECTION:
                self._finalize_turnover(
                    unit_id=unit_id,
                    lease_id=lease_id,
                    current_date=current_date,
                )

        # Phase B — Start PENDING items with all prior-sequence items COMPLETED/SKIPPED
        # Sequence gating prevents catch-up scenarios from violating sequential rule.
        ready_to_start = self.db.execute_query(
            """
            SELECT rwo.TurnoverWorkOrderID, rwo.LeaseID, rwo.UnitID, rwo.WorkItemType
            FROM simulation.TurnoverWorkOrder rwo
            WHERE rwo.RunID = ?
              AND rwo.Status = ?
              AND rwo.ScheduledStartDate <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM simulation.TurnoverWorkOrder prior
                  WHERE prior.RunID = rwo.RunID
                    AND prior.LeaseID = rwo.LeaseID
                    AND prior.UnitID = rwo.UnitID
                    AND prior.WorkSequence < rwo.WorkSequence
                    AND prior.Status NOT IN (?, ?)
              )
            ORDER BY rwo.UnitID, rwo.WorkSequence
            """,
            (
                self.run_id,
                WorkOrderStatus.PENDING,
                current_date,
                WorkOrderStatus.COMPLETED,
                WorkOrderStatus.SKIPPED,
            ),
        )

        for work_order_id, lease_id, unit_id, work_item_type in ready_to_start:
            self.db.execute_non_query(
                "UPDATE simulation.TurnoverWorkOrder "
                "SET Status=?, ActualStartDate=?, UpdatedAt=CURRENT_TIMESTAMP "
                "WHERE RunID=? AND TurnoverWorkOrderID=?",
                (WorkOrderStatus.IN_PROGRESS, current_date, self.run_id, work_order_id),
            )
            self.event_logger.log_event(
                event_type=EventType.SIMULATION,
                effective_date=current_date,
                entity_type=EntityType.UNIT,
                entity_id=unit_id,
                action_type=ActionType.START,
                metadata=(
                    f"TURNOVER/WORK_ITEM_STARTED: LeaseID={lease_id}, UnitID={unit_id}, "
                    f"WorkItemType={work_item_type}, TurnoverWorkOrderID={work_order_id}"
                ),
            )
            results.append(WorkItemTransition(
                event='started',
                work_order_id=work_order_id,
                lease_id=lease_id,
                unit_id=unit_id,
                work_item_type=work_item_type,
            ))

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_damage_amount(self, lease_id: int) -> Decimal:
        """
        Read damage state from LeaseDeposit.

        Returns Decimal damage amount.
        Raises RuntimeError if settlement state is missing or invalid.
        """
        rows = self.db.execute_query(
            "SELECT SettlementStatus, SettlementOutcome, DamageAmount "
            "FROM simulation.LeaseDeposit WHERE RunID=? AND LeaseID=?",
            (self.run_id, lease_id),
        )

        if not rows:
            raise RuntimeError(
                f"Settlement state missing for RunID={self.run_id}, LeaseID={lease_id}. "
                f"Turnover cannot be triggered without committed deposit settlement."
            )

        settlement_status, settlement_outcome, damage_amount = rows[0]

        # Case 1: explicit damage value (including 0.00) — normal path
        if damage_amount is not None:
            return Decimal(str(damage_amount))

        # Case 2: defensive — NULL DamageAmount with FULL_RETURN outcome
        if settlement_outcome == "FULL_RETURN":
            return Decimal("0.00")

        # Anything else — hard fail
        raise RuntimeError(
            f"Settlement state invalid for RunID={self.run_id}, LeaseID={lease_id}: "
            f"SettlementStatus={settlement_status}, SettlementOutcome={settlement_outcome}, "
            f"DamageAmount=NULL. Turnover requires explicit damage state."
        )

    def _compute_schedule(
        self,
        turnover_start: date,
        restoration_duration: int,
        restoration_required: bool,
    ) -> List[Tuple]:
        """
        Returns ordered list of (work_item_type, start, end, duration, required, status, sequence).

        Convention:
            end = start + duration_days
            next item.start = prior item.end
        """
        d_inspect_start = turnover_start
        d_inspect_end = d_inspect_start + timedelta(days=self.duration_inspect)
        d_clean_end = d_inspect_end + timedelta(days=self.duration_clean)
        d_paint_end = d_clean_end + timedelta(days=self.duration_paint)
        d_restoration_end = d_paint_end + timedelta(days=restoration_duration)
        d_final_end = d_restoration_end + timedelta(days=self.duration_final_inspection)

        if restoration_required:
            rest_status = WorkOrderStatus.PENDING
            rest_required = 1
        else:
            rest_status = WorkOrderStatus.SKIPPED
            rest_required = 0

        return [
            (WorkItemType.INSPECT,          d_inspect_start, d_inspect_end,     self.duration_inspect,           1,             WorkOrderStatus.PENDING, 1),
            (WorkItemType.CLEAN,            d_inspect_end,   d_clean_end,       self.duration_clean,             1,             WorkOrderStatus.PENDING, 2),
            (WorkItemType.PAINT,            d_clean_end,     d_paint_end,       self.duration_paint,             1,             WorkOrderStatus.PENDING, 3),
            (WorkItemType.RESTORATION,      d_paint_end,     d_restoration_end, restoration_duration,       rest_required, rest_status,             4),
            (WorkItemType.FINAL_INSPECTION, d_restoration_end, d_final_end,     self.duration_final_inspection,  1,             WorkOrderStatus.PENDING, 5),
        ]

    def _get_property_id_for_unit(self, unit_id: int) -> Optional[int]:
        rows = self.db.execute_query(
            "SELECT PropertyID FROM simulation.PropertyUnits "
            "WHERE RunID=? AND UnitID=? "
            "ORDER BY PropertyID OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY",
            (self.run_id, unit_id),
        )
        return rows[0][0] if rows else None

    def _create_work_order(
        self,
        lease_id: int,
        unit_id: int,
        property_id: Optional[int],
        termination_type: str,
        work_item_type: str,
        work_sequence: int,
        status: str,
        required_flag: int,
        scheduled_start_date: date,
        scheduled_end_date: date,
        duration_days: int,
        was_eviction: bool,
        damage_withheld_amount: Decimal,
    ) -> int:
        """
        Insert work order with EventID back-fill (Option B sequencing):
        1. INSERT row with EventID=NULL
        2. Log creation event with TurnoverWorkOrderID in metadata
        3. UPDATE row to set EventID

        Returns TurnoverWorkOrderID.

        Safe under current single-threaded per-run simulation execution.
        Revisit if per-run parallelism is introduced.
        """
        # Allocate next ID
        result = self.db.execute_query(
            "SELECT COALESCE(MAX(TurnoverWorkOrderID), 0) + 1 "
            "FROM simulation.TurnoverWorkOrder WHERE RunID=?",
            (self.run_id,),
        )
        work_order_id = result[0][0] if result else 1

        # Step 1: INSERT with EventID=NULL
        self.db.execute_non_query(
            """
            INSERT INTO simulation.TurnoverWorkOrder (
                RunID, TurnoverWorkOrderID, LeaseID, UnitID, PropertyID,
                TerminationType, WorkItemType, WorkSequence,
                Status, RequiredFlag,
                ScheduledStartDate, ScheduledEndDate,
                DurationDays, WasEviction, DamageWithheldAmount, EventID
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                self.run_id,
                work_order_id,
                lease_id,
                unit_id,
                property_id,
                termination_type,
                work_item_type,
                work_sequence,
                status,
                required_flag,
                scheduled_start_date,
                scheduled_end_date,
                duration_days,
                1 if was_eviction else 0,
                damage_withheld_amount,
            ),
        )

        # Step 2: Log creation event with TurnoverWorkOrderID in metadata
        event_id = self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=scheduled_start_date,
            entity_type=EntityType.UNIT,
            entity_id=unit_id,
            action_type=ActionType.CREATE,
            metadata=(
                f"TURNOVER/WORK_ORDER_CREATED: LeaseID={lease_id}, UnitID={unit_id}, "
                f"WorkItemType={work_item_type}, Sequence={work_sequence}, "
                f"Status={status}, ScheduledStart={scheduled_start_date}, "
                f"ScheduledEnd={scheduled_end_date}, Duration={duration_days}, "
                f"TurnoverWorkOrderID={work_order_id}"
            ),
        )

        # Step 3: Back-fill EventID
        if event_id is not None:
            self.db.execute_non_query(
                "UPDATE simulation.TurnoverWorkOrder "
                "SET EventID=?, UpdatedAt=CURRENT_TIMESTAMP "
                "WHERE RunID=? AND TurnoverWorkOrderID=?",
                (event_id, self.run_id, work_order_id),
            )

        return work_order_id

    def _finalize_turnover(
        self,
        unit_id: int,
        lease_id: int,
        current_date: date,
    ) -> None:
        """
        FINAL_INSPECTION just completed — transition unit to 'Available'
        (also called 'Vacant').

        Logs:
          UNIT/TURNOVER_COMPLETED — turnover lifecycle done
          VACANCY/STARTED — marker event; a later step will materialize the
                            Vacancy row and run the re-leasing pipeline.
        """
        self.db.execute_non_query(
            "UPDATE simulation.PropertyUnits SET UnitStatus='Available' "
            "WHERE RunID=? AND UnitID=?",
            (self.run_id, unit_id),
        )

        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.UNIT,
            entity_id=unit_id,
            action_type=ActionType.COMPLETE,
            metadata=f"UNIT/TURNOVER_COMPLETED: LeaseID={lease_id}, UnitID={unit_id}",
        )

        # VacancyStarted — boundary marker
        # This method logs the event; a later step creates the Vacancy row.
        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.UNIT,
            entity_id=unit_id,
            action_type=ActionType.START,
            metadata=(
                f"VACANCY/STARTED: UnitID={unit_id}, VacancyStartDate={current_date} "
                f"(marker event — Vacancy row creation deferred to a later step)"
            ),
        )

        if self.logger:
            self.logger.info(
                f"Turnover completed: LeaseID={lease_id}, UnitID={unit_id}, "
                f"UnitStatus=Available"
            )
