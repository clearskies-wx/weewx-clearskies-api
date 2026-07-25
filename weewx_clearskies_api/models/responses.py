"""Response envelope and data models for DB-backed endpoints.

Per ADR-010: camelCase field names everywhere (identical in Python and JSON).
Per ADR-075: datetime fields are UTC ISO-8601 with Z suffix. stationClock on all responses.

Pydantic v2 models with ConfigDict(extra="forbid") on all request/param models.
Response models use extra="ignore" so the serialisation layer doesn't reject
extra DB columns (they route to `extras`).

ruff: noqa: N815  (canonical fields use weewx camelCase: outTemp, windSpeed, etc.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def utc_isoformat(dt: datetime) -> str:
    """Serialise a UTC datetime to ISO-8601 with Z suffix (ADR-075)."""
    # Pydantic serialises datetime to "+00:00" by default; we want "Z".
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Observation / ArchiveRecord
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """Canonical observation (ADR-010 §3.1 + OpenAPI Observation schema).

    Full stock weewx column set per the user directive 2026-05-06: every column
    in STOCK_COLUMN_MAP is first-class here.  Operator-custom columns route
    through `extras`; stock weewx columns NEVER appear in `extras`.

    All numeric fields Optional — weather data is genuinely missing sometimes.
    `extras` is always present (may be empty).

    ruff: noqa: N815  (weewx camelCase names per ADR-010)
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    timestamp: str  # UTC ISO-8601 with Z

    # Core wview observation fields
    outTemp: float | None = None
    outHumidity: float | None = None
    windSpeed: float | None = None
    windDir: float | None = None
    windGust: float | None = None
    windGustDir: float | None = None
    barometer: float | None = None
    pressure: float | None = None
    altimeter: float | None = None
    dewpoint: float | None = None
    windchill: float | None = None
    heatindex: float | None = None
    rainRate: float | None = None
    rain: float | None = None
    radiation: float | None = None
    UV: float | None = None
    inTemp: float | None = None
    inHumidity: float | None = None

    # wview_extended core fields
    ET: float | None = None
    hail: float | None = None
    hailRate: float | None = None
    appTemp: float | None = None
    cloudbase: float | None = None
    cloudcover: float | None = None
    windrun: float | None = None
    maxSolarRad: float | None = None
    sunshineDur: float | None = None
    daySunshineDur: float | None = None
    rainDur: float | None = None
    THSW: float | None = None
    humidex: float | None = None
    pop: float | None = None
    illuminance: float | None = None
    noise: float | None = None

    # Lightning fields (wview_extended)
    lightning_strike_count: float | None = None
    lightning_distance: float | None = None
    lightning_noise_count: float | None = None
    lightning_disturber_count: float | None = None

    # Snow fields (wview_extended)
    snow: float | None = None
    snowDepth: float | None = None
    snowRate: float | None = None

    # Wind summary fields
    vecdir: float | None = None
    gustdir: float | None = None
    vecavg: float | None = None
    rms: float | None = None

    # Degree-days
    heatdeg: float | None = None
    cooldeg: float | None = None

    # Sensor expansion slots (wview_extended)
    extraTemp1: float | None = None
    extraTemp2: float | None = None
    extraTemp3: float | None = None
    extraHumid1: float | None = None
    extraHumid2: float | None = None
    soilTemp1: float | None = None
    soilTemp2: float | None = None
    soilTemp3: float | None = None
    soilTemp4: float | None = None
    soilMoist1: float | None = None
    soilMoist2: float | None = None
    soilMoist3: float | None = None
    soilMoist4: float | None = None
    leafTemp1: float | None = None
    leafTemp2: float | None = None
    leafWet1: float | None = None
    leafWet2: float | None = None

    # Electrical / system telemetry
    consBatteryVoltage: float | None = None
    heatingVoltage: float | None = None
    referenceVoltage: float | None = None
    supplyVoltage: float | None = None
    rxCheckPercent: float | None = None

    # Current conditions text from the blending engine (ADR-0B Phase 0B).
    # None when conditions engine is "off" or no provider is configured.
    weatherText: str | None = None

    # Provider-sourced precipitation type: "rain" | "snow" | "freezing-rain" | "sleet" | "hail" | null
    precipType: str | None = None

    # Operator-custom columns (stock weewx columns NEVER appear here)
    extras: dict[str, Any] = {}
    source: str = "weewx"


class ArchiveRecord(Observation):
    """ArchiveRecord = Observation + interval (ADR-010 §3.2)."""

    interval: int


class ProviderConditions(BaseModel):
    """Current conditions from a forecast provider — internal DTO for blending engine.

    Not an API response shape.  Populated by provider modules and consumed by
    the conditions blending engine to produce the weatherText field on Observation.
    extra="ignore" so provider wire fields beyond this set are silently dropped
    at parse time.
    """

    model_config = ConfigDict(extra="ignore")

    weatherText: str | None = None
    weatherCode: str | None = None
    precipType: str | None = None
    cloudCover: float | None = None
    isDay: bool | None = None
    temperature: float | None = None
    humidity: float | None = None
    windSpeed: float | None = None
    windDir: float | None = None
    snow: float | None = None
    snowRate: float | None = None
    source: str


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class PageInfo(BaseModel):
    """Pagination metadata matching OpenAPI PageInfo schema."""

    cursor: str | None = None
    next: str | None = None
    previous: str | None = None
    limit: int | None = None
    page: int | None = None
    totalPages: int | None = None
    totalRecords: int | None = None


class StationClock(BaseModel):
    """Station clock block — present on every API response (ADR-075 §3)."""

    date: str       # station-local YYYY-MM-DD
    time: str       # station-local ISO-8601 with UTC offset
    timezone: str   # IANA identifier


class FreshnessInfo(BaseModel):
    """Data freshness block — present on cacheable responses (ADR-075 §4)."""

    generatedAt: str      # UTC ISO-8601 Z
    validUntil: str       # UTC ISO-8601 Z
    refreshInterval: int  # seconds


class ObservationResponse(BaseModel):
    """ObservationResponse envelope."""

    data: Observation | None
    units: dict[str, str]
    source: str
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class ArchiveResponse(BaseModel):
    """ArchiveResponse envelope."""

    data: list[ArchiveRecord]
    units: dict[str, str]
    source: str
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None
    page: PageInfo


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class RecordEntry(BaseModel):
    """One named record (e.g. "All-time high outTemp")."""

    label: str
    canonicalField: str
    value: float | None = None
    observedAt: str | None = None  # UTC ISO-8601 with Z
    brokenInLast30Days: bool = False


class RecordsBundle(BaseModel):
    """Records grouped by section."""

    period: str
    sections: dict[str, list[RecordEntry]]


class RecordsResponse(BaseModel):
    """RecordsResponse envelope."""

    data: RecordsBundle
    units: dict[str, str]
    source: str
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Reports (NOAA)
# ---------------------------------------------------------------------------


class ReportEntry(BaseModel):
    """One NOAA report file entry (monthly or yearly)."""

    kind: str  # "monthly" | "yearly"
    year: int
    month: int | None = None
    filename: str
    modifiedAt: str  # UTC ISO-8601 with Z


class ReportIndex(BaseModel):
    """Index of available NOAA reports."""

    reports: list[ReportEntry]


class ReportIndexResponse(BaseModel):
    """ReportIndexResponse envelope."""

    data: ReportIndex
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class NOAAReport(BaseModel):
    """Raw monthly NOAA report text."""

    year: int
    month: int
    filename: str
    rawText: str
    modifiedAt: str  # UTC ISO-8601 with Z


