"""Tests for veloxq_mcp.server: the tools against an in-memory VeloxQ API.

The API is an ``httpx.MockTransport`` on the SDK's shared client. An API key
is configured so the tools run without the OAuth flow; the flow itself is
tested with a stand-in connection.
"""
import json
import os
import re
import sys
import tempfile
import asyncio
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from veloxq_mcp import report, server  # noqa: E402

SOLVER_ID = "3bce1dfa-e7af-4040-a283-67cff253cf94"
BACKEND_ID = "a87c8e0c-c883-4d6a-8495-6cd55e95ed96"
OFFLINE_ID = "1095cf2d-a3a0-4125-9615-45f2884e1aec"
SBM_ID = "5d2c0f1e-8a7b-4c3d-9e2f-1a2b3c4d5e6f"


def solver_json():
    return {
        "id": SOLVER_ID, "name": "VeloxQ", "description": "d",
        "costDollarsPerSecond": 0.01, "docLink": None, "fileFormats": {"HDF5": [".h5"]},
        "supportBackend": [{"id": BACKEND_ID, "name": "H100 x1"},
                           {"id": OFFLINE_ID, "name": "H100 x2"}],
        "parameters": [
            {"name": "numRep", "text": "NumRep", "description": "trajectories",
             "default": "128", "required": False, "autoTune": False,
             "type": "integer", "min": 1.0, "max": None, "values": []},
            {"name": "dt", "text": "dt", "description": "step", "default": None,
             "required": True, "autoTune": True, "type": "float", "min": 0.0,
             "max": 1.0, "values": []},
            {"name": "mode", "text": "Mode", "description": "", "default": "fast",
             "required": False, "autoTune": False, "type": "select", "min": None,
             "max": None, "values": [{"name": "Fast", "value": "fast"},
                                     {"name": "Slow", "value": "slow"}]},
            {"name": "timeout", "text": "Timeout", "description": "seconds", "default": "21600",
             "required": False, "autoTune": False, "type": "timeout", "min": None, "max": None,
             "values": []},
        ],
    }


def sbm_json():
    return {
        "id": SBM_ID, "name": "SBM", "description": "d", "costDollarsPerSecond": 0.01,
        "docLink": None, "fileFormats": {"HDF5": [".h5"]},
        "supportBackend": [{"id": BACKEND_ID, "name": "H100 x1"}], "parameters": [],
    }


def backends_json():
    return [
        {"id": BACKEND_ID, "name": "H100 x1", "type": "GPU", "status": "online",
         "description": "", "costScale": 1.0, "usageScale": 1.0, "source": "AZURE"},
        {"id": OFFLINE_ID, "name": "H100 x2", "type": "GPU", "status": "offline",
         "description": "", "costScale": 2.0, "usageScale": 2.0, "source": "AZURE"},
    ]


def job_json(job_id="job-1", status="pending", **extra):
    base = {
        "id": job_id, "status": status, "statusMessage": None, "fileId": "file-1",
        "createdAt": "2026-10-22T10:00:00Z", "updatedAt": "2026-10-22T10:00:05Z",
        "finishedAt": None, "resultAvailable": False,
        "solver": {"id": SOLVER_ID, "name": "VeloxQ"},
    }
    base.update(extra)
    return base


class FakeAPI:
    def __init__(self):
        self.requests = []
        self.routes = {}

    def add(self, method, path, json_body=None, status_code=200, headers=None):
        self.routes[(method, path)] = (status_code, json_body, headers or {})

    def calls(self, method, path):
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]

    def __call__(self, request):
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"message": f"no route for {key}"})
        status_code, body, headers = self.routes[key]
        return httpx.Response(status_code, json=body, headers=headers)


class ServerCase(unittest.TestCase):
    def setUp(self):
        from veloxq_sdk.api.core.http import ClientMixin, _RestClientGetter
        from veloxq_sdk.config import VeloxQAPIConfig

        if _RestClientGetter.client is not None:
            _RestClientGetter.client.close()
            _RestClientGetter.client = None
        VeloxQAPIConfig.clear_instance()
        config = VeloxQAPIConfig.instance()
        config.url = "https://api.test"
        config.token = "key"
        self.api = FakeAPI()
        ClientMixin.http._transport = httpx.MockTransport(self.api)
        ClientMixin.http.storage._transport = httpx.MockTransport(self.api)
        server.auth = server.Auth()
        server._catalog.clear()
        server._submissions.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._ledger = server.LEDGER_FILE
        server.LEDGER_FILE = Path(self.tmp.name) / "ledger.log"
        self.addCleanup(setattr, server, "LEDGER_FILE", self._ledger)
        self.api.add("GET", "/solvers", {"data": [sbm_json(), solver_json()]})
        self.api.add("GET", "/backends", {"data": backends_json()})


