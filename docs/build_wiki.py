#!/usr/bin/env python3
"""Build the documentation site.

Writes index.html, veloxq-solve.html and veloxq-solve-examples.html to the
output directory (default _site/ next to this script), with static/ and the
recorded HTML files of examples/ copied beside them. The example transcripts in
examples/<slug>.json were recorded from headless Claude Code runs with the
plugin loaded; EXAMPLES below says how each one is presented.

    python3 build_wiki.py [--out DIR] [--artifact-dir DIR]

`--artifact-dir` also writes a copy of the site whose index.html has no
document skeleton, which is what the claude.ai Artifact tool expects for the
main page. The build date printed on every page is today's date in UTC, or
the date of SOURCE_DATE_EPOCH when that variable is set. Requires mistune.
"""
import argparse
import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import mistune

HERE = Path(__file__).resolve().parent
EXAMPLES_DIR = HERE / "examples"
STATIC_DIR = HERE / "static"
REPO = "https://github.com/quantumz-io/qmz-claude"


def build_date():
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    d = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch else datetime.now(timezone.utc)
    return f"{d.day} {d:%B %Y}"


BUILT = build_date()
TOOL_PREFIX = "mcp__plugin_veloxq-solve_veloxq__"

PAGES = [
    ("index.html", "Home", "QuantumZ for Claude Code"),
    ("veloxq-solve.html", "veloxq-solve", "The veloxq-solve plugin"),
    ("veloxq-solve-examples.html", "Examples", "veloxq-solve examples"),
]

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,650;12..96,700;12..96,800'
         '&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500;600&display=swap">')

md_render = mistune.create_markdown(plugins=["table", "strikethrough"])


def esc(s):
    return html.escape(str(s), quote=True)


def md(text):
    out = md_render(text)
    return out.replace("<table>", '<div class="tablewrap"><table>').replace("</table>", "</table></div>")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- components

_counter = [0]


MARKETPLACE_ADD = "/plugin marketplace add quantumz-io/qmz-claude"
PLUGIN_INSTALL = "/plugin install veloxq-solve@quantumz"


def codeblock(code, label=None, slash=False):
    _counter[0] += 1
    cid = f"code-{_counter[0]}"
    body = esc(code.rstrip("\n"))
    if slash:
        # Claude Code runs one slash command per prompt. A pasted second line
        # becomes part of the first command's argument.
        if "\n" in body:
            raise ValueError(f"slash codeblock must hold a single command: {code!r}")
        body = re.sub(r"^(/\S+)", r'<span class="prompt-token">\1</span>', body, flags=re.M)
    head = f'<span>{esc(label or "")}</span><button class="copy-btn" type="button" data-copy="{cid}">Copy</button>'
    return (f'<div class="codeblock{" slash" if slash else ""}"><div class="cb-head">{head}</div>'
            f'<pre><code id="{cid}">{body}</code></pre></div>')


def install_blocks():
    return (codeblock(MARKETPLACE_ADD, "1 · Register the marketplace", slash=True)
            + codeblock(PLUGIN_INSTALL, "2 · Install the plugin from this marketplace", slash=True))


def adm(kind, title, body_html):
    return f'<div class="adm {kind}"><div class="adm-title">{esc(title)}</div>{body_html}</div>'