class NOAAYearlyReport(BaseModel):
    """Raw yearly NOAA report text."""

    year: int
    filename: str
    rawText: str
    modifiedAt: str  # UTC ISO-8601 with Z


class ReportResponse(BaseModel):
    """ReportResponse envelope."""

    data: NOAAReport
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class YearlyReportResponse(BaseModel):
    """YearlyReportResponse envelope."""

    data: NOAAYearlyReport
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Almanac
# ---------------------------------------------------------------------------


class SunSnapshot(BaseModel):
    """Sun data block within AlmanacSnapshot."""

    rise: str | None = None
    set: str | None = None
    transit: str | None = None
    civilTwilightDawn: str | None = None
    civilTwilightDusk: str | None = None
    azimuth: float | None = None
    altitude: float | None = None
    rightAscension: float | None = None
    declination: float | None = None
    daylightMinutes: int | None = None
    daylightDeltaVsYesterdayMinutes: int | None = None
    nextEquinox: str | None = None
    nextSolstice: str | None = None


class MoonSnapshot(BaseModel):
    """Moon data block within AlmanacSnapshot."""

    rise: str | None = None
    set: str | None = None
    transit: str | None = None
    azimuth: float | None = None
    altitude: float | None = None
    rightAscension: float | None = None
    declination: float | None = None
    phaseName: str | None = None
    illuminationPercent: float | None = None
    nextFullMoon: str | None = None
    nextNewMoon: str | None = None


class AlmanacSnapshot(BaseModel):
    """AlmanacSnapshot matching OpenAPI AlmanacSnapshot schema."""

    date: str
    sun: SunSnapshot
    moon: MoonSnapshot


class AlmanacResponse(BaseModel):
    """AlmanacResponse envelope."""

    data: AlmanacSnapshot
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class SunTimesDay(BaseModel):
    """One day in a SunTimesSeries."""

    date: str
    sunrise: str | None = None
    sunset: str | None = None
    daylightMinutes: int | None = None


class SunTimesSeries(BaseModel):
    """SunTimesSeries matching OpenAPI schema."""

    year: int
    days: list[SunTimesDay]


class SunTimesResponse(BaseModel):
    """SunTimesResponse envelope."""

    data: SunTimesSeries
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class MoonPhaseDay(BaseModel):
    """One day in a MoonPhaseCalendar."""

    date: str
    phaseName: str
    illuminationPercent: float


class MoonPhaseCalendar(BaseModel):
    """MoonPhaseCalendar matching OpenAPI schema."""

    year: int
    month: int | None = None
    days: list[MoonPhaseDay]


class MoonPhaseResponse(BaseModel):
    """MoonPhaseResponse envelope."""

    data: MoonPhaseCalendar
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class SpecialMoonEntry(BaseModel):
    """One full moon with traditional and special name flags."""

    date: str
    traditionalName: str
    isHarvestMoon: bool
    isBlueMoon: bool
    isHuntersMoon: bool
    isSupermoon: bool


class MoonNamesCalendar(BaseModel):
    """All full moons for a year with their special name flags."""

    year: int
    moons: list[SpecialMoonEntry]


class MoonNamesResponse(BaseModel):
    """MoonNamesResponse envelope (GET /almanac/moon-names)."""

    data: MoonNamesCalendar
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class PlanetEntry(BaseModel):
    """One visible planet entry."""

    name: str
    altitude: float | None = None
    direction: str | None = None
    rise: str | None = None
    set: str | None = None
    constellation: str | None = None
    magnitude: float | None = None          # Visual magnitude (lower = brighter)
    transitTime: str | None = None          # ISO-8601 UTC when planet crosses meridian
    rightAscension: float | None = None     # RA in degrees (0-360)
    declination: float | None = None        # Dec in degrees (-90 to +90)
    elongation: float | None = None         # Angular distance from Sun in degrees
    viewingQuality: str | None = None       # excellent|good|fair|poor|not_visible
    viewingScore: float | None = None       # 0.0–1.0 composite score
    viewingNote: str | None = None          # e.g. "Bright moon nearby"
    bestViewingTime: str | None = None      # ISO-8601 UTC optimal viewing time
    conjunction: str | None = None          # e.g. "Near Moon"
    clearWindowStart: str | None = None     # ISO-8601 UTC start of clear window
    clearWindowEnd: str | None = None       # ISO-8601 UTC end of clear window


class PlanetVisibility(BaseModel):
    """Planet visibility grouped by period."""

    evening: list[PlanetEntry]
    morning: list[PlanetEntry]
    allNight: list[PlanetEntry]


class PlanetResponse(BaseModel):
    """PlanetResponse envelope (GET /almanac/planets)."""

    data: PlanetVisibility
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class EclipseContactPoint(BaseModel):
    """One contact time with local altitude."""

    date: str       # ISO-8601 UTC
    altitude: float  # degrees above horizon


class LunarEclipseEntry(BaseModel):
    """One lunar eclipse with optional AstronomyAPI.com enrichment."""

    date: str
    type: str  # "penumbral" | "partial" | "total"
    contactTimes: dict[str, EclipseContactPoint | None] | None = None
    obscuration: float | None = None
    # "Visible All Night"|"Mostly Visible"|"Low in Sky"|"Barely Visible"|"Not Visible"
    visibility: str | None = None


class LunarEclipseList(BaseModel):
    """List of lunar eclipses for a rolling date window."""

    from_date: str  # "YYYY-MM-DD" — start of search window
    to_date: str    # "YYYY-MM-DD" — end of search window
    eclipses: list[LunarEclipseEntry]


class EclipseResponse(BaseModel):
    """EclipseResponse envelope (GET /almanac/eclipses)."""

    data: LunarEclipseList
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class SolarEclipseEntry(BaseModel):
    """One solar eclipse with optional AstronomyAPI.com enrichment."""

    date: str
    type: str  # "total" | "annular" | "partial"
    contactTimes: dict[str, EclipseContactPoint | None] | None = None
    obscuration: float | None = None
    # "Fully Visible"|"Mostly Visible"|"Partially Visible"|"Barely Visible"|"Not Visible"
    visibility: str | None = None


class SolarEclipseList(BaseModel):
    """List of solar eclipses."""

    from_date: str
    to_date: str
    eclipses: list[SolarEclipseEntry]


class SolarEclipseResponse(BaseModel):
    """Envelope for GET /almanac/eclipses/solar."""

    data: SolarEclipseList
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class MeteorShowerEntry(BaseModel):
    """One meteor shower with moon data."""

    name: str
    peakDate: str
    zhr: int
    radiantAltitudeDeg: float
    moonIlluminationPercent: float
    moonPhase: str
    parentBody: str
    activeStart: str | None = None    # ISO date: peakDate - duration_days/2
    activeEnd: str | None = None      # ISO date: peakDate + duration_days/2
    description: str | None = None    # from JSON catalog
    viewingQuality: str | None = None  # "Excellent"|"Good"|"Fair"|"Poor"|"Not Visible"
    velocityKms: float | None = None  # from JSON catalog
    image: str | None = None          # filename from JSON catalog


class MeteorShowerList(BaseModel):
    """List of meteor showers for a rolling date window."""

    from_date: str  # "YYYY-MM-DD" — start of search window
    to_date: str    # "YYYY-MM-DD" — end of search window
    showers: list[MeteorShowerEntry]


