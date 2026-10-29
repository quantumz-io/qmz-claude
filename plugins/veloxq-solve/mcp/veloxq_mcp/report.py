"""The VeloxQ solution report: one HTML page with figures of a solve.

The page is built from three inputs:

- the view, the decoded solution in the user's domain: title, summary tiles,
  constraint checks, and a list of figures (graph, route, bins, schedule,
  bars, grid, table). The format is documented in the skill's
  references/report-views.md;
- the result document of the job, for the sample-quality section;
- the model, for the coupling graph in the best sample's state.

`page` makes a standalone document with every style, script and figure
inline, which loads nothing from the network. `app_page` is the MCP App that
shows the same content inside an MCP host. Both follow the viewer's light or
dark theme.

A malformed view is a ReportError that names the offending key. Figures have
hard size limits; exceeding one is an error, not a silently thinned plot.
"""
from __future__ import annotations

import html
import json
import math
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from .models import INTEGER_LABEL, Model

W = 720          # figure width in SVG user units
PAIR_W = 480     # width of a figure that shares its card with a table
PAD = 16
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

MAX_NODES = 500
MAX_EDGES = 5000
MAX_LANES = 60
MAX_SEGMENTS = 2000      # bars drawn in one bins or schedule figure
MAX_CELLS = 40000
MAX_GRID_ROWS = 1000
MAX_GRID_COLS = 200
MAX_TABLE_ROWS = 2000
MAX_STRIP = 4096         # best-sample strip, in variables or blocks of variables
COARSE_BLOCKS = (40, 20, 12)   # index blocks in the simplified coupling graph, fewer
                               # when nearly every pair of blocks shares a coupling
TOP_ROWS = 300           # rows shown in the best-samples table
MAX_TEXT = 300           # characters in any string of the view
MAX_VIEWS = 10           # figures in one report
MAX_TIP = 120            # characters in a hover text
MAX_LAYOUT_NODES = 1000  # graph nodes placed by force layout in one report
MAX_VIEW_VALUES = 200_000     # values of any kind in the whole view
MAX_DEPTH = 10           # nesting of lists and objects in the view
MAX_TABLE_COLUMNS = 50
MAX_NOTES = 20
MAX_TILES = 12           # summary tiles
MAX_CHECKS = 100
MAX_MESSAGE = 1000       # characters in an error message
MAX_BODY_BYTES = 10_000_000   # the report's markup, figures included

# (light, dark). The categorical order is fixed: it is what keeps neighbouring
# slots distinguishable under color-vision deficiency.
SERIES = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
          ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300"),
          ("#4a3aa7", "#9085e9"), ("#e34948", "#e66767")]
# Label ink with the higher contrast on each series fill.
SERIES_INK = [("#0b0b0b", "#0b0b0b"), ("#0b0b0b", "#0b0b0b"), ("#0b0b0b", "#0b0b0b"),
              ("#0b0b0b", "#0b0b0b"), ("#0b0b0b", "#0b0b0b"), ("#ffffff", "#ffffff"),
              ("#ffffff", "#0b0b0b"), ("#0b0b0b", "#0b0b0b")]
SURFACE = ("#fcfcfb", "#1a1a19")
PLANE = ("#f9f9f7", "#0d0d0d")
INK = ("#0b0b0b", "#ffffff")
INK2 = ("#52514e", "#c3c2b7")
MUTED = ("#898781", "#898781")
GRID = ("#e1e0d9", "#2c2c2a")
AXIS = ("#c3c2b7", "#383835")
NEUTRAL = ("#f0efec", "#383835")
DEEMPH = ("#d6d5ce", "#4a4a46")
CRITICAL = ("#d03b3b", "#d03b3b")
GOOD = ("#0ca30c", "#0ca30c")

# Series slot for a group that is itself named after a color.
COLOR_NAMES = {"blue": 1, "orange": 2, "aqua": 3, "teal": 3, "cyan": 3, "turquoise": 3,
               "yellow": 4, "gold": 4, "magenta": 5, "pink": 5, "green": 6,
               "violet": 7, "purple": 7, "red": 8}

SHAPES = ["circle", "square", "diamond", "hexagon", "tri-up", "tri-down",
          "pentagon", "star"]


class ReportError(ValueError):
    """The view or the data cannot be drawn as given."""


def fail(msg: str) -> None:
    raise ReportError(msg if len(msg) <= MAX_MESSAGE else msg[:MAX_MESSAGE - 1] + "…")


# Control characters and lone surrogates, which HTML and UTF-8 output reject.
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def esc(s) -> str:
    # Line breaks as references: a written page then never starts a line
    # with view text, whatever file name it is given.
    return (html.escape(_XML_INVALID.sub("", str(s)), quote=True)
            .replace("\r", "&#13;").replace("\n", "&#10;"))


def fmt(x) -> str:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, int):
        return f"{x:,}"
    if not math.isfinite(x):
        return str(x)
    if abs(x - round(x)) < 1e-9 * max(1.0, abs(x)) and abs(x) < 1e15:
        return f"{int(round(x)):,}"
    # Six significant digits, but never fewer than the integer part needs.
    digits = max(6, len(str(int(abs(x)))) + 2) if abs(x) < 1e15 else 6
    return f"{x:,.{digits}g}"


ENERGY_UNIT = "J"
"""Unit of energies and model coefficients: the coupling scale J, so a
coefficient of 1 in the submitted model is 1 J."""


def with_unit(x, unit: str) -> str:
    return f"{fmt(x)} {unit}" if unit else fmt(x)


def col_name(name: str, unit: str) -> str:
    """A table column name with its unit in parentheses."""
    return f"{name} ({unit})" if unit else name


def tw(s, size: float = 12.0) -> float:
    """Approximate rendered width of s in the UI sans, erring wide."""
    return 0.6 * size * len(str(s))


def clip(s, max_px: float, size: float = 12.0) -> str:
    s = str(s)
    if tw(s, size) <= max_px:
        return s
    keep = max(1, int(max_px / (0.6 * size)) - 1)
    return s[:keep] + "…"


JOB_URL = "https://web.veloxq.com/jobs/"

SPEC_DOC = "The format is documented in the skill's references/report-views.md."


# Keys whose value is shown as text. A list or object there would be printed
# whole and copied into every tooltip and table row that names it.
TEXT_KEYS = {"title", "subtitle", "caption", "label", "detail", "note", "value", "unit",
             "size_label", "value_label", "highlight_label", "other_label",
             "unassigned_label", "on_label", "off_label"}


def scalar(x, where: str, none: bool = False):
    """Text or a number (``none``: or null), at most MAX_TEXT characters once printed."""
    if x is None and none:
        return x
    if isinstance(x, bool) or not isinstance(x, (str, int, float)):
        fail(f"{where} must be text or a number, got "
             f"{'null' if x is None else type(x).__name__}")
    if len(str(x)) > MAX_TEXT:
        fail(f"{where} is longer than {MAX_TEXT} characters")
    return x


def scalars(xs, where: str, none: bool = False) -> list:
    return [scalar(x, f"{where}[{k}]", none) for k, x in enumerate(items_of(xs, where))]


def group_list(xs, where: str) -> list:
    """The listed groups: distinct strings or integers, few enough to color."""
    order = items_of(xs, where)
    if len(order) > len(SERIES):
        fail(f"{where} has {len(order)} groups; at most {len(SERIES)} can be told apart "
             "by color. Drop the groups or use a table.")
    for k, g in enumerate(order):
        if not is_id(g):
            fail(f"{where}[{k}] must be text or an integer, got {g!r}")
        if g in order[:k]:
            fail(f"{where} lists group {g!r} twice")
    return list(order)


def add_group(order: list, g, where: str) -> None:
    order.append(g)
    if len(order) > len(SERIES):
        fail(f"{where} has {len(order)} groups; at most {len(SERIES)} can be "
             "told apart by color. Drop the groups or use a table.")


def check_shape(view) -> None:
    """Bound the view before anything is drawn: the number of values, their
    nesting, and the printed length of every string and integer."""
    stack, pushed = [(view, "view", 0)], 1
    while stack:
        x, where, depth = stack.pop()
        if depth > MAX_DEPTH:
            fail(f"{where} is nested deeper than {MAX_DEPTH} levels")
        if isinstance(x, (dict, list)):
            pushed += len(x)
            if pushed > MAX_VIEW_VALUES:
                fail(f"the view has more than {MAX_VIEW_VALUES} values")
            pairs = x.items() if isinstance(x, dict) else enumerate(x)
            fmt_key = "{}.{}" if isinstance(x, dict) else "{}[{}]"
            stack.extend((v, fmt_key.format(where, k), depth + 1) for k, v in pairs)
        elif isinstance(x, (str, int)) and not isinstance(x, bool) and len(str(x)) > MAX_TEXT:
            fail(f"{where} is longer than {MAX_TEXT} characters")


def take(d, where: str, required=(), optional=()) -> None:
    """Reject a spec object with missing or unknown keys, or with a list or
    object where text belongs."""
    allowed = f"required: {sorted(required)}, optional: {sorted(optional)}. {SPEC_DOC}"
    if not isinstance(d, dict):
        fail(f"{where} must be an object with keys {allowed} Got {type(d).__name__}.")
    missing = [k for k in required if k not in d]
    if missing:
        fail(f"{where} is missing {missing}; {allowed}")
    unknown = sorted(set(d) - set(required) - set(optional))
    if unknown:
        fail(f"{where} has unknown keys {unknown}; {allowed}")
    for k in TEXT_KEYS.intersection(d):
        scalar(d[k], f"{where}.{k}")


def num(x, where: str) -> float:
    # Compared rather than converted: float() overflows on large integers.
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not -1e300 < x < 1e300:
        fail(f"{where} must be a finite number below 1e300 in magnitude, got {x!r}")
    return x


def items_of(x, where: str) -> list:
    if not isinstance(x, list):
        fail(f"{where} must be a list, got {type(x).__name__}")
    return x


def count(x, where: str) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or x < 0:
        fail(f"{where} must be a non-negative integer, got {x!r}")
    return x


def is_id(x) -> bool:
    return isinstance(x, (int, str)) and not isinstance(x, bool)


def flag(d: dict, key: str, where: str, default: bool) -> bool:
    if not isinstance(d.get(key, default), bool):
        fail(f"{where}.{key} must be true or false, got {d[key]!r}")
    return d.get(key, default)


def group_of(d: dict, where: str):
    g = d.get("group")
    if g is not None and not is_id(g):
        fail(f"{where}.group must be text or an integer, got {g!r}")
    return g


def ticks(lo: float, hi: float, integer: bool = False, target: int = 6) -> list:
    """Round tick values covering [lo, hi]."""
    # A span too small to divide (equal or subnormal values, or lost to
    # rounding next to a large lo) gets a unit-scale axis instead.
    if not hi - lo > max(1e-300, 4 * sys.float_info.epsilon * abs(lo)):
        hi = lo + max(1.0, abs(lo))
    raw = (hi - lo) / target
    mag = 10.0 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if raw <= m * mag * (1 + 1e-9))
    if integer:
        step = max(1.0, round(step))
    first = math.floor(lo / step + 1e-9)
    last = math.ceil(hi / step - 1e-9)
    return [k * step for k in range(first, last + 1)]


# ---------------------------------------------------------------- styling

def _color_rules() -> list:
    rules = [(".vq-bg", "fill", SURFACE), (".vq-t", "fill", INK),
             (".vq-t2", "fill", INK2), (".vq-tm", "fill", MUTED),
             (".vq-grid", "stroke", GRID), (".vq-axis", "stroke", AXIS),
             (".vq-e", "stroke", AXIS), (".vq-eh", "stroke", INK),
             (".vq-em", "stroke", INK2),
             (".vq-ev", "stroke", CRITICAL), (".vq-mark", "stroke", INK),
             (".vq-ring", "stroke", SURFACE), (".vq-halo", "stroke", SURFACE),
             (".vq-n0", "fill", NEUTRAL), (".vq-n0", "stroke", AXIS),
             (".vq-mute", "fill", DEEMPH), (".vq-off", "fill", NEUTRAL),
             (".vq-crit", "fill", CRITICAL), (".vq-good", "fill", GOOD),
             (".vq-neg", "fill", SERIES[7])]
    for k, (fill, ink) in enumerate(zip(SERIES, SERIES_INK), 1):
        rules += [(f".vq-s{k}", "fill", fill), (f".vq-l{k}", "stroke", fill),
                  (f".vq-o{k}", "fill", ink)]
    return rules


# Page chrome tokens, (light, dark).
PAGE_TOKENS = [("plane", PLANE), ("surface", SURFACE), ("ink", INK), ("ink2", INK2),
               ("muted", MUTED), ("line", GRID), ("good", ("#006300", "#0ca30c")),
               ("bad", ("#d03b3b", "#e66767")), ("good-bg", ("#e7f5e7", "#12301a")),
               ("bad-bg", ("#fbeaea", "#3a1b1b"))]


def _token(sel: str, prop: str) -> str:
    return f"--{sel[1:]}-{prop}"


