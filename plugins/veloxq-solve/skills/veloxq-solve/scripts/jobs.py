#!/usr/bin/env python3
"""Inspect VeloxQ jobs: list recent jobs, check one job's info, read its logs.

    jobs.py list   [--status S] [--created P] [--limit N] [--json]
    jobs.py info  JOB_ID [--watch] [--json]
    jobs.py logs   JOB_ID [--category C] [--time-period P] [--grep TEXT] [--json]

Credentials resolve the same way as submit.py: an explicit --config, else
~/.veloxq/config.py, else VELOX_TOKEN. Read-only against the platform.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import submit  # noqa: E402  (sibling script; resolve_config/fail live there)

# Lowercased SDK enum member names. Kept in sync with veloxq_sdk.jobs by
# test_jobs.TestChoiceListsMatchEnums, which runs when the SDK is installed.
STATUS_CHOICES = ["created", "pending", "running", "paused", "canceled",
                  "completed", "failed"]
CREATED_CHOICES = ["today", "yesterday", "last_week", "last_month",
                   "last_3_months", "last_year", "all"]
CATEGORY_CHOICES = ["info", "notice", "warning", "error", "critical", "progress"]
TIME_PERIOD_CHOICES = ["all_time", "last_hour", "last_12_hours", "last_24_hours",
                       "last_3_days", "last_week", "last_month"]

TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="veloxq_sdk config file; defaults to ~/.veloxq/config.py")
    common.add_argument("--json", action="store_true",
                        help="emit JSON instead of a human-readable table")
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("list", parents=[common], help="list recent jobs")
    pl.add_argument("--status", choices=STATUS_CHOICES,
                    help="only jobs in this status")
    pl.add_argument("--created", choices=CREATED_CHOICES,
                    help="only jobs created in this window")
    pl.add_argument("--limit", type=int, default=20,
                    help="maximum number of jobs to return (default 20)")

    ps = sub.add_parser("info", parents=[common], help="show job's detailed info")
    ps.add_argument("job_id")
    ps.add_argument("--watch", action="store_true",
                    help="stream status updates until the job reaches a terminal state")

    pg = sub.add_parser("logs", parents=[common], help="show job's logs")
    pg.add_argument("job_id")
    pg.add_argument("--category", choices=CATEGORY_CHOICES,
                    help="only log entries of this category")
    pg.add_argument("--time-period", choices=TIME_PERIOD_CHOICES,
                    default="all_time", help="restrict logs to this window")
    pg.add_argument("--grep", default=None,
                    help="only log messages containing this substring")

    return p.parse_args(argv)


def _enum_value(x):
    return getattr(x, "value", x)


def _fmt_dt(dt) -> str:
    if dt is None:
        return "-"
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except AttributeError:
        return str(dt)


def _iso(dt):
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def _fmt_timeline_value(v) -> str:
    # Aggregated durations are unitless here: the API returns values that match
    # the per-state seconds in statistics, not the hours its schema names.
    if hasattr(v, "strftime"):
        return _fmt_dt(v)
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def format_job_table(jobs) -> str:
    header = (f"{'JOB ID':<36}  {'STATUS':<10}  {'CREATED':<19}")
    lines = [header]
    for job in jobs:
        lines.append(
            f"{str(job.id):<36}  {str(_enum_value(job.status)):<10}  "
            f"{_fmt_dt(job.created_at):<19}"
        )
    if len(lines) == 1:
        lines.append("(no jobs)")
    return "\n".join(lines)


def format_info(job) -> str:
    lines = [f"job:      {job.id}",
             f"status:   {_enum_value(job.status)}"]
    msg = getattr(job, "status_message", None)
    if msg:
        lines.append(f"message:  {msg}")
    lines += [f"created:  {_fmt_dt(job.created_at)}",
              f"updated:  {_fmt_dt(getattr(job, 'updated_at', None))}"]
    stats = getattr(job, "statistics", None)
    if stats is not None:
        lines += [
            f"pending:  {getattr(stats, 'pending_time', 0.0):.1f}s",
            f"running:  {getattr(stats, 'running_time', 0.0):.1f}s",
            f"solver:   {getattr(stats, 'cost_solver_time', 0.0):.1f}s",
            f"cost:     ${getattr(stats, 'total_cost', 0.0):.4f}",
            f"quota:    {getattr(stats, 'total_quota_usage', 0)}",
        ]
    timeline = getattr(job, "timeline", None) or []
    if timeline:
        lines.append("timeline:")
        for ev in timeline:
            lines.append(f"  {str(_enum_value(ev.name)):<10} "
                         f"{_fmt_timeline_value(ev.value)}")
    return "\n".join(lines)


def format_logs(rows) -> str:
    if not rows:
        return "(no log entries)"
    return "\n".join(str(r) for r in rows)


def format_update(update) -> str:
    parts = [_fmt_dt(getattr(update, "updated_at", None)),
             str(_enum_value(update.status))]
    msg = getattr(update, "status_message", None)
    if msg:
        parts.append(str(msg))
    return "  ".join(parts)


def job_to_dict(job) -> dict:
    stats = getattr(job, "statistics", None)
    return {
        "id": job.id,
        "status": _enum_value(job.status),
        "status_message": getattr(job, "status_message", None),
        "created_at": _iso(job.created_at),
        "updated_at": _iso(getattr(job, "updated_at", None)),
        "pending_time": getattr(stats, "pending_time", None),
        "running_time": getattr(stats, "running_time", None),
        "total_cost": getattr(stats, "total_cost", None),
        "total_quota_usage": getattr(stats, "total_quota_usage", None),
    }


def timeline_to_dict(ev) -> dict:
    return {"name": _enum_value(ev.name), "value": _iso(ev.value)
            if hasattr(ev.value, "isoformat") else ev.value}


def log_to_dict(row) -> dict:
    return {"timestamp": _iso(getattr(row, "timestamp", None)),
            "category": _enum_value(row.category), "message": row.message}


def load_credentials(explicit: str | None) -> None:
    cfg = submit.resolve_config(explicit)
    if cfg:
        from veloxq_sdk.config import VeloxQAPIConfig, load_config
        load_config(cfg)
        if not VeloxQAPIConfig.instance().token:
            submit.fail(f"config file {cfg} did not set VeloxQAPIConfig.token")


def cmd_list(args: argparse.Namespace) -> None:
    load_credentials(args.config)
    from veloxq_sdk.jobs import Job, JobStatus, PeriodFilter

    kwargs = {"limit": args.limit}
    if args.status:
        kwargs["status"] = JobStatus[args.status.upper()]
    if args.created:
        kwargs["created_at"] = PeriodFilter[args.created.upper()]
    jobs = list(Job.get_jobs(**kwargs))
    if args.json:
        print(json.dumps([job_to_dict(j) for j in jobs], indent=2))
    else:
        print(format_job_table(jobs))


def cmd_info(args: argparse.Namespace) -> None:
    load_credentials(args.config)
    from veloxq_sdk.jobs import Job

    job = Job.from_id(args.job_id)
    if args.watch and _enum_value(job.status) not in TERMINAL_STATUSES:
        print(f"watching job {job.id} (Ctrl-C to stop) ...", file=sys.stderr)
        for update in job.get_job_updates():
            print(format_update(update), file=sys.stderr)
    if args.json:
        d = job_to_dict(job)
        d["timeline"] = [timeline_to_dict(ev)
                         for ev in (getattr(job, "timeline", None) or [])]
        print(json.dumps(d, indent=2))
    else:
        print(format_info(job))


def cmd_logs(args: argparse.Namespace) -> None:
    load_credentials(args.config)
    from veloxq_sdk.jobs import Job, LogCategory, TimePeriod

    category = LogCategory[args.category.upper()] if args.category else None
    time_period = TimePeriod[args.time_period.upper()]
    rows = Job.from_id(args.job_id).get_job_logs(
        category=category, time_period=time_period, msg=args.grep)
    if args.json:
        print(json.dumps([log_to_dict(r) for r in rows], indent=2))
    else:
        print(format_logs(rows))


def main() -> None:
    args = parse_args()
    {"list": cmd_list, "info": cmd_info, "logs": cmd_logs}[args.command](args)


if __name__ == "__main__":
    main()
