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
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

MAX_INLINE_COEFFICIENTS = 20_000
"""Largest number of coefficients accepted in an inline spec. Larger models go
through a file so the model does not have to be spelled out in a tool call."""

MAX_INLINE_VARIABLES = 100_000
"""Largest variable count of an inline spec. Every index up to the largest is
a variable, so one coefficient at a huge index would allocate them all."""

MAX_INLINE_MATRIX_ROWS = 500
"""Largest inline dense matrix. Every cell is read, so the cell count, not the
nonzero count, is what costs memory and time."""

MAX_VARIABLES = 100_000_000
"""Largest variable count of any model, the platform's own scale. The biases
are held densely, so the index range, not the coefficient count, sets the
memory a model takes; the dimod model built from it adds about 100 MB per
million variables."""

MAX_DENSE_ROWS = 20_000
"""Largest dense square matrix read from a file, 3.2 GB of float64. Larger
models go through the sparse native layout."""

SPEC_EXTENSIONS = (".json",)
HDF5_EXTENSIONS = (".h5", ".hdf5")

LINEAR_NAMES = {"biases", "bias", "h", "linear", "fields"}
QUAD_NAMES = {"couplings", "coupling", "j", "quadratic", "interactions"}
QUBO_NAMES = {"q", "qubo"}

INTEGER_LABEL = re.compile(r"-?(0|[1-9][0-9]*)")
"""A label in the form the SDK writes an integer variable."""


class ModelError(ValueError):
    """The spec or file does not describe a model unambiguously."""


class InlineLimitError(ModelError):
    """An inline spec has more than ``MAX_INLINE_COEFFICIENTS`` coefficients."""


@dataclass
class Model:
    """A quadratic model over variables ``0..n-1``, held as arrays.

    ``kind`` is ``"qubo"`` or ``"ising"``. ``biases`` holds the linear
    coefficient of every variable, zero where the model has none, so its
    length is ``n``. ``rows``, ``cols`` and ``values`` hold each coupling
    once, with ``rows < cols`` and no pair repeated. ``native_path`` is set
    when the model came from an HDF5 file in the platform's own layout, which
    is uploaded as is. ``labels`` name the variables of such a file. Integer
    labels put the variables in ascending label order, and labels that read
    ``0..n-1`` are None. Other labels keep the file's order.
    """

    kind: str
    biases: np.ndarray
    rows: np.ndarray
    cols: np.ndarray
    values: np.ndarray
    labels: list[str] | None = None
    native_path: str | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        import numpy as np

        self.biases = np.asarray(self.biases, dtype=float)
        self.rows = np.asarray(self.rows, dtype=np.int64)
        self.cols = np.asarray(self.cols, dtype=np.int64)
        self.values = np.asarray(self.values, dtype=float)
        if self.biases.ndim != 1 or self.biases.size == 0:
            raise ModelError("the model has no variables")
        if self.n > MAX_VARIABLES:
            raise ModelError(f"the model has {self.n} variables, above the limit of "
                             f"{MAX_VARIABLES}")
        if not self.rows.shape == self.cols.shape == self.values.shape or self.rows.ndim != 1:
            raise ModelError("rows, cols and values must be vectors of one length")
        if self.rows.size and (self.rows.min() < 0 or self.cols.max() >= self.n):
            raise ModelError(f"coupling indices must lie in 0..{self.n - 1}")
        if not (self.rows < self.cols).all():
            raise ModelError("couplings must have rows < cols")
        if not (np.isfinite(self.biases).all() and np.isfinite(self.values).all()):
            raise ModelError("coefficients must be finite numbers")

    @property
    def n(self) -> int:
        return len(self.biases)

    @property
    def vartype(self) -> str:
        return "BINARY" if self.kind == "qubo" else "SPIN"

    @property
    def num_interactions(self) -> int:
        return len(self.values)

    @property
    def linear(self) -> dict[int, float]:
        """The nonzero linear terms keyed by variable."""
        import numpy as np

        return {int(i): float(self.biases[i]) for i in np.flatnonzero(self.biases)}

    @property
    def quadratic(self) -> dict[tuple[int, int], float]:
        """The couplings keyed ``(i, j)`` with ``i < j``."""
        return {(int(i), int(j)): float(v)
                for i, j, v in zip(self.rows, self.cols, self.values)}

    def bqm(self):
        """The dimod model with every index ``0..n-1`` present."""
        import dimod

        return dimod.BinaryQuadraticModel.from_numpy_vectors(
            self.biases, (self.rows, self.cols, self.values), 0.0, self.vartype)


def _by_label(labels, biases, rows, cols, values):
    """The variables in ascending order of their labels when every label is an
    integer, else in file order. Labels that then read 0..n-1 say nothing the
    indices do not and come back as None."""
    import numpy as np

    if not all(INTEGER_LABEL.fullmatch(x) for x in labels):
        return biases, rows, cols, values, labels
    order = np.argsort([int(x) for x in labels], kind="stable")
    new = np.empty_like(order)
    new[order] = np.arange(len(order))
    i, j = new[rows], new[cols]
    rows, cols = np.minimum(i, j), np.maximum(i, j)
    keep = np.lexsort((cols, rows))
    labels = [labels[k] for k in order]
    if labels == [str(k) for k in range(len(labels))]:
        labels = None
    return biases[order], rows[keep], cols[keep], values[keep], labels


