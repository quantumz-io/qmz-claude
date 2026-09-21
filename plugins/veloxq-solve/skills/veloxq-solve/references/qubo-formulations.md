# QUBO/Ising formulations

How to encode a discrete optimization problem for VeloxQ. The canonical survey
is Lucas, "Ising formulations of many NP problems" (Front. Physics 2, 2014);
the platform's own benchmark record is "VeloxQ: A Fast and Efficient QUBO
Solver" (arXiv:2501.19221). This file covers the encodings you will actually
need, plus the penalty-weight craft that papers gloss over.

## Contents

1. Definitions and conversions
2. The general recipe
3. Choosing penalty weights
4. Higher-order terms (quadratization)
5. Catalog: max-cut, number partitioning, max independent set / vertex cover,
   graph coloring, knapsack, TSP, MAX-3-SAT
6. Decoding and verification checklist

## 1. Definitions and conversions

- **QUBO**: minimize `x^T Q x` with `x_i ∈ {0,1}`. Diagonal `Q_ii` are linear
  terms (since `x_i^2 = x_i`).
- **Ising**: minimize `Σ_i h_i s_i + Σ_{i<j} J_ij s_i s_j` with `s_i ∈ {-1,+1}`.
- Conversion: `x = (s + 1)/2`. Don't do this by hand — build a `dimod`
  `BinaryQuadraticModel` in the natural vartype and the SDK converts on
  submission. But remember which vartype *you* formulated in when decoding.

Maximization: negate the objective. VeloxQ minimizes.

## 2. The general recipe

1. **Variables.** Pick binary decision variables. For "assign item v one of k
   labels", use one-hot: `x_{v,c} = 1` iff v gets label c (that's `n·k`
   variables, flattened to indices `v*k + c`).
2. **Objective.** Write the quantity to minimize as a polynomial in the
   variables. It must come out at most quadratic; if a term is cubic or
   higher, introduce auxiliary variables (rare in practice for the problems
   below).
3. **Constraints.** Convert each hard constraint into a quadratic penalty that
   is zero when satisfied and ≥ some positive gap when violated:
   - Equality `Σ a_i x_i = b`  →  `P · (Σ a_i x_i − b)²`
   - "Exactly one of the set S" → `P · (Σ_{i∈S} x_i − 1)²`
   - "At most one" (no-conflict) → `P · Σ_{i<j∈S} x_i x_j`
   - Inequality `Σ a_i x_i ≤ b` → add slack variables encoding the unused
     capacity (binary expansion: `slack = Σ_k 2^k y_k`, with enough bits to
     reach b), then penalize `(Σ a_i x_i + slack − b)²`.
4. **Total:** `H = objective + Σ P_c · penalty_c`.

## 3. Choosing penalty weights

This is the step that decides whether you get a feasible answer.

- **Lower bound:** P must exceed the largest objective improvement obtainable
  by violating the constraint by one unit. A safe generic choice is
  `P = 1 + Σ |objective coefficients touching the constrained variables|` —
  crude but sufficient for small problems.
- **Upper bound:** don't set P orders of magnitude beyond that. Solvers
  resolve energies with finite precision; `P = 10^6 ×` the objective scale
  turns the objective into rounding noise and you get *a* feasible solution
  but not a good one.
- Start with P ≈ 2–10× the largest objective coefficient (times the constraint
  degree). If the returned best sample violates a constraint, multiply that
  constraint's P by ~5 and resubmit once. If it still violates, the
  formulation (not the weight) is likely wrong — recheck it before burning
  more jobs.
- When two encodings are available, prefer the sparser one: solver runtime
  scales with the number of nonzero couplings, not with variable count or
  graph structure. A squared-sum penalty over k variables creates k(k−1)/2
  couplings — for large groups consider whether a local (per-edge, per-pair)
  encoding exists before reaching for `(Σ…)²`. Dense is not *wrong* (there is
  no embedding overhead, unlike on annealers); it is just the thing that
  costs.

## 4. Higher-order terms (quadratization)

The platform accepts only quadratic models. If the natural formulation has
cubic or higher terms (SAT clauses, three-way interaction costs), reduce the
order with auxiliary variables *in the binary form*, then submit the resulting
QUBO as usual.

**Rosenberg reduction** (binary): replace a product `x_i x_j` everywhere it
occurs by a new variable `y`, and add the penalty

    P · (x_i x_j − 2 x_i y − 2 x_j y + 3 y)

which is 0 iff `y = x_i x_j` and ≥ P otherwise. A cubic term
`c · x_i x_j x_k` becomes `c · y x_k` plus that penalty. Choose
`P > Σ |c|` over the terms that use `y`. One auxiliary variable handles *all*
terms containing the pair `(i, j)` — reuse it; don't mint one per term.

Bookkeeping rules:

- Reduce in the binary vartype. If you formulated in spins, convert with
  `x = (s+1)/2` first (dimod: `bqm.change_vartype('BINARY')` — but for
  higher-order use `dimod.BinaryPolynomial` + `dimod.make_quadratic`, which
  implements exactly this reduction and picks P for you).
