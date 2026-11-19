---
name: dwave-solve
description: >-
  Solve optimization problems on D-Wave Leap by formulating them as
  QUBO/Ising models and submitting them to a quantum annealer or a Leap
  hybrid solver with the Ocean SDK. Use this skill whenever the user asks to
  solve a combinatorial or discrete optimization problem — max-cut, graph
  partitioning, graph coloring, number partitioning, knapsack, TSP/routing,
  scheduling, maximum independent set, portfolio selection, spin glasses —
  or mentions QUBO, Ising, annealing, quadratic binary optimization, D-Wave,
  Leap, Advantage or Ocean, even when they do not name the platform or the
  model class explicitly. Small instances are no exception: use the skill
  even when the problem looks solvable by hand or brute force.
compatibility: >-
  Requires Claude Code with a shell, Python 3.10+ with uv or the
  dwave-ocean-sdk package, network access to cloud.dwavesys.com, and a
  D-Wave Leap account with a Solver API token. There is no D-Wave MCP
  server: the skill runs Ocean on the user's machine and asks the user for
  the token once.
---

# Solving optimization problems on D-Wave Leap

D-Wave Leap is a cloud service that samples low-energy states of Ising
models (equivalently, minimizes QUBOs) on quantum annealers (the Advantage
QPUs) and on quantum-classical hybrid solvers. There is no MCP server for
it. This skill reaches Leap through three scripts bundled under
`scripts/`, which use the Ocean SDK and run on the user's machine. The
Leap API token is the only credential, and every solve consumes solver
access time from the user's Leap account.

| Script | Purpose |
|---|---|
| `check_access.py` | Whether a token is configured, where it comes from, and which solvers it reaches |
| `store_token.py` | Store the token in Ocean's configuration file, or remove it with `--forget` |
| `solve.py` | Build the model from `problem.json`, submit it, and write the verified `result.json` |

Run every script as `uv run --no-project --no-config --script <path>`,
which installs the Ocean packages on first use. Without `uv`, run them
with a Python that has `dwave-ocean-sdk` installed (`pip install
dwave-ocean-sdk`). Use the scripts' absolute path under this skill's
`scripts/` directory. The scripts read Ocean's configuration from the
user's and the system's `dwave.conf` and from `DWAVE_*` variables, never
from a `dwave.conf` in the working directory.

**The platform is the solver.** Solve every in-scope problem on Leap,
including instances small enough to brute-force, solve by dynamic
programming, or eyeball — do not substitute a local solve because the
problem "doesn't need a solver". Local computation has exactly one role:
checking the *returned* samples (step 3) — decoding them, recomputing their
objective, and testing feasibility. Solving the problem by any other means —
brute-force enumeration, dynamic programming, greedy, an ILP, Ocean's
simulated annealing or exact solvers — is a local solve even when it is
framed as "verification" or "computing the known optimum to compare
against", and is out of scope.

The workflow is always the same steps; do them in order because each one
fails faster and cheaper than the next.

0. Check that a Leap API token is available and works
1. Formulate the problem as a QUBO or Ising model
2. Submit and fetch the result
3. Decode the solution back into the user's domain and verify it
4. Present the result

## Step 0: Check access

Run `check_access.py` before any formulation work. It prints a JSON report
and exits with:

- **0**: proceed. The report names the solvers the account reaches (QPUs
  with their qubit count, hybrid solvers with their variable limit). Leap
  does not expose the remaining solver-access time through the API; the
  user sees it on their Leap dashboard.
- **1**, no token: ask the user for one, as described below.
- **2**, token rejected: tell the user the token was refused and ask for a
  new one. Storing the new one replaces the refused one.
- **3**, anything else: the report's `error` says what failed (no network,
  a malformed `dwave.conf`, an endpoint that is not Leap). Relay it and
  stop. Do not ask for a token: one is not the problem.
- The script fails to start: `uv` is missing and `dwave-ocean-sdk` is not
  installed. Tell the user which to install and stop.

### Asking for the token

The token is a Solver API token from the user's Leap dashboard at
https://cloud.dwavesys.com/leap/. Ask for it in plain text as the last
lines of a message, then end the turn. Ask both questions at once:

