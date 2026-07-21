"""
Employee Manager - Property/Unit-Based Staffing with CSF Integration

This module implements the employee lifecycle management for Liberty Bee:
1. Property/unit-based hiring triggers
2. Annual raises tied to inflation parameters
3. Integration with fund manager for payroll processing
4. Complete event logging for audit trail

Key Logic (mixed model, Gray 2026-07-02):
- Core staff: STAFF.BaseAdminCount floor (tenant-ops Property Manager +
  business-side Administration Manager) — self-management is the mission core
- Maintenance: 0 FTE below STAFF.MaintCrossoverProperties (fully contracted —
  the MAINT event streams carry the cost); 1 tech per crossover-properties above
- Admin: per-UNIT — early years 1 per STAFF_UnitsPerAdmin_Early units, later
  1 per STAFF_UnitsPerAdmin_Late units (2026-06-08 per-unit ruling stands)
- Raises: Annual inflation-based adjustments at year end

Usage:
- Use EmployeeManager.check_staffing_needs() monthly during property acquisition
- Use EmployeeManager.process_annual_raises() at year end
- Use EmployeeManager.process_monthly_payroll() for regular payroll
"""

import logging
import random
import math
from typing import Optional, List, Tuple, Dict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from decimal import Decimal

from database_manager import DatabaseManager
from event_logger import EventLogger
from fund_manager import FundManager
from parameter_registry import ParameterRegistry


@dataclass
class EmployeeRecord:
    """Current employee information"""
    employee_id: int
    role_id: int
    role_name: str
    hired_date: date
    base_salary: Decimal
    benefits_cost: Decimal
    is_active: bool
    terminated_date: Optional[date] = None


@dataclass
class StaffingNeeds:
    """Staffing requirements calculation"""
    total_properties: int
    total_units: int
    years_operating: float
    maintenance_needed: int
    admin_needed: int
    current_maintenance: int
    current_admin: int
    needs_maintenance_hire: bool
    needs_admin_hire: bool


