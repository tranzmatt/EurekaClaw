"""Persistent token storage for OAuth credentials.

Tokens are stored in ~/.eurekaclaw/credentials/<provider>.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _creds_dir() -> Path:
    from eurekaclaw.config import settings
    d = settings.eurekaclaw_dir / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _creds_path(provider: str) -> Path:
    return _creds_dir() / f"{provider}.json"


def save_tokens(provider: str, tokens: dict[str, Any]) -> None:
    """Persist *tokens* for *provider* to disk."""
    path = _creds_path(provider)
    # Record when they were saved so we can detect expiry
    tokens.setdefault("saved_at", int(time.time()))
    path.write_text(json.dumps(tokens, indent=2))
    path.chmod(0o600)


def load_tokens(provider: str) -> dict[str, Any] | None:
    """Load stored tokens for *provider*, or return None if not found."""
    path = _creds_path(provider)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def delete_tokens(provider: str) -> None:
    """Remove stored tokens for *provider*."""
    _creds_path(provider).unlink(missing_ok=True)


def is_token_expired(tokens: dict[str, Any], buffer_seconds: int = 60) -> bool:
    """Return True if the access token has expired (or will within *buffer_seconds*)."""
    expires_in = tokens.get("expires_in")
    saved_at = tokens.get("saved_at", 0)
    if expires_in is None:
        return False  # no expiry info — assume valid
    return (saved_at + int(expires_in) - buffer_seconds) < int(time.time())
