"""
Rent Reduction Manager - Tenure-based rent reductions

Liberty Bee rewards long-tenured households with a schedule of rent reductions.
A household that continuously occupies a unit earns cumulative reductions at the
tenure thresholds defined by the projection's RR_* parameters (36/72/120/180
months at 5/5/10/5% for the current canonical projections).

Core model
----------
- `Lease.MonthlyRent` is the immutable original signed rent.
- `Lease.CumulativeRentReductionPct` is the total earned reduction, cumulative
  OFF the original rent (sum of crossed-tier pcts) — never compounded against
  the already-reduced rent.
- `Lease.EffectiveMonthlyRent` is the current billable rent, derived strictly as
  ROUND(MonthlyRent * (1 - CumulativeRentReductionPct), 2). It is operational
  state, never an independent source of truth.

Why per-lease state is sufficient
---------------------------------
Reduction state lives on the Lease row and tenure is measured from
`LeaseStartDate`. A renewal extends `LeaseEndDate` on the SAME LeaseID, so the
clock and the earned reduction carry forward automatically (renewal continuity).
Turnover and unit moves create a NEW LeaseID with a fresh `LeaseStartDate`, so a
new lease naturally starts at a zero clock and zero reduction (turnover/unit-move
reset). No household- or unit-level bookkeeping is required.

Idempotency
-----------
Each month the manager computes the expected cumulative reduction from tenure
and the active tier list, and only writes when the expected pct exceeds the
stored pct. Re-processing the same month is a no-op.

Usage
-----
Call `process_monthly_rent_reductions(current_date)` once per day in the
simulation loop, BEFORE rent collection. The method self-gates to the 1st of the
month, so the reduction is in place before that month's rent is billed.
"""

import json
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from database_manager import DatabaseManager
from event_logger import EventLogger, EventType, EntityType, ActionType
from configuration_loader import RentReductionTier


_CENT = Decimal("0.01")


