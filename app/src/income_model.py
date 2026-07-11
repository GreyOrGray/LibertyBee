"""
Income Model

Generates applicant HOUSEHOLD income as a SUM of independently-drawn earners,
from a renter-calibrated, %-of-median band structure whose dollars move over
simulation time with regime-coupled wage growth.

Replaces the retired static reference.IncomeBand draw (the frozen-income
artifact: 2025-dollar bands sampled unchanged for 240 months while rent
inflated at turnover — the honest screen then rejected an ever-growing
share, starving occupancy into a structural deficit).

Design (ratified D1–D6 + F1):
  D1  Bands are %-of-EarnerMedian tiers (AMI-relative STRUCTURE — the %-of-
      median methodology; AMI is a benchmark LB uses, never a program claim).
      Band DOLLARS = cutoff% × median × cumulative wage factor(date) — they
      track the model-internal median over time.
  D2  Leveled to the Salem RENTER pool (Census B25118; emergent household
      median ≈ $58k), widened both ends; the low tail is KEPT and allowed to
      self-screen/fail honestly (never truncated — the published
      "who LB cannot reach" metric depends on it).
  D3  Every parameter is a registry row (INC.*) — zero hardcoded income
      constants; other markets plug in their own data.
  D4  Wage growth reads the SAME regime path as rent: the cumulative
      factor compounds simulation.InflationSchedule.WageGrowthRate, generated
      by inflation_engine in a second pass (dedicated stream 30931) over the
      SAME regime months — shared regime by construction.
  D5  Earner count by household type: SINGLE=1; COUPLE/FAMILY Bernoulli
      P(2nd earner)=INC.SecondEarnerProb, non-earning partner draws a small
      residual (never hard zero); ROOMMATES = N independent adult draws with
      top-band weights dampened (selection effect; inter-roommate r=0 is a
      flagged assumption — no published data). Couple correlation via
      INC.CoupleSameBandProb (calibrated to realized r≈0.20–0.25). Top band
      draws log-uniform up to INC.TailCapMultiple×median (~4x — the renter-
      thin career tail, never CEO-ratio wide).
  F1  No invented flow-vs-stock upshift: the existing self-selection
      prescreen (~3x-rent standard) shapes the applicant flow endogenously.

Determinism (invariant): all draws use the caller-supplied applicant RNG
(seeded per run/vacancy/date/applicant) — this module owns no RNG stream.
The wage schedule itself is seeded upstream (run_seed + 30931).
"""

import math
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple
import random

BAND_LABELS = ("B1", "B2", "B3", "B4", "B5")


