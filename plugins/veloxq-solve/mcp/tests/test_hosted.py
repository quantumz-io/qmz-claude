"""Tests for veloxq_mcp.hosted: the tools over streamable HTTP with per-request tokens.

Requests go through the ASGI app with Starlette's test client; the VeloxQ API
is the in-memory ``FakeAPI`` of test_server, which accepts only the bearer
tokens a test registers.
"""
import asyncio
import base64
import contextlib
import io
import json
import os
import threading
import time
import unittest

import httpx
from starlette.testclient import TestClient

from . import test_server
from .test_server import FakeAPI, ServerCase, backends_json, job_json, sbm_json, solver_json  # sets sys.path

from veloxq_mcp import hosted, server  # noqa: E402

ACCEPT = "application/json, text/event-stream"


def jwt(**claims):
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'HS256', 'typ': 'at+jwt'})}.{enc(claims)}.signature"


def user_token(sub, **extra):
    claims = {"iss": "https://api.test", "aud": "veloxq-api", "sub": sub,
              "client_id": "claude-ai", "scope": " ".join(server.SCOPES),
              "exp": int(time.time()) + 600}
    claims.update(extra)
    return jwt(**claims)


class TokenAPI(FakeAPI):
    """FakeAPI that answers 401 unless the request carries a registered bearer token."""

    def __init__(self):
        super().__init__()
        self.tokens = set()

    def __call__(self, request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            self.requests.append(request)
            return httpx.Response(401, json={"message": "Not authenticated"})
        return super().__call__(request)


class HostedCase(ServerCase):
    def setUp(self):
        super().setUp()
        from veloxq_sdk.api.core.http import ClientMixin

        self.api = TokenAPI()
        ClientMixin.http._transport = httpx.MockTransport(self.api)
        self.api.add("GET", "/solvers", {"data": [sbm_json(), solver_json()]})
        self.api.add("GET", "/backends", {"data": backends_json()})
        self.api.add("GET", "/user/profile/", {"email": "ada@example.com", "isVerified": True})
        self.api.add("GET", "/billing/quota", {"remaining": 5})
        self.api.add("GET", "/groups", [])
        server.auth.config.token = ""
        self.addCleanup(setattr, server, "HOSTED", False)
        self.addCleanup(setattr, server, "MAX_WAIT", server.MAX_WAIT)
        self.addCleanup(setattr, server, "SUBMISSION_WINDOW", server.SUBMISSION_WINDOW)
        self.addCleanup(setattr, server, "auth", server.auth)
        self._configure = server.configure
        server.configure = lambda: None
        self.addCleanup(setattr, server, "configure", self._configure)
        args = hosted.parse_args(["--public-url", "https://mcp.test/mcp", "--max-wait", "5"])
        self.client = TestClient(hosted.build(args), base_url="https://mcp.test")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def token(self, sub="7", **extra):
        token = user_token(sub, **extra)
        self.api.tokens.add(token)
        return token

    def rpc(self, method, params=None, token=None):
        headers = {"accept": ACCEPT, "mcp-protocol-version": "2025-06-18"}
        if token is not None:
            headers["authorization"] = (b"Bearer " + token if isinstance(token, bytes)
                                        else f"Bearer {token}")
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return self.client.post("/mcp", json=body, headers=headers)

    def call(self, tool, token, **arguments):
        response = self.rpc("tools/call", {"name": tool, "arguments": arguments}, token)
        self.assertEqual(response.status_code, 200, response.text)
        data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        return json.loads(data[-1])["result"]

    def bearers(self, path):
        return [r.headers.get("authorization") for r in self.api.calls("GET", path)]


class TestDiscovery(HostedCase):
    def test_request_without_token_points_to_the_resource_metadata(self):
        response = self.rpc("tools/list")
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"',
                      response.headers["www-authenticate"])

    def test_resource_metadata_names_the_api_as_authorization_server(self):
        meta = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(meta["resource"], "https://mcp.test/mcp")
        self.assertEqual(meta["authorization_servers"], ["https://api.test"])
        self.assertEqual(meta["scopes_supported"], list(server.SCOPES))

    def test_tools_that_manage_this_machine_are_left_out(self):
        response = self.rpc("tools/list", token=self.token())
        self.assertEqual(response.status_code, 200)
        names = {t["name"] for t in json.loads(response.text.split("data: ", 1)[1])["result"]["tools"]}
        self.assertNotIn("connect", names)
        self.assertNotIn("disconnect", names)
        self.assertIn("submit_job", names)


