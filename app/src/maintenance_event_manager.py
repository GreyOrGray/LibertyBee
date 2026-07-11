"""
Event-driven maintenance draws.

Maintenance is event-driven and heavily skewed, not a smooth per-unit drain
(evidence_base SS5): most units are quiet, a minority drive the load, and the
severity tail is what stresses the reserve. A flat $/unit line can't have a
bad year — and the Strong tier is defined by restoring reserves after shocks,
so the shock must exist to be tested.

Two stochastic streams, drawn once per month at portfolio level:
  - ROUTINE repairs: negative-binomial monthly count (overdispersed; sum of
    per-unit-month NB(r/12, p) draws = portfolio NB with size units*r/12),
    lognormal severity per event (~$200 mean).
  - MAJOR events: Poisson monthly count (low rate), lognormal severity with a
    fat tail (~$2,250 mean, sigma 1.0 -> 99th pct ~$14k). This stream CARRIES
    capital replacement (roof/boiler/HVAC) — treated as "lumpy
    events": no separate smoothed reserve expense line, so cash feels the
    replacement when it actually happens (no double-charge).

The deterministic streams (turnover make-ready per actual turn, exterior per
property) are charged directly by the simulation month-end block — no draw.

RNG: dedicated stream `run_seed + 30910` (invariant — seed-reproducible;
existing offsets: 30403 tcs-onboarding, 30602 deposit, 30707 turnover,
30708 vacancy-fill, 30802 tcs-redemption, 30901 renewal, 30920 vacancy-
fluctuation). Draw order is the month sequence, so same seed => identical
event history. All returned costs are BASE-YEAR dollars — the caller applies
the cumulative OpEx inflation factor at charge time.
"""

import math
import random
from dataclasses import dataclass


# Poisson-by-inversion (Knuth) is exact but underflows for large means;
# LB portfolios stay far below this (mean 500 => ~1,200 units at routine
# lambda 5/unit-yr: units*5/12 = 500).
_MAX_POISSON_MEAN = 500.0

MAINTENANCE_EVENT_SEED_OFFSET = 30910


@dataclass(frozen=True)
class MaintenanceEventDraw:
    """One month's realized maintenance events (base-year dollars)."""
    routine_count: int
    routine_cost: float
    major_count: int
    major_cost: float


class MaintenanceEventManager:
    """Seeded monthly maintenance-event draws (evidence_base SS5)."""

    def __init__(self, config, run_seed: int):
        self.routine_rate_year = float(config.routine_requests_per_unit_year)
        self.routine_dispersion = float(config.routine_dispersion)
        self.routine_cost_mean = float(config.routine_cost_mean)
        self.routine_cost_sigma = float(config.routine_cost_sigma)
        self.major_rate_year = float(config.major_event_rate_per_unit_year)
        self.major_cost_mean = float(config.major_event_cost_mean)
        self.major_cost_sigma = float(config.major_event_cost_sigma)

        if self.routine_dispersion <= 0:
            raise ValueError(
                f"MAINT.RoutineDispersion must be > 0 (got {self.routine_dispersion})"
            )

        # Lognormal parameterized so E[X] = cost_mean: mu = ln(mean) - sigma^2/2.
        self._routine_mu = math.log(self.routine_cost_mean) - self.routine_cost_sigma ** 2 / 2
        self._major_mu = math.log(self.major_cost_mean) - self.major_cost_sigma ** 2 / 2

        self.rng = random.Random(run_seed + MAINTENANCE_EVENT_SEED_OFFSET) \
            if run_seed is not None else random.Random()

    # ---- expected values (the smooth basis for CSF target / acquisition gate) --

    def expected_monthly_routine_cost(self, owned_units: int) -> float:
        """E[routine $/month] = units * lambda * mean / 12 (base-year dollars)."""
        return owned_units * self.routine_rate_year * self.routine_cost_mean / 12.0

    def expected_monthly_major_cost(self, owned_units: int) -> float:
        """E[major $/month] = units * lambda * mean / 12 (base-year dollars)."""
        return owned_units * self.major_rate_year * self.major_cost_mean / 12.0

    # ---- realized draws ---------------------------------------------------------

    def draw_monthly_events(self, owned_units: int) -> MaintenanceEventDraw:
        """Draw one month of maintenance events for the whole portfolio.

        Zero-unit months return zeros WITHOUT consuming RNG state (keeps the
        stream aligned across runs whose portfolios grow at different dates).
        """
        if owned_units <= 0:
            return MaintenanceEventDraw(0, 0.0, 0, 0.0)

        # Routine: NB via Gamma-Poisson mixture. Portfolio-month size
        # k = units * r/12; scale theta = lambda/r (so mean = k*theta =
        # units*lambda/12). gammavariate(alpha=k, beta=theta) has mean k*theta.
        k = owned_units * self.routine_dispersion / 12.0
        theta = self.routine_rate_year / self.routine_dispersion
        intensity = self.rng.gammavariate(k, theta)
        routine_count = self._poisson(intensity)
        routine_cost = sum(
            self.rng.lognormvariate(self._routine_mu, self.routine_cost_sigma)
            for _ in range(routine_count)
        )

        # Major: low-rate Poisson, fat-tail lognormal severity.
        major_mean = owned_units * self.major_rate_year / 12.0
        major_count = self._poisson(major_mean)
        major_cost = sum(
            self.rng.lognormvariate(self._major_mu, self.major_cost_sigma)
            for _ in range(major_count)
        )

        return MaintenanceEventDraw(
            routine_count=routine_count,
            routine_cost=round(routine_cost, 2),
            major_count=major_count,
            major_cost=round(major_cost, 2),
        )

    def _poisson(self, mean: float) -> int:
        """Knuth Poisson draw (exact for the means LB reaches). Fail-loud on
        means large enough to underflow exp(-mean) — no silent misdraws."""
        if mean <= 0:
            return 0
        if mean > _MAX_POISSON_MEAN:
            raise ValueError(
                f"Poisson mean {mean:.1f} exceeds the exact-draw bound "
                f"{_MAX_POISSON_MEAN} — portfolio scale outgrew the draw "
                "algorithm; switch to a split/normal method deliberately."
            )
        limit = math.exp(-mean)
        product = self.rng.random()
        count = 0
        while product > limit:
            count += 1
            product *= self.rng.random()
        return count