def _couplings(i, j, v, *, combine: str):
    """Canonical coupling arrays from off-diagonal triples: each pair once,
    ``rows < cols``. A pair given more than once, in either order, is summed
    (``combine="sum"``) or must carry one value (``combine="same"``)."""
    import numpy as np

    i, j, v = (np.asarray(i, dtype=np.int64), np.asarray(j, dtype=np.int64),
               np.asarray(v, dtype=float))
    if not np.isfinite(v).all():
        raise ModelError("coefficients must be finite numbers")
    if not i.size:
        return i, j, v
    lo, hi = np.minimum(i, j), np.maximum(i, j)
    span = int(hi.max()) + 1
    # One int64 per pair: lo, hi < span <= MAX_VARIABLES keeps the key below 2**62.
    key = lo * span + hi
    uniq, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    if combine == "sum":
        values = np.bincount(inverse, weights=v, minlength=len(uniq))
    else:
        values = v[first]
        off = np.flatnonzero(~np.isclose(v, values[inverse]))
        if off.size:
            k = int(off[0])
            raise ModelError(f"coupling ({lo[k]},{hi[k]}) is given with two values, "
                             f"{values[inverse[k]]} and {v[k]}")
    return *np.divmod(uniq, span), values


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
    import numpy as np

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
    i, j, v = _triples(entries)
    if matrix:
        try:
            dense = np.asarray(matrix, dtype=float)
            if dense.ndim != 2 or dense.shape[0] != dense.shape[1]:
                raise ValueError
        except (TypeError, ValueError) as err:
            raise ModelError('"matrix" must be a square matrix of numbers') from err
        mi, mj = np.nonzero(dense)
        i, j, v = np.r_[i, mi], np.r_[j, mj], np.r_[v, dense[mi, mj]]

    try:
        if isinstance(lin, dict):
            lin_idx = np.array([_index(k) for k in lin], dtype=np.int64)
            lin_val = np.array([float(x) for x in lin.values()], dtype=float)
        else:
            lin_val = np.asarray(lin, dtype=float)
            if lin_val.ndim != 1:
                raise ValueError
            # A zero in the list is not a term; only terms define variables.
            lin_idx = np.flatnonzero(lin_val)
            lin_val = lin_val[lin_idx]
    except (TypeError, ValueError, OverflowError) as err:
        raise ModelError('"linear" must be a list of numbers or an {"i": value} map') from err
    if len(set(lin_idx.tolist())) != len(lin_idx):
        raise ModelError('"linear" names a variable twice')

    if (i.size and i.min() < 0) or (j.size and j.min() < 0) or (lin_idx.size and lin_idx.min() < 0):
        raise ModelError("variable indices must be >= 0")
    n = 1 + max([-1] + [int(x.max()) for x in (i, j, lin_idx) if x.size])
    if inline and n > MAX_INLINE_VARIABLES:
        raise InlineLimitError(f"variable index {n - 1} is above the inline limit of "
                               f"{MAX_INLINE_VARIABLES - 1}")
    if n > MAX_VARIABLES:
        raise ModelError(f"variable index {n - 1} is above the limit of {MAX_VARIABLES - 1}")
    biases = np.zeros(n)
    np.add.at(biases, lin_idx, lin_val)
    diag = i == j
    if diag.any():
        if kind == "ising":
            k = int(i[diag][0])
            raise ModelError(f"ising model has a diagonal entry at ({k},{k}); "
                             "self-couplings are linear terms, put them in 'linear'")
        np.add.at(biases, i[diag], v[diag])
    rows, cols, values = _couplings(i[~diag], j[~diag], v[~diag], combine="sum")

    count = int(np.count_nonzero(biases)) + len(values)
    if inline and count > MAX_INLINE_COEFFICIENTS:
        raise InlineLimitError(
            f"the inline spec has {count} coefficients, above the limit of "
            f"{MAX_INLINE_COEFFICIENTS}")
    return Model(kind=kind, biases=biases, rows=rows, cols=cols, values=values)


