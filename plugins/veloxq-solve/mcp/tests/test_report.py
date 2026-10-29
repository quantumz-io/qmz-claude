"""Tests for veloxq_mcp.report: the view spec, the figures and the page.

Figures that need a computed layout use numpy, which ships with the server's
venv; those tests are skipped without it.
"""
import glob
import html
import html as html_lib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from veloxq_mcp import report  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
if HAVE_NUMPY:
    from veloxq_mcp.models import model_from_spec
    from veloxq_mcp.results import objective_distribution
needs_numpy = unittest.skipUnless(HAVE_NUMPY, "numpy is not installed")

EDGES = [[0, 1], [1, 2], [2, 0], [2, 3]]


def graph_view(groups, **extra):
    v = {"type": "graph", "edges": EDGES,
         "nodes": [{"id": i, "group": g} for i, g in enumerate(groups)]}
    v.update(extra)
    return v


def page(view=None, result=None, model=None):
    return report.page(*report.render(view, result, model))


def figures(html):
    """Kinds of the figures drawn on a page, in page order."""
    return re.findall(r'<svg class="vq-fig" data-figure="(\w+)"', html)


def share_tables(html):
    """Per figure, the tables its Share menu offers as CSV, as rows of cell texts."""
    return [[[[t["s"][k] for k in row] for row in t["r"]] for t in json.loads(html_lib.unescape(d))]
            for d in re.findall(r'<details class="vq-share" data-csv="([^"]*)">', html)]


def legend_class(html, label):
    """Color class of the first legend entry whose label is ``label (count)``."""
    found = re.search(r'class="(vq-\w+)"/><text [^>]*>' + re.escape(label) + r' \(', html) \
        or re.search(r'class="(vq-\w+)"/><text [^>]*>' + re.escape(label) + r'<', html)
    return found and found.group(1)


def strip_legend(html):
    """The legend of the best-sample strip: its SVG up to the first cell."""
    return html.split('data-figure="state"')[1].split('class="vq-m')[0]


def ising_result(sample, objective=-2.0, **extra):
    r = {"type": "ising", "vartype": "SPIN", "num_variables": len(sample),
         "num_samples": 10, "best_objective": objective, "best_sample": sample,
         "num_samples_at_best": 7,
         "top": [{"sample": sample, "objective": objective}],
         "solver": "veloxq", "job_id": "job-1", "result_info": {"num_rep": "10"}}
    r.update(extra)
    return r


def qubo_result(sample, objective):
    return ising_result(sample, objective, type="qubo", vartype="BINARY")


class TestSpecValidation(unittest.TestCase):
    def test_unknown_key_names_the_key_and_the_allowed_ones(self):
        with self.assertRaisesRegex(
                report.ReportError, r"unknown keys \['node'\].*nodes.*report-views\.md"):
            report.view_graph({"type": "graph", "node": [0], "nodes": [0],
                               "edges": []}, "view.views[0]")

    def test_missing_checks_is_an_error(self):
        with self.assertRaisesRegex(report.ReportError, r"view is missing \['checks'\]"):
            page({"title": "t"})

    def test_check_ok_must_be_boolean(self):
        with self.assertRaisesRegex(report.ReportError, "ok must be true or false"):
            page({"title": "t", "checks": [{"label": "c", "ok": "yes"}]})

    def test_edge_to_unknown_node(self):
        with self.assertRaisesRegex(report.ReportError, "unknown node 9"):
            report.view_graph({"type": "graph", "nodes": [0, 1],
                               "edges": [[0, 9]]}, "v")

    def test_duplicate_node_ids(self):
        with self.assertRaisesRegex(report.ReportError, "duplicate ids"):
            report.view_graph({"type": "graph", "nodes": [0, 0], "edges": []}, "v")

    def test_unknown_view_type_lists_known_types(self):
        with self.assertRaisesRegex(report.ReportError, "known types.*graph"):
            page({"title": "t", "checks": [], "views": [{"type": "pie"}]})

    def test_non_finite_number(self):
        with self.assertRaisesRegex(report.ReportError, "finite number"):
            report.view_bars({"type": "bars", "items": [
                {"label": "a", "value": float("nan")}]}, "v")

    def test_graph_over_node_limit(self):
        nodes = list(range(report.MAX_NODES + 1))
        with self.assertRaisesRegex(report.ReportError, "limit is 500 nodes"):
            report.view_graph({"type": "graph", "nodes": nodes, "edges": []}, "v")

    def test_graph_over_group_limit(self):
        with self.assertRaisesRegex(report.ReportError, "9 groups"):
            report.view_graph(
                {"type": "graph", "edges": [],
                 "nodes": [{"id": i, "group": f"g{i}"} for i in range(9)]}, "v")

    def test_grid_over_cell_limit(self):
        with self.assertRaisesRegex(report.ReportError, "limit is 40000"):
            report.view_grid({"type": "grid", "cells": [[0] * 201] * 200}, "v")

    def test_ragged_grid(self):
        with self.assertRaisesRegex(report.ReportError, "rectangular"):
            report.view_grid({"type": "grid", "cells": [[0, 1], [0]]}, "v")

    def test_lane_limit(self):
        items = [{"label": str(i), "value": 1} for i in range(report.MAX_LANES + 1)]
        with self.assertRaisesRegex(report.ReportError, "limit is 60"):
            report.view_bars({"type": "bars", "items": items}, "v")

    def test_wrong_types_fail_with_the_key_named(self):
        cases = [
            (report.view_graph, {"type": "graph", "nodes": [0], "edges": [],
                                 "groups": None}, r"v\.groups must be a list"),
            (report.view_graph, {"type": "graph", "nodes": [{"id": [1]}],
                                 "edges": []}, r"id must be an integer or a string"),
            (report.view_graph, {"type": "graph", "nodes": [{"id": 0, "group": [1]}],
                                 "edges": []}, r"group must be text or an integer"),
            (report.view_graph, {"type": "graph", "nodes": [0, 1],
                                 "edges": [[0, 1], [1, 0]]}, "repeats the edge"),
            (report.view_route, {"type": "route", "points": [0, 1],
                                 "routes": [{"order": [0, 1], "closed": "false"}]},
             "closed must be true or false"),
            (report.view_route, {"type": "route", "points": [0, 1],
                                 "routes": [{"order": [0, 1]}],
                                 "distances": [[0, None], [None, 0]]},
             r"distances\[0\]\[1\] must be a finite number"),
            (report.view_route, {"type": "route",
                                 "points": [{"id": 0, "group": "a"}, 1],
                                 "routes": [{"order": [0, 1]}]}, r"unknown keys \['group'\]"),
            (report.view_bars, {"type": "bars", "items": [
                {"label": "a", "value": 1, "highlight": "no"}]},
             "highlight must be true or false"),
            (report.view_grid, {"type": "grid", "cells": [[0, 1]],
                                "violations": [[0.9, 1.0]]}, "integers inside the grid"),
            (report.view_grid, {"type": "grid", "cells": [[None, None]]},
             "every cell is null"),
            (report.view_grid, {"type": "grid", "cells": [[0] * 201]}, "200 columns"),
            (report.view_grid, {"type": "grid", "cells": [[0]] * 1001}, "1000 rows"),
            (report.view_table, {"type": "table", "columns": ["a"], "rows": [[1]],
                                 "status": 5}, "one true/false/null per row"),
        ]
        for render, spec, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(report.ReportError, message):
                    render(spec, "v")

    def test_route_and_bar_counts_are_limited(self):
        with self.assertRaisesRegex(report.ReportError, "more than 5000 legs"):
            report.view_route({"type": "route", "points": [0, 1],
                               "routes": [{"order": [0, 1] * 3000}]}, "v")
        items = [{"label": "i", "size": 1}] * (report.MAX_SEGMENTS + 1)
        with self.assertRaisesRegex(report.ReportError, "limit is 2000"):
            report.view_bins({"type": "bins",
                              "bins": [{"label": "A", "items": items}]}, "v")

    def test_malformed_distribution_is_refused(self):
        with self.assertRaisesRegex(report.ReportError, "counts must be non-empty"):
            page(result=ising_result([1, -1], objective_distribution={
                "values": [0, 1], "counts": [0, 0]}))