class TestHelpers(ServerCase):
    def test_snake(self):
        self.assertEqual(server._snake({"createdAt": [{"fileId": 1}], "x": 2}),
                         {"created_at": [{"file_id": 1}], "x": 2})

    def test_resolve_solver_by_name_id_or_prefix(self):
        for wanted in ("VeloxQ", "veloxq", "velox", SOLVER_ID.upper()):
            self.assertEqual(server.resolve_solver(wanted)["id"], SOLVER_ID)
        self.assertEqual(server.resolve_solver("sbm")["id"], SBM_ID)
        with self.assertRaisesRegex(ToolError, "unknown solver 'nope'; known: SBM, VeloxQ"):
            server.resolve_solver("nope")

    def test_resolve_backend_defaults_to_online(self):
        solver = server.resolve_solver("veloxq")
        self.assertEqual(server.resolve_backend(solver, None)["id"], BACKEND_ID)
        self.assertEqual(server.resolve_backend(solver, "h100 x1")["id"], BACKEND_ID)
        with self.assertRaisesRegex(ToolError, "H100 x2 is offline"):
            server.resolve_backend(solver, "H100 x2")

    def test_validate_parameters(self):
        solver = server.resolve_solver("veloxq")
        self.assertEqual(
            server.validate_parameters(solver, {"numRep": "64", "dt": 0.5, "mode": "slow",
                                                }),
            {"numRep": 64, "dt": 0.5, "mode": "slow"})
        # Omitted parameters take the listed defaults; dt has none.
        self.assertEqual(server.validate_parameters(solver, {}),
                         {"numRep": 128, "mode": "fast"})
        self.assertEqual(server.validate_parameters(solver, {"numRep": 64}),
                         {"numRep": 64, "mode": "fast"})
        cases = [
            ({"num_rep": 1}, "unknown parameters"),
            ({"timeout": 60}, "unknown parameters"),
            ({"numRep": 0}, "must be >= 1.0"),
            ({"dt": 2}, "must be <= 1.0"),
            ({"dt": "x"}, "must be a float"),
            ({"numRep": 1.5}, "must be a integer"),
            ({"mode": "medium"}, "one of \\['fast', 'slow'\\]"),
        ]
        for params, message in cases:
            with self.subTest(params=params), self.assertRaisesRegex(ToolError, message):
                server.validate_parameters(solver, params)

    def test_catalog_is_cached(self):
        server.catalog()
        server.catalog()
        self.assertEqual(len(self.api.calls("GET", "/solvers")), 1)
        server.catalog(refresh=True)
        self.assertEqual(len(self.api.calls("GET", "/solvers")), 2)

    def test_error_messages_carry_no_url(self):
        request = httpx.Request("GET", "https://blob.test/x?sig=SECRET")
        response = httpx.Response(403, json={"message": "Signature expired"}, request=request)
        err = server._tool_error(httpx.HTTPStatusError("boom", request=request, response=response))
        self.assertEqual(str(err), "VeloxQ refused the action (403: Signature expired).")
        self.assertNotIn("SECRET", str(err))
        response = httpx.Response(403, json={"message": "Missing scope jobs:write"},
                                  request=request)
        err = server._tool_error(httpx.HTTPStatusError("boom", request=request, response=response))
        self.assertEqual(str(err), "VeloxQ refused the action (403: Missing scope jobs:write). "
                                   "The connection lacks a scope the action needs; "
                                   "`disconnect` and `connect` again to grant it.")
        response = httpx.Response(429, json={"message": "queue_full"},
                                  headers={"Retry-After": "12"}, request=request)
        err = server._tool_error(httpx.HTTPStatusError("x", request=request, response=response))
        self.assertIn("429: queue_full", str(err))
        self.assertIn("Retry after 12 s", str(err))
        response = httpx.Response(401, json={"message": "Credentials expired"}, request=request)
        err = server._tool_error(httpx.HTTPStatusError("x", request=request, response=response))
        self.assertIn("Call `connect`", str(err))
        err = server._tool_error(httpx.ConnectError("refused", request=request))
        self.assertEqual(str(err), "cannot reach VeloxQ at https://api.test: ConnectError")


class TestAccountTools(ServerCase):
    def test_account_with_api_key(self):
        self.api.add("GET", "/user/profile/", {"email": "u@x.test", "firstName": "A",
                                                "lastName": "B", "isVerified": True,
                                                "balance": {"amount": 12.5, "currency": "USD"}})
        self.api.add("GET", "/billing/quota", {"quota": 100, "group": "g"})
        self.api.add("GET", "/groups", [{"name": "g", "quota": 100, "active": True}])
        out = server.account()
        self.assertTrue(out["connected"])
        self.assertEqual(out["auth_method"], "api_key")
        self.assertEqual(out["user"], {"email": "u@x.test", "name": "A B", "verified": True,
                                       "balance": {"amount": 12.5, "currency": "USD"}})
        self.assertEqual(out["quota"], {"quota": 100, "group": "g"})
        self.assertEqual(out["groups"][0]["active"], True)

    def test_account_not_connected(self):
        server.auth.config.token = ""
        out = server.account()
        self.assertFalse(out["connected"])
        self.assertIn("connect", out["next_step"])
        with self.assertRaisesRegex(ToolError, "Not connected"):
            server.list_solvers()

    def test_list_solvers_merges_backend_status(self):
        out = server.list_solvers()["solvers"][1]
        self.assertEqual(out["name"], "VeloxQ")
        self.assertEqual(out["cost_dollars_per_second"], 0.01)
        self.assertEqual([b["status"] for b in out["backends"]], ["online", "offline"])
        self.assertEqual(out["backends"][1]["cost_scale"], 2.0)
        self.assertEqual(out["parameters"][2]["values"], ["fast", "slow"])
        self.assertNotIn("timeout", [p["name"] for p in out["parameters"]])
        self.assertNotIn("support_backend", out)


class FakeConnection:
    """Stands in for veloxq_mcp.oauth.Connection during the connect flow."""

    instances = []
    stored = {}  # client id -> connected, the on-disk token store

    def __init__(self, client_id, *, scopes):
        self.client_id = client_id
        self.scopes = scopes
        self.installed = False
        self.disconnected = False
        self.gate = threading.Event()
        self.outcome = "ok"
        FakeConnection.instances.append(self)

    @property
    def connected(self):
        return FakeConnection.stored.get(self.client_id, False)

    @connected.setter
    def connected(self, value):
        FakeConnection.stored[self.client_id] = value

    @property
    def is_connected(self):
        return self.connected

    def tokens(self):
        if not self.connected:
            return None
        return type("T", (), {"scopes": ("jobs:read",)})()

    def authorize(self, *, open_browser, timeout, on_url):
        on_url("https://api.test/oauth/authorize?state=s")
        self.gate.wait(timeout)
        if self.outcome == "denied":
            from veloxq_mcp.oauth import OAuthError
            raise OAuthError("access_denied", "the user said no")
        self.connected = True
        return self.tokens()

    def activate(self):
        self.installed = True
        return "token"

    def disconnect(self):
        self.connected = False
        self.disconnected = True


