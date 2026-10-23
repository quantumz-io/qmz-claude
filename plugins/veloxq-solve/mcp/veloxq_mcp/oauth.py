"""Connect to VeloxQ: the OAuth 2.0 authorization code flow for a native application.

The server is a public client (no secret). It runs inside Claude Code and
identifies itself as Claude Code, with the OAuth client ID metadata document
Anthropic hosts for it: the client id is the URL of that document, which the
authorization server fetches, so no client is registered by hand. It sends
the user's browser to the VeloxQ consent
page, receives the authorization code on a loopback HTTP listener, exchanges
it with PKCE (RFC 7636) and keeps the tokens in a file under ``~/.veloxq``. ``activate`` returns an access token refreshed shortly
before it expires; the server sends it with the SDK's ``use_access_token``.
Refresh tokens rotate on every use, so the file is the single source of truth for
every process on the machine and every refresh runs under a file lock.

``python -m veloxq_mcp.oauth login`` runs the same flow from a terminal.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import html
import json
import os
import secrets
import ssl
import sys
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from veloxq_sdk.config import VeloxQAPIConfig

DEFAULT_SCOPES: tuple[str, ...] = (
    "user:read", "jobs:read", "jobs:write", "files:read", "files:write",
)
"""Scopes the server needs: account reads, jobs, files, solver timeouts."""

REFRESH_MARGIN = 120.0
"""Seconds before expiry at which an access token is refreshed."""

DEFAULT_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
"""Claude Code's client ID metadata document. The VeloxQ API accepts only the
metadata documents it pre-authorizes, Claude's and Claude Code's; the plugin
runs inside Claude Code and connects as it. ``VELOXQ_OAUTH_CLIENT_ID``
overrides it."""

CALLBACK_PATH = "/callback"
"""Path of the loopback redirect URI. The metadata document holds
``http://127.0.0.1/callback``; the port varies per run (RFC 8252 section 7.3)."""

ENDPOINTS = ("authorization_endpoint", "token_endpoint", "revocation_endpoint")

METADATA_PATH = "/.well-known/oauth-authorization-server"
TOKEN_FILE = Path(os.environ.get("VELOXQ_OAUTH_TOKEN_FILE", "~/.veloxq/oauth-tokens.json"))


class OAuthError(Exception):
    """An error answer from the authorization server (RFC 6749 section 5.2)."""

    def __init__(self, error: str, description: str = "", *,
                 status_code: int | None = None) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status_code = status_code


class NotConnectedError(Exception):
    """No usable tokens: the user has not connected, or the connection ended."""