@needs_numpy
class TestGraph(unittest.TestCase):
    def test_cut_rule_highlights_edges_between_groups(self):
        fig = report.view_graph(graph_view(["a", "b", "a", "b"], edge_rule="cut"), "v")
        self.assertIn("cut edge (3)", fig.body)
        self.assertIn("uncut edge (1)", fig.body)

    def test_conflict_rule_flags_edges_within_a_group(self):
        fig = report.view_graph(
            graph_view(["a", "b", "a", "b"], edge_rule="conflict"), "v")
        self.assertIn("conflict (1)", fig.body)
        self.assertIn("edge (3)", fig.body)

    def test_ungrouped_nodes_never_conflict(self):
        fig = report.view_graph(
            graph_view(["in", None, None, "in"], edge_rule="conflict"), "v")
        self.assertNotIn("conflict (", fig.body)

    def test_explicit_status_overrides_the_rule(self):
        v = graph_view(["a", "a", "a", "a"], edge_rule="cut")
        v["edges"] = [{"u": 0, "v": 1, "status": "violated"}]
        self.assertIn("violated edge (1)", report.view_graph(v, "v").body)

    def test_group_named_after_a_color_takes_that_color(self):
        self.assertEqual(report.group_slots(["red", "green", "blue"]),
                         {"red": 8, "green": 6, "blue": 1})

    def test_other_group_names_take_slots_in_order(self):
        self.assertEqual(report.group_slots(["red", "left"]), {"red": 1, "left": 2})

    def test_layout_is_deterministic(self):
        v = graph_view(["a", "b", "a", "b"])
        self.assertEqual(report.view_graph(v, "v").body, report.view_graph(v, "v").body)

    def test_single_node_and_edgeless_graphs_render(self):
        report.view_graph({"type": "graph", "nodes": [0], "edges": []}, "v")
        report.view_graph({"type": "graph", "nodes": [0, 1, 2], "edges": []}, "v")

    def test_route_reports_length_from_distances(self):
        fig = report.view_route({"type": "route", "points": [0, 1, 2],
                                 "routes": [{"order": [0, 2, 1]}],
                                 "distances": [[0, 1, 2], [1, 0, 4], [2, 4, 0]]}, "v")
        self.assertIn("length 7", fig.body)
        self.assertIn("approximate", fig.caption)

    def test_route_rejects_wrong_distance_shape(self):
        with self.assertRaisesRegex(report.ReportError, "must be a 3x3 matrix"):
            report.view_route({"type": "route", "points": [0, 1, 2],
                               "routes": [{"order": [0, 1]}],
                               "distances": [[0, 1], [1, 0]]}, "v")


@needs_numpy
class TestDocumentedExamples(unittest.TestCase):
    """Every JSON example in the skill's docs must be accepted as written."""

    SKILL = REPO / "skills" / "veloxq-solve"

    def blocks(self, path, containing=""):
        found = re.findall(r"```json\n(.*?)```", path.read_text(), re.S)
        return [json.loads(b) for b in found if containing in b]

    def test_reference_examples_render(self):
        blocks = self.blocks(self.SKILL / "references" / "report-views.md")
        shown = [b for b in blocks if "type" in b]
        self.assertEqual(sorted(f["type"] for f in shown),
                         sorted(list(report.VIEWS) + ["table"]))
        top = next(b for b in blocks if "checks" in b)
        self.assertIn("Feasible: all 1", page(top))
        # The figure examples draw violations, so the page needs a failed check.
        failed = {"label": "example constraint", "ok": False}
        html = page(dict(top, views=shown, checks=[failed]))
        self.assertEqual(sorted(figures(html)), sorted(report.VIEWS))
        self.assertIn("Infeasible: 1 of 1", html)

    def test_skill_example_renders(self):
        text = (self.SKILL / "SKILL.md").read_text()
        code = [b for b in re.findall(r"```python\n(.*?)```", text, re.S)
                if "view = {" in b]
        self.assertEqual(len(code), 1)
        scope = {"edges": [(0, 1), (1, 2), (0, 2)], "color": [0, 1, 1]}
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, cwd)
            exec(code[0], scope)
            view = json.loads(Path("view.json").read_text())
        html = page(view)
        self.assertEqual(figures(html), ["graph"])
        self.assertIn("Infeasible: 1 of 1", html)
        self.assertIn("conflict (1)", html)


class TestLanesAndGrid(unittest.TestCase):
    def test_bin_over_capacity_is_flagged(self):
        fig = report.view_bins({"type": "bins", "capacity": 10, "bins": [
            {"label": "A", "items": [{"label": "x", "size": 12}]},
            {"label": "B", "items": [{"label": "y", "size": 9}]}]}, "v")
        self.assertIn("12 / 10", fig.body)
        self.assertEqual(fig.body.count('class="vq-crit"'), 1)

    def test_negative_item_size_is_rejected(self):
        with self.assertRaisesRegex(report.ReportError, "size is negative"):
            report.view_bins({"type": "bins", "bins": [
                {"label": "A", "items": [{"label": "x", "size": -1}]}]}, "v")

    def test_schedule_task_ending_before_start(self):
        with self.assertRaisesRegex(report.ReportError, "ends before it starts"):
            report.view_schedule({"type": "schedule", "rows": [{"label": "m", "tasks": [
                {"label": "t", "start": 3, "end": 1}]}]}, "v")

    def test_grid_kinds(self):
        def legend_of(cells):
            return report.view_grid({"type": "grid", "cells": cells}, "v").body

        self.assertIn(">1</text>", legend_of([[0, 1]]))
        self.assertIn(">+1</text>", legend_of([[-1, 1]]))
        self.assertIn(">2.5</text>", legend_of([[0, 2.5]]))

    def test_grid_violation_must_be_inside(self):
        with self.assertRaisesRegex(report.ReportError, "inside the grid"):
            report.view_grid({"type": "grid", "cells": [[0, 1]],
                              "violations": [[0, 5]]}, "v")

    def test_table_row_width_and_status(self):
        with self.assertRaisesRegex(report.ReportError, "list of 2 values"):
            report.view_table({"type": "table", "columns": ["a", "b"],
                               "rows": [[1]]}, "v")
        out, bad = report.view_table({"type": "table", "columns": ["a"],
                                      "rows": [[1], [2]], "status": [True, False]}, "v")
        self.assertEqual(out.count('class="violated"'), 1)
        self.assertEqual(bad, 1)