class IncomeModel:
    """Renter-calibrated, time-varying, sum-of-earners household income generator."""

    def __init__(self, db_manager, registry, run_id: int):
        self.db = db_manager
        self.registry = registry
        self.run_id = run_id

        # --- INC params: read-once, fail-loud (no .get() fallbacks) -----------
        reg = self.registry
        self.earner_median_annual = reg.get_decimal('INC', 'EarnerMedianAnnual')
        floor_pct = reg.get_decimal('INC', 'BandFloorPct')
        cutoffs = [reg.get_decimal('INC', f'BandCutoffPct_{i}') for i in (1, 2, 3, 4)]
        cap = reg.get_decimal('INC', 'TailCapMultiple')
        # Band bounds as fractions of the (time-varying) median:
        #   B1 [floor, c1) B2 [c1, c2) B3 [c2, c3) B4 [c3, c4) B5 [c4, cap]
        edges = [floor_pct] + cutoffs + [cap]
        self.band_bounds: Dict[str, Tuple[Decimal, Decimal]] = {
            BAND_LABELS[i]: (edges[i], edges[i + 1]) for i in range(5)
        }
        self.band_weights = [reg.get_float('INC', f'BandWeight_{i}') for i in (1, 2, 3, 4, 5)]
        self.second_earner_prob = reg.get_float('INC', 'SecondEarnerProb')
        self.no_earner_residual_max_pct = reg.get_decimal('INC', 'NoEarnerResidualMaxPct')
        # Fixed-income (non-earner) SINGLE households — the deep-poor renter
        # segment the working-earner bands can't produce (Census: bottom-quintile
        # households average 0.40 earners; SSI ~$11k/yr to avg Social Security
        # ~$23k/yr). Without this state the <$25k renter share runs ~6pt light
        # vs B25118 — a FLATTERING gap (fewer unqualifiable applicants).
        self.single_non_earner_prob = reg.get_float('INC', 'SingleNonEarnerProb')
        self.fixed_income_min_pct = reg.get_decimal('INC', 'FixedIncomeMinPct')
        self.fixed_income_max_pct = reg.get_decimal('INC', 'FixedIncomeMaxPct')
        self.couple_same_band_prob = reg.get_float('INC', 'CoupleSameBandProb')
        roommate_dampen = reg.get_float('INC', 'RoommateTopBandDampen')
        self.roommate_weights = self.band_weights[:3] + [
            w * roommate_dampen for w in self.band_weights[3:]
        ]
        self.band_mults = {
            BAND_LABELS[i]: reg.get_decimal('INC', f'WageBandMult_{i + 1}') for i in range(5)
        }

        # Wage schedule cache: list of (date, monthly_rate) — static after month 0.
        self._wage_rows: Optional[List[Tuple[date, Decimal]]] = None
        # Per-(date, band) cumulative factor cache (fill attempts cluster on dates).
        self._factor_cache: Dict[Tuple[date, str], Decimal] = {}

    # -------------------------------------------------------------------------
    # Wage-growth factors (D4 — the shared regime path)
    # -------------------------------------------------------------------------

    def _load_wage_rows(self) -> List[Tuple[date, Decimal]]:
        if self._wage_rows is None:
            rows = self.db.execute_query(
                """
                SELECT InflationDate, WageGrowthRate
                FROM simulation.InflationSchedule
                WHERE RunID = ?
                ORDER BY InflationDate
                """,
                (self.run_id,)
            )
            self._wage_rows = [(r[0], Decimal(str(r[1]))) for r in rows]
        return self._wage_rows

    def cumulative_wage_factor(self, target_date: date, band: str) -> Decimal:
        """Π(1 + WageGrowthRate × bandMult) over schedule months ≤ target_date.

        Mirrors _compute_inflation_adjusted_rent's Π(1+RentRate) convention —
        the income side compounds off the SAME schedule table, so income and
        rent live on the same regime path (D4). With no schedule the factor is
        1.0 (month-0 dollars), matching the rent-side behavior.
        """
        key = (target_date, band)
        cached = self._factor_cache.get(key)
        if cached is not None:
            return cached
        mult = self.band_mults[band]
        factor = Decimal('1.0')
        for row_date, rate in self._load_wage_rows():
            if row_date > target_date:
                break
            factor *= (Decimal('1.0') + rate * mult)
        self._factor_cache[key] = factor
        return factor

    # -------------------------------------------------------------------------
    # Draws
    # -------------------------------------------------------------------------

    def _draw_band(self, rng: random.Random, weights: List[float]) -> str:
        return rng.choices(BAND_LABELS, weights=weights)[0]

    def _draw_earner_annual(self, rng: random.Random, band: str, target_date: date) -> Decimal:
        """One earner's annual income: banded draw × time-varying median.

        B1–B4: uniform within [lo, hi) × median. B5 (the career tail): LOG-
        uniform within [c4, TailCap] × median — fat-but-bounded right skew
        (ratified D5: ~4x cap per BLS chief-exec median, never CEO-ratio wide).
        """
        lo_frac, hi_frac = self.band_bounds[band]
        median_now = self.earner_median_annual * self.cumulative_wage_factor(target_date, band)
        if band == 'B5':
            lo, hi = math.log(float(lo_frac)), math.log(float(hi_frac))
            frac = Decimal(str(math.exp(rng.uniform(lo, hi))))
        else:
            frac = Decimal(str(rng.uniform(float(lo_frac), float(hi_frac))))
        return (median_now * frac)

    def _draw_residual_annual(self, rng: random.Random, target_date: date) -> Decimal:
        """Non-earning 2nd adult: small residual income (part-time/gig/benefits),
        uniform(0, NoEarnerResidualMaxPct × median). Never hard zero (
        a hard zero puts a discontinuity in the household distribution)."""
        median_now = self.earner_median_annual * self.cumulative_wage_factor(target_date, 'B3')
        max_residual = float(self.no_earner_residual_max_pct * median_now)
        return Decimal(str(rng.uniform(0.0, max_residual)))

    # -------------------------------------------------------------------------
    # Household assembly (D5)
    # -------------------------------------------------------------------------

    def generate_household_income(
        self,
        household_type: str,
        adult_count: int,
        rng: random.Random,
        target_date: date,
    ) -> Tuple[Decimal, str]:
        """Household monthly income (pooled — D6) + the primary earner's band label.

        Draw pattern is FIXED per household type + adult count (invariant):
        the same seeded RNG always consumes the same number of draws for the
        same inputs, so slates stay deterministic.
        """
        if household_type == 'SINGLE':
            # Fixed draw pattern: state + band + value, consumed on every path.
            is_fixed_income = rng.random() < self.single_non_earner_prob
            band = self._draw_band(rng, self.band_weights)
            if is_fixed_income:
                # Non-earner single (retiree/SSI/disability): uniform within the
                # fixed-income range × median; band label reports B1 (bottom tier).
                median_now = self.earner_median_annual * self.cumulative_wage_factor(target_date, 'B1')
                lo = float(self.fixed_income_min_pct * median_now)
                hi = float(self.fixed_income_max_pct * median_now)
                rng.uniform(0, 1)  # consume the earner-value slot (fixed pattern)
                total = Decimal(str(rng.uniform(lo, hi)))
                band = 'B1'
            else:
                total = self._draw_earner_annual(rng, band, target_date)

        elif household_type in ('COUPLE', 'FAMILY'):
            band = self._draw_band(rng, self.band_weights)
            total = self._draw_earner_annual(rng, band, target_date)
            # Second adult: earner (Bernoulli) → same-band-biased draw (the
            # couple correlation, calibrated to realized r≈0.20–0.25); else a
            # small residual. All three draws consumed on every path (fixed
            # pattern) so determinism never depends on the Bernoulli outcome.
            is_earner = rng.random() < self.second_earner_prob
            same_band = rng.random() < self.couple_same_band_prob
            band2 = band if same_band else self._draw_band(rng, self.band_weights)
            if is_earner:
                total += self._draw_earner_annual(rng, band2, target_date)
            else:
                # consume the earner-value draw slot via the residual draw
                total += self._draw_residual_annual(rng, target_date)

        elif household_type == 'ROOMMATES':
            # N independent adult draws, top-band-dampened weights (the
            # selection effect — roommate formation skews lower-income).
            # Inter-roommate correlation r=0: FLAGGED ASSUMPTION (no data).
            band = self._draw_band(rng, self.roommate_weights)
            total = self._draw_earner_annual(rng, band, target_date)
            for _ in range(adult_count - 1):
                b = self._draw_band(rng, self.roommate_weights)
                total += self._draw_earner_annual(rng, b, target_date)

        else:
            raise ValueError(f"Unknown household type: {household_type}")

        monthly = (total / Decimal('12')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return monthly, band
