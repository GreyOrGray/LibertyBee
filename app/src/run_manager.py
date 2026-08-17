"""
Run Manager - Handle simulation run lifecycle and status tracking

This module manages the complete lifecycle of simulation runs including
creation, progress tracking, completion, and audit trail maintenance.
It provides a clean interface for run state management with proper
timestamps and status tracking.

Run Status Flow:
- CREATED → Run initialized, ready to start
- RUNNING → Simulation in progress
- COMPLETED → Finished successfully (all months)
- CLOSED → Terminated early (error, interruption, etc.)

Usage:
- Use RunManager.create_run() to start a new simulation
- Use RunManager.update_progress() during simulation execution
- Use RunManager.complete_run() or close_run() to finish
"""

import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime, date
from enum import Enum

from database_manager import DatabaseManager
from configuration_loader import ConfigurationLoader, ProjectionConfig

class RunStatus(Enum):
    """Simulation run status enumeration.

    COMPLETED = ran the full requested horizon.
    HALTED    = terminated on protected-obligation failure (Cash+CSF could not
                cover payroll) — an EXPECTED simulation outcome, not an error.
                CLOSED remains error/interruption.
    """
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    HALTED = "HALTED"
    CLOSED = "CLOSED"

@dataclass
class RunInfo:
    """Complete run information with metadata"""
    run_id: int
    projection_id: int
    status: RunStatus
    start_date: date
    end_date: date
    current_date: Optional[date]
    random_seed: Optional[int]
    total_days: int
    days_completed: int

    # Timestamps
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    closed_at: Optional[datetime]
    close_reason: Optional[str]

    # Linked configuration
    projection_config: Optional[ProjectionConfig] = None

