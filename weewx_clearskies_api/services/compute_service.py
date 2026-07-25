"""Compute service for SwellTrack/SurfBeat offloading (T3.1).

Runs on a remote host (librewxr) and accepts wave model computation
requests from the Clear Skies API on weewx.  Three endpoints:

  POST /compute/swelltrack  — runs run_pipeline() for one timestep
  POST /compute/surfbeat    — runs run_surfbeat_strip() for one timestep
  GET  /health              — liveness probe (no auth required)

Startup::

    python -m weewx_clearskies_api.services.compute_service \\
        [--host HOST] [--port PORT] [--cert-dir DIR]

Environment:
    SURF_COMPUTE_SECRET  — Bearer token shared with the weewx API (required).
                           The service refuses to start when this variable is
                           absent or empty.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from weewx_clearskies_api.services.surf_1d_analytical import BreakPoint
from weewx_clearskies_api.services.surf_1d_pipeline import (
    PartitionBreakInfo,
    PartitionBreakResult,
    PipelineResult,
    TransectResult,
    run_pipeline,
)
from weewx_clearskies_api.services.surfbeat_runner import SurfBeatResult, run_surfbeat_strip
from weewx_clearskies_api.services.swan_formats import TransectInfo
from weewx_clearskies_api.services.transect_handoff import L2_REFERENCE_DEPTH_M
from weewx_clearskies_api.tls import compute_fingerprint

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_PORT: int = 8770
_DEFAULT_CERT_DIR: str = "/etc/weewx-clearskies/compute"

#: Absolute path to the SWAN binary on the compute host (librewxr).
_SWAN_BINARY: str = "/usr/local/bin/swan"

#: Base directory for SWAN per-run working directories.
_SWAN_WORKDIR_BASE: str = "/var/run/weewx-clearskies/swan"

#: CUDEM bathymetric profile cache on the compute host (librewxr) — SWAN
#: downloads profiles here (bfff1f7), and compute_swelltrack() reads them
#: locally rather than trusting the API host to have pre-populated them
#: over the wire. Same path/convention as providers/nearshore/swan.py's
#: _PROFILE_CACHE_DIR and endpoints/setup.py's _MARINE_PROFILE_CACHE_DIR —
#: a module-level constant here (not inlined, as it was before) so tests
#: can monkeypatch it instead of touching the real /etc directory
#: (2026-07-25 reopen: the previous inline literal made
#: tests/test_compute_service_handoff.py hit real production permissions
#: on Linux while resolving harmlessly on Windows).
_PROFILE_CACHE_DIR = Path("/etc/weewx-clearskies/spot_profiles")

# ---------------------------------------------------------------------------
# Module-level secret — set in __main__ before uvicorn starts.
# The _require_auth dependency validates every protected request against this.
# ---------------------------------------------------------------------------

_surf_compute_secret: str = ""


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class TransectData(BaseModel):
    """One cross-shore transect descriptor for a SwellTrack pipeline request."""

    model_config = ConfigDict(extra="forbid")

    index: int
    origin_lat: float
    origin_lon: float
    is_structure_affected: bool
    bathymetric_profile: list[dict[str, float]]
    # T4A.9/T4A.10 fix (2026-07-25): this hour's per-hour handoff selection,
    # computed in-process by services.transect_handoff.select_hourly_handoff()
    # (and possibly services.transect_handoff.refine_handoff_with_qb()) BEFORE
    # the request is built. Optional/nullable so an older client is
    # detectable rather than silently defaulted (see compute_swelltrack()'s
    # handling below) — the field existing with a hardcoded fallback value
    # is exactly the defect this reopened. Not reimplemented server-side;
    # the service only re-assembles what it is given, per-transect, into
    # the shape run_pipeline()'s own handoff_by_transect parameter expects.
    handoff_depth_m: float | None = None
    handoff_source_level: str | None = None

    @field_validator("bathymetric_profile")
    @classmethod
    def _validate_profile_depths(
        cls, v: list[dict[str, float]]
    ) -> list[dict[str, float]]:
        for i, point in enumerate(v):
            depth = point.get("depth_m")
            if depth is not None and depth < 0.0:
                raise ValueError(
                    f"bathymetric_profile[{i}].depth_m must be non-negative, got {depth}"
                )
        return v


class SwellTrackRequest(BaseModel):
    """Request body for POST /compute/swelltrack."""

    model_config = ConfigDict(extra="forbid")

    spot_id: str = ""
    specout_data: dict[str, Any] | None = None
    transects: list[TransectData]
    tide_level: float
    beach_facing: float
    gamma: float = 0.73
    cfjon: float = 0.038
    bulk_hs: float | None = None
    bulk_tp: float | None = None
    bulk_dir: float | None = None
    canonical_partitions: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate_transects_non_empty(self) -> "SwellTrackRequest":
        if not self.transects:
            raise ValueError("transects must be non-empty")
        return self


class SurfBeatRequest(BaseModel):
    """Request body for POST /compute/surfbeat."""

    model_config = ConfigDict(extra="forbid")

    spot_id: str
    profile: list[list[float]]
    hs: float
    tp: float
    direction: float
    cfjon: float
    wind_speed_ms: float = 0.0
    wind_direction_deg: float = 0.0

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) < 2:
            raise ValueError(
                f"profile must have >= 2 points, got {len(v)}"
            )
        for i, point in enumerate(v):
            if len(point) < 2:
                raise ValueError(
                    f"profile[{i}] must have 2 elements [dist_m, depth_m], got {len(point)}"
                )
            if point[1] < 0.0:
                raise ValueError(
                    f"profile[{i}] depth_m must be non-negative, got {point[1]}"
                )
        return v

    @field_validator("hs")
    @classmethod
    def _validate_hs(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"hs must be > 0, got {v}")
        return v

    @field_validator("tp")
    @classmethod
    def _validate_tp(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"tp must be > 0, got {v}")
        return v


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class BreakPointResponse(BaseModel):
    """Serializable mirror of surf_1d_analytical.BreakPoint."""

    model_config = ConfigDict(extra="forbid")

    distance_m: float
    depth_m: float
    hs_m: float
    breaker_type: str
    iribarren: float


class PartitionBreakResultResponse(BaseModel):
    """Serializable mirror of surf_1d_pipeline.PartitionBreakResult."""

    model_config = ConfigDict(extra="forbid")

    partition_index: int
    break_points: list[BreakPointResponse]
    face_height_m: float
    hs_at_break_m: float


class TransectResultResponse(BaseModel):
    """Serializable mirror of surf_1d_pipeline.TransectResult (arrays as lists)."""

    model_config = ConfigDict(extra="forbid")

    transect_index: int
    is_structure_affected: bool
    hs_total_profile: list[float]
    distances: list[float]
    depths: list[float]
    per_partition: list[PartitionBreakResultResponse | None]
    best_face_height_m: float
    handoff_depth_m: float
    handoff_source_level: str


class PartitionBreakInfoResponse(BaseModel):
    """Serializable mirror of surf_1d_pipeline.PartitionBreakInfo."""

    model_config = ConfigDict(extra="forbid")

    partition_index: int
    period_s: float
    direction_deg: float
    height_m: float
    classification: str
    mean_break_distance_m: float | None
    mean_face_height_m: float | None
    peak_face_height_m: float | None
    mean_break_depth_m: float | None
    dominant_breaker_type: str | None


class SwellTrackResponse(BaseModel):
    """Serializable mirror of surf_1d_pipeline.PipelineResult."""

    model_config = ConfigDict(extra="forbid")

    best_peak_face_height_m: float
    spot_average_face_height_m: float
    peel_angle_deg: float | None
    peel_classification: str | None
    peel_direction: str | None
    transect_count: int
    open_transect_count: int
    per_transect: list[TransectResultResponse]
    per_partition_breaks: list[PartitionBreakInfoResponse]
    degraded: bool


class SurfBeatResponse(BaseModel):
    """Response for POST /compute/surfbeat."""

    model_config = ConfigDict(extra="forbid")

    hs_ig_shoreline: float
    set_timing_minutes: float | None
    set_amplitude_m: float | None
    hs_sw_profile: list[dict[str, float]]
    runtime_ms: float


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Clear Skies Compute Service",
    version="1.0.0",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Authentication dependency
# ---------------------------------------------------------------------------


async def _require_auth(authorization: str | None = Header(None)) -> None:
    """Verify the Bearer token in the Authorization header.

    Raises 401 when the header is missing, malformed, or the token does not
    match SURF_COMPUTE_SECRET.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header — expected: Bearer <token>",
        )
    token = authorization[len("Bearer "):]
    if token != _surf_compute_secret:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _break_point_to_dict(bp: BreakPoint) -> dict[str, Any]:
    """Convert a BreakPoint dataclass to a JSON-serializable dict."""
    return {
        "distance_m": float(bp.distance_m),
        "depth_m": float(bp.depth_m),
        "hs_m": float(bp.hs_m),
        "breaker_type": bp.breaker_type,
        "iribarren": float(bp.iribarren),
    }