def run(coroutine):
    return asyncio.run(coroutine)


class FakeSession:
    """The client side of an elicitation on a 2025-11-25 connection."""

    def __init__(self, action):
        self.action = action
        self.elicited = []
        self.completed = []

    async def elicit_url(self, message, url, elicitation_id):
        from mcp.types import ElicitResult

        self.elicited.append((message, url, elicitation_id))
        return ElicitResult(action=self.action)

    async def send_elicit_complete(self, elicitation_id):
        self.completed.append(elicitation_id)


class FakeContext:
    """Stands in for the request Context: capabilities, protocol and retry answers."""

    def __init__(self, *, protocol="2026-07-28", url_mode=True, responses=None, state=None,
                 action="accept"):
        self.protocol_version = protocol
        self.input_responses = responses
        self.request_state = state
        self.client_capabilities = SimpleNamespace(
            elicitation=SimpleNamespace(form={}, url={} if url_mode else None))
        self.session = FakeSession(action)

    async def elicit_url(self, message, url, elicitation_id):
        return await self.session.elicit_url(message, url, elicitation_id)


class TestConnectFlow(ServerCase):
    def setUp(self):
        super().setUp()
        server.auth.config.token = ""
        self._real = server.Connection
        server.Connection = FakeConnection
        self.addCleanup(setattr, server, "Connection", self._real)
        self._client_id = server.DEFAULT_CLIENT_ID
        server.DEFAULT_CLIENT_ID = "vq_test"
        self.addCleanup(setattr, server, "DEFAULT_CLIENT_ID", self._client_id)
        FakeConnection.instances.clear()
        FakeConnection.stored.clear()
        self.opened = []
        real_open = server._open_browser
        server._open_browser = lambda url: (self.opened.append(url), False)[1]
        self.addCleanup(setattr, server, "_open_browser", real_open)
        self.api.add("GET", "/user/profile/", {"email": "u@x.test"})
        self.api.add("GET", "/billing/quota", {"quota": 1, "group": "g"})
        self.api.add("GET", "/groups", [])

    def test_pending_then_connected(self):
        first = run(server.connect(wait_seconds=0))
        self.assertEqual(first["status"], "pending")
        self.assertFalse(first["browser_opened"])
        self.assertEqual(first["authorization_url"], "https://api.test/oauth/authorize?state=s")
        self.assertEqual(self.opened, ["https://api.test/oauth/authorize?state=s"])
        self.assertIn("call `connect` again", first["next_step"])
        connection = FakeConnection.instances[0]
        self.assertEqual(connection.scopes, server.SCOPES)

        connection.gate.set()
        second = run(server.connect(wait_seconds=5))
        self.assertTrue(second["connected"])
        self.assertEqual(second["auth_method"], "oauth")
        self.assertEqual(second["scopes"], ["jobs:read"])
        self.assertEqual(second["user"], {"email": "u@x.test"})
        self.assertIn("confirm", second["note"])
        self.assertTrue(connection.installed)
        self.assertEqual(len(FakeConnection.instances), 1)

        again = run(server.connect())
        self.assertIn("Already connected", again["note"])

    def test_denied_consent_is_reported_and_can_restart(self):
        run(server.connect(wait_seconds=0))
        connection = FakeConnection.instances[0]
        connection.outcome = "denied"
        connection.gate.set()
        with self.assertRaisesRegex(ToolError, "access_denied: the user said no"):
            run(server.connect(wait_seconds=5))
        self.assertIsNone(server.auth.pending)
        connection.outcome = "ok"
        connection.gate.clear()
        restarted = run(server.connect(wait_seconds=0))
        self.assertEqual(restarted["status"], "pending")
        connection.gate.set()
        self.assertTrue(run(server.connect(wait_seconds=5))["connected"])

    def test_disconnect(self):
        self.assertEqual(server.disconnect()["note"], "Nothing to disconnect.")
        run(server.connect(wait_seconds=0))
        FakeConnection.instances[0].gate.set()
        run(server.connect(wait_seconds=5))
        out = server.disconnect()
        self.assertFalse(out["connected"])
        self.assertTrue(FakeConnection.instances[0].disconnected)

    def test_the_first_call_returns_the_link_at_once(self):
        server._open_browser = lambda url: True
        started = time.monotonic()
        first = run(server.connect(wait_seconds=90))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(first["status"], "pending")
        self.assertTrue(first["browser_opened"])
        self.assertEqual(first["authorization_url"], "https://api.test/oauth/authorize?state=s")
        self.assertIn("If no browser window appeared, open this link yourself: "
                      "https://api.test/oauth/authorize?state=s", first["message"])
        self.assertIn("Show the user `message`", first["next_step"])
        self.assertIn("call `connect` again", first["next_step"])

        started = time.monotonic()
        second = run(server.connect(wait_seconds=1))
        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.assertEqual(second["status"], "pending")
        self.assertIn("open this link yourself", second["message"])

    def test_modern_client_gets_a_consent_dialog_then_connects(self):
        from mcp.types import ElicitResult, InputRequiredResult

        started = time.monotonic()
        first = run(server.connect(wait_seconds=90, ctx=FakeContext()))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsInstance(first, InputRequiredResult)
        request = first.input_requests[server.CONSENT_KEY]
        self.assertEqual(request.method, "elicitation/create")
        self.assertEqual(request.params.mode, "url")
        self.assertEqual(request.params.url, "https://api.test/oauth/authorize?state=s")
        self.assertEqual(first.request_state, server.auth.pending["id"])
        self.assertEqual(request.params.elicitation_id, first.request_state)

        FakeConnection.instances[0].gate.set()
        retry = FakeContext(responses={server.CONSENT_KEY: ElicitResult(action="accept")},
                            state=first.request_state)
        second = run(server.connect(wait_seconds=5, ctx=retry))
        self.assertTrue(second["connected"])
        self.assertEqual(second["user"], {"email": "u@x.test"})

    def test_modern_client_cancelling_the_dialog_gets_the_link(self):
        from mcp.types import ElicitResult

        first = run(server.connect(wait_seconds=90, ctx=FakeContext()))
        retry = FakeContext(responses={server.CONSENT_KEY: ElicitResult(action="cancel")},
                            state=first.request_state)
        started = time.monotonic()
        out = run(server.connect(wait_seconds=90, ctx=retry))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(out["status"], "pending")
        self.assertIn("open this link yourself: https://api.test/oauth/authorize?state=s",
                      out["message"])

    def test_legacy_client_dialog_accepted(self):
        ctx = FakeContext(protocol="2025-11-25")
        threading.Timer(0.2, lambda: FakeConnection.instances[0].gate.set()).start()
        out = run(server.connect(wait_seconds=5, ctx=ctx))
        self.assertTrue(out["connected"])
        self.assertEqual(ctx.session.elicited[0][1], "https://api.test/oauth/authorize?state=s")
        self.assertEqual(ctx.session.completed, [ctx.session.elicited[0][2]])

    def test_legacy_client_dialog_declined_gets_the_link(self):
        ctx = FakeContext(protocol="2025-11-25", action="decline")
        out = run(server.connect(wait_seconds=90, ctx=ctx))
        self.assertEqual(out["status"], "pending")
        self.assertIn("open this link yourself", out["message"])
        self.assertEqual(ctx.session.completed, [])

    def test_client_without_url_elicitation_gets_the_link(self):
        ctx = FakeContext(url_mode=False)
        out = run(server.connect(wait_seconds=90, ctx=ctx))
        self.assertEqual(out["status"], "pending")
        self.assertIn("open this link yourself", out["message"])
        self.assertEqual(ctx.session.elicited, [])

    def test_tool_calls_carry_the_users_access_token(self):
        run(server.connect(wait_seconds=0))
        FakeConnection.instances[0].gate.set()
        run(server.connect(wait_seconds=5))
        FakeConnection.instances[0].installed = False
        server.list_solvers()
        self.assertTrue(FakeConnection.instances[0].installed)
        request = self.api.calls("GET", "/solvers")[-1]
        self.assertEqual(request.headers.get("authorization"), "Bearer token")
        self.assertNotIn("x-api-key", {k.lower() for k in request.headers})

    def test_a_stored_connection_is_seen_after_a_restart(self):
        run(server.connect(wait_seconds=0))
        FakeConnection.instances[0].gate.set()
        run(server.connect(wait_seconds=5))
        server.auth = server.Auth()  # a new server process
        status = server.account()
        self.assertTrue(status["connected"])
        self.assertEqual(status["auth_method"], "oauth")
        self.assertIn("Already connected", run(server.connect())["note"])
        self.assertTrue(FakeConnection.instances[-1].installed)
        self.assertFalse(server.disconnect()["connected"])
        self.assertTrue(FakeConnection.instances[-1].disconnected)

    def test_client_id_is_the_metadata_document_url_unless_overridden(self):
        from unittest import mock

        from veloxq_mcp.oauth import DEFAULT_CLIENT_ID

        server.DEFAULT_CLIENT_ID = self._client_id
        with mock.patch.dict(os.environ):
            os.environ.pop("VELOXQ_OAUTH_CLIENT_ID", None)
            server.auth = server.Auth()
            run(server.connect(wait_seconds=0))
            self.assertEqual(FakeConnection.instances[-1].client_id, DEFAULT_CLIENT_ID)
            os.environ["VELOXQ_OAUTH_CLIENT_ID"] = "vq_registered"
            server.auth = server.Auth()
            run(server.connect(wait_seconds=0))
            self.assertEqual(FakeConnection.instances[-1].client_id, "vq_registered")


