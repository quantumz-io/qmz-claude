# /// script
# requires-python = ">=3.10"
# dependencies = ["dwave-system>=1.20,<2", "dimod>=0.12,<1"]
# ///
"""Submit a QUBO or Ising spec to a D-Wave Leap solver and write result.json.

The spec file holds {"type": "qubo"|"ising", "quadratic": [[i, j, v], ...]
or "matrix": [[...]], "linear": [...] or {"i": v}}. Indices are integers
starting at 0 and every pair appears once. result.json holds the best
samples with their objectives recomputed locally from the spec, and the
solver's timing. It is written only when the solve succeeded; a stale copy
is removed first.

The token comes from DWAVE_API_TOKEN or Ocean's configuration file; this
script takes no token argument.
"""

import argparse
import json
import math
import os
import sys

import dimod
import numpy as np
from dwave.cloud.config import load_config
from dwave.system import DWaveSampler, EmbeddingComposite, LeapHybridSampler
from ocean_config import config_files, silence_debug_logging

QPU_AUTO_MAX_VARIABLES = 100
MAX_INDEX = 10_000_000
RESULT_FILE = "result.json"
LABEL = "dwave-solve"


def load_spec(path):
    with open(path) as fp:
        spec = json.load(fp)
    vartype = {"qubo": "BINARY", "ising": "SPIN"}.get(str(spec.get("type", "")).lower())
    if vartype is None:
        raise ValueError(f'spec "type" must be "qubo" or "ising", got {spec.get("type")!r}')
    if ("quadratic" in spec) == ("matrix" in spec):
        raise ValueError('spec needs exactly one of "quadratic" or "matrix"')

    diagonal = set()
    if "matrix" in spec:
        m = np.asarray(spec["matrix"], dtype=float)
        if m.ndim != 2 or m.shape[0] != m.shape[1]:
            raise ValueError(f"matrix must be square, got shape {m.shape}")
        if not np.all(np.isfinite(m)):
            raise ValueError("matrix holds NaN or infinite entries")
        if vartype == "SPIN" and np.any(np.diag(m) != 0):
            raise ValueError('an Ising matrix has no diagonal; put the fields in "linear"')
        bqm = dimod.BinaryQuadraticModel(m, vartype)
    else:
        bqm = dimod.BinaryQuadraticModel(vartype)
        seen = set()
        for k, triple in enumerate(spec["quadratic"]):
            if len(triple) != 3:
                raise ValueError(f"quadratic entry {k} is not an [i, j, value] triple: {triple!r}")
            i, j, v = triple
            check_index(i, f"quadratic entry {k}")
            check_index(j, f"quadratic entry {k}")
            check_finite(v, f"quadratic entry {k}")
            pair = frozenset((i, j))
            if pair in seen:
                raise ValueError(f"quadratic entry {k} repeats the pair ({i}, {j}); each pair appears once")
            seen.add(pair)
            if i == j:
                if vartype == "SPIN":
                    raise ValueError(f'quadratic entry {k} is an Ising self-coupling; put fields in "linear"')
                bqm.add_variable(i, v)
                diagonal.add(i)
            else:
                bqm.add_interaction(i, j, v)

    linear = spec.get("linear")
    items = linear.items() if isinstance(linear, dict) else enumerate(linear or [])
    for key, v in items:
        i = int(key) if isinstance(key, str) and key.lstrip("-").isdigit() else key
        check_index(i, f"linear[{key}]")
        check_finite(v, f"linear[{key}]")
        if "matrix" in spec:
            raise ValueError('with "matrix" the diagonal holds the linear terms; drop "linear"')
        if i in diagonal:
            raise ValueError(f"variable {i} has both a diagonal entry and a linear entry")
        bqm.add_variable(i, v)

    n = max(bqm.variables) + 1 if bqm.num_variables else 0
    for i in range(n):
        bqm.add_variable(i)
    return bqm, n


def check_index(i, where):
    if not isinstance(i, int) or isinstance(i, bool) or i < 0 or i > MAX_INDEX:
        raise ValueError(f"{where}: index {i!r} is not an integer in 0..{MAX_INDEX}")


def check_finite(v, where):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise ValueError(f"{where}: coefficient {v!r} is not a finite number")