class TestTokens(HostedCase):
    def test_token_the_api_rejects_is_refused(self):
        self.assertEqual(self.rpc("tools/list", token=user_token("7")).status_code, 401)

    def test_token_without_scope_claim_is_refused(self):
        token = self.token()
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        del claims["scope"]
        first_party = jwt(**claims)
        self.api.tokens.add(first_party)
        self.assertEqual(self.rpc("tools/list", token=first_party).status_code, 401)

    def test_expired_token_is_refused_without_asking_the_api(self):
        token = self.token(exp=int(time.time()) - 1)
        self.assertEqual(self.rpc("tools/list", token=token).status_code, 401)
        self.assertEqual(self.api.calls("GET", "/user/profile/"), [])

    def test_token_without_a_required_scope_is_refused(self):
        token = self.token(scope="user:read jobs:read")
        self.assertEqual(self.rpc("tools/list", token=token).status_code, 403)

    def test_tokens_of_every_client_are_accepted_by_default(self):
        token = self.token(client_id="https://claude.ai/oauth/claude-code-client-metadata")
        self.assertEqual(self.rpc("tools/list", token=token).status_code, 200)

    def test_token_without_a_client_id_claim_is_refused(self):
        token = self.token(client_id="")
        self.assertEqual(self.rpc("tools/list", token=token).status_code, 401)
        self.assertEqual(self.api.calls("GET", "/user/profile/"), [])

    def test_client_id_allowlist_refuses_other_clients(self):
        verifier = hosted.VeloxQTokenVerifier(["claude-ai", "https://claude.ai/oauth/x"])
        self.assertIsNone(asyncio.run(verifier.verify_token(self.token(client_id="other-app"))))
        access = asyncio.run(verifier.verify_token(self.token(client_id="https://claude.ai/oauth/x")))
        self.assertEqual(access.client_id, "https://claude.ai/oauth/x")

    def test_malformed_tokens_are_refused_not_crashed(self):
        nested = jwt(sub="7", scope="x", exp=int(time.time()) + 600)
        deep = nested.split(".")
        deep[1] = base64.urlsafe_b64encode(b"[" * 100_000 + b"]" * 100_000).rstrip(b"=").decode()
        for token in (".".join(deep), (self.token() + "\u00e9").encode("latin-1"), "a.b"):
            with self.subTest(token=token[:20]):
                self.assertEqual(self.rpc("tools/list", token=token).status_code, 401)

    def test_rejected_token_is_not_asked_about_twice(self):
        token = user_token("7")
        self.rpc("tools/list", token=token)
        self.rpc("tools/list", token=token)
        self.assertEqual(len(self.api.calls("GET", "/user/profile/")), 1)

    def test_refused_tokens_never_evict_accepted_ones(self):
        self._max = hosted.MAX_CACHED_TOKENS
        hosted.MAX_CACHED_TOKENS = 2
        self.addCleanup(setattr, hosted, "MAX_CACHED_TOKENS", self._max)
        token = self.token()
        self.rpc("tools/list", token=token)
        for k in range(5):
            self.rpc("tools/list", token=user_token(f"forged-{k}"))
        self.rpc("tools/list", token=token)
        calls = [r for r in self.api.calls("GET", "/user/profile/")
                 if r.headers["authorization"] == f"Bearer {token}"]
        self.assertEqual(len(calls), 1)

    def test_accepted_token_is_cached(self):
        token = self.token()
        self.rpc("tools/list", token=token)
        self.rpc("tools/list", token=token)
        self.assertEqual(len(self.api.calls("GET", "/user/profile/")), 1)

    def test_verifier_cache_expires_with_the_token(self):
        verifier = hosted.VeloxQTokenVerifier()
        token = self.token(exp=int(time.time()) + 2)
        access = asyncio.run(verifier.verify_token(token))
        self.assertEqual((access.subject, access.client_id), ("7", "claude-ai"))
        self.assertLessEqual(next(iter(verifier._accepted.values()))[0], time.time() + 2)


