"""Tests for veloxq_mcp.oauth.

The authorization server is an ``httpx.MockTransport``. The browser is a
thread that performs the redirect against the loopback listener itself.
"""
import base64
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from veloxq_mcp import oauth  # noqa: E402
from veloxq_mcp.oauth import (  # noqa: E402
    DEFAULT_CLIENT_ID,
    REFRESH_MARGIN,
    Connection,
    NotConnectedError,
    OAuthError,
    TokenSet,
    TokenStore,
    pkce_pair,
)

ISSUER = "https://api.test"
CLIENT_ID = "vq_public"


def token_payload(access="at-1", refresh="rt-1", expires_in=3600):
    return {"access_token": access, "token_type": "Bearer", "expires_in": expires_in,
            "refresh_token": refresh, "scope": "jobs:read jobs:write"}


class FakeServer:
    """The OAuth endpoints of the VeloxQ API, served through MockTransport."""

    def __init__(self):
        self.token_requests = []
        self.revoke_requests = []
        self.info_requests = []
        self.token_responses = []
        self.info_response = (200, {"clientName": "app"})
        self.metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/oauth/authorize",
            "token_endpoint": f"{ISSUER}/oauth/token",
            "revocation_endpoint": f"{ISSUER}/oauth/revoke",
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
        }

    def answer(self, payload, status_code=200):
        self.token_responses.append((status_code, payload))

    def __call__(self, request):
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=self.metadata)
        if path == "/oauth/authorize/info":
            self.info_requests.append(dict(request.url.params))
            status_code, payload = self.info_response
            return httpx.Response(status_code, json=payload)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if path == "/oauth/token":
            self.token_requests.append(form)
            status_code, payload = self.token_responses.pop(0)
            return httpx.Response(status_code, json=payload)
        if path == "/oauth/revoke":
            self.revoke_requests.append(form)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"no route for {path}"})


def browser(reply):
    """An ``on_url`` callback that answers the consent page in a thread.

    ``reply`` receives the redirect URI and the authorization query and
    returns the query string the browser is sent back with.
    """

    def _open(url):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        redirect = reply(query["redirect_uri"], query)

        def _visit():
            with httpx.Client() as http:
                http.get(f'{query["redirect_uri"]}?{redirect}')

        threading.Thread(target=_visit, daemon=True).start()

    return _open


class OAuthCase(unittest.TestCase):
    def setUp(self):
        from veloxq_sdk.config import VeloxQAPIConfig

        VeloxQAPIConfig.clear_instance()
        self.config = VeloxQAPIConfig.instance()
        self.config.url = ISSUER
        self.config.token = ""
        self.server = FakeServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TokenStore(Path(self.tmp.name) / "veloxq" / "oauth-tokens.json")
        fake = self.server
        real = Connection._http
        Connection._http = lambda self: httpx.Client(
            base_url=self.config.url, transport=httpx.MockTransport(fake))
        self.addCleanup(setattr, Connection, "_http", real)
        self.connection = Connection(CLIENT_ID, store=self.store, config=self.config)


class TestTokenSet(unittest.TestCase):
    def test_from_response_and_expiry(self):
        tokens = TokenSet.from_response(token_payload(expires_in=100), now=1000.0)
        self.assertEqual(tokens.expires_at, 1100.0)
        self.assertEqual(tokens.scopes, ("jobs:read", "jobs:write"))
        self.assertFalse(tokens.expires_within(50, now=1000.0))
        self.assertTrue(tokens.expires_within(150, now=1000.0))


class TestTokenStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TokenStore(Path(self.tmp.name) / "d" / "tokens.json")

    def test_round_trip_and_permissions(self):
        tokens = TokenSet("a", "r", 1.0, "jobs:read")
        self.store.save("k", tokens)
        self.assertEqual(self.store.load("k"), tokens)
        self.assertIsNone(self.store.load("other"))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.path.parent.stat().st_mode), 0o700)

    def test_delete_keeps_other_connections(self):
        self.store.save("a", TokenSet("a", "r", 1.0, ""))
        self.store.save("b", TokenSet("b", "r", 1.0, ""))
        self.store.delete("a")
        self.store.delete("missing")
        self.assertIsNone(self.store.load("a"))
        self.assertIsNotNone(self.store.load("b"))

    def test_extra_keys_are_ignored(self):
        self.store._write({"k": {"access_token": "a", "refresh_token": "r",
                                 "expires_at": 1.0, "scope": "", "note": "x"}})
        self.assertEqual(self.store.load("k"), TokenSet("a", "r", 1.0, ""))

    def test_locked_creates_the_directory(self):
        with self.store.locked():
            pass
        self.assertTrue(self.store.path.parent.is_dir())