def _partition_break_result_to_dict(pbr: PartitionBreakResult) -> dict[str, Any]:
    """Convert a PartitionBreakResult dataclass to a JSON-serializable dict."""
    return {
        "partition_index": pbr.partition_index,
        "break_points": [_break_point_to_dict(bp) for bp in pbr.break_points],
        "face_height_m": float(pbr.face_height_m),
        "hs_at_break_m": float(pbr.hs_at_break_m),
    }


def _transect_result_to_dict(tr: TransectResult) -> dict[str, Any]:
    """Convert a TransectResult dataclass to a JSON-serializable dict.

    Converts numpy arrays (hs_total_profile, distances, depths) to plain
    Python lists via ndarray.tolist().
    """
    return {
        "transect_index": tr.transect_index,
        "is_structure_affected": tr.is_structure_affected,
        "hs_total_profile": tr.hs_total_profile.tolist(),
        "distances": tr.distances.tolist(),
        "depths": tr.depths.tolist(),
        "per_partition": [
            _partition_break_result_to_dict(pbr) if pbr is not None else None
            for pbr in tr.per_partition
        ],
        "best_face_height_m": float(tr.best_face_height_m),
        "handoff_depth_m": float(tr.handoff_depth_m),
        "handoff_source_level": tr.handoff_source_level,
    }


