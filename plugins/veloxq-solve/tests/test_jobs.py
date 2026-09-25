"""Tests for scripts/jobs.py.

Formatting and argument parsing are exercised in-process with duck-typed
stand-ins, so the suite runs without the veloxq_sdk venv. The choice lists
that mirror the SDK enums are checked against the real enums only when the
SDK is importable.
"""
import importlib.util
import re
import types
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "veloxq-solve" / "scripts"

_spec = importlib.util.spec_from_file_location("jobs", SCRIPTS / "jobs.py")
jobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jobs)


def ns(**kw):
    return types.SimpleNamespace(**kw)


def fake_stats(**kw):
    base = dict(pending_time=1.0, running_time=2.5, cost_solver_time=2.0,
                total_cost=0.0123, total_quota_usage=7)
    base.update(kw)
    return ns(**base)


def fake_job(**kw):
    base = dict(
        id="job-abc",
        status="running",
        status_message=None,
        created_at=datetime(2026, 9, 24, 10, 0, 0),
        updated_at=datetime(2026, 9, 24, 10, 5, 0),
        statistics=fake_stats(),
        timeline=[],
    )
    base.update(kw)
    return ns(**base)


class TestEnumValue(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(jobs._enum_value("running"), "running")

    def test_enum_like(self):
        self.assertEqual(jobs._enum_value(ns(value="completed")), "completed")


class TestFormatting(unittest.TestCase):
    def test_job_table_has_header_and_row(self):
        out = jobs.format_job_table([fake_job()])
        self.assertIn("STATUS", out)
        self.assertIn("job-abc", out)
        self.assertIn("running", out)

    def test_job_table_empty(self):
        self.assertIn("no jobs", jobs.format_job_table([]).lower())

    def test_info_shows_stats_and_message(self):
        job = fake_job(status_message="queued behind 2 jobs")
        out = jobs.format_info(job)
        self.assertIn("job-abc", out)
        self.assertIn("queued behind 2 jobs", out)
        self.assertIn("running", out)
        self.assertIn("0.0123", out)

    def test_info_timeline(self):
        job = fake_job(timeline=[ns(name="pending", value=datetime(2026, 9, 24, 10, 1)),
                                 ns(name="running", value=0.5)])
        out = jobs.format_info(job)
        self.assertIn("timeline", out.lower())
        self.assertIn("pending", out)
        self.assertIn("running", out)

    def test_logs_render_via_str(self):
        class Row:
            def __str__(self):
                return "2026-09-24 10:02:00 [ERROR] boom"
        out = jobs.format_logs([Row()])
        self.assertIn("ERROR", out)
        self.assertIn("boom", out)

    def test_logs_empty(self):
        self.assertIn("no log", jobs.format_logs([]).lower())

    def test_update_line(self):
        upd = ns(status="running", status_message="step 3000/5000",
                 updated_at=datetime(2026, 9, 24, 10, 3), finished=False)
        line = jobs.format_update(upd)
        self.assertIn("running", line)
        self.assertIn("step 3000/5000", line)


class TestDicts(unittest.TestCase):
    def test_job_to_dict(self):
        d = jobs.job_to_dict(fake_job())
        self.assertEqual(d["id"], "job-abc")
        self.assertEqual(d["status"], "running")
        self.assertEqual(d["total_cost"], 0.0123)
        self.assertTrue(d["created_at"].startswith("2026-09-24"))

    def test_log_to_dict(self):
        row = ns(timestamp=datetime(2026, 9, 24), category=ns(value="INFO"),
                 message="hi")
        d = jobs.log_to_dict(row)
        self.assertEqual(d["category"], "INFO")
        self.assertEqual(d["message"], "hi")


class TestParseArgs(unittest.TestCase):
    def test_list_defaults(self):
        a = jobs.parse_args(["list"])
        self.assertEqual(a.command, "list")
        self.assertEqual(a.limit, 20)

    def test_list_filters(self):
        a = jobs.parse_args(["list", "--status", "failed", "--created", "today",
                             "--limit", "5", "--json"])
        self.assertEqual(a.status, "failed")
        self.assertEqual(a.created, "today")
        self.assertEqual(a.limit, 5)
        self.assertTrue(a.json)

    def test_info_requires_id(self):
        with self.assertRaises(SystemExit):
            jobs.parse_args(["info"])

    def test_info_watch(self):
        a = jobs.parse_args(["info", "job-1", "--watch"])
        self.assertEqual(a.job_id, "job-1")
        self.assertTrue(a.watch)

    def test_logs_filters(self):
        a = jobs.parse_args(["logs", "job-1", "--category", "error",
                             "--time-period", "last_hour", "--grep", "fail"])
        self.assertEqual(a.category, "error")
        self.assertEqual(a.time_period, "last_hour")
        self.assertEqual(a.grep, "fail")

    def test_bad_status_choice(self):
        with self.assertRaises(SystemExit):
            jobs.parse_args(["list", "--status", "nope"])


def _sdk_available():
    return importlib.util.find_spec("veloxq_sdk") is not None


@unittest.skipUnless(_sdk_available(), "veloxq_sdk not importable")
class TestChoiceListsMatchEnums(unittest.TestCase):
    """The hardcoded CLI choices must stay in step with the SDK enums."""

    def test_choices(self):
        from veloxq_sdk.jobs import (
            JobStatus,
            LogCategory,
            PeriodFilter,
            TimePeriod,
        )
        self.assertEqual(sorted(jobs.STATUS_CHOICES),
                         sorted(m.name.lower() for m in JobStatus))
        self.assertEqual(sorted(jobs.CREATED_CHOICES),
                         sorted(m.name.lower() for m in PeriodFilter))
        self.assertEqual(sorted(jobs.CATEGORY_CHOICES),
                         sorted(m.name.lower() for m in LogCategory))
        self.assertEqual(sorted(jobs.TIME_PERIOD_CHOICES),
                         sorted(m.name.lower() for m in TimePeriod))
        for name in jobs.STATUS_CHOICES:
            self.assertIn(jobs._enum_value(JobStatus[name.upper()]),
                          [m.value for m in JobStatus])


class TestSkillDocMatchesChoices(unittest.TestCase):
    """SKILL.md enumerates each flag's values inline; they must match the CLI."""

    def test_documented_flag_values(self):
        text = (SCRIPTS.parent / "SKILL.md").read_text()
        documented = dict(re.findall(
            r"`--(status|created|category|time-period)`\s*\(([^)]*)\)", text))
        expected = {
            "status": jobs.STATUS_CHOICES,
            "created": jobs.CREATED_CHOICES,
            "category": jobs.CATEGORY_CHOICES,
            "time-period": jobs.TIME_PERIOD_CHOICES,
        }
        self.assertEqual(set(documented), set(expected))
        for flag, choices in expected.items():
            values = [v.strip() for v in documented[flag].split(",")]
            self.assertEqual(values, choices,
                             f"--{flag} values documented in SKILL.md")


if __name__ == "__main__":
    unittest.main()
