"""
Simulation Snapshot Instrumentation

Captures periodic end-of-day state snapshots into simulation.RunSnapshot for
trajectory visibility across long-horizon runs.

Usage in simulation.py daily loop:

    snapshot_mgr = SnapshotManager(db_manager, run_id, cadence='QUARTERLY')

    # After daily events processed:
    if snapshot_mgr.should_capture(current_date):
        snapshot_mgr.capture(current_date, source='LIVE')

    # On simulation halt, if last cadence boundary is in the past:
    snapshot_mgr.capture_final(halt_date)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


# Quarter-end dates
_QUARTER_END_MONTHS = {3, 6, 9, 12}
_QUARTER_END_DAYS = {3: 31, 6: 30, 9: 30, 12: 31}


@dataclass
class SnapshotRow:
    """Materialized snapshot fields. Used internally by capture() and by
    backfill_snapshots.py for parity checks."""
    run_id: int
    snapshot_date: date
    month_index: int
    snapshot_cadence: str
    snapshot_source: str
    cash_balance: Decimal
    csf_balance: Decimal
    eip_balance: Decimal
    escrow_balance: Decimal
    total_fund_balance: Decimal
    properties_owned: int
    units_total: int
    units_occupied: int
    units_vacant: int
    units_in_turnover: int
    occupancy_rate: Decimal
    vacancy_rate: Decimal
    active_leases: int
    month_rent_collected: Decimal
    month_opex: Decimal
    month_payroll: Decimal
    employees_active: int
    terminations_cumulative: int
    evictions_cumulative: int
    turnovers_completed: int


class SnapshotManager:
    """Periodic state-snapshot capture for a simulation run."""

    def __init__(self, db_manager, run_id: int, cadence: str = 'QUARTERLY', simulation_start: Optional[date] = None):
        """
        Args:
            db_manager: DatabaseManager
            run_id: Current simulation run ID
            cadence: 'MONTHLY' | 'QUARTERLY' | 'ANNUAL'
            simulation_start: Sim start date (for MonthIndex computation). If None,
                it's resolved from simulation.Run on first use.
        """
        self.db = db_manager
        self.run_id = run_id
        self.cadence = cadence
        self._sim_start = simulation_start

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_capture(self, current_date: date) -> bool:
        """True if `current_date` is a cadence boundary for this run's cadence."""
        if self.cadence == 'MONTHLY':
            # Last day of the month
            next_month = (current_date.month % 12) + 1
            next_year = current_date.year + (1 if current_date.month == 12 else 0)
            from datetime import timedelta
            return (date(next_year, next_month, 1) - timedelta(days=1)) == current_date

        if self.cadence == 'QUARTERLY':
            return (current_date.month in _QUARTER_END_MONTHS
                    and current_date.day == _QUARTER_END_DAYS[current_date.month])

        if self.cadence == 'ANNUAL':
            return current_date.month == 12 and current_date.day == 31

        return False

    def capture(self, current_date: date, source: str = 'LIVE') -> Optional[int]:
        """Capture a snapshot for `current_date`. Skips if a row already exists."""
        # Idempotency: do not duplicate
        exists = self.db.execute_query(
            "SELECT 1 FROM simulation.RunSnapshot WHERE RunID=? AND SnapshotDate=?",
            (self.run_id, current_date),
        )
        if exists:
            return None

        row = self._gather(current_date, source)
        self._insert(row)
        return 1

    def capture_final(self, halt_date: date, source: str = 'LIVE') -> Optional[int]:
        """Capture a final snapshot at halt date if the halt falls
        between cadence boundaries (i.e., the most recent normal snapshot is
        not for halt_date itself)."""
        if self.should_capture(halt_date):
            return self.capture(halt_date, source)
        # Otherwise force-capture
        return self.capture(halt_date, source)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _gather(self, current_date: date, source: str) -> SnapshotRow:
        sim_start = self._get_sim_start()
        month_index = self._month_index(sim_start, current_date)

        cash, csf, eip, escrow = self._fund_balances(current_date)
        properties_owned = self._properties_owned()
        units_total, units_occupied, units_vacant, units_in_turnover = self._unit_counts()
        active_leases = self._active_leases()
        month_rent, month_opex, month_payroll = self._month_aggregates(current_date)
        employees_active = self._employees_active(current_date)
        terms_cum, evict_cum = self._termination_counts(current_date)
        turnovers_done = self._turnovers_completed(current_date)

        total_fund = cash + csf + eip
        if units_total > 0:
            occupancy_rate = Decimal(units_occupied) / Decimal(units_total)
            vacancy_rate = Decimal(units_vacant) / Decimal(units_total)
        else:
            occupancy_rate = Decimal('0')
            vacancy_rate = Decimal('0')

        return SnapshotRow(
            run_id=self.run_id,
            snapshot_date=current_date,
            month_index=month_index,
            snapshot_cadence=self.cadence,
            snapshot_source=source,
            cash_balance=cash,
            csf_balance=csf,
            eip_balance=eip,
            escrow_balance=escrow,
            total_fund_balance=total_fund,
            properties_owned=properties_owned,
            units_total=units_total,
            units_occupied=units_occupied,
            units_vacant=units_vacant,
            units_in_turnover=units_in_turnover,
            occupancy_rate=occupancy_rate.quantize(Decimal('0.000001')),
            vacancy_rate=vacancy_rate.quantize(Decimal('0.000001')),
            active_leases=active_leases,
            month_rent_collected=month_rent,
            month_opex=month_opex,
            month_payroll=month_payroll,
            employees_active=employees_active,
            terminations_cumulative=terms_cum,
            evictions_cumulative=evict_cum,
            turnovers_completed=turnovers_done,
        )

    def _insert(self, r: SnapshotRow) -> None:
        self.db.execute_non_query(
            """
            INSERT INTO simulation.RunSnapshot
            (RunID, SnapshotDate, MonthIndex, SnapshotCadence, SnapshotSource,
             CashBalance, CSFBalance, EIPBalance, EscrowBalance, TotalFundBalance,
             PropertiesOwned, UnitsTotal, UnitsOccupied, UnitsVacant, UnitsInTurnover,
             OccupancyRate, VacancyRate, ActiveLeases,
             MonthRentCollected, MonthOpEx, MonthPayroll, EmployeesActive,
             TerminationsCumulative, EvictionsCumulative, TurnoversCompleted)
            VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?)
            """,
            (
                r.run_id, r.snapshot_date, r.month_index, r.snapshot_cadence, r.snapshot_source,
                r.cash_balance, r.csf_balance, r.eip_balance, r.escrow_balance, r.total_fund_balance,
                r.properties_owned, r.units_total, r.units_occupied, r.units_vacant, r.units_in_turnover,
                r.occupancy_rate, r.vacancy_rate, r.active_leases,
                r.month_rent_collected, r.month_opex, r.month_payroll, r.employees_active,
                r.terminations_cumulative, r.evictions_cumulative, r.turnovers_completed,
            ),
        )

    # ------------------------------------------------------------------
    # Aggregate queries (each returns a value "as of current_date")
    # ------------------------------------------------------------------

    def _get_sim_start(self) -> date:
        if self._sim_start is None:
            row = self.db.execute_query(
                "SELECT StartDate FROM simulation.Run WHERE RunID=?", (self.run_id,)
            )
            self._sim_start = row[0][0]
        return self._sim_start

    @staticmethod
    def _month_index(sim_start: date, current_date: date) -> int:
        """1-based month index."""
        months = (current_date.year - sim_start.year) * 12 + (current_date.month - sim_start.month)
        return months + 1

    def _fund_balances(self, current_date: date):
        """Latest FundLedger row at or before current_date."""
        row = self.db.execute_query(
            """
            SELECT CashBalance, CSFBalance, EIPBalance, EscrowBalance
            FROM simulation.FundLedger
            WHERE RunID=? AND LedgerDate<=?
            ORDER BY LedgerDate DESC, EventID DESC
            OFFSET 0 ROWS FETCH FIRST 1 ROWS ONLY
            """,
            (self.run_id, current_date),
        )
        if not row:
            return Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0')
        return (Decimal(str(row[0][0])), Decimal(str(row[0][1])),
                Decimal(str(row[0][2])), Decimal(str(row[0][3])))

    def _properties_owned(self) -> int:
        return self.db.execute_query(
            "SELECT COUNT(*) FROM simulation.Properties WHERE RunID=?",
            (self.run_id,),
        )[0][0]

    def _unit_counts(self):
        rows = self.db.execute_query(
            """
            SELECT
              COUNT(*) AS Total,
              SUM(CASE WHEN UnitStatus='Occupied' THEN 1 ELSE 0 END) AS Occ,
              SUM(CASE WHEN UnitStatus='Available' THEN 1 ELSE 0 END) AS Vac,
              SUM(CASE WHEN UnitStatus IN ('Turnover','Pending_Move_Out') THEN 1 ELSE 0 END) AS Tov
            FROM simulation.PropertyUnits WHERE RunID=?
            """,
            (self.run_id,),
        )
        if not rows or rows[0][0] is None:
            return 0, 0, 0, 0
        total, occ, vac, tov = rows[0]
        return int(total), int(occ or 0), int(vac or 0), int(tov or 0)

    def _active_leases(self) -> int:
        return self.db.execute_query(
            "SELECT COUNT(*) FROM simulation.Lease WHERE RunID=? AND LeaseStatus='ACTIVE'",
            (self.run_id,),
        )[0][0]

    def _month_aggregates(self, current_date: date):
        """Rent collected / OpEx / Payroll for the calendar month containing
        current_date, computed from FundLedger movements."""
        month_start = current_date.replace(day=1)
        # Next month start as exclusive upper bound
        if current_date.month == 12:
            next_month_start = date(current_date.year + 1, 1, 1)
        else:
            next_month_start = date(current_date.year, current_date.month + 1, 1)

        # Rent collected = CashCredit events from rent collection metadata.
        # Rent income flows as CashCredit (fund_manager.process_income);
        # the prior SUM(CashDebit) read the wrong side of the double-entry ledger
        # and returned 0 for every month. Heuristic via event log: SUM CashCredit
        # where event metadata mentions RENT.
        rent_row = self.db.execute_query(
            """
            SELECT COALESCE(SUM(fl.CashCredit), 0)
            FROM simulation.FundLedger fl
            JOIN simulation.Event e ON e.RunID=fl.RunID AND e.EventID=fl.EventID
            WHERE fl.RunID=? AND fl.LedgerDate >= ? AND fl.LedgerDate < ?
              AND e.Metadata LIKE '%RENT%'
            """,
            (self.run_id, month_start, next_month_start),
        )
        month_rent = Decimal(str(rent_row[0][0])) if rent_row else Decimal('0')

        # OpEx = CashDebit events for recurring non-payroll OpEx.
        # Previously this snapshot was `SUM(CashCredit) ... LIKE '%OPEX%'`
        # which was doubly-broken: (a) no producer wrote events with `OPEX` in
        # metadata so the result was always zero, and (b) even if a producer had
        # existed, expenses live in CashDebit not CashCredit. Both fixed: tighten
        # LIKE to the new producer's tag, and use the correct column.
        opex_row = self.db.execute_query(
            """
            SELECT COALESCE(SUM(fl.CashDebit), 0)
            FROM simulation.FundLedger fl
            JOIN simulation.Event e ON e.RunID=fl.RunID AND e.EventID=fl.EventID
            WHERE fl.RunID=? AND fl.LedgerDate >= ? AND fl.LedgerDate < ?
              AND e.Metadata LIKE '%RECURRING_NONPAYROLL_OPEX%'
            """,
            (self.run_id, month_start, next_month_start),
        )
        month_opex = Decimal(str(opex_row[0][0])) if opex_row else Decimal('0')

        # Payroll = CashDebit where metadata carries the payroll producer tag.
        # Payroll flows as CashDebit (fund_manager.process_expense); the
        # prior SUM(CashCredit) read the wrong side of the ledger and returned 0.
        # The filter must be the specific '%Bi-Monthly Payroll%', NOT '%Payroll%':
        # '%Payroll%' also matches 'RECURRING_NONPAYROLL_OPEX' CSF-draw events
        # (NON-PAYROLL contains PAYROLL), folding OpEx into the payroll figure.
        # Under the old CashCredit column that latent over-match summed to ~0;
        # the CashDebit correction exposed it, so tighten the tag here. Verified
        # to reconcile to simulation.Payroll.TotalCost month-by-month.
        pay_row = self.db.execute_query(
            """
            SELECT COALESCE(SUM(fl.CashDebit), 0)
            FROM simulation.FundLedger fl
            JOIN simulation.Event e ON e.RunID=fl.RunID AND e.EventID=fl.EventID
            WHERE fl.RunID=? AND fl.LedgerDate >= ? AND fl.LedgerDate < ?
              AND e.Metadata LIKE '%Bi-Monthly Payroll%'
            """,
            (self.run_id, month_start, next_month_start),
        )
        month_payroll = Decimal(str(pay_row[0][0])) if pay_row else Decimal('0')

        return month_rent, month_opex, month_payroll

    def _employees_active(self, current_date: date) -> int:
        # Active = hired by current_date AND (not terminated OR terminated after current_date)
        row = self.db.execute_query(
            """
            SELECT COUNT(*)
            FROM simulation.Employees
            WHERE RunID=?
              AND HiredDate <= ?
              AND (TerminatedDate IS NULL OR TerminatedDate > ?)
            """,
            (self.run_id, current_date, current_date),
        )
        return int(row[0][0]) if row else 0

    def _termination_counts(self, current_date: date):
        row = self.db.execute_query(
            """
            SELECT
              COUNT(*) AS Terms,
              SUM(CASE WHEN TerminationType='EVICTION' THEN 1 ELSE 0 END) AS Evicts
            FROM simulation.LeaseTermination
            WHERE RunID=? AND TerminationDate<=?
            """,
            (self.run_id, current_date),
        )
        if not row or row[0][0] is None:
            return 0, 0
        return int(row[0][0]), int(row[0][1] or 0)

    def _turnovers_completed(self, current_date: date) -> int:
        row = self.db.execute_query(
            """
            SELECT COUNT(DISTINCT LeaseID)
            FROM simulation.TurnoverWorkOrder
            WHERE RunID=?
              AND WorkItemType='FINAL_INSPECTION'
              AND Status='COMPLETED'
              AND ActualEndDate<=?
            """,
            (self.run_id, current_date),
        )
        return int(row[0][0]) if row else 0
