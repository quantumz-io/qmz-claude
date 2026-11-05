# QuantumZ Claude plugin marketplace

A [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
providing QuantumZ plugins.

## Installation

Run each command on its own in a Claude Code session.

- Register the marketplace:

  ```bash
  /plugin marketplace add quantumz-io/qmz-claude
  ```

- Install the plugin from this marketplace:

  ```bash
  /plugin install veloxq-solve@quantumz
  ```

## Plugins

### [veloxq-solve](plugins/veloxq-solve/README.md)

Solve QUBO/Ising optimization problems on the VeloxQ platform. Provides a
skill that formulates combinatorial optimization problems, such as max-cut,
graph coloring, knapsack, TSP, and scheduling, as QUBO or Ising models,
submits them through the VeloxQ MCP server of the VeloxQ API, and decodes and
verifies the returned samples. See the
[plugin README](plugins/veloxq-solve/README.md) for requirements and usage.