class TestJobTools(ServerCase):
    def test_submit_job_posts_catalog_ids_and_records_the_ledger(self):
        import veloxq_sdk

        uploaded = {}

        class FakeFile:
            id = "file-9"

        def from_instance(instance, **kwargs):
            uploaded["bqm"] = instance
            uploaded["kwargs"] = kwargs
            return FakeFile()

        real = veloxq_sdk.File.from_instance
        veloxq_sdk.File.from_instance = staticmethod(from_instance)
        self.addCleanup(setattr, veloxq_sdk.File, "from_instance", real)
        self.api.add("POST", "/jobs", [job_json("job-7", "created")],
                     headers={"RateLimit": '"queue";r=41'})

        out = server.submit_job(spec={"type": "qubo", "quadratic": [[0, 1, -2.0]],
                                      "linear": [1.0, 1.0]},
                                parameters={"numRep": 32})

        self.assertEqual(out["job_id"], "job-7")
        self.assertEqual(out["solver"], "VeloxQ")
        self.assertEqual(out["backend"], "H100 x1")
        self.assertEqual(out["queue_remaining"], 41)
        self.assertEqual(out["file_id"], "file-9")
        self.assertEqual(out["model"]["num_variables"], 2)
        self.assertEqual(uploaded["kwargs"], {})
        self.assertEqual(sorted(uploaded["bqm"].variables), [0, 1])
        body = json.loads(self.api.calls("POST", "/jobs")[0].content)
        self.assertEqual(body, {"problemId": None, "solvers": [{
            "solverId": SOLVER_ID, "backendId": BACKEND_ID,
            "files": [{"fileId": "file-9"}], "parameters": {"numRep": 32, "mode": "fast"}}]})
        ledger = server.LEDGER_FILE.read_text()
        self.assertIn(" job-7 VeloxQ H100 x1 qubo n=2", ledger)

    def test_submit_job_refuses_after_the_ceiling(self):
        server._submissions["local"] = [0.0] * server.MAX_SUBMISSIONS
        with self.assertRaisesRegex(ToolError, "already submitted"):
            server.submit_job(spec={"type": "qubo", "linear": [1]})

    def test_submit_job_validates_before_uploading(self):
        with self.assertRaisesRegex(ToolError, "unknown parameters"):
            server.submit_job(spec={"type": "qubo", "linear": [1]}, parameters={"x": 1})
        with self.assertRaisesRegex(ToolError, "exactly one"):
            server.submit_job()
        self.assertEqual(self.api.calls("POST", "/jobs"), [])

    def test_submit_job_relays_platform_refusals(self):
        import veloxq_sdk

        patcher = mock.patch.object(veloxq_sdk.File, "from_instance",
                                    lambda instance, **kw: type("F", (), {"id": "f"})())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api.add("POST", "/jobs", {"message": "Insufficient balance to start job!"}, 422)
        with self.assertRaisesRegex(ToolError, "422: Insufficient balance"):
            server.submit_job(spec={"type": "ising", "linear": [1]})

    def test_a_failed_post_still_counts_toward_the_ceiling(self):
        import veloxq_sdk

        patcher = mock.patch.object(veloxq_sdk.File, "from_instance",
                                    lambda instance, **kw: type("F", (), {"id": "f"})())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api.add("POST", "/jobs", {"message": "gateway timeout"}, 504)
        with self.assertRaisesRegex(ToolError, "504"):
            server.submit_job(spec={"type": "ising", "linear": [1]})
        self.assertEqual(len(server._submissions["local"]), 1)

    def test_an_invalid_request_does_not_count_toward_the_ceiling(self):
        with self.assertRaises(ToolError):
            server.submit_job(spec={"type": "ising", "linear": [1]}, solver="nope")
        self.assertEqual(server._submissions.get("local", []), [])

    def test_get_job_view(self):
        self.api.add("GET", "/jobs/job-1", job_json(
            status="completed", resultAvailable=True, finishedAt="2026-10-22T10:01:00Z",
            backend={"id": BACKEND_ID, "name": "H100 x1"},
            statistics={"pendingTime": 1.0, "runningTime": 2.0, "costSolverTime": 1.5,
                        "totalCost": 0.015, "totalQuotaUsage": 2},
            timeline=[{"name": "created", "value": "2026-10-22T10:00:00Z"}],
            parameters=[{"name": "numRep", "value": 32}]))
        out = server.get_job("job-1")
        self.assertTrue(out["finished"])
        self.assertEqual(out["statistics"]["total_cost_usd"], 0.015)
        self.assertEqual(out["statistics"]["quota_usage_ms"], 2)
        self.assertEqual(out["parameters"], {"numRep": 32})
        self.assertEqual(out["backend"], "H100 x1")
        self.assertEqual(out["timeline"][0]["name"], "created")

    def test_get_job_waits_on_the_stream(self):
        self.api.add("GET", "/jobs/job-1", job_json(status="running"))
        frames = [json.dumps({"status": "running", "finished": False}).encode(),
                  json.dumps({"status": "completed", "finished": True}).encode()]
        opened = []

        class FakeWS:
            def recv(self, timeout=None, decode=None):
                if not frames:
                    raise TimeoutError
                return frames.pop(0)

        class FakeHTTP:
            def get(self, path, params=None):
                return httpx.Client(transport=httpx.MockTransport(self.api_)).get(
                    "https://api.test/" + path, params=params)

            def open_ws(self, path):
                from contextlib import contextmanager

                @contextmanager
                def cm():
                    opened.append(path)
                    yield FakeWS()
                return cm()

        fake = FakeHTTP()
        fake.api_ = self.api
        real = server._http
        server._http = lambda: fake
        self.addCleanup(setattr, server, "_http", real)

        out = server.get_job("job-1", wait_seconds=5)
        self.assertEqual(opened, ["jobs/job-1/status-updates"])
        self.assertEqual(frames, [])
        self.assertEqual(out["status"], "running")  # the final GET is what is reported
        self.assertEqual(len(self.api.calls("GET", "/jobs/job-1")), 2)

    def test_wait_reports_a_refused_handshake(self):
        import websockets

        class FakeHTTP:
            def open_ws(self, path):
                from contextlib import contextmanager

                @contextmanager
                def cm():
                    raise websockets.exceptions.InvalidURI("wss://x", "bad")
                    yield
                return cm()

        real = server._http
        server._http = lambda: FakeHTTP()
        self.addCleanup(setattr, server, "_http", real)
        with self.assertRaisesRegex(ToolError, "could not be opened"):
            server._wait("job-1", 5)

    def test_wait_stops_when_the_stream_stalls(self):
        class StalledWS:
            def recv(self, timeout=None, decode=None):
                raise TimeoutError

        class FakeHTTP:
            def open_ws(self, path):
                from contextlib import contextmanager

                @contextmanager
                def cm():
                    yield StalledWS()
                return cm()

        real = server._http
        server._http = lambda: FakeHTTP()
        self.addCleanup(setattr, server, "_http", real)
        self.assertIsNone(server._wait("job-1", 5))

    def test_list_jobs_filters_and_trims(self):
        self.api.add("GET", "/jobs", {"data": [job_json(), job_json("job-2", "completed")]})
        out = server.list_jobs(status="completed", created="lastWeek", limit=500, q="job")
        params = self.api.calls("GET", "/jobs")[0].url.params
        self.assertEqual(params["status"], "completed")
        self.assertEqual(params["createdAt"], "lastWeek")
        self.assertEqual(params["_limit"], "200")
        self.assertEqual(params["q"], "job")
        self.assertEqual(out["count"], 2)
        self.assertNotIn("statistics", out["jobs"][0])
        with self.assertRaisesRegex(ToolError, "status must be one of"):
            server.list_jobs(status="created")

    def test_job_logs_tail(self):
        rows = [{"timestamp": f"t{i}", "category": "INFO", "message": f"m{i}"} for i in range(5)]
        self.api.add("GET", "/jobs/job-1/logs", rows)
        out = server.job_logs("job-1", category="info", grep="m", tail=2)
        params = self.api.calls("GET", "/jobs/job-1/logs")[0].url.params
        self.assertEqual(params["category"], "INFO")
        self.assertEqual(params["timePeriod"], "allTime")
        self.assertEqual(params["q"], "m")
        self.assertEqual([line["message"] for line in out["lines"]], ["m3", "m4"])
        self.assertEqual(out["total"], 5)
        self.assertTrue(out["truncated"])

    def test_job_ids_are_validated_before_any_request(self):
        for bad in ("../oauth/revoke", "x/../../groups", "a?b=c", "", "x" * 65):
            with self.subTest(bad=bad):
                for call in (lambda: server.get_job(bad), lambda: server.cancel_job(bad),
                             lambda: server.job_logs(bad), lambda: server.get_result(bad)):
                    with self.assertRaisesRegex(ToolError, "invalid job id"):
                        call()
        self.assertEqual([r for r in self.api.requests if "jobs" in r.url.path], [])

    def test_cancel_job(self):
        self.api.add("POST", "/jobs/job-1/cancel", {"message": "Job cancel requested"})
        self.api.add("GET", "/jobs/job-1", job_json(status="running"))
        out = server.cancel_job("job-1")
        self.assertEqual(out["status"], "running")
        self.assertIn("Cancellation requested", out["note"])
        self.api.add("POST", "/jobs/job-1/cancel", {"message": "Job already finished"}, 409)
        with self.assertRaisesRegex(ToolError, "409: Job already finished"):
            server.cancel_job("job-1")


