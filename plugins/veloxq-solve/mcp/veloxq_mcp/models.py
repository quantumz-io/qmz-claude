"""Problem models: the JSON spec, HDF5 files and the dimod model built from them.

A model is a QUBO or Ising problem over integer variables ``0..n-1``. The JSON
spec is::

    {"type": "qubo",  "quadratic": [[i, j, v], ...], "linear": [v0, ...] | {"i": v}}
    {"type": "ising", "quadratic": ..., "linear": ...}

``"matrix"`` (a dense square matrix) may replace ``"quadratic"``; ``"linear"``
is optional. An HDF5 file in the platform's native layout (group ``Ising``
with ``biases`` and ``couplings``) is used as is; a foreign file is converted
when its layout is unambiguous.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

MAX_INLINE_COEFFICIENTS = 20_000
"""Largest number of coefficients accepted in an inline spec. Larger models go
through a file so the model does not have to be spelled out in a tool call."""

MAX_INLINE_VARIABLES = 100_000
"""Largest variable count of an inline spec. Every index up to the largest is
a variable, so one coefficient at a huge index would allocate them all."""

MAX_INLINE_MATRIX_ROWS = 500
"""Largest inline dense matrix. Every cell is read, so the cell count, not the
nonzero count, is what costs memory and time."""

SPEC_EXTENSIONS = (".json",)
HDF5_EXTENSIONS = (".h5", ".hdf5")

LINEAR_NAMES = {"biases", "bias", "h", "linear", "fields"}
QUAD_NAMES = {"couplings", "coupling", "j", "quadratic", "interactions"}
QUBO_NAMES = {"q", "qubo"}


class ModelError(ValueError):
    """The spec or file does not describe a model unambiguously."""


class InlineLimitError(ModelError):
    """An inline spec has more than ``MAX_INLINE_COEFFICIENTS`` coefficients."""


@dataclass
class Model:
    """A quadratic model over variables ``0..n-1``.

    ``kind`` is ``"qubo"`` or ``"ising"``. ``quadratic`` holds each pair once,
    keyed ``(i, j)`` with ``i < j``. ``native_path`` is set when the model came
    from an HDF5 file in the platform's own layout, which is uploaded as is.
    ``labels`` are the variable names stored in such a file, if any.
    """

    kind: str
    linear: dict[int, float]
    quadratic: dict[tuple[int, int], float]
    n: int
    labels: list[str] | None = None
    native_path: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def vartype(self) -> str:
        return "BINARY" if self.kind == "qubo" else "SPIN"

    @property
    def num_interactions(self) -> int:
        return len(self.quadratic)

    def bqm(self):
        """The dimod model with every index ``0..n-1`` present."""
        import dimod

        if self.kind == "qubo":
            q = dict(self.quadratic)
            for i, v in self.linear.items():
                q[(i, i)] = q.get((i, i), 0.0) + v
            bqm = dimod.BinaryQuadraticModel.from_qubo(q)
        else:
            diag = [ij for ij in self.quadratic if ij[0] == ij[1]]
            if diag:
                raise ModelError(
                    f"ising model has diagonal quadratic entries {diag}; "
                    "self-couplings are linear terms, put them in 'linear'")
            bqm = dimod.BinaryQuadraticModel.from_ising(self.linear, self.quadratic)
        # The platform works on dense index ranges: every index must exist so
        # the returned state columns line up with 0..n-1.
        for i in range(self.n):
            bqm.add_variable(i, 0.0)
        return bqm


def load_model(spec: dict | None = None, path: str | None = None,
               h5_type: str | None = None) -> Model:
    """Build a model from exactly one of an inline spec or a file path."""
    if (spec is None) == (path is None):
        raise ModelError("give exactly one of 'spec' (inline model) or 'path' (file)")
    if spec is not None:
        return model_from_spec(spec)
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise ModelError(f"not a file: {path}")
    lower = path.lower()
    if lower.endswith(HDF5_EXTENSIONS):
        return model_from_h5(path, h5_type)
    if lower.endswith(SPEC_EXTENSIONS):
        with open(path) as f:
            try:
                spec = json.load(f)
            except json.JSONDecodeError as err:
                raise ModelError(f"{path} is not valid JSON: {err}") from err
        return model_from_spec(spec, inline=False)
    raise ModelError(
        f"unsupported file {path}: expected a JSON spec {SPEC_EXTENSIONS} "
        f"or an HDF5 file {HDF5_EXTENSIONS}")


def _index(value: Any) -> int:
    """A variable index from JSON: an integer, an integral float or a numeral string."""
    if isinstance(value, bool):
        raise TypeError(f"index {value!r} is a boolean")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"index {value!r} is not a whole number")
        return int(value)
    return int(value)


def model_from_spec(spec: dict, *, inline: bool = True) -> Model:
    """Parse the JSON spec. ``inline`` enforces ``MAX_INLINE_COEFFICIENTS``."""
    if not isinstance(spec, dict):
        raise ModelError("the spec must be a JSON object")
    kind = spec.get("type")
    if kind not in ("qubo", "ising"):
        raise ModelError(f'spec "type" must be "qubo" or "ising", got {kind!r}')
    unknown = set(spec) - {"type", "quadratic", "matrix", "linear"}
    if unknown:
        raise ModelError(f"unknown spec keys {sorted(unknown)}; "
                         "allowed: type, quadratic, matrix, linear")
    if "quadratic" in spec and "matrix" in spec:
        raise ModelError('give either "quadratic" (triples) or "matrix" (dense), not both')

    entries = spec.get("quadratic", [])
    matrix = spec.get("matrix", [])
    lin = spec.get("linear", {})
    if not isinstance(entries, list) or not isinstance(matrix, list):
        raise ModelError('"quadratic" and "matrix" must be lists')
    if not isinstance(lin, (list, dict)):
        raise ModelError('"linear" must be a list of numbers or an {"i": value} map')
    if inline:
        for what, size in (("quadratic", len(entries)), ("linear", len(lin))):
            if size > MAX_INLINE_COEFFICIENTS:
                raise InlineLimitError(f"the inline spec has {size} {what} entries, above "
                                       f"the limit of {MAX_INLINE_COEFFICIENTS}")
        if len(matrix) > MAX_INLINE_MATRIX_ROWS:
            raise InlineLimitError(f"the inline matrix has {len(matrix)} rows, above the "
                                   f"limit of {MAX_INLINE_MATRIX_ROWS}")
    quadratic: dict[tuple[int, int], float] = {}
    for entry in entries:
        try:
            i, j, v = entry
            i, j, v = _index(i), _index(j), float(v)
        except (TypeError, ValueError) as err:
            raise ModelError(f"quadratic entry {entry!r} is not [i, j, value]") from err
        key = (min(i, j), max(i, j))
        quadratic[key] = quadratic.get(key, 0.0) + v
    if matrix and any(len(row) != len(matrix) for row in matrix):
        raise ModelError('"matrix" must be square')
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if v:
                key = (min(i, j), max(i, j))
                quadratic[key] = quadratic.get(key, 0.0) + float(v)

    try:
        if isinstance(lin, dict):
            linear = {_index(k): float(v) for k, v in lin.items()}
        else:
            linear = {i: float(v) for i, v in enumerate(lin) if v}
    except (TypeError, ValueError) as err:
        raise ModelError('"linear" must be a list of numbers or an {"i": value} map') from err

    for i, j in [ij for ij in quadratic if ij[0] == ij[1]]:
        if kind == "ising":
            raise ModelError(f"ising model has a diagonal entry at ({i},{i}); "
                             "self-couplings are linear terms, put them in 'linear'")
        linear[i] = linear.get(i, 0.0) + quadratic.pop((i, j))

    count = len(linear) + len(quadratic)
    if inline and count > MAX_INLINE_COEFFICIENTS:
        raise InlineLimitError(
            f"the inline spec has {count} coefficients, above the limit of "
            f"{MAX_INLINE_COEFFICIENTS}")
    indices = set(linear) | {i for ij in quadratic for i in ij}
    if not indices:
        raise ModelError("the model has no variables")
    if min(indices) < 0:
        raise ModelError("variable indices must be >= 0")
    if inline and max(indices) >= MAX_INLINE_VARIABLES:
        raise InlineLimitError(f"variable index {max(indices)} is above the inline limit of "
                               f"{MAX_INLINE_VARIABLES - 1}")
    bad = [v for v in list(linear.values()) + list(quadratic.values())
           if v != v or v in (float("inf"), float("-inf"))]
    if bad:
        raise ModelError("coefficients must be finite numbers")
    return Model(kind=kind, linear=linear, quadratic=quadratic, n=max(indices) + 1)


def h5_structure(path: str) -> str:
    """One line per group, dataset and attribute of an HDF5 file."""
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


def _vartype_from_attrs(f, dataset_path):
    """Map a var_type or vartype attribute near the dataset to ising or qubo."""
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


def model_from_h5(path: str, forced_type: str | None = None) -> Model:
    """Classify an HDF5 file: native platform layout, convertible foreign, or neither."""
    import h5py
    import numpy as np

    if forced_type not in (None, "ising", "qubo"):
        raise ModelError("h5_type must be 'ising' or 'qubo'")

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
            # Both symmetric halves hold the full pair value; the upper
            # triangle alone recovers each coupling once.
            for i, j, v in zip(rows, cols, vals):
                if i < j:
                    quadratic[(int(i), int(j))] = float(v)
            linear = {i: float(v) for i, v in enumerate(biases) if v}
            labels = None
            if "labels" in g:
                labels = [x.decode() if isinstance(x, bytes) else str(x)
                          for x in np.asarray(g["labels"])]
            return Model(kind="ising", linear=linear, quadratic=quadratic, n=n,
                         labels=labels, native_path=path,
                         notes=["native platform HDF5 layout, uploaded as is"])

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
            raise ModelError(
                f"{path} holds both an Ising-style matrix ({found['quad']}) and "
                f"a QUBO matrix ({found['qubo']}); pass h5_type to disambiguate")

        notes = []
        inferred = "quad" not in found and "qubo" not in found
        if inferred:
            squares = [(nm, sh[0]) for nm, sh in datasets
                       if len(sh) == 2 and sh[0] == sh[1] and sh[0] >= 2]
            if not squares:
                raise ModelError(
                    f"cannot infer a model from {path}; structure:\n"
                    + h5_structure(path)
                    + "\nConvert it explicitly (extract biases and couplings into "
                      "the JSON spec) or rename datasets to a recognized form.")
            if len(squares) > 1:
                raise ModelError(
                    f"multiple square matrices in {path}: {[nm for nm, _ in squares]}; "
                    "cannot infer which one is the model")
            mname, nq = squares[0]
            vectors = [nm for nm, sh in datasets if len(sh) == 1 and sh[0] == nq]
            if len(vectors) > 1:
                raise ModelError(
                    f"multiple length-{nq} vectors in {path}: {vectors}; cannot "
                    "infer which one holds the linear terms")
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
                       else " (zero diagonal)") + "; pass h5_type to override"
            notes.append("shape-inferred roles: matrix=" + src
                         + (f", linear={found['linear']}" if "linear" in found else "")
                         + f"; interpreted as {kind}{how}")
        else:
            kind = forced_type or ("qubo" if "qubo" in found else "ising")
            notes.append(f"converted foreign HDF5 ({src}, interpreted as {kind})")

    n = matrix.shape[0]
    linear = {}
    if lin_data is not None:
        if len(lin_data) != n:
            raise ModelError(f"linear dataset {found['linear']} has length "
                             f"{len(lin_data)} but the matrix is {n}x{n}")
        linear = {i: float(v) for i, v in enumerate(lin_data) if v}

    quadratic = {}
    for i, j in zip(*np.nonzero(matrix)):
        i, j = int(i), int(j)
        if i == j:
            if kind == "ising":
                raise ModelError(f"nonzero diagonal at ({i},{i}) in an Ising matrix; "
                                 "self-couplings belong in the linear dataset")
            linear[i] = linear.get(i, 0.0) + float(matrix[i, i])
            continue
        key = (min(i, j), max(i, j))
        if kind == "qubo":
            quadratic[key] = quadratic.get(key, 0.0) + float(matrix[i, j])
        else:
            prev = quadratic.get(key)
            if prev is not None and not np.isclose(prev, matrix[i, j]):
                raise ModelError(f"asymmetric Ising matrix at ({i},{j}): "
                                 f"{prev} vs {matrix[i, j]}")
            quadratic[key] = float(matrix[i, j])
    return Model(kind=kind, linear=linear, quadratic=quadratic, n=n, notes=notes)


def describe(model: Model) -> dict[str, Any]:
    """The fields a tool reports about a model."""
    return {
        "type": model.kind,
        "vartype": model.vartype,
        "num_variables": model.n,
        "num_interactions": model.num_interactions,
        "notes": list(model.notes),
    }