def _partition_break_info_to_dict(pbi: PartitionBreakInfo) -> dict[str, Any]:
    """Convert a PartitionBreakInfo dataclass to a JSON-serializable dict."""
    return {
        "partition_index": pbi.partition_index,
        "period_s": float(pbi.period_s),
        "direction_deg": float(pbi.direction_deg),
        "height_m": float(pbi.height_m),
        "classification": pbi.classification,
        "mean_break_distance_m": (
            float(pbi.mean_break_distance_m)
            if pbi.mean_break_distance_m is not None
            else None
        ),
        "mean_face_height_m": (
            float(pbi.mean_face_height_m)
            if pbi.mean_face_height_m is not None
            else None
        ),
        "peak_face_height_m": (
            float(pbi.peak_face_height_m)
            if pbi.peak_face_height_m is not None
            else None
        ),
        "mean_break_depth_m": (
            float(pbi.mean_break_depth_m)
            if pbi.mean_break_depth_m is not None
            else None
        ),
        "dominant_breaker_type": pbi.dominant_breaker_type,
    }


def _pipeline_result_to_dict(result: PipelineResult) -> dict[str, Any]:
    """Recursively convert a PipelineResult to a JSON-serializable dict.

    Converts all numpy arrays (hs_total_profile, distances, depths on each
    TransectResult) to plain Python lists.  All dataclass instances are
    converted to dicts.
    """
    return {
        "best_peak_face_height_m": float(result.best_peak_face_height_m),
        "spot_average_face_height_m": float(result.spot_average_face_height_m),
        "peel_angle_deg": (
            float(result.peel_angle_deg) if result.peel_angle_deg is not None else None
        ),
        "peel_classification": result.peel_classification,
        "peel_direction": result.peel_direction,
        "transect_count": result.transect_count,
        "open_transect_count": result.open_transect_count,
        "per_transect": [_transect_result_to_dict(tr) for tr in result.per_transect],
        "per_partition_breaks": [
            _partition_break_info_to_dict(pbi) for pbi in result.per_partition_breaks
        ],
        "degraded": result.degraded,
    }


def _surfbeat_result_to_response(result: SurfBeatResult) -> SurfBeatResponse:
    """Convert a SurfBeatResult dataclass to a SurfBeatResponse model."""
    return SurfBeatResponse(
        hs_ig_shoreline=result.hs_ig_shoreline,
        set_timing_minutes=result.set_timing_minutes,
        set_amplitude_m=result.set_amplitude_m,
        hs_sw_profile=[
            {"distance_m": float(p["distance_m"]), "hs_m": float(p["hs_m"])}
            for p in result.hs_sw_profile
        ],
        runtime_ms=result.runtime_ms,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=dict[str, str])
