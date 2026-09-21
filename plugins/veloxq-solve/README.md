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
| `tests/` | Tests for the bundled scripts |
