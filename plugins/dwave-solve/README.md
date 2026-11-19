# dwave-solve

A Claude Code plugin and skill, for solving optimization problems on
[D-Wave Leap](https://cloud.dwavesys.com/leap/), the cloud service that
samples low-energy states of Ising models (equivalently, minimizes QUBOs)
on Advantage quantum annealers and on quantum-classical hybrid solvers.

It solves the same class of problems as [veloxq-solve](../veloxq-solve/README.md)
with the same formulation catalog, so a problem can be posed to either
platform in the same words.

## Installation

The plugin installs from the [quantumz marketplace](../../README.md) in
Claude Desktop or in the Claude Code terminal. Run each command on its own
in a Claude Code session:

- Register the marketplace:

  ```bash
  /plugin marketplace add quantumz-io/qmz-claude
  ```

- Install the plugin from this marketplace:

  ```bash
  /plugin install dwave-solve@quantumz
  ```

In Claude Desktop, add the marketplace from the Directory as described in
the [marketplace README](../../README.md#installation) and click + on
D-Wave Solve. No connector is involved. Start a new session so the skill
loads.

## The API token

There is no D-Wave MCP server, so nothing is connected under `/mcp`. The
skill runs the [Ocean SDK](https://docs.dwavequantum.com/en/latest/ocean/)
on your machine and authenticates with your Leap Solver API token, which
the Leap dashboard shows under API Token.

The first time a solve needs the token, Claude asks for it in the
conversation and asks at the same time whether to store it for re-use:

- **Stored**: the token goes into Ocean's configuration file, on Linux
  `~/.config/dwave/dwave.conf` and on macOS
  `~/Library/Application Support/dwave/dwave.conf` (`dwave config ls -m`
  lists the paths Ocean looks at), as `token = ...` in the profile Ocean
  reads. Other lines of the file are kept, and the file is readable by your
  user only. Every Ocean tool reads it, so `dwave ping` and `dwave solvers`
  work too. Claude tells you the path it wrote. To remove the token later,
  delete that line, or ask Claude, which runs the bundled
  `store_token.py --forget`.
- **Session only**: the token is passed to each script as the environment
  variable `DWAVE_API_TOKEN` and nothing is written to disk. Claude asks
  again in the next session.

In both cases the pasted token is part of the conversation and of the
session transcript Claude Code keeps under `~/.claude`. To keep it out of
the conversation entirely, run `dwave auth login`, which signs in through
the browser, and then `dwave config create --auto-token`, which writes the
token to the same configuration file; Claude then finds it there. A token
already present in `DWAVE_API_TOKEN` or in `dwave.conf` is used without
asking. The scripts never read a `dwave.conf` from the working directory,
so a cloned repository cannot redirect the token to another endpoint.

Every solve consumes solver-access time from your Leap account. Leap does
not expose the remaining time through the API; the dashboard shows it.

## What it does

The skill activates when a conversation involves a combinatorial or
discrete optimization problem, such as max-cut, graph partitioning, graph
coloring, number partitioning, knapsack, TSP/routing, scheduling, maximum
independent set, portfolio selection, spin glasses, or mentions QUBO,
Ising, annealing, D-Wave or Leap directly. Claude then:

1. Checks for a working token with `check_access.py`, and asks for one
   when there is none.
2. Formulates the problem as a QUBO or Ising model from the catalog in
   `skills/dwave-solve/references/qubo-formulations.md`.
3. Submits one job with `solve.py`: to an Advantage QPU for models up to
   100 variables, to a Leap hybrid solver above that, or to the solver you
   name. Sample energies are recomputed locally from the model before the
   result is accepted.
4. Decodes the best samples back into the problem's own terms and checks
   every hard constraint.
5. Presents the answer, the solution, the constraint checks, the sample
   counts, and the solver line with the problem id you can look up on the
   Leap dashboard.

Resubmissions are capped: after two in one conversation Claude asks before
spending more solver time.

Local solving is out of scope for the skill, including Ocean's own
simulated annealing and exact solvers: local computation only decodes and
checks what the service returned.

## Example prompts

The following prompts all activate the skill.

Number partitioning:

> I have these server loads [23, 45, 12, 67, 34, 89, 21, 43, 55, 18] and want to split them across two racks so the total load is as balanced as possible. Use D-Wave.

Max-cut:

> Solve max-cut on this 8-node graph on the quantum annealer: edges = [(0,1),(0,3),(1,2),(1,4),(2,5),(3,4),(3,6),(4,5),(4,7),(5,7),(6,7),(2,7)]. Which nodes go on each side, and how many edges are cut?

A QUBO given directly as a matrix:

> I need to minimize x^T Q x over binary x, with symmetric Q = [[-3,2,0,1,0,0],[2,-5,2,0,1,0],[0,2,-2,3,0,0],[1,0,3,-6,2,1],[0,1,0,2,-4,2],[0,0,0,1,2,-3]]. Give me the optimal assignment and objective value.

Graph coloring:

> Here's a 6-node graph: triangle edges (0,1),(1,2),(0,2) and (3,4),(4,5),(3,5), plus rungs (0,3),(1,4),(2,5). Find me a valid 3-coloring.

Knapsack, whose capacity constraint is encoded with slack variables:

> I have 8 items, weights [12, 7, 11, 8, 9, 6, 5, 14], values [24, 13, 23, 15, 16, 8, 7, 25], capacity 30. Which items should I take to maximize the total value?

A larger instance, which goes to a hybrid solver:

> Generate a ferromagnetic Ising ring with 5,000 spins (J_{i,i+1} = -1 around the ring, periodic boundary conditions, no external fields) and solve it on Leap. What ground-state energy does the solver reach, and is it the true ground state?

## Requirements

- A [D-Wave Leap](https://cloud.dwavesys.com/leap/) account with solver
  access and its Solver API token
- Network access to `cloud.dwavesys.com`
- Python 3.10 or later with [uv](https://docs.astral.sh/uv/), which installs
  the Ocean packages the scripts declare on first use; or a Python with
  [dwave-ocean-sdk](https://pypi.org/project/dwave-ocean-sdk/) installed

## Contents

| Path | Purpose |
| --- | --- |
| `skills/dwave-solve/SKILL.md` | The skill: token handling, workflow, submission format, verification, troubleshooting |
| `skills/dwave-solve/scripts/check_access.py` | Reports the token source and the solvers it reaches |
| `skills/dwave-solve/scripts/ocean_config.py` | Where Ocean reads its configuration, shared by the other scripts |
| `skills/dwave-solve/scripts/store_token.py` | Stores the token in `dwave.conf`, or removes it with `--forget` |
| `skills/dwave-solve/scripts/solve.py` | Builds the model from `problem.json`, submits it, writes the verified `result.json` |
| `skills/dwave-solve/references/qubo-formulations.md` | Encoding catalog for standard problems and penalty-weight guidance |