class MeteorShowerResponse(BaseModel):
    """MeteorShowerResponse envelope (GET /almanac/meteor-showers)."""

    data: MeteorShowerList
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class SunPosition(BaseModel):
    """Current sun azimuth and altitude (GET /almanac/positions)."""

    azimuth: float
    altitude: float


class MoonPosition(BaseModel):
    """Current moon azimuth, altitude, illumination, and phase (GET /almanac/positions)."""

    azimuth: float
    altitude: float
    illuminationPercent: float
    phaseName: str


class PositionsSnapshot(BaseModel):
    """Real-time sun and moon positions snapshot."""

    sun: SunPosition
    moon: MoonPosition


class PositionsResponse(BaseModel):
    """PositionsResponse envelope (GET /almanac/positions)."""

    data: PositionsSnapshot
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class SeeingForecastPointResponse(BaseModel):
    """One 3-hour forecast step from the 7Timer ASTRO product."""

    model_config = ConfigDict(extra="forbid")

    validTime: datetime          # UTC timestamp
    seeingIndex: int             # 1-8
    transparencyIndex: int       # 1-8
    cloudCoverOctet: int         # 1-9
    liftedIndex: int             # atmospheric stability
    windSpeedClass: int          # 1-8
    windDirection: str           # N/NE/E/SE/S/SW/W/NW
    temp2mC: int                 # Celsius
    humidityClass: int           # -4 to 16
    precType: str                # none/rain/snow/frzr/icep


class SeeingForecastData(BaseModel):
    """The 7Timer seeing forecast bundle."""

    model_config = ConfigDict(extra="forbid")

    initTime: str                        # ISO-8601, 7Timer model initialization time
    points: list[SeeingForecastPointResponse]


class SeeingForecastResponse(BaseModel):
    """SeeingForecastResponse envelope (GET /almanac/seeing-forecast)."""

    model_config = ConfigDict(extra="forbid")

    data: SeeingForecastData
    generatedAt: str             # ISO-8601
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Station
# ---------------------------------------------------------------------------


class StationMetadata(BaseModel):
    """StationMetadata matching OpenAPI StationMetadata schema."""

    stationId: str
    name: str
    latitude: float
    longitude: float
    altitude: float
    timezone: str
    timezoneOffsetMinutes: int
    unitSystem: str
    firstRecord: str | None = None
    lastRecord: str | None = None
    hardware: str | None = None
    defaultLocale: str = "en"
    archiveIntervalSeconds: int = 300
    weekStartDay: int = 6
    idleTimeout: int = 30          # minutes; 0 = disabled (ADR-075 T1.6)
    idleRefreshFactor: int = 10    # multiplier applied when tab is idle


class StationResponse(BaseModel):
    """StationResponse envelope."""

    data: StationMetadata
    units: dict[str, str]
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class WeewxColumnEntry(BaseModel):
    """One weewx archive column entry in CapabilityRegistry."""

    canonicalField: str
    archiveColumn: str


class ProviderAttributionResponse(BaseModel):
    """Attribution metadata for a provider (ADR-080). camelCase for JSON."""

    attributionRequired: bool
    displayName: str
    attributionText: str
    textPrefix: str
    textProviderName: str
    url: str
    textTranslatable: bool = False
    textLanguage: str = "en"
    logoRequired: bool = False
    doNotUseLogo: bool = False


class CapabilityDeclaration(BaseModel):
    """Per ADR-038: one configured provider module."""

    providerId: str
    domain: str
    suppliedCanonicalFields: list[str]
    geographicCoverage: str
    defaultPollIntervalSeconds: int | None = None
    operatorNotes: str | None = None
    tileUrlTemplate: str | None = None
    wmsEndpointUrl: str | None = None
    wmsLayerName: str | None = None
    tileContentType: str | None = None
    iframeUrl: str | None = None
    # LibreWxR-specific fields (None for non-LibreWxR providers)
    caddyPrefix: str | None = None
    alertUrl: str | None = None
    bounds: dict[str, float] | None = None
    refreshInterval: int | None = None
    nowcastAvailable: bool | None = None
    alertsAvailable: bool | None = None
    satelliteAvailable: bool | None = None
    satelliteTileUrlTemplate: str | None = None
    isObservedSource: bool = True
    attribution: ProviderAttributionResponse | None = None


class CapabilityRegistry(BaseModel):
    """CapabilityRegistry matching OpenAPI CapabilityRegistry schema."""

    providers: list[CapabilityDeclaration]
    weewxColumns: list[WeewxColumnEntry]
    canonicalFieldsAvailable: list[str]


class CapabilityResponse(BaseModel):
    """CapabilityResponse envelope."""

    data: CapabilityRegistry
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class PageMetadata(BaseModel):
    """PageMetadata matching OpenAPI PageMetadata schema."""

    slug: str
    name: str
    icon: str
    navPosition: int
    builtIn: bool
    hidden: bool = False


class PageList(BaseModel):
    """PageList matching OpenAPI PageList schema."""

    pages: list[PageMetadata]


class PageListResponse(BaseModel):
    """PageListResponse envelope."""

    data: PageList
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


class ChartGroup(BaseModel):
    """ChartGroup matching OpenAPI ChartGroup schema."""

    id: str
    name: str
    builtIn: bool
    members: list[str]
    defaultRange: str | None = None


class ChartGroupList(BaseModel):
    """ChartGroupList matching OpenAPI ChartGroupList schema."""

    groups: list[ChartGroup]


class ChartGroupResponse(BaseModel):
    """ChartGroupResponse envelope."""

    data: ChartGroupList
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Charts Config (GET /charts/config)
# ---------------------------------------------------------------------------


class SeriesConfigResponse(BaseModel):
    """One data series configuration."""

    seriesId: str
    observationType: str | None = None
    name: str | None = None
    color: str | None = None
    type: str | None = None
    zIndex: int | None = None
    yAxis: int | None = None
    yAxisMin: float | None = None
    yAxisMax: float | None = None
    yAxisTickDecimals: int | None = None
    yAxisLabel: str | None = None
    yAxisTickInterval: float | None = None
    lineWidth: int | None = None
    connectNulls: bool | None = None
    visible: bool | None = None
    opacity: float | None = None
    stacking: str | None = None
    aggregateType: str | None = None
    averageType: str | None = None
    markerEnabled: bool | None = None
    markerRadius: int | None = None
    beaufortColors: dict[int, str] = {}
    rangeType: str | None = None
    areaDisplay: int | None = None
    useCustomSql: bool = False
    customSqlQuery: str | None = None
    xColumn: str | None = None
    yColumn: str | None = None
    yAxisSoftMin: float | None = None
    yAxisSoftMax: float | None = None
    borderWidth: int | None = None
    numberFormat: dict[str, Any] | None = None


class ChartConfigResponse(BaseModel):
    """One chart configuration."""

    chartId: str
    title: str | None = None
    type: str | None = None
    connectNulls: bool | None = None
    yAxisMin: float | None = None
    aggregateType: str | None = None
    aggregateInterval: int | None = None
    xAxisGroupby: str | None = None
    xAxisCategories: list[str] = []
    forceFullYear: bool | None = None
    timeLength: int | str | None = None
    series: list[SeriesConfigResponse] = []


class ChartGroupConfigResponse(BaseModel):
    """One chart group (tab) configuration."""

    groupId: str
    title: str | None = None
    showButton: bool = True
    buttonText: str | None = None
    type: str | None = None
    enableDateRanges: bool = False
    rollingRanges: list[str] = []
    availableYears: list[int] = []
    enableMonthlyBreakdown: bool = False
    timeLength: int | str | None = None
    timespanStart: int | None = None
    timespanStop: int | None = None
    tooltipDateFormat: str | None = None
    gapsize: int | None = None
    aggregateInterval: int | None = None
    aggregateType: str | None = None
    forceFullYear: bool = False
    startAtBeginningOfMonth: bool = False
    pageContent: str | None = None
    generate: str | None = None
    charts: list[ChartConfigResponse] = []


