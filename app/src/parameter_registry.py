"""
ParameterRegistry — the single read path for the unified EAV parameter store
(reference.ParameterRegistry + reference.ParameterCategory).

Replaces the scattered reads of reference.ProjectionParameters /
reference.TenantParameters and their COALESCE / .get() fallbacks. The contract:

  - read ONCE per projection (load());
  - resolve override(projection) -> else global(NULL ProjectionID);
  - coerce by the row's DataType;
  - FAIL LOUD on a missing or malformed parameter — there are no code-side
    defaults. The codesweep showed fallbacks mask real wiring bugs, so the
    registry is the only source of truth and an absent required param is an error.

Variable-length groups (e.g. rent-reduction tiers) read via get_category(),
which returns whatever rows are present (absence = that lever is not configured).
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
        self._projection_id = None

    def load(self, projection_id: int) -> "ParameterRegistry":
        """Load + resolve all parameters for one projection (override else global)."""
        rows = self.db.execute_query(
            """
            SELECT Category, Name, ProjectionID, Value, DataType
            FROM reference.ParameterRegistry
            WHERE ProjectionID IS NULL OR ProjectionID = ?
            """,
            (projection_id,),
        )
        # override (ProjectionID set) wins over global (ProjectionID NULL)
        staged = {}  # (cat,name) -> {'override': (val,dt), 'global': (val,dt)}
        for cat, name, pid, val, dt in rows:
            slot = "override" if pid is not None else "global"
            staged.setdefault((cat, name), {})[slot] = (val, dt)

        resolved = {}
        for key, slots in staged.items():
            resolved[key] = slots.get("override") or slots["global"]
        self._resolved = resolved
        self._projection_id = projection_id
        self.logger.info(
            f"ParameterRegistry: loaded {len(resolved)} resolved parameters for projection {projection_id}"
        )
        return self

    def load_globals(self) -> "ParameterRegistry":
        """Load only the global (ProjectionID NULL) parameters — for consumers
        that read no per-projection overrides (e.g. tenant/applicant generation,
        whose params are all global). One read, cached; same fail-loud contract."""
        rows = self.db.execute_query(
            """
            SELECT Category, Name, Value, DataType
            FROM reference.ParameterRegistry
            WHERE ProjectionID IS NULL
            """
        )
        self._resolved = {(c, n): (v, dt) for c, n, v, dt in rows}
        self._projection_id = None
        self.logger.info(f"ParameterRegistry: loaded {len(self._resolved)} global parameters")
        return self

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