class EmployeeManager:
    """Manage employee lifecycle with property/unit-based triggers"""

    def __init__(self, db_manager: DatabaseManager, fund_manager: FundManager,
                 event_logger: Optional[EventLogger] = None):
        self.db = db_manager
        self.fund_manager = fund_manager
        self.event_logger = event_logger
        self.logger = logging.getLogger(__name__)

        # benefits % from the registry (STAFF.BenefitsPct,
        # global) — was hardcoded 0.25 at the hire + raise sites. Read-once, fail-loud.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.benefits_pct = self.registry.get_decimal('STAFF', 'BenefitsPct')

        # minimum core-team count from the registry (STAFF.*,
        # global) — was hardcoded base_admin=2. Fail-loud. Retired
        # BaseMaintenanceCount: there is no day-1 maintenance FTE — maintenance
        # is contracted below the property crossover (the MAINT event streams
        # carry the cost), in-house tech(s) above it.
        self.base_admin_count = self.registry.get_int('STAFF', 'BaseAdminCount')

        # raise-tier cash-cushion thresholds (months of
        # the honest OpEx denominator) — were hardcoded 12/10/8.
        self.raise_tier_top_cushion_months = self.registry.get_int('STAFF', 'RaiseTierTopCushionMonths')
        self.raise_tier_mid_cushion_months = self.registry.get_int('STAFF', 'RaiseTierMidCushionMonths')
        self.raise_tier_low_cushion_months = self.registry.get_int('STAFF', 'RaiseTierLowCushionMonths')

        # KD-042: memo for expected_annual_role_cost (reference.EmployeeRole
        # is static; the gate-basis provider calls this daily).
        self._expected_role_cost_cache: dict = {}



    def get_owned_property_count(self, run_id: int, as_of_date: date) -> int:
        """Get count of properties owned by Liberty Bee"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*)
                FROM simulation.Properties
                WHERE RunID = ? AND AcquisitionDate <= ?
            """, (run_id, as_of_date))

            result = cursor.fetchone()
            return result[0] if result else 0

    def get_owned_unit_count(self, run_id: int, as_of_date: date) -> int:
        """Get total units in all owned properties"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(u.UnitID)
                FROM simulation.Properties p
                INNER JOIN reference.Units u ON p.AddressID = u.PropertyID
                WHERE p.RunID = ? AND p.AcquisitionDate <= ?
            """, (run_id, as_of_date))

            result = cursor.fetchone()
            return int(result[0]) if result and result[0] else 0

    def get_active_employees(self, run_id: int, as_of_date: date) -> List[EmployeeRecord]:
        """Get all active employees as of specified date"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.EmployeeID, e.RoleID, er.Role, e.HiredDate,
                       e.BaseSalary, e.BenefitsCost, e.IsActive, e.TerminatedDate
                FROM simulation.Employees e
                INNER JOIN reference.EmployeeRole er ON e.RoleID = er.RoleID
                WHERE e.RunID = ?
                  AND e.HiredDate <= ?
                  AND (e.TerminatedDate IS NULL OR e.TerminatedDate > ?)
                  AND e.IsActive = 1
            """, (run_id, as_of_date, as_of_date))

            employees = []
            for row in cursor.fetchall():
                employees.append(EmployeeRecord(
                    employee_id=row[0],
                    role_id=row[1],
                    role_name=row[2],
                    hired_date=row[3],
                    base_salary=Decimal(str(row[4])),
                    benefits_cost=Decimal(str(row[5])),
                    is_active=bool(row[6]),
                    terminated_date=row[7]
                ))

            return employees

    def count_employees_by_role(self, employees: List[EmployeeRecord], role_name: str) -> int:
        """Count active employees in specified role"""
        return len([emp for emp in employees if emp.role_name == role_name])

    @staticmethod
    def staffing_formula(total_properties: int, total_units: int,
                         years_operating: float, maint_crossover_properties: int,
                         units_per_admin_early: int, units_per_admin_late: int,
                         early_admin_years: int, base_admin_count: int) -> "tuple[int, int]":
        """(maintenance_needed, admin_needed) at the given portfolio size.

        The pure staffing rule, extracted (KD-042) so calculate_staffing_needs
        (owned counts) and the pro-forma acquisition gate (owned + in-pipeline
        counts) evaluate the SAME formula — the marginal staffing step is
        f(owned+pipeline) − f(owned) of this function, never a re-derivation.

        Ratified mixed model (2026-07-02): maintenance
        scales per PROPERTY (coordination/travel tracks buildings, not doors
        — evidence_base §5: first in-house tech ~12-17 buildings). Below the
        crossover: 0 maintenance FTE, fully contracted (the MAINT event
        streams carry the cost). Refines G1.0's per-unit UnitsPerMaintenance.

        KD-030 (#59): admin scales with units, FLOORED at the core team — the
        same `max(base, unit-scaled)` shape maintenance uses. The prior
        `base + ceil(...)` double-counted the fixed overhead; `max` makes
        base_admin a true floor. No maintenance floor by design — 0 FTE below the
        property crossover is the design (contracted maintenance), not a gap.
        """
        maintenance_needed = total_properties // maint_crossover_properties if total_properties > 0 else 0
        admin_threshold = units_per_admin_early if years_operating < early_admin_years else units_per_admin_late
        unit_scaled_admin_needed = math.ceil(total_units / admin_threshold) if total_units > 0 else 0
        return maintenance_needed, max(base_admin_count, unit_scaled_admin_needed)

    def calculate_staffing_needs(self, run_id: int, as_of_date: date,
                               maint_crossover_properties: int, units_per_admin_early: int,
                               units_per_admin_late: int, early_admin_years: int,
                               start_date: date) -> StaffingNeeds:
        """Calculate current staffing needs based on property/unit counts."""

        # Get current portfolio
        total_properties = self.get_owned_property_count(run_id, as_of_date)
        total_units = self.get_owned_unit_count(run_id, as_of_date)

        # Calculate years of operation
        years_operating = (as_of_date - start_date).days / 365.25

        maintenance_needed, admin_needed = self.staffing_formula(
            total_properties, total_units, years_operating,
            maint_crossover_properties, units_per_admin_early,
            units_per_admin_late, early_admin_years, self.base_admin_count)

        # Get current staffing
        current_employees = self.get_active_employees(run_id, as_of_date)
        current_maintenance = self.count_employees_by_role(current_employees, "Maintenance Crew")
        current_admin = self.count_employees_by_role(current_employees, "Administration Manager")
        current_admin += self.count_employees_by_role(current_employees, "Property Manager")  # Both count as admin

        return StaffingNeeds(
            total_properties=total_properties,
            total_units=total_units,
            years_operating=years_operating,
            maintenance_needed=maintenance_needed,
            admin_needed=admin_needed,
            current_maintenance=current_maintenance,
            current_admin=current_admin,
            needs_maintenance_hire=(maintenance_needed > current_maintenance),
            needs_admin_hire=(admin_needed > current_admin)
        )

    def expected_annual_role_cost(self, role_name: str) -> Decimal:
        """KD-042: E[annual cost] of a FUTURE hire in this role.

        Hire salaries are seeded uniform draws in [BaseSalary, SalaryCap]
        (KD-039), so a not-yet-made hire has no knowable salary — the
        pro-forma gate prices its EXPECTATION: the band mean plus benefits.
        Deterministic (reference data only), matching the E[·] basis the
        rest of the gate uses. Memoized per role — reference.EmployeeRole
        is static, and the gate-basis provider runs daily (read-once idiom).
        """
        cached = self._expected_role_cost_cache.get(role_name)
        if cached is not None:
            return cached
        row = self.db.execute_query(
            "SELECT BaseSalary, SalaryCap FROM reference.EmployeeRole WHERE Role = ?",
            (role_name,))
        if not row:
            raise ValueError(f"Role '{role_name}' not found in reference.EmployeeRole")
        base, cap = Decimal(str(row[0][0])), Decimal(str(row[0][1]))
        cost = ((base + cap) / 2 * (1 + self.benefits_pct)).quantize(Decimal('0.01'))
        self._expected_role_cost_cache[role_name] = cost
        return cost

    def get_run_seed(self, run_id: int) -> int:
        """Get the random seed for this run to ensure reproducibility"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT RandomSeed FROM simulation.Run WHERE RunID = ?", (run_id,))
            result = cursor.fetchone()
            return result[0] if result else 12345  # Default seed if not found

    def hire_employee(self, run_id: int, role_name: str, hire_date: date,
                     hire_reason: str) -> int:
        """Hire new employee in specified role"""

        if self.event_logger:
            event_id = self.event_logger.log_fund_event(
                action="HIRE",
                amount=Decimal('0'),  # Will be updated with salary
                message=f"Hiring {role_name}: {hire_reason}",
                effective_date=hire_date
            )
        else:
            event_id = None

        # Get role information
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT RoleID, BaseSalary, SalaryCap
                FROM reference.EmployeeRole
                WHERE Role = ?
            """, (role_name,))

            role_info = cursor.fetchone()
            if not role_info:
                raise ValueError(f"Role '{role_name}' not found in reference.EmployeeRole")

            role_id, min_salary, max_salary = role_info

            # Get run seed for reproducible randomization
            run_seed = self.get_run_seed(run_id)

            # Get next employee ID for this run (run-specific numbering).
            # Fetched before salary assignment so it can seed the per-hire RNG.
            cursor.execute("""
                SELECT ISNULL(MAX(EmployeeID), 0) + 1
                FROM simulation.Employees
                WHERE RunID = ?
            """, (run_id,))
            employee_id = cursor.fetchone()[0]

            # Assign salary within range (seeded, reproducible, per-hire).
            # seed a LOCAL random.Random per hire instead of calling the
            # module-level random.seed(). The old code did random.seed(run_seed +
            # role_id) on the GLOBAL RNG, which (a) poisoned module-level random
            # state for any later caller and (b) gave every same-role hire an
            # identical salary (the seed varied by role, not by hire). Seeding on
            # employee_id varies per hire while staying fully reproducible.
            salary_rng = random.Random(run_seed + role_id * 1000 + employee_id)
            assigned_salary = Decimal(str(salary_rng.uniform(float(min_salary), float(max_salary))))
            assigned_salary = assigned_salary.quantize(Decimal('0.01'))  # Round to cents

            # Calculate benefits (STAFF.BenefitsPct from the registry)
            benefits_cost = assigned_salary * self.benefits_pct

            # Insert employee record with run-specific ID (no identity column)
            cursor.execute("""
                INSERT INTO simulation.Employees
                (RunID, EmployeeID, RoleID, HiredDate, BaseSalary, BenefitsCost, IsActive)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (run_id, employee_id, role_id, hire_date, assigned_salary, benefits_cost))

            conn.commit()

        # Update event with salary information
        if self.event_logger:
            self.event_logger.log_fund_event(
                action="HIRE_COMPLETE",
                amount=assigned_salary,
                message=f"Hired {role_name} (ID: {employee_id}): ${assigned_salary:,}/year + ${benefits_cost:,} benefits",
                effective_date=hire_date
            )

        self.logger.info(f"Hired {role_name} (ID: {employee_id}): ${assigned_salary:,}/year, reason: {hire_reason}")
        return employee_id

    def check_staffing_needs(self, run_id: int, current_date: date,
                           maint_crossover_properties: int, units_per_admin_early: int,
                           units_per_admin_late: int, early_admin_years: int,
                           start_date: date) -> List[int]:
        """Check staffing needs and hire as necessary"""

        staffing = self.calculate_staffing_needs(
            run_id, current_date, maint_crossover_properties,
            units_per_admin_early, units_per_admin_late, early_admin_years, start_date
        )

        hired_employee_ids = []

        self.logger.info(f"Staffing assessment: {staffing.total_properties} properties, {staffing.total_units} units")
        self.logger.info(f"  Maintenance: {staffing.current_maintenance}/{staffing.maintenance_needed}")
        self.logger.info(f"  Admin: {staffing.current_admin}/{staffing.admin_needed}")

        # Hire maintenance staff if needed
        if staffing.needs_maintenance_hire:
            maintenance_to_hire = staffing.maintenance_needed - staffing.current_maintenance
            for i in range(maintenance_to_hire):
                employee_id = self.hire_employee(
                    run_id, "Maintenance Crew", current_date,
                    f"Property crossover: {staffing.total_properties} properties >= "
                    f"{maint_crossover_properties * (staffing.current_maintenance + 1)} "
                    f"(1 tech per {maint_crossover_properties} properties)"
                )
                hired_employee_ids.append(employee_id)

        # Hire admin staff if needed
        if staffing.needs_admin_hire:
            admin_to_hire = staffing.admin_needed - staffing.current_admin
            threshold = units_per_admin_early if staffing.years_operating < early_admin_years else units_per_admin_late

            for i in range(admin_to_hire):
                # Alternate between Administration Manager and Property Manager
                role = "Administration Manager" if i % 2 == 0 else "Property Manager"
                employee_id = self.hire_employee(
                    run_id, role, current_date,
                    f"Unit threshold: {staffing.total_units} units > {threshold * staffing.current_admin} (year {staffing.years_operating:.1f})"
                )
                hired_employee_ids.append(employee_id)

        return hired_employee_ids

    def process_bi_monthly_payroll(self, run_id: int, payroll_date: date) -> Decimal:
        """Process bi-monthly payroll for all active employees (15th and month-end)

        Pays half of monthly amount on the 15th and the other half on the last day of the month.
        """
        employees = self.get_active_employees(run_id, payroll_date)

        if not employees:
            self.logger.info("No active employees for bi-monthly payroll processing")
            return Decimal('0')

        total_payroll = Decimal('0')

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            for employee in employees:
                # Calculate bi-monthly amounts (half of monthly)
                monthly_gross = employee.base_salary / 12
                monthly_benefits = employee.benefits_cost / 12
                bi_monthly_gross = monthly_gross / 2
                bi_monthly_benefits = monthly_benefits / 2
                bi_monthly_total = bi_monthly_gross + bi_monthly_benefits

                # Log individual payroll event
                if self.event_logger:
                    event_id = self.event_logger.log_fund_event(
                        action="PAYROLL",
                        amount=bi_monthly_total,
                        message=f"Bi-monthly payroll: {employee.role_name} (ID: {employee.employee_id}) on {payroll_date}",
                        effective_date=payroll_date
                    )
                else:
                    event_id = None

                # Get next payroll ID for this run (run-specific numbering)
                cursor.execute("""
                    SELECT ISNULL(MAX(PayrollID), 0) + 1
                    FROM simulation.Payroll
                    WHERE RunID = ?
                """, (run_id,))
                payroll_id = cursor.fetchone()[0]

                # Insert payroll record with run-specific ID (no identity column)
                cursor.execute("""
                    INSERT INTO simulation.Payroll
                    (RunID, PayrollID, PayrollDate, EmployeeID, GrossPay, BenefitsCost, TotalCost, EventID)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (run_id, payroll_id, payroll_date, employee.employee_id, bi_monthly_gross,
                     bi_monthly_benefits, bi_monthly_total, event_id))

                total_payroll += bi_monthly_total

            conn.commit()

        # Payroll is a protected operating obligation.
        # process_expense_protected pays from Cash first, then draws from CSF
        # for any shortfall. Cash floor does NOT block protected expenses.
        if total_payroll > 0:
            self.fund_manager.process_expense_protected(
                run_id=run_id,
                expense_amount=total_payroll,
                ledger_date=payroll_date,
                expense_type="Bi-Monthly Payroll"
            )

        self.logger.info(f"Processed bi-monthly payroll for {len(employees)} employees: ${total_payroll:,}")
        return total_payroll


    def _load_salary_caps(self) -> dict:
        """cache role_id -> SalaryCap mapping.

        Used by `process_annual_raises` to clamp post-raise base salaries to the
        role's `reference.EmployeeRole.SalaryCap`. Hard cap.
        No soft-cap / COLA-above-cap behavior in V1.
        """
        rows = self.db.execute_query(
            "SELECT RoleID, SalaryCap FROM reference.EmployeeRole"
        )
        return {int(r[0]): Decimal(str(r[1])) for r in rows}

    def process_annual_raises(self, run_id: int, raise_date: date,
                            raise_pct_min: float, raise_pct_8mo: float,
                            raise_pct_10mo: float, raise_pct_12mo: float,
                            honest_monthly_opex: Optional[Decimal] = None) -> Decimal:
        """Process annual raises based on cash flow reserves.

        cushion denominator now uses the
        honest monthly_opex (payroll + recurring non-payroll OpEx) when supplied
        by the caller, instead of the prior payroll-only `annual_payroll / 12`.
        Falls back to payroll-only if `honest_monthly_opex` is None (preserves
        callers that haven't been updated yet).

        each employee's post-raise base
        salary is clamped to `reference.EmployeeRole.SalaryCap`. Once an
        employee reaches cap, base salary stops increasing (hard cap).
        Benefits remain 0.25 × base salary.
        """
        employees = self.get_active_employees(run_id, raise_date)

        if not employees:
            self.logger.info("No active employees for annual raises")
            return Decimal('0')

        balances = self.fund_manager.get_fund_balances(run_id, raise_date)

        # cushion denominator is the honest
        # monthly_opex (payroll + non-payroll recurring) when caller supplies it.
        # Old behavior (payroll-only) preserved as fallback for callers that
        # haven't been threaded through with the breakdown yet.
        annual_payroll = sum(emp.base_salary + emp.benefits_cost for emp in employees)
        if honest_monthly_opex is not None:
            cushion_denom = Decimal(str(honest_monthly_opex))
            denom_label = "honest monthly_opex (payroll + non-payroll)"
        else:
            cushion_denom = Decimal(str(annual_payroll)) / Decimal(12)
            denom_label = "payroll-only monthly_opex (fallback)"

        top_m = self.raise_tier_top_cushion_months
        mid_m = self.raise_tier_mid_cushion_months
        low_m = self.raise_tier_low_cushion_months
        if balances.cash_balance >= cushion_denom * top_m:
            raise_rate = raise_pct_12mo
            cash_months = f"{top_m}+ months"
        elif balances.cash_balance >= cushion_denom * mid_m:
            raise_rate = raise_pct_10mo
            cash_months = f"{mid_m}-{top_m} months"
        elif balances.cash_balance >= cushion_denom * low_m:
            raise_rate = raise_pct_8mo
            cash_months = f"{low_m}-{mid_m} months"
        else:
            raise_rate = raise_pct_min
            cash_months = f"< {low_m} months"

        # load role caps once per call
        role_caps = self._load_salary_caps()

        total_raise_cost = Decimal('0')
        capped_count = 0

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            for emp in employees:
                old_salary = Decimal(str(emp.base_salary))
                target_salary = old_salary * (Decimal('1') + Decimal(str(raise_rate)))

                # Hard SalaryCap
                role_cap = role_caps.get(int(emp.role_id))
                if role_cap is not None and target_salary > role_cap:
                    new_salary = role_cap
                    capped_count += 1
                else:
                    new_salary = target_salary

                raise_amount = (new_salary - old_salary).quantize(Decimal('0.01'))
                new_salary = new_salary.quantize(Decimal('0.01'))
                new_benefits = (new_salary * self.benefits_pct).quantize(Decimal('0.01'))

                cursor.execute("""
                    UPDATE simulation.Employees
                    SET BaseSalary = ?, BenefitsCost = ?
                    WHERE RunID = ? AND EmployeeID = ?
                """, (new_salary, new_benefits, run_id, emp.employee_id))

                total_raise_cost += raise_amount

            conn.commit()

        if self.event_logger:
            self.event_logger.log_fund_event(
                action="ANNUAL_RAISES",
                amount=total_raise_cost,
                message=(
                    f"Annual raises: {raise_rate:.2%} based on {cash_months} "
                    f"cash reserves (denom: {denom_label}); "
                    f"{capped_count}/{len(employees)} employees at SalaryCap"
                ),
                effective_date=raise_date
            )

        self.logger.info(
            f"Processed annual raises for {len(employees)} employees: "
            f"${total_raise_cost:,} total increase; "
            f"raise rate {raise_rate:.2%} on {cash_months} cushion ({denom_label}); "
            f"{capped_count} at SalaryCap"
        )

        return total_raise_cost