class ChartsConfigData(BaseModel):
    """Full charts configuration."""

    aggregateType: str | None = None
    timeLength: int | str | None = None
    type: str = "line"
    colors: list[str] = []
    tooltipDateFormat: str | None = None
    groups: list[ChartGroupConfigResponse] = []


class ChartsConfigResponse(BaseModel):
    """GET /charts/config envelope."""

    data: ChartsConfigData
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Content (markdown passthrough)
# ---------------------------------------------------------------------------


class MarkdownContent(BaseModel):
    """MarkdownContent matching OpenAPI MarkdownContent schema."""

    markdown: str
    updatedAt: str | None = None


class MarkdownResponse(BaseModel):
    """MarkdownResponse envelope."""

    data: MarkdownContent
    generatedAt: str
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Forecast (ADR-007, canonical-data-model §3.3, §3.4, §3.5, §3.10)
# ---------------------------------------------------------------------------

# ruff: noqa: N815  (field names use canonical camelCase: validTime, outTemp, etc.)


class HourlyForecastPoint(BaseModel):
    """Canonical hourly forecast record (ADR-010 §3.3, OpenAPI HourlyForecastPoint schema).

    extra="ignore" so provider-specific wire fields don't break normalization.
    Required fields per OpenAPI: validTime, source.
    All forecast numeric fields are Optional — providers may not supply all.
    """

    model_config = ConfigDict(extra="ignore")

    validTime: str                      # UTC ISO-8601 with Z
    outTemp: float | None = None
    outHumidity: float | None = None
    windSpeed: float | None = None
    windDir: float | None = None
    windGust: float | None = None
    precipProbability: float | None = None   # 0-100
    precipAmount: float | None = None
    precipType: str | None = None            # "rain" | "snow" | "freezing-rain" | null
    cloudCover: float | None = None          # 0-100
    weatherCode: str | None = None           # WMO code as string (opaque to api)
    weatherText: str | None = None           # Human-readable (decoded from WMO)
    feelsLike: float | None = None           # Apparent temp (heat index / wind chill)
    dewpoint: float | None = None            # Dewpoint temperature
    source: str
    extras: dict[str, Any] = {}


class DailyForecastPoint(BaseModel):
    """Canonical daily forecast record (ADR-010 §3.4, OpenAPI DailyForecastPoint schema).

    extra="ignore" so provider-specific wire fields don't break normalization.
    Required fields per OpenAPI: validDate, source.
    validDate is station-local "YYYY-MM-DD" (ADR-020: date-only fields stay local).
    sunrise/sunset are UTC ISO-8601 with Z (full datetime fields).
    narrative is always None for Open-Meteo (no per-day narrative supplied).
    forecastText is populated by sse/forecast_text_enrichment.py (ADR-082
    T7.3) from GFE-composed period text; left None for the NWS provider,
    which uses the narrative pass-through instead (API-MANUAL SS15).
    """

    model_config = ConfigDict(extra="ignore")

    validDate: str                           # station-local "YYYY-MM-DD"
    tempMax: float | None = None
    tempMin: float | None = None
    precipAmount: float | None = None
    precipProbabilityMax: float | None = None  # 0-100
    windSpeedMax: float | None = None
    windGustMax: float | None = None
    sunrise: str | None = None               # UTC ISO-8601 with Z
    sunset: str | None = None                # UTC ISO-8601 with Z
    uvIndexMax: float | None = None
    weatherCode: str | None = None           # WMO code as string
    cloudCover: float | None = None          # 0-100; max cloud cover across the day's hourly points
    weatherText: str | None = None
    narrative: str | None = None             # per-day summary (NWS/some Aeris plans; null here)
    forecastText: str | None = None          # GFE text engine output (ADR-082 T7.3); null for
                                              # the NWS pass-through path (see narrative above)
    dewpointMax: float | None = None
    dewpointMin: float | None = None
    humidityMax: float | None = None
    humidityMin: float | None = None
    visibilityMax: float | None = None
    visibilityMin: float | None = None
    snowAmount: float | None = None
    iceAccumulation: float | None = None     # inches (US) / mm (metric); Xweather only
    thunderRisk: float | None = None
    tornadoRisk: float | None = None
    hailRisk: float | None = None
    windRisk: float | None = None
    source: str
    extras: dict[str, Any] = {}


class ForecastDiscussion(BaseModel):
    """Canonical forecast discussion (ADR-010 §3.5, OpenAPI ForecastDiscussion schema).

    Always null for Open-Meteo (no discussion endpoint).
    Defined here for completeness and for future NWS / Aeris rounds.
    """

    model_config = ConfigDict(extra="ignore")

    headline: str | None = None
    body: str
    issuedAt: str                # UTC ISO-8601 with Z
    validFrom: str | None = None # UTC ISO-8601 with Z
    validUntil: str | None = None
    senderName: str | None = None
    source: str


class ForecastBundle(BaseModel):
    """ForecastBundle container (ADR-010 §3.10, OpenAPI ForecastBundle schema).

    extra="ignore" for round-trip safety through cache (model_dump → model_validate).
    discussion is always None for Open-Meteo; ForecastDiscussion for providers
    that supply it (NWS AFD, some Aeris plans).
    source: provider_id (e.g. "openmeteo") or "none" when no provider is configured.
    """

    model_config = ConfigDict(extra="ignore")

    hourly: list[HourlyForecastPoint] = []
    daily: list[DailyForecastPoint] = []
    discussion: ForecastDiscussion | None = None
    source: str
    generatedAt: str                # UTC ISO-8601 with Z


class ForecastResponse(BaseModel):
    """ForecastResponse envelope (OpenAPI ForecastResponse schema).

    data: ForecastBundle (hourly + daily + discussion + source + generatedAt).
    units: UnitsBlock from services/units.py — same wiring as observations + records.
    source: mirrors data.source (provider_id or "none").
    generatedAt: UTC ISO-8601 with Z.
    """

    data: ForecastBundle
    units: dict[str, str]
    source: str
    generatedAt: str                # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Alerts (ADR-016, canonical-data-model §3.6 + §3.11)
# ---------------------------------------------------------------------------

# ruff: noqa: N815  (field names use NWS/OpenAPI camelCase: senderName, areaDesc, etc.)