def figure_css() -> str:
    """CSS for the figure classes, colored by the tokens of theme_css."""
    lines = "".join(f".vq-l{k}," for k in range(1, len(SERIES) + 1))
    static = (
        f".vq-fig text{{font-family:{FONT}}}"
        f"{lines}.vq-e,.vq-eh,.vq-em,.vq-ev,.vq-mark,.vq-grid,.vq-axis"
        "{fill:none;stroke-linecap:round}"
        ".vq-grid,.vq-axis{stroke-width:1}"
        ".vq-e{stroke-width:1.25}.vq-eh{stroke-width:2.5}.vq-em{stroke-width:1.5}"
        ".vq-ev{stroke-width:2.5;stroke-dasharray:6 4}"
        ".vq-mark{stroke-width:2}.vq-route{stroke-width:2}"
        ".vq-ring{stroke-width:2}.vq-n0{stroke-width:1.25}"
        ".vq-halo{paint-order:stroke;stroke-width:3px;stroke-linejoin:round}"
        ".vq-m:hover{opacity:.8}")
    return static + "".join(f"{sel}{{{prop}:var({_token(sel, prop)})}}"
                            for sel, prop, _ in _color_rules())


def theme_css() -> str:
    """Every color token of the page. Light values sit on :root. The dark
    values apply under a dark system setting unless the viewer chose light,
    and under an explicit dark choice."""
    tokens = [(f"--{name}", c) for name, c in PAGE_TOKENS]
    tokens += [(_token(sel, prop), c) for sel, prop, c in _color_rules()]
    light = ";".join(f"{name}:{c[0]}" for name, c in tokens)
    dark = ";".join(f"{name}:{c[1]}" for name, c in tokens) + ";color-scheme:dark"
    return (f":root{{color-scheme:light;{light}}}"
            f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{dark}}}}}'
            f':root[data-theme="dark"]{{{dark}}}')


# ----------------------------------------------------------- SVG primitives

def text(x, y, s, cls="vq-t", size=12, anchor="start", weight=None, extra="") -> str:
    w = f' font-weight="{weight}"' if weight else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" font-size="{size}" '
            f'text-anchor="{anchor}"{w}{extra}>{esc(s)}</text>')


def tip(s, mark: str = "") -> str:
    """Hover text clipped to MAX_TIP characters, ``mark`` kept at its end."""
    s, room = str(s), MAX_TIP - len(mark)
    return f"<title>{esc((s if len(s) <= room else s[:room - 1] + '…') + mark)}</title>"


def _polygon(cx, cy, r, n, rot) -> str:
    return " ".join(
        f"{cx + r * math.sin(rot + 2 * math.pi * i / n):.1f},"
        f"{cy - r * math.cos(rot + 2 * math.pi * i / n):.1f}" for i in range(n))


def node_shape(shape: str, cx, cy, r, cls: str) -> str:
    if shape == "circle":
        return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" class="{cls}"/>'
    if shape == "star":
        pts = " ".join(
            f"{cx + (1.4 if i % 2 == 0 else 0.65) * r * math.sin(math.pi * i / 5):.1f},"
            f"{cy - (1.4 if i % 2 == 0 else 0.65) * r * math.cos(math.pi * i / 5):.1f}"
            for i in range(10))
        return f'<polygon points="{pts}" class="{cls}"/>'
    # Sides, rotation, and a radius factor that equalizes apparent size.
    sides, rot, k = {"square": (4, math.pi / 4, 1.2), "diamond": (4, 0.0, 1.25),
                     "hexagon": (6, math.pi / 6, 1.1), "tri-up": (3, 0.0, 1.4),
                     "tri-down": (3, math.pi, 1.4), "pentagon": (5, 0.0, 1.15)}[shape]
    return f'<polygon points="{_polygon(cx, cy, r * k, sides, rot)}" class="{cls}"/>'


def bar_path(x0, x1, y, h, rl=0.0, rr=0.0) -> str:
    w = x1 - x0
    rl, rr = min(rl, w / 2, h / 2), min(rr, w / 2, h / 2)
    return (f"M{x0 + rl:.1f},{y:.1f}H{x1 - rr:.1f}"
            f"Q{x1:.1f},{y:.1f} {x1:.1f},{y + rr:.1f}V{y + h - rr:.1f}"
            f"Q{x1:.1f},{y + h:.1f} {x1 - rr:.1f},{y + h:.1f}H{x0 + rl:.1f}"
            f"Q{x0:.1f},{y + h:.1f} {x0:.1f},{y + h - rl:.1f}V{y + rl:.1f}"
            f"Q{x0:.1f},{y:.1f} {x0 + rl:.1f},{y:.1f}Z")


def column_path(x0, x1, top, base, r=4.0) -> str:
    """Vertical bar: rounded at the data end, square at the baseline."""
    r = min(r, (x1 - x0) / 2, (base - top) / 2)
    return (f"M{x0:.1f},{base:.1f}V{top + r:.1f}Q{x0:.1f},{top:.1f} {x0 + r:.1f},{top:.1f}"
            f"H{x1 - r:.1f}Q{x1:.1f},{top:.1f} {x1:.1f},{top + r:.1f}V{base:.1f}Z")


def legend(entries) -> tuple:
    """Flow legend entries left to right from the top-left corner.

    entries: (kind, cls, label, shape) with kind in node / start / edge / rect.
    Returns (svg, height).
    """
    if not entries:
        return "", 0.0
    out, x, y = [], float(PAD), 20.0
    for kind, cls, label, shape in entries:
        width = 24 + tw(label, 12) + 18
        if x + width > W - PAD and x > PAD:
            x, y = float(PAD), y + 22
        if kind == "start":
            out.append(f'<circle cx="{x + 9:.1f}" cy="{y - 4:.1f}" r="8" class="vq-eh" '
                       'style="stroke-width:1.5"/>'
                       + node_shape("circle", x + 9, y - 4, 5, cls))
        elif kind == "node":
            out.append(node_shape(shape or "circle", x + 8, y - 4, 6, cls))
        elif kind == "edge":
            out.append(f'<line x1="{x:.1f}" y1="{y - 4:.1f}" x2="{x + 18:.1f}" '
                       f'y2="{y - 4:.1f}" class="{cls}"/>')
        else:
            out.append(f'<rect x="{x + 2:.1f}" y="{y - 10:.1f}" width="12" '
                       f'height="12" rx="2" class="{cls}"/>')
        out.append(text(x + 24, y, label, "vq-t2"))
        x += width
    return "".join(out), y + 10


class Figure:
    def __init__(self, kind, title, caption, height, body, tables=(), violations=0,
                 simplified=None, width=W):
        self.kind = kind
        self.width = width
        self.violations = violations   # marks drawn as violated
        self.simplified = simplified   # how the figure was coarsened for the page footnote
        self.title = title
        self.caption = caption
        self.height = height
        self.body = body
        self.tables = list(tables)   # (heading, columns, rows)

    def svg(self) -> str:
        return (f'<svg class="vq-fig" data-figure="{self.kind}" '
                f'viewBox="0 0 {self.width} {self.height:.0f}" role="img" '
                f'aria-label="{esc(self.title or self.kind)}">{self.body}</svg>')


# ------------------------------------------------------------------ layouts

def force_layout(n: int, pairs, seed: int = 7, iters: int = 250):
    """Fruchterman-Reingold layout. Deterministic for a given input."""
    import numpy as np

    ang = 2 * np.pi * np.arange(n) / max(n, 1)
    pos = 0.5 * np.c_[np.cos(ang), np.sin(ang)]
    if n < 3 or not pairs:
        return pos
    pos = pos + np.random.default_rng(seed).normal(0.0, 1e-3, (n, 2))
    adj = np.zeros((n, n))
    for u, v in pairs:
        adj[u, v] = adj[v, u] = 1.0
    k = 1.0 / math.sqrt(n)
    t, dt = 0.1, 0.1 / (iters + 1)
    for _ in range(iters):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.clip(np.linalg.norm(delta, axis=-1), 0.01, None)
        disp = np.einsum("ijk,ij->ik", delta, k * k / dist ** 2 - adj * dist / k)
        # Pull toward the centroid so disconnected components stay in frame.
        disp -= (pos - pos.mean(0)) * 0.6
        length = np.linalg.norm(disp, axis=-1)
        length = np.where(length < 0.01, 0.1, length)
        pos = pos + disp * (t / length)[:, None]
        t -= dt
    centered = pos - pos.mean(0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    return centered @ vt.T       # principal axis horizontal


def mds_layout(dist):
    """Classical multidimensional scaling of a distance matrix to 2-D."""
    import numpy as np

    d = np.asarray(dist, dtype=float)
    d = (d + d.T) / 2.0
    n = d.shape[0]
    j = np.eye(n) - np.ones((n, n)) / n
    vals, vecs = np.linalg.eigh(-0.5 * j @ (d ** 2) @ j)
    top = np.argsort(vals)[::-1][:2]
    return vecs[:, top] * np.sqrt(np.clip(vals[top], 0.0, None))


def place(pos, top: float, keep_aspect: bool, margin: float) -> tuple:
    """Map layout coordinates to pixels below `top`. Returns (xy, height)."""
    import numpy as np

    pos = np.asarray(pos, dtype=float)
    lo, hi = pos.min(0), pos.max(0)
    flat = (hi - lo) <= 1e-12 * max(1.0, float(np.abs(pos).max()))
    span = np.where(flat, 1.0, hi - lo)
    inner_w = W - 2 * margin
    aspect = 0.6 if flat.any() else span[1] / span[0]
    inner_h = float(np.clip(inner_w * aspect, 200.0, 420.0))
    inner = np.array([inner_w, inner_h])
    if keep_aspect:
        scale = float(min(inner / span))
        xy = (pos - lo) * scale + (inner - span * scale) / 2.0
    else:
        xy = (pos - lo) / span * inner
    xy[:, flat] = inner[flat] / 2.0
    return xy + np.array([margin, top + margin]), top + inner_h + 2 * margin


# ------------------------------------------------------------------ network

def draw_network(nodes, edges, xy, radius: float, directed: bool = False) -> str:
    """nodes: {label, fill, ink, shape, tip, start}; edges: {u, v, cls, tip,
    text, head}. Edges are drawn in list order, nodes on top."""
    n = len(nodes)
    out = []
    for e in edges:
        (x1, y1), (x2, y2) = xy[e["u"]], xy[e["v"]]
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        if directed:
            back = radius + 3
            x2, y2 = x2 - ux * back, y2 - uy * back
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                   f'class="vq-m {e["cls"]}">{tip(e["tip"], e.get("mark", ""))}</line>')
        if directed:
            bx, by = x2 - ux * 9, y2 - uy * 9
            out.append(f'<polygon points="{x2:.1f},{y2:.1f} '
                       f'{bx - uy * 4.5:.1f},{by + ux * 4.5:.1f} '
                       f'{bx + uy * 4.5:.1f},{by - ux * 4.5:.1f}" class="{e["head"]}"/>')
    for e in edges:
        if e.get("text"):
            (x1, y1), (x2, y2) = xy[e["u"]], xy[e["v"]]
            out.append(text((x1 + x2) / 2, (y1 + y2) / 2 + 3.5, e["text"],
                            "vq-t2 vq-halo", 10, "middle"))
    inside = radius >= 13
    outside = n <= 60
    for nd, (x, y) in zip(nodes, xy):
        g = [f'<g class="vq-m">{tip(nd["tip"])}']
        if radius < 12:
            g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="transparent"/>')
        if nd.get("start"):
            g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius + 4:.1f}" '
                     'class="vq-eh" style="stroke-width:1.5"/>')
        ring = "" if nd["fill"] == "vq-n0" else " vq-ring"
        g.append(node_shape(nd["shape"], x, y, radius, nd["fill"] + ring))
        label = nd["label"]
        if inside and tw(label, 11) <= 2 * radius - 4:
            g.append(text(x, y + 4, label, nd["ink"], 11, "middle", 600))
        elif outside:
            g.append(text(x, y + radius + 13, clip(label, 90, 11), "vq-t2 vq-halo",
                          11, "middle"))
        g.append("</g>")
        out.append("".join(g))
    return "".join(out)


def group_slots(order) -> dict:
    """Series slot per group, in the given order. Groups all named after
    distinct palette colors take those colors instead, so that a group
    called "red" is not drawn blue."""
    named = [COLOR_NAMES.get(str(g).strip().lower()) for g in order]
    if order and all(named) and len(set(named)) == len(named):
        return dict(zip(order, named))
    return {g: k for k, g in enumerate(order, 1)}


def node_radius(n: int) -> float:
    for limit, r in ((12, 16.0), (30, 13.0), (80, 9.0), (200, 6.0)):
        if n <= limit:
            return r
    return 4.0


