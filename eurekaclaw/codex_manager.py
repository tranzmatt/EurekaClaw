"""OpenAI Codex OAuth credential management.

Allows users with an OpenAI Codex subscription to run EurekaClaw without
a separate API key, by using EurekaClaw's built-in OAuth login:

    eurekaclaw login --provider openai-codex   # one-time browser login

Tokens are stored in ~/.eurekaclaw/credentials/openai-codex.json and
refreshed automatically.  No external proxy is needed — OpenAI's API
accepts Bearer tokens directly via the OpenAI-compatible adapter.

Mirrors the role of ccproxy_manager.py for Anthropic OAuth.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROVIDER = "openai-codex"


# =============================================================================
# Token retrieval & refresh
# =============================================================================


def _load_valid_tokens() -> dict | None:
    """Return stored tokens for openai-codex, refreshing if expired.

    Returns None if no credentials are stored.
    Raises RuntimeError if refresh fails.
    """
    from eurekaclaw.auth.token_store import (
        is_token_expired,
        load_tokens,
        save_tokens,
    )

    tokens = load_tokens(_PROVIDER)
    if tokens is None:
        return None

    if is_token_expired(tokens):
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            # No refresh token — credentials must be re-obtained
            return None

        logger.debug("Codex access token expired; refreshing…")
        from eurekaclaw.auth.oauth import refresh_tokens
        from eurekaclaw.auth.providers import get_provider

        provider = get_provider(_PROVIDER)
        try:
            new_tokens = refresh_tokens(
                token_url=provider.token_url,
                client_id=provider.client_id,
                refresh_token=refresh_token,
            )
            # Carry forward the refresh token if the new response omits it
            new_tokens.setdefault("refresh_token", refresh_token)
            save_tokens(_PROVIDER, new_tokens)
            tokens = new_tokens
        except Exception as exc:
            raise RuntimeError(
                f"Failed to refresh OpenAI Codex token: {exc}\n"
                "Re-authenticate with: eurekaclaw login --provider openai-codex"
            ) from exc

    return tokens


# =============================================================================
# Environment setup
# =============================================================================


def setup_codex_env(access_token: str) -> None:
    """Inject the Codex access token into the environment.

    ``openai.AsyncOpenAI`` reads ``OPENAI_COMPAT_API_KEY`` (and the base URL)
    so the OpenAICompatAdapter picks it up without any code changes.
    """
    os.environ["OPENAI_COMPAT_API_KEY"] = access_token


# =============================================================================
# High-level entry point  (mirrors maybe_start_ccproxy)
# =============================================================================


def maybe_setup_codex_auth() -> None:
    """Conditionally inject Codex OAuth credentials based on EurekaClaw settings.

    Reads ``settings.codex_auth_mode``.  When auth mode is ``"oauth"``:
    - Loads (and refreshes if needed) the stored token
    - Sets ``OPENAI_COMPAT_API_KEY`` in the process environment

    Returns None (no subprocess to manage — OpenAI accepts Bearer tokens directly).

    Raises:
        RuntimeError: If ``CODEX_AUTH_MODE=oauth`` but no credentials are stored.
    """
    from eurekaclaw.config import settings

    if settings.codex_auth_mode != "oauth":
        return

    tokens = _load_valid_tokens()
    if not tokens:
        raise RuntimeError(
            "CODEX_AUTH_MODE=oauth but no OpenAI Codex credentials found.\n"
            "Log in first with: eurekaclaw login --provider openai-codex"
        )

    access_token = tokens.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "Stored OpenAI Codex credentials are missing an access_token.\n"
            "Re-authenticate with: eurekaclaw login --provider openai-codex"
        )

    setup_codex_env(access_token)
    logger.info("OpenAI Codex OAuth credentials loaded.")
