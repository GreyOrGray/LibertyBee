"""
Retention model — voluntary lease-exit probability as a
function of the tenant's realized deal and the regional difficulty of leaving.

Replaces the flat, circumstance-blind renewal coin-flip. Evidence:
docs/reference/evidence_base.md §10.

    voluntary_exit = clamp(
        base_exit
          * (1 - beta  * effective_discount)     # the deal they'd lose
          * (1 - gamma * external_scarcity),      # nowhere else to go (regional)
        floor_exit, base_exit)

  effective_discount = 1 - effective_rent / market_rent            (vs the current unit's market)
      market_rent = market_anchor_rent * rent_factor(t) — the anchor is the unit's
      AdjustedRent (the bath-adjusted market figure charging prices off, KD-012).
      Charging and the deal comparator share ONE basis; do not repoint the anchor
      at the BaseRent column (that re-splits the basis this fix killed).
  external_scarcity  = 1 - availability * (1 - affordability_gap)  (regional)
      availability      = clamp(regional_vacancy / vac_ref, 0, 1)
      affordability_gap = clamp((market_rent/income - burden_floor)/(burden_ceiling - burden_floor), 0, 1)
      regional_vacancy  = VacancyRateBase + regime VacancyDelta(t)   (evidence §4a + §7)
      income(t)         = SigningMonthlyIncome * wage_factor(t)/wage_factor(onboarding)  (§9)

base_exit = 1 - RenewalRatePct/100 (RenewalRatePct reinterpreted).

This module owns NO RNG — the caller (LeaseRenewalManager) draws the single
renewal_rng.random() and compares it to the returned probability, so the RNG stream
and byte-reproducibility (invariant) are unchanged: one draw per renew/exit
decision, exactly as before.

1.0 scope (static bedroom-fit): the affordability comparator is the tenant's CURRENT
unit's market rent — the unit they already fit — so no separate fit-unit query is
needed. Dynamic transitions (downsizing / roommate-split into a different unit type)
are deferred to V0.4. Band-specific wage multipliers (INC.WageBandMult_*) are not
applied in the income reconstruction below — exact while they are all 1.0 (the shipped
default); revisit if a market differentiates them.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class _DateContext:
    """Per-current_date factors, computed ONCE per pre-roll (lease-independent)."""
    __slots__ = ("rent_factor_now", "wage_factor_now", "_wage_months",
                 "_wage_prefix", "regional_vacancy")

    def __init__(self, rent_factor_now: float, wage_factor_now: float,
                 wage_months: List[date], wage_prefix: List[float],
                 regional_vacancy: float):
        self.rent_factor_now = rent_factor_now      # Π(1+RentRate) up to current_date
        self.wage_factor_now = wage_factor_now       # Π(1+WageGrowthRate) up to current_date
        self._wage_months = wage_months              # sorted list[date]
        self._wage_prefix = wage_prefix              # parallel cumulative wage factor
        self.regional_vacancy = regional_vacancy     # VacancyRateBase + VacancyDelta(now)

    def wage_factor_as_of(self, d: date) -> float:
        """Cumulative wage factor for months <= d (1.0 if d precedes the schedule)."""
        months = self._wage_months
        lo, hi = 0, len(months)
        while lo < hi:
            mid = (lo + hi) // 2
            if months[mid] <= d:
                lo = mid + 1
            else:
                hi = mid
        return self._wage_prefix[lo - 1] if lo > 0 else 1.0


class RetentionModel:
    """Computes voluntary-exit probability. Owns no RNG; caller draws + compares."""

    def __init__(self, db, *, base_exit: float, beta: float, gamma: float,
                 floor_exit: float, vac_ref: float, burden_ceiling: float,
                 burden_floor: float, regional_vacancy_rate: float, form_is_logistic: int = 0,
                 logger: Optional[logging.Logger] = None):
        self.db = db
        self.base_exit = float(base_exit)
        self.beta = float(beta)
        self.gamma = float(gamma)
        self.floor_exit = float(floor_exit)
        self.vac_ref = float(vac_ref)
        self.burden_ceiling = float(burden_ceiling)
        self.burden_floor = float(burden_floor)
        # The vacancy a LEAVING tenant faces = the wider REGIONAL market they'd search
        # (North Shore/metro), NOT LB's Salem-local occupancy (PROP.VacancyRateBase).
        # A mover is not confined to one tight town (Gray 2026-07-08).
        self.regional_vacancy_rate = float(regional_vacancy_rate)
        self.form_is_logistic = int(form_is_logistic)
        self.logger = logger or logging.getLogger(__name__)
        if self.vac_ref <= 0.0 or self.burden_ceiling <= self.burden_floor:
            raise ValueError(
                f"RetentionModel: vac_ref must be > 0 and burden_ceiling > burden_floor "
                f"(got vac_ref={self.vac_ref}, burden_ceiling={self.burden_ceiling}, "
                f"burden_floor={self.burden_floor})")

    # ------------------------------------------------------------------
    def build_date_context(self, run_id: int, current_date: date) -> _DateContext:
        """Cumulative rent/wage factors + regional vacancy as of current_date.
        Queried once per pre-roll batch (all inputs are date-only, lease-independent)."""
        rows = self.db.execute_query(
            """
            SELECT CAST(InflationDate AS DATE), RentRate, WageGrowthRate, VacancyDelta
            FROM simulation.InflationSchedule
            WHERE RunID = ? AND InflationDate <= ?
            ORDER BY InflationDate
            """,
            (run_id, current_date),
        )
        rent_factor = 1.0
        wage_factor = 1.0
        wage_months: List[date] = []
        wage_prefix: List[float] = []
        vacancy_delta_now = 0.0
        for inflation_date, rent_rate, wage_rate, vac_delta in rows:
            rent_factor *= (1.0 + float(rent_rate))
            wage_factor *= (1.0 + float(wage_rate))
            wage_months.append(inflation_date)
            wage_prefix.append(wage_factor)
            vacancy_delta_now = float(vac_delta)  # last row <= current_date = current month
        regional_vacancy = self.regional_vacancy_rate + vacancy_delta_now
        return _DateContext(rent_factor, wage_factor, wage_months, wage_prefix,
                            regional_vacancy)

    # ------------------------------------------------------------------
    def breakdown(self, ctx: _DateContext, *, effective_rent, market_anchor_rent,
                  signing_income, income_reference_date: date) -> dict:
        """Full component breakdown for one lease at renewal (diagnostics + the live
        decision). `voluntary_exit_prob` returns just the ['voluntary_exit'] field, so
        this is the single source of truth for the formula.

        market_anchor_rent: the unit's AdjustedRent — the same bath-adjusted
        market basis charging uses (KD-012)."""
        base_exit = self.base_exit

        # --- effective_discount: 1 - effective_rent / market_rent (the current unit) ---
        market_rent = float(market_anchor_rent) * ctx.rent_factor_now
        if market_rent <= 0.0:
            effective_discount = 0.0
        else:
            effective_discount = 1.0 - (float(effective_rent) / market_rent)
        # Not pre-clamped: a downturn can push effective_discount < 0 (a sitting tenant
        # above a crashed market, invariant) — the final clamp caps it.

        # --- affordability_gap: current market rent burden vs income at t ---
        if signing_income is None or float(signing_income) <= 0.0:
            affordability_gap = 1.0  # unknown/zero income -> treat as maximally locked in
            income_t = 0.0
        else:
            wage_growth = ctx.wage_factor_now / ctx.wage_factor_as_of(income_reference_date)
            income_t = float(signing_income) * wage_growth
            if income_t <= 0.0:
                affordability_gap = 1.0
            else:
                burden = market_rent / income_t
                affordability_gap = _clamp(
                    (burden - self.burden_floor) / (self.burden_ceiling - self.burden_floor),
                    0.0, 1.0)

        # --- availability + external_scarcity (regional) ---
        availability = _clamp(ctx.regional_vacancy / self.vac_ref, 0.0, 1.0)
        external_scarcity = 1.0 - availability * (1.0 - affordability_gap)

        # --- combine (linear multiplicative; logistic form is a research variant) ---
        if self.form_is_logistic:
            raise NotImplementedError(
                "RET.FormIsLogistic=1 (logistic form) is a research variant; "
                "production ships linear (FormIsLogistic=0).")
        raw = (base_exit
               * (1.0 - self.beta * effective_discount)
               * (1.0 - self.gamma * external_scarcity))
        voluntary_exit = _clamp(raw, self.floor_exit, base_exit)
        return {
            "market_rent": market_rent, "income_t": income_t,
            "effective_discount": effective_discount, "affordability_gap": affordability_gap,
            "availability": availability, "external_scarcity": external_scarcity,
            "voluntary_exit": voluntary_exit,
        }

    def voluntary_exit_prob(self, ctx: _DateContext, *, effective_rent, market_anchor_rent,
                            signing_income, income_reference_date: date) -> float:
        """Annual voluntary-exit probability for one lease at renewal. In [floor_exit, base_exit]."""
        return self.breakdown(
            ctx, effective_rent=effective_rent, market_anchor_rent=market_anchor_rent,
            signing_income=signing_income, income_reference_date=income_reference_date
        )["voluntary_exit"]
