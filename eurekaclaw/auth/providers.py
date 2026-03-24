"""OAuth provider configurations for EurekaClaw login."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    # device_code_url: RFC 8628 device authorization endpoint
    device_code_url: str
    token_url: str
    client_id: str
    scopes: list[str]
    # Human-readable label shown during login
    label: str


# ---------------------------------------------------------------------------
# Registered providers
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, OAuthProvider] = {
    "openai-codex": OAuthProvider(
        name="openai-codex",
        device_code_url="https://auth.openai.com/oauth/device/code",
        token_url="https://auth.openai.com/oauth/token",
        client_id="app_eurekaclaw",
        scopes=["openid", "profile", "email", "openai.api"],
        label="OpenAI Codex",
    ),
}


def get_provider(name: str) -> OAuthProvider:
    """Return the OAuthProvider for *name*, or raise ValueError."""
    if name not in _PROVIDERS:
        available = ", ".join(_PROVIDERS)
        raise ValueError(
            f"Unknown provider {name!r}. Available: {available}"
        )
    return _PROVIDERS[name]


def list_providers() -> list[str]:
    return list(_PROVIDERS)
