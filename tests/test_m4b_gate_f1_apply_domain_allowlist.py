"""Gate M4-B finding F1 (2026-08-27, lead-direct): the wizard-apply write path
only persists the known provider domains.

Pre-change (API 99d43c2): ``_write_api_conf()`` wrote ``cfg[domain]["provider"]``
for every key in ``apply.providers`` — a payload carrying ``"imagery"`` (a stale
wizard bundle after Q10-6, or a hand-crafted request) deposited an orphan
``[imagery] provider = naip`` section in api.conf (auditor's live reproduction,
scratch/gate-m4b/test_r16_e2e.py). Post-change: unknown domains are logged and
dropped; the five known domains are written exactly as before.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from weewx_clearskies_api.endpoints import setup as setup_mod
from weewx_clearskies_api.endpoints.setup import (
    _WRITABLE_PROVIDER_DOMAINS,
    ApplyRequest,
    _write_api_conf,
)


def _apply_with(providers: dict) -> ApplyRequest:
    return ApplyRequest.model_validate({
        "database": {
            "kind": "sqlite", "path": "/tmp/x.db",
            "host": "", "port": 3306, "user": "", "name": "weewx",
        },
        "providers": providers,
    })


def test_unknown_domain_is_dropped_and_known_domain_is_written(
    tmp_path: Path, caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger=setup_mod.logger.name)
    apply = _apply_with({
        "imagery": {"provider": "naip"},
        "forecast": {"provider": "openmeteo"},
    })
    _write_api_conf(tmp_path, apply)

    text = (tmp_path / "api.conf").read_text(encoding="utf-8")
    assert "[forecast]" in text
    assert "[imagery]" not in text
    assert "naip" not in text
    warned = [r for r in caplog.records if "ignoring unknown provider domain 'imagery'" in r.getMessage()]
    assert len(warned) == 1


def test_all_known_domains_still_written(tmp_path: Path) -> None:
    apply = _apply_with({d: {"provider": "x"} for d in sorted(_WRITABLE_PROVIDER_DOMAINS)})
    _write_api_conf(tmp_path, apply)
    text = (tmp_path / "api.conf").read_text(encoding="utf-8")
    for d in _WRITABLE_PROVIDER_DOMAINS:
        assert f"[{d}]" in text


def test_write_allowlist_matches_read_side_domain_tuple() -> None:
    # The read side keeps its own literal tuple inside get_current_config();
    # pin the two together so a future domain is added to both or neither.
    source = Path(setup_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r'_PROVIDER_DOMAINS = \(([^)]*)\)', source)
    assert m, "read-side _PROVIDER_DOMAINS tuple not found"
    read_side = {s.strip().strip('"\'') for s in m.group(1).split(",") if s.strip()}
    assert read_side == set(_WRITABLE_PROVIDER_DOMAINS)