class TestPkce(unittest.TestCase):
    def test_pair_matches_rfc_7636(self):
        verifier, challenge = pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        digest = hashlib.sha256(verifier.encode()).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        self.assertEqual(len(challenge), 43)


class TestSetup(OAuthCase):
    def test_requires_a_client_id(self):
        with self.assertRaisesRegex(ValueError, "client id"):
            Connection("", store=self.store, config=self.config)

    def test_default_client_id_is_a_metadata_document_url(self):
        parts = urlsplit(DEFAULT_CLIENT_ID)
        self.assertEqual(parts.scheme, "https")
        self.assertTrue(parts.path and parts.path != "/")
        self.assertEqual((parts.query, parts.fragment, parts.username), ("", "", None))
        self.assertTrue(Connection(DEFAULT_CLIENT_ID, store=self.store,
                                   config=self.config).uses_metadata_document)
        self.assertFalse(self.connection.uses_metadata_document)

    def test_scheme_less_endpoints_get_the_api_scheme(self):
        for name in oauth.ENDPOINTS:
            self.server.metadata[name] = self.server.metadata[name].removeprefix("https://")
        self.assertEqual(self.connection.metadata()["token_endpoint"], f"{ISSUER}/oauth/token")
        self.server.answer(token_payload())
        tokens = self.connection.authorize(
            open_browser=False, timeout=5,
            on_url=browser(lambda uri, q: f'code=c&state={q["state"]}'))
        self.assertEqual(tokens.access_token, "at-1")
        self.assertEqual(len(self.server.token_requests), 1)

    def test_missing_endpoints_are_refused(self):
        del self.server.metadata["token_endpoint"]
        with self.assertRaisesRegex(OAuthError, "token_endpoint=None.*not an absolute URL"):
            self.connection.metadata()
        self.assertIsNone(self.connection._metadata)

    def test_key_and_authorization_url(self):
        self.assertEqual(self.connection.key, f"{ISSUER}|{CLIENT_ID}")
        url = self.connection.authorization_url("http://127.0.0.1:5/callback",
                                                state="st", code_challenge="ch")
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         f"{ISSUER}/oauth/authorize")
        self.assertEqual({k: v[0] for k, v in parse_qs(parts.query).items()}, {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": "http://127.0.0.1:5/callback",
            "scope": " ".join(self.connection.scopes), "state": "st",
            "code_challenge": "ch", "code_challenge_method": "S256"})