class TestPage(unittest.TestCase):
    def test_feasibility_follows_the_checks(self):
        ok = {"label": "c", "ok": True}
        bad = {"label": "d", "ok": False, "detail": "2 violated"}
        self.assertIn("Feasible: all 1", page({"title": "t", "checks": [ok]}))
        html = page({"title": "t", "checks": [ok, bad]})
        self.assertIn("Infeasible: 1 of 2", html)
        self.assertNotIn("Feasible: all", html)
        self.assertIn("No hard constraints", page({"title": "t", "checks": []}))

    def test_drawn_violation_needs_a_failed_check(self):
        over = {"type": "bins", "capacity": 5,
                "bins": [{"label": "A", "items": [{"label": "x", "size": 9}]}]}
        cell = {"type": "grid", "cells": [[0, 1]], "violations": [[0, 1]]}
        row = {"type": "table", "columns": ["a"], "rows": [[1]], "status": [False]}
        task = {"type": "schedule", "rows": [{"label": "m", "tasks": [
            {"label": "t", "start": 0, "end": 1, "status": "violated"}]}]}
        passing = {"label": "c", "ok": True}
        for fig in (over, cell, row, task):
            for checks in ([], [passing]):
                with self.subTest(type=fig["type"], checks=len(checks)):
                    with self.assertRaisesRegex(report.ReportError, "no entry of view.checks"):
                        page({"title": "t", "checks": checks, "views": [fig]})
            html = page({"title": "t", "views": [fig],
                            "checks": [passing, {"label": "d", "ok": False}]})
            self.assertIn("Infeasible: 1 of 2", html)

    def test_footer_claims_recomputation_only_with_a_result(self):
        self.assertNotIn("recomputed locally", page({"title": "t", "checks": []}))
        self.assertIn("recomputed locally", page(result=ising_result([1, -1])))

    def test_numbers_keep_their_integer_digits(self):
        self.assertEqual(report.fmt(-148327085.5), "-148,327,085.5")
        self.assertEqual(report.fmt(1234567.89), "1,234,567.89")
        self.assertEqual(report.fmt(2.0909090909), "2.09091")
        self.assertEqual(report.fmt(12.0), "12")

    def test_control_characters_are_dropped(self):
        html = page({"title": "t", "checks": [], "views": [{
            "type": "bars", "title": "a\x00b", "items": [{"label": "x\x01", "value": 1}]}]})
        self.assertNotRegex(html, "[\x00-\x08]")
        self.assertIn("<h3>ab</h3>", html)

    def test_figures_are_wellformed_inline_svg(self):
        html = self.full_page() + page(result=ising_result([1, -1]))
        drawn = re.findall(r'<svg class="vq-fig".*?</svg>', html, re.S)
        self.assertGreaterEqual(len(drawn), 4)
        for svg in drawn:
            ET.fromstring(svg)

    def test_schedule_axis_and_ungrouped_tasks(self):
        fig = report.view_schedule({"type": "schedule", "rows": [{"label": "m", "tasks": [
            {"label": "a", "start": 1700000000, "end": 1700003600, "group": "g"},
            {"label": "b", "start": 1700003600, "end": 1700007200}]}]}, "v")
        widths = [float(b) - float(a) for a, b in
                  re.findall(r'<path d="M([\d.]+),[\d.]+H([\d.]+)', fig.body)]
        self.assertEqual(len(widths), 2)
        self.assertTrue(all(w > 100 for w in widths), widths)
        self.assertIn('class="vq-m vq-mute"', fig.body)

    def test_grid_text_and_violation_reach_the_tooltip(self):
        fig = report.view_grid({"type": "grid", "cells": [[1] + [0] * 99],
                                "rows": ["only row"], "text": [["Q"] + [""] * 99],
                                "violations": [[0, 0]]}, "v")
        self.assertIn("only row · column 0 · 1 · Q · violated", fig.body)

    def full_page(self):
        view = {"title": "Rack loads", "checks": [{"label": "c", "ok": True}],
                "summary": [{"label": "Imbalance", "value": 3}],
                "views": [{"type": "bars", "items": [{"label": "a", "value": 1}]},
                          {"type": "grid", "cells": [[0, 1], [-2.5, 3]]},
                          {"type": "table", "columns": ["a"], "rows": [[1]]}]}
        result = ising_result([1, -1], objective_distribution={
            "values": [-2.0, 0.0], "counts": [7, 3]})
        return page(view, result)

    def test_page_loads_nothing_from_outside(self):
        html = self.full_page()
        link = ('<a href="https://web.veloxq.com/jobs/job-1" target="_blank" '
                'rel="noopener noreferrer">job-1</a>')
        self.assertEqual(html.count(link), 1)
        rest = html.replace(link, "")
        for needle in ("http:", "https:", "//", "src=", "href=", "url(", "@import",
                       "<link", "<img", "<iframe", "<object"):
            self.assertNotIn(needle, rest)

    STATS = {"usage_time": 37.972596, "pending_time": 18.912194, "running_time": 15.078,
             "quota_solver_time": 15.078, "cost_solver_time": 0.0, "solver_cost": 0.55,
             "solver_usage_scale": 1.0, "backend_cost_scale": 2.0,
             "backend_usage_scale": 3.0, "total_cost": 0.0123, "total_quota_usage": 30156}

    def run_details(self, **extra):
        html = page(None, ising_result([1, -1], **extra))
        return html.split("<h2>Run details</h2>")[1].split("<footer>")[0]

    def test_run_details_split_into_a_job_and_a_usage_card(self):
        details = self.run_details(
            statistics=self.STATS, finished_at="2026-10-23T18:22:27.267882Z",
            backend={"name": "veloxq_h100_2", "description": "GPU: 2"})
        job, usage = details.split("<h3>Usage and cost</h3>")
        self.assertIn("<h3>Job</h3>", job)
        for row in ('<dt>Solver</dt><dd>veloxq</dd>',
                    '<dt>Backend</dt><dd>veloxq_h100_2<span class="sub">GPU: 2</span></dd>',
                    '<dt>Model</dt><dd>Ising<span class="sub">2 spin variables</span></dd>',
                    '<dt>Samples</dt><dd>10<span class="sub">7 at the best value</span></dd>',
                    '<dt>Finished</dt><dd>2026-10-23 18:22 UTC</dd>'):
            self.assertIn(row, job)
        for label, value, note in (
                ("API time", "38 s", "18.9 s pending, 15.1 s running"),
                ("Solver time", "15.1 s", "0 s billed"),
                ("Quota used", "30,156 ms", None),
                ("Cost", "$0.012", "at $0.55/s")):
            self.assertIn(f'<div class="label">{label}</div><div class="value">{value}</div>'
                          + (f'<div class="note">{note}</div>' if note else "</div>"), usage)
        self.assertIn('<dt>Solver multiplier</dt><dd>×1<span class="sub">quota</span></dd>',
                      usage)
        self.assertIn('<dt>Backend multiplier</dt><dd>×2 cost, ×3 quota</dd>', usage)

    def test_equal_backend_scales_read_as_one_multiplier(self):
        details = self.run_details(statistics=dict(self.STATS, backend_usage_scale=2.0))
        self.assertIn('<dt>Backend multiplier</dt><dd>×2<span class="sub">cost and quota'
                      '</span></dd>', details)

    def test_no_statistics_no_usage_card(self):
        details = self.run_details()
        self.assertIn("<h3>Job</h3>", details)
        self.assertNotIn("Usage and cost", details)

    def test_durations_and_money_read_naturally(self):
        for seconds, text in ((0, "0 s"), (0.292, "292 ms"), (2.530341, "2.53 s"),
                              (75.2, "1 min 15 s"), (3725, "1 h 2 min")):
            self.assertEqual(report.duration(seconds), text)
        for usd, text in ((0, "$0.00"), (0.0123, "$0.012"), (12.5, "$12.50"),
                          (1234.5, "$1,234.50")):
            self.assertEqual(report.dollars(usd), text)

    def test_solver_metadata_is_a_closed_section_of_the_run_details(self):
        details = self.run_details()
        found = re.search(r'<details( open)? class="card meta"><summary>Solver metadata'
                          r"</summary><dl>(.*?)</dl></details>", details)
        self.assertIsNotNone(found)
        self.assertIsNone(found.group(1))
        self.assertEqual(found.group(2), "<dt>num_rep</dt><dd>10</dd>")
        self.assertNotIn("Solver num_rep", details)

    def test_no_solver_metadata_no_section(self):
        self.assertNotIn("Solver metadata", self.run_details(result_info={}))

    def test_page_is_a_bare_document_with_an_early_title(self):
        html = self.full_page()
        self.assertTrue(html.startswith("<!doctype html>"))
        for tag in ("<html", "<head", "<body"):
            self.assertNotIn(tag, html)
        self.assertIn("<title>Rack loads</title>", html[:8192])

    def test_every_color_follows_the_theme_setting(self):
        html = self.full_page()
        style = html[html.index("<style>"):html.index("</style>")]
        light = re.match(r"<style>:root\{(.*?)\}", style).group(1)
        defined = set(re.findall(r"(--[\w-]+):", light))
        self.assertEqual(set(re.findall(r"var\((--[\w-]+)\)", style)) - defined, set())
        dark = re.findall(r"\{((?:--[\w-]+:[^;{}]+;)+color-scheme:dark)\}", style)
        self.assertEqual(len(dark), 2)       # system-dark block and explicit-dark block
        self.assertEqual(dark[0], dark[1])
        self.assertEqual(set(re.findall(r"(--[\w-]+):", dark[0])), defined)
        self.assertIn('@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){',
                      style)
        self.assertIn(':root[data-theme="dark"]{', style)
        rules = style[style.index("*{box-sizing"):]
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", rules), [])
        self.assertIn("body{margin:0;background:var(--plane)", rules)

    @staticmethod
    def documented_view():
        """Every documented figure example in one view, with every top-level key."""
        doc = (REPO / "skills" / "veloxq-solve" / "references" / "report-views.md").read_text()
        examples = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", doc, re.S)]
        return {"title": "t", "subtitle": "s", "notes": ["n"],
                "summary": [{"label": "l", "value": "v", "note": "n"}],
                "checks": [{"label": "c", "ok": False, "detail": "d"}],
                "views": [b for b in examples if "type" in b]}

    @staticmethod
    def string_paths(x, at=()):
        if isinstance(x, str):
            yield at
        elif isinstance(x, dict):
            for k, v in x.items():
                yield from TestPage.string_paths(v, at + (k,))
        elif isinstance(x, list):
            for k, v in enumerate(x):
                yield from TestPage.string_paths(v, at + (k,))

    @staticmethod
    def replaced(base, at, value):
        view = json.loads(json.dumps(base))
        node = view
        for k in at[:-1]:
            node = node[k]
        node[at[-1]] = value
        return view

    def test_every_string_field_is_escaped(self):
        """Each string of every documented example, replaced by markup that
        breaks out of text and attributes, stays inert."""
        evil = '"><img src=x onerror=alert(1)>\n<script>alert(2)</script>'
        base = self.documented_view()
        drawn = 0
        for at in self.string_paths(base):
            view = self.replaced(base, at, evil)
            try:
                html = page(view)
            except report.ReportError:
                continue           # an enumerated value such as a figure type
            drawn += 1
            with self.subTest(at=at):
                body = html[html.index("<main>"):html.index("</main>")]
                self.assertNotIn("<img", body)
                self.assertNotIn("<script", body)
                self.assertNotIn("\n", body)
        self.assertGreater(drawn, 40)

    def test_text_is_never_a_list(self):
        """A list where text belongs would be copied into every tooltip and
        table row; every documented string refuses one."""
        base = self.documented_view()
        bomb = ["x" * report.MAX_TEXT] * 50
        for at in self.string_paths(base):
            with self.subTest(at=at):
                with self.assertRaises(report.ReportError):
                    page(self.replaced(base, at, bomb))

    def test_view_shape_is_bounded_before_drawing(self):
        """Oversized lists, deep nesting and long numbers are refused before
        any figure is built, each within a fraction of a second."""
        long_list = list(range(report.MAX_VIEW_VALUES + 1))
        deep = "n"
        for _ in range(report.MAX_DEPTH + 1):
            deep = [deep]
        cases = [
            ({"title": "t", "checks": [], "notes": long_list}, "more than"),
            ({"title": "t", "checks": [], "notes": [deep]}, "nested deeper than"),
            ({"title": "t", "checks": [], "views": [
                {"type": "graph", "nodes": [10 ** 400], "edges": []}]}, "longer than"),
            ({"title": "t", "checks": [], "views": [{"type": "bars", "unit": "😀" * 300, "items": [
                {"label": 0, "value": 0}] * 5000}]}, "rows; the limit is"),
            ({"title": "t", "checks": [], "views": [{"type": "schedule", "groups": list(range(9)),
                "rows": [{"label": "r", "tasks": []}]}]}, "groups; at most"),
            ({"title": "t", "checks": [], "views": [{"type": "schedule", "rows": [
                {"label": "r", "tasks": [{"label": 0, "start": 0, "end": 1, "group": k}
                                         for k in range(5000)]}]}]}, "bars; the limit is"),
            ({"title": "t", "checks": [], "views": [{"type": "bins", "bins": [
                {"label": "b", "items": [{"label": 0, "size": 1}] * 5000}]}]}, "bars; the limit is"),
            ({"title": "t", "checks": [], "views": [{"type": "table",
                "columns": list(range(report.MAX_TABLE_COLUMNS + 1)), "rows": []}]}, "columns; the limit is"),
            ({"title": "t", "checks": [], "notes": ["n"] * (report.MAX_NOTES + 1)}, "notes; the limit is"),
            ({"title": "t", "checks": [{"label": "c", "ok": True}] * (report.MAX_CHECKS + 1)},
             "checks; the limit is"),
            ({"title": "t", "checks": [], "summary": [{"label": "l", "value": 1}] * (report.MAX_TILES + 1)},
             "summary; the limit is"),
        ]
        for view, message in cases:
            with self.subTest(message=message):
                started = time.process_time()
                with self.assertRaisesRegex(report.ReportError, message):
                    page(view)
                self.assertLess(time.process_time() - started, 0.5)

    def test_error_messages_are_clipped(self):
        bomb = ["\U000F0000" * report.MAX_TEXT] * 300
        with self.assertRaises(report.ReportError) as caught:
            page({"title": "t", "checks": [], "views": [{"type": bomb}]})
        self.assertLessEqual(len(str(caught.exception)), report.MAX_MESSAGE)

    def test_text_is_never_null_or_boolean(self):
        for value in (None, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(report.ReportError, "title must be text or a number"):
                    page({"title": value, "checks": []})
        html = page({"title": "t", "checks": [], "views": [
            {"type": "table", "columns": ["a", "b"], "rows": [[None, 1]]}]})
        self.assertNotIn("None", html)
        with self.assertRaisesRegex(report.ReportError, "groups\\[0\\] must be"):
            page({"title": "t", "checks": [], "views": [
                {"type": "graph", "groups": [None, "a"], "nodes": [0], "edges": []}]})
        with self.assertRaisesRegex(report.ReportError, "lists group 'a' twice"):
            page({"title": "t", "checks": [], "views": [
                {"type": "graph", "groups": ["a", "a"], "nodes": [0], "edges": []}]})

    def test_edge_status_survives_clipping(self):
        label = "n" * 71
        html = page({"title": "t", "checks": [{"label": "c", "ok": False}], "views": [
            {"type": "graph", "edge_rule": "conflict", "edges": [[0, 1, 2.5]],
             "nodes": [{"id": 0, "label": label, "group": "a"},
                       {"id": 1, "label": label + "m", "group": "a"}]}]})
        tips = [t for t in re.findall(r"<title>(.*?)</title>", html) if "–" in t]
        self.assertTrue(tips and tips[0].endswith("· conflict"), tips)

    def test_narrow_spans_at_large_offsets_keep_their_scale(self):
        lo = 1.7e15
        marks = report.ticks(lo, lo + 1500)
        self.assertGreater(len({m for m in marks if lo <= m <= lo + 1500}), 1)

    def test_violation_mark_survives_clipping(self):
        label = "r" * report.MAX_TEXT
        html = page({"title": "t", "checks": [{"label": "c", "ok": False}], "views": [
            {"type": "grid", "cells": [[1, 0]], "rows": [label], "violations": [[0, 0]]}]})
        tips = re.findall(r"<title>(.*?)</title>", html)
        self.assertTrue(any(t.endswith("· violated") for t in tips), tips)
        self.assertLessEqual(max(len(t) for t in tips), report.MAX_TIP)

    def test_newlines_in_strings_never_start_a_line(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@host"
        html = page({"title": "t", "checks": [], "notes": [f"x\n{key}\r\n"]})
        self.assertNotIn(f"\n{key}", html)
        self.assertIn("&#10;", html)

    def test_long_strings_are_refused_with_their_key(self):
        view = {"title": "t", "checks": [], "views": [
            {"type": "route", "points": ["a" * (report.MAX_TEXT + 1), "b"],
             "routes": [{"order": [0, 1]}]}]}
        with self.assertRaisesRegex(report.ReportError,
                                    rf"view.views\[0\].points\[0\] is longer than {report.MAX_TEXT}"):
            page(view)

    def test_number_of_figures_is_limited(self):
        bars = {"type": "bars", "items": [{"label": "a", "value": 1}]}
        with self.assertRaisesRegex(report.ReportError, f"at most {report.MAX_VIEWS} figures"):
            page({"title": "t", "checks": [], "views": [bars] * (report.MAX_VIEWS + 1)})

    def test_repeated_labels_cannot_inflate_the_page(self):
        label = '"' * report.MAX_TEXT
        route = {"type": "route", "points": [{"id": 0, "label": label, "x": 0, "y": 0},
                                             {"id": 1, "label": label, "x": 1, "y": 0}],
                 "routes": [{"order": [0, 1] * 2500}]}
        html = page({"title": "t", "checks": [], "views": [route]})
        self.assertLess(len(html), report.MAX_BODY_BYTES)

    def test_whole_page_has_a_size_budget(self):
        bars = {"type": "bars", "items": [{"label": f"item {i}", "value": i} for i in range(60)]}
        with mock.patch.object(report, "MAX_BODY_BYTES", 50_000):
            with self.assertRaisesRegex(report.ReportError,
                                        r"view.views\[\d\] makes the report larger than 0.05 MB"):
                page({"title": "t", "checks": [], "views": [bars] * 3})

    def test_graph_layout_work_is_limited(self):
        n = report.MAX_LAYOUT_NODES // 2 + 1
        graph = {"type": "graph", "nodes": list(range(n)), "edges": [[0, 1]]}
        with self.assertRaisesRegex(report.ReportError,
                                    f"lay out {2 * n} graph nodes; the limit is"):
            page({"title": "t", "checks": [], "views": [graph, graph]})

    def test_size_budget_counts_bytes(self):
        view = {"title": "t", "checks": [], "notes": ["€" * report.MAX_TEXT] * 3}
        chars = len(report.render(view, None, None)[1])
        with mock.patch.object(report, "MAX_BODY_BYTES", chars + 100):
            with self.assertRaisesRegex(report.ReportError, "larger than"):
                report.render(view, None, None)

    def test_extreme_numbers_draw_or_name_their_key(self):
        for value in (5e-324, 1e200, -1e-160):
            with self.subTest(value=value):
                page({"title": "t", "checks": [], "views": [
                    {"type": "bars", "items": [{"label": "a", "value": value}]}]})
        page({"title": "t", "checks": [], "views": [{"type": "schedule", "rows": [
            {"label": "m", "tasks": [{"label": "j", "start": 1e20, "end": 1e20}]}]}]})
        page(result=ising_result([1, -1], objective_distribution={
            "values": [-2e150, 1e150], "counts": [7, 3]}))
        with self.assertRaisesRegex(report.ReportError,
                                    r"view.views\[0\].items\[0\].value is longer than"):
            page({"title": "t", "checks": [], "views": [
                {"type": "bars", "items": [{"label": "a", "value": 10 ** 400}]}]})
        points = [{"id": 0, "x": 0, "y": 0}, {"id": 1, "x": 1, "y": 0}]
        with self.assertRaisesRegex(report.ReportError, r"distances\[0\]\[1\] must be"):
            page({"title": "t", "checks": [], "views": [
                {"type": "route", "points": points, "routes": [{"order": [0, 1]}],
                 "distances": [[0, 1e200], [1e200, 0]]}]})

    def test_wrong_shape_message_is_short(self):
        with self.assertRaisesRegex(report.ReportError, "view must be an object") as caught:
            page(["x" * 100] * 1000)
        self.assertLess(len(str(caught.exception)), 600)

    def test_result_only_report_has_sample_quality(self):
        html = page(result=ising_result(
            [1, -1, 1], objective_distribution={"values": [-2.0, 0.0],
                                                "counts": [7, 3]}))
        self.assertIn("7 of 10 samples reached the best Ising energy of -2 J", html)
        self.assertEqual(figures(html), ["samples", "state"])

    def test_best_samples_sit_left_of_the_distribution_in_one_card(self):
        html = page(result=ising_result([1, -1, 1], objective_distribution={
            "values": [-2.0, 0.0], "counts": [7, 3]}))
        quality = html.split("<h2>Sample quality</h2>")[1].split("<h2>")[0]
        self.assertEqual(quality.count('<section class="card'), 1)
        self.assertIn('<section class="card pair"><div class="top"><h3>Best distinct samples',
                      quality)
        self.assertLess(quality.index("Best distinct samples"),
                        quality.index('data-figure="samples"'))

    def test_distribution_beside_the_state_column_is_drawn_narrower(self):
        dist = {"values": [-2.0, 0.0], "counts": [7, 3]}
        narrow = page(result=ising_result([1, -1, 1], objective_distribution=dist))
        self.assertIn(f'data-figure="samples" viewBox="0 0 {report.PAIR_W} ', narrow)
        wide = page(result=ising_result([1, -1] * 40, objective_distribution=dist))
        self.assertIn(f'data-figure="samples" viewBox="0 0 {report.W} ', wide)

    def test_best_samples_keep_their_card_without_a_distribution(self):
        html = page(result=ising_result([1, -1], objective_distribution={
            "values": [-2.0], "counts": [10]}))
        quality = html.split("<h2>Sample quality</h2>")[1].split("<h2>")[0]
        self.assertIn('<section class="card"><div class="top"><h3>Best distinct samples',
                      quality)
        self.assertNotIn("pair", quality)

    def test_single_valued_distribution_is_not_plotted(self):
        self.assertIsNone(report.distribution_figure(
            {"values": [-2.0], "counts": [10]}, "ising"))

    def test_large_sample_strip_is_coarse_grained_with_a_footnote(self):
        n = report.MAX_STRIP + 1
        html = page(result=ising_result([1] * (n - 2) + [-1, -1]))
        self.assertEqual(figures(html), ["state"])
        self.assertIn("<h3>Best sample*</h3>", html)
        self.assertIn(f"{n:,} variables in {n // 2 + 1:,} blocks of 2", html)
        self.assertIn("s[4096..4096]: 0 of 1 are +1", html)
        self.assertIn("s[4094..4095]: 1 of 2 are +1", html)
        self.assertIn(f"<footer>* Simplified for {n:,} variables: the best-sample strip "
                      "shows the share set to +1 per block of 2 variables.</footer>", html)
        # Shares 0 to 1: all +1 -> solid, half -> light second color, all -1 -> solid second.
        self.assertIn('class="vq-m vq-s1">', html)
        self.assertIn('class="vq-m vq-s2" fill-opacity="0.15">', html)
        self.assertNotIn('class="vq-m vq-s1" fill-opacity', html)
        # Three shares need the gradient: one swatch strip from the −1 to the +1 color.
        legend = strip_legend(html)
        self.assertIn('class="vq-s2" fill-opacity', legend)
        self.assertIn('class="vq-s1" fill-opacity', legend)
        self.assertIn(">0%<", legend)
        self.assertIn(">100% set to +1<", legend)
        self.assertNotIn(" are ", legend)

    def test_coarse_strip_scale_spans_the_block_shares(self):
        n = report.MAX_STRIP + 1          # blocks of 2; the last one holds one variable
        half = report.MAX_STRIP // 4
        sample = [1, -1] * half + [1, 1] * (n // 2 - half) + [1]
        html = page(result=ising_result(sample))
        # Half-set blocks are the lowest share present, so they take the solid −1 color.
        self.assertIn('class="vq-m vq-s2">', html)
        self.assertNotIn("fill-opacity", html.split('data-figure="state"')[1])
        # Two shares need no gradient: two plain swatches.
        self.assertEqual(legend_class(html, "100% set to +1"), "vq-s1")
        self.assertEqual(legend_class(html, "50% set to +1"), "vq-s2")
        self.assertNotIn("fill-opacity", strip_legend(html))
        self.assertIn("Every block is 50% or 100% set to +1.", html)

    def test_coarse_strip_of_pure_blocks_uses_the_value_names(self):
        n = report.MAX_STRIP + 1
        html = page(result=ising_result([1, 1] * (n // 4) + [-1] * (n - 2 * (n // 4))))
        self.assertEqual(legend_class(html, "+1"), "vq-s1")
        self.assertEqual(legend_class(html, "−1"), "vq-s2")
        self.assertNotIn("%", strip_legend(html))
        self.assertIn("Every block holds a single value.", html)

    def test_coarse_strip_of_one_value_is_solid(self):
        html = page(result=ising_result([1] * (report.MAX_STRIP + 1)))
        cells = html.split('data-figure="state"')[1]
        self.assertIn('class="vq-m vq-s1">', cells)
        self.assertNotIn("vq-m vq-s2", cells)
        self.assertNotIn("fill-opacity", cells)
        self.assertIn("Every block holds a single value.", html)

    def test_qubo_strip_draws_zero_like_the_legend(self):
        html = page(result=qubo_result([1, 0, 1], 0.0))
        self.assertEqual(legend_class(html, "0"), "vq-s2")
        self.assertIn('class="vq-m vq-s2">', html)
        self.assertNotIn('vq-s2" fill-opacity', html)
        self.assertIn("x[1] = 0", html)

    def test_small_report_has_no_footnote(self):
        html = page(result=ising_result([1, -1, 1]))
        self.assertNotIn("*", html[html.index("<main>"):html.index("</main>")])

    def test_result_without_distribution_still_renders(self):
        html = page(result=ising_result([1, -1]))
        self.assertIn("Best distinct samples", html)


@needs_numpy
class TestModel(unittest.TestCase):
    def test_full_report_draws_every_section(self):
        model = model_from_spec({"type": "ising", "quadratic": [[u, v, 1.0] for u, v in EDGES]})
        result = ising_result([1, -1, 1, 1], 0.0, objective_distribution={
            "values": [0.0, 2.0], "counts": [7, 3]})
        view = {"title": "Max-cut", "checks": [],
                "views": [graph_view(["a", "b", "a", "a"], edge_rule="cut")]}
        html = page(view, result, model)
        self.assertIn("coupling raising the objective (2)", html)
        self.assertIn("coupling lowering the objective or inactive (2)", html)
        self.assertIn("+1 (3)", html)
        self.assertIn("−1 (1)", html)
        self.assertEqual(figures(html), ["graph", "samples", "model", "state"])

    def test_figures_have_no_standalone_image_form(self):
        self.assertFalse(hasattr(report.Figure, "standalone"))
        self.assertNotIn("xmlns", page({"title": "t", "checks": [], "views": [
            {"type": "bars", "items": [{"label": "a", "value": 1}]}]}))

    def test_model_graph_highlights_softly(self):
        model = model_from_spec({"type": "ising", "quadratic": [[0, 1, 1.0], [1, 2, -1.0]]})
        view = {"title": "t", "checks": [], "views": [graph_view(["a", "b", "a", "a"],
                                                                 edge_rule="cut")]}
        html = page(view, ising_result([1, 1, 1], 0.0), model)
        solution, rest = html.split("<h2>Model</h2>")
        self.assertIn('class="vq-m vq-eh"', solution)        # the cut edges keep the ink
        self.assertIn('class="vq-m vq-em"', rest)             # the raising coupling is mid-tone
        self.assertNotIn('class="vq-m vq-eh"', rest)

    def test_ground_state_legend_counts_zero_raising_couplings(self):
        model = model_from_spec({"type": "ising", "quadratic": [[0, 1, -1.0], [1, 2, -1.0]]})
        html = page(result=ising_result([1, 1, 1], -2.0), model=model)
        self.assertIn("coupling raising the objective (0)", html)
        self.assertIn("coupling lowering the objective or inactive (2)", html)
        self.assertIn("−1 (0)", html)
        self.assertIn("0 – 1 · weight -1 J · coupling lowering the objective or inactive", html)

    def test_qubo_model_uses_the_same_style_as_ising(self):
        model = model_from_spec({"type": "qubo", "quadratic": [[0, 1, 2.0], [1, 2, -2.0]],
                                 "linear": [-1, -1, -1]})
        html = page(result=qubo_result([1, 1, 1], -1.0), model=model)
        # Both values are colored groups; only a positive term between two 1s raises.
        self.assertIn("1 (3)", html)
        self.assertIn("0 (0)", html)
        self.assertIn("coupling raising the objective (1)", html)
        self.assertIn("coupling lowering the objective or inactive (1)", html)
        self.assertIn("0 – 1 · weight 2 J · coupling raising the objective", html)
        self.assertIn("1 – 2 · weight -2 J · coupling lowering the objective or inactive", html)
        for needle in ("frustrated", "active coupling", "x = 0", "spin +1"):
            self.assertNotIn(needle, html)

    def test_qubo_diagonal_is_linear_not_a_coupling(self):
        model = model_from_spec({"type": "qubo", "matrix": [[-1, 2], [0, -1]]})
        html = page(result=qubo_result([1, 0], -1.0), model=model)
        self.assertIn('<div class="label">Couplings</div><div class="value">1</div>',
                      html)
        edges = share_tables(html)[figures(html).index("model")][1]
        self.assertEqual([e[3] for e in edges[1:]],
                         ["coupling lowering the objective or inactive"])

    def test_large_model_graph_is_coarse_grained(self):
        n = 400
        model = model_from_spec({"type": "ising",
                                 "quadratic": [[i, i + 1, -1.0] for i in range(n - 1)]})
        # Blocks of 10: a domain wall inside block 1 and one on the edge 19 | 20.
        sample = [1] * 15 + [-1] * 5 + [1] * (n - 20)
        html = page(result=ising_result(sample, -396.0), model=model)
        self.assertEqual(figures(html), ["model", "state"])
        self.assertIn("<h3>Model in its best state*</h3>", html)
        self.assertIn("mostly +1 (39)", html)
        self.assertIn("mixed (1)", html)
        self.assertIn("block 1: s[10..19] · 5 of 10 are +1 · 9 couplings inside, 1 raising "
                      "the objective", html)
        self.assertIn("block 1 – block 2 · 1 couplings, 1 raising the objective", html)
        self.assertIn("blocks sharing a coupling that raises the objective (1)", html)
        self.assertIn("blocks sharing only couplings that lower it or are inactive (38)", html)
        self.assertIn("mostly −1 (0)", html)
        self.assertIn("* Simplified for 400 variables: the coupling graph groups the "
                      "variables into 40 blocks of 10.", html)

    def test_coarse_graph_colors_values_like_the_fine_one(self):
        n = 400
        for kind, one, zero, on, off in (("ising", "+1", "−1", 1, -1), ("qubo", "1", "0", 1, 0)):
            with self.subTest(kind=kind):
                model = model_from_spec({"type": kind, "quadratic": [[i, i + 1, -1.0]
                                                                     for i in range(n - 1)]})
                fine = model_from_spec({"type": kind, "quadratic": [[0, 1, -1.0]]})
                result = ising_result if kind == "ising" else qubo_result
                coarse = page(result=result([on] * 15 + [off] * 5 + [off] * (n - 20), 0.0),
                              model=model).split("<h3>Best sample")[0]
                small = page(result=result([on, off], 0.0), model=fine).split("<h3>Best sample")[0]
                self.assertEqual(legend_class(small, one), "vq-s1")
                self.assertEqual(legend_class(small, zero), "vq-s2")
                self.assertEqual(legend_class(coarse, f"mostly {one}"), "vq-s1")
                self.assertEqual(legend_class(coarse, f"mostly {zero}"), "vq-s2")
                self.assertEqual(legend_class(coarse, "mixed"), "vq-n0")

    def test_dense_block_graph_falls_back_to_fewer_blocks(self):
        import numpy as np

        n = 400
        r = np.random.default_rng(0)
        i, j = r.integers(0, n, 6000), r.integers(0, n, 6000)
        keep = i != j
        key = np.unique(np.minimum(i, j)[keep] * n + np.maximum(i, j)[keep])
        model = model_from_spec({"type": "ising", "quadratic": [[int(k // n), int(k % n), 1.0]
                                                               for k in key]})
        html = page(result=ising_result([1] * n, 0.0), model=model)
        # 40 blocks would share couplings on nearly all 780 pairs; 12 blocks are drawn.
        self.assertIn("groups the variables into 12 blocks of 34", html)
        self.assertIn("mostly +1 (12)", html)

    def test_dense_qubo_graph_is_coarse_grained(self):
        n = 90      # 4,005 couplings, above the edge limit at a small variable count
        model = model_from_spec({"type": "qubo", "quadratic": [[i, j, 1.0] for i in range(n)
                                                              for j in range(i + 1, n)]})
        html = page(result=qubo_result([0] * 6 + [1] * 4 + [0] * (n - 10), 6.0), model=model)
        self.assertEqual(figures(html), ["model", "state"])
        # The complete graph connects every pair of blocks, so 12 blocks of 8 are drawn.
        self.assertIn("block 0: x[0..7] · 2 of 8 are 1 · 28 couplings inside, 1 raising the "
                      "objective", html)
        self.assertIn("block 0 – block 1 · 64 couplings, 4 raising the objective", html)
        self.assertIn("blocks sharing a coupling that raises the objective (1)", html)
        self.assertIn("blocks sharing only couplings that lower it or are inactive (65)", html)
        self.assertIn("groups the variables into 12 blocks of 8", html)

    def test_a_million_variable_model_reports_its_stats(self):
        import numpy as np
        from veloxq_mcp.models import Model

        n = 1_000_000
        model = Model("ising", np.zeros(n), np.arange(n - 1), np.arange(1, n), -np.ones(n - 1))
        html = page(result=ising_result([1] * n, -float(n - 1)), model=model)
        self.assertIn('<div class="label">Variables</div><div class="value">1,000,000</div>',
                      html)
        self.assertIn('<div class="label">Couplings</div><div class="value">999,999</div>',
                      html)
        self.assertIn("Coefficient range</div><div class=\"value\">1 to 1 J</div>", html)
        self.assertEqual(figures(html), ["model", "state"])
        self.assertIn("blocks of 25,000", html)
        self.assertIn("per block of 245 variables", html)


class TestShare(unittest.TestCase):
    """The Share menu that replaces a figure's data table."""

    def share(self, fig):
        body = report.figure_body(fig)
        items = re.findall(r'<button type="button" data-share="(\w+)" data-file="([^"]+)">'
                           r"([^<]+)</button>", body)
        return body, items, share_tables(body)[0]

    def test_figure_offers_its_image_and_its_table_as_csv(self):
        fig = report.view_bars({"type": "bars", "title": "Load per item", "unit": "kg",
                                "items": [{"label": "a", "value": 1234.5},
                                          {"label": "b", "value": 2}]}, "v")
        body, items, tables = self.share(fig)
        self.assertNotIn("<summary>Data</summary>", body)
        self.assertNotIn("<table>", body)
        self.assertEqual(items, [("png", "load-per-item.png", "Download image (PNG)"),
                                 ("0", "load-per-item-values.csv", "Download data (CSV)")])
        self.assertEqual(tables, [[["item", "value (kg)"], ["a", "1234.5"], ["b", "2"]]])

    def test_each_table_gets_its_own_csv(self):
        fig = report.Figure("graph", "", "", 10, "",
                            [("Nodes", ["node"], [["a"]]), ("Edges", ["from", "to"], [])])
        _, items, tables = self.share(fig)
        self.assertEqual(items, [("png", "graph.png", "Download image (PNG)"),
                                 ("0", "graph-nodes.csv", "Download nodes (CSV)"),
                                 ("1", "graph-edges.csv", "Download edges (CSV)")])
        self.assertEqual(tables, [[["node"], ["a"]], [["from", "to"]]])

    def test_figure_without_a_table_offers_only_its_image(self):
        _, items, tables = self.share(report.Figure("state", "Best sample", "", 10, ""))
        self.assertEqual(items, [("png", "best-sample.png", "Download image (PNG)")])
        self.assertEqual(tables, [])

    def test_csv_holds_every_row_of_a_wide_grid(self):
        cells = [[1234.5] * 50 for _ in range(400)]
        _, _, tables = self.share(report.view_grid({"type": "grid", "cells": cells}, "v"))
        self.assertEqual(len(tables[0]), 401)
        self.assertEqual(tables[0][1], ["0"] + ["1234.5"] * 50)

    def test_repeated_cell_text_is_stored_once(self):
        label = "x" * report.MAX_TEXT
        fig = report.view_bars({"type": "bars", "items": [{"label": label, "value": 1}] * 50},
                               "v")
        body, _, tables = self.share(fig)
        self.assertEqual(body.count(label), 1)
        self.assertEqual(tables[0][1:], [[label, "1"]] * 50)

    def test_labels_cannot_leave_the_data_attribute(self):
        label = '"><b>x'
        fig = report.view_bars({"type": "bars", "items": [{"label": label, "value": 1}]}, "v")
        body, _, tables = self.share(fig)
        self.assertNotIn("<b>", body)
        self.assertEqual(tables[0][1], [label, "1"])


class TestVariableLabels(unittest.TestCase):
    """Variables named by the labels of their model file, not by position."""

    def test_strip_without_labels_counts_positions(self):
        fig = report.strip_figure(ising_result([1, -1]))
        self.assertIn("<title>s[1] = -1</title>", fig.body)
        self.assertIn("in index order", fig.caption)

    def test_strip_names_integer_labels(self):
        fig = report.strip_figure(ising_result([1, -1], labels=["3", "10"]))
        self.assertIn("<title>s[10] = -1</title>", fig.body)
        self.assertIn("in label order", fig.caption)

    def test_strip_names_other_labels(self):
        fig = report.strip_figure(ising_result([1, -1], labels=["b", "a"]))
        self.assertIn("<title>a = -1</title>", fig.body)
        self.assertIn("in the model file's order", fig.caption)

    def test_coarse_strip_names_the_block_ends(self):
        n = report.MAX_STRIP + 1
        labels = [f"v{k}" for k in range(n)]
        fig = report.strip_figure(ising_result([1] * n, labels=labels))
        self.assertIn("<title>v0..v1: 2 of 2 are +1</title>", fig.body)
        self.assertIn("in the model file's order", fig.caption)

    @needs_numpy
    def test_model_figures_name_labeled_variables(self):
        from veloxq_mcp.models import Model

        model = Model("ising", [0.0, 0.0], [0], [1], [-1.0], labels=["b", "a"])
        result = ising_result([1, 1], -1.0, labels=["b", "a"])
        html = page(result=result, model=model)
        self.assertIn("<title>a · +1</title>", html)
        n = 400
        model = Model("ising", [0.0] * n, list(range(n - 1)), list(range(1, n)),
                      [-1.0] * (n - 1), labels=[f"v{k}" for k in range(n)])
        html = page(result=ising_result([1] * n, -399.0, labels=model.labels), model=model)
        self.assertIn("block 0: v0..v9 · 10 of 10 are +1", html)


class TestApp(unittest.TestCase):
    """The MCP App page that shows a report body inside an MCP host."""

    def test_app_loads_nothing_from_outside(self):
        html = report.app_page()
        for needle in ("http:", "https:", "src=", "href=", "url(", "@import",
                       "<link", "<img", "<iframe", "<object"):
            self.assertNotIn(needle, html)

    def test_app_speaks_the_mcp_apps_view_protocol(self):
        html = report.app_page()
        self.assertTrue(html.startswith("<!doctype html><html><head>"))
        for method in ("ui/initialize", "ui/notifications/initialized",
                       "ui/notifications/tool-result", "ui/notifications/size-changed",
                       "ui/notifications/host-context-changed"):
            self.assertIn(f"'{method}'", html)
        self.assertIn(f"protocolVersion:'{report.APP_PROTOCOL}'", html)


def _browser():
    """A headless Chromium: VELOXQ_TEST_BROWSER, one on PATH, or Playwright's."""
    found = [os.environ.get("VELOXQ_TEST_BROWSER")]
    found += [shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome")]
    found += sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/*/headless_shell")))
    return next((b for b in found if b), None)


# Plays the MCP host: answers ui/initialize with a dark theme and CAPS as its
# capabilities, sends the tool input and result once the view is initialized,
# then records what the view drew and every message it sent. Downloads are
# recorded and accepted.
HOST = """<!doctype html><meta charset="utf-8">
<iframe id="f" style="width:800px;height:300px" srcdoc="APP"></iframe>
<script>
var frame=document.getElementById('f'),sent=[],sizes=[],opened=[],prevented=null,
  downloads=[];
function reply(m){frame.contentWindow.postMessage(m,'*');}
window.addEventListener('message',function(e){
  if(e.source!==frame.contentWindow)return;
  var m=e.data;sent.push(m.method||'response');
  if(m.method==='ui/initialize')reply({jsonrpc:'2.0',id:m.id,result:{
    protocolVersion:m.params.protocolVersion,hostInfo:{name:'test',version:'1'},
    hostCapabilities:CAPS,hostContext:{theme:'dark'}}});
  else if(m.method==='ui/notifications/initialized'){
    reply({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{job_id:'job-1'}}});
    reply({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{
      content:[{type:'text',text:'{}'}],structuredContent:RESULT}});
    reply({jsonrpc:'2.0',id:7,method:'ping',params:{}});
    setTimeout(function(){
      var click=new MouseEvent('click',{bubbles:true,cancelable:true});
      prevented=!frame.contentDocument.querySelector('dd a').dispatchEvent(click);},300);
  }
  else if(m.method==='ui/open-link')opened.push(m.params.url);
  else if(m.method==='ui/download-file'){downloads.push(m.params);
    reply({jsonrpc:'2.0',id:m.id,result:{}});}
  else if(m.method==='ui/notifications/size-changed')sizes.push(m.params.height);
});
function seen(){
  var d=frame.contentDocument;
  return JSON.stringify({sent:sent,sizes:sizes,opened:opened,prevented:prevented,
    theme:d.documentElement.getAttribute('data-theme'),title:d.title,
    figures:d.querySelectorAll('svg.vq-fig').length,
    tips:d.querySelectorAll('[data-tip]').length,
    titles:d.querySelectorAll('svg title').length});
}
</script>"""


def run_in_browser(browser: str, page_path: Path, expression: str, settle: float) -> str:
    """Load a page in headless Chromium over the DevTools protocol and return
    ``expression`` evaluated after ``settle`` seconds, awaited if it is a
    promise. DevTools, unlike
    --dump-dom, lets the page render frames, so requestAnimationFrame and
    ResizeObserver run."""
    from websockets.sync.client import connect

    with tempfile.TemporaryDirectory() as profile:
        proc = subprocess.Popen(
            [browser, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for line in proc.stderr:
                if line.startswith("DevTools listening on "):
                    port = urllib.parse.urlparse(line.split()[-1]).port
                    break
            else:
                raise RuntimeError(f"{browser} exited without a DevTools endpoint")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as response:
                target = next(t for t in json.load(response) if t["type"] == "page")
            with connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
                def send(n, method, params):
                    ws.send(json.dumps({"id": n, "method": method, "params": params}))
                    while (reply := json.loads(ws.recv(timeout=30))).get("id") != n:
                        pass
                    return reply

                send(1, "Page.navigate", {"url": page_path.as_uri()})
                time.sleep(settle)
                reply = send(2, "Runtime.evaluate", {"expression": expression,
                                                     "returnByValue": True,
                                                     "awaitPromise": True})
                return reply["result"]["result"]["value"]
        finally:
            proc.kill()
            proc.wait()
            proc.stderr.close()


@needs_numpy
@unittest.skipUnless(_browser(), "no headless Chromium found")
class TestAppInBrowser(unittest.TestCase):
    def host(self, expression, caps="{}"):
        title, body = report.render(
            {"title": "Max-cut", "checks": [],
             "views": [graph_view(["a", "b", "a", "a"], edge_rule="cut")]},
            ising_result([1, -1, 1, 1], 0.0), None)
        result = json.dumps({"title": title, "html": body}).replace("</", "<\\/")
        host = (HOST.replace("APP", html.escape(report.app_page(), quote=True))
                .replace("RESULT", result).replace("CAPS", caps))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.html"
            path.write_text(host, encoding="utf-8")
            return json.loads(run_in_browser(_browser(), path, expression, settle=2.0))

    def test_app_shows_the_report_the_host_sends(self):
        seen = self.host("seen()")
        self.assertEqual(seen["sent"][:2], ["ui/initialize", "ui/notifications/initialized"])
        self.assertIn("response", seen["sent"])          # the answer to ping
        self.assertEqual(seen["opened"], ["https://web.veloxq.com/jobs/job-1"])
        self.assertIs(seen["prevented"], True)        # the frame opens nothing itself
        self.assertEqual(seen["theme"], "dark")
        self.assertEqual(seen["title"], "Max-cut")
        self.assertEqual(seen["figures"], 2)             # the graph and the best-sample strip
        self.assertGreater(seen["tips"], 0)
        self.assertEqual(seen["titles"], 0)
        self.assertGreater(max(seen["sizes"]), 300)      # grew to the report, past the frame

    # Clicks every Share item of the first figure, then reports the downloads
    # the host received and the message the menu shows.
    SHARE = """(async function(){
      var d=frame.contentDocument,box=d.querySelector('details.vq-share');
      box.querySelectorAll('[data-share]').forEach(function(b){b.click();});
      await new Promise(function(r){setTimeout(r,1000);});
      var msg=box.querySelector('.vq-share-msg');
      return JSON.stringify({downloads:downloads,message:msg.hidden?null:msg.textContent});
    })()"""

    def test_app_downloads_through_the_host(self):
        seen = self.host(self.SHARE, caps="{downloadFile:{}}")
        files = {c["resource"]["uri"]: c["resource"]
                 for d in seen["downloads"] for c in d["contents"]}
        self.assertEqual(sorted((u, f["mimeType"]) for u, f in files.items()),
                         [("file:///graph-edges.csv", "text/csv"),
                          ("file:///graph-nodes.csv", "text/csv"),
                          ("file:///graph.png", "image/png")])
        self.assertTrue(files["file:///graph.png"]["blob"].startswith("iVBORw0KGgo"))  # PNG
        self.assertTrue(files["file:///graph-nodes.csv"]["text"].startswith("node,group\r\n"))
        self.assertIsNone(seen["message"])

    def test_app_says_when_the_host_cannot_download(self):
        seen = self.host(self.SHARE)
        self.assertEqual(seen["downloads"], [])
        self.assertEqual(seen["message"], "This host does not allow downloads.")


@needs_numpy
@unittest.skipUnless(_browser(), "no headless Chromium found")
class TestPageShareInBrowser(unittest.TestCase):
    def test_page_downloads_the_image_and_the_data(self):
        html = page({"title": "Max-cut", "checks": [],
                     "views": [graph_view(["a", "b", "a", "a"], edge_rule="cut")]},
                    ising_result([1, -1, 1, 1], 0.0))
        clicks = """(async function(){
          var got=[];
          HTMLAnchorElement.prototype.click=function(){
            var a=this;got.push(fetch(a.href).then(function(r){return r.arrayBuffer();})
              .then(function(b){return {name:a.download,size:b.byteLength,
                head:Array.from(new Uint8Array(b.slice(0,10))),
                type:a.href.slice(0,5)};}));};
          document.querySelector('details.vq-share').querySelectorAll('[data-share]')
            .forEach(function(b){b.click();});
          await new Promise(function(r){setTimeout(r,1000);});
          return JSON.stringify({files:await Promise.all(got),
            quoted:vqCsv({r:[[0,1,2]],s:['a, "b"','c\\nd','e']})});
        })()"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.html"
            path.write_text(html, encoding="utf-8")
            seen = json.loads(run_in_browser(_browser(), path, clicks, settle=1.0))
        files = {f["name"]: f for f in seen["files"]}
        self.assertEqual(sorted(files), ["graph-edges.csv", "graph-nodes.csv", "graph.png"])
        self.assertEqual(files["graph.png"]["head"][:8], [137, 80, 78, 71, 13, 10, 26, 10])
        self.assertGreater(files["graph.png"]["size"], 1000)
        self.assertEqual(bytes(files["graph-nodes.csv"]["head"]).decode(), "node,group")
        self.assertEqual(seen["quoted"], '"a, ""b""","c\nd",e\r\n')

    def test_artifact_viewer_saves_through_its_downloads_capability(self):
        html = page({"title": "Max-cut", "checks": [],
                     "views": [graph_view(["a", "b", "a", "a"], edge_rule="cut")]},
                    ising_result([1, -1, 1, 1], 0.0))
        # Plays a claude.ai artifact viewer: the edges CSV is declined.
        clicks = """(async function(){
          var got=[],links=0;
          HTMLAnchorElement.prototype.click=function(){links++;};
          window.claude={use:function(name){return Promise.resolve(name!=='downloads'?null:{
            save:function(f){
              if(/edges/.test(f.filename))return Promise.reject({code:'declined',message:'no'});
              got.push(f.data.arrayBuffer().then(function(b){return {name:f.filename,
                size:b.byteLength,head:Array.from(new Uint8Array(b.slice(0,10)))};}));
              return Promise.resolve({status:'saved'});}});}};
          var box=document.querySelector('details.vq-share');
          box.querySelectorAll('[data-share]').forEach(function(b){b.click();});
          await new Promise(function(r){setTimeout(r,1000);});
          var msg=box.querySelector('.vq-share-msg');
          return JSON.stringify({files:await Promise.all(got),links:links,
            message:msg.hidden?null:msg.textContent});
        })()"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.html"
            path.write_text(html, encoding="utf-8")
            seen = json.loads(run_in_browser(_browser(), path, clicks, settle=1.0))
        files = {f["name"]: f for f in seen["files"]}
        self.assertEqual(sorted(files), ["graph-nodes.csv", "graph.png"])
        self.assertEqual(files["graph.png"]["head"][:8], [137, 80, 78, 71, 13, 10, 26, 10])
        self.assertEqual(bytes(files["graph-nodes.csv"]["head"]).decode(), "node,group")
        self.assertEqual(seen["links"], 0)
        self.assertIsNone(seen["message"])          # a declined save is not an error


@needs_numpy
class TestObjectiveDistribution(unittest.TestCase):
    def test_few_distinct_values_are_counted_exactly(self):
        d = objective_distribution([0.0, -2.0, -2.0, 4.0, -2.0 + 1e-12])
        self.assertEqual(d["counts"], [3, 1, 1])
        self.assertEqual(d["values"][1:], [0.0, 4.0])
        self.assertAlmostEqual(d["values"][0], -2.0)

    def test_tie_tolerance_follows_the_value_not_the_largest_one(self):
        d = objective_distribution([-3.5, -3.0, -1.0, 1e12])
        self.assertEqual(d["values"], [-3.5, -3.0, -1.0, 1e12])
        self.assertEqual(d["counts"], [1, 1, 1, 1])

    def test_many_distinct_values_are_binned(self):
        d = objective_distribution([float(i) for i in range(100)])
        self.assertEqual(len(d["bin_edges"]), len(d["counts"]) + 1)
        self.assertEqual(sum(d["counts"]), 100)
        self.assertEqual(d["above"], 0)
        self.assertNotIn("values", d)

    def test_long_upper_tail_is_cut_and_counted(self):
        values = [float(i) for i in range(96)] + [1e6, 2e6, 3e6, 4e6]
        d = objective_distribution(values)
        self.assertLess(d["bin_edges"][-1], 100)
        self.assertGreaterEqual(d["above"], 4)
        self.assertEqual(d["above"] + sum(d["counts"]), 100)
        fig = report.distribution_figure(d, "qubo")
        self.assertIn(f"{d['above']} samples above", fig.caption)
        self.assertIn("bin that holds the best value", fig.caption)

    def test_output_feeds_the_report_chart(self):
        for values in ([-2.0, -2.0, 0.0], [float(i) for i in range(100)]):
            fig = report.distribution_figure(
                objective_distribution(values), "qubo")
            self.assertIn("QUBO objective", fig.body)


class TestUnits(unittest.TestCase):
    def test_energies_carry_the_coupling_unit(self):
        html = page(result=ising_result([1, -1, 1], objective_distribution={
            "values": [-2.0, 0.0], "counts": [7, 3]}))
        self.assertIn('<div class="label">Best Ising energy</div><div class="value">-2 J</div>',
                      html)
        self.assertIn("7 of 10 samples reached the best Ising energy of -2 J.", html)
        self.assertIn("<dt>Best Ising energy</dt><dd>-2 J</dd>", html)
        self.assertIn("Ising energy in J (lower is better)", html)
        self.assertIn("<title>-2 J · 7 samples</title>", html)
        self.assertIn("<th>Ising energy (J)</th>", html)
        self.assertIn("<footer>Energies are in units of the coupling J: a coefficient of 1 "
                      "in the submitted model is 1 J.</footer>", html)

    def test_binned_distribution_names_carry_the_unit(self):
        fig = report.distribution_figure(
            {"bin_edges": [-3.0, -2.0, -1.0], "counts": [4, 5], "above": 1}, "qubo")
        self.assertIn("<title>-3 to -2 J · 4 samples</title>", fig.body)
        self.assertIn("1 samples above -1 J are not drawn", fig.caption)
        self.assertIn("QUBO objective in J (lower is better)", fig.body)
        self.assertEqual(fig.tables[0][1], ["QUBO objective (J)", "samples"])

    @needs_numpy
    def test_model_coefficients_carry_the_unit(self):
        model = model_from_spec({"type": "ising",
                                 "quadratic": [[0, 1, -1.0], [1, 2, 2.0]]})
        html = page(result=ising_result([1, 1, -1], -3.0), model=model)
        self.assertIn('Coefficient range</div><div class="value">1 to 2 J</div>', html)
        self.assertIn("0 – 1 · weight -1 J · ", html)
        self.assertEqual(share_tables(html)[0][1][0], ["from", "to", "weight (J)", "state"])

    def test_quota_is_in_milliseconds(self):
        html = page(result=ising_result([1, -1], statistics={"total_quota_usage": 30156}))
        self.assertIn('<div class="label">Quota used</div><div class="value">30,156 ms</div>',
                      html)

    def test_route_distances_carry_the_view_unit(self):
        fig = report.view_route({"type": "route", "points": [0, 1, 2], "unit": "km",
                                 "routes": [{"order": [0, 2, 1]}],
                                 "distances": [[0, 1, 2], [1, 0, 4], [2, 4, 0]]}, "v")
        self.assertIn("route (length 7 km)", fig.body)
        self.assertIn("step 1: 0 → 2 · 2 km", fig.body)
        self.assertIn(">2 km<", fig.body)
        self.assertEqual(fig.tables[0][1], ["route", "step", "from", "to", "distance (km)"])

    @needs_numpy
    def test_graph_weights_carry_the_view_unit(self):
        fig = report.view_graph(graph_view(["a", "b", "a", "a"], unit="kg",
                                           edges=[[0, 1, 2.5], [1, 2, 1], [2, 3, 1]]), "v")
        self.assertIn("0 – 1 · weight 2.5 kg", fig.body)
        self.assertIn(">2.5 kg<", fig.body)
        self.assertEqual(fig.tables[1][1], ["from", "to", "weight (kg)", "state"])

    def test_lane_tables_name_the_unit(self):
        bins = report.view_bins({"type": "bins", "unit": "kg", "size_label": "weight",
                                 "bins": [{"label": "b", "items": [
                                     {"label": "i", "size": 3}]}]}, "v")
        self.assertEqual(bins.tables[0][1], ["bin", "item", "weight (kg)", "value"])
        sched = report.view_schedule({"type": "schedule", "unit": "h", "rows": [
            {"label": "m", "tasks": [{"label": "j", "start": 0, "end": 2}]}]}, "v")
        self.assertEqual(sched.tables[0][1][:4], ["row", "task", "start (h)", "end (h)"])
        bars = report.view_bars({"type": "bars", "unit": "%",
                                 "items": [{"label": "a", "value": 1}]}, "v")
        self.assertEqual(bars.tables[0][1], ["item", "value (%)"])

    def test_summary_tile_carries_its_unit(self):
        html = page({"title": "t", "checks": [],
                     "summary": [{"label": "Tour length", "value": 42.5, "unit": "km"}]})
        self.assertIn('<div class="label">Tour length</div><div class="value">42.5 km</div>',
                      html)


if __name__ == "__main__":
    unittest.main()
