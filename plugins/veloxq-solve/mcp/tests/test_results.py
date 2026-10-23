"""Tests for veloxq_mcp.results: energy checks, top-K and the distribution."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from veloxq_mcp.models import model_from_spec  # noqa: E402
from veloxq_mcp.results import ResultError, objective_distribution, summarize  # noqa: E402


def sampleset(samples, energies, variables=None, vartype="SPIN", info=None):
    import dimod

    variables = list(range(len(samples[0]))) if variables is None else variables
    return dimod.SampleSet.from_samples((samples, variables), vartype, energy=energies,
                                        info=info or {})


class TestSummarize(unittest.TestCase):
    def setUp(self):
        # E(s) = -s0 s1 + 0.5 s1 s2 : ground states s0 = s1 = -s2
        self.model = model_from_spec({"type": "ising",
                                      "quadratic": [[0, 1, -1.0], [1, 2, 0.5]]})

    def test_recomputes_reorders_and_ranks(self):
        samples = [[1, 1, -1], [1, 1, 1], [-1, -1, 1], [1, -1, 1]]
        energies = [-1.5, -0.5, -1.5, 0.5]
        doc = summarize(sampleset(samples, energies), self.model, top=3)
        self.assertEqual(doc["best_objective"], -1.5)
        self.assertEqual(doc["num_samples_at_best"], 2)
        self.assertEqual(doc["num_samples"], 4)
        self.assertEqual(doc["vartype"], "SPIN")
        self.assertEqual([t["objective"] for t in doc["top"]], [-1.5, -1.5, -0.5])
        self.assertTrue(doc["energy_check"]["checked"])
        self.assertEqual(doc["energy_check"]["spread"], 0.0)
        self.assertEqual(doc["objective_distribution"],
                         {"values": [-1.5, -0.5, 0.5], "counts": [2, 1, 1]})

    def test_columns_follow_the_platform_variable_order(self):
        # Platform returns columns as (2, 0, 1): the same states, permuted.
        samples = [[-1, 1, 1], [1, -1, -1]]
        doc = summarize(sampleset(samples, [-1.5, -1.5], variables=[2, 0, 1]),
                        self.model, top=5)
        self.assertEqual(doc["best_sample"], [1, 1, -1])

    def test_constant_offset_is_tolerated_but_spread_is_not(self):
        samples = [[1, 1, -1], [1, 1, 1]]
        doc = summarize(sampleset(samples, [-1.5 + 7, -0.5 + 7]), self.model, top=1)
        self.assertEqual(doc["energy_check"]["offset"], 7.0)
        with self.assertRaisesRegex(ResultError, "non-constant"):
            summarize(sampleset(samples, [-1.5, 0.0]), self.model, top=1)

    def test_qubo_samples_are_reported_in_binary(self):
        qubo = model_from_spec({"type": "qubo", "quadratic": [[0, 1, 2.0]],
                                "linear": [-1.0, -1.0]})
        bqm = qubo.bqm()
        samples = [[1, 0], [0, 0], [1, 1]]
        energies = bqm.energies((samples, [0, 1]))
        # Platform works in spins; energies are the spin model's.
        spin = bqm.spin
        spin_samples = [[2 * x - 1 for x in s] for s in samples]
        doc = summarize(sampleset(spin_samples, spin.energies((spin_samples, [0, 1]))),
                        qubo, top=3)
        self.assertEqual(doc["vartype"], "BINARY")
        self.assertEqual(doc["best_sample"], [1, 0])
        self.assertAlmostEqual(doc["best_objective"], min(energies))

    def test_without_a_model_the_platform_energies_are_used(self):
        doc = summarize(sampleset([[1, -1], [1, 1]], [-2.0, 1.0]), None, top=5)
        self.assertFalse(doc["energy_check"]["checked"])
        self.assertEqual(doc["best_objective"], -2.0)
        self.assertEqual(doc["best_sample"], [1, -1])
        self.assertEqual(doc["num_variables"], 2)

    def test_unexpected_variables_are_an_error(self):
        with self.assertRaisesRegex(ResultError, "unexpected variables"):
            summarize(sampleset([[1, 1, 1]], [0.0], variables=["x", 0, 1]),
                      self.model, top=1)
        with self.assertRaisesRegex(ResultError, "expected 0..2"):
            summarize(sampleset([[1, 1]], [0.0], variables=[0, 1]), self.model, top=1)

    def test_degeneracy_warning(self):
        flat = model_from_spec({"type": "ising", "quadratic": [[0, 1, 0.0], [1, 2, 0.0]]})
        samples = [[1, 1, 1], [1, -1, 1], [-1, 1, 1], [-1, -1, -1]]
        doc = summarize(sampleset(samples, [0.0] * 4), flat, top=2)
        self.assertEqual(len(doc["warnings"]), 1)
        self.assertIn("distinct states tie", doc["warnings"][0])
        self.assertEqual(len(doc["top"]), 2)


class TestDistribution(unittest.TestCase):
    def test_histogram_when_many_distinct_values(self):
        dist = objective_distribution(list(range(100)), max_distinct=10, bins=5)
        self.assertEqual(len(dist["bin_edges"]), 6)
        self.assertEqual(sum(dist["counts"]) + dist["above"], 100)

    def test_ties_within_tolerance_collapse(self):
        dist = objective_distribution([1.0, 1.0 + 1e-12, 2.0])
        self.assertEqual(dist, {"values": [1.0, 2.0], "counts": [2, 1]})


if __name__ == "__main__":
    unittest.main()