def sample_qpu(bqm, args, paths):
    sampler = EmbeddingComposite(DWaveSampler(config_file=paths))
    kwargs = {"num_reads": args.num_reads, "label": LABEL, "return_embedding": True}
    if args.chain_strength is not None:
        kwargs["chain_strength"] = args.chain_strength
    sampleset = sampler.sample(bqm, **kwargs)
    sampleset.resolve()
    info = sampleset.info
    chains = [len(c) for c in info.get("embedding_context", {}).get("embedding", {}).values()]
    meta = {
        "sampler": "qpu",
        "solver": sampler.child.solver.name,
        "problem_id": info.get("problem_id"),
        "qpu_access_time_s": info.get("timing", {}).get("qpu_access_time", 0) / 1e6,
        "num_reads": args.num_reads,
        "embedding": {"physical_qubits": sum(chains), "max_chain_length": max(chains, default=0)},
    }
    if "chain_break_fraction" in sampleset.record.dtype.names:
        cbf = sampleset.record.chain_break_fraction
        meta["chain_break_fraction"] = {"mean": float(cbf.mean()), "max": float(cbf.max())}
    return sampleset, meta


def sample_hybrid(bqm, args, paths):
    sampler = LeapHybridSampler(config_file=paths)
    kwargs = {"label": LABEL}
    if args.time_limit is not None:
        kwargs["time_limit"] = args.time_limit
    sampleset = sampler.sample(bqm, **kwargs)
    sampleset.resolve()
    info = sampleset.info
    meta = {
        "sampler": "hybrid",
        "solver": sampler.solver.name,
        "problem_id": info.get("problem_id"),
        "run_time_s": info.get("run_time", 0) / 1e6,
        "qpu_access_time_s": info.get("qpu_access_time", 0) / 1e6,
        "time_limit_s": float(args.time_limit if args.time_limit is not None else sampler.min_time_limit(bqm)),
    }
    return sampleset, meta


def summarize(bqm, n, sampleset, top=5):
    """Rank the distinct samples by locally recomputed energy; the spec has no offset."""
    agg = sampleset.aggregate()
    reported = np.asarray(agg.record.energy, dtype=float)
    recomputed = np.asarray(bqm.energies(agg), dtype=float)
    deviation = float(np.abs(recomputed - reported).max())
    energy_check = {"ok": deviation <= 1e-6 * max(1.0, float(np.abs(recomputed).max())),
                    "max_abs_deviation": deviation}
    order = np.argsort(recomputed, kind="stable")
    column = {v: k for k, v in enumerate(agg.variables)}
    ranked = [{"sample": [int(agg.record.sample[idx][column[v]]) for v in range(n)],
               "objective": float(recomputed[idx]),
               "num_occurrences": int(agg.record.num_occurrences[idx])} for idx in order]
    best = ranked[0]["objective"]
    at_best = [d for d in ranked if d["objective"] <= best + 1e-9 * max(1.0, abs(best))]
    return {
        "num_variables": n,
        "num_interactions": bqm.num_interactions,
        "num_samples": int(agg.record.num_occurrences.sum()),
        "best_objective": best,
        "best_sample": ranked[0]["sample"],
        "num_samples_at_best": sum(d["num_occurrences"] for d in at_best),
        "num_distinct_samples_at_best": len(at_best),
        "top": ranked[:top],
        "energy_check": energy_check,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", help="problem spec JSON file")
    parser.add_argument("--sampler", choices=["auto", "qpu", "hybrid"], default="auto",
                        help=f"auto picks qpu up to {QPU_AUTO_MAX_VARIABLES} variables, hybrid above")
    parser.add_argument("--num-reads", type=int, default=1000, help="QPU reads (default 1000)")
    parser.add_argument("--chain-strength", type=float, default=None, help="QPU chain strength")
    parser.add_argument("--time-limit", type=float, default=None,
                        help="hybrid time limit in seconds, at or above the solver's minimum")
    args = parser.parse_args()

    silence_debug_logging()
    bqm, n = load_spec(args.spec)
    if n == 0:
        sys.exit("The spec defines no variables.")
    paths = config_files()
    if not load_config(config_file=paths).get("token"):
        sys.exit("No Leap API token: set DWAVE_API_TOKEN or store one with store_token.py.")
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)

    sampler = args.sampler
    if sampler == "auto":
        sampler = "qpu" if n <= QPU_AUTO_MAX_VARIABLES else "hybrid"
    sampleset, meta = (sample_qpu if sampler == "qpu" else sample_hybrid)(bqm, args, paths)

    result = {"spec": args.spec, "vartype": str(bqm.vartype.name), **meta, **summarize(bqm, n, sampleset)}
    if not result["energy_check"]["ok"]:
        sys.exit(f"Energy check failed for Leap problem {meta['problem_id']}: the solver's energies "
                 f"and the local recomputation differ by {result['energy_check']['max_abs_deviation']}.")
    with open(RESULT_FILE, "w") as fp:
        json.dump(result, fp, indent=2)
    shown = {k: v for k, v in result.items() if k != "top"}
    if n > 50:
        shown["best_sample"] = f"<{n} values, see {RESULT_FILE}>"
    print(json.dumps(shown, indent=2))


if __name__ == "__main__":
    main()