def _triples(entries: list) -> tuple:
    """The ``[i, j, value]`` entries of a spec as three arrays. A list of
    plain numbers converts in one step; anything else is checked entry by
    entry so the error names the offending one."""
    import numpy as np

    if not entries:
        return (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0))
    # A boolean index would convert to 0 or 1 unnoticed.
    plain = all(isinstance(e, list) and len(e) == 3 and not isinstance(e[0], bool)
                and not isinstance(e[1], bool) for e in entries)
    raw = None
    if plain:
        try:
            raw = np.asarray(entries)
        except ValueError:      # ragged nesting inside an entry
            pass
    if raw is not None and raw.ndim == 2 and raw.dtype.kind in "iuf":
        i, j, v = raw[:, 0], raw[:, 1], raw[:, 2].astype(float)
        whole = (np.abs(i) < 2**63) & (np.abs(j) < 2**63)
        if raw.dtype.kind == "f":
            whole &= np.isfinite(i) & np.isfinite(j) & (i == np.floor(i)) & (j == np.floor(j))
        if not whole.all():
            k = int(np.flatnonzero(~whole)[0])
            raise ModelError(f"quadratic entry {entries[k]!r} is not [i, j, value]")
        return i.astype(np.int64), j.astype(np.int64), v
    ii, jj, vv = [], [], []
    for entry in entries:
        try:
            i, j, v = entry
            ii.append(_index(i))
            jj.append(_index(j))
            vv.append(float(v))
        except (TypeError, ValueError) as err:
            raise ModelError(f"quadratic entry {entry!r} is not [i, j, value]") from err
    try:
        return (np.array(ii, dtype=np.int64), np.array(jj, dtype=np.int64),
                np.array(vv, dtype=float))
    except OverflowError:
        raise ModelError(f"variable index {max(ii + jj)} is above the limit of "
                         f"{MAX_VARIABLES - 1}") from None


def _vector_length(dataset, what: str) -> int:
    if len(dataset.shape) != 1:
        raise ModelError(f"{what} must be a vector, got shape {dataset.shape}")
    return int(dataset.shape[0])


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
            n = _vector_length(g["biases"], f"{path}: biases")
            if n > MAX_VARIABLES:
                raise ModelError(f"{path} has {n} variables, above the limit of {MAX_VARIABLES}")
            c = g["couplings"]
            if isinstance(c, h5py.Group):
                lengths = {_vector_length(c[k], f"{path}: couplings/{k}") for k in "IJV"}
                if len(lengths) != 1:
                    raise ModelError(f"{path}: couplings I, J and V must be vectors "
                                     "of one length")
                i = np.asarray(c["I"], dtype=np.int64) - 1  # stored 1-based
                j = np.asarray(c["J"], dtype=np.int64) - 1
                v = np.asarray(c["V"], dtype=float)
            else:
                if c.shape != (n, n):
                    raise ModelError(f"{path}: a dense couplings matrix must be {n}x{n}")
                if n > MAX_DENSE_ROWS:
                    raise ModelError(f"{path}: a dense {n}x{n} couplings matrix is above "
                                     f"the limit of {MAX_DENSE_ROWS} rows; store the "
                                     "couplings as I, J, V vectors")
                dense = np.asarray(c, dtype=float)
                i, j = np.nonzero(dense)
                v = dense[i, j]
            biases = np.asarray(g["biases"], dtype=float)
            if i.size and (min(i.min(), j.min()) < 0 or max(i.max(), j.max()) >= n):
                raise ModelError(f"{path}: couplings index a variable outside 1..{n}")
            notes = ["native platform HDF5 layout, uploaded as is"]
            diag = i == j
            if diag.any():
                # A self-coupling of a spin is a constant, which the energy
                # check tolerates as an offset.
                notes.append(f"{int(diag.sum())} diagonal couplings ignored as constants")
            # Each pair may be stored in both orders with the full value.
            rows, cols, values = _couplings(i[~diag], j[~diag], v[~diag], combine="same")
            labels = None
            if "labels" in g:
                labels = [x.decode() if isinstance(x, bytes) else str(x)
                          for x in np.asarray(g["labels"])]
                if len(labels) != n:
                    raise ModelError(f"{path}: {len(labels)} labels for {n} variables")
                label, count = Counter(labels).most_common(1)[0]
                if count > 1:
                    raise ModelError(f"{path}: label {label!r} names {count} variables")
                biases, rows, cols, values, labels = _by_label(labels, biases, rows, cols,
                                                               values)
            return Model(kind="ising", biases=biases, rows=rows, cols=cols, values=values,
                         labels=labels, native_path=path, notes=notes)

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
        if f[src].shape[0] > MAX_DENSE_ROWS:
            raise ModelError(f"{path}: the dense {f[src].shape[0]}x{f[src].shape[1]} matrix "
                             f"{src} is above the limit of {MAX_DENSE_ROWS} rows; convert it "
                             "to the sparse native layout")
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
    biases = np.zeros(n)
    if lin_data is not None:
        if lin_data.shape != (n,):
            raise ModelError(f"linear dataset {found['linear']} has length "
                             f"{len(lin_data)} but the matrix is {n}x{n}")
        biases += lin_data

    i, j = np.nonzero(matrix)
    v = matrix[i, j]
    diag = i == j
    if diag.any():
        if kind == "ising":
            k = int(i[diag][0])
            raise ModelError(f"nonzero diagonal at ({k},{k}) in an Ising matrix; "
                             "self-couplings belong in the linear dataset")
        biases[i[diag]] += v[diag]
    rows, cols, values = _couplings(i[~diag], j[~diag], v[~diag],
                                    combine="sum" if kind == "qubo" else "same")
    return Model(kind=kind, biases=biases, rows=rows, cols=cols, values=values, notes=notes)


def describe(model: Model) -> dict[str, Any]:
    """The fields a tool reports about a model."""
    return {
        "type": model.kind,
        "vartype": model.vartype,
        "num_variables": model.n,
        "num_interactions": model.num_interactions,
        "notes": list(model.notes),
    }