> I need your D-Wave Leap API token to submit this problem. You can copy it
> from the Leap dashboard (https://cloud.dwavesys.com/leap/).
>
> Do you want me to store it for re-use? If yes, I save it in Ocean's
> configuration file, on Linux `~/.config/dwave/dwave.conf` and on macOS
> `~/Library/Application Support/dwave/dwave.conf`, readable by your user
> only, where the `dwave` command-line tools also read it, and I show you
> the exact path once it is written. If no, I use it for this session only
> and write nothing to disk. Either way the token passes through this
> conversation and its transcript. To keep it out of the conversation, run
> `dwave auth login` and then `dwave config create --auto-token` yourself
> and tell me when done.

Then, with the answer:

- **Store it**: run `store_token.py` with the token on standard input,
  `printf '%s' '<token>' | uv run --no-project --no-config --script .../store_token.py`.
  It prints the path and profile it wrote. Tell the user that path, and
  that `store_token.py --forget` or deleting the `token` line removes it.
- **Session only**: prefix every later script call with
  `DWAVE_API_TOKEN='<token>'`.

Put the token in a command only if it consists of letters, digits, dots,
underscores and dashes; anything else is not a token, so ask again instead
of quoting it. In both cases run `check_access.py` again (with the prefix
when the token is session-only) and continue only on exit 0. Never repeat
the token in your answers and never write it into `problem.json`, a script,
or any other file.

## Step 1: Formulate as QUBO or Ising

Leap accepts:

- **Ising**: minimize `E(s) = Σ h_i s_i + Σ J_ij s_i s_j` over spins `s_i ∈ {-1,+1}`
- **QUBO**: minimize `x^T Q x` over binaries `x_i ∈ {0,1}`

Ocean converts between the two itself, so formulate in whichever form is
natural and don't convert by hand. Objectives with cubic or higher terms
(SAT clauses, three-way interactions) are still solvable: quadratize them
with auxiliary variables — see the "Higher-order terms" section of the
reference.

If the user already provides `Q` or `h`/`J`, use them as-is. Otherwise the
problem must be *encoded*: decision → binary variables, objective + hard
constraints → quadratic penalty terms. This encoding is where correctness
lives — a wrong penalty weight silently returns infeasible "solutions", and a
hand-derived expansion with a factor-of-2 convention slip returns degenerate
garbage. When the catalog has a recipe for the problem, use it verbatim
instead of re-deriving the algebra.
**Read `references/qubo-formulations.md`** for the standard encodings
(max-cut, number partitioning, coloring, knapsack, TSP, independent set,
vertex cover) and for how to pick penalty weights.

Always map problem variables to integer indices `0..n-1` yourself and keep the
mapping — the result holds states as plain arrays, so you need the mapping
to decode.

## Step 2: Submit and fetch

Write the model as a spec in a file `problem.json`, so the decode script of
step 3 reads the same model. Indices are integers `0..n-1`:

```json
{"type": "qubo",  "quadratic": [[0, 1, -2.0], [1, 2, 1.5]], "linear": [1.0, 0.0, -1.0]}
{"type": "ising", "quadratic": [[i, j, Jij], ...],          "linear": [h0, h1, ...]}
```

`quadratic` is a list of `[i, j, value]` triples. Each unordered pair
appears once; a repeated pair is an error, not a sum. A QUBO may put
linear terms on the diagonal as `[i, i, value]`, but not both there and in
`linear`. An Ising model has no diagonal: fields go in `linear`. Use
`"matrix"` instead of `quadratic` for a dense square matrix, whose
off-diagonal entries `Q_ij` and `Q_ji` are summed into one coupling and
whose diagonal holds the linear terms. `linear` is a list or an
`{"i": value}` map, and may be omitted. Build the spec in a script for
anything beyond a handful of coefficients.

Then run `solve.py problem.json` with a Bash timeout of 600000 ms: QPU
queues and hybrid runs can take minutes, and a run killed by the default
timeout has already been charged. If the command does time out, ask the
user to check the Leap dashboard for the job before submitting again.
The script builds the model, picks a solver, submits one job, waits for
it, recomputes every returned sample's energy locally with dimod against
the spec, and writes `result.json` only when that recomputation agrees
with the energies the sampler reported (the check matters for the hybrid
solvers, whose energies come from the service). A stale `result.json` is
removed before the submission, so a failed run leaves none behind. The
result holds `best_sample` (indexed by variable), `best_objective`,
`num_samples`, `num_samples_at_best`, `num_distinct_samples_at_best`,
`top` (the five best distinct samples with their occurrence counts),
`energy_check`, the solver name, Leap's `problem_id` and the timing.

Solver choice, `--sampler`:

- `auto` (the default): the QPU up to 100 variables, a hybrid solver above.
- `qpu`: `DWaveSampler` behind `EmbeddingComposite`. Each logical variable
  becomes a chain of physical qubits; the result reports the embedding size
  and the chain-break fraction. Returns `--num-reads` samples (default
  1000), which gives the occurrence counts the answer reports.
- `hybrid`: `LeapHybridSampler`, for models up to a million variables, dense
  or sparse, with no embedding. It returns one sample, so occurrence counts
  carry no information. `--time-limit` in seconds may raise the solver's
  minimum (about 3 s for small models), never lower it.

Leave the parameters at their defaults unless the user asks otherwise. Each
run is a real job charged against the account's solver-access time: budget
one submission for small problems, and only resubmit deliberately. When
the QPU reports a chain-break fraction above about 0.1 on the best
samples, or an embedding failure, say so and resubmit once with
`--chain-strength` at about twice the largest coupling magnitude, or with
`--sampler hybrid`. Never submit in a loop, and after two resubmissions in
one conversation, for whatever reasons, stop and ask the user before
spending more solver time.

## Step 3: Decode and verify

Never hand the user raw spins. Decode and check in a script and keep it.
Using your index mapping from step 1, `problem.json` and `result.json`:

1. Decode the best sample(s) into the original domain (which items are in the
   knapsack, the tour order, the color of each node...).
2. **Check every hard constraint against the decoded solution.** Penalty
   encodings make constraint violations *unlikely*, not impossible. If a
   constraint is violated, say so, raise the penalty weight (see the
   reference), and resubmit once.
3. Report the objective in the user's terms (cut size, sum difference, tour
   length), not the raw Ising energy. Mention the energy only as supporting
   detail.
