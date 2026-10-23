"""Tests for veloxq_mcp.report: the view spec, the figures and the page.

Figures that need a computed layout use numpy, which ships with the server's
venv; those tests are skipped without it.
"""
import glob
import html
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


def ising_result(sample, objective=-2.0, **extra):
    r = {"type": "ising", "vartype": "SPIN", "num_variables": len(sample),
         "num_samples": 10, "best_objective": objective, "best_sample": sample,
         "num_samples_at_best": 7,
         "top": [{"sample": sample, "objective": objective}],
         "solver": "veloxq", "job_id": "job-1", "platform_info": {"num_rep": "10"}}
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
        self.assertIn("7 of 10 samples reached the best Ising energy of -2", html)
        self.assertEqual(figures(html), ["samples", "state"])

    def test_single_valued_distribution_is_not_plotted(self):
        self.assertIsNone(report.distribution_figure(
            {"values": [-2.0], "counts": [10]}, "ising"))

    def test_large_sample_omits_the_strip_and_says_so(self):
        n = report.MAX_STRIP + 1
        html = page(result=ising_result([1] * n))
        self.assertEqual(figures(html), [])
        self.assertIn(f"sets {n:,} of {n:,} variables to +1", html)

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
        self.assertIn("frustrated coupling (2)", html)
        self.assertEqual(figures(html), ["graph", "samples", "model", "state"])

    def test_figures_have_no_standalone_image_form(self):
        self.assertFalse(hasattr(report.Figure, "standalone"))
        self.assertNotIn("xmlns", page({"title": "t", "checks": [], "views": [
            {"type": "bars", "items": [{"label": "a", "value": 1}]}]}))

    def test_qubo_model_marks_active_couplings(self):
        model = model_from_spec({"type": "qubo", "quadratic": [[0, 1, 2.0], [1, 2, 2.0]],
                                 "linear": [-1, -1, -1]})
        html = page(result=qubo_result([1, 1, 0], 0.0), model=model)
        self.assertIn("active coupling (both ends 1) (1)", html)
        self.assertIn("x = 0 (1)", html)

    def test_qubo_diagonal_is_linear_not_a_coupling(self):
        model = model_from_spec({"type": "qubo", "matrix": [[-1, 2], [0, -1]]})
        html = page(result=qubo_result([1, 0], -1.0), model=model)
        self.assertIn('<div class="label">Couplings</div><div class="value">1</div>',
                      html)
        self.assertEqual(html.count("<td>inactive coupling</td>"), 1)
        self.assertNotIn("<td>active coupling", html)

    def test_large_model_states_the_omission(self):
        n = 301
        model = model_from_spec({"type": "ising",
                                 "quadratic": [[i, i + 1, -1.0] for i in range(n - 1)]})
        html = page(result=ising_result([1] * n, -300.0), model=model)
        self.assertIn("drawn up to 300 variables", html)
        self.assertNotIn("model", figures(html))


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


# Plays the MCP host: answers ui/initialize with a dark theme, sends the tool
# input and result once the view is initialized, then records what the view
# drew and every message it sent.
HOST = """<!doctype html><meta charset="utf-8">
<iframe id="f" style="width:800px;height:300px" srcdoc="APP"></iframe>
<script>
var frame=document.getElementById('f'),sent=[],sizes=[],opened=[],prevented=null;
function reply(m){frame.contentWindow.postMessage(m,'*');}
window.addEventListener('message',function(e){
  if(e.source!==frame.contentWindow)return;
  var m=e.data;sent.push(m.method||'response');
  if(m.method==='ui/initialize')reply({jsonrpc:'2.0',id:m.id,result:{
    protocolVersion:m.params.protocolVersion,hostInfo:{name:'test',version:'1'},
    hostCapabilities:{},hostContext:{theme:'dark'}}});
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
    ``expression`` evaluated after ``settle`` seconds. DevTools, unlike
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
                                                     "returnByValue": True})
                return reply["result"]["result"]["value"]
        finally:
            proc.kill()
            proc.wait()
            proc.stderr.close()


@needs_numpy
@unittest.skipUnless(_browser(), "no headless Chromium found")
class TestAppInBrowser(unittest.TestCase):
    def test_app_shows_the_report_the_host_sends(self):
        title, body = report.render(
            {"title": "Max-cut", "checks": [],
             "views": [graph_view(["a", "b", "a", "a"], edge_rule="cut")]},
            ising_result([1, -1, 1, 1], 0.0), None)
        result = json.dumps({"title": title, "html": body}).replace("</", "<\\/")
        host = (HOST.replace("APP", html.escape(report.app_page(), quote=True))
                .replace("RESULT", result))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.html"
            path.write_text(host, encoding="utf-8")
            seen = json.loads(run_in_browser(_browser(), path, "seen()", settle=2.0))
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


if __name__ == "__main__":
    unittest.main()