@dataclass(frozen=True)
class TokenSet:
    """The tokens of one connection. ``expires_at`` is Unix time in seconds."""

    access_token: str
    refresh_token: str
    expires_at: float
    scope: str

    @classmethod
    def from_response(cls, payload: dict[str, Any], *, now: float | None = None) -> TokenSet:
        now = time.time() if now is None else now
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            expires_at=now + float(payload["expires_in"]),
            scope=payload.get("scope", ""),
        )

    def expires_within(self, seconds: float, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.expires_at - now <= seconds

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(self.scope.split())


class TokenStore:
    """Tokens of every connection, keyed by issuer and client id, in one JSON file.

    The file and its directory are created with owner-only permissions. Every
    write replaces the file atomically. Callers that read, refresh and write
    back hold ``locked()`` around the whole sequence so two processes cannot
    both rotate the same refresh token, which the server treats as theft.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        tmp.replace(self.path)

    def load(self, key: str) -> TokenSet | None:
        entry = self._read().get(key)
        if not entry:
            return None
        names = {f.name for f in fields(TokenSet)}
        return TokenSet(**{k: v for k, v in entry.items() if k in names})

    def save(self, key: str, tokens: TokenSet) -> None:
        data = self._read()
        data[key] = asdict(tokens)
        self._write(data)

    def delete(self, key: str) -> None:
        data = self._read()
        if key in data:
            del data[key]
            self._write(data)


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` pair for the S256 method."""
    verifier = _base64url(secrets.token_bytes(64))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _verify(setting: ssl.SSLContext | str | bool) -> ssl.SSLContext | bool:
    """The SDK's ``ssl_context`` setting as httpx's ``verify`` argument."""
    if isinstance(setting, str):
        if os.path.isdir(setting):
            return ssl.create_default_context(capath=setting)
        return ssl.create_default_context(cafile=setting)
    return setting


class _CallbackServer(HTTPServer):
    expected_state: str
    result: tuple[str, ...] | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer
    timeout = 10.0
    """Seconds a connected client may stay silent. Without it one idle
    connection (a browser preconnect, any local process) blocks the flow."""

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._reply(404, "Not found.")
            return
        query = parse_qs(parts.query)
        state = query.get("state", [None])[0]
        if state != self.server.expected_state:
            self._reply(400, "This response does not belong to the pending request.")
            return
        if "error" in query:
            description = query.get("error_description", [""])[0]
            self.server.result = ("error", query["error"][0], description)
            reason = html.escape(description or query["error"][0])
            self._reply(200, f"VeloxQ did not connect: {reason}. You can close this window.")
        elif "code" in query:
            self.server.result = ("code", query["code"][0])
            self._reply(200, "Connected to VeloxQ. You can close this window.")
        else:
            self._reply(400, "The response carries neither a code nor an error.")

    def _reply(self, status: int, text: str) -> None:
        body = (
            '<!doctype html><meta charset="utf-8"><title>VeloxQ</title>'
            f'<p style="font: 16px sans-serif; margin: 3em">{text}</p>'
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class Connection:
    """One application's connection to one VeloxQ account.

    Args:
        client_id: The client id: the URL of a client ID metadata document,
            or the id of a client registered with VeloxQ by hand.
        scopes: Scopes requested at ``authorize``.
        store: Where tokens live. Defaults to ``TOKEN_FILE``.
        config: The SDK configuration to read the API URL and TLS settings from.
    """

    def __init__(self, client_id: str, *, scopes: Iterable[str] = DEFAULT_SCOPES,
                 store: TokenStore | None = None,
                 config: VeloxQAPIConfig | None = None) -> None:
        if not client_id:
            raise ValueError("an OAuth client id is required")
        self.config = config or VeloxQAPIConfig.instance()
        self.client_id = client_id
        self.scopes = tuple(scopes)
        self.store = store or TokenStore(TOKEN_FILE)
        self._metadata: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return f"{self.config.url}|{self.client_id}"

    # -- server ---------------------------------------------------------------

    def _http(self) -> httpx.Client:
        """A client with no credential: OAuth endpoints authenticate the client, not the user."""
        return httpx.Client(
            base_url=self.config.url,
            verify=_verify(self.config.ssl_context),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def metadata(self) -> dict[str, Any]:
        """The RFC 8414 document of the server, fetched once.

        The VeloxQ API publishes its endpoints without a scheme
        (``api.veloxq.com/oauth/token``); they get the scheme of the API URL.

        Raises:
            OAuthError: An endpoint is missing or still not an absolute URL.
        """
        if self._metadata is None:
            with self._http() as http:
                response = http.get(METADATA_PATH)
                response.raise_for_status()
                document = response.json()
            scheme = urlsplit(self.config.url).scheme
            for name in ENDPOINTS:
                value = document.get(name)
                if isinstance(value, str) and value and "://" not in value:
                    value = f"{scheme}://{value}"
                parts = urlsplit(value or "")
                if parts.scheme not in ("http", "https") or not parts.netloc:
                    raise OAuthError(
                        "invalid_server_metadata",
                        f"the authorization server metadata of {self.config.url} gives "
                        f"{name}={document.get(name)!r}, which is not an absolute URL")
                document[name] = value
            self._metadata = document
        return self._metadata

    @property
    def uses_metadata_document(self) -> bool:
        """Whether the client id is the URL of a client ID metadata document."""
        return self.client_id.startswith("https://")

    @staticmethod
    def _error(response: httpx.Response, fallback: str = "") -> OAuthError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return OAuthError(
            payload.get("error", f"http_{response.status_code}"),
            payload.get("error_description", fallback),
            status_code=response.status_code,
        )

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        with self._http() as http:
            response = http.post(self.metadata()["token_endpoint"], data=form)
        if response.is_success:
            return response.json()
        raise self._error(response)

    # -- authorization code flow ---------------------------------------------

    def authorization_url(self, redirect_uri: str, *, state: str, code_challenge: str) -> str:
        query = urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
        return f'{self.metadata()["authorization_endpoint"]}?{query}'

    def _preflight(self, redirect_uri: str, *, state: str, code_challenge: str) -> None:
        """Validate the request before a browser is involved.

        A wrong client id or redirect URI is answered to the browser as a
        400 page and never reaches the loopback listener, so the flow would
        otherwise wait for the whole timeout.
        """
        if self.uses_metadata_document and not self.metadata().get(
                "client_id_metadata_document_supported"):
            raise OAuthError(
                "invalid_client",
                f"{self.config.url} does not accept client ID metadata documents, so the "
                f"client id {self.client_id} cannot be used there. Set "
                "VELOXQ_OAUTH_CLIENT_ID to a client registered with that server.")
        url = self.authorization_url(redirect_uri, state=state, code_challenge=code_challenge)
        info_url, _, query = url.replace("?", "/info?", 1).partition("?")
        with self._http() as http:
            response = http.get(f"{info_url}?{query}")
        if response.is_success:
            return
        error = self._error(response, "the authorization request was refused")
        if error.error == "invalid_client":
            raise OAuthError(
                "invalid_client",
                f"{self.config.url} refused the client id {self.client_id} "
                f"({error.description}). The VeloxQ API must allow this client ID "
                "metadata document URL.") from error
        raise error

    def exchange(self, code: str, *, redirect_uri: str, code_verifier: str) -> TokenSet:
        payload = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        })
        return TokenSet.from_response(payload)

    def authorize(self, *, open_browser: bool = True, timeout: float = 300.0,
                  on_url: Callable[[str], None] | None = None) -> TokenSet:
        """Run the consent flow and store the tokens.

        Listens on an ephemeral loopback port, sends the browser to the
        consent page and blocks until the redirect arrives or ``timeout``
        seconds pass. ``on_url`` receives the authorization URL so a caller
        can show it or open the browser itself.

        Raises:
            OAuthError: The user declined, or the server refused the request.
            TimeoutError: No redirect arrived in time.
        """
        verifier, challenge = pkce_pair()
        state = _base64url(secrets.token_bytes(32))
        server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
        server.expected_state = state
        server.timeout = 1.0
        try:
            redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
            self._preflight(redirect_uri, state=state, code_challenge=challenge)
            url = self.authorization_url(redirect_uri, state=state, code_challenge=challenge)
            if on_url is not None:
                on_url(url)
            if open_browser:
                webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while server.result is None:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"No answer from the browser after {timeout:.0f} seconds.")
                server.handle_request()
        finally:
            server.server_close()
        kind, *rest = server.result
        if kind == "error":
            raise OAuthError(rest[0], rest[1])
        tokens = self.exchange(rest[0], redirect_uri=redirect_uri, code_verifier=verifier)
        with self.store.locked():
            self.store.save(self.key, tokens)
        return tokens

    # -- tokens ---------------------------------------------------------------

    def tokens(self) -> TokenSet | None:
        return self.store.load(self.key)

    @property
    def is_connected(self) -> bool:
        return self.tokens() is not None

    def refresh(self, tokens: TokenSet) -> TokenSet:
        """Rotate ``tokens``. The caller holds the store lock and saves the result."""
        try:
            payload = self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": self.client_id,
            })
        except OAuthError as err:
            if err.error == "invalid_grant":
                self.store.delete(self.key)
                raise NotConnectedError(
                    f"The connection to VeloxQ ended ({err.description}). Connect again."
                ) from err
            raise
        return TokenSet.from_response(payload)

    def activate(self) -> str:
        """A valid access token, refreshed when close to expiry.

        Raises:
            NotConnectedError: No tokens are stored, or the refresh token died.
        """
        with self.store.locked():
            tokens = self.store.load(self.key)
            if tokens is None:
                raise NotConnectedError("Not connected to VeloxQ. Run the authorization flow first.")
            if tokens.expires_within(REFRESH_MARGIN):
                tokens = self.refresh(tokens)
                self.store.save(self.key, tokens)
        return tokens.access_token

    def disconnect(self) -> None:
        """Revoke the connection on the server and forget the tokens."""
        with self.store.locked():
            tokens = self.store.load(self.key)
            if tokens is not None:
                with self._http() as http:
                    response = http.post(
                        self.metadata()["revocation_endpoint"],
                        data={"token": tokens.refresh_token, "client_id": self.client_id},
                    )
                    response.raise_for_status()
            self.store.delete(self.key)