def table(headers, rows, num_cols=()):
    th = "".join(f'<th class="{"num" if i in num_cols else ""}">{h}</th>' for i, h in enumerate(headers))
    trs = []
    for r in rows:
        tds = "".join(f'<td class="{"num" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r))
        trs.append(f"<tr>{tds}</tr>")
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'


class Page:
    def __init__(self, file, name, title):
        self.file, self.name, self.title = file, name, title
        self.sections = []  # (id, title, level, html)
        self.intro = ""

    def section(self, title, body, level=2, id=None):
        sid = id or slug(title)
        self.sections.append((sid, title, level, body))
        return sid

    def body_html(self):
        out = [self.intro]
        for sid, title, level, body in self.sections:
            tag = f"h{level}"
            out.append(f'<{tag} id="{sid}">{esc(title)}<a class="anchor" href="#{sid}" aria-label="Link to {esc(title)}">#</a></{tag}>')
            out.append(body)
        return "\n".join(out)


# ---------------------------------------------------------------- transcripts

EXAMPLES = {
    "01-number-partitioning": {
        "title": "Balance server loads across two racks",
        "kind": "Number partitioning",
        "group": "simple",
        "note": "A ten-number partition. The skill encodes it as an Ising model with no constraints, submits one job, decodes the best sample into the two racks, and proves the difference of 1 is optimal because the total is odd.",
    },
    "02-max-cut": {
        "title": "Max-cut on an 8-node graph",
        "kind": "Max-cut",
        "group": "simple",
        "note": "Max-cut is the native problem of an Ising machine: one coupling per edge, nothing else. Claude counts the cut from the partition rather than from the energy, and finds the odd-cycle argument that shows 11 is the maximum.",
    },
    "03c-knapsack": {
        "title": "Knapsack with a capacity limit",
        "kind": "Knapsack, inequality constraint",
        "group": "complex",
        "note": "The capacity constraint is an inequality, so the encoding adds binary slack variables and a squared penalty. The penalty weight has to beat every item value, and the decoded answer is checked against the capacity before it is reported.",
    },
    "08b-graph-coloring": {
        "title": "3-coloring a prism graph",
        "kind": "Graph coloring, one-hot encoding",
        "group": "complex",
        "note": "Each node gets three binary variables, one per color, with a penalty that forces exactly one of them on and a penalty on every edge whose two ends share a color. The decoded coloring is checked edge by edge.",
    },
    "04-tsp-report": {
        "title": "Delivery round trip with a visual report",
        "kind": "Travelling salesman, visual report",
        "group": "complex",
        "note": "A seven-stop tour needs 36 position variables and two families of one-hot constraints. Because the prompt asked for a visual report, Claude builds the report view from the same decode script and renders it with the report tool in the same turn.",
    },
    "05-ising-ring-50k": {
        "title": "A 50,000-spin ferromagnetic ring",
        "kind": "Large sparse Ising model",
        "group": "scale",
        "note": "Fifty thousand spins go up as one JSON file through submit_job. The job ran in 14 seconds on two GPUs. Claude reports the energy, counts the 30 domain walls in the returned state, and explains why the default run stops 0.12% short of the ground state instead of calling it optimal.",
    },
    "06-paper-all-to-all": {
        "title": "Certified ground states from the VeloxQ paper",
        "kind": "Benchmark reproduction, section VII.1",
        "group": "paper",
        "note": "Claude clones the paper's data repository, notices the instance files are Git LFS pointers and fetches the real files from the media endpoint. A first 40-spin job read as QUBO lands far from the reference energy, so Claude switches to the Ising reading and resubmits. Every best energy then matches the certified ground state, and Claude separates the platform's running time from queueing and from the paper's own tuned runs before drawing conclusions.",
    },
    "07-paper-chimera": {
        "title": "A 512-spin Chimera instance against BEIT",
        "kind": "Benchmark reproduction, section VII.2",
        "group": "paper",
        "note": "An HDF5 file in the platform's native layout goes straight to submit_job. Claude verifies the download against its LFS hash, recomputes every returned energy from the file, and states what a heuristic runtime can and cannot be compared with.",
    },
}

GROUPS = [
    ("simple", "Simple prompts", "Everyday optimization questions asked in plain language. Nothing in the prompt mentions QUBO, Ising or VeloxQ; the skill recognizes the problem class and takes it from there."),
    ("complex", "Problems with hard constraints", "Constraints turn into penalty terms, and the encoding is where a solve succeeds or fails. These runs show the slack variables, one-hot groups and penalty weights the skill chooses, and the constraint checks it runs on the returned sample before answering."),
    ("scale", "Large instances", "The platform handles sparse models with millions of coefficients. This run shows a model too large to show in the conversation going through the same tools as the small ones."),
    ("paper", "Reproducing benchmarks", "The white paper <a href=\"https://arxiv.org/abs/2501.19221\">VeloxQ: A Fast and Efficient QUBO Solver</a> (arXiv:2501.19221) publishes its instances and measurements in the <a href=\"https://github.com/quantumz-io/velox-data\">velox-data</a> repository. These prompts name that repository, nothing more specific, and ask for time to solution, the best energy, and a comparison with the paper. Claude finds the files, works around the Git LFS storage and converts the formats itself. A prompt that names only the paper also worked when tested. Claude then locates the repository through the arXiv page, which adds a few minutes of browsing. Only a small subset of each benchmark is run, three instances in one case and one in the other, so the runs finish in about a minute and a half."),
]

SMALL_PROBLEM_TIP = adm("tip", "Name the platform when a small problem must run there",
    "<p>Claude may solve a tiny problem by hand instead of submitting it. Add <em>“Solve it on VeloxQ”</em> to the prompt when it must run on the platform, as the knapsack and 3-coloring examples do.</p>")

WORK_RE = re.compile(r"/tmp/claude-1000/[^\s\"'`)]*?/scratchpad/work/([\w-]+)")
PLUGIN_RE = re.compile(r"/home/lorentz/work_space/qmz/veloxq-solve/plugins/veloxq-solve")
EMAIL_RE = re.compile(r"hendrik\.louzada@qmz\.ai")
# Tool outputs can carry raw bytes (an HDF5 header printed by a shell command); they are not publishable text.
CONTROL_RE = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\ufffd]")


def clean(text):
    if not isinstance(text, str):
        text = json.dumps(text, indent=1, ensure_ascii=False)
    text = WORK_RE.sub(r"~/veloxq-examples/\1", text)
    text = PLUGIN_RE.sub("<plugin>", text)
    text = EMAIL_RE.sub("h•••@qmz.ai", text)
    text = CONTROL_RE.sub(".", text)
    return text


