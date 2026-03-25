"""Device Authorization Flow (RFC 8628) for EurekaClaw OAuth login.

Usage
-----
    from eurekaclaw.auth.oauth import run_device_flow
    from eurekaclaw.auth.providers import get_provider

    tokens = run_device_flow(get_provider("openai-codex"))
    # tokens = {"access_token": "...", "refresh_token": "...", "expires_in": 3600, ...}
"""

from __future__ import annotations

import time
import webbrowser
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Device Authorization Flow (RFC 8628)
# ---------------------------------------------------------------------------

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _request_device_code(provider: Any) -> dict[str, Any]:
    """POST to device_code_url and return the response dict."""
    resp = httpx.post(
        provider.device_code_url,
        data={
            "client_id": provider.client_id,
            "scope": " ".join(provider.scopes),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _poll_for_token(
    token_url: str,
    client_id: str,
    device_code: str,
    interval: int,
    expires_in: int,
) -> dict[str, Any]:
    """Poll the token endpoint until the user authorizes or the code expires."""
    deadline = time.monotonic() + expires_in
    wait = interval

    while time.monotonic() < deadline:
        time.sleep(wait)
        resp = httpx.post(
            token_url,
            data={
                "grant_type": _DEVICE_GRANT,
                "client_id": client_id,
                "device_code": device_code,
            },
            timeout=30,
        )

        body = resp.json()
        error = body.get("error")

        if resp.status_code == 200 and "access_token" in body:
            return body

        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            wait += 5
            continue
        elif error == "expired_token":
            raise RuntimeError(
                "The device code expired before the user completed authorization. "
                "Please run `eurekaclaw login --provider openai-codex` again."
            )
        elif error == "access_denied":
            raise RuntimeError("Authorization was denied by the user.")
        elif error:
            raise RuntimeError(f"Token endpoint error: {error} — {body.get('error_description', '')}")
        else:
            # Unexpected non-200 without a known error field
            resp.raise_for_status()

    raise RuntimeError(
        "Device authorization timed out. Please run `eurekaclaw login --provider openai-codex` again."
    )


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def refresh_tokens(
    token_url: str,
    client_id: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Exchange a refresh token for a new access token."""
    resp = httpx.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_device_flow(provider: Any) -> dict[str, Any]:
    """Run the Device Authorization Flow for *provider* (RFC 8628).

    1. Requests a device code from the provider.
    2. Prints the verification URL + user code and opens the browser.
    3. Polls until the user completes authorization.
    4. Returns the token dict.

    Raises:
        RuntimeError: on authorization failure, denial, or timeout.
    """
    from rich.console import Console

    console = Console()

    console.print(f"\n[bold]Authenticating with {provider.label}[/bold]")

    device_resp = _request_device_code(provider)

    # RFC 8628 field names (some providers use verification_url instead of verification_uri)
    verification_uri = device_resp.get("verification_uri") or device_resp.get("verification_url", "")
    verification_uri_complete = device_resp.get("verification_uri_complete") or verification_uri
    user_code = device_resp.get("user_code", "")
    device_code = device_resp["device_code"]
    interval = int(device_resp.get("interval", 5))
    expires_in = int(device_resp.get("expires_in", 300))

    console.print("\n[bold]Open this URL in your browser and enter the code shown:[/bold]")
    console.print(f"  [cyan]{verification_uri_complete}[/cyan]")
    if user_code:
        console.print(f"\n  User code: [bold yellow]{user_code}[/bold yellow]")
    console.print()

    webbrowser.open(verification_uri_complete)

    console.print("[dim]Waiting for you to complete authorization in the browser…[/dim]")

    tokens = _poll_for_token(
        token_url=provider.token_url,
        client_id=provider.client_id,
        device_code=device_code,
        interval=interval,
        expires_in=expires_in,
    )

    console.print("[green]✓ Authorization complete.[/green]")
    return tokens
