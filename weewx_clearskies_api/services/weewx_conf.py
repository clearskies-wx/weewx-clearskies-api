"""Shared weewx.conf ConfigObj cache (3a-2).

A single module owns the configobj parse of weewx.conf so that station.py
and units.py both consume the same object without re-parsing the file twice.

services/units.py was the original owner of the parse; this module extracts
the cache so both consumers can call get_weewx_conf() after load_weewx_conf()
has run at startup.

Failure modes:
  - File missing / unreadable / unparseable → WeewxConfLoadError raised by
    load_weewx_conf(); __main__.py catches it and exits non-zero.
"""

from __future__ import annotations

import logging
from pathlib import Path

import configobj

logger = logging.getLogger(__name__)


class WeewxConfLoadError(FileNotFoundError):
    """Raised when weewx.conf cannot be loaded or parsed at startup."""


# Module-level cache: set by load_weewx_conf(), consumed by get_weewx_conf().
_cached_cfg: configobj.ConfigObj | None = None
_cached_path: Path | None = None


def load_weewx_conf(weewx_conf_path: str | Path) -> configobj.ConfigObj:
    """Load and cache weewx.conf.  Called once at startup.

    Idempotent — subsequent calls with the same path return the cached object
    without re-parsing.

    Args:
        weewx_conf_path: Path to weewx.conf.

    Returns:
        The parsed ConfigObj.

    Raises:
        WeewxConfLoadError: File not found, unreadable, or parse failure.
    """
    global _cached_cfg, _cached_path  # noqa: PLW0603

    path = Path(weewx_conf_path)

    if _cached_cfg is not None and _cached_path == path:
        return _cached_cfg

    if not path.exists():
        raise WeewxConfLoadError(
            f"FATAL: weewx.conf not found at {path}. "
            "Set [weewx] config_path in api.conf to the correct path, "
            "or ensure weewx.conf exists at the default location /etc/weewx/weewx.conf."
        )

    try:
        cfg = configobj.ConfigObj(str(path), interpolation=False)
    except configobj.ConfigObjError as exc:
        raise WeewxConfLoadError(
            f"FATAL: weewx.conf at {path} could not be parsed: {exc}. "
            "Check the file is valid INI/configobj format."
        ) from exc
    except OSError as exc:
        raise WeewxConfLoadError(
            f"FATAL: Cannot read weewx.conf at {path}: {exc}. "
            "Check file permissions (readable by the clearskies-api process)."
        ) from exc

    logger.info("weewx.conf loaded", extra={"path": str(path)})
    _cached_cfg = cfg
    _cached_path = path
    return cfg


def get_weewx_conf() -> configobj.ConfigObj:
    """Return the cached ConfigObj.

    Raises:
        RuntimeError: load_weewx_conf() has not been called yet (startup bug).
    """
    if _cached_cfg is None:
        raise RuntimeError(
            "weewx.conf has not been loaded. "
            "Call load_weewx_conf() at startup before serving requests."
        )
    return _cached_cfg


def reset_cache() -> None:
    """Reset the module-level cache.  Used in tests only."""
    global _cached_cfg, _cached_path  # noqa: PLW0603
    _cached_cfg = None
    _cached_path = None