def pretty_result(text, limit=2600):
    if text is None:
        return ""
    s = text
    try:
        obj = json.loads(text)
        s = json.dumps(obj, indent=1, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    s = clean(s)
    if len(s) > limit:
        s = s[:limit] + f"\n… ({len(s) - limit:,} more characters)"
    return s


def short_name(name):
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


def step_html(i, s):
    if s["kind"] == "text":
        return (f'<li class="step"><span class="n">{i}</span><div class="what">'
                f'<span class="badge text">says</span><span class="said">{esc(clean(s["text"]))}</span></div></li>')
    name, inp, res = s["name"], s["input"], s["result"]
    is_mcp = name.startswith(TOOL_PREFIX)
    badge = f'<span class="badge {"mcp" if is_mcp else "local"}">{"veloxq · " + esc(short_name(name)) if is_mcp else esc(name)}</span>'
    summary, detail_in = "", None
    if name == "Skill":
        summary = "Loaded the <code>veloxq-solve</code> skill, which sets the workflow for the rest of the run."
    elif name == "ToolSearch":
        summary = "Loaded the definitions of the VeloxQ tools."
    elif name in ("Grep", "Read"):
        p = clean(inp.get("path") or inp.get("file_path") or "")
        what = "the formulation catalog" if "qubo-formulations" in p else "the report view reference" if "report-views" in p else f"<code>{esc(p)}</code>"
        summary = f"{'Searched' if name == 'Grep' else 'Read'} {what}" + (f" for <code>{esc(inp['pattern'])}</code>" if name == "Grep" else "") + "."
    elif name == "Write":
        summary = f"Wrote <code>{esc(Path(clean(inp.get('file_path', ''))).name)}</code>."
        detail_in = clean(inp.get("content", ""))
    elif name == "Bash":
        cmd = clean(inp.get("command", ""))
        first = cmd.strip().splitlines()[0] if cmd.strip() else ""
        summary = f"Ran a shell command: <code>{esc(first[:110])}{'…' if len(first) > 110 or len(cmd.strip().splitlines()) > 1 else ''}</code>"
        detail_in = cmd
    elif name == "WebFetch":
        summary = f"Fetched <code>{esc(inp.get('url', ''))}</code>."
    elif is_mcp:
        t = short_name(name)
        args = dict(inp)
        bits = []
        if "path" in args:
            bits.append(f"path <code>{esc(Path(clean(args['path'])).name)}</code>")
        if "job_id" in args:
            bits.append(f"job <code>{esc(args['job_id'][:8])}</code>")
        if "wait_seconds" in args:
            bits.append(f"wait up to {args['wait_seconds']} s")
        if "output" in args:
            bits.append(f"output <code>{esc(Path(clean(args['output'])).name)}</code>")
        if "view_path" in args:
            bits.append(f"view <code>{esc(Path(clean(args['view_path'])).name)}</code>")
        if "spec" in args and args["spec"]:
            bits.append("inline spec")
        verbs = {"account": "Checked the connection and balance", "submit_job": "Submitted the model",
                 "get_job": "Followed the job", "get_result": "Fetched the verified samples",
                 "report": "Rendered the visual report", "job_logs": "Read the platform log of the job",
                 "list_solvers": "Listed the solvers", "connect": "Started the OAuth consent"}
        summary = verbs.get(t, t) + (" (" + ", ".join(bits) + ")" if bits else "") + "."
        detail_in = json.dumps(inp, indent=1) if inp else None
    else:
        summary = esc(name)
        detail_in = json.dumps(inp, indent=1)
    parts = [f'<li class="step"><span class="n">{i}</span><div class="what">{badge}<span class="sum">{summary}</span>']
    if detail_in:
        parts.append(f'<details><summary>Input</summary><pre>{esc(clean(detail_in)[:2600])}</pre></details>')
    if res and name not in ("Skill", "ToolSearch"):
        label = "Error" if s.get("error") else "Result"
        parts.append(f'<details><summary>{label}</summary><pre>{esc(pretty_result(res))}</pre></details>')
    parts.append("</div></li>")
    return "".join(parts)


def load_example(slug_):
    data = json.loads((EXAMPLES_DIR / f"{slug_}.json").read_text())
    return data


def example_html(slug_, final_rewrite=None, extra_html="", after_html=""):
    meta = EXAMPLES[slug_]
    d = load_example(slug_)
    _counter[0] += 1
    pid = f"prompt-{_counter[0]}"
    tools_used = sum(1 for s in d["steps"] if s["kind"] == "tool")
    final = clean(d["final"])
    if final_rewrite:
        final = final_rewrite(final)
    secs = round((d.get("duration_ms") or 0) / 1000)
    prompt_paras = "".join(f"<p>{esc(p)}</p>" for p in d["prompt"].split("\n\n"))
    meta_row = (f'<div class="meta-row"><span>Model <b>{esc(d["model"])}</b></span>'
                f'<span>Run time <b>{secs} s</b></span><span>Turns <b>{d["turns"]}</b></span>'
                + '</div>')
    return f'''
<div class="example" id="ex-{slug_}">
  <div class="example-head"><h3 id="{slug_}">{esc(meta["title"])}<a class="anchor" href="#{slug_}">#</a></h3><span class="tag">{esc(meta["kind"])}</span></div>
  <p>{meta["note"]}</p>
  {meta_row}
  <div class="prompt"><div class="who"><span>You</span><button class="copy-btn" type="button" data-copy="{pid}">Copy prompt</button></div><div id="{pid}">{prompt_paras}</div></div>
  <div class="answer"><div class="who"><span>Claude</span><span class="model">{esc(d["model"])}</span></div>{md(final)}</div>
  {extra_html}
  <details class="trace"><summary>What Claude did <span class="count">· {tools_used} tool calls</span></summary>
    <ol>{"".join(step_html(i + 1, s) for i, s in enumerate(d["steps"]))}</ol>
  </details>
  {after_html}
</div>'''


# ---------------------------------------------------------------- figures

def architecture_svg():
    boxes = [
        (10, "You", "a question in plain language", "or a model file"),
        (240, "Claude Code", "veloxq-solve skill:", "formulate, decode, verify"),
        (470, "veloxq MCP server", "part of the VeloxQ API,", "acts with your OAuth token"),
        (700, "VeloxQ platform", "GPU solvers,", "samples back in seconds"),
    ]
    out = ['<svg viewBox="0 0 920 170" role="img" aria-label="Request flow from the user through Claude Code and the local MCP server to the VeloxQ platform">']
    out.append('<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" style="fill:var(--muted)"/></marker></defs>')
    for x, title, l1, l2 in boxes:
        out.append(f'<rect x="{x}" y="30" width="210" height="92" rx="12" style="fill:var(--bg);stroke:var(--line-strong)"/>')
        out.append(f'<text x="{x + 105}" y="60" text-anchor="middle" style="fill:var(--ink);font-weight:650;font-size:16px;font-family:var(--font-display)">{esc(title)}</text>')
        out.append(f'<text x="{x + 105}" y="84" text-anchor="middle" style="fill:var(--muted);font-size:12.5px">{esc(l1)}</text>')
        out.append(f'<text x="{x + 105}" y="102" text-anchor="middle" style="fill:var(--muted);font-size:12.5px">{esc(l2)}</text>')
    for x in (220, 450, 680):
        out.append(f'<line x1="{x}" y1="62" x2="{x + 20}" y2="62" style="stroke:var(--muted);stroke-width:2" marker-end="url(#arr)"/>')
        out.append(f'<line x1="{x + 20}" y1="92" x2="{x}" y2="92" style="stroke:var(--accent);stroke-width:2" marker-end="url(#arr)"/>')
    out.append('<text x="335" y="150" text-anchor="middle" style="fill:var(--muted);font-size:12.5px">grey arrows: prompt, model, job · teal arrows: samples, verified objective, report</text>')
    out.append("</svg>")
    return "".join(out)


def runtime_chart():
    """Log-scale runtime bars for the all-to-all benchmark: this run against the paper's table."""
    groups = [("40 spins", [0.281, 0.0456, 0.207, 1.96]),
              ("50 spins", [0.314, 0.0425, 0.984, 255.0]),
              ("60 spins", [0.312, 0.0595, 17.1, 255292.0])]
    series = ["This run, VeloxQ running time", "Paper, VeloxQ autotune", "Paper, CPLEX", "Paper, brute force"]
    colors = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"]
    import math
    lo, hi = -2, 6  # 0.01 s .. 1e6 s
    left, right, top = 130, 40, 10
    W = 860
    plot_w = W - left - right
    bar_h, gap, group_gap = 14, 3, 22
    group_h = len(series) * (bar_h + gap) + group_gap
    H = top + len(groups) * group_h + 30

    def x_of(v):
        return left + (math.log10(v) - lo) / (hi - lo) * plot_w

    def fmt(v):
        if v >= 3600:
            return f"{v / 3600:.0f} h"
        if v >= 100:
            return f"{v:.0f} s"
        if v >= 1:
            return f"{v:.2f} s"
        if v >= 0.1:
            return f"{v:.2f} s"
        return f"{v * 1000:.0f} ms"

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Runtime per method and instance size on a logarithmic scale">']
    for e in range(lo, hi + 1):
        x = x_of(10 ** e)
        out.append(f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{H - 28}"/>')
        lab = {-2: "10 ms", -1: "0.1 s", 0: "1 s", 1: "10 s", 2: "100 s", 3: "1000 s", 4: "~3 h", 5: "~1 d", 6: "~12 d"}[e]
        out.append(f'<text x="{x:.1f}" y="{H - 10}" text-anchor="middle">{lab}</text>')
    y = top
    for gname, vals in groups:
        out.append(f'<text class="grp" x="{left - 12}" y="{y + 2 * (bar_h + gap) + 2}" text-anchor="end">{gname}</text>')
        for k, v in enumerate(vals):
            yy = y + k * (bar_h + gap)
            x1 = x_of(v)
            out.append(f'<rect x="{left}" y="{yy}" width="{x1 - left:.1f}" height="{bar_h}" rx="3" style="fill:{colors[k]}"><title>{series[k]}, {gname}: {v:g} s</title></rect>')
            out.append(f'<text class="val" x="{x1 + 6:.1f}" y="{yy + bar_h - 3}">{fmt(v)}</text>')
        y += group_h
    out.append(f'<line class="base" x1="{left}" y1="{top}" x2="{left}" y2="{H - 28}"/>')
    out.append("</svg>")
    legend = "".join(f'<span style="--c:{c}">{esc(s)}</span>' for s, c in zip(series, colors))
    return (f'<div class="chart"><div class="legend">{legend}</div>{"".join(out)}'
            f'<div class="axis-note">Seconds on a logarithmic axis. Paper values are the per-instance means in <code>bf_all-to-all.csv</code>; this run is one job per instance with default parameters on the <code>veloxq_h100_2</code> backend.</div></div>')


# ---------------------------------------------------------------- pages

def build_home():
    p = Page(*PAGES[0])
    p.intro = f'''
<div class="hero">
  <span class="kicker">Claude Code plugin marketplace</span>
  <div>
      <h1>QuantumZ for Claude Code</h1>
      <p class="lead">A marketplace of plugins that connect Claude to QuantumZ products. Add it once, install a plugin, and Claude gains the tools and the know-how to use the product from a normal conversation.</p>
      {install_blocks()}
      <p class="small">Two commands, no API keys in the conversation, no SDK to learn. The first plugin, <a href="veloxq-solve.html">veloxq-solve</a>, solves optimization problems on the VeloxQ platform.</p>
  </div>
</div>'''

    p.section("What a plugin marketplace is", f'''
<p>Claude Code <a href="https://code.claude.com/docs/en/plugins">plugins</a> bundle skills, MCP servers, commands and hooks so that they install as one unit. A <a href="https://code.claude.com/docs/en/plugin-marketplaces">marketplace</a> is a Git repository with a catalog file, <code>.claude-plugin/marketplace.json</code>, that lists plugins and where they live. Once a marketplace is added, its plugins can be installed by name, updated from the source, and removed again, all from inside a session.</p>
<p>This marketplace is named <code>quantumz</code>. Its catalog points at the plugins in the <code>plugins/</code> directory of the same repository, so installing a plugin fetches the repository and copies the plugin into Claude Code's plugin cache.</p>
''')

    p.section("Installation", f'''
<p>Installation takes a minute and happens entirely inside a Claude Code session.</p>
<ol class="steps">
  <li><div><h4>Have Claude Code ready</h4><p>Any current Claude Code release works. The examples in this documentation were recorded with Claude Code 2.1.312. Open a session in any directory with <code>claude</code>.</p></div></li>
  <li><div><h4>Add the marketplace</h4>{codeblock(MARKETPLACE_ADD, None, slash=True)}<p>Claude Code clones the repository and registers the catalog under the name <code>quantumz</code>. Run <code>/plugin marketplace list</code> to confirm.</p></div></li>
  <li><div><h4>Install a plugin</h4>{codeblock(PLUGIN_INSTALL, None, slash=True)}<p>The <code>name@marketplace</code> form names the catalog explicitly. The plugin installs for your user by default, so it is available in every project. Add <code>--scope project</code> to install it for the current repository only.</p></div></li>
  <li><div><h4>Check that it loaded</h4><p><code>/plugin</code> opens the plugin manager and lists what is installed and enabled. A plugin that ships an MCP server, such as veloxq-solve, also shows up under <code>/mcp</code> once its server has started. Skills from the plugin appear in the skill list and fire on matching prompts.</p></div></li>
</ol>
{adm("tip", "Trying a plugin without installing it", "<p>To run a checkout of this repository without touching your installation, start Claude Code with <code>claude --plugin-dir /path/to/qmz-claude/plugins/veloxq-solve</code>. The plugin loads for that session only. The same flag also works with a <code>.zip</code> archive of a plugin.</p>")}
<p>The same operations exist as terminal commands for scripts and CI: <code>claude plugin marketplace add</code>, <code>claude plugin install</code>, <code>claude plugin enable</code> and <code>claude plugin disable</code>. Run <code>claude plugin --help</code> for the full list.</p>
''')

    p.section("Plugins in this marketplace", f'''
<div class="cards">
  <div class="card">
    <div class="row"><h3><a href="veloxq-solve.html">veloxq-solve</a></h3><span class="ver">v0.2.0</span></div>
    <p>Solve QUBO and Ising optimization problems on the <a href="https://web.veloxq.com">VeloxQ</a> platform. Ask for a max-cut, a schedule, a knapsack, a tour or a graph coloring in plain language, or hand over a model file, and Claude formulates, submits, verifies and reports.</p>
    <div class="tags"><span class="tag">MCP server</span><span class="tag">Skill</span><span class="tag">Formulation catalog</span><span class="tag">Visual report</span></div>
    <ul>
      <li>Ten tools covering account, solvers, jobs, results, logs and the report.</li>
      <li>OAuth 2.0 connection to your VeloxQ account, approved in the browser.</li>
      <li>Models as inline JSON, JSON files or HDF5 files up to hundreds of millions of coefficients.</li>
      <li>Every returned sample is re-scored locally before it is reported.</li>
    </ul>
    <a class="cta" href="veloxq-solve.html">Read the plugin page →</a>
  </div>
</div>
{table(["Plugin", "Install", "Needs", "What it adds"], [
    ["<code>veloxq-solve</code>", "<code>/plugin install veloxq-solve@quantumz</code>", "Python 3.10+, a VeloxQ account, network access", "The <code>veloxq</code> MCP server and the <code>veloxq-solve</code> skill"],
])}
''')

    p.section("Updating and removing", f'''
<p>Marketplaces are updated from their source, and plugins follow the catalog version.</p>
{codeblock("/plugin marketplace update quantumz", "Refresh the catalog from GitHub", slash=True)}
{codeblock("/plugin uninstall veloxq-solve@quantumz", "Remove the plugin", slash=True)}
{codeblock("/plugin marketplace remove quantumz", "Forget the marketplace", slash=True)}
<p>Disabling a plugin keeps it installed but stops it from loading: <code>/plugin disable veloxq-solve@quantumz</code>, and <code>enable</code> to turn it back on. The interactive manager behind <code>/plugin</code> offers the same actions with a menu.</p>
''')

    p.section("Repository layout", f'''
{table(["Path", "Purpose"], [
    ["<code>.claude-plugin/marketplace.json</code>", "The catalog: marketplace name, owner, and one entry per plugin with its source path and version"],
    ["<code>plugins/veloxq-solve/</code>", "The veloxq-solve plugin: manifest, MCP server, skill, evals and tests"],
    ["<code>docs/</code>", "This documentation, built by <code>docs/build_wiki.py</code> from the page sources and the recorded example transcripts in <code>docs/examples/</code>"],
])}
<p>Adding a plugin to the marketplace means adding a directory under <code>plugins/</code> with a <code>.claude-plugin/plugin.json</code> manifest and listing it in the catalog. The catalog follows the schema at <code>https://code.claude.com/schemas/marketplace.json</code>.</p>
''')

    p.section("Troubleshooting", f'''
{table(["Symptom", "What to do"], [
    ["<code>/plugin marketplace add</code> fails to fetch", "The source is a public GitHub repository; check that <code>git</code> can reach github.com from your machine, or add it by HTTPS URL: <code>https://github.com/quantumz-io/qmz-claude</code>."],
    ["The plugin installed but its skill never fires", "Run <code>/plugin</code> and confirm the plugin is enabled. Skills trigger on the problem class; mentioning the product by name in the prompt always works."],
    ["A plugin's MCP server shows as failed under <code>/mcp</code>", "Plugin-specific. For veloxq-solve see <a href='veloxq-solve.html#troubleshooting'>its troubleshooting section</a>; the first start builds a Python environment and can take a minute."],
])}
''')
    return p


def build_plugin():
    p = Page(*PAGES[1])
    p.intro = f'''
<p class="eyebrow">Plugin · version 0.2.0</p>
<h1>veloxq-solve</h1>
<p class="lead">Solve combinatorial optimization problems on the VeloxQ platform from a Claude Code conversation. Describe the problem, and Claude formulates it as a QUBO or Ising model, runs it on VeloxQ's GPU solvers, verifies the returned samples and explains the result in your terms.</p>
{install_blocks()}
'''
    p.section("What VeloxQ is", f'''
<p><a href="https://web.veloxq.com">VeloxQ</a> is a cloud service from QuantumZ that samples low-energy states of Ising models, which is the same thing as minimizing a quadratic unconstrained binary optimization problem (QUBO). Its solvers are physics-inspired and run on GPUs, handle any coupling graph without the embedding step that hardware annealers need, and scale to sparse instances with hundreds of millions of variables. The platform returns many candidate solutions per job and an energy for each.</p>
<p>Almost any discrete optimization problem can be written in this form: max-cut, graph partitioning and coloring, number partitioning, knapsack, routing, scheduling, independent sets, portfolio selection and spin glasses among them. The encoding is where the work is, and that is what this plugin automates.</p>
''')

    p.section("What the plugin provides", f'''
<p>The plugin has two parts that work together.</p>
<figure class="figure">{architecture_svg()}<figcaption>Claude never talks to the platform directly. The skill decides what to do; the MCP server does it with your account.</figcaption></figure>
<h3 id="the-mcp-server">The <code>veloxq</code> MCP server</h3>
<p>A server the VeloxQ API serves at <code>https://api.veloxq.com/mcp</code>, which the plugin registers in Claude Code. It wraps the platform as typed tools and acts with the OAuth token of your account on every call; Claude Code holds that token, nothing is installed on your machine. Its tools appear in Claude Code as <code>mcp__plugin_veloxq-solve_veloxq__&lt;tool&gt;</code>.</p>
<h3 id="the-skill">The <code>veloxq-solve</code> skill</h3>
<p>A skill that fires whenever a conversation involves a combinatorial or discrete optimization problem, or mentions QUBO, Ising, annealing or VeloxQ. It carries the workflow, a catalog of standard encodings with penalty-weight guidance, and the specification of the visual report. The skill makes Claude check the connection first, encode the problem, submit once, re-score the returned samples, check every hard constraint, and present the answer in the problem's own units.</p>
''')

    p.section("Requirements", f'''
<ul>
  <li>Network access to <code>api.veloxq.com</code>, and a browser for the one-time account consent.</li>
  <li>A <a href="https://web.veloxq.com">VeloxQ account</a> with balance or compute quota. Every job is charged to the account.</li>
</ul>
''')

    p.section("Installation and first use", f'''
<ol class="steps">
  <li><div><h4>Install from the marketplace</h4>{install_blocks()}</div></li>
  <li><div><h4>Connect your account</h4><p>Run <code>/mcp</code>, pick the <code>veloxq</code> server and authenticate. VeloxQ's consent page opens in your browser, and Claude Code shows the link in case it did not. Log in and approve. Claude Code keeps the tokens and refreshes them. No password, token or key passes through the conversation.</p></div></li>
  <li><div><h4>Ask your question</h4><p>From then on every session is connected. The connected-applications page of your VeloxQ account ends the connection.</p></div></li>
</ol>
{adm("tip", "A first prompt to try", "<p>Paste this and watch the whole workflow run: <em>“Solve max-cut on this 8-node graph: edges = [(0,1),(0,3),(1,2),(1,4),(2,5),(3,4),(3,6),(4,5),(4,7),(5,7),(6,7),(2,7)]. Which nodes go on each side, and how many edges are cut?”</em> The <a href='veloxq-solve-examples.html#02-max-cut'>recorded run</a> shows what to expect.</p>")}
{SMALL_PROBLEM_TIP}
''')

    p.section("How a solve works", f'''
<p>Every solve follows the same five steps, in this order because each one fails faster and cheaper than the next.</p>
<ol class="steps">
  <li><div><h4>Check the connection</h4><p><code>account</code> shows the balance and quota of the connected account. Claude warns before submitting when either is zero, since the platform refuses such jobs.</p></div></li>
  <li><div><h4>Formulate</h4><p>Decisions become binary variables, the objective becomes a quadratic polynomial, and each hard constraint becomes a penalty term that is zero when satisfied. The catalog in <code>references/qubo-formulations.md</code> gives the standard encodings and how to pick penalty weights, so that a weight is neither too small, which lets infeasible answers through, nor so large that the objective turns into rounding noise. Cubic terms, as in SAT, are reduced to quadratic ones with auxiliary variables. Claude keeps a map from problem variables to integer indices.</p></div></li>
  <li><div><h4>Submit, wait, fetch</h4><p><code>submit_job</code> takes the model as an inline spec and returns a job id at once. <code>get_job</code> follows it until it ends; most jobs finish within seconds. <code>get_result</code> fetches the best samples and recomputes every sample's objective with dimod, so the answer never depends on the platform's energy bookkeeping. The skill budgets one submission per problem and resubmits only deliberately, for example after raising a penalty weight.</p></div></li>
  <li><div><h4>Decode and verify</h4><p>A small script turns the best sample back into the user's domain, checks every hard constraint, recomputes the objective in the user's units, reports how many samples reached the best value, and runs a degeneracy check that catches a dropped linear term or a wrong variable type. Claude does not re-solve the problem by another method; these checks are the verification.</p></div></li>
  <li><div><h4>Present, then offer a report</h4><p>The answer gives the objective, the solution as a table or list, the constraint checks one per line, and the sample count, ending with an offer of a visual report. On yes, or when the prompt asked for a visual output, the same script writes the report view and the <code>report</code> tool renders it.</p></div></li>
</ol>
''')

    p.section("Tools reference", f'''
<p>All tools are provided by the <code>veloxq</code> server. Arguments in bold are required. File arguments take absolute paths on the machine running Claude Code.</p>
{table(["Tool", "Purpose", "Arguments"], [
    ["<code>account</code>", "The connected account's balance, quota and groups. Called first in every solve.", "none"],
    ["<code>list_solvers</code>", "Solvers with their parameter schemas, costs per second and backends.", "none"],
    ["<code>submit_job</code>", "Upload a QUBO or Ising model and start a job. Returns the job id at once.", "<b><code>spec</code></b> (inline, up to 20,000 coefficients); <code>solver</code> (default VeloxQ), <code>backend</code>, <code>parameters</code>"],
    ["<code>get_job</code>", "Status, timeline, cost and parameters of a job. With <code>wait_seconds</code> it follows the job until it ends.", "<b><code>job_id</code></b>, <code>wait_seconds</code> (default 0, capped at 60)"],
    ["<code>get_result</code>", "The best distinct samples of a finished job with objectives recomputed and checked against the platform's energies.", "<b><code>job_id</code></b>, the submitted <code>spec</code>, <code>top</code> (default 5, at most 100)"],
    ["<code>report</code>", "Render the visual report of a finished job, shown in the conversation as an MCP App.", "<b><code>job_id</code></b>, <code>view</code>, the submitted <code>spec</code>"],
    ["<code>list_jobs</code>", "Recent jobs of the account, newest first.", "<code>status</code>, <code>created</code> (today, yesterday, lastWeek, lastMonth, last3Months, lastYear, all), <code>limit</code> (default 20), <code>q</code>"],
    ["<code>job_logs</code>", "The platform's log lines of a job.", "<b><code>job_id</code></b>, <code>tail</code> (default 200), <code>category</code>, <code>grep</code>, <code>time_period</code>"],
    ["<code>cancel_job</code>", "Ask the platform to cancel a pending or running job. Asynchronous; confirm with <code>get_job</code>.", "<b><code>job_id</code></b>"],
])}
<p>Jobs started in another session or by other means can be inspected the same way. To pick up a solve whose session ended before the result came back, Claude calls <code>get_result</code> with the old job id and the same model, found with <code>list_jobs</code> if the id was not kept.</p>
''')

    p.section("Model formats", f'''
<p>Models are Ising (<code>E(s) = Σ h_i s_i + Σ J_ij s_i s_j</code> over spins ±1) or QUBO (<code>x<sup>T</sup> Q x</code> over binaries). The platform converts between the two, so the skill formulates in whichever is natural. Variables are integers <code>0..n-1</code>.</p>
{codeblock('{"type": "qubo",  "quadratic": [[0, 1, -2.0], [1, 2, 1.5]], "linear": [1.0, 0.0, -1.0]}\n{"type": "ising", "quadratic": [[0, 1, -1.0], [1, 2, -1.0]], "linear": [0.0, 0.0, 0.0]}', "JSON spec")}
<ul>
  <li><code>quadratic</code> is a list of <code>[i, j, value]</code> triples; <code>matrix</code> may replace it with a dense square matrix. <code>linear</code> is a list or an <code>{{"i": value}}</code> map and may be omitted.</li>
  <li>A spec is limited to 20,000 coefficients, indices below 100,000 and a matrix of at most 500 rows.</li>
  <li>A model file the user supplies (HDF5, CSV, NumPy) is read in a script and submitted as a spec; the skill asks about sign conventions and index bases the file does not make clear rather than guessing.</li>
  <li>Larger models go through the <a href="https://pypi.org/project/veloxq-sdk/">VeloxQ SDK</a>, whose native HDF5 layout reaches the platform's own scale of about 10<sup>8</sup> variables.</li>
</ul>
''')

    p.section("Solvers and backends", f'''
<p>The platform offered two solvers when this page was built. Both charge by solver time, scaled by the backend.</p>
{table(["Solver", "Parameters (defaults)", "Cost", "Backends online"], [
    ["<b>VeloxQ</b> (default)", "<code>num_rep</code> 4096 initial trajectories, <code>num_steps</code> 5000 time steps", "$0.55 per solver second × backend scale", "<code>veloxq_h100_1</code> (scale 1), <code>veloxq_h100_2</code> (scale 2), <code>plgrid-gpu-gh200</code> (scale 4)"],
    ["<b>SBM</b>, simulated bifurcation machine", "<code>dt</code>, <code>detuning</code>, <code>heating_rate</code>, <code>kerr_coeff</code>, <code>scale</code> (all 1.0), <code>num_rep</code> 4096, <code>num_steps</code> 5000", "$0.55 per solver second × backend scale", "same GPU backends"],
])}
<p>The skill leaves solver, backend and parameters at their defaults, which favor solution quality, unless asked otherwise. A request like <em>“use 512 replicas”</em> or <em>“run it on the SBM solver”</em> changes that for one job. <code>list_solvers</code> returns the live catalog, including parameter names and the cost scale of each backend.</p>
''')

    p.section("The visual report", f'''
<p>The <code>report</code> tool renders one self-contained HTML page per solve. It contains the headline numbers in the user's terms, the constraint checks, a figure of the decoded solution, the distribution of sample objectives, and the model's coupling graph in the state of the best sample. Figure types cover partitioned graphs, colorings, routes, bins, schedules, bar charts, grids and tables, chosen by the skill from the problem class. The page loads nothing from the network, follows the viewer's light or dark theme, and each figure has a Share menu that saves it as an image or CSV.</p>
<p>Claude builds the report's view from the same script that decoded and verified the sample, so the report shows the same checks and numbers as the answer. The page below was produced by the <a href="veloxq-solve-examples.html#04-tsp-report">delivery round trip example</a>.</p>
<a class="filelink" href="examples/tsp-report.html">Open the report of the delivery tour example ↗</a>
''')

    p.section("Problem catalog", f'''
<p>The skill's reference gives verified encodings for these problems and a general recipe for the rest. Indices are flattened integer indices, and the mapping back is kept for decoding.</p>
{table(["Problem", "Variables", "Encoding", "Report figure"], [
    ["Max-cut, graph partitioning", "one spin per node", "<code>H = Σ w_uv s_u s_v</code>, no penalties; cut size = (W − E)/2", "graph with cut edges"],
    ["Number partitioning", "one spin per number", "<code>H = (Σ a_i s_i)²</code>; H = 0 is a perfect partition", "bins"],
    ["Max independent set, vertex cover", "one binary per node", "node reward plus a penalty per edge with both ends chosen", "graph with conflicts"],
    ["Graph coloring", "n × k one-hot binaries", "exactly-one penalty per node plus a penalty per monochromatic edge", "graph with conflicts"],
    ["Knapsack", "one binary per item plus slack bits", "negative value plus a squared capacity penalty with binary-expanded slack", "bins"],
    ["TSP, routing", "n × n position one-hots", "distance terms between consecutive positions plus two one-hot families", "route"],
    ["Scheduling, assignment", "task × slot one-hots", "cost terms plus one-hot and conflict penalties", "schedule, grid"],
    ["MAX-3-SAT", "one binary per variable plus one auxiliary per clause", "Rosenberg reduction of cubic clause terms", "table"],
])}
<p>Problems that are not discrete, such as linear programs, least squares or continuous portfolio weights, are out of scope, and the skill says so instead of forcing a binary encoding.</p>
''')

    p.section("Troubleshooting", f'''
{table(["Symptom", "Cause and fix"], [
    ["The VeloxQ tools are missing, or a 401", "The account is not connected, or the connection ended. Run <code>/mcp</code> and authenticate the <code>veloxq</code> server in the browser. Never paste a token into the conversation."],
    ["A 403 naming a missing scope", "The consent was approved with fewer permissions than the action needs. Disconnect the app on the connected-applications page of your VeloxQ account and authenticate again, so the consent page lists every scope."],
    ["422 “Insufficient balance” or “account is not verified”", "The platform refuses the job. <code>account</code> shows the balance; top up or verify the account on the VeloxQ site."],
    ["429 with a retry hint", "The account's job queue is full. Wait the stated time."],
    ["A job shows FAILED", "Claude reads <code>job_logs</code> with category ERROR and relays the cause."],
    ["<code>get_result</code> reports an energy mismatch", "The platform's energies and the local recomputation disagree beyond a constant offset. The samples are not relayed; check that the same model was passed."],
    ["The solution violates a constraint", "The penalty weight is too small relative to the objective coefficients. The skill raises it and resubmits once."],
    ["Poor results on a textbook-sized problem", "Almost always a formulation bug. The skill re-derives against the catalog and runs the degeneracy check before blaming the solver."],
])}
''')

    p.section("Plugin contents", f'''
{table(["Path", "Purpose"], [
    ["<code>.claude-plugin/plugin.json</code>", "The plugin manifest: name, description, version"],
    ["<code>.mcp.json</code>", "Registers the <code>veloxq</code> MCP server, <code>https://api.veloxq.com/mcp</code>"],
    ["<code>skills/veloxq-solve/SKILL.md</code>", "The skill: workflow, submission formats, verification, troubleshooting"],
    ["<code>skills/veloxq-solve/references/qubo-formulations.md</code>", "Encoding catalog and penalty-weight guidance"],
    ["<code>skills/veloxq-solve/references/report-views.md</code>", "Format of the report's view spec and which figure suits which problem"],
    ["<code>evals/</code>", "Eval cases with prompts, graders and a headless runner that records traces like the ones on the examples page"],
])}
''')
    return p


def build_examples():
    p = Page(*PAGES[2])
    p.intro = f'''
<p class="eyebrow">veloxq-solve · recorded runs</p>
<h1>Examples</h1>
<p class="lead">Every example on this page is a real run. The prompt was given to Claude Code with the plugin loaded and nothing else, and the answer is shown as Claude wrote it. Expand <em>What Claude did</em> under an answer to see each tool call with its input and output.</p>
'''
    for gid, gtitle, gintro in GROUPS:
        body = [f"<p>{gintro}</p>"]
        if gid == "simple":
            body.append(SMALL_PROBLEM_TIP)
        order = ["03c-knapsack", "08b-graph-coloring", "04-tsp-report"]
        keys = [k for k, v in EXAMPLES.items() if v["group"] == gid]
        for s in sorted(keys, key=lambda k: order.index(k) if k in order else 99):
            if not (EXAMPLES_DIR / f"{s}.json").exists():
                continue
            extra, rewrite = "", None
            if s == "04-tsp-report":
                def rewrite(final):
                    final = re.sub(r"\*\*Report:\*\*.*?\n`[^`]*`\n", "**Report:** [report.html](examples/tsp-report.html)\n", final, flags=re.S)
                    return final
                extra = '<a class="filelink" href="examples/tsp-report.html">Open the report Claude produced ↗</a>'
            if s == "06-paper-all-to-all":
                extra = ('<h4>Runtime comparison</h4><p>The same numbers as Claude\'s table, drawn to scale. Brute force certifies the ground state; the others do not, and the paper\'s VeloxQ runs were on different hardware with tuned parameters.</p>'
                         + runtime_chart())
            if s == "07-paper-chimera":
                extra = ('<div class="bench">'
                         '<div class="tile"><div class="k">Best energy</div><div class="v">−7493</div><div class="d">4 of 88 samples</div></div>'
                         '<div class="tile"><div class="k">This run, running time</div><div class="v">0.29 s</div><div class="d">1.7 s job wall-clock</div></div>'
                         '<div class="tile"><div class="k">Paper, VeloxQ</div><div class="v">0.19 s</div><div class="d">0.054 s custom-tuned</div></div>'
                         '<div class="tile"><div class="k">Paper, BEIT</div><div class="v">115 s</div><div class="d">certified, 512 variables</div></div>'
                         '</div>')
            if s in ("06-paper-all-to-all", "07-paper-chimera"):
                body.append(example_html(s, rewrite, "", extra))
            else:
                body.append(example_html(s, rewrite, extra))
        p.section(gtitle, "\n".join(body), id=gid)
    return p


# ---------------------------------------------------------------- site

def strip_tags(s):
    s = re.sub(r"<(script|style|details)[^>]*>.*?</\1>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def search_index(pages):
    idx = []
    for pg in pages:
        idx.append({"page": pg.name, "title": pg.title, "url": pg.file, "text": strip_tags(pg.intro)[:300]})
        for sid, title, level, body in pg.sections:
            idx.append({"page": pg.name, "title": title, "url": f"{pg.file}#{sid}", "text": strip_tags(body)[:300]})
            for m in re.finditer(r'<h3 id="([^"]+)">(.*?)<a', body):
                idx.append({"page": pg.name, "title": strip_tags(m.group(2)), "url": f"{pg.file}#{m.group(1)}", "text": title})
    return idx


def sidebar(pages, current, index_json):
    nav = []
    for pg in pages:
        cur = pg.file == current.file
        nav.append(f'<div class="nav-group"><a class="nav-page{" current" if cur else ""}" href="{pg.file}">{esc(pg.name)}</a>')
        if cur:
            nav.append('<div class="nav-sub">' + "".join(f'<a href="#{sid}">{esc(t)}</a>' for sid, t, lvl, b in pg.sections if lvl == 2) + "</div>")
        nav.append("</div>")
    return f'''
<aside class="sidebar" id="sidebar">
  <a class="brand" href="index.html"><img class="brand-mark" src="static/qmz-logo.svg" alt=""><span><span class="brand-name">QuantumZ</span><br><span class="brand-sub">for Claude Code</span></span></a>
  <div class="search">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input id="search-input" type="search" placeholder="Search the docs" aria-label="Search the docs" autocomplete="off">
    <div class="search-results" id="search-results" hidden></div>
  </div>
  <nav class="nav" aria-label="Pages">{"".join(nav)}</nav>
  <div class="side-foot">Marketplace <code>quantumz</code><br><a href="{REPO}">quantumz-io/qmz-claude</a><br>Built {BUILT}</div>
</aside>
<script type="application/json" id="search-index">{index_json}</script>'''


def render(pages, pg, index_json, skeleton=True):
    i = pages.index(pg)
    prev_ = pages[i - 1] if i > 0 else None
    next_ = pages[i + 1] if i + 1 < len(pages) else None
    pager = '<nav class="pager" aria-label="Pages">'
    if prev_:
        pager += f'<a href="{prev_.file}"><span class="dir">Previous</span><span>{esc(prev_.title)}</span></a>'
    if next_:
        pager += f'<a class="next" href="{next_.file}"><span class="dir">Next</span><span>{esc(next_.title)}</span></a>'
    pager += "</nav>"
    crumbs = f'<a href="index.html">QuantumZ</a>' + ("" if pg.file == "index.html" else f'<span class="sep">/</span><span>{esc(pg.name)}</span>')
    head = (f'<title>{esc(pg.title)}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n'
            f'<link rel="icon" type="image/svg+xml" href="static/qmz-logo.svg">\n<link rel="stylesheet" href="static/wiki.css">')
    body = f'''
<div class="shell">
{sidebar(pages, pg, index_json)}
<div class="main">
  <header class="topbar"><button class="menu-btn" id="menu-btn" type="button" aria-label="Open navigation" aria-controls="sidebar">☰</button><div class="crumbs">{crumbs}</div><span class="spacer"></span><a class="repo" href="{REPO}">GitHub ↗</a></header>
  <article class="content">
{pg.body_html()}
{pager}
<p class="foot">QuantumZ for Claude Code · documentation built {BUILT} from the repository at that date.</p>
  </article>
</div>
</div>
<script src="static/wiki.js"></script>'''
    if not skeleton:
        return head + "\n" + body
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'{head}\n</head>\n<body>{body}\n</body>\n</html>\n')


def write_site(out, pages, index_json, skeleton_index=True):
    out.mkdir(parents=True, exist_ok=True)
    for pg in pages:
        skeleton = skeleton_index or pg.file != "index.html"
        (out / pg.file).write_text(render(pages, pg, index_json, skeleton=skeleton))
    shutil.copytree(STATIC_DIR, out / "static", dirs_exist_ok=True)
    (out / "examples").mkdir(exist_ok=True)
    for f in EXAMPLES_DIR.glob("*.html"):
        shutil.copy(f, out / "examples" / f.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE / "_site")
    ap.add_argument("--artifact-dir", type=Path)
    a = ap.parse_args()
    pages = [build_home(), build_plugin(), build_examples()]
    index_json = json.dumps(search_index(pages), ensure_ascii=False).replace("</", "<\\/")
    write_site(a.out, pages, index_json)
    print(f"site in {a.out}")
    if a.artifact_dir:
        write_site(a.artifact_dir, pages, index_json, skeleton_index=False)
        print(f"artifact copy in {a.artifact_dir}")


if __name__ == "__main__":
    main()