4. State how many of the returned samples achieved the best value — near-ties
   are useful signal for the user. For a hybrid solve there is one sample;
   say so instead.
5. Degeneracy sanity check: if several returned samples share one energy but
   decode to *different* user-facing objectives (different imbalances, cut
   sizes...), the encoding lost information — a dropped linear term or wrong
   vartype. Stop and fix the formulation; do not present the nicest-looking
   decode as the answer.

These five checks are the whole of verification, and they are sufficient
evidence to report the result as done. Do not additionally re-solve the
problem by another method (brute force, DP, exact enumeration, Ocean's
`ExactSolver` or `SimulatedAnnealingSampler`) to confirm optimality: the
solver returns its best samples, and points 3–5 establish that the reported
solution is feasible and its objective is computed correctly.

## Step 4: Present the result

Give the full answer in the conversation, in this order and in the user's
terms:

1. One sentence with the answer: the objective value and whether the solution
   is feasible.
2. The solution as a compact table or list (which nodes on which side, which
   items taken, the tour order). Summarize a solution too long to list.
3. The constraint checks, one line each, marked ✓ or ✗.
4. How many samples reached the best value, with the raw energy as supporting
   detail.
5. One line on the solver: its name, the QPU access time or hybrid run
   time, and Leap's problem id, which the user can look up on the Leap
   dashboard together with the remaining solver-access time.

If the user asked for a figure, draw it from the decode script with
matplotlib and save it next to `result.json`. Do not offer figures the user
did not ask for.

## Troubleshooting

- **"no embedding found"**: the model is too dense or too large for the
  QPU. Resubmit with `--sampler hybrid`.
- **A rejected `--time-limit`**: the hybrid solver's minimum grows with the
  model; omit the flag to let the sampler pick it.
- **"No Leap API token" from `solve.py`**: the `DWAVE_API_TOKEN` prefix
  was left off a session-only token.
- **An HTTP error about the project or the account**: the account's
  solver access is exhausted or the project is inactive. Relay the message
  and point to the Leap dashboard.
- **Energy check failed**: the solver's energies and the local recomputation
  disagree. No result was written. Check that `problem.json` is the
  submitted spec, then report the failure with the problem id the message
  names.
- **Solution violates constraints**: penalty too small relative to objective
  coefficients — see the penalty-weight section of the reference.
- **Suboptimal or degenerate results on a textbook-sized problem**: almost
  always a formulation bug, not a solver limitation. Re-derive against the
  catalog recipe and run the degeneracy check before blaming the annealer —
  and never present a reformulation as the fix without stating what actually
  changed (two objectives that differ by a constant factor are the same
  objective; if the second one worked, the first one's coefficients were
  wrong).
- **Huge coefficient ranges** (e.g. penalties 10^6× objective): the QPU
  rescales every model to its hardware range, so the objective becomes
  noise; rescale or tighten the penalty before blaming the hardware.