def network_figure(kind, title, caption, labels, groups, group_order, edges,
                   edge_labels, coords=None, node_tips=None, edge_tips=None,
                   simplified=None, edge_legend=None, soft=False, unit="") -> Figure:
    """Node-link figure. groups[i] is a group name or None (ungrouped);
    edges are (u, v, weight or None, status) with status in plain /
    highlight / violated. node_tips and edge_tips replace the hover text
    built from the labels. The edge states are listed in the legend when
    ``edge_legend`` is true, or by default when any edge is not plain.
    ``soft`` draws highlighted edges in a mid tone instead of the ink, for a
    figure where they are context rather than the result. ``unit`` is the
    unit of the edge weights."""
    import numpy as np

    n = len(labels)
    if len(group_order) > len(SERIES):
        fail(f"{kind} figure has {len(group_order)} groups; at most "
             f"{len(SERIES)} can be told apart by color. Use a grid or table.")
    slot = group_slots(group_order)
    shaped = len(group_order) > 3   # color alone stops being safe past three
    counts = {g: sum(1 for x in groups if x == g) for g in group_order}

    entries = [("node", f"vq-s{slot[g]}", f"{g} ({counts[g]})",
                SHAPES[slot[g] - 1] if shaped else "circle") for g in group_order]
    if group_order and any(g is None for g in groups):
        entries.append(("node", "vq-n0", edge_labels.get("ungrouped", "other")
                        + f" ({sum(1 for g in groups if g is None)})", "circle"))
    present = {s: sum(1 for e in edges if e[3] == s)
               for s in ("highlight", "plain", "violated")}
    cls_of = {"plain": "vq-e", "highlight": "vq-em" if soft else "vq-eh", "violated": "vq-ev"}
    listed = edge_legend          # True lists highlight and plain even when absent
    if edge_legend is None:
        edge_legend = bool(present["highlight"] or present["violated"])
    if edge_legend:
        for s in ("highlight", "plain", "violated"):
            if present[s] or (listed and s != "violated"):
                entries.append(("edge", cls_of[s], f"{edge_labels[s]} ({present[s]})", None))
    leg, top = legend(entries)

    radius = node_radius(n)
    margin = radius + 26
    pairs = [(u, v) for u, v, _, _ in edges]
    if coords is not None:
        pos = np.asarray(coords, dtype=float) * np.array([1.0, -1.0])  # y up
        xy, height = place(pos, top, True, margin)
    else:
        xy, height = place(force_layout(n, pairs), top, False, margin)

    weights = [w for _, _, w, _ in edges if w is not None]
    show_w = bool(weights) and len(set(weights)) > 1 and len(edges) <= 40
    drawn = []
    for status in ("plain", "highlight", "violated"):
        for k, (u, v, w, s) in enumerate(edges):
            if s != status:
                continue
            t = edge_tips[k] if edge_tips else f"{labels[u]} – {labels[v]}"
            if w is not None and not edge_tips:
                t += f" · weight {with_unit(w, unit)}"
            mark = f" · {edge_labels[s]}" if edge_legend else ""
            drawn.append({"u": u, "v": v, "cls": cls_of[s], "tip": t, "mark": mark,
                          "text": with_unit(w, unit) if show_w and w is not None else None})
    nodes = []
    for i, (label, g) in enumerate(zip(labels, groups)):
        k = slot.get(g)
        nodes.append({
            "label": label,
            "fill": f"vq-s{k}" if k else "vq-n0",
            "ink": f"vq-o{k}" if k else "vq-t",
            "shape": SHAPES[k - 1] if k and shaped else "circle",
            "tip": node_tips[i] if node_tips else
                   f"{label}" + (f" · {g}" if g is not None else ""),
        })
    body = leg + draw_network(nodes, drawn, xy, radius)
    tables = [("Nodes", ["node", "group"],
               [[lab, "" if g is None else g] for lab, g in zip(labels, groups)])]
    if edges:
        tables.append(("Edges", ["from", "to", col_name("weight", unit), "state"],
                       [[labels[u], labels[v], "" if w is None else w,
                         edge_labels[s]] for u, v, w, s in edges]))
    return Figure(kind, title, caption, height, body, tables, present["violated"],
                  simplified)


def _parse_nodes(raw, where: str, grouped: bool = True) -> tuple:
    """Returns (ids, labels, groups, coords or None)."""
    ids, labels, groups, coords = [], [], [], []
    if len(items_of(raw, where)) > MAX_NODES:
        fail(f"{where} has {len(raw)} entries; the limit is {MAX_NODES} nodes. "
             "Show an aggregate instead (bars, grid, or table).")
    for k, nd in enumerate(raw):
        w = f"{where}[{k}]"
        if isinstance(nd, dict):
            take(nd, w, ["id"], ["label", "x", "y"] + (["group"] if grouped else []))
            if not is_id(nd["id"]):
                fail(f"{w}.id must be an integer or a string, got {nd['id']!r}")
            if ("x" in nd) != ("y" in nd):
                fail(f"{w} must give both x and y, or neither")
            ids.append(nd["id"])
            labels.append(str(nd.get("label", nd["id"])))
            groups.append(group_of(nd, w))
            coords.append((num(nd["x"], f"{w}.x"), num(nd["y"], f"{w}.y"))
                          if "x" in nd else None)
        elif is_id(nd):
            ids.append(nd)
            labels.append(str(nd))
            groups.append(None)
            coords.append(None)
        else:
            fail(f"{w} must be an id or an object with an id, got {nd!r}")
    if not ids:
        fail(f"{where} is empty")
    if len(set(ids)) != len(ids):
        seen, dup = set(), set()
        for i in ids:
            (dup if i in seen else seen).add(i)
        fail(f"{where} has duplicate ids {sorted(str(i) for i in dup)}")
    placed = [c is not None for c in coords]
    if any(placed) and not all(placed):
        fail(f"{where}: either every node has x and y, or none does")
    return ids, labels, groups, (coords if all(placed) else None)


def view_graph(v: dict, where: str) -> Figure:
    take(v, where, ["type", "nodes", "edges"],
         ["title", "caption", "groups", "edge_rule", "edge_labels", "unit"])
    ids, labels, groups, coords = _parse_nodes(v["nodes"], f"{where}.nodes")
    index = {i: k for k, i in enumerate(ids)}

    order = group_list(v.get("groups", []), f"{where}.groups")
    for g in groups:
        if g is not None and g not in order:
            if "groups" in v:
                fail(f'{where}: node group {g!r} is not listed in "groups"')
            add_group(order, g, where)
    if len(items_of(v["edges"], f"{where}.edges")) > MAX_EDGES:
        fail(f"{where} has {len(v['edges'])} edges; the limit is {MAX_EDGES}. Show an "
             "aggregate instead (bars, grid, or table).")

    rule = v.get("edge_rule")
    if rule not in (None, "cut", "conflict"):
        fail(f'{where}.edge_rule must be "cut" or "conflict", got {rule!r}')
    names = {"highlight": "cut edge" if rule == "cut" else "highlighted edge",
             "plain": "uncut edge" if rule == "cut" else "edge",
             "violated": "conflict" if rule == "conflict" else "violated edge",
             "ungrouped": "other"}
    custom = v.get("edge_labels", {})
    take(custom, f"{where}.edge_labels", [], list(names))
    names.update({k: str(scalar(label, f"{where}.edge_labels.{k}"))
                  for k, label in custom.items()})

    edges, seen = [], set()
    for k, e in enumerate(items_of(v["edges"], f"{where}.edges")):
        w = f"{where}.edges[{k}]"
        if isinstance(e, dict):
            take(e, w, ["u", "v"], ["weight", "status"])
            a, b, weight, status = e["u"], e["v"], e.get("weight"), e.get("status")
        elif isinstance(e, list) and len(e) in (2, 3):
            a, b, weight, status = e[0], e[1], (e[2] if len(e) == 3 else None), None
        else:
            fail(f"{w} must be [u, v], [u, v, weight], or an object, got {e!r}")
        for end in (a, b):
            if not is_id(end) or end not in index:
                fail(f"{w} refers to unknown node {end!r}")
        if a == b:
            fail(f"{w} is a self-loop on {a!r}")
        if frozenset((a, b)) in seen:
            fail(f"{w} repeats the edge between {a!r} and {b!r}; give each "
                 "pair once, with the weights summed")
        seen.add(frozenset((a, b)))
        if weight is not None:
            num(weight, f"{w}.weight")
        u, t = index[a], index[b]
        if status is None:
            gu, gt = groups[u], groups[t]
            if rule == "cut" and gu is not None and gt is not None and gu != gt:
                status = "highlight"
            elif rule == "conflict" and gu is not None and gu == gt:
                status = "violated"
            else:
                status = "plain"
        if status not in ("plain", "highlight", "violated"):
            fail(f'{w}.status must be "plain", "highlight", or "violated", '
                 f"got {status!r}")
        edges.append((u, t, weight, status))
    return network_figure("graph", v.get("title", ""), v.get("caption", ""),
                          labels, groups, order, edges, names, coords,
                          unit=str(v.get("unit", "")))


def view_route(v: dict, where: str) -> Figure:
    import numpy as np

    take(v, where, ["type", "points", "routes"], ["title", "caption", "distances", "unit"])
    unit = str(v.get("unit", ""))
    ids, labels, _, coords = _parse_nodes(v["points"], f"{where}.points", grouped=False)
    index = {i: k for k, i in enumerate(ids)}
    n = len(ids)
    routes = items_of(v["routes"], f"{where}.routes")
    if not routes:
        fail(f"{where}.routes is empty")
    if len(routes) > len(SERIES):
        fail(f"{where} has {len(routes)} routes; at most {len(SERIES)} can be "
             "told apart by color. Split them over several route figures.")

    dist = None
    if "distances" in v:
        rows_ = items_of(v["distances"], f"{where}.distances")
        if len(rows_) != n or any(not isinstance(r, list) or len(r) != n for r in rows_):
            fail(f"{where}.distances must be a {n}x{n} matrix to match the points")
        for a, row in enumerate(rows_):
            for b, d in enumerate(row):
                # The layout squares the distances.
                if abs(num(d, f"{where}.distances[{a}][{b}]")) >= 1e150:
                    fail(f"{where}.distances[{a}][{b}] must be below 1e150 in magnitude, "
                         f"got {d!r}")
        dist = np.asarray(rows_, dtype=float)

    entries, edges, starts, rows = [], [], set(), []
    for r, route in enumerate(routes):
        w = f"{where}.routes[{r}]"
        take(route, w, ["order"], ["label", "closed"])
        order = items_of(route["order"], f"{w}.order")
        if len(edges) + len(order) > MAX_EDGES:
            fail(f"{where} has more than {MAX_EDGES} legs; the limit is {MAX_EDGES}")
        for stop in order:
            if not is_id(stop) or stop not in index:
                fail(f"{w}.order refers to unknown point {stop!r}")
        if len(order) < 2:
            fail(f"{w}.order needs at least two points")
        seq = [index[s] for s in order]
        if flag(route, "closed", w, True):
            seq.append(seq[0])
        name = str(route.get("label", f"route {r + 1}" if len(routes) > 1 else "route"))
        total = 0.0
        for step, (a, b) in enumerate(zip(seq, seq[1:]), 1):
            t = f"{name} · step {step}: {labels[a]} → {labels[b]}"
            d = None
            if dist is not None:
                d = float(dist[a, b])
                total += d
                t += f" · {with_unit(d, unit)}"
            edges.append({"u": a, "v": b, "cls": f"vq-l{r + 1} vq-route",
                          "head": f"vq-s{r + 1}", "tip": t, "d": d})
            rows.append([name, step, labels[a], labels[b], "" if d is None else d])
        starts.add(seq[0])
        entries.append(("edge", f"vq-l{r + 1} vq-route",
                        name + (f" (length {with_unit(total, unit)})" if dist is not None
                                else ""),
                        None))
    entries.append(("start", "vq-n0", "start", None))
    leg, top = legend(entries)

    if len(edges) <= 40:
        for e in edges:
            e["text"] = None if e["d"] is None else with_unit(e["d"], unit)
    radius = node_radius(n)
    margin = radius + 26
    if coords is not None:
        pos = np.asarray(coords, dtype=float) * np.array([1.0, -1.0])
    elif dist is not None:
        pos = mds_layout(dist)
    else:
        ang = 2 * np.pi * np.arange(n) / n
        pos = np.c_[np.sin(ang), -np.cos(ang)]
    xy, height = place(pos, top, True, margin)
    nodes = [{"label": lab, "fill": "vq-n0", "ink": "vq-t",
              "shape": "circle", "start": i in starts,
              "tip": lab + (" · start" if i in starts else "")}
             for i, lab in enumerate(labels)]
    body = leg + draw_network(nodes, edges, xy, radius, directed=True)
    caption = v.get("caption", "")
    if coords is None and dist is not None:
        caption = (caption + " " if caption else "") + (
            "Positions are a 2-D embedding of the distance matrix, so drawn "
            "lengths are approximate.")
    return Figure("route", v.get("title", ""), caption, height, body,
                  [("Steps", ["route", "step", "from", "to", col_name("distance", unit)],
                    rows)])


# -------------------------------------------------------------------- lanes