- Auxiliary variables get indices after the real ones; record which is which,
  decode only the real ones, and *verify* `y = x_i x_j` holds in the returned
  sample — a violated reduction penalty invalidates the whole sample.
- Each third-order term costs ~1 auxiliary variable. That growth is normal
  and stays tractable at very large scale (the platform's MAX-3-SAT
  benchmarks run at ~10⁸ variables after reduction).

## 5. Catalog

Indices below are the flattened integer indices you submit; keep the mapping.

### Max-cut
Given graph G=(V,E) with weights `w_uv`, maximize the cut. Pure Ising, no
penalties: `H = Σ_{(u,v)∈E} w_uv · s_u s_v`. An edge in the cut contributes
`−w`; cut size = `(W_total − E_min)/2` where `E_min` is the found energy
(unweighted: `w = 1`).

### Number partitioning
Split numbers `a_1..a_n` into two groups with equal (or closest) sums:
`H = (Σ a_i s_i)²`, i.e. `J_ij = 2 a_i a_j` (i<j), constant `Σ a_i²` dropped.
`s_i = ±1` is the group of item i. Final imbalance = `√(E + Σ a_i²)`... simpler:
recompute `|Σ_{group1} − Σ_{group2}| = |Σ a_i s_i|` from the decoded sample.
H = 0 ⟺ perfect partition.

Formulate this in spins, not binaries — the spin form above is three symbols
long and has no derivation to get wrong. Re-deriving the binary expansion of
`(2Σ a_i x_i − Σa)²` by hand has two verified failure modes, both of which
produce massively degenerate garbage (many tied "best" samples decoding to
wildly different imbalances, typically all-in-one-rack and single-item
splits):

- dropping the linear coefficients (`4a_i² − 4a_i Σa`), which are genuine in
  binary form even though the corresponding spin terms were constant;
- halving the cross terms: the expansion gives `8 a_i a_j x_i x_j` *as a
  once-per-pair coefficient*. Writing `4 a_i a_j` is correct only for a
  symmetric-matrix convention where each pair is counted twice — submit.py's
  `"quadratic"` triples count each pair once. With the halved value, all-ones
  and every all-but-one state tie exactly.

### Maximum independent set / minimum vertex cover
MIS on G: `H = −Σ_v x_v + P Σ_{(u,v)∈E} x_u x_v`, with `P ≥ 2` for unweighted
(any P > 1 works in theory; P = 2 is robust). Vertex cover is the complement:
`x_cover = 1 − x_MIS`.

### Graph coloring (k colors)
Variables `x_{v,c}`, index `v*k + c`.
`H = P₁ Σ_v (Σ_c x_{v,c} − 1)² + P₂ Σ_{(u,v)∈E} Σ_c x_{u,c} x_{v,c}`.
Feasibility problem: any H = 0 sample is a valid coloring. `P₁ = P₂ = 1`
suffices structurally; use larger equal weights only if mixing with an
objective.

### Knapsack
Items with values `v_i`, weights `w_i`, capacity W. Binary-expansion slack
`y_k`, `M = ⌊log₂ W⌋ + 1` bits (cap the top bit so slack ≤ W):
`H = −Σ v_i x_i + P (Σ w_i x_i + Σ 2^k y_k − W)²` with `P > max v_i`.
Note the equality trick forces `Σ w_i x_i + slack = W` exactly, so slack bits
must be able to express every residual value — verify decoded weight ≤ W and
if the best sample is infeasible, check the slack range before touching P.

### TSP (n cities)
n² variables `x_{v,t}` ("city v visited at step t", index `v*n + t`):
`H = P Σ_v (Σ_t x_{v,t} − 1)² + P Σ_t (Σ_v x_{v,t} − 1)²
   + Σ_{u≠v} d_uv Σ_t x_{u,t} x_{v,t+1}`  (t+1 mod n).
`P > n · max d_uv` is a safe start. Decode by reading the position of the 1
in each time-step column; verify it is a permutation before reporting a tour.

### MAX-3-SAT
Clauses `(l₁ ∨ l₂ ∨ l₃)` over binary `x_i`, literal `l = x` or `1−x`.
A clause is violated iff all three literals are false, so minimize
`Σ_clauses ẑ₁ ẑ₂ ẑ₃` where `ẑ` is the literal's *false*-indicator (`1−x` for a
positive literal, `x` for a negated one). Each clause contributes one cubic
term — expand the product and quadratize per section 4 (aux variables shared
between clauses that repeat a variable pair). Energy 0 ⟺ fully satisfiable;
otherwise the energy counts unsatisfied clauses.

## 6. Decoding and verification checklist

After every job, against the *decoded* solution:

1. One-hot groups: exactly one 1 per group? (coloring, TSP)
2. Conflict constraints: zero violated edges/pairs? (MIS, coloring)
3. Capacity/equality constraints: recompute the sums with the original data.
4. Objective: recompute in original terms (cut weight, tour length, value sum)
   directly from the user's data — never derive it from the Ising energy by
   arithmetic on offsets; that path is where sign and constant errors hide.
5. Report violations honestly. An infeasible "best" sample is a formulation
   or penalty-weight problem, and saying so is more useful than a polished
   wrong answer.