class TestGetResult(ServerCase):
    def _completed(self, samples, energies, variables, status="completed"):
        import dimod

        from veloxq_sdk.jobs import Job

        self.api.add("GET", "/jobs/job-1", job_json(
            status=status, resultAvailable=True,
            statistics={"usageTime": 2.5, "totalQuotaUsage": 584},
            backend={"name": "gpu-1", "description": "GPU: 2", "costScale": 2.0},
            finishedAt="2026-10-22T10:00:09Z"))
        result = dimod.SampleSet.from_samples((samples, variables), "SPIN", energy=energies)
        real = Job.result
        Job.result = property(lambda self: result)
        self.addCleanup(setattr, Job, "result", real)

    def test_result_document_and_output_file(self):
        self._completed([[1, -1], [1, 1]], [-1.0, 1.0], [1, 0])
        out_path = Path(self.tmp.name) / "result.json"
        spec = {"type": "ising", "quadratic": [[0, 1, 1.0]]}
        out = server.get_result("job-1", spec=spec, top=5, output=str(out_path))
        self.assertEqual(out["best_sample"], [-1, 1])
        self.assertEqual(out["best_objective"], -1.0)
        self.assertTrue(out["energy_check"]["checked"])
        self.assertEqual(out["job_id"], "job-1")
        self.assertNotIn("result_info", out)
        self.assertNotIn("statistics", out)
        saved = json.loads(out_path.read_text())
        self.assertEqual(saved["best_sample"], [-1, 1])
        self.assertIn("result_info", saved)
        self.assertEqual(saved["statistics"], {"usage_time": 2.5, "total_quota_usage": 584})
        self.assertEqual(saved["backend"], {"name": "gpu-1", "description": "GPU: 2"})
        self.assertEqual(saved["finished_at"], "2026-10-22T10:00:09Z")
        with self.assertRaisesRegex(ToolError, "exists; pass overwrite"):
            server.get_result("job-1", spec=spec, output=str(out_path))
        server.get_result("job-1", spec=spec, output=str(out_path), overwrite=True)

    def test_long_samples_stay_in_the_output_file(self):
        n = server.INLINE_SAMPLE_VARIABLES + 1
        self._completed([[1] * n, [-1] * n], [1.0, -1.0], list(range(n)))
        out_path = Path(self.tmp.name) / "result.json"
        out = server.get_result("job-1", spec={"type": "ising", "linear": [0.0] * (n - 1) + [1.0]},
                                top=2, output=str(out_path))
        self.assertNotIn("best_sample", out)
        self.assertEqual(out["top"], [{"objective": -1.0}, {"objective": 1.0}])
        self.assertIn(str(out_path), out["samples"])
        saved = json.loads(out_path.read_text())
        self.assertEqual(saved["best_sample"], [-1] * n)
        self.assertEqual(len(saved["top"][1]["sample"]), n)
        # Without an output file the samples stay inline.
        self.assertEqual(server.get_result("job-1")["best_sample"], [-1] * n)

    def test_result_names_the_solver(self):
        self._completed([[1, -1]], [-1.0], [0, 1])
        self.assertEqual(server.get_result("job-1")["solver"], "VeloxQ")

    def test_output_must_be_an_absolute_json_path(self):
        self._completed([[1, -1]], [-1.0], [0, 1])
        with self.assertRaisesRegex(ToolError, "`output` must be an absolute path"):
            server.get_result("job-1", output="result.json")
        with self.assertRaisesRegex(ToolError, r"`output` must end in \.json"):
            server.get_result("job-1", output=str(Path(self.tmp.name) / "x.pth"))

    def test_output_never_follows_a_symlink(self):
        self._completed([[1, -1]], [-1.0], [0, 1])
        target = Path(self.tmp.name) / "target.json"
        link = Path(self.tmp.name) / "link.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(ToolError, "exists; pass overwrite"):
            server.get_result("job-1", output=str(link))
        server.get_result("job-1", output=str(link), overwrite=True)
        self.assertFalse(target.exists())
        self.assertFalse(link.is_symlink())

    def test_output_keeps_private_permissions(self):
        self._completed([[1, -1]], [-1.0], [0, 1])
        out_path = Path(self.tmp.name) / "result.json"
        out_path.write_text("{}")
        out_path.chmod(0o600)
        server.get_result("job-1", output=str(out_path), overwrite=True)
        self.assertEqual(out_path.stat().st_mode & 0o777, 0o600)
        mask = os.umask(0o077)
        self.addCleanup(os.umask, mask)
        fresh = Path(self.tmp.name) / "fresh.json"
        server.get_result("job-1", output=str(fresh))
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o600)

    def test_output_directory_must_exist(self):
        self._completed([[1, -1]], [-1.0], [0, 1])
        missing = Path(self.tmp.name) / "missing" / "result.json"
        with self.assertRaisesRegex(ToolError, f"directory {missing.parent} does not exist"):
            server.get_result("job-1", output=str(missing))
        folder = Path(self.tmp.name) / "folder.json"
        folder.mkdir()
        with self.assertRaisesRegex(ToolError, f"{folder} is a directory"):
            server.get_result("job-1", output=str(folder), overwrite=True)
        a_file = Path(self.tmp.name) / "file"
        a_file.write_text("")
        with self.assertRaisesRegex(ToolError, f"{a_file} is not a directory"):
            server.get_result("job-1", output=str(a_file / "result.json"))
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        if not os.access(locked, os.W_OK):     # root ignores the mode
            with self.assertRaisesRegex(ToolError, f"directory {locked} is not writable"):
                server.get_result("job-1", output=str(locked / "result.json"))

    def test_output_name_may_use_the_whole_name_length(self):
        self._completed([[1, -1]], [-1.0], [0, 1])
        out_path = Path(self.tmp.name) / ("r" * 250 + ".json")
        server.get_result("job-1", output=str(out_path))
        self.assertTrue(out_path.is_file())

    def test_unfinished_and_resultless_jobs(self):
        self.api.add("GET", "/jobs/job-1", job_json(status="running"))
        with self.assertRaisesRegex(ToolError, "is running; wait"):
            server.get_result("job-1")
        self.api.add("GET", "/jobs/job-1", job_json(status="failed", statusMessage="OOM"))
        with self.assertRaisesRegex(ToolError, "failed and has no result: OOM"):
            server.get_result("job-1")

    def test_energy_mismatch_is_an_error(self):
        self._completed([[1, -1], [1, 1]], [-1.0, 5.0], [0, 1])
        with self.assertRaisesRegex(ToolError, "non-constant"):
            server.get_result("job-1", spec={"type": "ising", "quadratic": [[0, 1, 1.0]]})