def draw_lanes(rows, unit="", entries=(), mark_label=None, zero=True) -> tuple:
    """Horizontal lanes on one shared axis. Returns (svg, height).

    rows: {label, segs: [{a, b, cls, ink, text, tip, rl, rr, flag}],
           end, flag, marks}. A flag draws the row or segment as violated.
    """
    if len(rows) > MAX_LANES:
        fail(f"figure has {len(rows)} rows; the limit is {MAX_LANES}. "
             "Aggregate the rows or use a table.")
    segments = sum(len(r["segs"]) for r in rows)
    if segments > MAX_SEGMENTS:
        fail(f"figure has {segments} bars; the limit is {MAX_SEGMENTS}. "
             "Aggregate the items or use a table.")
    label_w = min(180.0, max(tw(r["label"]) for r in rows))
    left = PAD + label_w + 10
    end_w = max((tw(r.get("end") or "") + (16 if r.get("flag") else 0) for r in rows),
                default=0.0)
    right = W - PAD - (end_w + 10 if end_w else 10)
    values = [x for r in rows for s in r["segs"] for x in (s["a"], s["b"])]
    values += [m for r in rows for m in r.get("marks", [])]
    base = [0.0] if zero or not values else []   # bars grow from zero
    lo, hi = min(base + values), max(base + values)
    tk = ticks(lo, hi)
    lo, hi = min(lo, tk[0]), max(hi, tk[-1])

    def x_of(val):
        return left + (val - lo) / (hi - lo) * (right - left)

    leg, top = legend(entries)
    top += 22 if mark_label else 12
    pitch, bar = 38.0, 22.0
    bottom = top + len(rows) * pitch
    out = [leg]
    for t in tk:
        x = x_of(t)
        out.append(f'<line x1="{x:.1f}" y1="{top - 4:.1f}" x2="{x:.1f}" '
                   f'y2="{bottom:.1f}" class="{"vq-axis" if t == 0 else "vq-grid"}"/>')
        out.append(text(x, bottom + 16, fmt(t), "vq-tm", 11, "middle"))
    if unit:
        out.append(text((left + right) / 2, bottom + 34, unit, "vq-tm", 11, "middle"))
    for k, r in enumerate(rows):
        y = top + k * pitch + (pitch - bar) / 2
        out.append(text(left - 10, y + bar / 2 + 4, clip(r["label"], label_w),
                        "vq-t2", 12, "end"))
        for s in r["segs"]:
            x0, x1 = sorted((x_of(s["a"]), x_of(s["b"])))
            if x1 - x0 > 4:
                x0, x1 = x0 + 1, x1 - 1      # 2px surface gap between segments
            d = bar_path(x0, x1, y, bar, s.get("rl", 0.0), s.get("rr", 0.0))
            mark = " · violated" if s.get("flag") else ""
            out.append(f'<path d="{d}" class="vq-m {s["cls"]}">{tip(s["tip"], mark)}</path>')
            if s.get("flag"):
                out.append(f'<path d="{d}" class="vq-ev"/>')
            if s.get("text") and tw(s["text"], 11) + 10 <= x1 - x0:
                out.append(text((x0 + x1) / 2, y + bar / 2 + 4, s["text"], s["ink"],
                                11, "middle", extra=' pointer-events="none"'))
        for m in r.get("marks", []):
            x = x_of(m)
            out.append(f'<line x1="{x:.1f}" y1="{y - 5:.1f}" x2="{x:.1f}" '
                       f'y2="{y + bar + 5:.1f}" class="vq-mark"/>')
        if r.get("end"):
            x = right + 10
            if r.get("flag"):
                out.append(f'<circle cx="{x + 5:.1f}" cy="{y + bar / 2:.1f}" r="5" '
                           'class="vq-crit"/>'
                           + text(x + 5, y + bar / 2 + 3.5, "!", "vq-bg", 9, "middle", 700))
                x += 16
            out.append(text(x, y + bar / 2 + 4, r["end"], "vq-t", 12, weight=600))
    if mark_label:
        marks = {m for r in rows for m in r.get("marks", [])}
        x = x_of(marks.pop())
        anchor = "end" if x > (left + right) / 2 else "start"
        out.append(text(x + (-6 if anchor == "end" else 6), top - 8, mark_label,
                        "vq-t2", 11, anchor))
    return "".join(out), bottom + (44 if unit else 26)


def count_bars(lists, where: str) -> None:
    """Refuse more than MAX_SEGMENTS bars before any is built."""
    total = sum(len(x) for x in lists if isinstance(x, list))
    if total > MAX_SEGMENTS:
        fail(f"{where} has {total} bars; the limit is {MAX_SEGMENTS}")


def _unit(v: dict) -> str:
    return f" {v['unit']}" if v.get("unit") else ""


def view_bins(v: dict, where: str) -> Figure:
    take(v, where, ["type", "bins"],
         ["title", "caption", "unit", "capacity", "unassigned", "unassigned_label",
          "size_label", "value_label"])
    bins = items_of(v["bins"], f"{where}.bins")
    unassigned = v.get("unassigned", [])
    if len(bins) + bool(unassigned) > MAX_LANES:
        fail(f"{where} has {len(bins)} bins; the limit is {MAX_LANES} rows")
    count_bars([b.get("items") for b in bins if isinstance(b, dict)] + [unassigned], where)
    size_label = v.get("size_label", "size")
    value_label = v.get("value_label", "value")
    unit = _unit(v)
    table, rows = [], []

    def segments(items, w, cls, ink, bin_label):
        segs, at = [], 0.0
        for k, it in enumerate(items_of(items, w)):
            take(it, f"{w}[{k}]", ["label", "size"], ["value"])
            size = num(it["size"], f"{w}[{k}].size")
            if size < 0:
                fail(f"{w}[{k}].size is negative")
            t = f"{it['label']} · {size_label} {fmt(size)}{unit}"
            if "value" in it:
                t += f" · {value_label} {fmt(num(it['value'], f'{w}[{k}].value'))}"
            segs.append({"a": at, "b": at + size, "cls": cls, "ink": ink,
                         "text": str(it["label"]), "tip": t})
            table.append([bin_label, it["label"], size,
                          it["value"] if "value" in it else ""])
            at += size
        if segs:
            segs[-1]["rr"] = 4.0
        return segs, at

    capacities = set()
    for k, b in enumerate(items_of(v["bins"], f"{where}.bins")):
        w = f"{where}.bins[{k}]"
        take(b, w, ["label", "items"], ["capacity"])
        segs, total = segments(b["items"], f"{w}.items", "vq-s1", "vq-o1", b["label"])
        cap = b["capacity"] if b.get("capacity") is not None else v.get("capacity")
        row = {"label": str(b["label"]), "segs": segs, "end": fmt(total) + unit}
        if cap is not None:
            num(cap, f"{w}.capacity")
            capacities.add(cap)
            row["marks"] = [cap]
            row["end"] = f"{fmt(total)} / {fmt(cap)}{unit}"
            row["flag"] = total > cap + 1e-9 * max(1.0, abs(cap))
        rows.append(row)
    if not rows:
        fail(f"{where}.bins is empty")
    if v.get("unassigned"):
        name = str(v.get("unassigned_label", "Not selected"))
        segs, total = segments(v["unassigned"], f"{where}.unassigned", "vq-mute",
                               "vq-t", name)
        rows.append({"label": name, "segs": segs, "end": fmt(total) + unit})
    mark_label = (f"capacity {fmt(next(iter(capacities)))}{unit}"
                  if len(capacities) == 1 else None)
    body, height = draw_lanes(rows, v.get("unit", ""), mark_label=mark_label)
    return Figure("bins", v.get("title", ""), v.get("caption", ""), height, body,
                  [("Items", ["bin", "item", col_name(size_label, v.get("unit", "")),
                              value_label], table)],
                  sum(1 for r in rows if r.get("flag")))


def view_schedule(v: dict, where: str) -> Figure:
    take(v, where, ["type", "rows"], ["title", "caption", "unit", "groups"])
    order = group_list(v.get("groups", []), f"{where}.groups")
    lanes = items_of(v["rows"], f"{where}.rows")
    if len(lanes) > MAX_LANES:
        fail(f"{where} has {len(lanes)} rows; the limit is {MAX_LANES}")
    count_bars([r.get("tasks") for r in lanes if isinstance(r, dict)], where)
    parsed = []
    for k, r in enumerate(lanes):
        w = f"{where}.rows[{k}]"
        take(r, w, ["label", "tasks"])
        tasks = []
        for j, t in enumerate(items_of(r["tasks"], f"{w}.tasks")):
            wt = f"{w}.tasks[{j}]"
            take(t, wt, ["label", "start", "end"], ["group", "status"])
            a, b = num(t["start"], f"{wt}.start"), num(t["end"], f"{wt}.end")
            if b < a:
                fail(f"{wt} ends before it starts")
            if t.get("status") not in (None, "ok", "violated"):
                fail(f'{wt}.status must be "ok" or "violated", got {t["status"]!r}')
            g = group_of(t, wt)
            if g is not None and g not in order:
                if "groups" in v:
                    fail(f'{wt}: group {g!r} is not listed in "groups"')
                add_group(order, g, where)
            tasks.append((str(t["label"]), a, b, g, t.get("status") == "violated"))
        parsed.append((str(r["label"]), tasks))
    if not parsed:
        fail(f"{where}.rows is empty")
    if len(order) > len(SERIES):
        fail(f"{where} has {len(order)} groups; at most {len(SERIES)} can be "
             "told apart by color. Drop the groups or use a table.")
    slot = group_slots(order)
    unit = _unit(v)
    rows, table = [], []
    for label, tasks in parsed:
        segs = []
        for name, a, b, g, bad in tasks:
            k = slot.get(g, 0 if order else 1)   # ungrouped among groups: muted
            t = f"{name} · {fmt(a)}–{fmt(b)}{unit}"
            if g is not None:
                t += f" · {g}"
            segs.append({"a": a, "b": b, "cls": f"vq-s{k}" if k else "vq-mute",
                         "ink": f"vq-o{k}" if k else "vq-t",
                         "text": name, "tip": t, "rl": 3.0, "rr": 3.0, "flag": bad})
            table.append([label, name, a, b, "" if g is None else g,
                          "violated" if bad else ""])
        rows.append({"label": label, "segs": segs})
    entries = [("rect", f"vq-s{slot[g]}", str(g), None) for g in order] \
        if len(order) > 1 else []
    body, height = draw_lanes(rows, v.get("unit", ""), entries, zero=False)
    return Figure("schedule", v.get("title", ""), v.get("caption", ""), height, body,
                  [("Tasks", ["row", "task", col_name("start", v.get("unit", "")),
                              col_name("end", v.get("unit", "")), "group", "state"], table)],
                  sum(1 for r in rows for seg in r["segs"] if seg["flag"]))


def view_bars(v: dict, where: str) -> Figure:
    take(v, where, ["type", "items"],
         ["title", "caption", "unit", "highlight_label", "other_label"])
    items = items_of(v["items"], f"{where}.items")
    if not items:
        fail(f"{where}.items is empty")
    if len(items) > MAX_LANES:
        fail(f"{where} has {len(items)} rows; the limit is {MAX_LANES}")
    for k, it in enumerate(items):
        take(it, f"{where}.items[{k}]", ["label", "value"], ["highlight"])
        num(it["value"], f"{where}.items[{k}].value")
        flag(it, "highlight", f"{where}.items[{k}]", False)
    emphasis = any(it.get("highlight") for it in items)
    unit = _unit(v)
    rows = []
    for it in items:
        on = bool(it.get("highlight")) or not emphasis
        val = it["value"]
        rows.append({"label": str(it["label"]), "end": fmt(val) + unit, "segs": [{
            "a": 0.0, "b": val, "cls": "vq-s1" if on else "vq-mute",
            "ink": "vq-o1", "tip": f"{it['label']} · {fmt(val)}{unit}",
            "rr": 4.0 if val >= 0 else 0.0, "rl": 4.0 if val < 0 else 0.0}]})
    entries = []
    if emphasis:
        entries = [("rect", "vq-s1", str(v.get("highlight_label", "selected")), None),
                   ("rect", "vq-mute", str(v.get("other_label", "not selected")), None)]
    body, height = draw_lanes(rows, v.get("unit", ""), entries)
    table = [[it["label"], it["value"]]
             + ([("yes" if it.get("highlight") else "")] if emphasis else [])
             for it in items]
    cols = ["item", col_name("value", v.get("unit", ""))] + (
        [str(v.get("highlight_label", "selected"))] if emphasis else [])
    return Figure("bars", v.get("title", ""), v.get("caption", ""), height, body,
                  [("Values", cols, table)])


# --------------------------------------------------------------------- grid

