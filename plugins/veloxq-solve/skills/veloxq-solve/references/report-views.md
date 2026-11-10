# Report view spec

The `report` tool renders a solve as one HTML page. The view spec (`view`)
carries the part only you know: the solution decoded into the user's domain.

Sections: 1. Top-level keys. 2. Figure types. 3. Limits. 4. Full example.
SKILL.md step 4 says which figure suits which problem.

## 1. Top-level keys

```json
{
  "title": "Max-cut on an 8-node graph",
  "subtitle": "One sentence restating the problem.",
  "summary": [{"label": "Cut size", "value": "10 of 12 edges", "note": "optional"},
              {"label": "Tour length", "value": 42.5, "unit": "km"}],
  "checks":  [{"label": "Every node has exactly one color", "ok": true,
               "detail": "6 of 6 nodes"}],
  "notes":   ["Penalty weight P = 26, above the largest item value 25."],
  "views":   []
}
```

- `title` and `checks` are required. Everything else is optional.
- `summary` is a list of objects holding the headline numbers in the user's
  terms (cut size, total value, tour length). Put the answer first. Give a
  measured value its `unit`, which the tile prints after the value. Raw
  energies are shown elsewhere in the report and do not belong here.
- `checks` is a list with one object per hard constraint, with `ok` computed
  from the decoded sample. The header shows "Feasible" only when every check
  passes and "Infeasible" when any fails. Use `[]` for a problem without hard
  constraints, such as max-cut or number partitioning. A figure that draws a
  violation (a violated edge, task, cell or table row, a bin over capacity)
  requires a failed check, otherwise the tool refuses the view.
- `notes` is a list of short strings: the penalty weights used, an HDF5
  layout that was inferred from shape, a resubmission.
- `views` is the list of figures, in page order.

Unknown keys are an error everywhere in the spec. Build the spec by extending
the script that decoded and verified the sample. Do not type results in by
hand.

## 2. Figure types

Every figure accepts `title` and `caption`. The figures that draw measured
values (`graph` weights, `route` distances, `bins`, `schedule`, `bars`) take
an optional `unit`, printed after each value and in the CSV column headers.

### graph

```json
{"type": "graph", "title": "Partition", "edge_rule": "cut",
 "nodes": [{"id": 0, "label": "A", "group": "side A"},
           {"id": 1, "group": "side B"},
           {"id": 2, "group": "side A"}],
 "edges": [[0, 1], [1, 2, 2.5], {"u": 2, "v": 0, "weight": 1, "status": "violated"}]}
```

- `nodes`: objects with `id` and optional `label`, `group`, `x`, `y`. A bare
  id also works. A node without a group is drawn neutral. Give `x` and `y`
  on every node to fix the positions. Otherwise the layout is computed.
- `edges`: `[u, v]` or `[u, v, weight]`, each pair once. Use the object form
  to set a `status` (`plain`, `highlight`, `violated`) on one edge.
- `unit`: optional unit of the edge weights.
- `edge_rule` sets the status of the edges that do not give one:
  - `"cut"`: an edge between two different groups is highlighted.
  - `"conflict"`: an edge whose two ends share a group is violated. Ungrouped
    nodes never conflict.
- `groups`: optional list fixing the group order and so the colors. At most 8
  groups. Groups named after colors (`red`, `green`, `blue`, `orange`,
  `yellow`, `purple`, `pink`, `teal`) are drawn in those colors.
- `edge_labels`: optional legend names, keys `highlight`, `plain`, `violated`,
  `ungrouped`.

### route

```json
{"type": "route", "title": "Tour",
 "points": [{"id": 0, "label": "Depot", "x": 0, "y": 0},
            {"id": 1, "x": 4, "y": 0},
            {"id": 2, "x": 4, "y": 3}],
 "routes": [{"label": "tour", "order": [0, 2, 1], "closed": true}],
 "distances": [[0, 4, 5], [4, 0, 3], [5, 3, 0]], "unit": "km"}
```

- `points`: ids, or objects with `id` and optional `label`, `x`, `y`. With
  `x` and `y` on every point they are drawn to scale.
- `routes`: each visits `order` in sequence. `closed` defaults to true and
  returns to the first point. At most 8 routes.
- `distances`: optional square matrix in `points` order. It labels each leg
  and the route length. Without coordinates it also places the points, and
  the caption says the drawn lengths are approximate.
- `unit`: optional unit of the distances.

### bins

```json
{"type": "bins", "title": "Packing", "unit": "kg", "capacity": 30,
 "size_label": "weight",
 "bins": [{"label": "Knapsack",
           "items": [{"label": "item 0", "size": 12, "value": 24}]}],
 "unassigned": [{"label": "item 5", "size": 6, "value": 8}]}
```

