"""
ParameterRegistry — the single read path for the parameter store.

Backed by three tables (V00071):
  reference.Projection                 identity: a projection exists because a row
                                       here says so. Carries Description/Kind/
                                       ScenarioTag so meaning is queryable rather
                                       than parsed out of a name string.
  reference.ParameterRegistryDefault   default values, keyed (Category, Name).
  reference.ParameterRegistryDefined   per-projection overrides, ProjectionID NOT
                                       NULL with an FK to Projection.

Replaces the scattered reads of reference.ProjectionParameters /
reference.TenantParameters and their COALESCE / .get() fallbacks. The contract:

  - read ONCE per projection (load());
  - resolve override(projection) -> else default;
  - coerce by the row's DataType;
  - FAIL LOUD on a missing or malformed parameter — there are no code-side
    defaults. The codesweep showed fallbacks mask real wiring bugs, so the
    registry is the only source of truth and an absent required param is an error.

Variable-length groups (e.g. rent-reduction tiers) read via get_category(),
which returns whatever rows are present (absence = that lever is not configured).

Before V00071 both kinds of row lived in one table separated only by a nullable
ProjectionID, and a projection's existence was inferred from the presence of a
SIM.ProjectionName parameter row — identity conflated with configuration.
"""
import logging
from decimal import Decimal, InvalidOperation
from datetime import date


class ParameterRegistryError(Exception):
    """Raised on a missing or malformed required parameter (fail-loud)."""


