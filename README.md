# QuantumZ Claude plugin marketplace

A [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
providing QuantumZ plugins.

## Installation

Add the marketplace, then install plugins from it:

```
/plugin marketplace add quantumz-io/qmz-claude
/plugin install veloxq-solve@quantumz
```

## Plugins

### [veloxq-solve](plugins/veloxq-solve/README.md)

Solve QUBO/Ising optimization problems on the VeloxQ platform. Provides a
skill that formulates combinatorial optimization problems, such as max-cut,
graph coloring, knapsack, TSP, and scheduling, as QUBO or Ising models,
submits them through the plugin's VeloxQ MCP server, and decodes and
verifies the returned samples. See the
[plugin README](plugins/veloxq-solve/README.md) for requirements and usage.