One stacked bar per bin. `capacity` (top level, or per bin) draws a marker and
flags a bin that exceeds it. `unassigned` adds a muted row for items left
out, named by `unassigned_label`. `value` is optional and appears in the
tooltip and the CSV under `value_label`.

### schedule

```json
{"type": "schedule", "title": "Machines", "unit": "h",
 "rows": [{"label": "Machine 1", "tasks": [
   {"label": "J1", "start": 0, "end": 3, "group": "job 1"},
   {"label": "J2", "start": 3, "end": 7, "group": "job 2", "status": "violated"}]}]}
```

One lane per row, tasks placed on a shared time axis. `group` colors tasks
(at most 8 groups, order fixed by an optional `groups` list).
`status: "violated"` outlines a task that breaks a constraint.

### bars

```json
{"type": "bars", "title": "Expected return", "unit": "%",
 "items": [{"label": "AAPL", "value": 4.2, "highlight": true},
           {"label": "MSFT", "value": -1.1}],
 "highlight_label": "selected", "other_label": "not selected"}
```

One horizontal bar per item. When any item sets `highlight`, those bars take
the accent color and the rest are muted.

### grid

```json
{"type": "grid", "title": "Assignment",
 "rows": ["n0", "n1"], "cols": ["red", "green", "blue"],
 "cells": [[1, 0, 0], [0, 0, 1]],
 "text": [["", "", ""], ["", "", "x"]],
 "violations": [[1, 2]], "on_label": "assigned", "off_label": "not assigned"}
```

`cells` is a rectangular matrix of numbers. Values in {0, 1} draw as on and
off, values in {-1, +1} as the two spin states, and anything else on a color
scale. `null` leaves a cell blank. `text` prints a short string in each cell.
`violations` outlines cells given as `[row, column]`. On a large grid the
labels and the text move to the tooltips.

### table

```json
{"type": "table", "title": "Clauses",
 "columns": ["clause", "satisfied by"],
 "rows": [["x1 ∨ x2 ∨ ¬x3", "x1"], ["¬x2 ∨ x4", ""]],
 "status": [true, false]}
```

`status` is optional, one `true`, `false`, or `null` per row. It adds a check
or cross and marks failed rows.

## 3. Limits

| Figure | Limit |
|---|---|
| graph, route | 500 nodes, 5,000 edges or route legs, 8 groups or routes |
| bins, schedule, bars | 60 rows (the `unassigned` row counts), 2,000 bars |
| grid | 40,000 cells, 1,000 rows, 200 columns |
| table | 2,000 rows, 50 columns |
| groups | 8 distinct groups per figure, each a string or an integer |
| the whole view | 10 figures, 1,000 graph nodes in all, 20 notes, 12 summary tiles, 100 checks, 200,000 values, nesting 10 levels deep, 5 MB of JSON |
| values | text of at most 300 characters (a string or a number, never a list or null; table cells and grid text may be null), numbers below 1e300 in magnitude (route distances below 1e150) |
| the whole page | 10 MB; a grid near its cell limit with long row or column labels can reach it |

Exceeding a limit makes the tool refuse the view with a message. For an instance above the
limits keep the figure type the small instance would get and aggregate its
elements, so reports of one problem look alike at every size. A `graph`
becomes blocks of consecutive nodes when a block has a dominant group (a
partition, a selection, spins): one node per block in its majority group, an
edge between blocks that share an edge, and few enough blocks that the block
graph is not complete. When every node needs its own group (a coloring), show
instead the induced subgraph on the first few hundred nodes with their real
groups and conflicts. A `grid` is downsampled, a `route` goes through the
centroid of each block of consecutive stops, a `table` keeps the worst rows.
Say in a note how the figure was reduced.

The sections built from the job's samples and the model show energies and
coefficients in units of the coupling J, so a coefficient of 1 in the
submitted model is 1 J.

The sections built from the job's samples and the model scale down by themselves.
The coupling graph is drawn variable by variable up to 300 variables and 3,000
couplings, and the best-sample strip up to 4,096 variables. Above that both are
drawn over consecutive index blocks (at most 40 for the graph, 4,096 for the
strip), their titles carry an asterisk, and a footnote states the
simplification.

## 4. Full example

Max-cut, appended to the decode script from step 3:

```python
import json

side = result["best_sample"]                     # spins, index = node
cut = [(u, v) for u, v in edges if side[u] != side[v]]
view = {
    "title": "Max-cut on an 8-node graph",
    "summary": [{"label": "Cut size", "value": f"{len(cut)} of {len(edges)} edges"}],
    "checks": [],
    "views": [{
        "type": "graph", "title": "Partition", "edge_rule": "cut",
        "nodes": [{"id": v, "group": "side A" if s > 0 else "side B"}
                  for v, s in enumerate(side)],
        "edges": [list(e) for e in edges],
    }],
}
print(json.dumps(view))
```
