"""Trust token and setup session management for the Clear Skies API.

Handles the one-time trust token used to pair the config UI with the API on
first setup (ADR-038 §secure channel).  Token lives in secrets.env alongside
other runtime secrets; it is consumed only when setup is fully committed via
mark_setup_complete(), not at handshake time.

Secrets file I/O is direct (not via env vars) because this module manages the
first-start state before the process manager has had a chance to write the env.
All other secrets come from env vars per ADR-027 §3.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_KEY = "WEEWX_CLEARSKIES_SETUP_TOKEN"
_COMPLETE_KEY = "WEEWX_CLEARSKIES_SETUP_COMPLETE"


def _read_secrets_env(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file.  Blank lines and # comments are skipped."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _write_secrets_env(path: Path, data: dict[str, str]) -> None:
    """Write a KEY=VALUE file.  Attempts chmod 0o600 (silently skipped on Windows)."""
    lines = [f"{k}={v}\n" for k, v in data.items()]
    path.write_text("".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


class TrustManager:
    """Manages the one-time trust token and the short-lived setup session.

    Token lifecycle:
      - Not complete + no token in file → generate token_hex(32), write to file.
      - Not complete + token in file    → use existing token.
      - Complete (SETUP_COMPLETE=1)     → no token, no session possible.

    Session lifecycle:
      - create_session() validates the token (constant-time) and returns a new
        session_id (token_hex(32)).  The token is NOT consumed here — it survives
        so that a partial or failed apply can be retried without a new token.
      - validate_session() checks session_id against the active session.
      - mark_setup_complete() consumes the token (removes from secrets.env),
        writes SETUP_COMPLETE=1, and clears the session.
    """

    def __init__(self, secrets_path: Path) -> None:
        self._secrets_path = secrets_path
        self._session_id: str | None = None
        self._session_data: dict[str, Any] = {}

        env = _read_secrets_env(secrets_path)
        self._setup_complete = env.get(_COMPLETE_KEY, "").strip() == "1"

        if self._setup_complete:
            self._token: str | None = None
            return

        existing_token = env.get(_TOKEN_KEY, "").strip()
        if existing_token:
            self._token = existing_token
        else:
            # First start — generate and persist the trust token.
            new_token = secrets.token_hex(32)
            env[_TOKEN_KEY] = new_token
            _write_secrets_env(secrets_path, env)
            self._token = new_token
            logger.info("Generated new setup trust token and wrote to %s", secrets_path)

    @property
    def token(self) -> str | None:
        """Current trust token.  None if setup is complete."""
        return self._token

    @property
    def setup_complete(self) -> bool:
        """Whether initial setup has been completed."""
        return self._setup_complete

    def create_session(self, token: str) -> str | None:
        """Validate the trust token and open a setup session.

        The token is NOT consumed here — it survives across multiple handshake
        attempts so that a failed apply does not lock the operator out.  The
        token is consumed only when setup is fully committed via
        mark_setup_complete().

        Returns a new session_id, or None if the token is invalid or setup is done.
        """
        if self._setup_complete or self._token is None:
            return None

        # Constant-time comparison prevents timing-oracle attacks.
        if not hmac.compare_digest(self._token.encode(), token.encode()):
            logger.warning("Setup trust token validation failed (incorrect token).")
            return None

        session_id = secrets.token_hex(32)
        self._session_id = session_id
        logger.info("Trust token validated; setup session opened (token still live).")
        return session_id

    def validate_session(self, session_id: str) -> bool:
        """Return True if session_id matches the active setup session."""
        if self._session_id is None:
            return False
        return hmac.compare_digest(self._session_id.encode(), session_id.encode())

    def get_session_data(self) -> dict[str, Any]:
        """Return the session data dict (mutable reference)."""
        return self._session_data

    def set_session_data(self, key: str, value: Any) -> None:
        """Store a value in the session data."""
        self._session_data[key] = value

    def mark_setup_complete(self) -> None:
        """Mark setup complete, persist the flag, and invalidate the token and session.

        This is the single point where the trust token is consumed — removing it
        from secrets.env so it cannot be reused after a successful apply.
        """
        env = _read_secrets_env(self._secrets_path)
        env.pop(_TOKEN_KEY, None)   # consume the trust token now that setup is done
        env[_COMPLETE_KEY] = "1"
        _write_secrets_env(self._secrets_path, env)
        self._session_id = None
        self._token = None
        self._setup_complete = True
        logger.info("Setup marked complete; trust token consumed and session cleared.")