async def health() -> dict[str, str]:
    """Liveness probe — no authentication required."""
    return {"status": "ok", "version": "1.0.0"}


@app.post("/compute/swelltrack", response_model=SwellTrackResponse)
def compute_swelltrack(
    request: SwellTrackRequest,
    _auth: None = Depends(_require_auth),
) -> SwellTrackResponse:
    """Run the SwellTrack per-partition pipeline for one forecast timestep.

    Converts the JSON request body to TransectInfo objects, calls run_pipeline(),
    converts all numpy arrays in the result to Python lists, and returns the
    serialized PipelineResult.
    """
    # Load CUDEM bathymetric profile from local cache when transects arrive
    # with empty profiles (remote-mode: API on weewx doesn't have profiles,
    # but they exist here on the compute host at /etc/weewx-clearskies/spot_profiles/).
    _local_profile: list[dict[str, float]] = []
    if request.spot_id:
        _profile_path = _PROFILE_CACHE_DIR / f"{request.spot_id}.json"
        if _profile_path.exists():
            try:
                _cached = json.loads(_profile_path.read_text(encoding="utf-8"))
                _pts = _cached.get("profile", [])
                _local_profile = sorted(
                    [p for p in _pts if p.get("depth_m") is not None and p.get("depth_m", 0) > 0],
                    key=lambda p: p["distance_m"],
                )
            except Exception:
                logger.debug("compute_swelltrack: failed to load CUDEM profile for %s", request.spot_id)

    # T4A.9/T4A.10 fix (2026-07-25, reopened): compute offloading is the LIVE
    # path in production (surf_compute_host is configured on both weewx and
    # librewxr) -- a hardcoded handoff_depth_m here silently discarded every
    # per-hour selection computed in-process and replaced it with a constant,
    # the exact frozen-depth defect T4A.9 exists to remove. Fail LOUDLY (not
    # silently) when a transect's per-hour selection is missing from the
    # request -- an older client, or a malformed payload -- rather than
    # hiding it behind a fallback that looks like a real value.
    transects: list[TransectInfo] = []
    handoff_by_transect: dict[int, tuple[float, str]] = {}
    for i, t in enumerate(request.transects):
        if t.handoff_depth_m is None or t.handoff_source_level is None:
            logger.error(
                "compute_swelltrack: spot=%r transect index=%d missing "
                "handoff_depth_m/handoff_source_level in request (client did "
                "not send the T4A.9 per-hour selection -- stale client or "
                "malformed payload) -- falling back to the L2 reference "
                "depth (%.1f m) for this transect only, NOT a silent 10.0.",
                request.spot_id, t.index, L2_REFERENCE_DEPTH_M,
            )
            handoff_depth = L2_REFERENCE_DEPTH_M
            handoff_source = "L2"
        else:
            handoff_depth = t.handoff_depth_m
            handoff_source = t.handoff_source_level
        handoff_by_transect[i] = (handoff_depth, handoff_source)
        transects.append(TransectInfo(
            index=t.index,
            origin_lat=t.origin_lat,
            origin_lon=t.origin_lon,
            bearing_deg=0.0,
            handoff_depth_m=handoff_depth,
            is_structure_affected=t.is_structure_affected,
            bathymetric_profile=list(t.bathymetric_profile) if t.bathymetric_profile else _local_profile,
        ))

    try:
        result = run_pipeline(
            specout_data=request.specout_data,
            transects=transects,
            tide_level=request.tide_level,
            beach_facing=request.beach_facing,
            gamma=request.gamma,
            cfjon=request.cfjon,
            bulk_hs=request.bulk_hs,
            bulk_tp=request.bulk_tp,
            bulk_dir=request.bulk_dir,
            canonical_partitions=request.canonical_partitions,
            handoff_by_transect=handoff_by_transect,
        )
    except Exception as exc:
        logger.error(
            "compute_swelltrack: run_pipeline() raised an unexpected exception",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Computation failed: {str(exc)[:200]}",
        ) from exc

    result_dict = _pipeline_result_to_dict(result)
    return SwellTrackResponse.model_validate(result_dict)