class AlertRecord(BaseModel):
    """Canonical alert record (ADR-010 §3.6, ADR-052, OpenAPI AlertRecord schema).

    extra="ignore" so provider wire shapes that have extra fields don't break
    normalization.  Required fields per OpenAPI: id, headline, event,
    effective, source.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    headline: str
    description: str = ""
    urgency: str | None = None
    certainty: str | None = None
    event: str
    effective: str  # UTC ISO-8601 with Z
    expires: str | None = None  # UTC ISO-8601 with Z — CAP message expiry
    ends: str | None = None     # UTC ISO-8601 with Z — expected event end time
    senderName: str | None = None
    areaDesc: str | None = None
    category: str | None = None
    source: str
    # ADR-052: geography-correct alert model fields
    severityLevel: int | None = None    # 1–4 ordinal; None for passthrough providers
    severityLabel: str | None = None    # human-readable, locale-appropriate label
    alertSystem: str | None = None      # originating system: "nws", "meteoalarm", etc.
    hazardType: str | None = None       # category: "thunderstorm", "fire", etc.
    nativeName: str | None = None       # alert name in original language
    color: str | None = None            # provider-supplied hex color code


class AlertList(BaseModel):
    """AlertList container (ADR-010 §3.11, OpenAPI AlertList schema)."""

    model_config = ConfigDict(extra="ignore")

    alerts: list[AlertRecord]
    retrievedAt: str  # UTC ISO-8601 with Z
    source: str  # provider_id or "none"


class AlertListResponse(BaseModel):
    """AlertListResponse envelope (OpenAPI AlertListResponse schema)."""

    model_config = ConfigDict(extra="ignore")

    data: AlertList
    source: str  # mirrors data.source
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# AQI (ADR-013, canonical-data-model §3.8)
# ---------------------------------------------------------------------------

# ruff: noqa: N815  (field names use canonical camelCase: aqiCategory, etc.)


class AQIReading(BaseModel):
    """Canonical AQI reading (ADR-010 §3.8, OpenAPI AQIReading schema).

    Raw provider data — no EPA conversion at ingest. The dashboard applies
    any display-scale conversion using aqiScale as the discriminator.
    extra="ignore" so provider wire shapes that have extra fields don't break
    normalization.  Required fields per OpenAPI: observedAt, source.
    All AQI numeric fields are Optional — providers may not supply all.
    """

    model_config = ConfigDict(extra="ignore")

    aqi: float | None = None
    aqiScale: str | None = None          # scale the aqi value is on: "epa" (0-500), "owm" (1-5), etc.
    aqiCategory: str | None = None       # dashboard-computed from aqi+aqiScale; parsers set None
    aqiMainPollutant: str | None = None  # canonical pollutant id: PM2.5/PM10/O3/NO2/SO2/CO
    aqiLocation: str | None = None       # free-form provider location label (PARTIAL-DOMAIN for Open-Meteo)
    pollutantPM25: float | None = None   # µg/m³ (group_concentration)
    pollutantPM10: float | None = None   # µg/m³ (group_concentration)
    pollutantO3: float | None = None     # µg/m³ (group_concentration; raw provider value)
    pollutantNO2: float | None = None    # µg/m³ (group_concentration; raw provider value)
    pollutantSO2: float | None = None    # µg/m³ (group_concentration; raw provider value)
    pollutantCO: float | None = None     # µg/m³ (group_concentration; raw provider value)
    pollutantNO: float | None = None     # µg/m³ (group_concentration; OWM/OpenMeteo only — ADR-059)
    pollutantNH3: float | None = None    # µg/m³ (group_concentration; OWM/OpenMeteo Europe only — ADR-059)
    observedAt: str                      # UTC ISO-8601 with Z; required per OpenAPI
    source: str                          # "weewx" (Path A) or provider_id (Path B)
    pollutantSources: dict[str, str] | None = None  # field → source string (T4B.3); non-null only when merge ran
    pollutantSubIndices: dict[str, float | None] | None = None  # canonical pollutant id → sub-AQI on same scale as aqi; None when provider doesn't supply


class AQIResponse(BaseModel):
    """AQIResponse envelope (OpenAPI AQIResponse schema).

    data: AQIReading or None (null when no AQI provider configured or no reading).
    units: UnitsBlock — pollutant unit declarations.
    source: provider_id or "none".
    generatedAt: UTC ISO-8601 with Z.
    """

    data: AQIReading | None
    units: dict[str, str]
    source: str
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


class AQIHistoryResponse(BaseModel):
    """AQIHistoryResponse envelope (OpenAPI AQIHistoryResponse schema, P4-T3).

    data: list of AQIReading from the weewx archive (Path A operators).
    units: UnitsBlock — pollutant unit declarations (same as /aqi/current).
    source: always "weewx" (reads from weewx archive per ADR-013 corrected).
    generatedAt: UTC ISO-8601 with Z.
    page: pagination metadata.
    """

    data: list[AQIReading]
    units: dict[str, str]
    source: str
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None
    page: PageInfo


# ---------------------------------------------------------------------------
# Earthquakes (ADR-040, canonical-data-model §3.7 + §2.4)
# ---------------------------------------------------------------------------

# ruff: noqa: N815  (camelCase canonical names: magnitudeType, etc.)


class EarthquakeRecord(BaseModel):
    """Canonical earthquake record (ADR-010 §3.7, OpenAPI EarthquakeRecord schema).

    extra="ignore" so provider wire shapes that have extra fields don't break
    normalization.  Required fields per OpenAPI: id, time, latitude, longitude,
    magnitude, source.

    Coordinates are always WGS84 decimal degrees and magnitude is always a
    dimensionless number — those remain unit-system-invariant. ``depth`` and
    ``distance``, however, DO participate in the unit system: the endpoint
    converts both to the operator's configured `group_distance` display unit
    (mile or km) before this model is populated. See API-MANUAL.md §2
    "Earthquake fields".
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    time: str  # UTC ISO-8601 with Z
    latitude: float
    longitude: float
    magnitude: float
    magnitudeType: str | None = None
    depth: float | None = None  # operator's group_distance unit (mile or km)
    place: str | None = None
    url: str | None = None
    tsunami: bool | None = None
    felt: int | None = None
    mmi: float | None = None
    alert: str | None = None  # green/yellow/orange/red (USGS PAGER); None for non-USGS
    status: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    source: str
    distance: float | None = None  # distance from station, operator's group_distance unit


