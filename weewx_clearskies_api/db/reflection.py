"""Schema introspection and ColumnRegistry (ADR-012, ADR-035).

At startup (after the write-probe passes), MetaData.reflect() runs against
the archive table.  The resulting column list feeds ColumnRegistry with two
parts:

  a) Auto-mapped stock columns — canonical names from docs/contracts/
     canonical-data-model.md §3.1.  For the weewx-stock set, the weewx
     archive column name IS the canonical name (ADR-010 §Decision: "weewx-
     aligned camelCase everywhere").  A few columns have a trivial casing
     difference that is documented in the mapping table below.

  b) Unmapped non-stock columns — discovered columns that are not in the
     stock table.  Auto-mapped using the DB column name as the canonical
     name (identity mapping) so every archive column is immediately
     queryable via /archive.  Task 3's operator-mapping UI can later
     assign a different canonical name.

Re-introspection (operator-triggered via the config UI in task 3) calls
refresh().  At v0.1 task 2, refresh() simply re-runs reflect().

Design constraints (ADR-035):
  - Stock columns auto-map silently. Operator can override later (task 3 /
    Phase 4 — not in this task).
  - Non-stock columns are surfaced as unmapped; the operator is the final
    authority.
  - Heuristic name-match suggestions for non-stock columns are NOT built here
    (that is task 3 / Phase 4 work per the task brief).
  - "Simple means simple" — no operator-mapping storage, no UI, no suggestions
    in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine, MetaData, Table
from sqlalchemy.exc import InvalidRequestError, OperationalError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stock-column lookup table
#
# Source: docs/contracts/canonical-data-model.md §3.1 (Observation entity)
#         + §3.2 (ArchiveRecord adds `interval`).
#
# Key:   weewx archive column name (verbatim, case-sensitive as SQLite/MariaDB
#        store it).
# Value: canonical API field name (camelCase per ADR-010; identical to weewx
#        column name in the overwhelming majority of cases).
#
# Notes:
#   - Most entries are identity mappings (weewx column name == canonical name).
#   - `dateTime` (epoch int) maps to `timestamp` because the canonical entity
#     uses `timestamp` as the JSON key and converts from epoch to ISO-8601 UTC
#     at ingest.
#   - `usUnits` and `interval` are meta-columns, not observation fields, but
#     they appear in the archive table and are listed here so the registry
#     knows they're stock (not operator-custom columns).
# ---------------------------------------------------------------------------

STOCK_COLUMN_MAP: dict[str, str] = {
    # Meta / administrative columns
    "dateTime": "timestamp",      # epoch s → ISO-8601 UTC at ingest
    "usUnits": "usUnits",         # weewx unit system identifier (internal)
    "interval": "interval",       # archive interval, minutes (ArchiveRecord)

    # Observation core fields (§3.1 verbatim)
    "outTemp": "outTemp",
    "outHumidity": "outHumidity",
    "windSpeed": "windSpeed",
    "windDir": "windDir",
    "windGust": "windGust",
    "windGustDir": "windGustDir",
    "barometer": "barometer",
    "pressure": "pressure",
    "altimeter": "altimeter",
    "dewpoint": "dewpoint",
    "windchill": "windchill",
    "heatindex": "heatindex",
    "rainRate": "rainRate",
    "rain": "rain",
    "radiation": "radiation",
    "UV": "UV",
    "inTemp": "inTemp",
    "inHumidity": "inHumidity",

    # wview_extended columns listed in §3.1 as first-class
    "ET": "ET",
    "hail": "hail",
    "hailRate": "hailRate",

    # wview_extended promotion candidates (§3.1 note — currently route through
    # extras but are stock weewx columns, not operator-custom).  Listed here so
    # they are classified as "stock-but-not-yet-promoted" rather than
    # "operator-custom" — task 3's UI treats these differently.
    "appTemp": "appTemp",
    "cloudbase": "cloudbase",
    "lightning_strike_count": "lightning_strike_count",
    "lightning_distance": "lightning_distance",

    # Additional wview_extended columns (sensor expansion slots)
    "extraTemp1": "extraTemp1",
    "extraTemp2": "extraTemp2",
    "extraTemp3": "extraTemp3",
    "extraTemp4": "extraTemp4",
    "extraTemp5": "extraTemp5",
    "extraTemp6": "extraTemp6",
    "extraTemp7": "extraTemp7",
    "extraTemp8": "extraTemp8",
    "extraHumid1": "extraHumid1",
    "extraHumid2": "extraHumid2",
    "extraHumid3": "extraHumid3",
    "extraHumid4": "extraHumid4",
    "extraHumid5": "extraHumid5",
    "extraHumid6": "extraHumid6",
    "extraHumid7": "extraHumid7",
    "extraHumid8": "extraHumid8",
    "dewpoint1": "dewpoint1",
    "soilTemp1": "soilTemp1",
    "soilTemp2": "soilTemp2",
    "soilTemp3": "soilTemp3",
    "soilTemp4": "soilTemp4",
    "soilMoist1": "soilMoist1",
    "soilMoist2": "soilMoist2",
    "soilMoist3": "soilMoist3",
    "soilMoist4": "soilMoist4",
    "leafTemp1": "leafTemp1",
    "leafTemp2": "leafTemp2",
    "leafWet1": "leafWet1",
    "leafWet2": "leafWet2",

    # Electrical / system columns
    "consBatteryVoltage": "consBatteryVoltage",
    "heatingVoltage": "heatingVoltage",
    "referenceVoltage": "referenceVoltage",
    "supplyVoltage": "supplyVoltage",
    "rxCheckPercent": "rxCheckPercent",

    # Degree-day columns
    "heatdeg": "heatdeg",
    "cooldeg": "cooldeg",

    # Precipitation / lightning extra
    "snow": "snow",
    "snowDepth": "snowDepth",
    "snowRate": "snowRate",
    "lightning_noise_count": "lightning_noise_count",
    "lightning_disturber_count": "lightning_disturber_count",
    "noise": "noise",

    # Other wview_extended observation fields
    "THSW": "THSW",
    "humidex": "humidex",
    "pop": "pop",
    "cloudcover": "cloudcover",
    "maxSolarRad": "maxSolarRad",
    "sunshineDur": "sunshineDur",
    "daySunshineDur": "daySunshineDur",
    "rainDur": "rainDur",
    "windrun": "windrun",
    "vecdir": "vecdir",
    "gustdir": "gustdir",
    "vecavg": "vecavg",
    "rms": "rms",
    "illuminance": "illuminance",
}


@dataclass
class ColumnInfo:
    """Metadata for one column in the archive table."""

    #: Verbatim column name as it appears in the DB schema.
    db_name: str
    #: Canonical API field name.  For stock columns this comes from
    #: STOCK_COLUMN_MAP; for unmapped (extension) columns the DB column name
    #: is used as-is (identity mapping) so every archive column is queryable.
    canonical_name: str
    #: True for columns in STOCK_COLUMN_MAP; False for operator-custom columns.
    is_stock: bool


@dataclass
class ColumnRegistry:
    """Registry of archive table columns, split into stock and unmapped sets.

    Populated at startup by reflecting the archive table.  Task 3 will add:
      - Operator-supplied mappings for non-stock columns.
      - Persistence of those mappings to api.conf.

    At v0.1 (task 2), non-stock columns are surfaced as unmapped only.
    """

    #: Stock columns: db_name → ColumnInfo (canonical_name set, is_stock=True).
    stock: dict[str, ColumnInfo] = field(default_factory=dict)
    #: Non-stock / unmapped columns: db_name → ColumnInfo (is_stock=False).
    #: canonical_name is set to db_name (identity mapping) so every archive
    #: column is immediately queryable via /archive without operator action.
    unmapped: dict[str, ColumnInfo] = field(default_factory=dict)

    def all_columns(self) -> list[ColumnInfo]:
        """Return all columns (stock + unmapped) as a flat list."""
        return list(self.stock.values()) + list(self.unmapped.values())

    def get_canonical(self, db_name: str) -> str | None:
        """Return the canonical name for a DB column, or None if not reflected."""
        if db_name in self.stock:
            return self.stock[db_name].canonical_name
        if db_name in self.unmapped:
            return self.unmapped[db_name].canonical_name
        return None


def _build_registry(
    columns: list[str],
    operator_mapping: dict[str, str] | None = None,
) -> ColumnRegistry:
    """Build a ColumnRegistry from a list of DB column names.

    Classifies each column as stock (in STOCK_COLUMN_MAP) or unmapped.
    Operator mappings from [column_mapping] in api.conf override the
    identity mapping for non-stock columns.
    """
    op_map = operator_mapping or {}
    registry = ColumnRegistry()
    for col in columns:
        canonical = STOCK_COLUMN_MAP.get(col)
        if canonical is not None:
            registry.stock[col] = ColumnInfo(
                db_name=col,
                canonical_name=canonical,
                is_stock=True,
            )
        elif col in op_map:
            registry.unmapped[col] = ColumnInfo(
                db_name=col,
                canonical_name=op_map[col],
                is_stock=False,
            )
            logger.info(
                "Non-stock column mapped by operator: %s → %s",
                col, op_map[col],
            )
        else:
            registry.unmapped[col] = ColumnInfo(
                db_name=col,
                canonical_name=col,
                is_stock=False,
            )
            logger.info(
                "Non-stock archive column found — auto-mapped to its DB column name.",
                extra={"column": col},
            )
    return registry


class SchemaReflector:
    """Reflects the archive table schema and maintains the ColumnRegistry.

    Constructed once at startup; refresh() re-runs on operator request
    (e.g., after adding a weewx extension that adds new columns).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._registry: ColumnRegistry = ColumnRegistry()
        self._reflected = False

    @property
    def registry(self) -> ColumnRegistry:
        """The current ColumnRegistry.  Populated after reflect() is called."""
        return self._registry

    def reflect(
        self, operator_mapping: dict[str, str] | None = None,
    ) -> ColumnRegistry:
        """Run MetaData.reflect() against the archive table and build the registry.

        Args:
            operator_mapping: db_name → canonical_name from [column_mapping] in api.conf.

        Returns:
            The populated ColumnRegistry.

        Raises:
            RuntimeError: The archive table is not found in the database.
            sqlalchemy.exc.OperationalError: DB connection failed.
        """
        meta = MetaData()
        try:
            meta.reflect(bind=self._engine, only=["archive"])
        except OperationalError as exc:
            raise RuntimeError(
                f"Schema reflection failed — cannot read the archive table: {exc}"
            ) from exc
        except InvalidRequestError as exc:
            # SQLAlchemy raises InvalidRequestError when the table named in `only`
            # does not exist in the database (e.g. fresh install before weewx runs).
            raise RuntimeError(
                f"Schema reflection: 'archive' table not found in the database. "
                "Verify [database] name and connection settings in api.conf. "
                "The weewx archive table must exist before clearskies-api starts. "
                f"(SQLAlchemy: {exc})"
            ) from exc

        if "archive" not in meta.tables:
            raise RuntimeError(
                "Schema reflection: 'archive' table not found in the database. "
                "Verify [database] name and connection settings in api.conf. "
                "The weewx archive table must exist before clearskies-api starts."
            )

        archive_table: Table = meta.tables["archive"]
        column_names = [col.name for col in archive_table.columns]

        logger.info(
            "Archive table reflected",
            extra={"column_count": len(column_names), "columns": column_names},
        )

        self._registry = _build_registry(column_names, operator_mapping)
        self._reflected = True

        stock_count = len(self._registry.stock)
        unmapped_count = len(self._registry.unmapped)
        logger.info(
            "Column registry built",
            extra={
                "stock_columns": stock_count,
                "unmapped_columns": unmapped_count,
            },
        )
        return self._registry

    def refresh(self) -> ColumnRegistry:
        """Re-run schema reflection (operator-triggered; task 3 wires the trigger).

        Clears the existing registry and rebuilds it from the current schema.
        This picks up new columns added by a weewx extension since startup.
        """
        logger.info("Schema reflection refresh requested.")
        return self.reflect()