class TestReport(ServerCase):
    SPEC = {"type": "ising", "quadratic": [[0, 1, 1.0]]}
    VIEW = {"title": "Two spins", "checks": [{"label": "c", "ok": True}],
            "views": [{"type": "graph", "edge_rule": "cut", "edges": [[0, 1]],
                       "nodes": [{"id": 0, "group": "a"}, {"id": 1, "group": "b"}]}]}

    def setUp(self):
        super().setUp()
        TestGetResult._completed(self, [[1, -1], [1, 1]], [-1.0, 1.0], [1, 0])

    def test_output_is_the_standalone_page(self):
        out_path = Path(self.tmp.name) / "report.html"
        out = server.report("job-1", view=self.VIEW, spec=self.SPEC, output=str(out_path))
        self.assertIsNone(out.structured_content)
        self.assertEqual(json.loads(out.content[0].text),
                         {"job_id": "job-1", "title": "Two spins", "output": str(out_path)})
        html = out_path.read_text()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertEqual(re.findall(r'data-figure="(\w+)"', html),
                         ["graph", "samples", "model", "state"])
        self.assertIn("<dt>Solver</dt><dd>VeloxQ</dd>", html)
        with self.assertRaisesRegex(ToolError, "exists; pass overwrite"):
            server.report("job-1", view=self.VIEW, output=str(out_path))
        server.report("job-1", output=str(out_path), overwrite=True)
        self.assertNotIn('data-figure="graph"', out_path.read_text())

    def test_file_arguments_must_be_absolute(self):
        for argument, value in (("output", "report.html"), ("view_path", "view.json"),
                                ("path", "problem.json")):
            with self.subTest(argument=argument):
                kw = {"output": str(Path(self.tmp.name) / "r.html"), argument: value}
                with self.assertRaisesRegex(ToolError, f"`{argument}` must be an absolute path"):
                    server.report("job-1", **kw)

    def test_output_must_be_an_html_file(self):
        target = Path(self.tmp.name) / "authorized_keys"
        with self.assertRaisesRegex(ToolError, r"`output` must end in \.html"):
            server.report("job-1", view=self.VIEW, output=str(target))
        self.assertFalse(target.exists())

    def test_overwrite_replaces_the_file_in_one_step(self):
        out_path = Path(self.tmp.name) / "report.html"
        out_path.write_text("previous")
        previous_mode = out_path.stat().st_mode & 0o777
        os.mkfifo(Path(self.tmp.name) / "fifo.html")
        server.report("job-1", output=str(out_path), overwrite=True)
        server.report("job-1", output=str(Path(self.tmp.name) / "fifo.html"), overwrite=True)
        self.assertTrue((Path(self.tmp.name) / "fifo.html").is_file())
        self.assertIn("<h1>VeloxQ solve</h1>", out_path.read_text())
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["fifo.html", "report.html"])
        self.assertEqual(out_path.stat().st_mode & 0o777, previous_mode)

    def test_view_file_must_be_small_json(self):
        out = str(Path(self.tmp.name) / "r.html")
        bad = Path(self.tmp.name) / "view.json"
        bad.write_text("{'title': 1}")
        with self.assertRaisesRegex(ToolError, f"{bad} is not UTF-8 JSON"):
            server.report("job-1", view_path=str(bad), output=out)
        with self.assertRaisesRegex(ToolError, "/dev/zero is not a regular file"):
            server.report("job-1", view_path="/dev/zero", output=out)
        utf16 = Path(self.tmp.name) / "utf16.json"
        utf16.write_text(json.dumps(self.VIEW), encoding="utf-16")
        with self.assertRaisesRegex(ToolError, f"{utf16} is not UTF-8 JSON"):
            server.report("job-1", view_path=str(utf16), output=out)
        digits = Path(self.tmp.name) / "digits.json"
        digits.write_text('{"title": ' + "9" * 5000 + "}")
        with self.assertRaisesRegex(ToolError, f"{digits} is not UTF-8 JSON"):
            server.report("job-1", view_path=str(digits), output=out)
        null = Path(self.tmp.name) / "null.json"
        null.write_text("null")
        with self.assertRaisesRegex(ToolError, f"{null} must hold a JSON object"):
            server.report("job-1", view_path=str(null), output=out)
        bom = Path(self.tmp.name) / "bom.json"
        bom.write_text(json.dumps(self.VIEW), encoding="utf-8-sig")
        server.report("job-1", view_path=str(bom), output=out, overwrite=True)
        big = Path(self.tmp.name) / "big.json"
        big.write_text(" " * (server.MAX_VIEW_BYTES + 1))
        with self.assertRaisesRegex(ToolError, "larger than"):
            server.report("job-1", view_path=str(big), output=out)

    def test_inline_view_size_is_limited(self):
        view = dict(self.VIEW, notes=["n" * 200] * (server.MAX_VIEW_BYTES // 200))
        with self.assertRaisesRegex(ToolError, "`view` is larger than"):
            server.report("job-1", view=view, output=str(Path(self.tmp.name) / "r.html"))

    def test_view_from_a_file(self):
        view_path = Path(self.tmp.name) / "view.json"
        view_path.write_text(json.dumps(self.VIEW))
        out_path = Path(self.tmp.name) / "report.html"
        server.report("job-1", view_path=str(view_path), output=str(out_path))
        self.assertIn("<h1>Two spins</h1>", out_path.read_text())
        with self.assertRaisesRegex(ToolError, "either `view` or `view_path`"):
            server.report("job-1", view=self.VIEW, view_path=str(view_path),
                          output=str(out_path), overwrite=True)

    def test_malformed_view_names_the_key(self):
        out_path = Path(self.tmp.name) / "report.html"
        with self.assertRaisesRegex(ToolError, r"^view is missing \['checks'\]"):
            server.report("job-1", view={"title": "t"}, output=str(out_path))
        self.assertFalse(out_path.exists())

    def _session(self, body, apps):
        """``body(client)`` against the server over an in-process MCP session."""
        import anyio
        from mcp import Client
        from mcp.client import advertise
        from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

        extensions = [advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})] if apps else None

        async def main():
            async with Client(server.build_server(), extensions=extensions) as client:
                return await body(client)
        return anyio.run(main)

    def test_tool_is_bound_to_the_app_resource(self):
        from mcp.server.apps import APP_MIME_TYPE

        async def body(client):
            tools = {t.name: t for t in (await client.list_tools()).tools}
            resources = (await client.list_resources()).resources
            content = (await client.read_resource(server.REPORT_URI)).contents[0]
            return tools["report"], resources, content

        tool, resources, content = self._session(body, apps=True)
        self.assertEqual(tool.meta["ui"], {"resourceUri": server.REPORT_URI})
        self.assertEqual([(str(r.uri), r.mime_type) for r in resources],
                         [(server.REPORT_URI, APP_MIME_TYPE)])
        self.assertEqual(content.text, report.app_page())

    def test_client_with_mcp_apps_gets_the_body_out_of_the_text(self):
        async def body(client):
            return await client.call_tool("report", {"job_id": "job-1", "view": self.VIEW,
                                                     "spec": self.SPEC})

        out = self._session(body, apps=True)
        self.assertFalse(out.is_error, out.content)
        self.assertEqual(out.structured_content["title"], "Two spins")
        html = out.structured_content["html"]
        self.assertTrue(html.startswith('<p class="eyebrow">'))
        self.assertIn('data-figure="model"', html)
        text = json.loads(out.content[0].text)
        self.assertEqual(text, {"job_id": "job-1", "title": "Two spins",
                                "display": server.SHOWN_BY_APP})

    def test_client_without_mcp_apps_needs_output(self):
        async def body(client):
            return await client.call_tool("report", {"job_id": "job-1"})

        out = self._session(body, apps=False)
        self.assertTrue(out.is_error)
        self.assertIn("pass `output`", out.content[0].text)


if __name__ == "__main__":
    unittest.main()
