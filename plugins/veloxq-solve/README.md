# veloxq-solve

A Claude Code plugin for solving optimization problems on the
[VeloxQ](https://veloxq.com) platform, a cloud service that samples
low-energy states of Ising models (equivalently, minimizes QUBOs) with
physics-inspired solvers.

## Installation

From the [quantumz marketplace](../../README.md):

```
/plugin marketplace add quantumz-io/qmz-claude
/plugin install veloxq-solve@quantumz
```

## What it does

The plugin provides the `veloxq-solve` skill. It activates when a
conversation involves a combinatorial or discrete optimization problem,
such as max-cut, graph partitioning, graph coloring, number partitioning,
knapsack, TSP/routing, scheduling, maximum independent set, portfolio
selection, spin glasses, or mentions QUBO, Ising, annealing, or VeloxQ
directly. Claude then:

1. Checks that the SDK environment and credentials are in place.
2. Formulates the problem as a QUBO or Ising model, using a catalog of
   standard encodings (`skills/veloxq-solve/references/qubo-formulations.md`)
   with guidance on penalty-weight selection.
3. Submits the model through the `veloxq_sdk` Python client and waits for
   the result. Models can be given as JSON coefficient lists or matrices, or
   as HDF5 files. The energies of returned samples are recomputed locally
   instead of being trusted from the platform.
4. Decodes the best samples back into the problem's own terms and checks
   every hard constraint before reporting.

For control over submissions, the plugin can also list recent jobs, check a
single job's status and timeline, and read its platform logs, all without
resubmitting. If a session ends before a job's result comes back, the
submission runner can resume the job by id and fetch its verified result
instead of submitting again.

## Example prompts

The following prompts all activate the skill. They are adapted from the
plugin's benchmark suite, which verifies each returned solution against
the known optimum.

Number partitioning:

> I have these server loads [23, 45, 12, 67, 34, 89, 21, 43, 55, 18] and want to split them across two racks so the total load is as balanced as possible.

Max-cut:

> Solve max-cut on this 8-node graph: edges = [(0,1),(0,3),(1,2),(1,4),(2,5),(3,4),(3,6),(4,5),(4,7),(5,7),(6,7),(2,7)]. Which nodes go on each side, and how many edges are cut?

A QUBO given directly as a matrix:

> I need to minimize x^T Q x over binary x, with symmetric Q = [[-3,2,0,1,0,0],[2,-5,2,0,1,0],[0,2,-2,3,0,0],[1,0,3,-6,2,1],[0,1,0,2,-4,2],[0,0,0,1,2,-3]]. Give me the optimal assignment and objective value.

Max-3-SAT, which needs quadratization before submission:

>I have this 3-SAT instance over variables x1..x6 and want an assignment satisfying as many clauses as possible: (x1 v x2 v x3), (!x2 v x4 v !x5), (!x1 v !x6 v x5), (x3 v x5 v x6), (!x3 v !x4 v !x5), (x2 v x4 v x6), (!x1 v x4 v !x2), (x1 v !x5 v x6), (!x6 v !x2 v x3).

Graph coloring:

> Here's a 6-node graph: triangle edges (0,1),(1,2),(0,2) and (3,4),(4,5),(3,5), plus rungs (0,3),(1,4),(2,5). I think 3 colors should be enough. Find me a valid 3-coloring.

Knapsack, whose capacity constraint is encoded with slack variables:

> I have 8 items, weights [12, 7, 11, 8, 9, 6, 5, 14], values [24, 13, 23, 15, 16, 8, 7, 25], capacity 30. Which items should I take to maximize the total value?

A large instance:

> Generate a ferromagnetic Ising ring with 50,000 spins (J_{i,i+1} = -1 around the ring, periodic boundary conditions, no external fields) and solve it. What ground-state energy does the solver reach, and is it the true ground state?

## Requirements

- Python 3.9+
- Network access to the VeloxQ API
- A VeloxQ API token

The skill installs the SDK into a dedicated virtual environment at
`~/.veloxq/venv` on first use. Credentials live in `~/.veloxq/config.py`.
The bundled `configure_veloxq.py` script creates this file with a masked
prompt so the token never enters the conversation.

## Contents

| Path | Purpose |
| --- | --- |
| `skills/veloxq-solve/SKILL.md` | The skill: workflow, submission formats, verification, troubleshooting |
| `skills/veloxq-solve/references/qubo-formulations.md` | Encoding catalog for standard problems and penalty-weight guidance |
| `skills/veloxq-solve/scripts/setup_env.sh` | Creates the SDK virtual environment |
| `skills/veloxq-solve/scripts/configure_veloxq.py` | Interactive credential setup (masked prompt, file mode 600) |
| `skills/veloxq-solve/scripts/submit.py` | Submission runner: JSON/HDF5 input, local energy recomputation |
| `skills/veloxq-solve/scripts/jobs.py` | Job inspection: list recent jobs, check status, read logs |
| `tests/` | Tests for the bundled scripts |