def draw_grid(cells, rows=None, cols=None, texts=None, violations=(),
              on_label="1", off_label="0", tip_of=None, cell_max=36,
              kind=None, poles=("+1", "−1"), gradient=None) -> tuple:
    """Cell matrix. A None cell is left blank. Returns (svg, height).

    Unless kind is given, values in {0, 1} draw as on/off (binary), values in
    {-1, +1} as two poles (spin, labeled ``poles``), and anything else on a
    one-hue scale, with two poles around zero when the sign varies. A spin
    cell given a value between the poles is drawn lighter, the closer to zero
    the lighter. ``gradient`` (low label, high label) replaces the two spin
    swatches with one strip from the second pole's color to the first's.
    """
    nr = len(cells)
    nc = len(cells[0]) if nr else 0
    if nr == 0 or nc == 0:
        fail("grid has no cells")
    if nr * nc > MAX_CELLS or nr > MAX_GRID_ROWS or nc > MAX_GRID_COLS:
        fail(f"grid has {nr}x{nc} = {nr * nc} cells; the limit is {MAX_CELLS} "
             f"cells, {MAX_GRID_ROWS} rows and {MAX_GRID_COLS} columns. "
             "Downsample, transpose, or aggregate it.")
    flat = [c for row in cells for c in row if c is not None]
    if not flat:
        fail("grid has no values: every cell is null")
    values = set(flat)
    if kind is None:
        if values <= {0, 1}:
            kind = "binary"
        elif values <= {-1, 1}:
            kind = "spin"
        else:
            kind = "diverging" if min(flat) < 0 < max(flat) else "sequential"
    vmin, vmax = min(flat), max(flat)
    peak = max(abs(vmin), abs(vmax)) or 1.0

    # Labels and text too small to draw stay available in the tooltips.
    row_w = min(140.0, max(tw(r) for r in rows)) + 8 if rows else 0.0
    cell = min(cell_max, int((W - 2 * PAD - row_w) // nc))
    row_labels = rows
    if rows and cell < 12:
        row_w, row_labels = 0.0, None
        cell = min(cell_max, int((W - 2 * PAD) // nc))
    gap = 2 if cell >= 12 else (1 if cell >= 5 else 0)

    if kind == "binary":
        entries = [("rect", "vq-s1", on_label, None), ("rect", "vq-off", off_label, None)]
    elif kind == "spin" and gradient is None:
        entries = [("rect", "vq-s1", poles[0], None), ("rect", "vq-s2", poles[1], None)]
    else:
        entries = []
    leg, top = legend(entries)
    if kind == "spin" and gradient is not None:
        x = float(PAD)
        leg = text(x, 20, gradient[0], "vq-t2")
        x += tw(gradient[0]) + 8
        for s in (-1, -2 / 3, -1 / 3, 0, 1 / 3, 2 / 3, 1):
            cls = "vq-s1" if s > 0 else "vq-s2"
            op = 0.15 + 0.85 * abs(s)
            leg += (f'<rect x="{x:.1f}" y="10" width="14" height="12" class="vq-off"/>'
                    f'<rect x="{x:.1f}" y="10" width="14" height="12" class="{cls}" '
                    f'fill-opacity="{op:.2f}"/>')
            x += 14
        leg += text(x + 8, 20, gradient[1], "vq-t2")
        top = 30.0
    elif not entries:      # value scale
        x = float(PAD)
        lo_label = fmt(-peak if kind == "diverging" else vmin)
        hi_label = fmt(peak if kind == "diverging" else vmax)
        leg = text(x, 20, lo_label, "vq-t2")
        x += tw(lo_label) + 8
        steps = [-1, -0.5, 0, 0.5, 1] if kind == "diverging" else [0, 0.25, 0.5, 0.75, 1]
        for s in steps:
            cls = "vq-neg" if s < 0 else "vq-s1"
            op = abs(s) if kind == "diverging" else 0.1 + 0.9 * s
            leg += (f'<rect x="{x:.1f}" y="10" width="18" height="12" class="vq-off"/>'
                    f'<rect x="{x:.1f}" y="10" width="18" height="12" class="{cls}" '
                    f'fill-opacity="{op:.2f}"/>')
            x += 18
        leg += text(x + 8, 20, hi_label, "vq-t2")
        top = 30.0

    col_h = 0.0
    col_mode = None
    if cols:
        widest = max(tw(c, 11) for c in cols)
        if widest <= cell - 2:
            col_mode, col_h = "flat", 16.0
        elif cell >= 12:
            col_mode, col_h = "turned", min(120.0, widest) + 6
    x0 = PAD + row_w
    y0 = top + 8 + col_h
    out = [leg]
    if col_mode:
        for c, name in enumerate(cols):
            cx = x0 + c * cell + cell / 2
            if col_mode == "flat":
                out.append(text(cx, y0 - 5, name, "vq-t2", 11, "middle"))
            else:
                out.append(text(cx + 4, y0 - 5, clip(name, 120, 11), "vq-t2", 11,
                                extra=f' transform="rotate(-90 {cx + 4:.1f} {y0 - 5:.1f})"'))
    bad = {(r, c) for r, c in violations}
    for r, row in enumerate(cells):
        y = y0 + r * cell
        if row_labels:
            out.append(text(x0 - 8, y + cell / 2 + 4, clip(rows[r], row_w - 8),
                            "vq-t2", 11, "end"))
        for c, val in enumerate(row):
            x = x0 + c * cell
            size = cell - gap
            geom = (f'x="{x + gap / 2:.1f}" y="{y + gap / 2:.1f}" width="{size}" '
                    f'height="{size}"' + (' rx="2"' if cell >= 12 else ""))
            if val is None:
                if (r, c) in bad:
                    out.append(f'<rect {geom} class="vq-ev"/>')
                continue
            strong = False
            if kind == "binary":
                cls, op, strong = ("vq-s1", None, True) if val else ("vq-off", None, False)
            elif kind == "spin":
                cls = "vq-s1" if val > 0 else "vq-s2"
                op = None if abs(val) >= 1 else 0.15 + 0.85 * abs(val)
                strong = op is None or op > 0.55
            elif kind == "diverging":
                cls, op = ("vq-s1" if val > 0 else "vq-neg"), abs(val) / peak
                strong = op > 0.55
            else:
                t = (val - vmin) / (vmax - vmin) if vmax > vmin else 1.0
                cls, op, strong = "vq-s1", 0.1 + 0.9 * t, t > 0.5
            label = tip_of(r, c, val) if tip_of else (
                f"{rows[r] if rows else f'row {r}'} · "
                f"{cols[c] if cols else f'column {c}'} · {fmt(val)}")
            shown = texts is not None and texts[r][c] not in (None, "")
            if shown:
                label += f" · {texts[r][c]}"
            mark = " · violated" if (r, c) in bad else ""
            fill = f' fill-opacity="{op:.2f}"' if op is not None else ""
            under = f'<rect {geom} class="vq-off"/>' if op is not None else ""
            out.append(f'{under}<rect {geom} class="vq-m {cls}"{fill}>{tip(label, mark)}</rect>')
            if (r, c) in bad:
                out.append(f'<rect {geom} class="vq-ev"/>')
            if shown and cell >= 16:
                ink = {"vq-neg": "vq-o8", "vq-s2": "vq-o2"}.get(cls, "vq-o1") \
                    if strong else "vq-t"
                out.append(text(x + cell / 2, y + cell / 2 + 4, texts[r][c], ink, 11,
                                "middle", 600, ' pointer-events="none"'))
    return "".join(out), y0 + nr * cell + PAD


def view_grid(v: dict, where: str) -> Figure:
    take(v, where, ["type", "cells"],
         ["title", "caption", "rows", "cols", "text", "violations", "on_label",
          "off_label"])
    cells = items_of(v["cells"], f"{where}.cells")
    if not cells or not all(isinstance(r, list) and len(r) == len(cells[0]) and r
                            for r in cells):
        fail(f"{where}.cells must be a non-empty rectangular list of rows")
    for r, row in enumerate(cells):
        for c, val in enumerate(row):
            if val is not None:
                num(val, f"{where}.cells[{r}][{c}]")
    nr, nc = len(cells), len(cells[0])
    rows, cols, texts = v.get("rows"), v.get("cols"), v.get("text")
    if rows is not None and len(scalars(rows, f"{where}.rows")) != nr:
        fail(f"{where}.rows has {len(rows)} labels for {nr} rows")
    if cols is not None and len(scalars(cols, f"{where}.cols")) != nc:
        fail(f"{where}.cols has {len(cols)} labels for {nc} columns")
    if texts is not None and (
            len(items_of(texts, f"{where}.text")) != nr
            or any(not isinstance(t, list) or len(t) != nc for t in texts)):
        fail(f"{where}.text must have the same shape as cells ({nr}x{nc})")
    for r, t in enumerate(texts or []):
        scalars(t, f"{where}.text[{r}]", none=True)
    violations = items_of(v.get("violations", []), f"{where}.violations")
    for k, rc in enumerate(violations):
        if not (isinstance(rc, list) and len(rc) == 2
                and all(isinstance(i, int) and not isinstance(i, bool) for i in rc)
                and 0 <= rc[0] < nr and 0 <= rc[1] < nc):
            fail(f"{where}.violations[{k}] must be [row, column] integers "
                 f"inside the grid, got {rc!r}")
    rows = [str(r) for r in rows] if rows else None
    cols = [str(c) for c in cols] if cols else None
    body, height = draw_grid(cells, rows, cols, texts, violations,
                             str(v.get("on_label", "1")), str(v.get("off_label", "0")))
    table = [[(rows[r] if rows else r)] + ["" if x is None else x for x in row]
             for r, row in enumerate(cells)]
    header = [""] + (cols if cols else [str(c) for c in range(nc)])
    return Figure("grid", v.get("title", ""), v.get("caption", ""), height, body,
                  [("Cells", header, table)],
                  len({tuple(rc) for rc in violations}))


# -------------------------------------------------------------- HTML tables

def html_table(columns, rows, status=None, limit=None) -> str:
    shown = rows if limit is None else rows[:limit]
    head = "".join(f"<th>{esc(c)}</th>" for c in columns)
    if status is not None:
        head = "<th></th>" + head
    body = []
    for k, row in enumerate(shown):
        cells = "".join(
            f'<td class="num">{esc(fmt(c))}</td>'
            if isinstance(c, (int, float)) and not isinstance(c, bool)
            else f"<td>{esc(c)}</td>" for c in row)
        mark = ""
        if status is not None:
            s = status[k]
            mark = ("<td></td>" if s is None else
                    '<td><span class="ok" title="satisfied">✓</span></td>' if s else
                    '<td><span class="bad" title="violated">✗</span></td>')
        bad = status is not None and status[k] is False
        attr = ' class="violated"' if bad else ""
        body.append(f"<tr{attr}>{mark}{cells}</tr>")
    note = ""
    if limit is not None and len(rows) > limit:
        note = f'<p class="note">First {limit} of {fmt(len(rows))} rows.</p>'
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>{note}')


def view_table(v: dict, where: str) -> tuple:
    """Returns (the card's inner HTML, the number of rows marked violated)."""
    take(v, where, ["type", "columns", "rows"], ["title", "caption", "status"])
    columns = scalars(v["columns"], f"{where}.columns")
    if len(columns) > MAX_TABLE_COLUMNS:
        fail(f"{where} has {len(columns)} columns; the limit is {MAX_TABLE_COLUMNS}")
    rows = items_of(v["rows"], f"{where}.rows")
    if len(rows) > MAX_TABLE_ROWS:
        fail(f"{where} has {len(rows)} rows; the limit is {MAX_TABLE_ROWS}")
    for k, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            fail(f"{where}.rows[{k}] must be a list of {len(columns)} values")
        scalars(row, f"{where}.rows[{k}]", none=True)
    rows = [["" if c is None else c for c in row] for row in rows]
    status = v.get("status")
    if status is not None:
        if (not isinstance(status, list) or len(status) != len(rows)
                or any(s is not None and not isinstance(s, bool) for s in status)):
            fail(f"{where}.status must hold one true/false/null per row")
    return html_table(columns, rows, status), sum(1 for s in status or [] if s is False)


VIEWS = {"graph": view_graph, "route": view_route, "bins": view_bins,
         "schedule": view_schedule, "bars": view_bars, "grid": view_grid}


# ------------------------------------------------------- result and problem

def distribution_figure(dist: dict, kind: str, fig_w: float = W):
    """Sample counts per objective value, best value in the accent color,
    drawn ``fig_w`` user units wide."""
    where = "result.objective_distribution"
    take(dist, where, ["counts"], ["values", "bin_edges", "above"])
    counts = [count(c, f"{where}.counts") for c in items_of(dist["counts"], f"{where}.counts")]
    binned = "bin_edges" in dist
    marks = [num(x, f"{where}.{'bin_edges' if binned else 'values'}")
             for x in items_of(dist.get("bin_edges" if binned else "values"),
                               f"{where}.{'bin_edges' if binned else 'values'}")]
    above = count(dist.get("above", 0), f"{where}.above")
    if binned:
        edges = marks
        names = [f"{fmt(a)} to {fmt(b)}" for a, b in zip(edges, edges[1:])]
    else:
        names = [fmt(x) for x in marks]
    J = ENERGY_UNIT
    if len(names) != len(counts) or len(counts) > 200 or sum(counts) == 0:
        fail(f"{where}: counts must be non-empty, at most 200 long, and match "
             "the values or bins")
    if len(counts) < 2:
        return None
    left, right, top, bottom = 56.0, fig_w - PAD, 22.0, 200.0
    tk = ticks(0, max(counts), integer=True, target=4)
    ymax = tk[-1]
    band = (right - left) / len(counts)
    width = max(1.0, min(24.0, band - 2))
    best = next(k for k, c in enumerate(counts) if c)
    out = []
    for t in tk:
        y = bottom - t / ymax * (bottom - top)
        out.append(f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" '
                   f'class="{"vq-axis" if t == 0 else "vq-grid"}"/>')
        out.append(text(left - 8, y + 4, fmt(t), "vq-tm", 11, "end"))
    for k, c in enumerate(counts):
        if not c:
            continue
        cx = left + (k + 0.5) * band
        h = max(1.5, c / ymax * (bottom - top))
        d = column_path(cx - width / 2, cx + width / 2, bottom - h, bottom)
        cls = "vq-s1" if k == best else "vq-mute"
        out.append(f'<path d="{d}" class="vq-m {cls}">'
                   f'{tip(f"{names[k]} {J} · {fmt(c)} samples")}</path>')
        if k == best:
            out.append(text(cx, bottom - h - 6, fmt(c), "vq-t", 11,
                            "middle" if cx > left + 20 else "start", 600))
    if binned:
        lo, hi = edges[0], edges[-1]
        for t in ticks(lo, hi, target=5):
            if lo <= t <= hi:
                x = left + (t - lo) / (hi - lo) * (right - left)
                out.append(text(x, bottom + 16, fmt(t), "vq-tm", 11, "middle"))
    else:
        widest = max(tw(s, 11) for s in names) + 10
        every = max(1, math.ceil(widest / band))
        for k in range(0, len(names), every):
            out.append(text(left + (k + 0.5) * band, bottom + 16, names[k], "vq-tm",
                            11, "middle"))
    axis = "Ising energy" if kind == "ising" else "QUBO objective"
    rows = [[n, c] for n, c in zip(names, counts)]
    caption = "The accent column is the best value reached."
    if binned:
        caption = "The accent column is the bin that holds the best value."
        if above:
            tail = f"{fmt(above)} samples above {with_unit(edges[-1], J)} are not drawn"
            out.append(text(right, top - 8, tail, "vq-t2", 11, "end"))
            rows.append([f"above {fmt(edges[-1])}", above])
            caption += f" {tail}."
    out.append(text((left + right) / 2, bottom + 36, f"{axis} in {J} (lower is better)",
                    "vq-tm", 11, "middle"))
    out.append(text(14, (top + bottom) / 2, "samples", "vq-tm", 11, "middle",
                    extra=f' transform="rotate(-90 14 {(top + bottom) / 2:.1f})"'))
    return Figure("samples", "Samples by objective value", caption, bottom + 48,
                  "".join(out), [("Samples", [col_name(axis, J), "samples"], rows)],
                  width=fig_w)


def model_figures(result: dict, model: Model) -> tuple:
    """The coupling graph in the best sample's state. Returns (figures, notes,
    stats)."""
    import numpy as np

    kind, n, m = model.kind, model.n, model.num_interactions
    sample = result["best_sample"]
    coefs = np.abs(np.r_[model.biases[model.biases != 0], model.values[model.values != 0]])
    stats = [("Variables", fmt(n)), ("Couplings", fmt(m)),
             ("Linear terms", fmt(int(np.count_nonzero(model.biases))))]
    if n > 1:
        stats.append(("Coupling density", f"{m / (n * (n - 1) / 2):.1%}"))
    if coefs.size:
        stats.append(("Coefficient range", f"{fmt(float(coefs.min()))} to "
                      f"{with_unit(float(coefs.max()), ENERGY_UNIT)}"))
    sample = np.asarray(sample)
    if n > 300 or m > 3000:
        return [coarse_model_figure(model, sample)], [], stats
    one, zero = value_names(kind)
    groups = [one if x > 0 else zero for x in sample]
    edges = [(int(i), int(j), float(c), "highlight" if h else "plain")
             for i, j, c, h in zip(model.rows, model.cols, model.values, raising(model, sample))]
    caption = ("Each node is a variable colored by its value in the best sample, and "
               "each edge a coupling. A highlighted coupling raises the objective in "
               "this state.")
    fig = network_figure("model", "Model in its best state", caption,
                         model.labels or [str(i) for i in range(n)], groups, [one, zero],
                         edges, COUPLING_NAMES,
                         edge_legend=True, soft=True, unit=ENERGY_UNIT)
    return [fig], [], stats


COUPLING_NAMES = {"highlight": "coupling raising the objective",
                  "plain": "coupling lowering the objective or inactive",
                  "violated": "", "ungrouped": ""}


def value_names(kind: str) -> tuple:
    """The two values a variable takes, set first."""
    return ("+1", "−1") if kind == "ising" else ("1", "0")


def variable_names(kind: str, labels) -> tuple:
    """How the report names variable k and the block a..b, and the order the
    variables are drawn in. ``labels`` are a model file's, or None."""
    s = "s" if kind == "ising" else "x"
    if labels is None:
        return (lambda k: f"{s}[{k}]", lambda a, b: f"{s}[{a}..{b}]", "in index order")
    if all(INTEGER_LABEL.fullmatch(x) for x in labels):
        return (lambda k: f"{s}[{labels[k]}]", lambda a, b: f"{s}[{labels[a]}..{labels[b]}]",
                "in label order")
    return (lambda k: labels[k], lambda a, b: f"{labels[a]}..{labels[b]}",
            "in the model file's order")


def raising(model: Model, sample) -> "np.ndarray":
    """Which couplings raise the objective in ``sample``: the coupling and
    the product of its two values have the same sign."""
    c, rows, cols = model.values, model.rows, model.cols
    if model.kind == "ising":
        return c * sample[rows] * sample[cols] > 0
    return (c > 0) & (sample[rows] > 0) & (sample[cols] > 0)


def blocks(n: int, at_most: int) -> tuple:
    """Split 0..n-1 into at most ``at_most`` consecutive blocks of one size,
    the last one shorter. Returns (block size, block count)."""
    size = -(-n // min(n, at_most))
    return size, -(-n // size)


def coarse_model_figure(model: Model, sample) -> Figure:
    """The coupling graph with consecutive index blocks as nodes. A block is
    colored by the share of its variables set in the best sample, and an edge
    joins two blocks that share a coupling. Couplings inside one block are
    counted in its hover text."""
    import numpy as np

    n, rows, cols = model.n, model.rows, model.cols
    on = sample > 0
    hot = raising(model, sample)
    for at_most in COARSE_BLOCKS:
        size, nb = blocks(n, at_most)
        bi, bj = rows // size, cols // size
        inner = bi == bj
        pairs = np.unique(bi[~inner] * nb + bj[~inner])
        if len(pairs) <= 6 * nb:
            break
    sizes = np.minimum(size, n - np.arange(nb) * size)
    ones = np.bincount(np.arange(n) // size, weights=on, minlength=nb)
    within = np.bincount(bi[inner], minlength=nb)
    within_hot = np.bincount(bi[inner], weights=hot[inner], minlength=nb)
    inverse = np.searchsorted(pairs, bi[~inner] * nb + bj[~inner])
    between = np.bincount(inverse, minlength=len(pairs))
    between_hot = np.bincount(inverse, weights=hot[~inner], minlength=len(pairs))

    one, zero = value_names(model.kind)
    _, span, _ = variable_names(model.kind, model.labels)
    share = ones / sizes
    # Mixed blocks stay neutral so that each value keeps the color it has everywhere.
    groups = [f"mostly {one}" if f >= 2 / 3 else f"mostly {zero}" if f <= 1 / 3 else None
              for f in share]
    labels = [f"block {k}" for k in range(nb)]
    node_tips = [f"block {k}: {span(k * size, k * size + int(sizes[k]) - 1)} · "
                 f"{fmt(int(ones[k]))} of {fmt(int(sizes[k]))} are {one} · "
                 f"{fmt(int(within[k]))} couplings inside, {fmt(int(within_hot[k]))} "
                 "raising the objective" for k in range(nb)]
    edges, edge_tips = [], []
    for key, c, h in zip(pairs.tolist(), between.tolist(), between_hot.tolist()):
        u, v = divmod(key, nb)
        edges.append((u, v, None, "highlight" if h else "plain"))
        edge_tips.append(f"block {u} – block {v} · {fmt(c)} couplings, {fmt(int(h))} "
                         "raising the objective")
    names = {"highlight": "blocks sharing a coupling that raises the objective",
             "plain": "blocks sharing only couplings that lower it or are inactive",
             "violated": "", "ungrouped": "mixed"}
    caption = (f"Each node is a block of {fmt(size)} consecutive variables, colored by the "
               f"share set to {one} in the best sample. An edge joins two blocks with a "
               f"coupling between them; couplings inside a block are counted in its "
               "hover text.")
    return network_figure("model", "Model in its best state", caption, labels, groups,
                          [f"mostly {one}", f"mostly {zero}"], edges, names,
                          node_tips=node_tips, edge_tips=edge_tips, edge_legend=True, soft=True,
                          simplified=f"the coupling graph groups the variables into "
                                     f"{fmt(nb)} blocks of {fmt(size)}")


def strip_figure(result: dict):
    sample = result["best_sample"]
    n = len(sample)
    if n > MAX_STRIP:
        return coarse_strip_figure(result)
    ncol = n if n <= 48 else (64 if n <= 1024 else 128)
    # A QUBO 0 takes the −1 pole, so both values draw at full strength.
    poles = sample if result["type"] == "ising" else [2 * x - 1 for x in sample]
    rows = [poles[k:k + ncol] + [None] * (ncol - len(poles[k:k + ncol]))
            for k in range(0, n, ncol)]
    name, _, order = variable_names(result["type"], result.get("labels"))
    body, height = draw_grid(rows, tip_of=lambda r, c, val: f"{name(r * ncol + c)} = "
                                                            f"{sample[r * ncol + c]}",
                             cell_max=14, kind="spin", poles=value_names(result["type"]))
    return Figure("state", "Best sample",
                  f"All {fmt(n)} variables {order}, left to right, top to bottom.",
                  height, body)


def coarse_strip_figure(result: dict) -> Figure:
    """The best sample as the share of set variables per consecutive block."""
    import numpy as np

    sample = np.asarray(result["best_sample"])
    n = len(sample)
    size, nb = blocks(n, MAX_STRIP)
    sizes = np.minimum(size, n - np.arange(nb) * size)
    ones = np.bincount(np.arange(n) // size, weights=sample > 0, minlength=nb)
    share = ones / sizes
    # The scale spans the shares present: the lowest takes the solid unset color,
    # the highest the solid set color, so near-equal blocks still tell apart. One
    # share alone keeps the absolute scale.
    lo, hi = float(share.min()), float(share.max())
    t = (share - lo) / (hi - lo) if hi > lo else share
    mean = (2 * t - 1).tolist()
    ncol = 128
    rows = [mean[k:k + ncol] + [None] * (ncol - len(mean[k:k + ncol]))
            for k in range(0, nb, ncol)]
    _, span, order = variable_names(result["type"], result.get("labels"))
    one, zero = value_names(result["type"])
    low, high = percents(lo, hi)

    def tip_of(r, c, val):
        k = r * ncol + c
        return (f"{span(k * size, k * size + int(sizes[k]) - 1)}: "
                f"{fmt(int(ones[k]))} of {fmt(int(sizes[k]))} are {one}")

    if len(np.unique(share)) > 2:
        legend = {"gradient": (low, f"{high} set to {one}")}
        how = (f"A cell is colored by the share of its block set to {one}, on a scale "
               f"from {low} ({zero} color) to {high} ({one} color) that is lighter in "
               "between.")
    elif {lo, hi} <= {0.0, 1.0}:
        legend = {"poles": (one, zero)}
        how = "Every block holds a single value."
    elif lo == hi:
        legend = {"poles": (one, zero)}
        how = f"Every block is {low} set to {one}."
    else:
        legend = {"poles": (f"{high} set to {one}", f"{low} set to {one}")}
        how = f"Every block is {low} or {high} set to {one}."
    body, height = draw_grid(rows, tip_of=tip_of, cell_max=14, kind="spin", **legend)
    return Figure("state", "Best sample",
                  f"{fmt(n)} variables in {fmt(nb)} blocks of {fmt(size)}, {order}, "
                  f"left to right, top to bottom. {how}", height, body,
                  simplified=f"the best-sample strip shows the share set to {one} per "
                             f"block of {fmt(size)} variables")


def percents(lo: float, hi: float) -> tuple:
    """Two shares as percentages with the fewest decimals that tell them apart."""
    for digits in range(4):
        a, b = f"{lo:.{digits}%}", f"{hi:.{digits}%}"
        if a != b or lo == hi:
            return a, b
    return a, b


# --------------------------------------------------------------------- page

PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font:15px/1.5 FONT}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0}
h1{font-size:28px;line-height:1.2;margin:4px 0 6px;font-weight:650}
.subtitle{color:var(--ink2);margin:0 0 14px;max-width:70ch}
h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
font-weight:600;margin:36px 0 12px}
h3{font-size:16px;margin:0 0 4px;font-weight:600}
.pill{display:inline-flex;align-items:center;gap:8px;padding:5px 12px;border-radius:999px;
font-size:13px;font-weight:600;border:1px solid var(--line);color:var(--ink2)}
.pill.ok{color:var(--good);background:var(--good-bg);border-color:transparent}
.pill.bad{color:var(--bad);background:var(--bad-bg);border-color:transparent}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0 14px}
.tile,.card{background:var(--surface);border:1px solid var(--line);border-radius:12px}
.tile{padding:14px 16px}
.tile .label{font-size:12px;color:var(--ink2)}
.tile .value{font-size:24px;font-weight:650;line-height:1.25;overflow-wrap:break-word}
.tile .note{font-size:12px;color:var(--muted)}
.card{padding:18px 18px 14px;margin-bottom:14px}
.caption,.note{color:var(--ink2);font-size:13px;margin:0 0 6px;max-width:80ch}
.vq-fig{display:block;width:100%;min-width:600px;height:auto;margin-top:6px}
ul.checks{list-style:none;margin:0;padding:0}
ul.checks li{display:flex;gap:10px;padding:7px 0;border-top:1px solid var(--line)}
ul.checks li:first-child{border-top:0}
.ok,.bad{font-weight:700}.ok{color:var(--good)}.bad{color:var(--bad)}
.detail{color:var(--ink2)}
details{margin-top:8px;font-size:13px}
summary{cursor:pointer;color:var(--ink2)}
details dl{margin-top:6px}
.fig-head{display:flex;align-items:flex-start;gap:12px}
details.vq-share{position:relative;margin:0 0 0 auto;flex:none}
.vq-share summary{list-style:none;padding:2px 10px;border:1px solid var(--line);border-radius:6px}
.vq-share summary::-webkit-details-marker{display:none}
.vq-share summary:hover,.vq-share[open] summary{color:var(--ink)}
.vq-menu{position:absolute;right:0;top:calc(100% + 4px);z-index:5;min-width:210px;padding:4px;
background:var(--surface);border:1px solid var(--line);border-radius:8px;
box-shadow:0 4px 16px rgba(0,0,0,.15)}
.vq-menu button{display:block;width:100%;text-align:left;font:inherit;color:var(--ink);
background:none;border:0;border-radius:4px;padding:6px 10px;cursor:pointer}
.vq-menu button:hover,.vq-menu button:focus-visible{background:var(--plane)}
.vq-share-msg{color:var(--bad);margin:4px 10px}
.run{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:14px;
margin-bottom:14px}
.run .card{margin:0}
.run h3{margin-bottom:12px}
.run dl{row-gap:8px}
dd .sub{display:block;color:var(--muted);font-size:12px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 18px;
padding-bottom:14px;margin-bottom:14px;border-bottom:1px solid var(--line)}
.stat .label{font-size:12px;color:var(--ink2)}
.stat .value{font-size:20px;font-weight:650;line-height:1.3;font-variant-numeric:tabular-nums}
.stat .note{font-size:12px;color:var(--muted)}
details.meta{margin-top:0;padding:12px 18px}
details.meta[open] summary{margin-bottom:10px}
.scroll{overflow-x:auto}
.pair{display:grid;grid-template-columns:minmax(0,max-content) minmax(0,1fr);gap:28px;
align-items:start}
.pair .vq-fig{min-width:400px}
@media (max-width:760px){.pair{grid-template-columns:minmax(0,1fr)}}
.top td:nth-child(3){font:12px ui-monospace,SFMono-Regular,Menlo,monospace;
word-break:break-all;max-width:33ch}
table{border-collapse:collapse;font-size:13px;margin:8px 0;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 12px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink2);font-weight:600}
td.num{text-align:right}
tr.violated td{color:var(--bad)}
dl{display:grid;grid-template-columns:minmax(0,max-content) minmax(0,1fr);gap:4px 18px;margin:0;font-size:13px}
dt{color:var(--ink2)}dd{margin:0;overflow-wrap:anywhere}
dd a{color:var(--ink)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
#vq-tip{position:fixed;z-index:9;pointer-events:none;background:var(--ink);color:var(--surface);
font-size:12px;padding:5px 9px;border-radius:6px;max-width:320px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
footer+footer{margin-top:6px}
"""

PAGE_CSS = PAGE_CSS.replace("FONT", FONT)

APP_CSS = """
main{padding:12px 16px 20px}
"""

# Figure marks carry their hover text as SVG <title>. The page shows it in a
# styled tooltip instead; the text is assigned with textContent, never as HTML.
TIPS_JS = """
function vqTips(root){
  root.querySelectorAll('svg.vq-fig title').forEach(function(t){
    t.parentNode.setAttribute('data-tip',t.textContent);t.parentNode.removeChild(t);});
}
(function(){
  var tip=document.getElementById('vq-tip');
  document.addEventListener('pointermove',function(e){
    var el=e.target.closest?e.target.closest('[data-tip]'):null;
    if(!el){tip.hidden=true;return;}
    tip.textContent=el.getAttribute('data-tip');tip.hidden=false;
    var w=tip.offsetWidth,x=e.clientX+14,y=e.clientY+16;
    if(x+w>window.innerWidth-8)x=e.clientX-w-14;
    tip.style.left=Math.max(8,x)+'px';tip.style.top=y+'px';});
  document.addEventListener('pointerleave',function(){tip.hidden=true;});
})();
"""

# The PNG is drawn from a copy of the SVG that carries the figure CSS and the
# current theme's values of the tokens it uses, so it looks as on the page.
# `save` takes {name, mime, text} or {name, mime, blob} with blob in base64
# and returns a promise. vqDownload saves through a claude.ai artifact
# viewer's `downloads` capability when there is one, since that frame
# cannot download by itself; the publish must declare {downloads: true}.
SHARE_JS = """
function vqPng(svg){
  var vb=svg.viewBox.baseVal,cs=getComputedStyle(svg),copy=svg.cloneNode(true),vars='';
  (VQ_FIG_CSS.match(/--[\\w-]+/g)||[]).forEach(function(v){
    vars+=v+':'+cs.getPropertyValue(v)+';';});
  var style=document.createElementNS(svg.namespaceURI,'style');
  style.textContent='svg{'+vars+'}'+VQ_FIG_CSS;
  copy.insertBefore(style,copy.firstChild);
  copy.setAttribute('width',vb.width);copy.setAttribute('height',vb.height);
  return new Promise(function(ok,no){
    var img=new Image();
    img.onload=function(){
      var k=2,c=document.createElement('canvas'),g=c.getContext('2d');
      c.width=Math.ceil(vb.width*k);c.height=Math.ceil(vb.height*k);
      g.fillStyle=cs.getPropertyValue('--surface');g.fillRect(0,0,c.width,c.height);
      g.scale(k,k);g.drawImage(img,0,0,vb.width,vb.height);
      ok(c.toDataURL('image/png').split(',')[1]);};
    img.onerror=function(){no(new Error('The figure could not be drawn as an image.'));};
    img.setAttribute('src','data:image/svg+xml;charset=utf-8,'
      +encodeURIComponent(new XMLSerializer().serializeToString(copy)));
  });
}
function vqDownload(f){
  var data=new Blob([f.text!=null?f.text
    :Uint8Array.from(atob(f.blob),function(c){return c.charCodeAt(0);})],{type:f.mime});
  var viewer=window.claude&&window.claude.use?window.claude.use('downloads'):Promise.resolve(null);
  return viewer.then(function(d){
    if(d)return d.save({filename:f.name,data:data}).then(function(){},function(e){
      if(!e||e.code!=='declined')throw new Error((e&&e.message)||'The download failed.');});
    if(window.claude&&window.top!==window)throw new Error('This view does not allow downloads.');
    var a=document.createElement('a'),url=URL.createObjectURL(data);
    a.setAttribute('href',url);a.download=f.name;
    document.body.appendChild(a);a.click();a.remove();
    setTimeout(function(){URL.revokeObjectURL(url);},1000);
  });
}
function vqCsv(t){
  return t.r.map(function(row){return row.map(function(k){
    var v=t.s[k];return /[",\\r\\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;
  }).join(',')+'\\r\\n';}).join('');
}
function vqShare(save){
  document.addEventListener('click',function(e){
    document.querySelectorAll('details.vq-share[open]').forEach(function(d){
      if(!d.contains(e.target))d.open=false;});
    var b=e.target.closest?e.target.closest('[data-share]'):null;
    if(!b)return;
    var box=b.closest('.vq-share'),msg=box.querySelector('.vq-share-msg'),
      what=b.getAttribute('data-share'),name=b.getAttribute('data-file');
    msg.hidden=true;
    var file=what==='png'
      ?vqPng(box.closest('.vq-figure').querySelector('svg.vq-fig')).then(function(blob){
        return {name:name,mime:'image/png',blob:blob};})
      :Promise.resolve({name:name,mime:'text/csv',
        text:vqCsv(JSON.parse(box.getAttribute('data-csv'))[+what])});
    file.then(save).then(function(){box.open=false;},
      function(err){msg.textContent=err.message;msg.hidden=false;});
  });
}
"""

APP_PROTOCOL = "2026-01-26"

# structuredContent.html is markup built by `render` with every value
# escaped, so it is inserted as HTML.
APP_JS = """
(function(){
  var main=document.getElementById('vq-report'),root=document.documentElement,nextId=1,
    pending={},canDownload=false;
  function post(m){m.jsonrpc='2.0';window.parent.postMessage(m,'*');}
  function request(method,params){
    var id=++nextId;post({id:id,method:method,params:params});
    return new Promise(function(ok,no){pending[id]=[ok,no];});
  }
  // A sandboxed frame cannot save files itself; the host saves them.
  vqShare(function(f){
    if(!canDownload)return Promise.reject(new Error('This host does not allow downloads.'));
    var r={uri:'file:///'+encodeURIComponent(f.name),mimeType:f.mime};
    if(f.text!=null)r.text=f.text;else r.blob=f.blob;
    return request('ui/download-file',{contents:[{type:'resource',resource:r}]});
  });
  // The sandboxed frame cannot open pages itself; the host opens them.
  document.addEventListener('click',function(e){
    var a=e.target.closest?e.target.closest('a[href]'):null;
    if(!a)return;
    e.preventDefault();
    post({id:++nextId,method:'ui/open-link',params:{url:a.href}});
  });
  function note(text){var p=document.createElement('p');p.className='note';
    p.textContent=text;main.replaceChildren(p);}
  function context(c){if(c&&(c.theme==='light'||c.theme==='dark'))root.setAttribute('data-theme',c.theme);}
  function show(r){
    var data=r&&r.structuredContent;
    if(r&&!r.isError&&data&&typeof data.html==='string'){
      main.innerHTML=data.html;vqTips(main);
      if(typeof data.title==='string')document.title=data.title;
      return;
    }
    if(!r||!r.isError){note('The report was written to a file.');return;}
    var text=(r.content||[]).filter(function(c){return c.type==='text';})
      .map(function(c){return c.text;}).join('\\n');
    note(text||'The report could not be built.');
  }
  var last=[0,0],queued=false;
  function measure(){
    if(queued)return;queued=true;
    requestAnimationFrame(function(){
      queued=false;var h0=root.style.height;root.style.height='max-content';
      var h=Math.ceil(root.getBoundingClientRect().height);root.style.height=h0;
      var w=Math.ceil(window.innerWidth);
      if(w!==last[0]||h!==last[1]){last=[w,h];
        post({method:'ui/notifications/size-changed',params:{width:w,height:h}});}
    });
  }
  window.addEventListener('message',function(e){
    if(e.source!==window.parent)return;
    var m=e.data;if(!m||m.jsonrpc!=='2.0')return;
    if(m.id===1&&!m.method){
      if(m.error){note('The host did not start the report view: '+(m.error.message||''));return;}
      context(m.result&&m.result.hostContext);
      canDownload=!!(m.result&&m.result.hostCapabilities
        &&m.result.hostCapabilities.downloadFile);
      post({method:'ui/notifications/initialized'});
      var ro=new ResizeObserver(measure);ro.observe(root);ro.observe(document.body);measure();
    }else if(m.id!=null&&!m.method&&pending[m.id]){
      var p=pending[m.id];delete pending[m.id];
      if(m.error)p[1](new Error(m.error.message||'The host refused the request.'));
      else p[0](m.result);
    }else if(m.method==='ui/notifications/tool-result')show(m.params);
    else if(m.method==='ui/notifications/tool-cancelled')note('The report was cancelled.');
    else if(m.method==='ui/notifications/host-context-changed')context(m.params);
    else if(m.id!=null&&m.method)post(m.method==='ping'||m.method==='ui/resource-teardown'
      ?{id:m.id,result:{}}:{id:m.id,error:{code:-32601,message:'Method not found'}});
  });
  post({id:1,method:'ui/initialize',params:{appCapabilities:{},
    appInfo:{name:'VeloxQ report',version:'1'},protocolVersion:'PROTOCOL'}});
})();
""".replace("PROTOCOL", APP_PROTOCOL)


def share_js() -> str:
    return f"var VQ_FIG_CSS={json.dumps(figure_css())};{SHARE_JS}"


def figure_card(fig: Figure) -> str:
    return f'<section class="card">{figure_body(fig)}</section>'


def figure_body(fig: Figure) -> str:
    head = f"<h3>{esc(fig.title)}{'*' if fig.simplified else ''}</h3>" if fig.title else ""
    parts = [f'<div class="fig-head">{head}{share_menu(fig)}</div>']
    if fig.caption:
        parts.append(f'<p class="caption">{esc(fig.caption)}</p>')
    parts.append(f'<div class="scroll">{fig.svg()}</div>')
    return f'<div class="vq-figure">{"".join(parts)}</div>'


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pooled(columns, rows) -> dict:
    """A table as its distinct cell texts and rows of indices into them, so a
    label repeated on every row is stored once."""
    pool: dict[str, int] = {}
    return {"r": [[pool.setdefault(str(c), len(pool)) for c in row]
                  for row in [columns, *rows]], "s": list(pool)}


def share_menu(fig: Figure) -> str:
    """The figure's Share menu: its image as PNG and each of its tables as CSV.
    SHARE_JS does the downloads."""
    base = slug(fig.title) or fig.kind
    items = [("png", f"{base}.png", "Download image (PNG)")]
    for k, (heading, _, _) in enumerate(fig.tables):
        what = "data" if len(fig.tables) == 1 else heading.lower()
        items.append((str(k), f"{base}-{slug(heading)}.csv", f"Download {what} (CSV)"))
    buttons = "".join(f'<button type="button" data-share="{a}" data-file="{esc(f)}">'
                      f"{esc(label)}</button>" for a, f, label in items)
    data = esc(json.dumps([pooled(c, r) for _, c, r in fig.tables]))
    return (f'<details class="vq-share" data-csv="{data}"><summary>Share</summary>'
            f'<div class="vq-menu">{buttons}<p class="vq-share-msg" hidden></p></div>'
            "</details>")


def tiles_html(tiles) -> str:
    out = ['<div class="tiles">']
    for label, value, note in tiles:
        out.append(f'<div class="tile"><div class="label">{esc(label)}</div>'
                   f'<div class="value">{esc(value)}</div>'
                   + (f'<div class="note">{esc(note)}</div>' if note else "")
                   + "</div>")
    out.append("</div>")
    return "".join(out)


def render(view: dict | None, result: dict | None, model: Model | None) -> tuple[str, str]:
    """The report's title and body markup. ``result`` is the document
    `summarize` builds; ``model`` adds the coupling graph and needs it."""
    title = "VeloxQ solve"
    head, tiles, sections, footnotes = [], [], [], []
    if view is not None:
        check_shape(view)
        take(view, "view", ["title", "checks"], ["subtitle", "summary", "notes", "views"])
        for key, limit in (("notes", MAX_NOTES), ("summary", MAX_TILES), ("checks", MAX_CHECKS)):
            if len(items_of(view.get(key, []), f"view.{key}")) > limit:
                fail(f"view.{key} has {len(view[key])} {key}; the limit is {limit}")
        views = items_of(view.get("views", []), "view.views")
        if len(views) > MAX_VIEWS:
            fail(f"view.views has at most {MAX_VIEWS} figures")
        laid_out = sum(len(v["nodes"]) for v in views
                       if isinstance(v, dict) and v.get("type") == "graph"
                       and isinstance(v.get("nodes"), list))
        if laid_out > MAX_LAYOUT_NODES:
            fail(f"the figures lay out {laid_out} graph nodes; the limit is "
                 f"{MAX_LAYOUT_NODES} per report")
        title = str(view["title"])
        checks = items_of(view["checks"], "view.checks")
        for k, c in enumerate(checks):
            take(c, f"view.checks[{k}]", ["label", "ok"], ["detail"])
            if not isinstance(c["ok"], bool):
                fail(f"view.checks[{k}].ok must be true or false, got {c['ok']!r}")
        for k, t in enumerate(items_of(view.get("summary", []), "view.summary")):
            take(t, f"view.summary[{k}]", ["label", "value"], ["note", "unit"])
            tiles.append((t["label"], with_unit(t["value"], str(t.get("unit", ""))),
                          t.get("note")))
        failed = [c for c in checks if not c["ok"]]
        if failed:
            head.append(f'<span class="pill bad">✗ Infeasible: {len(failed)} of '
                        f"{len(checks)} constraint checks fail</span>")
        elif checks:
            head.append(f'<span class="pill ok">✓ Feasible: all {len(checks)} '
                        "constraint checks pass</span>")
        else:
            head.append('<span class="pill">No hard constraints in this problem</span>')
        for note in scalars(view.get("notes", []), "view.notes"):
            head.append(f'<p class="note">{esc(note)}</p>')

        cards, drawn, used = [], 0, 0     # drawn: marks the figures show as violated
        for k, v in enumerate(views):
            where = f"view.views[{k}]"
            if not isinstance(v, dict) or "type" not in v:
                fail(f'{where} must be an object with a "type"')
            if v["type"] == "table":
                inner, bad_rows = view_table(v, where)
                drawn += bad_rows
                cards.append('<section class="card">'
                             + (f"<h3>{esc(v['title'])}</h3>" if v.get("title") else "")
                             + (f'<p class="caption">{esc(v["caption"])}</p>'
                                if v.get("caption") else "")
                             + inner + "</section>")
            elif isinstance(v["type"], str) and v["type"] in VIEWS:
                fig = VIEWS[v["type"]](v, where)
                drawn += fig.violations
                cards.append(figure_card(fig))
            else:
                fail(f"{where}.type is {v['type']!r}; "
                     f"known types: {sorted(list(VIEWS) + ['table'])}. {SPEC_DOC}")
            used += len(cards[-1].encode("utf-8"))
            check_size(used, f"{where} makes the report")
        if drawn and not failed:
            fail(f"the figures draw {drawn} violations (violated edges, tasks, "
                 "cells or rows, or bins over capacity) but no entry of "
                 "view.checks has ok false. Add the failed constraint to checks: "
                 "the report must not call an infeasible solution feasible.")
        if cards:
            sections.append("<h2>Solution</h2>" + "".join(cards))
        if checks:
            rows = "".join(
                f'<li><span class="{"ok" if c["ok"] else "bad"}">'
                f'{"✓" if c["ok"] else "✗"}</span><span>{esc(c["label"])}'
                + (f' <span class="detail">{esc(c["detail"])}</span>'
                   if c.get("detail") else "") + "</span></li>" for c in checks)
            sections.append('<h2>Constraint checks</h2><section class="card">'
                            f'<ul class="checks">{rows}</ul></section>')

    if result is not None:
        kind = result["type"]
        axis = "Ising energy" if kind == "ising" else "QUBO objective"
        at_best, total = result["num_samples_at_best"], result["num_samples"]
        if not tiles:
            tiles = [(f"Best {axis}", with_unit(result["best_objective"], ENERGY_UNIT), None),
                     ("Samples at the best value", f"{fmt(at_best)} of {fmt(total)}", None),
                     ("Variables", fmt(result["num_variables"]), None)]
        quality = [f'<p class="caption">{fmt(at_best)} of {fmt(total)} samples reached '
                   f'the best {axis} of '
                   f'{esc(with_unit(result["best_objective"], ENERGY_UNIT))}.</p>']
        dist = result.get("objective_distribution")
        compact = result["num_variables"] <= 64
        # Narrower beside the state column, so its text keeps the page's size.
        fig = distribution_figure(dist, kind, PAIR_W if compact else W) if dist else None
        if fig is None and dist:
            quality.append('<p class="note">Every sample has the same objective '
                           "value, so there is no distribution to plot.</p>")
        top = result["top"]
        rows = [[k + 1, t["objective"]]
                + (["".join("+" if x > 0 else "−" for x in t["sample"]) if kind == "ising"
                    else "".join(str(x) for x in t["sample"])] if compact else [])
                for k, t in enumerate(top)]
        best = ('<div class="top"><h3>Best distinct samples</h3>'
                + html_table(["rank", col_name(axis, ENERGY_UNIT)]
                             + (["state"] if compact else []), rows, limit=TOP_ROWS)
                + "</div>")
        if fig is None:
            quality.append(f'<section class="card">{best}</section>')
        else:
            quality.append(f'<section class="card pair">{best}{figure_body(fig)}</section>')
        sections.append("<h2>Sample quality</h2>" + "".join(quality))

        parts, figs = [], []
        if model is not None:
            figs, notes, stats = model_figures(result, model)
            parts.append(tiles_html([(a, b, None) for a, b in stats]))
            parts += [figure_card(f) for f in figs]
            parts += [f'<p class="note">{esc(n)}</p>' for n in notes]
        figs.append(strip_figure(result))
        parts.append(figure_card(figs[-1]))
        sections.append("<h2>Model</h2>" + "".join(parts))
        simplified = [f.simplified for f in figs if f.simplified]
        if simplified:
            footnotes.append(f"* Simplified for {fmt(result['num_variables'])} variables: "
                             + "; ".join(simplified) + ".")

        cards = [job_card(result, axis)]
        if result.get("statistics"):
            cards.append(usage_card(result["statistics"]))
        run = f'<div class="run">{"".join(cards)}</div>'
        meta = [(k, esc(val), None) for k, val in result.get("result_info", {}).items()]
        if meta:
            run += ('<details class="card meta"><summary>Solver metadata</summary>'
                    f"<dl>{dl_html(meta)}</dl></details>")
        sections.append(f"<h2>Run details</h2>{run}")

    if result is not None:
        footnotes.append("Objective values are recomputed locally from the returned samples.")
        footnotes.append(f"Energies are in units of the coupling {ENERGY_UNIT}: a coefficient "
                         f"of 1 in the submitted model is 1 {ENERGY_UNIT}.")
    footer = "".join(f"<footer>{esc(f)}</footer>" for f in footnotes)
    subtitle = (f'<p class="subtitle">{esc(view["subtitle"])}</p>'
                if view is not None and view.get("subtitle") else "")
    body = (f'<p class="eyebrow">VeloxQ solution report</p><h1>{esc(title)}</h1>'
            f'{subtitle}{"".join(head)}{tiles_html(tiles) if tiles else ""}'
            f'{"".join(sections)}{footer}')
    check_size(len(body.encode("utf-8")), "the report would be")
    return title, body


def dl_html(rows) -> str:
    """Label, value markup and optional note of each row, as a definition list."""
    return "".join(f"<dt>{esc(k)}</dt><dd>{html}"
                   + (f'<span class="sub">{esc(note)}</span>' if note else "") + "</dd>"
                   for k, html, note in rows)


def duration(seconds: float) -> str:
    if seconds == 0:
        return "0 s"
    if seconds < 1:
        return f"{seconds * 1000:.3g} ms"
    if seconds < 60:
        return f"{seconds:.3g} s"
    minutes, rest = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def dollars(x: float) -> str:
    """Cents above a dollar, two significant digits below it."""
    if x == 0 or abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.{max(2, 1 - math.floor(math.log10(abs(x))))}f}"


def job_card(result: dict, axis: str) -> str:
    kind = "Ising" if result["type"] == "ising" else "QUBO"
    variables = "spin" if result["vartype"] == "SPIN" else "binary"
    backend = result.get("backend") or {}
    job_id = result.get("job_id")
    finished = result.get("finished_at")
    if finished:
        finished = (datetime.fromisoformat(finished.replace("Z", "+00:00"))
                    .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    rows = [("Solver", result.get("solver"), None),
            ("Backend", backend.get("name"), backend.get("description")),
            ("Job id", job_id, None),
            ("Model", kind, f"{fmt(result['num_variables'])} {variables} variables"),
            ("Samples", fmt(result["num_samples"]),
             f"{fmt(result['num_samples_at_best'])} at the best value"),
            (f"Best {axis}", with_unit(result["best_objective"], ENERGY_UNIT), None),
            ("Finished", finished, None)]
    cells = [(k, esc(v), note) for k, v, note in rows if v is not None]
    if job_id:
        cells = [(k, f'<a href="{JOB_URL}{quote(str(job_id), safe="")}" target="_blank" '
                     f'rel="noopener noreferrer">{html}</a>' if k == "Job id" else html, note)
                 for k, html, note in cells]
    return f'<section class="card"><h3>Job</h3><dl>{dl_html(cells)}</dl></section>'


def usage_card(stats: dict) -> str:
    """Time, quota and cost of the job from the platform's statistics. A
    statistic the platform left out is not shown."""
    def has(*keys):
        return all(stats.get(k) is not None for k in keys)

    tiles = []
    if has("usage_time"):
        tiles.append(("API time", duration(stats["usage_time"]),
                      f"{duration(stats['pending_time'])} pending, "
                      f"{duration(stats['running_time'])} running"
                      if has("pending_time", "running_time") else None))
    if has("quota_solver_time"):
        tiles.append(("Solver time", duration(stats["quota_solver_time"]),
                      f"{duration(stats['cost_solver_time'])} billed"
                      if has("cost_solver_time") else None))
    if has("total_quota_usage"):
        tiles.append(("Quota used", f"{fmt(stats['total_quota_usage'])} ms", None))
    if has("total_cost"):
        tiles.append(("Cost", dollars(stats["total_cost"]),
                      f"at {dollars(stats['solver_cost'])}/s"
                      if has("solver_cost") else None))
    rows = []
    if has("solver_usage_scale"):
        rows.append(("Solver multiplier", f"×{fmt(stats['solver_usage_scale'])}", "quota"))
    cost, usage = stats.get("backend_cost_scale"), stats.get("backend_usage_scale")
    if cost is not None and cost == usage:
        rows.append(("Backend multiplier", f"×{fmt(cost)}", "cost and quota"))
    elif cost is not None or usage is not None:
        rows.append(("Backend multiplier", ", ".join(
            f"×{fmt(x)} {what}" for x, what in ((cost, "cost"), (usage, "quota"))
            if x is not None), None))
    stat = "".join(f'<div class="stat"><div class="label">{esc(label)}</div>'
                   f'<div class="value">{esc(value)}</div>'
                   + (f'<div class="note">{esc(note)}</div>' if note else "") + "</div>"
                   for label, value, note in tiles)
    return ('<section class="card"><h3>Usage and cost</h3>'
            + (f'<div class="stats">{stat}</div>' if stat else "")
            + (f"<dl>{dl_html([(k, esc(v), n) for k, v, n in rows])}</dl>" if rows else "")
            + "</section>")


def check_size(size: int, what: str) -> None:
    if size > MAX_BODY_BYTES:
        fail(f"{what} larger than {MAX_BODY_BYTES / 1e6:g} MB; draw fewer nodes, edges, "
             "cells or rows, or shorten their labels")


def page(title: str, body: str) -> str:
    """The report as one standalone HTML document."""
    # No <html>, <head> or <body> tags: HTML does not need them, and an
    # artifact host wraps the file in its own.
    return (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f"<style>{theme_css()}{PAGE_CSS}{figure_css()}</style>"
        f'<main>{body}</main><div id="vq-tip" hidden></div>'
        f"<script>{share_js()}{TIPS_JS}vqTips(document);vqShare(vqDownload);</script>")


def app_page() -> str:
    """The MCP App that shows a report body sent as a tool result."""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>VeloxQ report</title>"
        f"<style>{theme_css()}{PAGE_CSS}{APP_CSS}{figure_css()}</style></head>"
        '<body><main id="vq-report"><p class="note">Building the report…</p></main>'
        '<div id="vq-tip" hidden></div>'
        f"<script>{share_js()}{TIPS_JS}{APP_JS}</script></body></html>")
