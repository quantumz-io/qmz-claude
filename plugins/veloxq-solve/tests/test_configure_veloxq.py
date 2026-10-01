"""Tests for scripts/configure_veloxq.py and submit.py credential resolution.

configure_veloxq.py runs as a subprocess with HOME pointed at a temp
directory. submit.resolve_config is tested in-process (os.path.expanduser
re-reads HOME on every call).
"""
import contextlib
import importlib.util
import io
import os
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "veloxq-solve" / "scripts"
SCRIPT = SCRIPTS / "configure_veloxq.py"

_spec = importlib.util.spec_from_file_location("submit", SCRIPTS / "submit.py")
submit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(submit)


def run(home, token_env=None, stdin_text=""):
    env = {k: v for k, v in os.environ.items() if k != "VELOX_TOKEN"}
    env["HOME"] = str(home)
    if token_env is not None:
        env["VELOX_TOKEN"] = token_env
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text, capture_output=True, text=True, env=env,
    )


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config = self.home / ".veloxq" / "config.py"
        self.addCleanup(self._tmp.cleanup)


class TestConfigureVeloxq(TempHomeCase):
    def test_no_source_fails_fast(self):
        r = run(self.home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no credentials found", r.stderr)
        self.assertFalse(self.config.exists())

    def test_env_token_persisted_mode_600(self):
        r = run(self.home, token_env="env-tok-123")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved to", r.stdout)
        self.assertNotIn("env-tok-123", r.stdout + r.stderr)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertIn('token = "env-tok-123"', self.config.read_text())

    def test_existing_config_untouched(self):
        run(self.home, token_env="first-tok")
        before = self.config.read_text()
        r = run(self.home, token_env="second-tok")
        self.assertEqual(r.returncode, 0)
        self.assertIn("already configured", r.stdout)
        self.assertEqual(self.config.read_text(), before)

    def test_piped_stdin_saved(self):
        r = run(self.home, stdin_text="piped-tok-xyz\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("piped-tok-xyz", r.stdout + r.stderr)
        self.assertIn('token = "piped-tok-xyz"', self.config.read_text())

    @unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
    def test_interactive_prompt_masked(self):
        """Token typed at the interactive prompt is saved but never echoed."""
        parent, child = os.openpty()
        try:
            env = {k: v for k, v in os.environ.items() if k != "VELOX_TOKEN"}
            env["HOME"] = str(self.home)
            proc = subprocess.Popen(
                [sys.executable, str(SCRIPT)],
                stdin=child, stdout=child, stderr=child, env=env,
            )
            os.close(child)
            output = bytearray()
            deadline = time.monotonic() + 10
            sent = False
            while proc.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([parent], [], [], 0.2)
                if not ready:
                    continue
                try:
                    output += os.read(parent, 4096)
                except OSError:
                    break
                # Type the token only once the prompt is up (echo is off by then).
                if not sent and b"token" in output.lower():
                    os.write(parent, b"pty-tok-9\r")
                    sent = True
            self.assertEqual(proc.wait(timeout=10), 0, output.decode(errors="replace"))
        finally:
            os.close(parent)
        self.assertNotIn(b"pty-tok-9", bytes(output))
        self.assertIn('token = "pty-tok-9"', self.config.read_text())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)


class TestSubmitResolveConfig(TempHomeCase):
    def setUp(self):
        super().setUp()
        self._old_home = os.environ.get("HOME")
        self._old_token = os.environ.pop("VELOX_TOKEN", None)
        os.environ["HOME"] = str(self.home)

        def restore():
            os.environ["HOME"] = self._old_home
            if self._old_token is not None:
                os.environ["VELOX_TOKEN"] = self._old_token
        self.addCleanup(restore)

    def _write_default_config(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text('c.VeloxQAPIConfig.token = "x"\n')

    def test_nothing_available_delegates_to_await(self):
        sentinel = str(self.config)
        calls = []
        original = submit.await_credentials
        submit.await_credentials = lambda path: calls.append(path) or sentinel
        try:
            self.assertEqual(submit.resolve_config(None), sentinel)
        finally:
            submit.await_credentials = original
        self.assertEqual(calls, [str(self.config)])

    def test_await_times_out_with_waiting_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                submit.await_credentials(str(self.config), timeout=0.3, poll=0.05)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("waiting for a valid VeloxQ key", err.getvalue())
        self.assertIn("timed out", err.getvalue())

    def test_await_picks_up_config_when_it_appears(self):
        def write_later():
            time.sleep(0.3)
            self.config.parent.mkdir(parents=True)
            self.config.write_text('c.VeloxQAPIConfig.token = "x"\n')
        t = threading.Thread(target=write_later)
        t.start()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                got = submit.await_credentials(str(self.config),
                                               timeout=10, poll=0.05)
        finally:
            t.join()
        self.assertEqual(got, str(self.config))
        self.assertIn("waiting for a valid VeloxQ key", err.getvalue())

    def test_default_config_wins_over_env(self):
        self._write_default_config()
        os.environ["VELOX_TOKEN"] = "tok"
        self.assertEqual(submit.resolve_config(None), str(self.config))

    def test_env_is_last_resort(self):
        os.environ["VELOX_TOKEN"] = "tok"
        self.assertIsNone(submit.resolve_config(None))

    def test_explicit_config_wins(self):
        self._write_default_config()
        other = self.home / "other.py"
        other.write_text("")
        self.assertEqual(submit.resolve_config(str(other)), str(other))

    def test_explicit_config_missing_fails(self):
        with self.assertRaises(SystemExit):
            submit.resolve_config(str(self.home / "absent.py"))


class _V:
    def __init__(self, value):
        self.value = value


class FakeJobStatus:
    COMPLETED = _V("completed")
    FAILED = _V("failed")
    CANCELED = _V("canceled")


class FakeUpdate:
    def __init__(self, status, status_message=None):
        self.status = status
        self.status_message = status_message


class FakeJob:
    """Stand-in for a platform Job whose get_job_updates() drives its status."""

    def __init__(self, job_id, status, updates=(), final_status=None,
                 result="RESULT"):
        self.id = job_id
        self.status = status
        self._updates = list(updates)
        self._final = final_status
        self._result = result
        self.streamed = False
        self.requested_id = None

    def get_job_updates(self):
        self.streamed = True
        for u in self._updates:
            self.status = getattr(u.status, "value", u.status)
            yield u
        if self._final is not None:
            self.status = self._final

    @property
    def result(self):
        return self._result


@contextlib.contextmanager
def inject_sdk(job, record=None):
    """Put fake veloxq_sdk / veloxq_sdk.jobs modules in sys.modules."""
    pkg = types.ModuleType("veloxq_sdk")
    sub = types.ModuleType("veloxq_sdk.jobs")

    class File:
        @staticmethod
        def from_instance(instance, name=None):
            if record is not None:
                record.append(("from_instance", instance, name))
            return ("uploaded", instance)

    class Job:
        @staticmethod
        def from_id(job_id):
            job.requested_id = job_id
            return job

    pkg.File = File
    pkg.jobs = sub
    sub.Job = Job
    sub.JobStatus = FakeJobStatus
    saved = {k: sys.modules.get(k) for k in ("veloxq_sdk", "veloxq_sdk.jobs")}
    sys.modules["veloxq_sdk"] = pkg
    sys.modules["veloxq_sdk.jobs"] = sub
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class TestRunSolver(unittest.TestCase):
    """run_solver exposes the job id and streams status while waiting."""

    def test_returns_job_id_and_streams_updates(self):
        record = []
        job = FakeJob("job-xyz", "pending",
                      updates=[FakeUpdate("running", "step 1/2"),
                               FakeUpdate("completed")],
                      final_status="completed", result="RES")

        class FakeSolver:
            def submit(self, file):
                record.append(("submit", file))
                return job

        err = io.StringIO()
        with inject_sdk(job, record), contextlib.redirect_stderr(err):
            jid, res = submit.run_solver(FakeSolver(), "BQM", "myjob")

        self.assertEqual(jid, "job-xyz")
        self.assertEqual(res, "RES")
        self.assertEqual(record[0], ("from_instance", "BQM", "myjob"))
        self.assertIn(("submit", ("uploaded", "BQM")), record)
        self.assertTrue(job.streamed)
        self.assertIn("running", err.getvalue())
        self.assertIn("step 1/2", err.getvalue())


class TestResumeJob(unittest.TestCase):
    """resume_job attaches to an existing job and returns its result."""

    def test_completed_returns_result_without_streaming(self):
        job = FakeJob("job-1", "completed")
        with inject_sdk(job), contextlib.redirect_stderr(io.StringIO()):
            job_id, res = submit.resume_job("job-1")
        self.assertEqual(job_id, "job-1")
        self.assertEqual(res, "RESULT")
        self.assertFalse(job.streamed)
        self.assertEqual(job.requested_id, "job-1")

    def test_running_streams_then_returns(self):
        job = FakeJob("job-1", "running",
                      updates=[FakeUpdate("running", "3000/5000"),
                               FakeUpdate("completed")],
                      final_status="completed")
        err = io.StringIO()
        with inject_sdk(job), contextlib.redirect_stderr(err):
            job_id, res = submit.resume_job("job-1")
        self.assertTrue(job.streamed)
        self.assertEqual(res, "RESULT")
        self.assertIn("3000/5000", err.getvalue())

    def test_failed_is_fatal_and_points_to_logs(self):
        job = FakeJob("job-1", "failed")
        err = io.StringIO()
        with inject_sdk(job), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                submit.resume_job("job-1")
        self.assertEqual(cm.exception.code, 4)
        self.assertIn("failed", err.getvalue())
        self.assertIn("logs job-1", err.getvalue())
        self.assertFalse(job.streamed)

    def test_running_then_fails_is_fatal(self):
        job = FakeJob("job-1", "running",
                      updates=[FakeUpdate("running")], final_status="failed")
        err = io.StringIO()
        with inject_sdk(job), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                submit.resume_job("job-1")
        self.assertTrue(job.streamed)


if __name__ == "__main__":
    unittest.main()
