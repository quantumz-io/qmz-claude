# veloxq-solve

A Claude Code plugin and skill, for solving
optimization problems on the [VeloxQ](https://web.veloxq.com) platform, a cloud
service that samples low-energy states of Ising models (equivalently,
minimizes QUBOs) with physics-inspired solvers.

## Installation

From the [quantumz marketplace](../../README.md), run each command on its own in a Claude Code session.

- Register the marketplace:

  ```bash
  /plugin marketplace add quantumz-io/qmz-claude
  ```

- Install the plugin from this marketplace:

  ```bash
  /plugin install veloxq-solve@quantumz
  ```

## What it does

The plugin provides two things: the `veloxq` MCP server, whose tools let
Claude (or any MCP client) use the VeloxQ platform, and the `veloxq-solve`
skill, which drives those tools through a complete solve. Only the server
uses the VeloxQ SDK; the agent reaches the platform through the tools
alone.

### The MCP server

Claude Code starts the server from the plugin. Its tools are named
`mcp__plugin_veloxq-solve_veloxq__<tool>`:

| Tool | Purpose |
| --- | --- |
| `account` | Connection state, the account's balance, quota and groups |
| `connect`, `disconnect` | Connect the machine to a VeloxQ account with OAuth 2.0 |
| `list_solvers` | Solvers with their parameter schemas, costs and backends |
| `submit_job` | Upload a QUBO or Ising model (inline spec, JSON or HDF5 file) and start a job |
| `get_job` | Status, timeline, cost and parameters of a job, optionally waiting for it to end |
| `get_result` | The best samples of a finished job, with objectives recomputed locally |
| `report` | The visual report of a solve, as an HTML file or shown in the conversation |
| `list_jobs`, `job_logs`, `cancel_job` | Inspect and control jobs |

The server acts on the user's own account. On first use `connect` opens the
VeloxQ consent page in the browser and shows the link in a Claude Code
dialog; the user logs in there and approves the access, and the server
keeps the resulting tokens in
`~/.veloxq/oauth-tokens.json` (owner-only permissions) and refreshes them
by itself. No password, token or key ever passes through the conversation.
The user can end the connection at any time with `disconnect` or from the
connected applications page of their VeloxQ account.

### The skill

The skill activates when a conversation involves a combinatorial or
discrete optimization problem, such as max-cut, graph partitioning, graph
coloring, number partitioning, knapsack, TSP/routing, scheduling, maximum
independent set, portfolio selection, spin glasses, or mentions QUBO,
Ising, annealing, or VeloxQ directly. Claude then:

1. Checks with `account` that the machine is connected, and runs `connect`
   if it is not.
2. Formulates the problem as a QUBO or Ising model, using a catalog of
   standard encodings (`skills/veloxq-solve/references/qubo-formulations.md`)
   with guidance on penalty-weight selection.
3. Submits the model with `submit_job`, follows it with `get_job` and
   fetches the samples with `get_result`. Models can be given as JSON
   coefficient lists or matrices, or as HDF5 files. The energies of returned
   samples are recomputed locally instead of being trusted from the platform.
4. Decodes the best samples back into the problem's own terms and checks
   every hard constraint before reporting.
5. Presents the result in the conversation and offers a visual report. On
   yes the `report` tool renders one HTML page with figures of the decoded
   solution (the partitioned graph, the packed knapsack, the tour, the
   schedule), the constraint checks, the distribution of sample objectives,
   and the model's coupling graph in its best state. The page loads nothing
   from the network and follows the viewer's light or dark theme. In Claude
   Code the tool writes a self-contained file and Claude hands it over as a
   link: a private Claude artifact if the user chose that and the Artifact
   tool is available, otherwise a link to the local file.

For control over submissions, the tools also list recent jobs, check a
single job's status and timeline, and read its platform logs, all without
resubmitting. If a session ends before a job's result comes back,
`get_result` on the job id fetches its verified result instead of
submitting again.

## Example prompts

The following prompts all activate the skill. They are adapted from the
plugin's eval suite, which verifies each returned solution against
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

> Here's a 6-node graph: triangle edges (0,1),(1,2),(0,2) and (3,4),(4,5),(3,5), plus rungs (0,3),(1,4),(2,5). Find me a valid 3-coloring.

Knapsack, whose capacity constraint is encoded with slack variables:

> I have 8 items, weights [12, 7, 11, 8, 9, 6, 5, 14], values [24, 13, 23, 15, 16, 8, 7, 25], capacity 30. Which items should I take to maximize the total value?

A large instance:

> Generate a ferromagnetic Ising ring with 50,000 spins (J_{i,i+1} = -1 around the ring, periodic boundary conditions, no external fields) and solve it. What ground-state energy does the solver reach, and is it the true ground state?

## Requirements

- Python 3.10+ with `venv`
- Network access to the VeloxQ API and a browser for the one-time consent
- A VeloxQ account

The server's launcher creates a virtual environment at `~/.veloxq/venv`
with `veloxq-sdk` and `mcp` on first start, which can take a minute; the
server then appears under `/mcp`. The SDK must provide per-request access
tokens (`veloxq_sdk.api.core.http.use_access_token`); until a release on
PyPI has them, set `VELOXQ_SDK_SOURCE` to a checkout of the SDK before the
first start. The launcher refuses an SDK without them. An API key in `VELOX_TOKEN` or in
`~/.veloxq/config.py` (`c.VeloxQAPIConfig.token = "..."`) is used instead of
OAuth when present.

The server identifies itself to the API with an OAuth client ID metadata
document, so no client is registered by hand and there is no secret. It runs
inside Claude Code and connects as Claude Code, with the metadata document
Anthropic hosts at `https://claude.ai/oauth/claude-code-client-metadata`
(`DEFAULT_CLIENT_ID` in `mcp/veloxq_mcp/oauth.py`). That document names the
loopback redirect URI `http://127.0.0.1/callback`; the port varies per run.
The VeloxQ API accepts exactly two metadata documents, Claude's and Claude
Code's. `VELOXQ_OAUTH_CLIENT_ID` replaces the client id, for instance with a
client registered by hand on a test deployment.

## Using it on claude.ai

claude.ai cannot start a local server, so it reaches the same tools through
a hosted copy of the server, added as a custom connector. The skill is
uploaded separately and needs the connector.

### Hosting the server

```
mcp/run.sh serve --public-url https://mcp.example.com/mcp --port 8000
```

The SDK must provide `veloxq_sdk.api.core.http.use_access_token`; set
`VELOXQ_SDK_SOURCE` to a checkout or requirement that does when the venv is
first created, or `VELOXQ_VENV` to an existing venv. It serves streamable HTTP on
`127.0.0.1:8000` (`--host` to change) and expects a TLS proxy in front at
the public URL. `VELOXQ_API_URL` selects the API. Every option also reads an
environment variable, listed by `--help`.

The server is an OAuth resource server. It publishes protected resource
metadata (RFC 9728) that names the VeloxQ API as the authorization server,
accepts a bearer token when the API accepts it (`--client-id`, repeatable,
restricts this to tokens issued to the named OAuth clients), and makes every
API call of a request with the token of that request. It refuses to start when an API key or access token is configured
on the host. Compared with the local server it leaves out `connect` and
`disconnect`, refuses the file arguments `path`, `view_path` and `output`,
returns the report for display as an MCP App (`ui://veloxq/report.html`),
caps `get_job`
waits at 60 s (`--max-wait`), runs at most four tool calls per user at once,
and limits each user to 100 submissions per hour. The solver catalog,
the call and submission counters and the token cache live in the process,
so run one process.

The server cannot check a token's signature, since the API signs with a
key only it holds, so every new token costs one call to the API. Put a
per-client rate limit in the proxy in front of the server, because anyone
can send forged tokens. Limit the request body size there too, to about
10 MB: the server parses a request before any tool checks its arguments.

claude.ai registers itself: the API's authorization server metadata
advertises `client_id_metadata_document_supported` and the `none` token
endpoint authentication method, so Claude identifies itself with the client
ID metadata document Anthropic hosts at
`https://claude.ai/oauth/mcp-oauth-client-metadata`, as a public client with
PKCE, and the API pre-authorizes that document. No client is created for it
and no client secret exists.

### Adding the connector and the skill

1. In claude.ai, Settings → Connectors → Add custom connector: the public
   URL, with Advanced settings left empty. Connecting opens the VeloxQ
   consent page.
2. Build the skill archive and upload it under Settings → Capabilities →
   Skills. With code execution enabled Claude decodes and checks the samples
   in a script; the report itself is built by the server.

   ```
   cd skills && zip -r veloxq-solve.zip veloxq-solve -x '*/__pycache__/*'
   ```

## Contents

| Path | Purpose |
| --- | --- |
| `.mcp.json` | Declares the `veloxq` MCP server, started through `mcp/run.sh` |
| `mcp/veloxq_mcp/server.py` | The tools: connection, account, catalog, jobs, results |
| `mcp/veloxq_mcp/hosted.py` | The hosted server for claude.ai: streamable HTTP, bearer token check; `python -m veloxq_mcp serve` |
| `mcp/veloxq_mcp/oauth.py` | The OAuth 2.0 flow, token file and refresh; also `python -m veloxq_mcp.oauth login` |
| `mcp/veloxq_mcp/models.py` | The JSON spec and HDF5 model loaders |
| `mcp/veloxq_mcp/results.py` | Result decoding and the local energy check |
| `mcp/veloxq_mcp/report.py` | The report page and the MCP App that shows it |
| `mcp/setup_env.sh` | Creates the server's virtual environment with the SDK and the MCP package |
| `mcp/tests/` | Tests for the server against an in-memory API, and for the report |
| `skills/veloxq-solve/SKILL.md` | The skill: workflow, submission formats, verification, troubleshooting |
| `skills/veloxq-solve/references/qubo-formulations.md` | Encoding catalog for standard problems and penalty-weight guidance |
| `skills/veloxq-solve/references/report-views.md` | Format of the report's view spec and which figure suits which problem |
