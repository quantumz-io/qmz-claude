"""Turn a platform result into verified, decoded numbers.

Every returned sample's objective is recomputed with dimod when the model is
known. The platform's energies are cross-checked against it and a mismatch
that is not a constant offset is an error rather than a silently wrong answer.
"""
from __future__ import annotations

from typing import Any

from .models import Model

TIE_RTOL = 1e-9
"""Two objective values closer than this, relative to their size, are a tie."""

ENERGY_RTOL = 1e-4
"""Allowed spread of (platform energy - recomputed energy) across samples,
relative to the largest recomputed magnitude."""


class ResultError(ValueError):
    """The platform result cannot be trusted or decoded."""


def objective_distribution(objective, max_distinct: int = 40, bins: int = 30) -> dict:
    """Sample counts per objective value, lowest value first.

    Values within TIE_RTOL of each other count as one. Up to ``max_distinct``
    distinct values are listed exactly as ``values``; beyond that the samples
    are histogrammed into ``bin_edges``. A long upper tail, typical of samples
    that violate a penalty, is cut at the 95th percentile so the bins resolve
    the low end; ``above`` counts the samples past the last edge.
    """
    import numpy as np

    obj = np.sort(np.asarray(objective, dtype=float))
    tol = TIE_RTOL * np.maximum(1.0, np.abs(obj[1:]))
    starts = np.flatnonzero(np.r_[True, np.diff(obj) > tol])
    if len(starts) <= max_distinct:
        counts = np.diff(np.r_[starts, len(obj)])
        return {"values": obj[starts].tolist(), "counts": counts.tolist()}
    upper = obj[-1]
    p95 = float(np.quantile(obj, 0.95))
    if p95 > obj[0] and obj[-1] - obj[0] > 3 * (p95 - obj[0]):
        upper = p95
    kept = obj[obj <= upper]
    counts, edges = np.histogram(kept, bins=bins)
    return {"bin_edges": edges.tolist(), "counts": counts.tolist(),
            "above": int(len(obj) - len(kept))}


def summarize(result, model: Model | None, top: int) -> dict[str, Any]:
    """Decode a ``dimod.SampleSet`` into the result document.

    With ``model`` the samples are reordered to ``0..n-1``, every objective is
    recomputed locally and checked against the platform's energies. A model
    with labels adds ``labels``, the name of each column. Without
    it the platform's energies are the objectives and the columns stay in the
    order the platform returned them.
    """
    import numpy as np

    samples = np.asarray(result.record.sample)
    platform_energies = np.asarray(result.record.energy, dtype=float)
    if samples.ndim != 2 or samples.shape[0] == 0:
        raise ResultError("the result holds no samples")
    values = np.unique(samples)
    if not (set(values) <= {-1, 1} or set(values) <= {0, 1}):
        raise ResultError(f"unexpected sample values {values}")

    energy_check: dict[str, Any] = {"checked": False}
    if model is None:
        n = samples.shape[1]
        kind = "ising" if set(values) <= {-1, 1} else "qubo"
        reported = samples.astype(int)
        objective = platform_energies
        vartype = "SPIN" if kind == "ising" else "BINARY"
    else:
        n = model.n
        kind = model.kind
        try:
            if model.labels is not None:
                label_to_idx = {lab: i for i, lab in enumerate(model.labels)}
                order = np.fromiter((label_to_idx[str(v)] for v in result.variables),
                                    dtype=np.int64, count=len(result.variables))
            else:
                order = np.asarray(result.variables)
                if order.dtype.kind == "U":
                    # The platform labels the columns "0", "1", ...
                    order = order.astype(np.int64)
                elif order.dtype.kind not in "iu":
                    raise ValueError
        except (KeyError, TypeError, ValueError) as err:
            raise ResultError(
                f"the platform returned unexpected variables "
                f"{list(result.variables)[:10]}") from err
        if not np.array_equal(np.sort(order), np.arange(n)):
            raise ResultError(f"the platform returned variables {order[:10].tolist()}..., "
                              f"expected 0..{n - 1}")
        samples = samples[:, np.argsort(order)]
        spins = samples if set(values) <= {-1, 1} else 2 * samples - 1
        bqm = model.bqm()
        spin_bqm = bqm.spin
        recomputed_spin = spin_bqm.energies((spins, np.arange(n)))
        diff = platform_energies - recomputed_spin
        spread = float(diff.max() - diff.min()) if diff.size else 0.0
        scale = max(1.0, float(np.abs(recomputed_spin).max()))
        energy_check = {"checked": True, "spread": spread,
                        "offset": float(diff[0]) if diff.size else 0.0}
        if spread > ENERGY_RTOL * scale:
            raise ResultError(
                "platform energies disagree with locally recomputed energies by a "
                f"non-constant amount (spread {spread:.6g}); not trusting this result")
        if kind == "qubo":
            reported = ((spins + 1) // 2).astype(int)
            objective = bqm.energies((reported, np.arange(n)))
            vartype = "BINARY"
        else:
            reported = spins.astype(int)
            objective = recomputed_spin
            vartype = "SPIN"

    objective = np.asarray(objective, dtype=float)
    idx = np.argsort(objective, kind="stable")
    best_value = float(objective[idx[0]])
    at_best = objective - best_value <= TIE_RTOL * max(1.0, abs(best_value))
    num_best = int(np.sum(at_best))
    tied_distinct = len({s.tobytes() for s, b in zip(reported, at_best) if b})
    warnings = []
    if tied_distinct >= 3 and num_best * 2 >= len(objective):
        warnings.append(
            f"{tied_distinct} distinct states tie at the best energy "
            f"({num_best}/{len(objective)} samples). Legitimate for feasibility "
            "problems with many optima; otherwise this usually means a "
            "formulation error (dropped linear terms or halved cross terms). "
            "Verify the decoded solutions against the original objective.")

    top_rows, seen = [], set()
    for k in idx:
        key = reported[k].tobytes()
        if key in seen:
            continue
        seen.add(key)
        top_rows.append({"sample": reported[k].tolist(),
                         "objective": float(objective[k])})
        if len(top_rows) >= top:
            break

    doc = {
        "type": kind,
        "vartype": vartype,
        "num_variables": int(n),
        "num_samples": int(samples.shape[0]),
        "best_objective": best_value,
        "best_sample": top_rows[0]["sample"],
        "num_samples_at_best": num_best,
        "top": top_rows,
        "objective_distribution": objective_distribution(objective),
        "energy_check": energy_check,
        "warnings": warnings,
        "result_info": {k: str(v) for k, v in dict(result.info).items()},
    }
    if model is not None and model.labels is not None:
        doc["labels"] = model.labels
    return doc
