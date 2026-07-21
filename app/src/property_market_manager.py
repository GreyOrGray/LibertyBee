"""
Property Market Manager
Handles daily property market flow with listing/delisting mechanics.

Key Features:
- Zero-day market initialization (all properties assigned states at start)
- Daily processing with scheduled event triggers
- Seasonal multipliers from FRED data
- Seed-based deterministic randomness for reproducibility
"""

import pyodbc
from decimal import Decimal
from datetime import date, timedelta
from random import Random
from typing import Dict, List, Tuple, Optional
import json

from event_logger import EventType, EntityType, ActionType
from parameter_registry import ParameterRegistry


class PropertyMarketManager:
    """Manages property market state transitions and daily market flow."""

    def __init__(self, db_manager, event_logger, config: Dict):
        """
        Initialize the property market manager.

        Args:
            db_manager: DatabaseManager instance
            event_logger: EventLogger instance for audit trail
            config: Configuration dictionary with projection parameters
        """
        self.db = db_manager
        self.event_logger = event_logger
        self.config = config

        # MARKET params come from
        # the unified registry, read-once + fail-loud — the former
        # reference.ProjectionConstants island (with its silent .get() fallbacks
        # and the SaleSuccessRate/SalePercentage key mismatch) is retired.
        self.registry = ParameterRegistry(self.db).load_globals()
        self.init_listed_pct_mid = self.registry.get_float('MARKET', 'InitListedPctMid')
        self.init_listed_pct_jitter = self.registry.get_float('MARKET', 'InitListedPctJitter')
        self.init_listed_pct_min = self.registry.get_float('MARKET', 'InitListedPctMin')
        self.init_listed_pct_max = self.registry.get_float('MARKET', 'InitListedPctMax')
        self.init_available_pct_mid = self.registry.get_float('MARKET', 'InitAvailablePctMid')
        self.init_available_pct_jitter = self.registry.get_float('MARKET', 'InitAvailablePctJitter')
        self.init_available_pct_min = self.registry.get_float('MARKET', 'InitAvailablePctMin')
        self.init_available_pct_max = self.registry.get_float('MARKET', 'InitAvailablePctMax')
        self.daily_listing_base_pct = self.registry.get_float('MARKET', 'DailyListingBasePct')
        self.daily_listing_window_days = self.registry.get_int('MARKET', 'DailyListingWindowDays')
        self.hold_lt2yr_cum_pct = self.registry.get_float('MARKET', 'HoldLT2YrCumPct')
        self.hold_lt5yr_cum_pct = self.registry.get_float('MARKET', 'HoldLT5YrCumPct')
        self.hold_lt10yr_cum_pct = self.registry.get_float('MARKET', 'HoldLT10YrCumPct')
        self.hold_flip_min_days = self.registry.get_int('MARKET', 'HoldFlipMinDays')
        self.hold_2yr_days = self.registry.get_int('MARKET', 'Hold2YrDays')
        self.hold_5yr_days = self.registry.get_int('MARKET', 'Hold5YrDays')
        self.hold_10yr_days = self.registry.get_int('MARKET', 'Hold10YrDays')
        self.hold_tail_mean_years = self.registry.get_int('MARKET', 'HoldTailMeanYears')
        self.hold_cap_days = self.registry.get_int('MARKET', 'HoldCapDays')
        self.days_on_market_mean = self.registry.get_float('MARKET', 'DaysOnMarket_Mean')
        self.days_on_market_stddev = self.registry.get_float('MARKET', 'DaysOnMarket_StdDev')
        self.days_on_market_min_days = self.registry.get_int('MARKET', 'DaysOnMarketMinDays')
        self.days_on_market_max_days = self.registry.get_int('MARKET', 'DaysOnMarketMaxDays')
        self.sale_success_rate = self.registry.get_float('MARKET', 'SaleSuccessRate')

        # Inflation schedule (loaded per run)
        self.inflation_schedule = {}
        self.simulation_start_date = None

    def _load_inflation_schedule(self, run_id: int, start_date: date) -> None:
        """Load inflation schedule for this run and calculate cumulative inflation."""
        self.simulation_start_date = start_date
        self.inflation_schedule = {}

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT InflationDate, PropertyRate
                FROM simulation.InflationSchedule
                WHERE RunID = ?
                ORDER BY InflationDate
            """, run_id)

            # Calculate cumulative inflation from monthly rates
            cumulative = Decimal('1.0')
            for row in cursor.fetchall():
                monthly_rate = Decimal(str(row.PropertyRate))
                cumulative *= (Decimal('1.0') + monthly_rate)
                self.inflation_schedule[row.InflationDate] = float(cumulative)

            cursor.close()

    def _calculate_inflated_price(self, base_price: Decimal, current_date: date) -> Decimal:
        """
        Calculate inflated price from simulation start to current date.

        Args:
            base_price: Base price (from reference.Properties.PurchasePrice)
            current_date: Date to inflate to

        Returns:
            Inflated price
        """
        if not self.inflation_schedule:
            # No inflation schedule loaded, return base price
            return base_price

        # Find the cumulative inflation factor for current date
        # Use the latest inflation factor <= current_date
        inflation_factor = Decimal('1.0')
        for eff_date, cum_inflation in sorted(self.inflation_schedule.items()):
            if eff_date <= current_date:
                inflation_factor = Decimal(str(cum_inflation))
            else:
                break

        return base_price * inflation_factor

    def _get_listing_price(self, property_id: int, listing_date: date) -> Tuple[Decimal, Decimal]:
        """
        Calculate listing price with inflation and seasonal adjustment.

        Args:
            property_id: Property ID
            listing_date: Date property is being listed

        Returns:
            Tuple of (OriginalBasePrice, CurrentListingPrice)
        """
        # Get base price from reference.Properties.BasePrice
        # (populated from public property reference data during the data load)
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT BasePrice
                FROM reference.Properties
                WHERE PropertyID = ?
            """, property_id)

            row = cursor.fetchone()
            cursor.close()

            if not row or row.BasePrice is None:
                raise ValueError(f"Property {property_id} not found or has no BasePrice")

            base_price = Decimal(str(row.BasePrice))

        # Apply inflation to get original base price (inflated to listing date)
        original_base_price = self._calculate_inflated_price(base_price, listing_date)

        # Apply seasonal price multiplier
        month = listing_date.month
        month_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        multiplier_key = f'PriceMultiplier_{month_names[month]}'
        seasonal_multiplier = Decimal(str(self.registry.get_float('MARKET', multiplier_key)))

        # Calculate current listing price with seasonal adjustment
        current_listing_price = original_base_price * seasonal_multiplier

        return (original_base_price, current_listing_price)

    def initialize_market(self, run_id: int, seed: int, start_date: date) -> None:
        """
        Zero-day market initialization - assign all properties to initial states.

        Distribution (synthetic steady-state; MARKET.Init*
        registry knobs — seeded Listed 1-2% mid 1.5%, Available 3-5% mid 4%,
        OtherBuyerOwned = remainder):

        Uses seed + property_id for deterministic randomness.

        Args:
            run_id: Simulation run ID
            seed: Random seed for reproducibility
            start_date: Simulation start date (required — all market-state
                seeding anchors to it; SIM.StartDate flows in via config)
        """
        # Load inflation schedule for pricing
        self._load_inflation_schedule(run_id, start_date)

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get all properties
            cursor.execute("SELECT PropertyID FROM reference.Properties ORDER BY PropertyID")
            property_ids = [row.PropertyID for row in cursor.fetchall()]
            cursor.close()

        total_properties = len(property_ids)
        self.event_logger.log_event(
            EventType.MODULE,
            start_date,
            entity_type=EntityType.PROPERTY,
            action_type=ActionType.INITIALIZE,
            metadata=f'Initializing market with {total_properties} properties'
        )

        # Calculate target distributions with seed-based variation
        master_rng = Random(seed)

        # Vary the percentages slightly using the seed (jitter half-widths from MARKET knobs)
        listed_pct = self.init_listed_pct_mid + master_rng.uniform(
            -self.init_listed_pct_jitter, self.init_listed_pct_jitter)
        available_pct = self.init_available_pct_mid + master_rng.uniform(
            -self.init_available_pct_jitter, self.init_available_pct_jitter)
        # OtherBuyerOwned gets the remainder

        listed_pct = max(self.init_listed_pct_min, min(self.init_listed_pct_max, listed_pct))
        available_pct = max(self.init_available_pct_min, min(self.init_available_pct_max, available_pct))

        # Calculate actual counts
        listed_count = int(total_properties * listed_pct)
        available_count = int(total_properties * available_pct)
        owned_count = total_properties - listed_count - available_count

        self.event_logger.log_event(
            EventType.MODULE,
            start_date,
            entity_type=EntityType.PROPERTY,
            action_type=ActionType.INITIALIZE,
            metadata=f'Distribution: {listed_count} Listed ({listed_pct*100:.1f}%), '
                     f'{available_count} Available ({available_pct*100:.1f}%), '
                     f'{owned_count} OtherBuyerOwned ({(1-listed_pct-available_pct)*100:.1f}%)'
        )

        # Shuffle properties deterministically
        master_rng.shuffle(property_ids)

        # Assign states
        listed_properties = property_ids[:listed_count]
        available_properties = property_ids[listed_count:listed_count + available_count]
        owned_properties = property_ids[listed_count + available_count:]

        # Process each group
        zero_day = start_date

        # 1. Listed properties (with pricing)
        for prop_id in listed_properties:
            self._initialize_listed_property(run_id, prop_id, zero_day, seed)

        # 2. Available properties
        for prop_id in available_properties:
            self._initialize_available_property(run_id, prop_id, zero_day)

        # 3. OtherBuyerOwned properties
        for prop_id in owned_properties:
            self._initialize_owned_property(run_id, prop_id, zero_day, seed)

        self.event_logger.log_event(
            EventType.MODULE,
            zero_day,
            entity_type=EntityType.PROPERTY,
            action_type=ActionType.COMPLETE,
            metadata=f'Market initialized: {total_properties} properties assigned to initial states'
        )

    def _initialize_listed_property(self, run_id: int, property_id: int, zero_day: date, seed: int) -> None:
        """Initialize a property in Listed state with pricing."""
        # Property-specific RNG
        rng = Random(seed + property_id)

        # Sample days on market (using January since it's month 1)
        days_on_market = self._sample_days_on_market(rng, 1)

        # Schedule sale or withdrawal
        expected_return_date = zero_day + timedelta(days=days_on_market)

        # Calculate listing prices (inflated + seasonal)
        original_base_price, current_listing_price = self._get_listing_price(property_id, zero_day)

        # Insert into PropertyMarket
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get next PropertyMarketID for this run (run-specific numbering)
            cursor.execute("""
                SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                FROM simulation.PropertyMarket
                WHERE RunID = ?
            """, (run_id,))
            property_market_id = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO simulation.PropertyMarket (
                    RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                    ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                    OriginalBasePrice, HoldPeriodDays, LastOwnerType
                ) VALUES (?, ?, ?, 'Listed', ?, NULL, ?, ?, ?, ?, NULL, 'OtherBuyer')
            """, run_id, property_market_id, property_id, zero_day, expected_return_date, days_on_market,
                 current_listing_price, original_base_price)
            conn.commit()
            cursor.close()

    def _initialize_available_property(self, run_id: int, property_id: int, zero_day: date) -> None:
        """Initialize a property in Available state."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get next PropertyMarketID for this run (run-specific numbering)
            cursor.execute("""
                SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                FROM simulation.PropertyMarket
                WHERE RunID = ?
            """, (run_id,))
            property_market_id = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO simulation.PropertyMarket (
                    RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                    ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                    OriginalBasePrice, HoldPeriodDays, LastOwnerType
                ) VALUES (?, ?, ?, 'Available', ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """, run_id, property_market_id, property_id, zero_day)
            conn.commit()
            cursor.close()

    def _initialize_owned_property(self, run_id: int, property_id: int, zero_day: date, seed: int) -> None:
        """Initialize a property in OtherBuyerOwned state."""
        # Property-specific RNG
        rng = Random(seed + property_id)

        # Sample hold period
        hold_period_days = self._sample_hold_period(rng)

        # Schedule return to market
        expected_return_date = zero_day + timedelta(days=hold_period_days)

        # Insert into PropertyMarket
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get next PropertyMarketID for this run (run-specific numbering)
            cursor.execute("""
                SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                FROM simulation.PropertyMarket
                WHERE RunID = ?
            """, (run_id,))
            property_market_id = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO simulation.PropertyMarket (
                    RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                    ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                    OriginalBasePrice, HoldPeriodDays, LastOwnerType
                ) VALUES (?, ?, ?, 'OtherBuyerOwned', ?, NULL, ?, NULL, NULL, NULL, ?, 'OtherBuyer')
            """, run_id, property_market_id, property_id, zero_day, expected_return_date, hold_period_days)
            conn.commit()
            cursor.close()

    def _sample_hold_period(self, rng: Random) -> int:
        """
        Sample hold period using bucket-based distribution from survey data.

        Based on 40k+ multi-family property owner survey:
        - 6%: < 2 years (90-730 days)
        - 14%: 2-5 years (730-1825 days)
        - 9%: 5-10 years (1825-3650 days)
        - 71%: 10+ years (3650-10950 days, exponential decay)

        Args:
            rng: Random instance seeded for this property

        Returns:
            Hold period in days
        """
        bucket = rng.random()

        if bucket < self.hold_lt2yr_cum_pct:  # flips
            return rng.randint(self.hold_flip_min_days, self.hold_2yr_days)

        elif bucket < self.hold_lt5yr_cum_pct:  # 2 to 5 years
            return rng.randint(self.hold_2yr_days, self.hold_5yr_days)

        elif bucket < self.hold_lt10yr_cum_pct:  # 5 to 10 years
            return rng.randint(self.hold_5yr_days, self.hold_10yr_days)

        else:  # remainder - more than 10 years, exponential long tail
            years_above_10 = int(rng.expovariate(1 / self.hold_tail_mean_years))
            days = self.hold_10yr_days + (years_above_10 * 365)
            return min(days, self.hold_cap_days)

    def _sample_days_on_market(self, rng: Random, month: int) -> int:
        """
        Sample days on market with seasonal adjustment.

        Uses Essex County data (26.4 days mean) and seasonal multipliers.

        Args:
            rng: Random instance seeded for this property
            month: Current month (1-12)

        Returns:
            Days on market
        """
        # Get base values from the MARKET registry knobs
        base_mean = self.days_on_market_mean
        base_stddev = self.days_on_market_stddev

        # Get seasonal multiplier for this month
        month_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        multiplier_key = f'DaysOnMarket_SeasonalMultiplier_{month_names[month]}'
        seasonal_multiplier = self.registry.get_float('MARKET', multiplier_key)

        # Adjust mean by seasonal multiplier
        adjusted_mean = base_mean * seasonal_multiplier

        # Sample from normal distribution
        days = int(rng.gauss(adjusted_mean, base_stddev))

        # Clamp to configured range
        return max(self.days_on_market_min_days, min(self.days_on_market_max_days, days))

    def process_daily_market(self, run_id: int, current_date: date, seed: int) -> None:
        """
        Process daily property market events.

        Checks for scheduled transitions:
        1. OtherBuyerOwned → Available (hold period complete)
        2. Available → Listed (daily listing probability)
        3. Listed → OtherBuyerOwned or Available (sale/withdrawal)

        Args:
            run_id: Simulation run ID
            current_date: Current simulation date
            seed: Random seed for daily randomness
        """
        # Daily RNG (seed + date hash for determinism)
        day_seed = seed + current_date.toordinal()
        rng = Random(day_seed)

        # Get current month for seasonal adjustments
        current_month = current_date.month

        # Step 1: Process scheduled returns (OtherBuyerOwned → Available)
        self._process_scheduled_returns(run_id, current_date, rng)

        # Step 2: Process new listings (Available → Listed)
        self._process_new_listings(run_id, current_date, current_month, rng)

        # Step 3: Process sales and withdrawals (Listed → OtherBuyerOwned or Available)
        self._process_listed_outcomes(run_id, current_date, current_month, rng)

    def _process_scheduled_returns(self, run_id: int, current_date: date, rng: Random) -> None:
        """Process properties returning to market from OtherBuyerOwned status."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Find properties scheduled to return today
            cursor.execute("""
                SELECT PropertyID, PropertyMarketID
                FROM simulation.PropertyMarket
                WHERE RunID = ?
                  AND MarketStatus = 'OtherBuyerOwned'
                  AND ExpectedReturnDate = ?
                  AND ExpirationDate IS NULL
            """, run_id, current_date)

            returning_properties = cursor.fetchall()

            for row in returning_properties:
                property_id = row.PropertyID
                old_record_id = row.PropertyMarketID

                # Close old record (SCD Type 2)
                cursor.execute("""
                    UPDATE simulation.PropertyMarket
                    SET ExpirationDate = ?
                    WHERE PropertyMarketID = ? AND RunID = ?
                """, current_date, old_record_id, run_id)

                # Get next PropertyMarketID for this run (run-specific numbering)
                cursor.execute("""
                    SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                    FROM simulation.PropertyMarket
                    WHERE RunID = ?
                """, (run_id,))
                new_property_market_id = cursor.fetchone()[0]

                # Create new Available record
                cursor.execute("""
                    INSERT INTO simulation.PropertyMarket (
                        RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                        ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                        OriginalBasePrice, HoldPeriodDays, LastOwnerType
                    ) VALUES (?, ?, ?, 'Available', ?, NULL, NULL, NULL, NULL, NULL, NULL, 'OtherBuyer')
                """, run_id, new_property_market_id, property_id, current_date)

                # Log event
                self.event_logger.log_event(
                    EventType.MODULE,
                    current_date,
                    entity_type=EntityType.PROPERTY,
                    entity_id=property_id,
                    action_type=ActionType.UPDATE,
                    metadata=f'Property {property_id} returned to market (Available)'
                )

            conn.commit()
            cursor.close()

    def _process_new_listings(self, run_id: int, current_date: date, month: int, rng: Random) -> None:
        """Process Available properties becoming Listed."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get all Available properties.
            # ORDER BY is an invariant-#8 guard (#187): the per-day rng draws
            # one listing roll per row below, so the draw<->property mapping
            # must not ride plan row order. PropertyMarketID = clustered-PK
            # trailing column.
            cursor.execute("""
                SELECT PropertyID, PropertyMarketID
                FROM simulation.PropertyMarket
                WHERE RunID = ?
                  AND MarketStatus = 'Available'
                  AND ExpirationDate IS NULL
                ORDER BY PropertyMarketID
            """, run_id)

            available_properties = cursor.fetchall()

            # Get seasonal listing multiplier
            month_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            multiplier_key = f'SeasonalMultiplier_{month_names[month]}'
            seasonal_multiplier = self.registry.get_float('MARKET', multiplier_key)

            # Base daily listing probability (steady-state listed share / window days)
            base_daily_probability = self.daily_listing_base_pct / self.daily_listing_window_days
            adjusted_probability = base_daily_probability * seasonal_multiplier

            for row in available_properties:
                property_id = row.PropertyID
                old_record_id = row.PropertyMarketID

                # Roll for listing
                if rng.random() < adjusted_probability:
                    # Sample days on market
                    days_on_market = self._sample_days_on_market(rng, month)
                    expected_sale_date = current_date + timedelta(days=days_on_market)

                    # Calculate listing prices (inflated + seasonal)
                    original_base_price, current_listing_price = self._get_listing_price(property_id, current_date)

                    # Close old record
                    cursor.execute("""
                        UPDATE simulation.PropertyMarket
                        SET ExpirationDate = ?
                        WHERE PropertyMarketID = ? AND RunID = ?
                    """, current_date, old_record_id, run_id)

                    # Get next PropertyMarketID for this run (run-specific numbering)
                    cursor.execute("""
                        SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                        FROM simulation.PropertyMarket
                        WHERE RunID = ?
                    """, (run_id,))
                    new_property_market_id = cursor.fetchone()[0]

                    # Create new Listed record with pricing
                    cursor.execute("""
                        INSERT INTO simulation.PropertyMarket (
                            RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                            ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                            OriginalBasePrice, HoldPeriodDays, LastOwnerType
                        ) VALUES (?, ?, ?, 'Listed', ?, NULL, ?, ?, ?, ?, NULL, 'OtherBuyer')
                    """, run_id, new_property_market_id, property_id, current_date, expected_sale_date, days_on_market,
                         current_listing_price, original_base_price)

                    # Log event
                    self.event_logger.log_event(
                        EventType.MODULE,
                        current_date,
                        entity_type=EntityType.PROPERTY,
                        entity_id=property_id,
                        action_type=ActionType.UPDATE,
                        amount=current_listing_price,
                        metadata=f'Property {property_id} listed at ${current_listing_price:,.2f} (expected sale: {expected_sale_date})'
                    )

            conn.commit()
            cursor.close()

    def _process_listed_outcomes(self, run_id: int, current_date: date, month: int, rng: Random) -> None:
        """Process Listed properties reaching expected sale date."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Find listings scheduled to resolve today.
            # ORDER BY is an invariant-#8 guard (#187): the per-day rng draws
            # one sale/withdraw roll (+ hold-period draws) per row below.
            cursor.execute("""
                SELECT PropertyID, PropertyMarketID
                FROM simulation.PropertyMarket
                WHERE RunID = ?
                  AND MarketStatus = 'Listed'
                  AND ExpectedReturnDate = ?
                  AND ExpirationDate IS NULL
                ORDER BY PropertyMarketID
            """, run_id, current_date)

            resolving_listings = cursor.fetchall()

            # Sale-vs-withdrawal probability (fail-loud registry read
            # under the correct key — the old .get('SaleSuccessRate', 0.90)
            # never found the row, which was seeded as 'SalePercentage')
            sale_probability = self.sale_success_rate

            for row in resolving_listings:
                property_id = row.PropertyID
                old_record_id = row.PropertyMarketID

                # Close old record
                cursor.execute("""
                    UPDATE simulation.PropertyMarket
                    SET ExpirationDate = ?
                    WHERE PropertyMarketID = ? AND RunID = ?
                """, current_date, old_record_id, run_id)

                # Roll for sale vs withdrawal
                if rng.random() < sale_probability:
                    # Property sold - goes to OtherBuyerOwned
                    hold_period_days = self._sample_hold_period(rng)
                    expected_return_date = current_date + timedelta(days=hold_period_days)

                    # Get next PropertyMarketID for this run (run-specific numbering)
                    cursor.execute("""
                        SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                        FROM simulation.PropertyMarket
                        WHERE RunID = ?
                    """, (run_id,))
                    new_property_market_id = cursor.fetchone()[0]

                    cursor.execute("""
                        INSERT INTO simulation.PropertyMarket (
                            RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                            ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                            OriginalBasePrice, HoldPeriodDays, LastOwnerType
                        ) VALUES (?, ?, ?, 'OtherBuyerOwned', ?, NULL, ?, NULL, NULL, NULL, ?, 'OtherBuyer')
                    """, run_id, new_property_market_id, property_id, current_date, expected_return_date, hold_period_days)

                    self.event_logger.log_event(
                        EventType.MODULE,
                        current_date,
                        entity_type=EntityType.PROPERTY,
                        entity_id=property_id,
                        action_type=ActionType.UPDATE,
                        metadata=f'Property {property_id} sold (return to market: {expected_return_date})'
                    )
                else:
                    # Listing withdrawn - back to Available

                    # Get next PropertyMarketID for this run (run-specific numbering)
                    cursor.execute("""
                        SELECT ISNULL(MAX(PropertyMarketID), 0) + 1
                        FROM simulation.PropertyMarket
                        WHERE RunID = ?
                    """, (run_id,))
                    new_property_market_id = cursor.fetchone()[0]

                    cursor.execute("""
                        INSERT INTO simulation.PropertyMarket (
                            RunID, PropertyMarketID, PropertyID, MarketStatus, EffectiveDate, ExpirationDate,
                            ExpectedReturnDate, DaysOnMarket, CurrentListingPrice,
                            OriginalBasePrice, HoldPeriodDays, LastOwnerType
                        ) VALUES (?, ?, ?, 'Available', ?, NULL, NULL, NULL, NULL, NULL, NULL, 'OtherBuyer')
                    """, run_id, new_property_market_id, property_id, current_date)

                    self.event_logger.log_event(
                        EventType.MODULE,
                        current_date,
                        entity_type=EntityType.PROPERTY,
                        entity_id=property_id,
                        action_type=ActionType.UPDATE,
                        metadata=f'Property {property_id} listing withdrawn (back to Available)'
                    )

            conn.commit()
            cursor.close()