class TestAuthorize(OAuthCase):
    def test_exchanges_the_code_and_stores_the_tokens(self):
        self.server.answer(token_payload())
        seen = {}

        def reply(redirect_uri, query):
            seen.update(query)
            return f'code=the-code&state={query["state"]}'

        tokens = self.connection.authorize(open_browser=False, timeout=5, on_url=browser(reply))

        self.assertEqual(tokens.access_token, "at-1")
        self.assertEqual(self.connection.tokens(), tokens)
        form = self.server.token_requests[0]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "the-code")
        self.assertEqual(form["client_id"], CLIENT_ID)
        self.assertEqual(form["redirect_uri"], seen["redirect_uri"])
        self.assertNotIn("client_secret", form)
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        self.assertEqual(seen["code_challenge"],
                         base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        self.assertEqual(self.server.info_requests[0]["client_id"], CLIENT_ID)

    def test_metadata_document_client_id_needs_server_support(self):
        connection = Connection(DEFAULT_CLIENT_ID, store=self.store, config=self.config)
        del self.server.metadata["client_id_metadata_document_supported"]
        with self.assertRaisesRegex(OAuthError, "does not accept client ID metadata documents"):
            connection.authorize(open_browser=False, timeout=5, on_url=self.fail)
        self.assertEqual(self.server.info_requests, [])

        self.server.metadata["client_id_metadata_document_supported"] = True
        connection._metadata = None
        self.server.answer(token_payload())
        connection.authorize(open_browser=False, timeout=5,
                             on_url=browser(lambda uri, q: f'code=c&state={q["state"]}'))
        self.assertEqual(self.server.info_requests[0]["client_id"], DEFAULT_CLIENT_ID)
        self.assertEqual(self.server.token_requests[0]["client_id"], DEFAULT_CLIENT_ID)

    def test_a_refused_client_id_names_the_cause(self):
        connection = Connection(DEFAULT_CLIENT_ID, store=self.store, config=self.config)
        self.server.info_response = (400, {
            "error": "invalid_client",
            "error_description": "client_id is not a pre-authorized client"})
        with self.assertRaisesRegex(
                OAuthError, f"refused the client id {DEFAULT_CLIENT_ID} .*pre-authorized"):
            connection.authorize(open_browser=False, timeout=5, on_url=self.fail)
        self.assertIsNone(connection.tokens())

    def test_a_denied_consent_raises(self):
        def reply(redirect_uri, query):
            return ("error=access_denied&error_description=%3Cscript%3Eno"
                    f'&state={query["state"]}')

        with self.assertRaisesRegex(OAuthError, "access_denied: <script>no"):
            self.connection.authorize(open_browser=False, timeout=5, on_url=browser(reply))
        self.assertIsNone(self.connection.tokens())

    def test_a_response_with_the_wrong_state_is_ignored(self):
        self.server.answer(token_payload())

        def reply(redirect_uri, query):
            def _later():
                with httpx.Client() as http:
                    http.get(f'{redirect_uri}?code=good&state={query["state"]}')

            threading.Timer(0.3, _later).start()
            return "code=forged&state=wrong"

        tokens = self.connection.authorize(open_browser=False, timeout=5, on_url=browser(reply))
        self.assertEqual(tokens.access_token, "at-1")
        self.assertEqual(self.server.token_requests[0]["code"], "good")

    def test_an_idle_connection_does_not_block_the_flow(self):
        real = oauth._CallbackHandler.timeout
        oauth._CallbackHandler.timeout = 0.2
        self.addCleanup(setattr, oauth._CallbackHandler, "timeout", real)
        self.server.answer(token_payload())
        idle = []

        def reply(redirect_uri, query):
            parts = urlsplit(redirect_uri)
            idle.append(socket.create_connection((parts.hostname, parts.port)))
            return f'code=good&state={query["state"]}'

        tokens = self.connection.authorize(open_browser=False, timeout=5, on_url=browser(reply))
        self.assertEqual(tokens.access_token, "at-1")
        for sock in idle:
            sock.close()

    def test_times_out_without_a_redirect(self):
        with self.assertRaisesRegex(TimeoutError, "No answer from the browser"):
            self.connection.authorize(open_browser=False, timeout=0.2, on_url=lambda url: None)

    def test_the_request_is_validated_before_the_browser(self):
        self.server.info_response = (
            400, {"error": "invalid_request", "error_description": "unknown client"})
        opened = []
        with self.assertRaisesRegex(OAuthError, "invalid_request: unknown client"):
            self.connection.authorize(open_browser=False, timeout=5, on_url=opened.append)
        self.assertEqual(opened, [])
        sent = self.server.info_requests[0]
        self.assertEqual(sent["code_challenge_method"], "S256")
        self.assertTrue(sent["redirect_uri"].startswith("http://127.0.0.1:"))


class TestActivate(OAuthCase):
    def test_not_connected(self):
        with self.assertRaisesRegex(NotConnectedError, "Not connected"):
            self.connection.activate()

    def test_fresh_token_is_applied_without_a_refresh(self):
        self.store.save(self.connection.key, TokenSet.from_response(token_payload()))
        self.assertEqual(self.connection.activate(), "at-1")
        self.assertEqual(self.server.token_requests, [])

    def test_expiring_token_is_rotated_and_saved(self):
        self.store.save(self.connection.key, TokenSet.from_response(token_payload(), now=0.0))
        self.server.answer(token_payload(access="at-2", refresh="rt-2"))

        self.assertEqual(self.connection.activate(), "at-2")

        self.assertEqual(self.server.token_requests[0], {
            "grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": CLIENT_ID})
        saved = self.store.load(self.connection.key)
        self.assertEqual(saved.refresh_token, "rt-2")
        self.assertFalse(saved.expires_within(REFRESH_MARGIN))

    def test_a_dead_refresh_token_ends_the_connection(self):
        self.store.save(self.connection.key, TokenSet("stale", "rt-old", 0.0, ""))
        self.server.answer({"error": "invalid_grant",
                            "error_description": "refresh token reuse detected"}, 400)
        with self.assertRaisesRegex(NotConnectedError, "reuse detected"):
            self.connection.activate()
        self.assertIsNone(self.connection.tokens())

    def test_other_server_errors_propagate(self):
        self.store.save(self.connection.key, TokenSet("a", "r", 0.0, ""))
        self.server.answer({"error": "invalid_client", "error_description": "unknown"}, 401)
        with self.assertRaisesRegex(OAuthError, "invalid_client") as err:
            self.connection.activate()
        self.assertEqual(err.exception.status_code, 401)
        self.assertIsNotNone(self.connection.tokens())


class TestDisconnect(OAuthCase):
    def test_revokes_the_current_token_and_forgets(self):
        self.store.save(self.connection.key, TokenSet.from_response(token_payload()))
        self.store.save(self.connection.key, TokenSet("at-2", "rt-2", 10**12, ""))
        self.connection.activate()
        self.connection.disconnect()
        self.assertEqual(self.server.revoke_requests,
                         [{"token": "rt-2", "client_id": CLIENT_ID}])
        self.assertIsNone(self.connection.tokens())

    def test_without_tokens_is_a_no_op(self):
        self.connection.disconnect()
        self.assertEqual(self.server.revoke_requests, [])


class TestCommandLine(OAuthCase):
    def setUp(self):
        super().setUp()
        store = self.store
        real = oauth.TokenStore
        oauth.TokenStore = lambda path: store
        self.addCleanup(setattr, oauth, "TokenStore", real)

    def test_status(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_info:
            oauth.main(["--client-id", CLIENT_ID, "status"])
        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn("not connected", out.getvalue())
        self.store.save(self.connection.key, TokenSet.from_response(token_payload()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            oauth.main(["--client-id", CLIENT_ID, "status"])
        self.assertIn("jobs:read jobs:write", out.getvalue())

    def test_default_client_id_without_the_option(self):
        import contextlib
        import io
        from unittest import mock

        key = f"{ISSUER}|{DEFAULT_CLIENT_ID}"
        self.store.save(key, TokenSet.from_response(token_payload()))
        out = io.StringIO()
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(out):
            os.environ.pop("VELOXQ_OAUTH_CLIENT_ID", None)
            oauth.main(["status"])
        self.assertIn("connected to https://api.test", out.getvalue())

    def test_login_without_a_browser(self):
        import contextlib
        import io

        self.server.answer(token_payload())
        err, out = io.StringIO(), io.StringIO()
        opened = []
        real_open = oauth.webbrowser.open
        oauth.webbrowser.open = lambda url: opened.append(url)
        self.addCleanup(setattr, oauth.webbrowser, "open", real_open)

        def visit_from_stderr():
            import time

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                text = err.getvalue()
                if "http" in text:
                    url = text.split("\n", 1)[1].strip()
                    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
                    with httpx.Client() as http:
                        http.get(f'{query["redirect_uri"]}?code=c&state={query["state"]}')
                    return
                time.sleep(0.05)

        threading.Thread(target=visit_from_stderr, daemon=True).start()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            oauth.main(["--client-id", CLIENT_ID, "login", "--no-browser", "--timeout", "5"])
        self.assertEqual(opened, [])
        self.assertIn("connected to https://api.test", out.getvalue())
        saved = json.loads(self.store.path.read_text())[self.connection.key]
        self.assertEqual(saved["access_token"], "at-1")


if __name__ == "__main__":
    unittest.main()
