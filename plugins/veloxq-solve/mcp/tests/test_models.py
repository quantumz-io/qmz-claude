"""Tests for veloxq_mcp.models: spec parsing and HDF5 classification."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

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
        with self.assertRaisesRegex(ModelError, "above the limit of"):
            model_from_spec({"type": "qubo", "linear": {"1000000000000": 1.0}}, inline=False)
        self.assertEqual(model_from_spec({"type": "qubo", "linear": {"200000": 1.0}},
                                         inline=False).n, 200_001)

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

    def test_only_terms_define_variables(self):
        m = model_from_spec({"type": "ising", "linear": [0.0, 0.0, 1.0, 0.0]})
        self.assertEqual((m.n, m.linear), (3, {2: 1.0}))
        self.assertEqual(model_from_spec({"type": "ising", "linear": {"3": 0.0}}).n, 4)
        with self.assertRaisesRegex(ModelError, "no variables"):
            model_from_spec({"type": "ising", "linear": [0.0]})
        with self.assertRaisesRegex(ModelError, "names a variable twice"):
            model_from_spec({"type": "ising", "linear": {"1": 1.0, "01": 2.0}})

    def test_nested_ragged_and_huge_entries_are_refused(self):
        for entry in ([[0, 1], [1, 2], [1.0, 2.0]], [[0], [1], [1.0]], [0, 1, [1.0]],
                      [0, 2**63, 1], [0, 9.3e18, 1.0], [0, 1e300, 1.0]):
            with self.subTest(entry=entry), \
                    self.assertRaisesRegex(ModelError, r"is not \[i, j, value\]"):
                model_from_spec({"type": "qubo", "quadratic": [entry]})
        for entry in ([0, 2**70, 1.0], ["0", str(2**70), 1.0]):
            with self.subTest(entry=entry), self.assertRaisesRegex(ModelError, "above the limit"):
                model_from_spec({"type": "qubo", "quadratic": [entry]})
        with self.assertRaisesRegex(ModelError, '"linear" must be'):
            model_from_spec({"type": "qubo", "linear": {str(2**70): 1.0}})

    def test_numeral_strings_and_matrix_rows_are_checked(self):
        m = model_from_spec({"type": "qubo", "quadratic": [["1", "0", 2.0]]})
        self.assertEqual(m.quadratic, {(0, 1): 2.0})
        for matrix in ([[1.0, 2.0], [3.0]], [[1.0, 2.0, 3.0]], [["a", 2.0], [3.0, 4.0]]):
            with self.subTest(matrix=matrix), \
                    self.assertRaisesRegex(ModelError, "must be a square matrix of numbers"):
                model_from_spec({"type": "qubo", "matrix": matrix})

    def test_a_million_variables_load_as_arrays(self):
        import numpy as np

        n = 1_000_000
        spec = {"type": "ising", "quadratic": np.c_[np.arange(n - 1), np.arange(1, n),
                                                    -np.ones(n - 1)].tolist()}
        m = model_from_spec(spec, inline=False)
        self.assertEqual((m.n, m.num_interactions), (n, n - 1))
        bqm = m.bqm()
        self.assertEqual((bqm.num_variables, bqm.num_interactions), (n, n - 1))
        self.assertEqual(bqm.energy((np.ones(n, dtype=np.int8), np.arange(n))), -(n - 1))


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

    def test_native_couplings_stored_once_in_either_order(self):
        import numpy as np

        def build(f):
            g = f.create_group("Ising")
            g["biases"] = np.zeros(3)
            c = g.create_group("couplings")
            c["I"] = np.array([2, 2])
            c["J"] = np.array([1, 3])
            c["V"] = np.array([-1.0, 2.0])

        m = load_model(path=self._write("once.h5", build))
        self.assertEqual(m.quadratic, {(0, 1): -1.0, (1, 2): 2.0})

    def test_native_couplings_with_two_values_are_refused(self):
        import numpy as np

        def build(f):
            g = f.create_group("Ising")
            g["biases"] = np.zeros(2)
            c = g.create_group("couplings")
            c["I"] = np.array([1, 2])
            c["J"] = np.array([2, 1])
            c["V"] = np.array([1.0, 2.0])

        with self.assertRaisesRegex(ModelError, r"coupling \(0,1\) is given with two values"):
            load_model(path=self._write("twice.h5", build))

    def test_native_shapes_labels_and_values_are_checked(self):
        import numpy as np
        from veloxq_mcp import models

        def native(biases, I, J, V, labels=None, dense=None):
            def build(f):
                g = f.create_group("Ising")
                g["biases"] = np.asarray(biases, dtype=float)
                if dense is not None:
                    g["couplings"] = np.asarray(dense, dtype=float)
                else:
                    c = g.create_group("couplings")
                    c["I"], c["J"], c["V"] = np.asarray(I), np.asarray(J), np.asarray(V, dtype=float)
                if labels is not None:
                    g["labels"] = np.array(labels)
            return build

        cases = [
            (native([0, 0], [1, 2], [2], [1.0]), "vectors of one length"),
            (native([[0, 0]], [1], [2], [1.0]), "biases must be a vector"),
            (native([0, 0], [1], [2], [float("nan")]), "finite"),
            (native([0, 0, 0], [1], [2], [1.0], labels=[b"a"]), "1 labels for 3 variables"),
            (native([0, 0], None, None, None, dense=np.zeros((3, 3))), "must be 2x2"),
        ]
        for k, (build, message) in enumerate(cases):
            with self.subTest(message=message), self.assertRaisesRegex(ModelError, message):
                load_model(path=self._write(f"bad{k}.h5", build))
        with mock.patch.object(models, "MAX_VARIABLES", 2), \
                self.assertRaisesRegex(ModelError, "above the limit"):
            load_model(path=self._write("wide.h5", native([0, 0, 0], [1], [2], [1.0])))
        with mock.patch.object(models, "MAX_DENSE_ROWS", 1), \
                self.assertRaisesRegex(ModelError, "store the couplings as I, J, V"):
            load_model(path=self._write("dense.h5", native([0, 0], None, None, None,
                                                           dense=np.zeros((2, 2)))))

    def test_foreign_dense_matrix_has_a_row_limit(self):
        import numpy as np
        from veloxq_mcp import models

        def build(f):
            f["J"] = np.zeros((3, 3))

        with mock.patch.object(models, "MAX_DENSE_ROWS", 2), \
                self.assertRaisesRegex(ModelError, "above the limit of 2 rows"):
            load_model(path=self._write("big.h5", build))

    def test_native_diagonal_and_out_of_range_couplings(self):
        import numpy as np

        def build(f):
            g = f.create_group("Ising")
            g["biases"] = np.zeros(2)
            c = g.create_group("couplings")
            c["I"] = np.array([1, 1, 2])
            c["J"] = np.array([1, 2, 1])
            c["V"] = np.array([5.0, 1.0, 1.0])

        m = load_model(path=self._write("diag.h5", build))
        self.assertEqual(m.quadratic, {(0, 1): 1.0})
        self.assertIn("1 diagonal couplings ignored as constants", m.notes)

        def build_out(f):
            g = f.create_group("Ising")
            g["biases"] = np.zeros(2)
            c = g.create_group("couplings")
            c["I"] = np.array([1])
            c["J"] = np.array([3])
            c["V"] = np.array([1.0])

        with self.assertRaisesRegex(ModelError, "outside 1..2"):
            load_model(path=self._write("out.h5", build_out))

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

        with self.assertRaisesRegex(ModelError, "two values"):
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
