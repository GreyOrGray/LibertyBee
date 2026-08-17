"""
Inflation Engine - Generate and manage inflation schedules for simulations

Discrete-regime Markov model with PER-CATEGORY effects,
replacing the flat lockstep scenario multiplier (one scalar to all four
categories — the model structurally could not have a bad year, and the
max(0) clamp made even a modeled downturn inert). The real "inflation hurts
landlords" mechanism is DECOUPLING, not magnitude (evidence_base §7: 2008 —
property −27% while rent sat sticky-flat, OpEx kept rising, vacancy +3–5pt).

Modes (INF.Mode, per-projection overridable):
- Regime  — THE default baseline (Gray 2026-07-02): seeded Markov chain over
  Normal / Surge / Normalization / DownturnFinancial / DownturnShock; each
  regime carries its own annual mean+vol per category (rent/OpEx/property)
  and a vacancy LEVEL delta; persistence via the transition-matrix diagonal.
  Rates may go NEGATIVE (the gate-① clamp removal) — every consumer
  compounds Π(1+rate) and handles factors < 1.
- Static  — flat annual/12 from the 4 INF.*InflationRate params (the earlier
  behavior; retained for the matrix report + two-point attribution).
- Archetype library — per-projection INF.ForcedRegime(+StartMonth/DwellMonths)
  scripts a single regime window (Normal elsewhere, transitions OFF) for
  deterministic, comparable per-archetype matrix runs. Absence = not forced.

One-shot pre-sim: generates all months into simulation.InflationSchedule at
month 0; consumers read the table. Seeded via a DEDICATED stream
(run_seed + 30930 — invariant; the old code seeded the GLOBAL random
module, the only engine module that did). All regime params are registry
rows (declared decisions, swept — see V00050).
"""

import bisect
import logging
import math
import random
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from decimal import Decimal

from database_manager import DatabaseManager
from configuration_loader import ConfigurationLoader, ProjectionConfig
from event_logger import EventLogger, EventType, EntityType, ActionType
from parameter_registry import ParameterRegistry

INFLATION_REGIME_SEED_OFFSET = 30930
# Wage growth rides the SAME regime path as rent, generated in
# a SECOND pass with its OWN dedicated stream — so the regime/rent/opex/property
# draws for a given seed remain IDENTICAL to the pre-1.8 engine (same market,
# only incomes change: the V03R2->V03R3 attribution property).
WAGE_GROWTH_SEED_OFFSET = 30931

# Fixed regime order — transition draws walk this list cumulatively, so the
# order is part of the seed-reproducibility contract (do not reorder).
REGIMES = ("Normal", "Surge", "Normalization", "DownturnFinancial", "DownturnShock")

MODE_REGIME = "Regime"
MODE_STATIC = "Static"

@dataclass
class MonthlyInflationRates:
    """Monthly inflation rates for all categories"""
    month_index: int
    inflation_date: date
    general_rate: Decimal
    rent_rate: Decimal
    opex_rate: Decimal
    property_rate: Decimal
    scenario_type: str
    scenario_phase: Optional[str] = None
    notes: Optional[str] = None
    vacancy_delta: Decimal = Decimal("0")
    # Monthly nominal wage-growth rate for this month's
    # regime — the income model compounds Π(1+rate) exactly as rent does.
    wage_rate: Decimal = Decimal("0")

@dataclass
class InflationSchedule:
    """Complete inflation schedule for a simulation run"""
    run_id: int
    projection_config: ProjectionConfig
    monthly_rates: List[MonthlyInflationRates]
    total_months: int
    random_seed: int

