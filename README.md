# QuantumZ Claude plugin marketplace

A [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
providing QuantumZ plugins.

## Installation

The marketplace is added once. Each plugin is then installed from it, with
the steps on the plugin's own page.

In Claude Desktop, open a Code session, click + in the message box and
choose Add plugins. In the Directory, click + at the top right of the
plugin list, choose Add from a repository, enter `quantumz-io/qmz-claude`
and click Sync. The marketplace appears as a Code tab listing its plugins.
The [plugin README](plugins/veloxq-solve/README.md#claude-desktop) shows
every step with screenshots.

In the Claude Code terminal, run in a session:

```bash
/plugin marketplace add quantumz-io/qmz-claude
```

## Plugins

### [veloxq-solve](plugins/veloxq-solve/README.md)

Solve QUBO/Ising optimization problems on the VeloxQ platform. Provides a
skill that formulates combinatorial optimization problems, such as max-cut,
graph coloring, knapsack, TSP, and scheduling, as QUBO or Ising models,
submits them through the [veloxq-mcp](https://mcp.veloxq.com) server, and
decodes and verifies the returned samples. See the
[plugin README](plugins/veloxq-solve/README.md) for requirements and usage.
