"""The hosted VeloxQ MCP server: the tools over streamable HTTP for remote clients.

claude.ai and other remote MCP clients run the OAuth 2.0 authorization code
flow against the VeloxQ API themselves and send the user's access token with
every request. This server is the OAuth resource: it publishes protected
resource metadata (RFC 9728) naming the API as the authorization server,
checks each bearer token with the API and makes every API call of the
request with that token.

    python -m veloxq_mcp serve --public-url https://mcp.example.com/mcp
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from typing import Any, Collection
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from veloxq_sdk.api.core.http import ClientMixin, use_access_token

from . import server

log = logging.getLogger("veloxq_mcp")

DEFAULT_MAX_WAIT = 60
"""Default ``get_job`` wait cap in seconds. A remote client gives up on a tool
call it has waited on too long; the agent repeats ``get_job`` instead."""

VERIFY_TTL = 60.0
"""Seconds the API's answer about a token is reused. Every tool call still
sends the token to the API, which rejects a revoked one."""

MAX_CACHED_TOKENS = 10_000

VERIFY_THREADS = 8
"""Worker threads for token checks, apart from the ones tools run in, so
busy tools never hold up authentication."""

MAX_TOKEN_LENGTH = 8192


def _claims(token: str) -> dict[str, Any] | None:
    """The payload of a JWT, read without checking its signature.

    Only used after the API accepted the token, which checks the signature.
    """
    if len(token) > MAX_TOKEN_LENGTH or not token.isascii() or not token.isprintable():
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, RecursionError):
        return None
    return claims if isinstance(claims, dict) else None


class VeloxQTokenVerifier:
    """Accept a bearer token when the VeloxQ API accepts it.

    The API's access tokens are JWTs signed with a key only the API holds,
    so the API itself is asked, with the SDK client and its configuration.
    Tokens without a ``scope`` claim are first-party session tokens of the
    VeloxQ web app and are refused. With ``client_ids`` only tokens issued
    to one of those OAuth clients are accepted; by default any client is,
    since the API has already consented the user to it.
    """

    def __init__(self, client_ids: Collection[str] | None = None) -> None:
        self.client_ids = frozenset(client_ids or ())
        self._accepted: dict[str, tuple[float, AccessToken]] = {}
        self._refused: dict[str, float] = {}
        """Separate from ``_accepted`` so that forged tokens, which anyone can
        mint, never push out the tokens of real users."""
        self._limiter = None

    async def verify_token(self, token: str) -> AccessToken | None:
        import anyio
        import anyio.to_thread

        now = time.time()
        accepted = self._accepted.get(token)
        if accepted is not None and accepted[0] > now:
            return accepted[1]
        if self._refused.get(token, 0.0) > now:
            return None
        claims = _claims(token)
        client_id = claims.get("client_id") if claims else None
        if (not isinstance(client_id, str) or not client_id
                or (self.client_ids and client_id not in self.client_ids)
                or not isinstance(claims.get("scope"), str)
                or not isinstance(claims.get("exp"), int) or claims["exp"] <= now
                or not claims.get("sub")):
            return None
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(VERIFY_THREADS)
        until = min(now + VERIFY_TTL, claims["exp"])
        if not await anyio.to_thread.run_sync(_api_accepts, token, limiter=self._limiter):
            if len(self._refused) >= MAX_CACHED_TOKENS:
                self._refused.clear()
            self._refused[token] = until
            return None
        access = AccessToken(token=token, client_id=client_id,
                             scopes=claims["scope"].split(), expires_at=claims["exp"],
                             subject=str(claims["sub"]))
        if len(self._accepted) >= MAX_CACHED_TOKENS:
            self._accepted = {k: v for k, v in self._accepted.items() if v[0] > now}
        self._accepted[token] = (until, access)
        return access


def _api_accepts(token: str) -> bool:
    with use_access_token(token):
        response = ClientMixin.http.get("user/profile/")
    if response.status_code in (401, 403):
        return False
    response.raise_for_status()
    return True


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m veloxq_mcp serve",
        description="Serve the VeloxQ MCP tools over streamable HTTP for remote clients.")
    p.add_argument("--public-url", default=os.environ.get("VELOXQ_MCP_PUBLIC_URL"),
                   help="URL clients connect to, path included, e.g. "
                        "https://mcp.example.com/mcp (env VELOXQ_MCP_PUBLIC_URL)")
    p.add_argument("--host", default=os.environ.get("VELOXQ_MCP_HOST", "127.0.0.1"),
                   help="interface to listen on (env VELOXQ_MCP_HOST)")
    p.add_argument("--port", type=int, default=int(os.environ.get("VELOXQ_MCP_PORT", "8000")),
                   help="port to listen on (env VELOXQ_MCP_PORT)")
    p.add_argument("--client-id", action="append", dest="client_ids", default=None,
                   help="accept only tokens issued to this OAuth client id; repeatable. "
                        "By default tokens of every client the API accepts are accepted "
                        "(env VELOXQ_MCP_CLIENT_ID, comma separated)")
    p.add_argument("--issuer", default=os.environ.get("VELOXQ_OAUTH_ISSUER"),
                   help="OAuth issuer of the VeloxQ API, exactly as in its RFC 8414 "
                        "metadata; defaults to the API URL (env VELOXQ_OAUTH_ISSUER)")
    p.add_argument("--max-wait", type=int,
                   default=int(os.environ.get("VELOXQ_MCP_MAX_WAIT", DEFAULT_MAX_WAIT)),
                   help="longest get_job wait in seconds (env VELOXQ_MCP_MAX_WAIT)")
    args = p.parse_args(argv)
    if not args.public_url:
        p.error("--public-url (or VELOXQ_MCP_PUBLIC_URL) is required")
    if args.client_ids is None:
        args.client_ids = [c.strip() for c in os.environ.get("VELOXQ_MCP_CLIENT_ID", "").split(",")
                           if c.strip()]
    url = urlparse(args.public_url)
    loopback = url.hostname in ("127.0.0.1", "localhost", "::1")
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        p.error("--public-url must be https (plain http only for a loopback host)")
    if not url.path or url.path == "/":
        p.error("--public-url must include the MCP path, e.g. /mcp")
    if not 1 <= args.max_wait <= 3600:
        p.error("--max-wait must be between 1 and 3600")
    return args


def build(args: argparse.Namespace):
    """Configure the server module for hosted use and return the ASGI app."""
    server.configure()
    server.auth = server.HostedAuth()
    config = server.auth.config
    if config.token:
        raise SystemExit(
            "veloxq-mcp: an API key is configured on this host (~/.veloxq/config.py or "
            "VELOX_TOKEN). The hosted server acts only with each user's own token; remove it.")
    server.HOSTED = True
    server.MAX_WAIT = args.max_wait
    server.SUBMISSION_WINDOW = 3600.0
    settings = AuthSettings(
        issuer_url=args.issuer or config.url,
        resource_server_url=args.public_url,
        required_scopes=list(server.SCOPES),
        # The API issues tokens for itself (aud "veloxq-api") and has no
        # resource indicators, so there is no per-resource audience to check.
        validate_token_resource=False,
    )
    mcp = server.build_server(auth=settings, token_verifier=VeloxQTokenVerifier(args.client_ids))
    # Every request needs a bearer token, which a browser never sends on its
    # own, so DNS rebinding gains nothing. Behind a proxy the Host header is
    # the public one, which the loopback default would reject.
    return mcp.streamable_http_app(
        streamable_http_path=urlparse(args.public_url).path, stateless_http=True,
        host=args.host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("VELOXQ_MCP_LOG", "INFO"), stream=sys.stderr,
                        format="veloxq-mcp %(levelname)s %(message)s")
    # httpx logs every request URL at INFO, signed storage URLs included.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    args = parse_args(argv)
    app = build(args)
    import uvicorn

    log.info("serving %s for %s", args.public_url, server.auth.config.url)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)