# -- command line -------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m veloxq_mcp.oauth",
        description="Connect this machine to a VeloxQ account with OAuth 2.0.",
    )
    parser.add_argument("--client-id",
                        default=os.environ.get("VELOXQ_OAUTH_CLIENT_ID") or DEFAULT_CLIENT_ID,
                        help="client id, by default the plugin's client ID metadata document "
                             "URL (VELOXQ_OAUTH_CLIENT_ID)")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="open the consent page and store the tokens")
    login.add_argument("--scope", nargs="+", default=list(DEFAULT_SCOPES))
    login.add_argument("--no-browser", action="store_true",
                       help="print the URL instead of opening it")
    login.add_argument("--timeout", type=float, default=300.0)
    commands.add_parser("status", help="show whether this machine is connected")
    commands.add_parser("logout", help="revoke the connection and delete the tokens")
    args = parser.parse_args(argv)

    connection = Connection(args.client_id, scopes=getattr(args, "scope", DEFAULT_SCOPES))
    url = connection.config.url
    if args.command == "login":
        tokens = connection.authorize(
            open_browser=not args.no_browser, timeout=args.timeout,
            on_url=lambda link: print(f"Open this URL to connect:\n{link}", file=sys.stderr),
        )
        print(f"connected to {url} with scopes {tokens.scope}")
    elif args.command == "status":
        tokens = connection.tokens()
        if tokens is None:
            print(f"not connected to {url}")
            sys.exit(1)
        print(f"connected to {url} with scopes {tokens.scope}")
    elif args.command == "logout":
        connection.disconnect()
        print(f"disconnected from {url}")


if __name__ == "__main__":
    main()
