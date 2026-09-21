#!/usr/bin/env python3
"""Submit a QUBO/Ising model to the VeloxQ platform and report verified results.

Input is a JSON spec with integer variables 0..n-1:

    {"type": "qubo",  "quadratic": [[i, j, v], ...], "linear": [v0, ...] | {"i": v}}
    {"type": "ising", "quadratic": ..., "linear": ...}

"matrix" (dense square) may replace "quadratic"; "linear" is optional.

An .h5/.hdf5 input is also accepted:
- a file already in the platform's native layout (group "Ising" with biases +
  couplings) is uploaded as-is;
- a foreign file is converted when its layout is unambiguous — a 1-D linear
  dataset named biases/bias/h/linear/fields plus a dense square quadratic
  dataset named couplings/coupling/J/quadratic/interactions (Ising; a
  symmetric matrix is read as pair values, not summed), or a dense square Q
  /qubo dataset (QUBO; opposite halves are summed, diagonal is linear);
  --h5-type forces ising/qubo when names alone can't tell.
- anything else exits with a dump of the file's structure instead of guessing.

Every returned sample's objective is recomputed locally with dimod; the
platform's energies are cross-checked and a non-constant mismatch is a fatal
error rather than a silently wrong answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def fail(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("problem", help="path to the JSON problem spec")
    p.add_argument("--solver", choices=["veloxq", "sbm"], default="veloxq")
    p.add_argument("--num-rep", type=int, default=None)
    p.add_argument("--num-steps", type=int, default=None)
    p.add_argument("--top", type=int, default=5,
                   help="number of distinct best samples to report")
    p.add_argument("--name", default=None, help="job name on the platform")
    p.add_argument("--output", default=None,
                   help="write the result JSON here instead of stdout")
    p.add_argument("--config", default=None,
                   help="veloxq_sdk config file (.py or .json) supplying the "
                        "token; defaults to ~/.veloxq/config.py")
    p.add_argument("--h5-type", choices=["ising", "qubo"], default=None,
                   help="force interpretation of a foreign HDF5 file")
    return p.parse_args()


def load_spec(path: str):
    with open(path) as f:
        spec = json.load(f)
    kind = spec.get("type")
    if kind not in ("qubo", "ising"):
        fail(f'problem "type" must be "qubo" or "ising", got {kind!r}')

    if "quadratic" in spec and "matrix" in spec:
        fail('give either "quadratic" (triples) or "matrix" (dense), not both')
    quadratic = {}
    for entry in spec.get("quadratic", []):
        i, j, v = entry
        i, j = int(i), int(j)
        key = (min(i, j), max(i, j))
        quadratic[key] = quadratic.get(key, 0.0) + float(v)
    matrix = spec.get("matrix", [])
    if matrix and any(len(row) != len(matrix) for row in matrix):
        fail('"matrix" must be square')
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if v:
                key = (min(i, j), max(i, j))
                quadratic[key] = quadratic.get(key, 0.0) + float(v)

    linear = {}
    lin = spec.get("linear", {})
    if isinstance(lin, dict):
        linear = {int(k): float(v) for k, v in lin.items()}
    else:
        linear = {i: float(v) for i, v in enumerate(lin) if v}

    indices = set(linear) | {i for ij in quadratic for i in ij}
    if not indices:
        fail("problem has no variables")
    if min(indices) < 0:
        fail("variable indices must be >= 0")
    n = max(indices) + 1
    return kind, linear, quadratic, n


def dump_h5_structure(path):
    import h5py

    lines = []

    def visit(name, obj):
        if isinstance(obj, h5py.Dataset):
            lines.append(f"  dataset {name}: shape {obj.shape}, dtype {obj.dtype}")
        else:
            lines.append(f"  group   {name}/")
        for k, v in obj.attrs.items():
            lines.append(f"    attr {name}.{k} = {v!r}")

    with h5py.File(path, "r") as f:
        f.visititems(visit)
    return "\n".join(lines) or "  (empty file)"


LINEAR_NAMES = {"biases", "bias", "h", "linear", "fields"}
QUAD_NAMES = {"couplings", "coupling", "j", "quadratic", "interactions"}
QUBO_NAMES = {"q", "qubo"}


def _vartype_from_attrs(f, dataset_path):
    """Map a var_type/vartype attribute on the file, dataset, or its parent
    group to ising/qubo, if one exists."""
    objs = [f, f[dataset_path]]
    if "/" in dataset_path:
        objs.append(f[dataset_path.rsplit("/", 1)[0]])
    for obj in objs:
        for k, v in obj.attrs.items():
            if k.lower() in ("var_type", "vartype"):
                s = (v.decode() if isinstance(v, bytes) else str(v)).upper()
                if "SPIN" in s:
                    return "ising"
                if "BINARY" in s:
                    return "qubo"
    return None


def load_h5(path, forced_type):
    """Classify an HDF5 file: native platform layout, convertible foreign, or neither."""
    import h5py
    import numpy as np

    with h5py.File(path, "r") as f:
        if "Ising" in f and "biases" in f["Ising"] and "couplings" in f["Ising"]:
            g = f["Ising"]
            biases = np.asarray(g["biases"], dtype=float)
            n = len(biases)
            c = g["couplings"]
            quadratic = {}
            if isinstance(c, h5py.Group):
                rows = np.asarray(c["I"], dtype=np.int64) - 1  # stored 1-based
                cols = np.asarray(c["J"], dtype=np.int64) - 1
                vals = np.asarray(c["V"], dtype=float)
            else:
                dense = np.asarray(c, dtype=float)
                rows, cols = np.nonzero(dense)
                vals = dense[rows, cols]
            # Both symmetric halves are stored at the full pair value; the
            # upper triangle alone recovers each pair coupling once.
            for i, j, v in zip(rows, cols, vals):
                if i < j:
                    quadratic[(int(i), int(j))] = float(v)
            linear = {i: float(v) for i, v in enumerate(biases) if v}
            labels = None
            if "labels" in g:
                labels = [x.decode() if isinstance(x, bytes) else str(x)
                          for x in np.asarray(g["labels"])]
            return {"mode": "native", "linear": linear, "quadratic": quadratic,
                    "n": n, "labels": labels}

        # Foreign file: recognize datasets by name first; when no name matches,
        # fall back to shape-based inference so a file with arbitrary names
        # still loads if its structure is unambiguous.
        datasets = []

        def collect(dsname, obj):
            if isinstance(obj, h5py.Dataset) and obj.dtype.kind in "fiu":
                datasets.append((dsname, obj.shape))

        f.visititems(collect)

        found = {}
        for dsname, shape in datasets:
            base = dsname.rsplit("/", 1)[-1].lower()
            square = len(shape) == 2 and shape[0] == shape[1]
            if base in LINEAR_NAMES and len(shape) == 1:
                found.setdefault("linear", dsname)
            elif base in QUAD_NAMES and square:
                found.setdefault("quad", dsname)
            elif base in QUBO_NAMES and square:
                found.setdefault("qubo", dsname)

        if "quad" in found and "qubo" in found and forced_type is None:
            fail(f"{path} holds both an Ising-style matrix ({found['quad']}) and "
                 f"a QUBO matrix ({found['qubo']}); pass --h5-type to disambiguate", 2)

        inferred = "quad" not in found and "qubo" not in found
        if inferred:
            squares = [(nm, sh[0]) for nm, sh in datasets
                       if len(sh) == 2 and sh[0] == sh[1] and sh[0] >= 2]
            if not squares:
                fail(f"cannot infer a model from {path}; structure:\n"
                     + dump_h5_structure(path)
                     + "\nConvert it explicitly (extract biases/couplings and use "
                       "the JSON spec or File.from_ising), or rename datasets to a "
                       "recognized form.", 2)
            if len(squares) > 1:
                fail(f"multiple square matrices in {path}: {[nm for nm, _ in squares]}; "
                     "cannot infer which one is the model — convert explicitly or "
                     "rename datasets", 2)
            mname, nq = squares[0]
            vectors = [nm for nm, sh in datasets if len(sh) == 1 and sh[0] == nq]
            if len(vectors) > 1:
                fail(f"multiple length-{nq} vectors in {path}: {vectors}; cannot "
                     "infer which one holds the linear terms", 2)
            found["matrix"] = mname
            if vectors:
                found["linear"] = vectors[0]

        src = found.get("qubo") or found.get("quad") or found["matrix"]
        matrix = np.asarray(f[src], dtype=float)
        lin_data = (np.asarray(f[found["linear"]], dtype=float)
                    if "linear" in found else None)

        if inferred:
            kind = forced_type or _vartype_from_attrs(f, src)
            how = ""
            if kind is None:
                # A nonzero diagonal cannot be an Ising coupling matrix.
                kind = "qubo" if np.any(np.diagonal(matrix)) else "ising"
                how = (" (nonzero diagonal)" if kind == "qubo"
                       else " (zero diagonal)") + " — pass --h5-type to override"
            print("shape-inferred roles: matrix=" + src
                  + (f", linear={found['linear']}" if "linear" in found else "")
                  + f"; interpreted as {kind}{how}", file=sys.stderr)
        else:
            kind = forced_type or ("qubo" if "qubo" in found else "ising")

    n = matrix.shape[0]
    linear = {}
    if lin_data is not None:
        if len(lin_data) != n:
            fail(f"linear dataset {found['linear']} has length {len(lin_data)} "
                 f"but the matrix is {n}x{n}", 2)
        linear = {i: float(v) for i, v in enumerate(lin_data) if v}

    quadratic = {}
    for i, j in zip(*np.nonzero(matrix)):
        i, j = int(i), int(j)
        if i == j:
            if kind == "ising":
                fail(f"nonzero diagonal at ({i},{i}) in an Ising matrix; "
                     "self-couplings belong in the linear dataset", 2)
            linear[i] = linear.get(i, 0.0) + float(matrix[i, i])
            continue
        key = (min(i, j), max(i, j))
        if kind == "qubo":
            quadratic[key] = quadratic.get(key, 0.0) + float(matrix[i, j])
        else:
            prev = quadratic.get(key)
            if prev is not None and not np.isclose(prev, matrix[i, j]):
                fail(f"asymmetric Ising matrix at ({i},{j}): {prev} vs {matrix[i, j]}", 2)
            quadratic[key] = float(matrix[i, j])
    print(f"converted foreign HDF5 ({src}, interpreted as {kind})", file=sys.stderr)
    return {"mode": kind, "linear": linear, "quadratic": quadratic, "n": n,
            "labels": None}


def build_bqm(kind, linear, quadratic, n):
    import dimod

    if kind == "qubo":
        q = dict(quadratic)
        for i, v in linear.items():
            q[(i, i)] = q.get((i, i), 0.0) + v
        bqm = dimod.BinaryQuadraticModel.from_qubo(q)
    else:
        diag = [ij for ij in quadratic if ij[0] == ij[1]]
        if diag:
            fail(f"ising spec has diagonal quadratic entries {diag}; "
                 "self-couplings are linear terms — put them in 'linear'")
        bqm = dimod.BinaryQuadraticModel.from_ising(linear, quadratic)
    # The platform works on dense index ranges: make every index explicit so
    # returned state columns line up with 0..n-1.
    for i in range(n):
        bqm.add_variable(i, 0.0)
    return bqm


def resolve_config(explicit: str | None) -> str | None:
    """Return the config file to load, or None to rely on VELOX_TOKEN.

    An explicit --config wins; otherwise ~/.veloxq/config.py (written by
    configure_veloxq.py at install time) is assumed, with the env var as a
    last resort. With none of those, blocks until the user configures a key
    (see await_credentials).
    """
    if explicit:
        if not os.path.isfile(explicit):
            fail(f"config file not found: {explicit}")
        return explicit
    default_cfg = os.path.expanduser("~/.veloxq/config.py")
    if os.path.isfile(default_cfg):
        return default_cfg
    if os.environ.get("VELOX_TOKEN"):
        return None
    return await_credentials(default_cfg)


def await_credentials(default_cfg: str, timeout: float = 600.0,
                      poll: float = 2.0) -> str:
    """Run the configure helper non-interactively, then wait for the key.

    stdin is closed for the helper — the same contract as the plugin's
    install hook — so it can only persist an existing credential source or
    report that interactive setup is needed; it can never read this
    process's stdin as a token. The masked TUI has to run in the user's own
    terminal, so this process polls for ~/.veloxq/config.py to appear.
    """
    import subprocess
    import time

    configure = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "configure_veloxq.py")
    r = subprocess.run([sys.executable, configure],
                       stdin=subprocess.DEVNULL, capture_output=True)
    if r.returncode == 0 and os.path.isfile(default_cfg):
        return default_cfg
    print("no credentials found. Run this once in your own terminal "
          "(masked prompt):\n"
          f"    python3 {configure}\n"
          "waiting for a valid VeloxQ key ...", file=sys.stderr)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.isfile(default_cfg):
            print(f"credentials found: {default_cfg}", file=sys.stderr)
            return default_cfg
        time.sleep(poll)
    fail(f"timed out after {timeout:.0f}s waiting for {default_cfg}")


def main() -> None:
    args = parse_args()
    args.config = resolve_config(args.config)

    is_h5 = args.problem.lower().endswith((".h5", ".hdf5"))
    native = False
    labels = None
    if is_h5:
        spec = load_h5(args.problem, args.h5_type)
        native = spec["mode"] == "native"
        kind = "ising" if native else spec["mode"]
        linear, quadratic, n = spec["linear"], spec["quadratic"], spec["n"]
        labels = spec["labels"]
    else:
        kind, linear, quadratic, n = load_spec(args.problem)

    import numpy as np

    bqm = build_bqm(kind, linear, quadratic, n)

    if args.config:
        from veloxq_sdk.config import VeloxQAPIConfig, load_config
        load_config(args.config)
        if not VeloxQAPIConfig.instance().token:
            fail(f"config file {args.config} did not set VeloxQAPIConfig.token")

    from veloxq_sdk import (
        SBMParameters,
        SBMSolver,
        VeloxQParameters,
        VeloxQSolver,
    )

    param_cls = VeloxQParameters if args.solver == "veloxq" else SBMParameters
    overrides = {}
    if args.num_rep is not None:
        overrides["num_rep"] = args.num_rep
    if args.num_steps is not None:
        overrides["num_steps"] = args.num_steps
    params = param_cls(**overrides)
    solver_cls = VeloxQSolver if args.solver == "veloxq" else SBMSolver
    solver = solver_cls(parameters=params)

    print(f"submitting {kind} problem with {n} variables to {args.solver} ...",
          file=sys.stderr)
    if native:
        from veloxq_sdk import File
        instance = File.from_path(args.problem)
        print("native platform HDF5 layout: uploading file as-is", file=sys.stderr)
    else:
        instance = bqm
    result = solver.sample(instance, name=args.name)

    samples = np.asarray(result.record.sample)
    platform_energies = np.asarray(result.record.energy, dtype=float)
    # The platform returns variable labels as strings.
    try:
        if labels is not None:
            label_to_idx = {lab: i for i, lab in enumerate(labels)}
            order = [label_to_idx[str(v)] for v in result.variables]
        else:
            order = [int(v) for v in result.variables]
    except (KeyError, TypeError, ValueError):
        fail(f"platform returned unexpected variables {list(result.variables)[:10]}", 2)
    if sorted(order) != list(range(n)):
        fail(f"platform returned variables {order[:10]}..., expected 0..{n - 1}", 2)
    # reorder columns to 0..n-1
    col = np.argsort(np.asarray(order))
    samples = samples[:, col]

    values = np.unique(samples)
    if not (set(values) <= {-1, 1} or set(values) <= {0, 1}):
        fail(f"unexpected sample values {values}", 2)
    spins = samples if set(values) <= {-1, 1} else 2 * samples - 1

    spin_bqm = bqm.spin
    recomputed_spin = spin_bqm.energies((spins, list(range(n))))
    diff = platform_energies - recomputed_spin
    if diff.size and (diff.max() - diff.min()) > 1e-4 * max(1.0, np.abs(recomputed_spin).max()):
        fail("platform energies disagree with locally recomputed energies by a "
             f"non-constant amount (spread {diff.max() - diff.min():.6g}); "
             "not trusting this result", 3)

    if kind == "qubo":
        x = ((spins + 1) // 2).astype(int)
        objective = bqm.energies((x, list(range(n))))
        reported_samples, vartype = x, "BINARY"
    else:
        objective = spin_bqm.energies((spins, list(range(n))))
        reported_samples, vartype = spins.astype(int), "SPIN"

    idx = np.argsort(objective, kind="stable")
    best_value = float(objective[idx[0]])
    num_best = int(np.sum(np.isclose(objective, best_value)))
    at_best = np.isclose(objective, best_value)
    tied_distinct = len({s.tobytes() for s, b in zip(reported_samples, at_best) if b})
    if tied_distinct >= 3 and num_best * 2 >= len(objective):
        print(f"WARNING: {tied_distinct} distinct states tie at the best energy "
              f"({num_best}/{len(objective)} samples). Legitimate for feasibility "
              "problems with many optima; otherwise this usually means a "
              "formulation error (dropped linear terms or halved cross terms — "
              "see the skill's qubo-formulations reference). Verify the decoded "
              "solutions against the original objective before trusting this.",
              file=sys.stderr)

    top, seen = [], set()
    for k in idx:
        key = reported_samples[k].tobytes()
        if key in seen:
            continue
        seen.add(key)
        top.append({"sample": reported_samples[k].tolist(),
                    "objective": float(objective[k])})
        if len(top) >= args.top:
            break

    out = {
        "type": kind,
        "vartype": vartype,
        "num_variables": n,
        "num_samples": int(samples.shape[0]),
        "best_objective": best_value,
        "best_sample": top[0]["sample"],
        "num_samples_at_best": num_best,
        "top": top,
        "solver": args.solver,
        "platform_info": {k: str(v) for k, v in dict(result.info).items()},
    }
    text = json.dumps(out, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")
        print(f"result written to {args.output}", file=sys.stderr)
    else:
        print(text)
    print(f"best objective {best_value:.6g} "
          f"({num_best}/{samples.shape[0]} samples at this value)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