class EarthquakeListResponse(BaseModel):
    """EarthquakeListResponse envelope (OpenAPI EarthquakeListResponse schema).

    Depth and distance are converted to the operator's configured
    `group_distance` display unit (mile or km); the `units` block reflects
    the unit actually used (`units.depth` and `units.distance`).
    """

    model_config = ConfigDict(extra="ignore")

    data: list[EarthquakeRecord]
    units: dict[str, str] | None = None
    source: str  # provider_id or "none"
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Branding (ADR-022, Gap #10)
# ---------------------------------------------------------------------------


class LogoBranding(BaseModel):
    """Logo branding block within BrandingConfig (ADR-022).

    lightUrl is required; darkUrl is optional (dashboard CSS-inverts when absent,
    per ADR-022 §consequences).  Alt text is optional (empty when not configured).
    """

    lightUrl: str
    darkUrl: str | None = None
    alt: str = ""


class SocialConfig(BaseModel):
    """Social media URLs block within BrandingConfig.

    All fields default to empty string (unconfigured).  Dashboard checks for
    non-empty before rendering social links in the footer.
    """

    facebook: str = ""
    twitter: str = ""
    instagram: str = ""
    youtube: str = ""


class BrandingConfig(BaseModel):
    """Operator branding configuration (ADR-022, Gap #10).

    Fields read from api.conf [branding] and [social] sections.
    All optional with safe defaults so the endpoint works even when no
    [branding] or [social] section is present in api.conf.
    """

    accent: Literal["blue", "teal", "indigo", "purple", "green", "amber"]
    defaultThemeMode: Literal["light", "dark", "auto-os", "auto-sunrise-sunset"]
    logo: LogoBranding | None = None
    customCssUrl: str | None = None
    siteTitle: str = ""
    copyrightEntity: str = ""
    faviconUrl: str = ""
    social: SocialConfig = SocialConfig()


class BrandingResponse(BaseModel):
    """BrandingResponse envelope (GET /api/v1/branding)."""

    data: BrandingConfig
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Radar (ADR-015, 3b-14)
# ---------------------------------------------------------------------------


class RadarFrame(BaseModel):
    """One radar frame (canonical-data-model §4.5, OpenAPI RadarFrame schema).

    Radar has no canonical-entity mapping — tiles are bytes fetched browser-side.
    RadarFrame carries the timestamp, kind, and (RainViewer only) a per-frame
    `path` that the dashboard combines with the response-level `tileHost` to
    materialize the tile URL via CAPABILITY.tile_url_template.

    `path` is None for WMS-T providers (they compose tile URLs purely via
    `?TIME=<time>`); set only for RainViewer per its api-docs (3b-14 auditor F2).
    """

    model_config = ConfigDict(extra="ignore")

    time: str  # UTC ISO-8601 with Z (ADR-020)
    kind: str  # "past" | "current" | "nowcast" (OpenAPI RadarFrame kind enum)
    path: str | None = None  # RainViewer per-frame tile path; None for WMS-T


class RadarFrameList(BaseModel):
    """List of radar frames for a single provider (OpenAPI RadarFrameList schema).

    required: [providerId, frames]
    attribution + tileHost are nullable.

    `tileHost` is RainViewer's per-fetch tile-server host from the JSON envelope
    (`weather-maps.json.host`; currently `https://tilecache.rainviewer.com`).
    The dashboard substitutes it into CAPABILITY.tile_url_template's `{host}`
    placeholder along with each frame's `path`. None for WMS-T providers
    (their tile URL is composed differently — see CAPABILITY.wms_endpoint_url).
    3b-14 auditor F2 added this.

    `colorSchemes` is LibreWxR-specific: the list of available color schemes
    from radar.colorSchemes in the wire response. None for all other providers.
    """

    model_config = ConfigDict(extra="ignore")

    providerId: str
    frames: list[RadarFrame]
    attribution: str | None = None
    tileHost: str | None = None  # RainViewer/LibreWxR per-fetch tile host; None for WMS-T
    colorSchemes: list[dict[str, Any]] | None = None  # LibreWxR [{id: int, name: str}, ...]; None for others
    satelliteFrames: list[RadarFrame] | None = None  # LibreWxR satellite IR frames; None for others


class RadarFramesResponse(BaseModel):
    """RadarFramesResponse envelope (OpenAPI RadarFramesResponse schema).

    Wraps RadarFrameList in the standard data + generatedAt envelope.
    """

    model_config = ConfigDict(extra="ignore")

    data: RadarFrameList
    generatedAt: str  # UTC ISO-8601 with Z
    stationClock: StationClock | None = None
    freshness: FreshnessInfo | None = None


# ---------------------------------------------------------------------------
# Marine (ADR-083..090, API-MANUAL §16 "Marine Data Model")
# ---------------------------------------------------------------------------

# ruff: noqa: N815  (canonical fields use weewx-aligned camelCase: windSpeed, etc.)


class SpectralWaveComponent(BaseModel):
    """Single swell system from NDBC spectral decomposition (API-MANUAL §16).

    height/period/direction/energy describe one decomposed swell system;
    classification is derived from period per NOAA convention.
    """

    model_config = ConfigDict(extra="ignore")

    height: float               # group_wave_height; significant wave height (Hs = 4*sqrt(m0))
    period: float                # group_wave_period; peak period (Tp = 1/fp)
    direction: float              # degrees true north; energy-weighted circular mean
    energy: float                 # zeroth spectral moment m0 (m^2)
    frequencyRange: list[float]   # [min_hz, max_hz] bounds of this spectral partition
    classification: str           # "groundswell" (>=12s) | "swell" (8-12s) | "wind_swell" (<8s)


class MarineObservation(BaseModel):
    """Single buoy observation snapshot from NDBC standard met data (API-MANUAL §16).

    extra="ignore" so provider wire shapes that have extra fields don't break
    normalization.  Required fields: stationId, time.  Everything else may be
    genuinely absent from a given buoy's payload.
    """

    model_config = ConfigDict(extra="ignore")

    windSpeed: float | None = None        # group_ocean_speed; sustained wind speed
    windDirection: float | None = None    # degrees true north, meteorological convention
    windGust: float | None = None         # group_ocean_speed
    waveHeight: float | None = None       # group_wave_height; significant wave height (Hs)
    dominantPeriod: float | None = None   # group_wave_period
    averagePeriod: float | None = None    # group_wave_period
    meanWaveDirection: float | None = None  # degrees true north
    pressure: float | None = None         # group_pressure; sea-level pressure
    airTemp: float | None = None          # group_temperature
    waterTemp: float | None = None        # group_temperature; sea surface temperature
    dewpoint: float | None = None         # group_temperature
    visibility: float | None = None       # group_visibility
    pressureTendency: float | None = None  # group_pressure; 3-hour tendency
    tideLevel: float | None = None        # group_water_level; where reported
    stationId: str
    time: str  # UTC ISO-8601 with Z
    spectralComponents: list[SpectralWaveComponent] | None = None  # decomposed swell systems
    weatherCode: int | None = None  # WMO code (no conversion); from forecast provider (T2.1/T2.2)
    isDay: bool | None = None       # from forecast provider (T2.1/T2.2)


class TidePrediction(BaseModel):
    """Predicted tide event from CO-OPS harmonic predictions (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    time: str  # UTC ISO-8601 with Z
    height: float  # group_water_level; relative to datum
    type: str | None = None  # "high" | "low" | None for interpolated points
    datum: str = "MLLW"  # vertical datum, e.g. "MLLW", "NAVD88" (ADR-098)


class WaterLevel(BaseModel):
    """Observed water level from CO-OPS gauges (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    time: str  # UTC ISO-8601 with Z
    height: float  # group_water_level; relative to datum
    datum: str  # reference datum, e.g. "MLLW", "MSL", "NAVD88"
    quality: str | None = None  # CO-OPS quality flag, e.g. "v" verified, "p" preliminary


class MarineForecastPoint(BaseModel):
    """Single timestep from a WaveWatch III wave forecast (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    time: str  # UTC ISO-8601 with Z; forecast valid time
    waveHeight: float | None = None       # group_wave_height; significant wave height (HSIGN)
    wavePeriod: float | None = None       # group_wave_period; peak wave period
    waveDirection: float | None = None    # degrees true north; peak wave direction
    windSpeed: float | None = None        # group_ocean_speed; 10m wind speed
    windDirection: float | None = None    # degrees true north
    swellHeight: float | None = None      # group_wave_height; SWAN HSWELL (swell-only Hs) at this point
    swellPeriod: float | None = None      # group_wave_period; primary swell period
    swellDirection: float | None = None   # degrees true north; primary swell direction
    windWaveHeight: float | None = None      # group_wave_height
    windWavePeriod: float | None = None      # group_wave_period
    windWaveDirection: float | None = None   # degrees true north
    waterTemp: float | None = None           # group_temperature; OFS model forecast water temp
    # T3.2 — cross-shore transect fields (SWAN CURVE TABLE output, API-MANUAL §17)
    depth: float | None = None                   # water depth at this transect point (m, positive = wet)
    breakingFraction: float | None = None        # QB — fraction of breaking waves (0.0–1.0)
    breakingDissipation: float | None = None     # DISSURF — depth-induced wave breaking dissipation (W/m²)
    setup: float | None = None                   # SETUP — wave-induced water level rise (m)
    directionalSpread: float | None = None       # DSPR — directional spreading of the wave spectrum (°)
    distanceFromShore: float | None = None       # distance along transect from the shoreline (m)


class MarineTextForecast(BaseModel):
    """Single period from an NWS marine zone text forecast (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    periodName: str  # e.g. "Tonight", "Thursday"
    text: str        # full forecast narrative
    wind: str | None = None         # wind description extracted from narrative
    seas: str | None = None         # seas description
    visibility: str | None = None   # visibility description
    weather: str | None = None      # weather description


class WaterColumnLayer(BaseModel):
    """Single depth layer in a water column profile (ADR-091, API-MANUAL §16)."""

    depth_m: float
    temperature: float | None = None
    salinity: float | None = None


class WaterColumnProfile(BaseModel):
    """Vertical temperature/salinity profile from OFS or RTOFS (ADR-091)."""

    layers: list[WaterColumnLayer]
    thermocline_depth_m: float | None = None
    bottom_temp: float | None = None
    seafloor_depth_m: float | None = None
    source: str
    timestamp: str


class OceanCurrentSnapshot(BaseModel):
    """Current speed and direction at a point (ADR-091)."""

    speed: float
    direction: float
    depth_m: float | None = None


class OceanForecastPoint(BaseModel):
    """Single timestep in an ocean data forecast (ADR-091)."""

    time: str
    surface_temp: float | None = None
    current_speed: float | None = None
    current_direction: float | None = None
    source: str


class OceanDataResult(BaseModel):
    """Ocean data resolver output — everything endpoints need from ocean model data.

    Returned by ocean_data_resolver.resolve(). Raw values in Celsius/m/s/PSU;
    unit conversion happens in the endpoint, not the resolver.
    """

    model_config = ConfigDict(extra="ignore")

    surface_temp: float | None = None
    column_profile: WaterColumnProfile | None = None
    thermocline_depth_m: float | None = None
    bottom_temp_c: float | None = None
    seafloor_depth_m: float | None = None

    surface_current_speed: float | None = None
    surface_current_dir: float | None = None
    current_profile: list[OceanCurrentSnapshot] | None = None

    surface_salinity: float | None = None

    water_level_msl: float | None = None
    water_level_mllw: float | None = None

    forecast: list[OceanForecastPoint] | None = None

    source: str = "unavailable"
    source_type: str = "modeled"
    coverage_tier: str = "unavailable"


class SurfScoringBreakdown(BaseModel):
    """Individual factor scores for the surf quality rating (API-MANUAL §17).

    Three weighted factors express the core surf quality (max points shown):
      waveHeight (max 35) + wavePeriod (max 35) + waveOrganization (max 30)

    Three signed-integer adjustments surface all penalty/bonus factors:
      beachAlignment + directionalExposure + timeOfDay

    Additive identity:
      total = waveHeight + wavePeriod + waveOrganization
              + beachAlignment + directionalExposure + timeOfDay

    Organization sub-factors show each component's point contribution within
    the 30-point organization budget:
      organizationWind (max ~15) + organizationSwellDominance (max 7.5)
      + organizationDirectionalSpread (max 4.5) + organizationCrossSwell (max 3)
    """

    model_config = ConfigDict(extra="ignore")

    # Top-level weighted factors (in their own scoring units)
    waveHeight: int              # 0-35; wave height component score
    wavePeriod: int              # 0-35+; wave period component score (multipliers may exceed 35)
    waveOrganization: int        # 0-30; wave organization composite score

    # Organization composite sub-factors (point contributions, not 0-1 ratios)
    organizationWind: float              # 0-~15; wind effect contribution (50% of 30)
    organizationSwellDominance: float    # 0-7.5; swell dominance contribution (25% of 30)
    organizationDirectionalSpread: float  # 0-4.5; directional spread contribution (15% of 30)
    organizationCrossSwell: float        # 0-3; cross-swell interference contribution (10% of 30)

    # Signed adjustment factors (negative = penalty, positive = bonus, 0 = neutral)
    beachAlignment: int      # signed; beach angle alignment adjustment
    directionalExposure: int  # signed; 0 when open, negative when blocked by headland/bathymetry
    timeOfDay: int            # signed; positive at dawn, negative in afternoon, 0 otherwise


class SurfForecast(BaseModel):
    """Surf quality forecast for one spot at one timestep (API-MANUAL §16).

    Produced by enrichment/surf_scorer.py, after wave_transform.py has
    applied the nearshore supplements and breaker_height.py has converted
    the post-supplement Hsig to face height (see API-MANUAL §17).
    """

    model_config = ConfigDict(extra="ignore")

    time: str  # UTC ISO-8601 with Z; forecast valid time
    # T4A.4: waveHeightAtBreak is now derived from the SwellTrack break-point
    # Hs (never the removed SWAN CURVE formula) — null when modelStatus is
    # "unavailable" (model failed/never ran), 0.0 when "no_breaking" (LC-19).
    waveHeightAtBreak: float | None = None
    period: float              # group_wave_period; dominant period
    direction: float            # degrees true north; dominant swell direction
    # T4A.4/LC-17: null when score_surf() receives no face height (model
    # unavailable) — a missing rating, not a defaulted "Poor" one.
    qualityStars: int | None = None    # 1-5 star rating
    qualityLabel: str | None = None    # "Poor" | "Fair" | "Good" | "Very Good" | "Epic"
    conditionsText: str         # natural-language conditions summary; resolves
    # through the locale file even in the null-score case (a "forecast
    # unavailable" sentence, never an empty string — API-MANUAL §17 i18n).
    windQuality: str            # offshore | cross_offshore | cross | cross_onshore | onshore
    swellDominance: float       # ratio of primary swell energy to total energy (0.0-1.0)
    multiSwell: list[SpectralWaveComponent] | None = None  # individual swell systems, if available
    scoring: SurfScoringBreakdown | None = None  # per-factor score breakdown; None if unavailable
    # T2.6 — breaker height pipeline (API-MANUAL §16, §17)
    swellHeight: float | None = None            # HSWELL at ~10m depth (display units, ADR-095)
    breakingFaceHeight: float | None = None     # trough-to-crest face height via breaker formula (meters)
    breakingHawaiianHeight: float | None = None  # back-of-wave scale = breakingFaceHeight × 0.5 (meters)
    windSource: str | None = None  # "hrrr" for forecast timesteps, "station" for t=0
    breakPoints: list[dict] | None = None  # QB peak locations [{distance, depth, hs}] (T3.4, T4A.1)
    # T5.2 — SWAN TABLE quantities at ~10m depth
    directionalSpread: float | None = None  # DSPR — directional spreading (degrees)
    setup: float | None = None              # SETUP — wave-induced water level rise (meters)


class FishingForecast(BaseModel):
    """Fishing conditions forecast for one spot for one period (API-MANUAL §16).

    Produced by enrichment/fishing_scorer.py.  Wind/swell fields are
    informational only — not inputs to overallScore.
    """

    model_config = ConfigDict(extra="ignore")

    periodStart: str  # UTC ISO-8601 with Z
    periodEnd: str     # UTC ISO-8601 with Z
    periodLabel: str   # e.g. "Early Morning", "Late Afternoon"
    overallScore: int         # composite score 0-100
    pressureScore: int        # pressure component sub-score 0-100
    tideScore: int             # tide component sub-score 0-100
    solunarScore: int          # solunar component sub-score 0-100
    waterTempScore: int | None = None  # always None (T6.2): temperature is scored
    # purely per-species (speciesScores) since 2026-07-11, not as a shared
    # component of overallScore — see enrichment/fishing_scorer.py module
    # docstring for why the category-level component was removed.
    timeofdayScore: int        # time-of-day component sub-score 0-100
    speciesScores: list[dict[str, Any]] | None = None  # per-species score adjustments
    conditionsText: str        # natural-language conditions summary
    windSpeed: float | None = None      # group_ocean_speed; informational, not scored
    windDirection: float | None = None  # degrees true north; informational
    windGust: float | None = None       # group_ocean_speed; informational
    swellHeight: float | None = None    # group_wave_height; informational, not scored
    swellPeriod: float | None = None    # group_wave_period; informational


class SolunarTimes(BaseModel):
    """Solunar major/minor feeding periods for one date at one location (API-MANUAL §16).

    Computed via Skyfield — no external API (enrichment/solunar.py).
    Not gated by the marine feature; also backs GET /api/v1/almanac/solunar.
    """

    model_config = ConfigDict(extra="ignore")

    date: str  # YYYY-MM-DD
    moonPhase: str  # "new" | "waxing_crescent" | "first_quarter" | "waxing_gibbous" |
                    # "full" | "waning_gibbous" | "last_quarter" | "waning_crescent"
    moonIllumination: float  # 0.0-1.0
    moonrise: str | None = None  # UTC ISO-8601 with Z; None if moon doesn't rise
    moonset: str | None = None   # UTC ISO-8601 with Z; None if moon doesn't set
    moonTransit: str      # UTC ISO-8601 with Z; highest point
    moonUnderfoot: str    # UTC ISO-8601 with Z; opposite transit
    majorPeriods: list[dict[str, str]]  # 2/day, on transit/underfoot: [{start, end}, ...]
    minorPeriods: list[dict[str, str]]  # 2/day, on moonrise/moonset: [{start, end}, ...]
    intensity: float  # 0.0-1.0; driven by moon phase (new/full = 1.0, quarter = 0.7, between = 0.5)


class SurfZoneForecast(BaseModel):
    """NWS Surf Zone Forecast (SRF) per county zone per day (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    date: str        # YYYY-MM-DD
    countyZone: str  # NWS county zone identifier
    ripCurrentRisk: str  # "low" | "moderate" | "high"
    surfHeightMin: float | None = None  # group_wave_height
    surfHeightMax: float | None = None  # group_wave_height
    uvIndex: int | None = None          # 1-11+
    waterTemp: float | None = None      # group_temperature
    windText: str | None = None         # wind description
    hazardsText: str | None = None      # hazard statements


class BeachSafetyAssessment(BaseModel):
    """Composite beach safety assessment per location (API-MANUAL §16).

    ripCurrentRisk is sourced from SRF when available.
    """

    model_config = ConfigDict(extra="ignore")

    safetyLevel: str | None = None  # T9.1: always null in v1 — see API-MANUAL §16
    waveHeight: float | None = None       # group_wave_height; current/forecast
    wavePeriod: float | None = None       # group_wave_period; current/forecast
    ripCurrentRisk: str | None = None     # "low" | "moderate" | "high"
    waterTemp: float | None = None        # group_temperature
    comfortLevel: str | None = None       # "comfortable" | "cool" | "cold" | "dangerous"
    uvIndex: int | None = None
    visibility: float | None = None       # group_visibility
    windSpeed: float | None = None        # group_ocean_speed
    windDirection: float | None = None    # degrees true north
    activeAlerts: list[str]  # alert headlines relevant to beach safety


class MarineAlertSummary(BaseModel):
    """One active NWS alert on a MarineLocationSummary, tagged for tab filtering.

    alertType classifies the alert's NWS `event` string into a dashboard
    activity-tab bucket (API-MANUAL §16 "Marine alert types", T3.5): one of
    "marineZone" (Small Craft Advisory, Gale/Storm/Hurricane Force Wind
    Warning, Special Marine Warning), "coastalFlood" (Coastal Flood
    Watch/Warning/Advisory/Statement), or "beachHazard" (Beach Hazards
    Statement, High Surf Warning/Advisory, Rip Current Statement).  See
    endpoints/marine.py's _classify_alert_type() for the exact keyword
    match; unrecognized marine event types default to "marineZone".
    """

    model_config = ConfigDict(extra="ignore")

    headline: str    # NWS alert headline text
    alertType: str   # "marineZone" | "coastalFlood" | "beachHazard"


class MarineLocationSummary(BaseModel):
    """Summary snapshot for one marine location, used by the Now page summary card.

    API-MANUAL §16.
    """

    model_config = ConfigDict(extra="ignore")

    locationId: str    # location slug from config
    name: str           # display name
    coordinates: dict[str, float]  # {lat, lon}
    activities: list[str]           # enabled activities for this location
    currentConditions: MarineObservation | None = None  # latest buoy obs, if buoy enabled
    currentTide: dict[str, Any] | None = None  # next high/low tide {type, time, height}
    activeAlerts: list[MarineAlertSummary] | None = None  # active marine alerts, tagged by type
    surfRating: int | None = None               # current surf quality stars (1-5), if surf enabled
    beachSafetyLevel: str | None = None         # current safety level, if beach_safety enabled
    weatherCode: int | None = None              # WMO weather code for hero icon
    isDay: bool | None = None                   # day/night for icon variant
    photoUrl: str | None = None                 # DEPRECATED — photo URLs are static assets served by Caddy, not API concerns


# ---------------------------------------------------------------------------
# Marine bundles (API-MANUAL §16 "Bundle types")
#
# Bundles wrap domain-specific models with location metadata, following the
# existing ForecastBundle/AlertList pattern: `source` + `generatedAt` are
# mirrored inside the bundle; `stationClock`/`freshness`/`units` live only on
# the outer *Response envelope (added when the corresponding endpoint ships).
# ---------------------------------------------------------------------------


class MarineBundle(BaseModel):
    """MarineBundle container for GET /api/v1/marine[/{locationId}] (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    locationId: str
    locationName: str
    coordinates: dict[str, float]  # {lat, lon}
    observation: MarineObservation | None = None  # latest buoy observation, if available
    forecast: list[MarineForecastPoint] = []       # WaveWatch III timesteps
    textForecast: list[MarineTextForecast] = []    # NWS marine zone text periods
    source: str
    generatedAt: str  # UTC ISO-8601 with Z


class TideBundle(BaseModel):
    """TideBundle container for GET /api/v1/tides[/{locationId}] (API-MANUAL §16)."""

    model_config = ConfigDict(extra="ignore")

    locationId: str
    locationName: str
    coordinates: dict[str, float]  # {lat, lon}
    predictions: list[TidePrediction] = []  # CO-OPS harmonic predictions
    waterLevels: list[WaterLevel] = []       # CO-OPS observed water levels
    # Composite water level fields (ADR-091 Decision 4, T4.2)
    totalWaterLevelForecast: list[dict[str, Any]] | None = None
    currentResidual: dict[str, Any] | None = None
    residualForecastSource: str | None = None
    stormSurgeLevel: str | None = None
    source: str
    generatedAt: str  # UTC ISO-8601 with Z


# NOTE: SurfBundle/FishingBundle/BeachSafetyBundle were removed here (T4.3,
# Phase 4 cleanup). They were never referenced outside this module and had
# drifted from the actual dict shapes returned by endpoints/surf.py,
# fishing.py, and beach_safety.py (e.g. the real fishing bundle returns
# `days`/`species`/`targetCategory`/`habitatFeatures`, not `forecast`/
# `solunar`). Those endpoints intentionally return plain dicts pending a
# real *BundleResponse model — see each endpoint module's docstring.