class TestTools(HostedCase):
    def test_every_api_call_carries_the_requesting_users_token(self):
        token = self.token()
        out = self.call("account", token)
        self.assertFalse(out.get("isError"), out)
        self.assertEqual(out["structuredContent"]["result"]["user"]["email"], "ada@example.com")
        self.assertEqual(set(self.bearers("/billing/quota")), {f"Bearer {token}"})
        self.assertFalse(any("x-veloxq-auth-key" in r.headers for r in self.api.requests))

    def test_concurrent_users_keep_their_own_tokens(self):
        tokens = [self.token(sub) for sub in ("1", "2", "3", "4")]
        for k, token in enumerate(tokens):
            self.api.add("GET", f"/jobs/job-{k}", job_json(f"job-{k}", "completed"))
        errors = []

        def run(k, token):
            out = self.call("get_job", token, job_id=f"job-{k}")
            if out.get("isError"):
                errors.append(out)

        threads = [threading.Thread(target=run, args=(k, t)) for k, t in enumerate(tokens)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        for k, token in enumerate(tokens):
            self.assertEqual(set(self.bearers(f"/jobs/job-{k}")), {f"Bearer {token}"})

    def test_catalog_is_kept_per_user(self):
        a, b = self.token("1"), self.token("2")
        self.call("list_solvers", a)
        self.call("submit_job", b, spec={"type": "ising", "quadratic": [[0, 1, 1.0]]})
        self.assertEqual(set(self.bearers("/solvers")), {f"Bearer {a}", f"Bearer {b}"})

    def test_concurrent_calls_are_capped_per_user(self):
        release = threading.Event()
        started = threading.Event()

        def blocking_wait(job_id, seconds):
            started.set()
            release.wait(10)

        self._wait = server._wait
        server._wait = blocking_wait
        self.addCleanup(setattr, server, "_wait", self._wait)
        self._cap = server.MAX_CONCURRENT_CALLS
        server.MAX_CONCURRENT_CALLS = 1
        self.addCleanup(setattr, server, "MAX_CONCURRENT_CALLS", self._cap)
        self.api.add("GET", "/jobs/job-1", job_json("job-1", "running"))
        a, b = self.token("1"), self.token("2")
        first = threading.Thread(target=self.call, args=("get_job", a),
                                 kwargs={"job_id": "job-1", "wait_seconds": 5})
        first.start()
        self.assertTrue(started.wait(5))
        try:
            busy = self.call("get_job", a, job_id="job-1")
            other = self.call("get_job", b, job_id="job-1")
        finally:
            release.set()
            first.join()
        self.assertIn("calls for this account are already running", busy["content"][0]["text"])
        self.assertFalse(other.get("isError"), other)

    def test_submit_job_refuses_a_server_path(self):
        out = self.call("submit_job", self.token(), path="/etc/passwd")
        self.assertTrue(out["isError"])
        self.assertIn("cannot read or write the user's files. Pass the model inline as `spec`.",
                      out["content"][0]["text"])
        self.assertEqual(self.api.calls("GET", "/files"), [])

    def test_rejected_credentials_point_to_the_connector(self):
        token = self.token()
        self.api.add("GET", "/jobs/job-9", {"message": "gone"}, status_code=403)
        out = self.call("get_job", token, job_id="job-9")
        self.assertIn("reconnect the VeloxQ connector", out["content"][0]["text"])
        self.assertNotIn("`connect`", out["content"][0]["text"])

    def test_get_result_refuses_an_output_file(self):
        out = self.call("get_result", self.token(), job_id="job-1", output="/tmp/r.json")
        self.assertTrue(out["isError"])
        self.assertIn("Save the returned document yourself.", out["content"][0]["text"])

    def test_report_is_shown_in_the_conversation(self):
        test_server.TestGetResult._completed(self, [[1, -1]], [-1.0], [0, 1])
        token = self.token()
        listed = self.rpc("tools/list", token=token)
        tools = {t["name"]: t for t in
                 json.loads(listed.text.split("data: ", 1)[1])["result"]["tools"]}
        self.assertEqual(tools["report"]["_meta"]["ui"], {"resourceUri": server.REPORT_URI})
        out = self.call("report", token, job_id="job-1", view=test_server.TestReport.VIEW,
                        spec=test_server.TestReport.SPEC)
        self.assertFalse(out["isError"], out["content"])
        self.assertIn('data-figure="graph"', out["structuredContent"]["html"])
        self.assertEqual(json.loads(out["content"][0]["text"])["display"], server.SHOWN_BY_APP)
        self.assertEqual(self.bearers("/jobs/job-1"), [f"Bearer {token}"])

    def test_report_refuses_server_files(self):
        for argument, hint in (("output", "shown in the conversation"),
                               ("view_path", "Pass the view as `view`.")):
            with self.subTest(argument=argument):
                out = self.call("report", self.token(), job_id="job-1",
                                **{argument: "/tmp/x"})
                self.assertTrue(out["isError"])
                self.assertIn(hint, out["content"][0]["text"])

    def test_large_inline_spec_gets_the_hosted_hint(self):
        quadratic = [[i, i + 1, 1.0] for i in range(20_001)]
        out = self.call("submit_job", self.token(), spec={"type": "ising", "quadratic": quadratic})
        self.assertTrue(out["isError"])
        self.assertIn("above the limit of 20000. The hosted server takes inline models only.",
                      out["content"][0]["text"])

    def test_submission_ceiling_is_per_user_and_per_hour(self):
        server._submissions["user:3"] = [time.monotonic() - 3601] * server.MAX_SUBMISSIONS
        spec = {"type": "ising", "quadratic": [[0, 1, 1.0]]}
        out = self.call("submit_job", self.token("3"), spec=spec)
        self.assertNotIn("already submitted", json.dumps(out))
        server._submissions["user:1"] = [time.monotonic()] * server.MAX_SUBMISSIONS
        spec = {"type": "ising", "quadratic": [[0, 1, 1.0]]}
        out = self.call("submit_job", self.token("1"), spec=spec)
        self.assertIn("already submitted", out["content"][0]["text"])
        out = self.call("submit_job", self.token("2"), spec=spec)
        self.assertNotIn("already submitted", json.dumps(out))

    def test_wait_is_capped_by_max_wait(self):
        waits = []
        self._wait = server._wait
        server._wait = lambda job_id, seconds: waits.append(seconds)
        self.addCleanup(setattr, server, "_wait", self._wait)
        self.api.add("GET", "/jobs/job-1", job_json("job-1", "running"))
        out = self.call("get_job", self.token(), job_id="job-1", wait_seconds=600)
        self.assertFalse(out.get("isError"), out)
        self.assertEqual(waits, [5])


class TestStartup(unittest.TestCase):
    def test_public_url_must_be_https_with_a_path(self):
        for url, message in (("http://mcp.example.com/mcp", "must be https"),
                             ("https://mcp.example.com", "must include the MCP path")):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                hosted.parse_args(["--public-url", url])
            self.assertIn(message, err.getvalue())

    def test_client_ids_are_optional(self):
        from unittest import mock

        with mock.patch.dict(os.environ):
            os.environ.pop("VELOXQ_MCP_CLIENT_ID", None)
            args = hosted.parse_args(["--public-url", "https://mcp.test/mcp"])
            self.assertEqual(args.client_ids, [])
            args = hosted.parse_args(["--public-url", "https://mcp.test/mcp",
                                      "--client-id", "a", "--client-id", "b"])
            self.assertEqual(args.client_ids, ["a", "b"])
            os.environ["VELOXQ_MCP_CLIENT_ID"] = "a, https://claude.ai/oauth/x"
            args = hosted.parse_args(["--public-url", "https://mcp.test/mcp"])
            self.assertEqual(args.client_ids, ["a", "https://claude.ai/oauth/x"])

    def test_logging_hides_request_urls(self):
        import logging

        hosted.configure_logging()
        self.assertGreaterEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)

    def test_loopback_http_is_allowed(self):
        args = hosted.parse_args(["--public-url", "http://127.0.0.1:8000/mcp"])
        self.assertEqual(args.public_url, "http://127.0.0.1:8000/mcp")

    def test_configured_api_key_stops_the_server(self):
        from veloxq_sdk.config import VeloxQAPIConfig

        VeloxQAPIConfig.clear_instance()
        VeloxQAPIConfig.instance().token = "key"
        saved = server.configure, server.auth, server.HOSTED
        server.configure = lambda: None
        try:
            args = hosted.parse_args(["--public-url", "https://mcp.test/mcp", "--client-id", "c"])
            with self.assertRaisesRegex(SystemExit, "acts only with each user's own token"):
                hosted.build(args)
            self.assertFalse(server.HOSTED)
        finally:
            server.configure, server.auth, server.HOSTED = saved
            VeloxQAPIConfig.clear_instance()


if __name__ == "__main__":
    unittest.main()