class InflationEngine:
    """Generate and manage inflation schedules"""

    def __init__(self, db_manager: DatabaseManager, config_loader: ConfigurationLoader):
        self.db = db_manager
        self.config_loader = config_loader
        self.logger = logging.getLogger(__name__)
        self.event_logger = EventLogger(db_manager)
        # run_id -> (dates, prefix OpEx factors); static post-generation,
        # invalidated by _save_schedule_to_db (step 0.5)
        self._opex_factor_cache: dict = {}


    def generate_schedule(self, run_id: int, projection_id: int, random_seed: int) -> Optional[InflationSchedule]:
        """Generate complete inflation schedule for a simulation run (pre-simulation setup, month_index=0)"""
        # Set run context for event logging
        self.event_logger.set_run_id(run_id)

        try:
            # Load projection configuration first (needed for start_date)
            projection = self.config_loader.load_projection(projection_id)
            if not projection:
                error_msg = f"Cannot generate inflation schedule: projection {projection_id} not found"
                self.logger.error(error_msg)
                self.event_logger.log_error(error_msg, context="InflationEngine.generate_schedule")
                return None

            # Use start_date for all inflation events (pre-simulation, month 0)
            start_date = projection.start_date

            # Log module execution start
            self.event_logger.log_module_event(
                module_name="InflationEngine",
                action=ActionType.START,
                message=f"Starting inflation schedule generation (pre-simulation setup)",
                effective_date=start_date,
                month_index=0,
                entity_type=EntityType.INFLATION
            )

            # Dedicated seeded stream (offset 30930) — never the global
            # random module (the earlier code was the only global-seeder).
            rng = random.Random(random_seed + INFLATION_REGIME_SEED_OFFSET)
            # Regime/mode params read here (fail-loud), NOT threaded through
            # ProjectionConfig — ~65 registry rows; manager-reads-own idiom.
            reg = ParameterRegistry(self.db).load(projection_id)
            mode = reg.get_str('INF', 'Mode')
            self.logger.info(f"Generating inflation schedule for run {run_id} with seed {random_seed}, mode {mode}")

            self.event_logger.log_module_event(
                module_name="InflationEngine",
                action=ActionType.INITIALIZE,
                message=f"Projection {projection_id}: {projection.projection_name}, seed={random_seed}, mode={mode}",
                effective_date=start_date,
                month_index=0,
                entity_type=EntityType.INFLATION
            )

            # Calculate total months
            total_months = self._calculate_total_months(projection.start_date, projection.end_date)

            self.event_logger.log_module_event(
                module_name="InflationEngine",
                action=ActionType.VALIDATE,
                message=f"Calculated {total_months} months from {projection.start_date} to {projection.end_date}",
                effective_date=start_date,
                month_index=0,
                entity_type=EntityType.INFLATION
            )

            # Generate monthly rates
            monthly_rates = self._generate_monthly_rates(projection, total_months, mode, reg, rng)

            # Wage-growth second pass over the SAME regime
            # months, on its own dedicated stream (30931) — existing draws
            # untouched, so pre-1.8 schedules reproduce bit-identically.
            self._apply_wage_rates(monthly_rates, mode, reg, random_seed)

            self.event_logger.log_module_event(
                module_name="InflationEngine",
                action=ActionType.CREATE,
                message=f"Generated {len(monthly_rates)} monthly inflation rates",
                effective_date=start_date,
                month_index=0,
                entity_type=EntityType.INFLATION
            )

            # Create schedule object
            schedule = InflationSchedule(
                run_id=run_id,
                projection_config=projection,
                monthly_rates=monthly_rates,
                total_months=total_months,
                random_seed=random_seed
            )

            # Save to database
            if self._save_schedule_to_db(schedule):
                self.logger.info(f"Generated inflation schedule: {total_months} months, "
                               f"mode {mode}")

                self.event_logger.log_module_event(
                    module_name="InflationEngine",
                    action=ActionType.COMPLETE,
                    message=f"Successfully generated and saved inflation schedule: {total_months} months, mode {mode}",
                    effective_date=start_date,
                    month_index=0,
                    entity_type=EntityType.INFLATION,
                    entity_id=run_id
                )

                return schedule
            else:
                error_msg = "Failed to save inflation schedule to database"
                self.logger.error(error_msg)
                self.event_logger.log_error(error_msg, context="InflationEngine.generate_schedule")
                return None

        except Exception as e:
            error_msg = f"Failed to generate inflation schedule: {e}"
            self.logger.error(error_msg)
            self.event_logger.log_error(error_msg, exception=e, context="InflationEngine.generate_schedule")
            return None

    def get_cumulative_opex_factor(self, run_id: int, target_date: date) -> Decimal:
        """Cumulative compound OpEx inflation factor from sim start through target_date.

        Centralizes the per-month compounding so
        the OpEx charge, CSF target, and Cash floor all see the same factor.
        Mirrors the rent-side pattern in tenant_manager._compute_inflation_adjusted_rent.

        Step 0.5: the schedule is static once _save_schedule_to_db has written
        it (pre-simulation), yet this factor re-queried it 14.9k times per
        240-month run (measured). The rows and their sequential prefix
        products are cached per run and bisected by date — the prefix
        accumulation is the SAME left-to-right Decimal multiplication as the
        old per-call loop, so every returned factor is bit-identical. The
        cache is invalidated whenever the schedule is (re)written.
        """
        cache = self._opex_factor_cache.get(run_id)
        if cache is None:
            rows = self.db.execute_query(
                "SELECT InflationDate, OpExRate FROM simulation.InflationSchedule "
                "WHERE RunID = ? ORDER BY InflationDate",
                (run_id,),
            )
            dates, prefix = [], []
            factor = Decimal('1.0')
            for d, rate in rows:
                factor *= (Decimal('1.0') + Decimal(str(rate)))
                dates.append(d)
                prefix.append(factor)
            cache = (dates, prefix)
            self._opex_factor_cache[run_id] = cache
        dates, prefix = cache
        idx = bisect.bisect_right(dates, target_date) - 1
        return prefix[idx] if idx >= 0 else Decimal('1.0')

    def _calculate_total_months(self, start_date: date, end_date: date) -> int:
        """Calculate total months between start and end dates"""
        # Add 1 to include both start and end months
        months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
        return months

    def _generate_monthly_rates(self, projection: ProjectionConfig, total_months: int,
                                mode: str, reg, rng: random.Random) -> List[MonthlyInflationRates]:
        """Generate monthly rates — Static (flat) or Regime (Markov).

        Regime mode consumes RNG in a FIXED per-month pattern (3 gauss draws +
        1 transition uniform), unconditionally, so the stream stays aligned
        regardless of parameter values (invariant). NO max(0) clamp —
        downturn regimes must produce negative rates (gate ①). Forced-archetype
        runs (per-projection INF.ForcedRegime) script the regime path and
        consume NO transition draws (deterministic scenarios).
        """
        monthly_rates = []
        current_date = projection.start_date
        base_general = float(projection.general_inflation_rate) / 12

        if mode == MODE_STATIC:
            # Earlier behavior: flat annual/12, no draws, no clamp needed
            # (positive by construction). Retained for the matrix report +
            # the two-point re-baseline attribution.
            base_rent = float(projection.rent_inflation_rate) / 12
            base_opex = float(projection.opex_inflation_rate) / 12
            base_property = float(projection.property_inflation_rate) / 12
            for month_index in range(total_months):
                monthly_rates.append(MonthlyInflationRates(
                    month_index=month_index + 1,
                    inflation_date=current_date,
                    general_rate=Decimal(str(round(base_general, 6))),
                    rent_rate=Decimal(str(round(base_rent, 6))),
                    opex_rate=Decimal(str(round(base_opex, 6))),
                    property_rate=Decimal(str(round(base_property, 6))),
                    scenario_type=MODE_STATIC,
                    scenario_phase=None,
                    notes="Static: flat annual/12",
                    vacancy_delta=Decimal("0"),
                ))
                current_date = current_date + relativedelta(months=1)
            return monthly_rates

        if mode != MODE_REGIME:
            raise ValueError(f"INF.Mode must be '{MODE_REGIME}' or '{MODE_STATIC}' (got '{mode}')")

        # --- Regime mode -------------------------------------------------------
        # Per-regime params (annual; converted monthly here). Fail-loud reads.
        params = {}
        for regime in REGIMES:
            params[regime] = {
                'rent_mean': reg.get_float('INF', f'{regime}_RentMean') / 12,
                'rent_vol': reg.get_float('INF', f'{regime}_RentVol') / math.sqrt(12),
                'opex_mean': reg.get_float('INF', f'{regime}_OpExMean') / 12,
                'opex_vol': reg.get_float('INF', f'{regime}_OpExVol') / math.sqrt(12),
                'prop_mean': reg.get_float('INF', f'{regime}_PropertyMean') / 12,
                'prop_vol': reg.get_float('INF', f'{regime}_PropertyVol') / math.sqrt(12),
                'vacancy_delta': reg.get_float('INF', f'{regime}_VacancyDelta'),  # LEVEL, not /12
            }
        transitions = {}
        for src in REGIMES:
            row = [reg.get_float('INF', f'Trans_{src}_{dst}') for dst in REGIMES]
            if abs(sum(row) - 1.0) > 1e-6:
                raise ValueError(f"INF.Trans_{src}_* rows sum to {sum(row)}, not 1.0")
            transitions[src] = row

        start_regime = reg.get_str('INF', 'StartRegime')
        if start_regime not in REGIMES:
            raise ValueError(f"INF.StartRegime '{start_regime}' is not one of {REGIMES}")

        # Archetype library: per-projection ForcedRegime scripts the path
        # (absence = normal stochastic chain — the registry carries no NULLs).
        forced = reg.get_str('INF', 'ForcedRegime') if reg.has('INF', 'ForcedRegime') else None
        if forced is not None:
            if forced not in REGIMES:
                raise ValueError(f"INF.ForcedRegime '{forced}' is not one of {REGIMES}")
            forced_start = reg.get_int('INF', 'ForcedRegimeStartMonth')
            forced_dwell = reg.get_int('INF', 'ForcedRegimeDwellMonths')

        regime = start_regime
        for month_index in range(total_months):
            month_1idx = month_index + 1
            if forced is not None:
                # Scripted: the archetype occupies its window, Normal elsewhere.
                in_window = forced_start <= month_1idx < forced_start + forced_dwell
                regime = forced if in_window else "Normal"

            p = params[regime]
            # Fixed draw pattern: 3 gauss per month, always consumed.
            final_rent = p['rent_mean'] + rng.gauss(0, p['rent_vol'])
            final_opex = p['opex_mean'] + rng.gauss(0, p['opex_vol'])
            final_property = p['prop_mean'] + rng.gauss(0, p['prop_vol'])
            # NO clamp (gate ①): negative monthly rates are the point — every
            # consumer compounds Π(1+rate) and handles factors < 1.

            monthly_rates.append(MonthlyInflationRates(
                month_index=month_1idx,
                inflation_date=current_date,
                general_rate=Decimal(str(round(base_general, 6))),  # dead consumer; flat
                rent_rate=Decimal(str(round(final_rent, 6))),
                opex_rate=Decimal(str(round(final_opex, 6))),
                property_rate=Decimal(str(round(final_property, 6))),
                scenario_type=MODE_REGIME,
                scenario_phase=regime,
                notes=f"regime={regime}" + (" (forced)" if forced is not None else ""),
                vacancy_delta=Decimal(str(round(p['vacancy_delta'], 4))),
            ))
            current_date = current_date + relativedelta(months=1)

            # Transition draw for NEXT month (stochastic chain only). Walks
            # REGIMES in fixed order — part of the reproducibility contract.
            if forced is None:
                draw = rng.random()
                cumulative = 0.0
                for idx, prob in enumerate(transitions[regime]):
                    cumulative += prob
                    if draw < cumulative:
                        regime = REGIMES[idx]
                        break

        return monthly_rates

    def _apply_wage_rates(self, monthly_rates: List[MonthlyInflationRates],
                          mode: str, reg, random_seed: int) -> None:
        """Stamp each month's nominal wage-growth rate.

        Regime mode: rate = {regime}_WageMean/12 + gauss(0, {regime}_WageVol/√12)
        read off the month's ALREADY-DRAWN regime (scenario_phase) — the shared
        regime path, band multipliers applied downstream (income_model). One
        gauss per month on the dedicated 30931 stream (fixed draw pattern).
        Static mode: flat Normal_WageMean/12, no draws (mirrors the flat-rate
        semantics of the Static rent/opex/property legs).
        """
        flat_monthly = reg.get_float('INC', 'Normal_WageMean') / 12
        if mode == MODE_STATIC:
            for mr in monthly_rates:
                mr.wage_rate = Decimal(str(round(flat_monthly, 6)))
            return

        wage_params = {}
        for regime in REGIMES:
            wage_params[regime] = (
                reg.get_float('INC', f'{regime}_WageMean') / 12,
                reg.get_float('INC', f'{regime}_WageVol') / math.sqrt(12),
            )
        wage_rng = random.Random(random_seed + WAGE_GROWTH_SEED_OFFSET)
        for mr in monthly_rates:
            regime = mr.scenario_phase
            if regime not in wage_params:
                raise ValueError(
                    f"Wage pass: month {mr.month_index} has no recognizable regime "
                    f"(scenario_phase={mr.scenario_phase!r})"
                )
            mean, vol = wage_params[regime]
            mr.wage_rate = Decimal(str(round(mean + wage_rng.gauss(0, vol), 6)))

    def _save_schedule_to_db(self, schedule: InflationSchedule) -> bool:
        """Save inflation schedule to database (pre-simulation, month 0)"""
        # the schedule for this run is about to change — drop the factor cache
        self._opex_factor_cache.pop(schedule.run_id, None)
        try:
            # Get start date from first rate record
            start_date = schedule.monthly_rates[0].inflation_date if schedule.monthly_rates else date.today()

            # Clear any existing schedule for this run
            self.event_logger.log_database_event(
                action=ActionType.DELETE,
                message=f"Clearing existing inflation schedule for run {schedule.run_id}",
                table_name="simulation.InflationSchedule",
                effective_date=start_date,
                month_index=0
            )

            delete_query = "DELETE FROM simulation.InflationSchedule WHERE RunID = ?"
            deleted_rows = self.db.execute_non_query(delete_query, (schedule.run_id,))

            if deleted_rows > 0:
                self.event_logger.log_database_event(
                    action=ActionType.DELETE,
                    message=f"Deleted {deleted_rows} existing inflation records",
                    table_name="simulation.InflationSchedule",
                    record_count=deleted_rows,
                    effective_date=start_date,
                    month_index=0
                )

            # Insert new schedule (+ VacancyDelta, the regime vacancy channel;
            # + WageGrowthRate, the shared-regime income channel)
            insert_query = """
            INSERT INTO simulation.InflationSchedule (
                RunID, MonthIndex, InflationDate, GeneralRate, RentRate,
                OpExRate, PropertyRate, ScenarioType, ScenarioPhase, Notes, VacancyDelta,
                WageGrowthRate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            rows_inserted = 0
            for rate in schedule.monthly_rates:
                params = (
                    schedule.run_id,
                    rate.month_index,
                    rate.inflation_date,
                    rate.general_rate,
                    rate.rent_rate,
                    rate.opex_rate,
                    rate.property_rate,
                    rate.scenario_type,
                    rate.scenario_phase,
                    rate.notes,
                    rate.vacancy_delta,
                    rate.wage_rate
                )

                affected = self.db.execute_non_query(insert_query, params)
                rows_inserted += affected

            self.logger.info(f"Saved {rows_inserted} inflation rate records to database")

            # Log database operation
            self.event_logger.log_database_event(
                action=ActionType.CREATE,
                message=f"Inserted inflation schedule records",
                table_name="simulation.InflationSchedule",
                record_count=rows_inserted,
                effective_date=start_date,
                month_index=0
            )

            success = rows_inserted == len(schedule.monthly_rates)
            if not success:
                self.event_logger.log_error(
                    f"Inflation schedule save incomplete: {rows_inserted}/{len(schedule.monthly_rates)} records saved",
                    context="InflationEngine._save_schedule_to_db"
                )

            return success

        except Exception as e:
            error_msg = f"Failed to save inflation schedule: {e}"
            self.logger.error(error_msg)
            self.event_logger.log_error(error_msg, exception=e, context="InflationEngine._save_schedule_to_db")
            return False
