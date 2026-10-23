"""Tests for veloxq_mcp.models: spec parsing and HDF5 classification."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from veloxq_mcp.models import (  # noqa: E402
    MAX_INLINE_COEFFICIENTS,
    InlineLimitError,
    ModelError,
    describe,
    load_model,
    model_from_spec,
)


class TestSpec(unittest.TestCase):
    def test_triples_and_linear_list(self):
        m = model_from_spec({"type": "qubo", "quadratic": [[0, 1, -2.0], [1, 2, 1.5]],
                             "linear": [1.0, 0.0, -1.0]})
        self.assertEqual(m.kind, "qubo")
        self.assertEqual(m.n, 3)
        self.assertEqual(m.linear, {0: 1.0, 2: -1.0})
        self.assertEqual(m.quadratic, {(0, 1): -2.0, (1, 2): 1.5})
        self.assertEqual(m.vartype, "BINARY")

    def test_symmetric_entries_are_summed_and_ordered(self):
        m = model_from_spec({"type": "ising", "quadratic": [[2, 0, 1.0], [0, 2, 1.0]]})
        self.assertEqual(m.quadratic, {(0, 2): 2.0})

    def test_matrix_form(self):
        m = model_from_spec({"type": "qubo", "matrix": [[1, 2], [2, -1]]})
        self.assertEqual(m.quadratic, {(0, 1): 4.0})
        self.assertEqual(m.linear, {0: 1.0, 1: -1.0})
        q = m.bqm().to_qubo()[0]
        self.assertEqual(q[(0, 0)], 1.0)
        self.assertEqual(q[(1, 1)], -1.0)

    def test_linear_map(self):
        m = model_from_spec({"type": "ising", "linear": {"3": 0.5}})
        self.assertEqual(m.n, 4)
        self.assertEqual(m.linear, {3: 0.5})

    def test_rejections(self):
        cases = [
            ({"type": "spin"}, '"type" must be'),
            ({"type": "qubo"}, "no variables"),
            ({"type": "qubo", "quadratic": [[0, 1, 1]], "matrix": [[0]]}, "not both"),
            ({"type": "qubo", "matrix": [[0, 1], [1]]}, "square"),
            ({"type": "qubo", "quadratic": [[-1, 0, 1]]}, ">= 0"),
            ({"type": "qubo", "quadratic": [[0, 1]]}, "not \\[i, j, value\\]"),
            ({"type": "qubo", "linear": [float("nan")]}, "finite"),
            ({"type": "qubo", "quadratic": [[0, 1, 1]], "extra": 1}, "unknown spec keys"),
            ([1, 2], "JSON object"),
        ]
        for spec, message in cases:
            with self.subTest(spec=spec), self.assertRaisesRegex(ModelError, message):
                model_from_spec(spec)

    def test_ising_diagonal_is_rejected(self):
        with self.assertRaisesRegex(ModelError, "diagonal"):
            model_from_spec({"type": "ising", "quadratic": [[1, 1, 2.0]]})
        with self.assertRaisesRegex(ModelError, "diagonal"):
            model_from_spec({"type": "ising", "matrix": [[1.0, 0.0], [0.0, 0.0]]})

    def test_qubo_diagonal_triples_are_linear(self):
        m = model_from_spec({"type": "qubo", "quadratic": [[1, 1, 2.0], [0, 1, 1.0]]})
        self.assertEqual(m.linear, {1: 2.0})
        self.assertEqual(m.quadratic, {(0, 1): 1.0})

    def test_inline_limit_counts_entries_before_merging(self):
        quadratic = [[0, 1, 1.0]] * (MAX_INLINE_COEFFICIENTS + 1)
        with self.assertRaisesRegex(InlineLimitError, "above the limit"):
            model_from_spec({"type": "ising", "quadratic": quadratic})

    def test_inline_limit_bounds_the_largest_index(self):
        for spec in ({"type": "qubo", "linear": {"1000000000000": 1.0}},
                     {"type": "ising", "quadratic": [[0, 10**12, 1.0]]}):
            with self.subTest(spec=spec), self.assertRaisesRegex(InlineLimitError, "variable index"):
                model_from_spec(spec)
        self.assertEqual(model_from_spec({"type": "qubo", "linear": {"1000000000000": 1.0}},
                                         inline=False).n, 10**12 + 1)

    def test_inline_limit_is_checked_before_building(self):
        for spec, message in (
                ({"type": "qubo", "linear": [1.0] * (MAX_INLINE_COEFFICIENTS + 1)}, "linear entries"),
                ({"type": "qubo", "linear": {str(i): 1.0 for i in range(MAX_INLINE_COEFFICIENTS + 1)}},
                 "linear entries"),
                ({"type": "qubo", "matrix": [[1.0] * 501] * 501}, "matrix has 501 rows")):
            with self.subTest(message=message), self.assertRaisesRegex(InlineLimitError, message):
                model_from_spec(spec)

    def test_indices_must_be_whole_finite_numbers(self):
        for entry in ([0, 1.9, 1.0], [0, float("inf"), 1.0], [True, 1, 1.0]):
            with self.subTest(entry=entry), self.assertRaisesRegex(ModelError, "is not \\[i, j, value\\]"):
                model_from_spec({"type": "ising", "quadratic": [entry]})
        self.assertEqual(model_from_spec({"type": "ising", "quadratic": [[0, 2.0, 1.0]]}).n, 3)

    def test_inline_limit(self):
        quadratic = [[i, i + 1, 1.0] for i in range(MAX_INLINE_COEFFICIENTS + 1)]
        with self.assertRaisesRegex(ModelError, "above the limit"):
            model_from_spec({"type": "ising", "quadratic": quadratic})
        self.assertEqual(model_from_spec({"type": "ising", "quadratic": quadratic},
                                         inline=False).n, MAX_INLINE_COEFFICIENTS + 2)

    def test_bqm_has_every_index(self):
        m = model_from_spec({"type": "ising", "quadratic": [[0, 3, -1.0]]})
        self.assertEqual(sorted(m.bqm().variables), [0, 1, 2, 3])


class TestLoadModel(unittest.TestCase):
    def test_exactly_one_source(self):
        with self.assertRaisesRegex(ModelError, "exactly one"):
            load_model()
        with self.assertRaisesRegex(ModelError, "exactly one"):
            load_model({"type": "qubo", "linear": [1]}, "x.json")

    def test_json_file_without_inline_limit(self):
        quadratic = [[i, i + 1, 1.0] for i in range(MAX_INLINE_COEFFICIENTS + 1)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "big.json")
            with open(path, "w") as f:
                json.dump({"type": "ising", "quadratic": quadratic}, f)
            m = load_model(path=path)
        self.assertEqual(m.n, MAX_INLINE_COEFFICIENTS + 2)

    def test_missing_and_unsupported_files(self):
        with self.assertRaisesRegex(ModelError, "not a file"):
            load_model(path="/nonexistent/model.json")
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            with self.assertRaisesRegex(ModelError, "unsupported file"):
                load_model(path=f.name)
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{not json")
        try:
            with self.assertRaisesRegex(ModelError, "not valid JSON"):
                load_model(path=f.name)
        finally:
            os.unlink(f.name)


class TestHdf5(unittest.TestCase):
    def setUp(self):
        import h5py  # noqa: F401
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _write(self, name, build):
        import h5py
        path = os.path.join(self.dir.name, name)
        with h5py.File(path, "w") as f:
            build(f)
        return path

    def test_native_layout_is_uploaded_as_is(self):
        import numpy as np

        def build(f):
            g = f.create_group("Ising")
            g["biases"] = np.array([0.5, 0.0, -1.0])
            c = g.create_group("couplings")
            c["I"] = np.array([1, 2, 2, 3])
            c["J"] = np.array([2, 1, 3, 2])
            c["V"] = np.array([-1.0, -1.0, 2.0, 2.0])
            g["labels"] = np.array([b"a", b"b", b"c"])

        path = self._write("native.h5", build)
        m = load_model(path=path)
        self.assertEqual(m.native_path, path)
        self.assertEqual(m.kind, "ising")
        self.assertEqual(m.quadratic, {(0, 1): -1.0, (1, 2): 2.0})
        self.assertEqual(m.linear, {0: 0.5, 2: -1.0})
        self.assertEqual(m.labels, ["a", "b", "c"])

    def test_named_datasets(self):
        import numpy as np

        def build(f):
            f["h"] = np.array([1.0, 0.0])
            f["J"] = np.array([[0.0, -1.0], [-1.0, 0.0]])

        m = load_model(path=self._write("named.h5", build))
        self.assertEqual(m.kind, "ising")
        self.assertEqual(m.quadratic, {(0, 1): -1.0})
        self.assertEqual(m.linear, {0: 1.0})
        self.assertFalse(any("shape-inferred" in n for n in m.notes))

    def test_qubo_matrix_sums_halves_and_takes_diagonal(self):
        import numpy as np

        def build(f):
            f["Q"] = np.array([[1.0, 2.0], [1.0, -3.0]])

        m = load_model(path=self._write("q.h5", build))
        self.assertEqual(m.kind, "qubo")
        self.assertEqual(m.quadratic, {(0, 1): 3.0})
        self.assertEqual(m.linear, {0: 1.0, 1: -3.0})

    def test_shape_inference_and_forced_type(self):
        import numpy as np

        def build(f):
            f["weights"] = np.array([[0.0, 1.0], [1.0, 0.0]])
            f["offsets"] = np.array([0.0, 2.0])

        path = self._write("foreign.h5", build)
        m = load_model(path=path)
        self.assertEqual(m.kind, "ising")
        self.assertTrue(any("zero diagonal" in n for n in m.notes))
        forced = load_model(path=path, h5_type="qubo")
        self.assertEqual(forced.kind, "qubo")
        self.assertEqual(forced.quadratic, {(0, 1): 2.0})

    def test_ambiguous_files_are_refused(self):
        import numpy as np

        def build(f):
            f["a"] = np.zeros((2, 2))
            f["b"] = np.zeros((2, 2))

        with self.assertRaisesRegex(ModelError, "multiple square matrices"):
            load_model(path=self._write("two.h5", build))

        def build_empty(f):
            f["v"] = np.zeros(3)

        with self.assertRaisesRegex(ModelError, "cannot infer"):
            load_model(path=self._write("none.h5", build_empty))

    def test_asymmetric_ising_is_refused(self):
        import numpy as np

        def build(f):
            f["J"] = np.array([[0.0, 1.0], [2.0, 0.0]])

        with self.assertRaisesRegex(ModelError, "asymmetric"):
            load_model(path=self._write("asym.h5", build))

    def test_bad_h5_type(self):
        with self.assertRaisesRegex(ModelError, "h5_type"):
            load_model(path=self._write("x.h5", lambda f: None), h5_type="spin")


class TestDescribe(unittest.TestCase):
    def test_fields(self):
        m = model_from_spec({"type": "ising", "quadratic": [[0, 1, 1.0]], "linear": [1, 0]})
        self.assertEqual(describe(m), {"type": "ising", "vartype": "SPIN",
                                       "num_variables": 2, "num_interactions": 1,
                                       "notes": []})


if __name__ == "__main__":
    unittest.main()