class ParameterRegistry:
    """Read-once, validate, fail-loud reader over reference.ParameterRegistry."""

    def __init__(self, db_manager):
        self.db = db_manager
        self.logger = logging.getLogger(__name__)
        self._resolved = {}        # (category, name) -> (value_str, datatype)
        self._town_overrides = {}  # (town, category, name) -> value_str  (V00077, Scope='local')
        self._projection_id = None
        self._projection = None    # the reference.Projection row, or None if absent

    def load(self, projection_id: int) -> "ParameterRegistry":
        """Load the projection and resolve its parameters (override else default).

        Identity and configuration are read separately and mean different things:
        the projection EXISTS if reference.Projection has a row (see
        `projection_exists`), independently of which parameters happen to be
        configured for it. Previously existence was inferred from the presence of
        a SIM.ProjectionName parameter row, which conflated the two — an
        incompletely-seeded projection reported as "not found".
        """
        proj = self.db.execute_query(
            """
            SELECT ProjectionID, Name, Description, Kind, ScenarioTag
            FROM reference.Projection
            WHERE ProjectionID = ?
            """,
            (projection_id,),
        )
        self._projection = ({
            "projection_id": proj[0][0], "name": proj[0][1], "description": proj[0][2],
            "kind": proj[0][3], "scenario_tag": proj[0][4],
        } if proj else None)

        rows = self.db.execute_query(
            """
            SELECT Category, Name, Value, DataType, 0 AS IsOverride
            FROM reference.ParameterRegistryDefault
            UNION ALL
            SELECT Category, Name, Value, DataType, 1 AS IsOverride
            FROM reference.ParameterRegistryDefined
            WHERE ProjectionID = ?
            """,
            (projection_id,),
        )
        # An override for this projection wins over the default.
        staged = {}  # (cat,name) -> {'override': (val,dt), 'default': (val,dt)}
        for cat, name, val, dt, is_override in rows:
            slot = "override" if is_override else "default"
            staged.setdefault((cat, name), {})[slot] = (val, dt)

        resolved = {}
        for key, slots in staged.items():
            resolved[key] = slots.get("override") or slots["default"]
        self._resolved = resolved
        self._projection_id = projection_id
        self._load_town_overrides()
        self.logger.info(
            f"ParameterRegistry: loaded {len(resolved)} resolved parameters for projection {projection_id}"
        )
        return self

    def load_globals(self) -> "ParameterRegistry":
        """Load only the DEFAULT parameters — for consumers that read no
        per-projection overrides (e.g. tenant/applicant generation, whose params
        are all defaults). One read, cached; same fail-loud contract.

        NOTE: this path is projection-blind by design. A per-projection override
        is invisible here, so a parameter intended to vary by projection (a
        scenario knob, say) must be read through load() instead — otherwise the
        override silently does not apply.
        """
        rows = self.db.execute_query(
            """
            SELECT Category, Name, Value, DataType
            FROM reference.ParameterRegistryDefault
            """
        )
        self._resolved = {(c, n): (v, dt) for c, n, v, dt in rows}
        self._projection_id = None
        self._projection = None
        self._load_town_overrides()
        self.logger.info(f"ParameterRegistry: loaded {len(self._resolved)} default parameters")
        return self

    # The params the engine actually resolves per-town today. EXTEND THIS SET
    # in the same commit that wires a new consumer through get_for_town — an
    # override for anything outside it is loaded but never read per-town, and
    # the adopter is warned by name below (lb-ba 2026-08-12 finding #5: a
    # silent no-op knob is dishonest; prose disclosure decays, a mechanism
    # doesn't).
    TOWN_RESOLVED = {
        ('OPEX', 'PropertyTaxPerUnit'),   # OpEx + acquisition gate (KD-229)
        ('OPEX', 'InsurancePerUnit'),     # OpEx + acquisition gate (KD-229)
        ('INC', 'EarnerMedianAnnual'),    # applicant pool median level
        ('PROP', 'VacancyRateBase'),      # fill durations + underwriting
    }

    def _load_town_overrides(self):
        """Load per-town overrides for Scope='local' params (V00077 reference.TownParameterOverride).
        Absent table or no rows -> empty map, so get_for_town() falls back to the region-wide value
        (a single-town region or one with no overrides resolves exactly as today).

        Integrity (lb-ba 2026-08-12 #5/#6): an override for a non-'local' or
        unregistered param RAISES — 'model' mechanics (rent-reduction tiers,
        TCS, eviction thresholds) must never vary by town, and that guarantee
        is a fail-loud mechanism here, not a convention. An override for a
        local param the engine does not yet resolve per-town WARNS by name —
        stored, honest, inert until its consumer is wired."""
        self._town_overrides = {}
        has_table = self.db.execute_query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE LOWER(table_schema) = 'reference' "
            "AND LOWER(table_name) = 'townparameteroverride'"
        )
        if has_table:
            rows = self.db.execute_query(
                "SELECT o.Town, o.Category, o.Name, o.Value, d.Scope "
                "FROM reference.TownParameterOverride o "
                "LEFT JOIN reference.ParameterRegistryDefault d "
                "ON d.Category = o.Category AND d.Name = o.Name"
            )
            bad = [(t, c, n, s) for t, c, n, _v, s in rows if s != 'local']
            if bad:
                detail = "; ".join(
                    f"{t} {c}.{n} ({'unregistered param' if s is None else f'Scope={s}'})"
                    for t, c, n, s in bad)
                raise ValueError(
                    f"TownParameterOverride contains non-town-scopable rows — "
                    f"model mechanics never vary by town: {detail}")
            for t, c, n, _v, _s in rows:
                if (c, n) not in self.TOWN_RESOLVED:
                    self.logger.warning(
                        f"TownParameterOverride {t} {c}.{n} is not yet town-resolved "
                        f"by the engine — the region-wide value will be used until "
                        f"its consumer is wired (stored, not lost)")
            self._town_overrides = {(t, c, n): v for t, c, n, v, _s in rows}

    # --- projection identity -------------------------------------------------
    # Identity comes from the entity, never from a parameter value. A projection
    # that exists but is missing parameters is a DIFFERENT failure from one that
    # does not exist, and the caller can now tell them apart.
    @property
    def projection_exists(self) -> bool:
        return self._projection is not None

    @property
    def projection_name(self):
        return self._projection["name"] if self._projection else None

    @property
    def projection_kind(self):
        return self._projection["kind"] if self._projection else None

    @property
    def projection_scenario_tag(self):
        return self._projection["scenario_tag"] if self._projection else None

    @property
    def projection_description(self):
        return self._projection["description"] if self._projection else None

    # --- scalar access -------------------------------------------------------
    def get(self, category: str, name: str):
        key = (category, name)
        if key not in self._resolved:
            raise ParameterRegistryError(
                f"required parameter {category}.{name} not found for projection "
                f"{self._projection_id} (no per-projection override and no global default). "
                f"Fail-loud: the registry has no fallback."
            )
        val, dt = self._resolved[key]
        return self._coerce(val, dt, category, name)

    def get_decimal(self, category, name) -> Decimal:
        return Decimal(str(self.get(category, name)))

    def get_float(self, category, name) -> float:
        return float(self.get(category, name))

    def get_int(self, category, name) -> int:
        return int(self.get(category, name))

    def get_str(self, category, name) -> str:
        return str(self.get(category, name))

    def get_date(self, category, name) -> date:
        v = self.get(category, name)
        return v if isinstance(v, date) else date.fromisoformat(str(v))

    # --- town-scoped access (Scope='local' params; V00077) -------------------
    # The ONLY town-aware read. Model-mechanics params keep get(); only local-market-context
    # params are consulted per-town, and only where a consumer opts in by passing a town.
    def get_for_town(self, category, name, town):
        """A local param resolved for a specific TOWN: the town override if one exists, else the
        region-wide value. `town` None/blank -> region-wide. Coerced by the base param's DataType,
        so a single-town region (or no override for that town) resolves EXACTLY as get()."""
        if town:
            key = (town, category, name)
            if key in self._town_overrides:
                base = self._resolved.get((category, name))
                dt = base[1] if base else "decimal"
                return self._coerce(self._town_overrides[key], dt, category, name)
        return self.get(category, name)

    def get_decimal_for_town(self, category, name, town) -> Decimal:
        return Decimal(str(self.get_for_town(category, name, town)))

    def get_float_for_town(self, category, name, town) -> float:
        return float(self.get_for_town(category, name, town))

    def get_category(self, category: str) -> dict:
        """All resolved (name -> coerced value) for a category. Used for
        variable-length groups (e.g. RR tiers) where absence is meaningful."""
        return {
            name: self._coerce(val, dt, cat, name)
            for (cat, name), (val, dt) in self._resolved.items()
            if cat == category
        }

    def has(self, category: str, name: str) -> bool:
        return (category, name) in self._resolved

    # --- coercion ------------------------------------------------------------
    def _coerce(self, val, dt, category, name):
        if val is None:
            raise ParameterRegistryError(
                f"{category}.{name} has a NULL value (DataType={dt}); the registry must "
                f"carry real values (V00039 asserts none are NULL)."
            )
        try:
            if dt == "decimal":
                return Decimal(str(val))
            if dt == "int":
                return int(str(val))
            if dt == "bit":
                return str(val).strip() not in ("0", "False", "false", "")
            if dt == "date":
                return date.fromisoformat(str(val)[:10])
            return str(val)  # varchar
        except (InvalidOperation, ValueError, TypeError) as e:
            raise ParameterRegistryError(
                f"cannot coerce {category}.{name}={val!r} as {dt}: {e}"
            )