class RentReductionManager:
    """Apply the tenure-based rent-reduction schedule to active leases."""

    def __init__(self, db_manager: DatabaseManager, event_logger: EventLogger,
                 run_id: int, rent_reduction_tiers: List[RentReductionTier],
                 logger: Optional[logging.Logger] = None):
        self.db = db_manager
        self.event_logger = event_logger
        self.run_id = run_id
        # Tiers arrive already filtered (active only) and sorted ascending by
        # month threshold from the configuration loader. Re-sort defensively so
        # the cumulative walk below is correct regardless of caller.
        self.tiers: List[RentReductionTier] = sorted(
            rent_reduction_tiers, key=lambda t: t.months
        )
        self.logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process_monthly_rent_reductions(self, current_date: date) -> int:
        """Evaluate every active lease and apply newly earned reduction tiers.

        Self-gates to the 1st of the month — calling it on any other day is a
        no-op. Returns the number of leases whose reduction advanced this month.
        """
        if current_date.day != 1:
            return 0
        if not self.tiers:
            # All-NULL / empty schedule — valid no-op configuration.
            return 0

        leases = self._get_active_leases(current_date)
        advanced = 0

        for lease_id, monthly_rent, stored_pct, lease_start, household_id, unit_id in leases:
            monthly_rent = Decimal(str(monthly_rent))
            stored_pct = Decimal(str(stored_pct))
            tenure_months = self._tenure_months(lease_start, current_date)

            transitions = self._tier_transitions(tenure_months, stored_pct)
            if not transitions:
                continue  # idempotent no-op — no newly crossed tier

            expected_pct = transitions[-1].after_pct
            effective_after = self._effective_rent(monthly_rent, expected_pct)

            # Single row update to the final cumulative value.
            self.db.execute_non_query(
                """
                UPDATE simulation.Lease
                SET CumulativeRentReductionPct = ?,
                    EffectiveMonthlyRent = ?
                WHERE RunID = ? AND LeaseID = ?
                """,
                (expected_pct, effective_after, self.run_id, lease_id),
            )

            # One audit event per newly crossed tier, with sequential
            # before/after pcts.
            for tr in transitions:
                self._log_reduction_event(
                    current_date=current_date,
                    household_id=household_id,
                    lease_id=lease_id,
                    unit_id=unit_id,
                    tenure_months=tenure_months,
                    monthly_rent=monthly_rent,
                    transition=tr,
                )

            advanced += 1
            self.logger.debug(
                f"Rent reduction: LeaseID={lease_id} tenure={tenure_months}mo "
                f"{stored_pct} -> {expected_pct} "
                f"(effective rent ${monthly_rent:.2f} -> ${effective_after:.2f})"
            )

        if advanced:
            self.logger.info(
                f"Rent reductions on {current_date}: {advanced} lease(s) advanced a tier."
            )
        return advanced

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_active_leases(self, current_date: date) -> List:
        """Active leases on `current_date`.

        Uses the same active-lease definition as rent collection
        (`rent_collection_manager._get_active_leases`): a lease is active when
        it has started and has not yet ended. The post-termination-arrears
        branch that rent collection also includes is intentionally omitted —
        rent reduction only concerns currently-occupied leases. `LeaseStatus`
        is not part of rent collection's authoritative predicate, so it is not
        used here either. ORDER BY LeaseID for stable iteration.
        """
        query = """
            SELECT l.LeaseID, l.MonthlyRent, l.CumulativeRentReductionPct,
                   l.LeaseStartDate, l.HouseholdID, l.UnitID
            FROM simulation.Lease l
            WHERE l.RunID = ?
              AND l.LeaseStartDate <= ?
              AND l.LeaseEndDate >= ?
            ORDER BY l.LeaseID
        """
        return self.db.execute_query(
            query, (self.run_id, current_date, current_date)
        )

    @staticmethod
    def _tenure_months(lease_start: date, current_date: date) -> int:
        """Months of continuous occupancy in the current unit.

        Matches SQL `DATEDIFF(MONTH, LeaseStartDate, current_date)` — counts
        calendar-month boundaries crossed, ignoring day-of-month. For a lease
        starting 2025-01-01, on 2028-01-01 this is exactly 36.
        """
        return ((current_date.year - lease_start.year) * 12
                + (current_date.month - lease_start.month))

    @staticmethod
    def _effective_rent(monthly_rent: Decimal, cumulative_pct: Decimal) -> Decimal:
        """Derived billable rent: ROUND(MonthlyRent * (1 - pct), 2)."""
        return (monthly_rent * (Decimal(1) - cumulative_pct)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )

    def _tier_transitions(self, tenure_months: int, stored_pct: Decimal):
        """Tiers newly crossed this pass, as sequential before/after transitions.

        Walks the sorted tiers accumulating cumulative pct. A tier is *newly
        applied* when its running cumulative exceeds the already-stored pct.
        Because the manager always writes the full prefix-sum, `stored_pct` is
        always a clean prefix sum — so `cumulative > stored_pct` cleanly
        separates already-applied tiers from newly crossed ones.
        """
        transitions = []
        cumulative = Decimal(0)
        for tier in self.tiers:
            if tenure_months < tier.months:
                break  # sorted ascending — no later tier qualifies either
            before = cumulative
            cumulative = cumulative + tier.pct
            if cumulative > stored_pct:
                transitions.append(_TierTransition(
                    tier=tier,
                    before_pct=before,
                    after_pct=cumulative,
                ))
        return transitions

    def _log_reduction_event(self, current_date, household_id, lease_id,
                             unit_id, tenure_months, monthly_rent, transition):
        """Write one RENT_REDUCTION audit event for a newly applied tier.

        Uses the RENT_REDUCTION ActionType (a dedicated audit lane, mirroring
        the TENANT_CREDIT precedent) on an EventType.SIMULATION event. Metadata
        is a JSON document carrying the reduction-event fields, so reconciliation tests
        can extract values with `JSON_VALUE`.
        """
        if not self.event_logger:
            return
        tier = transition.tier
        eff_before = self._effective_rent(monthly_rent, transition.before_pct)
        eff_after = self._effective_rent(monthly_rent, transition.after_pct)
        metadata = json.dumps({
            "event": "TIER_APPLIED",
            "HouseholdID": household_id,
            "LeaseID": lease_id,
            "UnitID": unit_id,
            "TenureMonths": tenure_months,
            "TierNumber": tier.tier_number,
            "TierMonths": tier.months,
            "TierPct": str(tier.pct),
            "PreviousCumulativeRentReductionPct": str(transition.before_pct),
            "NewCumulativeRentReductionPct": str(transition.after_pct),
            "OriginalMonthlyRent": str(monthly_rent),
            "EffectiveMonthlyRentBefore": str(eff_before),
            "EffectiveMonthlyRentAfter": str(eff_after),
        }, default=str)
        self.event_logger.log_event(
            event_type=EventType.SIMULATION,
            effective_date=current_date,
            entity_type=EntityType.LEASE,
            entity_id=lease_id,
            action_type=ActionType.RENT_REDUCTION,
            amount=eff_after,
            metadata=metadata,
        )


class _TierTransition:
    """One newly applied tier: the tier plus its sequential before/after pcts."""

    __slots__ = ("tier", "before_pct", "after_pct")

    def __init__(self, tier: RentReductionTier,
                 before_pct: Decimal, after_pct: Decimal):
        self.tier = tier
        self.before_pct = before_pct
        self.after_pct = after_pct
