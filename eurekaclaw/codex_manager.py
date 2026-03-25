"""OpenAI Codex credential management.

Reads credentials stored by the official OpenAI Codex CLI
(https://github.com/openai/codex) after the user runs::

    codex auth login      # one-time browser login via the Codex CLI

Credentials are read from ``~/.codex/auth.json`` and optionally copied into
``~/.eurekaclaw/credentials/openai-codex.json`` for management.

This mirrors the role of ccproxy_manager.py for Anthropic OAuth — EurekaClaw
does NOT initiate its own OAuth flow; it piggybacks on the Codex CLI's login,
exactly as it piggybacks on Claude Code's login for Anthropic OAuth.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROVIDER = "openai-codex"
_CODEX_CLI_AUTH_PATH = Path.home() / ".codex" / "auth.json"


# =============================================================================
# Read Codex CLI credentials
# =============================================================================


def _read_codex_cli_tokens() -> dict[str, Any] | None:
    """Read the access token stored by the official Codex CLI.

    Supports the ``~/.codex/auth.json`` format::

        {
          "auth_mode": "chatgpt",
          "tokens": {
            "access_token": "sk-...",
            "refresh_token": "...",
            "id_token": "eyJ..."
          },
          "last_refresh": "2026-..."
        }

    Returns a flat token dict with ``access_token`` / ``refresh_token``,
    or None if the file is absent or malformed.
    """
    if not _CODEX_CLI_AUTH_PATH.exists():
        return None
    try:
        raw = json.loads(_CODEX_CLI_AUTH_PATH.read_text())
    except Exception:
        return None

    tokens = raw.get("tokens", {})
    if not tokens.get("access_token"):
        return None

    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "id_token": tokens.get("id_token", ""),
        "account_id": tokens.get("account_id", ""),
        "auth_mode": raw.get("auth_mode", "chatgpt"),
        "last_refresh": raw.get("last_refresh", ""),
        # No expires_in in Codex CLI format — token_store.is_token_expired
        # will return False when expires_in is absent, which is correct here
        # since the Codex CLI refreshes tokens on its own expiry schedule.
    }


# =============================================================================
# Token retrieval (EurekaClaw store → Codex CLI file)
# =============================================================================


def _load_valid_tokens() -> dict[str, Any] | None:
    """Return a valid access token, trying in order:

    1. EurekaClaw's own credential store (``~/.eurekaclaw/credentials/openai-codex.json``)
       — populated by ``eurekaclaw login --provider openai-codex``
    2. The Codex CLI's credential file (``~/.codex/auth.json``)

    Returns None if no credentials are found anywhere.
    """
    from eurekaclaw.auth.token_store import (
        is_token_expired,
        load_tokens,
        save_tokens,
    )

    # 1 — Try EurekaClaw's own store first
    tokens = load_tokens(_PROVIDER)
    if tokens and not is_token_expired(tokens):
        return tokens

    # 2 — Fall back to the Codex CLI's credential file
    codex_tokens = _read_codex_cli_tokens()
    if codex_tokens:
        # Cache into EurekaClaw's store so future runs skip the file read
        save_tokens(_PROVIDER, codex_tokens)
        return codex_tokens

    return None


# =============================================================================
# Environment setup
# =============================================================================


def setup_codex_env(access_token: str) -> None:
    """Inject the Codex access token into the environment.

    The OpenAICompatAdapter reads ``OPENAI_COMPAT_API_KEY`` (and the base URL),
    so no code changes are needed in the adapter itself.
    """
    os.environ["OPENAI_COMPAT_API_KEY"] = access_token


# =============================================================================
# High-level entry point  (mirrors maybe_start_ccproxy)
# =============================================================================


def maybe_setup_codex_auth() -> None:
    """Conditionally inject Codex OAuth credentials based on EurekaClaw settings.

    Reads ``settings.codex_auth_mode``.  When auth mode is ``"oauth"``:

    - Loads credentials from EurekaClaw's store or ``~/.codex/auth.json``
    - Sets ``OPENAI_COMPAT_API_KEY`` in the process environment

    Raises:
        RuntimeError: If ``CODEX_AUTH_MODE=oauth`` but no credentials are found.
    """
    from eurekaclaw.config import settings

    if settings.codex_auth_mode != "oauth":
        return

    tokens = _load_valid_tokens()
    if not tokens:
        raise RuntimeError(
            "CODEX_AUTH_MODE=oauth but no OpenAI Codex credentials found.\n"
            "Log in with the Codex CLI first:\n"
            "  npm install -g @openai/codex\n"
            "  codex auth login\n"
            "Or run: eurekaclaw login --provider openai-codex  (if already logged in)"
        )

    access_token = tokens.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "Stored OpenAI Codex credentials are missing an access_token.\n"
            "Re-authenticate with the Codex CLI: codex auth login"
        )

    setup_codex_env(access_token)
    logger.info("OpenAI Codex OAuth credentials loaded.")