class RunManager:
    """Manage simulation run lifecycle and status"""

    def __init__(self, db_manager: DatabaseManager, config_loader: ConfigurationLoader):
        self.db = db_manager
        self.config_loader = config_loader
        self.logger = logging.getLogger(__name__)
        self.event_logger = None


    def create_run(self, projection_id: int, random_seed: Optional[int] = None,
                   run_type: str = "Simulation") -> Optional[int]:
        """Create a new simulation run and return RunID

        Args:
            projection_id: Which projection scenario to use
            random_seed: Random seed for reproducibility
            run_type: Type of run (Simulation, ComponentTest, IntegrationTest)
        """
        if self.event_logger:
            from event_logger import ActionType, EntityType
            self.event_logger.log_module_event(
                module_name="RunManager",
                action=ActionType.START,
                message=f"Creating new simulation run for projection {projection_id}",
                entity_type=EntityType.RUN,
                entity_id=projection_id
            )

        try:
            # Load projection config to get date range
            if self.event_logger:
                from event_logger import ActionType, EntityType
                self.event_logger.log_module_event(
                    module_name="RunManager",
                    action=ActionType.VALIDATE,
                    message=f"Loading projection configuration {projection_id}",
                    entity_type=EntityType.CONFIGURATION,
                    entity_id=projection_id
                )

            projection = self.config_loader.load_projection(projection_id)
            if not projection:
                self.logger.error(f"Cannot create run: projection {projection_id} not found")
                if self.event_logger:
                    self.event_logger.log_error(
                        error_message=f"Cannot create run: projection {projection_id} not found",
                        context="RunManager.create_run"
                    )
                return None

            # Calculate total days
            total_days = (projection.end_date - projection.start_date).days + 1

            if self.event_logger:
                from event_logger import ActionType, EntityType
                self.event_logger.log_module_event(
                    module_name="RunManager",
                    action=ActionType.VALIDATE,
                    message=f"Calculated {total_days} days from {projection.start_date} to {projection.end_date}",
                    entity_type=EntityType.RUN
                )

            # Get next RunID manually (RunID is no longer IDENTITY)
            next_run_id_query = "SELECT COALESCE(MAX(RunID), 0) + 1 FROM simulation.Run"
            next_run_id = self.db.execute_scalar(next_run_id_query)

            if self.event_logger:
                from event_logger import ActionType
                self.event_logger.log_module_event(
                    module_name="RunManager",
                    action=ActionType.VALIDATE,
                    message=f"Assigned next RunID: {next_run_id}",
                    entity_type=EntityType.RUN,
                    entity_id=next_run_id
                )

            # Insert new run record with explicit RunID
            query = """
            INSERT INTO simulation.Run (
                RunID, ProjectionID, StartDate, EndDate, Status, CreatedAt, RandomSeed, RunType, Notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            params = (
                next_run_id,
                projection_id,
                projection.start_date,
                projection.end_date,
                RunStatus.CREATED.value,
                datetime.now(),
                random_seed,
                run_type,
                f"Total days: {total_days}"  # Store additional info in Notes
            )

            if self.event_logger:
                from event_logger import ActionType
                self.event_logger.log_database_event(
                    action=ActionType.CREATE,
                    message=f"Inserting new run record {next_run_id} for projection {projection_id}",
                    table_name="simulation.Run"
                )

            affected_rows = self.db.execute_non_query(query, params)
            if affected_rows > 0:
                # Use the manually assigned RunID
                run_id = next_run_id
                self.logger.info(f"Created run {run_id} for projection {projection_id} "
                               f"({projection.start_date} to {projection.end_date}, {total_days} days)")

                if self.event_logger:
                    from event_logger import ActionType, EntityType
                    self.event_logger.log_module_event(
                        module_name="RunManager",
                        action=ActionType.COMPLETE,
                        message=f"Successfully created run {run_id} ({total_days} days, seed: {random_seed})",
                        entity_type=EntityType.RUN,
                        entity_id=run_id
                    )

                return run_id
            else:
                self.logger.error("Failed to create run: no rows affected")
                if self.event_logger:
                    self.event_logger.log_error(
                        error_message="Failed to create run: no rows affected",
                        context="RunManager.create_run"
                    )
                return None

        except Exception as e:
            self.logger.error(f"Failed to create run: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"Failed to create run for projection {projection_id}",
                    exception=e,
                    context="RunManager.create_run"
                )
            return None

    def start_run(self, run_id: int) -> bool:
        """Mark run as started (update status only - no StartedAt column)"""
        if self.event_logger:
            from event_logger import ActionType, EntityType
            self.event_logger.log_module_event(
                module_name="RunManager",
                action=ActionType.START,
                message=f"Starting simulation run {run_id}",
                entity_type=EntityType.RUN,
                entity_id=run_id
            )

        try:
            query = """
            UPDATE simulation.Run
            SET Status = ?
            WHERE RunID = ? AND Status = ?
            """

            params = (RunStatus.RUNNING.value, run_id, RunStatus.CREATED.value)

            if self.event_logger:
                from event_logger import ActionType
                self.event_logger.log_database_event(
                    action=ActionType.UPDATE,
                    message=f"Updating run {run_id} status from CREATED to RUNNING",
                    table_name="simulation.Run"
                )

            affected_rows = self.db.execute_non_query(query, params)

            if affected_rows > 0:
                self.logger.info(f"Started run {run_id}")
                if self.event_logger:
                    from event_logger import ActionType, EntityType
                    self.event_logger.log_module_event(
                        module_name="RunManager",
                        action=ActionType.COMPLETE,
                        message=f"Successfully started run {run_id}",
                        entity_type=EntityType.RUN,
                        entity_id=run_id
                    )
                return True
            else:
                self.logger.warning(f"Cannot start run {run_id}: not in CREATED status")
                if self.event_logger:
                    self.event_logger.log_error(
                        error_message=f"Cannot start run {run_id}: not in CREATED status",
                        context="RunManager.start_run"
                    )
                return False

        except Exception as e:
            self.logger.error(f"Failed to start run {run_id}: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"Failed to start run {run_id}",
                    exception=e,
                    context="RunManager.start_run"
                )
            return False
    
    def halt_run(self, run_id: int, notes: str) -> bool:
        """Mark run as HALTED (protected-obligation failure).

        The death certificate: Status='HALTED' + the halt month and combined
        shortfall in Notes, captured AT BREAK TIME by the caller (post-loop
        balances differ). Mirrors complete_run's RUNNING-guarded update."""
        if self.event_logger:
            from event_logger import ActionType, EntityType
            self.event_logger.log_module_event(
                module_name="RunManager",
                action=ActionType.FAIL,
                message=f"Halting simulation run {run_id}: {notes}",
                entity_type=EntityType.RUN,
                entity_id=run_id
            )
        try:
            affected = self.db.execute_non_query(
                """
                UPDATE simulation.Run
                SET Status = ?, CompletedAt = ?, Notes = ?
                WHERE RunID = ? AND Status = ?
                """,
                (RunStatus.HALTED.value, datetime.now(), notes,
                 run_id, RunStatus.RUNNING.value),
            )
            if affected:
                self.logger.info(f"Run {run_id} HALTED: {notes}")
                return True
            self.logger.error(f"halt_run: run {run_id} was not in RUNNING state")
            return False
        except Exception as e:
            self.logger.error(f"halt_run failed for run {run_id}: {e}")
            return False

    def complete_run(self, run_id: int) -> bool:
        """Mark run as completed successfully"""
        if self.event_logger:
            from event_logger import ActionType, EntityType
            self.event_logger.log_module_event(
                module_name="RunManager",
                action=ActionType.START,
                message=f"Completing simulation run {run_id}",
                entity_type=EntityType.RUN,
                entity_id=run_id
            )

        try:
            query = """
            UPDATE simulation.Run
            SET Status = ?, CompletedAt = ?
            WHERE RunID = ? AND Status = ?
            """

            params = (RunStatus.COMPLETED.value, datetime.now(), run_id, RunStatus.RUNNING.value)

            if self.event_logger:
                from event_logger import ActionType
                self.event_logger.log_database_event(
                    action=ActionType.UPDATE,
                    message=f"Updating run {run_id} status to COMPLETED with timestamp",
                    table_name="simulation.Run"
                )

            affected_rows = self.db.execute_non_query(query, params)

            if affected_rows > 0:
                self.logger.info(f"Completed run {run_id} successfully")
                if self.event_logger:
                    from event_logger import ActionType, EntityType
                    self.event_logger.log_module_event(
                        module_name="RunManager",
                        action=ActionType.COMPLETE,
                        message=f"Successfully completed run {run_id}",
                        entity_type=EntityType.RUN,
                        entity_id=run_id
                    )
                return True
            else:
                self.logger.warning(f"Cannot complete run {run_id}: not in RUNNING status")
                if self.event_logger:
                    self.event_logger.log_error(
                        error_message=f"Cannot complete run {run_id}: not in RUNNING status",
                        context="RunManager.complete_run"
                    )
                return False

        except Exception as e:
            self.logger.error(f"Failed to complete run {run_id}: {e}")
            if self.event_logger:
                self.event_logger.log_error(
                    error_message=f"Failed to complete run {run_id}",
                    exception=e,
                    context="RunManager.complete_run"
                )
            return False

    def get_run_info(self, run_id: int) -> Optional[RunInfo]:
        """Get complete run information by ID (using actual table structure)"""
        try:
            query = """
            SELECT
                RunID, ProjectionID, StartDate, EndDate, Status, CreatedAt, CompletedAt, RandomSeed, Notes
            FROM simulation.Run
            WHERE RunID = ?
            """

            results = self.db.execute_query(query, (run_id,))
            if not results:
                return None

            row = results[0]

            # Convert dates properly
            def safe_date_convert(value):
                if value is None:
                    return None
                if isinstance(value, datetime):
                    return value.date()
                return value

            # Calculate derived values
            start_date = safe_date_convert(row[2])
            end_date = safe_date_convert(row[3])
            total_days = (end_date - start_date).days + 1 if start_date and end_date else 0

            # Parse progress from Notes if available
            current_date = start_date  # Default
            days_completed = 0
            close_reason = None

            notes = row[8] or ""
            if "Progress:" in notes:
                # Extract current date from progress notes
                try:
                    import re
                    match = re.search(r'Progress: (\d{4}-\d{2}-\d{2})', notes)
                    if match:
                        current_date = datetime.strptime(match.group(1), '%Y-%m-%d').date()
                        days_completed = (current_date - start_date).days
                except:
                    pass
            elif "CLOSED:" in notes:
                close_reason = notes

            run_info = RunInfo(
                run_id=row[0],
                projection_id=row[1],
                status=RunStatus(row[4]),
                start_date=start_date,
                end_date=end_date,
                current_date=current_date,
                random_seed=row[7],
                total_days=total_days,
                days_completed=days_completed,
                created_at=row[5],
                started_at=None,  # Not tracked in this table
                completed_at=row[6],
                closed_at=None,  # Not tracked separately
                close_reason=close_reason
            )

            return run_info

        except Exception as e:
            self.logger.error(f"Failed to get run info for {run_id}: {e}")
            return None