@app.post("/compute/surfbeat", response_model=SurfBeatResponse)
def compute_surfbeat(
    request: SurfBeatRequest,
    _auth: None = Depends(_require_auth),
) -> SurfBeatResponse:
    """Run a SurfBeat strip computation for one forecast timestep.

    Converts the profile list to a numpy Nx2 array, calls run_surfbeat_strip(),
    and returns the IG wave statistics.
    """
    profile_array = np.array(request.profile, dtype=float)

    try:
        result: SurfBeatResult = run_surfbeat_strip(
            spot_id=request.spot_id,
            profile=profile_array,
            hs=request.hs,
            tp=request.tp,
            direction=request.direction,
            cfjon=request.cfjon,
            swan_binary=_SWAN_BINARY,
            workdir_base=_SWAN_WORKDIR_BASE,
            wind_speed_ms=request.wind_speed_ms,
            wind_direction_deg=request.wind_direction_deg,
        )
    except Exception as exc:
        logger.error(
            "compute_surfbeat: run_surfbeat_strip() raised an unexpected exception",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Computation failed: {str(exc)[:200]}",
        ) from exc

    return _surfbeat_result_to_response(result)


# ---------------------------------------------------------------------------
# TLS certificate management
# ---------------------------------------------------------------------------


def _ensure_compute_tls(
    cert_dir: Path,
    hostnames: list[str] | None = None,
) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a self-signed Ed25519 cert if needed.

    Checks for cert.pem and key.pem in *cert_dir*.  When both exist they are
    returned immediately.  When either is absent, a self-signed Ed25519
    certificate with 10-year validity is generated and written to those paths,
    then (cert_path, key_path) is returned.

    Args:
        cert_dir: Directory in which to find or create cert.pem and key.pem.
        hostnames: Additional hostnames and/or IP addresses to include in the
            cert's Subject Alternative Names.  Each entry is parsed as an IP
            (v4 or v6) first; if that fails, it is added as a DNS name.

    Returns:
        Tuple of (cert_path, key_path) as Path objects.
    """
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    if cert_path.exists() and key_path.exists():
        logger.info("Using existing TLS cert at %s", cert_path)
        return cert_path, key_path

    logger.info(
        "No TLS cert found at %s — generating self-signed Ed25519 cert (10-year validity).",
        cert_dir,
    )

    cert_dir.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()

    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "clearskies-compute")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    for name in hostnames or []:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san_entries.append(x509.DNSName(name))
    san = x509.SubjectAlternativeName(san_entries)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(private_key, algorithm=None)
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # Restrict key file permissions — silently ignored on Windows.
    try:
        key_path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass

    logger.info("Self-signed TLS cert written to %s", cert_path)
    return cert_path, key_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Clear Skies Compute Service (SwellTrack/SurfBeat offloading)",
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Bind address (default: {_DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Bind port (default: {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--cert-dir",
        default=_DEFAULT_CERT_DIR,
        help=f"Directory for TLS cert.pem / key.pem (default: {_DEFAULT_CERT_DIR})",
    )
    parser.add_argument(
        "--hostname",
        action="append",
        default=[],
        help=(
            "Additional hostname or IP for the TLS cert SAN "
            "(repeatable, e.g. --hostname librewxr.example.com --hostname 192.168.7.22)"
        ),
    )
    args = parser.parse_args()

    # Step 1: Validate SURF_COMPUTE_SECRET before anything else.
    _secret = os.environ.get("SURF_COMPUTE_SECRET", "").strip()
    if not _secret:
        logging.basicConfig(level=logging.CRITICAL)
        logging.critical(
            "FATAL: SURF_COMPUTE_SECRET environment variable is not set or empty. "
            "Set it to a strong random token before starting the compute service."
        )
        sys.exit(1)

    # Step 2: Configure logging.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Step 3: Wire the secret into the module-level variable used by _require_auth.
    _surf_compute_secret = _secret

    # Step 4: Generate TLS cert if needed.
    _cert_dir = Path(args.cert_dir)
    _cert_path, _key_path = _ensure_compute_tls(
        _cert_dir, hostnames=args.hostname or None
    )

    # Step 5: Log cert fingerprint so the operator can pin it on the weewx client.
    _fingerprint = compute_fingerprint(_cert_path)
    logger.info(
        "Compute service TLS fingerprint: %s  (pin this value on the weewx client)",
        _fingerprint,
    )

    # Step 6: Start uvicorn with TLS.
    logger.info(
        "Starting Clear Skies compute service on https://%s:%d", args.host, args.port
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=str(_cert_path),
        ssl_keyfile=str(_key_path),
        log_level="info",
        access_log=False,
    )
