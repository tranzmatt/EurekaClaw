"""PKCE OAuth 2.0 flow with a local loopback callback server.

Usage
-----
    from eurekaclaw.auth.oauth import run_pkce_flow
    from eurekaclaw.auth.providers import get_provider

    tokens = run_pkce_flow(get_provider("openai-codex"))
    # tokens = {"access_token": "...", "refresh_token": "...", "expires_in": 3600, ...}
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the single redirect from the OAuth provider."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            self.server._auth_code = params["code"][0]  # type: ignore[attr-defined]
            self.server._auth_error = None  # type: ignore[attr-defined]
            body = b"<html><body><h2>Authentication successful!</h2><p>You can close this tab and return to EurekaClaw.</p></body></html>"
        else:
            error = params.get("error", ["unknown"])[0]
            self.server._auth_code = None  # type: ignore[attr-defined]
            self.server._auth_error = error  # type: ignore[attr-defined]
            body = f"<html><body><h2>Authentication failed: {error}</h2></body></html>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # suppress access logs
        pass


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_callback(port: int, timeout: int = 120) -> tuple[str | None, str | None]:
    """Start the callback server and block until a code or error arrives.

    Returns:
        ``(auth_code, error)`` — exactly one will be non-None.
    """
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server._auth_code = None  # type: ignore[attr-defined]
    server._auth_error = None  # type: ignore[attr-defined]

    deadline = time.monotonic() + timeout
    server.timeout = 1.0  # check every second

    while time.monotonic() < deadline:
        server.handle_request()
        if server._auth_code is not None or server._auth_error is not None:  # type: ignore[attr-defined]
            server.server_close()
            return server._auth_code, server._auth_error  # type: ignore[attr-defined]

    server.server_close()
    return None, "timed_out"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def _exchange_code(
    token_url: str,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    resp = httpx.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


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


def run_pkce_flow(provider: Any) -> dict[str, Any]:
    """Run the full PKCE OAuth flow for *provider*.

    Opens the user's browser, waits for the callback, exchanges the code
    for tokens, and returns the token dict.

    Raises:
        RuntimeError: on authentication failure or timeout.
    """
    from rich.console import Console

    console = Console()

    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(provider.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = provider.auth_url + "?" + urllib.parse.urlencode(params)

    console.print(f"\n[bold]Authenticating with {provider.label}[/bold]")
    console.print("\nOpening your browser. If it does not open automatically, visit:")
    console.print(f"[cyan]{auth_url}[/cyan]\n")

    # Open browser in a background thread so the server starts first
    threading.Timer(0.5, webbrowser.open, args=[auth_url]).start()

    console.print("[dim]Waiting for authentication… (timeout: 2 min)[/dim]")
    auth_code, error = _wait_for_callback(port, timeout=120)

    if error:
        raise RuntimeError(f"OAuth authentication failed: {error}")
    if not auth_code:
        raise RuntimeError("OAuth authentication timed out. Please try again.")

    console.print("[green]✓ Browser authentication complete. Exchanging tokens…[/green]")

    tokens = _exchange_code(
        token_url=provider.token_url,
        client_id=provider.client_id,
        code=auth_code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )
    return tokens
