"""Multi-source AQI pollutant merge service (T4B.3, FIX-004).

Merges pollutant data from a provider AQI response with weewx-mapped DB columns.

Use case: operators running IQAir (which returns headline AQI only, no per-
pollutant concentrations) can supplement with their own sensor columns
(e.g., pm2_5, pm10 from a PurpleAir weewx extension) by mapping those columns
to AQI canonical names via the wizard column-mapping UI.

Merge rules:
  - Provider wins: if both the provider and the DB supply the same pollutant
    field, the provider value is kept and source is recorded as the provider name.
  - DB fills gaps: if the provider did not supply a pollutant (null), the most
    recent DB value is used and source is recorded as "weewx".
  - The merge is a no-op when no AQI-related columns are mapped in the column
    registry (no DB query executed, pollutantSources stays None).
  - DB errors are isolated: if the archive query fails, the error is logged and
    the original provider-only AQIReading is returned unchanged.

SQL note: all column identifiers come from the ColumnRegistry (trusted constants
  built from schema reflection, not from user input).  All value bindings use
  SQLAlchemy named parameters.

ruff: noqa: N815  (canonical field names use camelCase per ADR-010)
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from weewx_clearskies_api.db.reflection import ColumnRegistry
from weewx_clearskies_api.models.responses import AQIReading

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical AQI pollutant field names — the merge targets.
# These are the only AQIReading fields that can be sourced from the DB.
# ---------------------------------------------------------------------------

AQI_POLLUTANT_FIELDS: frozenset[str] = frozenset({
    "pollutantPM25",
    "pollutantPM10",
    "pollutantO3",
    "pollutantNO2",
    "pollutantSO2",
    "pollutantCO",
    "pollutantNO",
    "pollutantNH3",
})


# ---------------------------------------------------------------------------
# Column registry lookup: canonical → DB column name
# ---------------------------------------------------------------------------


def _find_aqi_mapped_columns(
    registry: ColumnRegistry,
) -> dict[str, str]:
    """Return {canonical_field: db_column_name} for any AQI pollutant columns in the registry.

    Searches both stock and unmapped entries.  In practice, AQI pollutant fields
    (pollutantPM25, pollutantNO2, etc.) will never appear in STOCK_COLUMN_MAP —
    they are operator-mapped extension columns.  Searching stock as well is
    defensive, in case a future stock extension adds these.

    Returns an empty dict when no AQI-related columns are mapped.
    """
    result: dict[str, str] = {}

    for col_info in registry.all_columns():
        if col_info.canonical_name in AQI_POLLUTANT_FIELDS:
            result[col_info.canonical_name] = col_info.db_name

    return result


# ---------------------------------------------------------------------------
# DB query: most recent archive row, only the mapped pollutant columns
# ---------------------------------------------------------------------------


def _fetch_latest_pollutants(
    db_engine: object,
    canonical_to_db: dict[str, str],
) -> dict[str, float | None]:
    """Fetch the most recent value for each mapped pollutant DB column.

    Issues a single SELECT for the most recent archive row that includes the
    mapped columns.  Column identifiers come from the ColumnRegistry (trusted
    schema constants, not user input).  Values are bound as named parameters.

    Args:
        db_engine: SQLAlchemy Engine (from get_engine()).
        canonical_to_db: {canonical_field: db_column_name} from _find_aqi_mapped_columns.

    Returns:
        {canonical_field: value_or_None} for each mapped column.
        Returns {} on any DB error (caller logs and continues with provider-only data).
    """
    # Build SELECT list from trusted column names (schema reflection output).
    # Each column aliased to its canonical field name for easy row mapping.
    select_parts = ", ".join(
        f"{db_col} AS {canonical}"
        for canonical, db_col in canonical_to_db.items()
    )

    sql = text(
        f"SELECT {select_parts} "
        f"FROM archive "
        f"ORDER BY dateTime DESC "
        f"LIMIT 1"
    )

    try:
        from sqlalchemy.orm import Session  # noqa: PLC0415
        with Session(db_engine) as session:  # type: ignore[arg-type]
            row = session.execute(sql).fetchone()
    except (OperationalError, ProgrammingError) as exc:
        logger.error(
            "[aqi_merge] DB query for pollutant columns failed; using provider-only data. "
            "Error: %s",
            exc,
        )
        return {}

    if row is None:
        return {}

    row_dict = dict(row._mapping)  # noqa: SLF001
    return {
        canonical: row_dict.get(canonical)
        for canonical in canonical_to_db
    }


# ---------------------------------------------------------------------------
# Primary merge function
# ---------------------------------------------------------------------------


def merge_aqi_with_db(
    reading: AQIReading,
    provider_id: str,
    registry: ColumnRegistry,
    db_engine: object,
) -> AQIReading:
    """Merge a provider AQIReading with weewx-mapped pollutant DB columns.

    Provider wins on conflict (same pollutant available from both sources).
    DB fills null gaps (pollutant not supplied by provider → use archive value).
    Returns the original reading unchanged when no AQI columns are mapped.
    Returns the original reading unchanged if the DB query fails (logged error).

    Args:
        reading:     AQIReading from the provider dispatch.
        provider_id: Provider name string (e.g. "iqair", "openmeteo").
                     Used as the source label in pollutantSources.
        registry:    ColumnRegistry from db.registry.get_registry().
        db_engine:   SQLAlchemy Engine from db.session.get_engine().

    Returns:
        New AQIReading (Pydantic model rebuilt) with pollutantSources set, or
        the original reading if no merge was possible.
    """
    # --- Step 1: find any AQI pollutant columns in the column registry ---
    canonical_to_db = _find_aqi_mapped_columns(registry)

    if not canonical_to_db:
        # No AQI-related columns mapped — skip entirely, no DB hit.
        logger.debug(
            "[aqi_merge] No AQI pollutant columns in column registry; skipping merge."
        )
        return reading

    # --- Step 2: fetch the most recent archive values ---
    db_values = _fetch_latest_pollutants(db_engine, canonical_to_db)

    if not db_values:
        # DB query failed or returned no rows; log already emitted by _fetch_latest_pollutants.
        return reading

    # --- Step 3: build merged field dict and pollutantSources tracker ---
    reading_dict = reading.model_dump()
    sources: dict[str, str] = {}

    # Record source for the aqi field itself (always provider-owned).
    if reading_dict.get("aqi") is not None:
        sources["aqi"] = provider_id

    for canonical, db_value in db_values.items():
        provider_value = reading_dict.get(canonical)

        if provider_value is not None:
            # Provider has a value — it wins.  Record it as provider-sourced.
            sources[canonical] = provider_id
        elif db_value is not None:
            # Provider didn't supply this pollutant; DB fills the gap.
            try:
                reading_dict[canonical] = float(db_value)
            except (TypeError, ValueError):
                logger.warning(
                    "[aqi_merge] DB value for %r could not be cast to float: %r; skipping.",
                    canonical,
                    db_value,
                )
                continue
            sources[canonical] = "weewx"

    # Only attach pollutantSources when at least one field was tracked.
    reading_dict["pollutantSources"] = sources if sources else None

    return AQIReading(**reading_dict)
