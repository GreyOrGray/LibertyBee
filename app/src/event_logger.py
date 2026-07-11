"""
Event Logger - Centralized logging to simulation.Event table

This module provides centralized event logging for all simulation activities.
Instead of using file-based or console logging, all significant events are
logged to the simulation.Event table for easy querying and analysis.

Event Types:
- SYSTEM: System-level events (initialization, component startup)
- SIMULATION: Simulation lifecycle events (run creation, completion)
- MODULE: Module-level events (inflation generation, fund operations)
- DATABASE: Database operations and validation
- ERROR: Error conditions and failures
- DEBUG: Detailed debugging information

STANDARDIZATION (2025-12-09):
All convenience methods now follow a consistent signature:
  log_<domain>_event(action, message, effective_date, amount, entity_type, entity_id)

Metadata format: "{DOMAIN}/{action}: {message}"
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime, date
from decimal import Decimal
from enum import Enum

from database_manager import DatabaseManager


class EventType(Enum):
    """Event type enumeration"""
    SYSTEM = "SYSTEM"
    SIMULATION = "SIMULATION"
    MODULE = "MODULE"
    DATABASE = "DATABASE"
    ERROR = "ERROR"
    DEBUG = "DEBUG"

class EntityType(Enum):
    """
    Entity type enumeration

    Updated 2025-12-09: Added UNIT, TENANT, ACQUISITION, LEASE
    for event-ledger standardization
    Updated 2025-12-20: Added HOUSEHOLD, PERSON for tenant onboarding
    """
    RUN = "RUN"
    INFLATION = "INFLATION"
    FUND = "FUND"
    EMPLOYEE = "EMPLOYEE"
    PROPERTY = "PROPERTY"
    UNIT = "UNIT"  # NEW: For PropertyUnits operations
    TENANT = "TENANT"  # NEW: For Tenants operations
    HOUSEHOLD = "HOUSEHOLD"  # NEW: For Household operations
    PERSON = "PERSON"  # NEW: For Person operations
    ACQUISITION = "ACQUISITION"  # NEW: For PropertyAcquisitionAttempt operations
    LEASE = "LEASE"  # NEW: For Leases operations
    DATABASE = "DATABASE"
    CONFIGURATION = "CONFIGURATION"

class ActionType(Enum):
    """Action type enumeration

    Updated 2025-12-26: Added FINANCIAL for rent collection and other financial transactions
    Updated 2026-05-16: Added TENANT_CREDIT for TCS accrual; separate audit lane
    Updated 2026-05-21: Added RENT_REDUCTION for tenure-based rent reductions; separate audit lane
    """
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    VALIDATE = "VALIDATE"
    INITIALIZE = "INITIALIZE"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    START = "START"
    STOP = "STOP"
    FINANCIAL = "FINANCIAL"  # Rent collection, fund transactions
    TENANT_CREDIT = "TENANT_CREDIT"  # TCS accrual/redemption/forfeiture; separate from FINANCIAL
    RENT_REDUCTION = "RENT_REDUCTION"  # tenure-based rent reduction tier application; separate audit lane

class EventLogger:
    """Centralized event logging to simulation.Event table"""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.logger = logging.getLogger(__name__)
        self.current_run_id: Optional[int] = None
        self.simulation_start_date: Optional[date] = None

    def set_run_id(self, run_id: int):
        """Set the current run ID for all subsequent events"""
        self.current_run_id = run_id

        # Get simulation start date from the run
        if run_id:
            try:
                start_date = self.db.execute_scalar(
                    "SELECT StartDate FROM simulation.Run WHERE RunID = ?", (run_id,)
                )
                self.simulation_start_date = start_date
            except Exception as e:
                self.logger.warning(f"Could not get start date for run {run_id}: {e}")
                self.simulation_start_date = None

    def calculate_month_index(self, effective_date: date) -> int:
        """Calculate month index relative to simulation start"""
        if not self.simulation_start_date:
            return 1  # Default to month 1 if no start date

        # Calculate months difference
        months_diff = (effective_date.year - self.simulation_start_date.year) * 12 + \
                     (effective_date.month - self.simulation_start_date.month)
        return max(1, months_diff + 1)  # Months are 1-indexed

    def log_event(self,
                  event_type: EventType,
                  effective_date: date,
                  month_index: Optional[int] = None,
                  entity_type: Optional[EntityType] = None,
                  entity_id: Optional[int] = None,
                  action_type: Optional[ActionType] = None,
                  amount: Optional[Decimal] = None,
                  currency: str = "USD",
                  causal_tag: Optional[str] = None,
                  metadata: Optional[str] = None) -> Optional[int]:
        """Log an event to the simulation.Event table"""

        if not self.current_run_id:
            self.logger.warning("No run ID set for event logging")
            return None

        try:
            # Calculate month index if not provided
            if month_index is None:
                month_index = self.calculate_month_index(effective_date)

            # Get next EventID for this specific RunID (per-run EventID)
            next_event_id = self.db.execute_scalar(
                "SELECT ISNULL(MAX(EventID), 0) + 1 FROM simulation.Event WHERE RunID = ?",
                (self.current_run_id,)
            )

            query = """
            INSERT INTO simulation.Event (
                RunID, EventID, MonthIndex, EffectiveDate, LoggedAt, EventType,
                EntityType, EntityID, ActionType, Amount, Currency,
                CausalTag, Metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            params = (
                self.current_run_id,
                next_event_id,
                month_index,
                effective_date,
                datetime.now(),
                event_type.value,
                entity_type.value if entity_type else None,
                entity_id,
                action_type.value if action_type else None,
                amount,
                currency if amount else None,
                causal_tag,
                metadata
            )

            affected_rows = self.db.execute_non_query(query, params)

            if affected_rows > 0:
                self.logger.debug(f"Logged event {next_event_id}: {event_type.value} - {metadata}")
                return next_event_id
            else:
                self.logger.error("Failed to log event: no rows affected")
                return None

        except Exception as e:
            self.logger.error(f"Failed to log event: {e}")
            return None

    # ============================================================================
    # STANDARDIZED CONVENIENCE METHODS
    # ============================================================================
    # All convenience methods follow this pattern:
    #   log_<domain>_event(action, message, effective_date, amount, entity_type, entity_id)
    # Metadata format: "{DOMAIN}/{action}: {message}"
    # ============================================================================

    def log_fund_event(self,
                      action: str,
                      message: str,
                      effective_date: Optional[date] = None,
                      amount: Optional[Decimal] = None,
                      entity_type: Optional[EntityType] = None,
                      entity_id: Optional[int] = None) -> Optional[int]:
        """
        Convenience method for logging fund-related events

        Standard signature (updated 2025-12-09)

        Args:
            action: Action identifier (e.g., "DISBURSEMENT", "CSF_DRAW")
            message: Human-readable description
            effective_date: When the event occurred (defaults to today)
            amount: Transaction amount (optional)
            entity_type: Related entity type (optional)
            entity_id: Related entity ID (optional)

        Returns:
            EventID if successful, None otherwise
        """
        if effective_date is None:
            effective_date = date.today()
        return self.log_event(
            event_type=EventType.SIMULATION,
            effective_date=effective_date,
            action_type=ActionType.UPDATE,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=f"FUND/{action}: {message}",
            amount=amount
        )

    def log_acquisition_event(self,
                             action: str,
                             message: str,
                             effective_date: Optional[date] = None,
                             amount: Optional[Decimal] = None,
                             entity_type: Optional[EntityType] = None,
                             entity_id: Optional[int] = None) -> Optional[int]:
        """
        Convenience method for logging acquisition pipeline events

        Standard signature (already compliant)

        Args:
            action: Action identifier (e.g., "OFFER_MADE", "INSPECTION_SCHEDULED")
            message: Human-readable description
            effective_date: When the event occurred (defaults to today)
            amount: Transaction amount (optional)
            entity_type: Related entity type (optional, usually EntityType.ACQUISITION)
            entity_id: Related entity ID (optional, usually AttemptID)

        Returns:
            EventID if successful, None otherwise
        """
        if effective_date is None:
            effective_date = date.today()
        return self.log_event(
            event_type=EventType.SIMULATION,
            effective_date=effective_date,
            action_type=ActionType.UPDATE,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=f"ACQUISITION/{action}: {message}",
            amount=amount
        )

    def log_module_event(self, module_name: str, action: ActionType, message: str,
                        effective_date: Optional[date] = None,
                        month_index: Optional[int] = None,
                        entity_type: Optional[EntityType] = None,
                        entity_id: Optional[int] = None) -> Optional[int]:
        """Log a module-level event"""
        return self.log_event(
            event_type=EventType.MODULE,
            effective_date=effective_date or date.today(),
            month_index=month_index,
            entity_type=entity_type,
            entity_id=entity_id,
            action_type=action,
            causal_tag=module_name,
            metadata=message
        )

    def log_database_event(self, action: ActionType, message: str,
                          table_name: Optional[str] = None,
                          record_count: Optional[int] = None,
                          effective_date: Optional[date] = None,
                          month_index: Optional[int] = None) -> Optional[int]:
        """Log a database operation event"""
        metadata = message
        if table_name:
            metadata += f" [Table: {table_name}]"
        if record_count is not None:
            metadata += f" [Records: {record_count}]"

        return self.log_event(
            event_type=EventType.DATABASE,
            effective_date=effective_date or date.today(),
            month_index=month_index,
            entity_type=EntityType.DATABASE,
            action_type=action,
            metadata=metadata
        )
    
    
    def log_error(self, error_message: str, exception: Optional[Exception] = None,
                  context: Optional[str] = None) -> Optional[int]:
        """Log an error event"""
        metadata = error_message
        if context:
            metadata = f"{context}: {metadata}"
        if exception:
            metadata += f" [Exception: {str(exception)}]"

        return self.log_event(
            event_type=EventType.ERROR,
            effective_date=date.today(),
            action_type=ActionType.FAIL,
            metadata=metadata
        )

    def clear_events(self, run_id: Optional[int] = None) -> bool:
        """Clear events for a specific run"""
        try:
            use_run_id = run_id or self.current_run_id
            if not use_run_id:
                self.logger.error("No run ID specified for event clearing")
                return False

            query = "DELETE FROM simulation.Event WHERE RunID = ?"
            affected_rows = self.db.execute_non_query(query, (use_run_id,))
            self.logger.info(f"Cleared {affected_rows} events for run {use_run_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to clear events: {e}")
            return False
