"""Resource decisions use supplied observations and signal no process."""
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zai_devdashd.model_resources import Guard


class GuardTests(unittest.TestCase):
    def test_no_sample_is_unknown_not_a_zero_measurement(self):
        g = Guard(lambda: {(1, "old"): 10})
        self.assertIsNone(g.receipt()["client_peak_rss_bytes"])
        self.assertIsNone(g.receipt()["conservative_positive_host_rss_growth_bytes"])
        self.assertEqual(g.receipt()["client_observations"], 0)

    def guard(self, first, second):
        rows = iter((first, second))
        return Guard(lambda: next(rows))

    def test_freed_memory_cannot_offset_new_growth(self):
        g = self.guard({(1, "old"): 8 * 1024**3}, {(1, "old"): 1, (2, "new"): 5 * 1024**3})
        with self.assertRaises(RuntimeError): g.check(2)

    def test_reused_pid_is_new_memory_not_old_identity_credit(self):
        g = self.guard({(2, "old"): 8 * 1024**3}, {(2, "new"): 5 * 1024**3})
        with self.assertRaises(RuntimeError): g.check(2)

    def test_client_bound_applies_even_when_host_growth_is_small(self):
        g = self.guard({}, {(2, "new"): 257 * 1024**2})
        with self.assertRaises(RuntimeError): g.check(2)

    def test_unknown_client_refuses_and_known_client_records_real_peaks(self):
        with self.assertRaises(ProcessLookupError): self.guard({}, {(1, "old"): 2}).check(2)
        g = self.guard({(1, "old"): 50}, {(1, "old"): 40, (2, "new"): 10})
        g.check(2)
        self.assertEqual(g.receipt()["conservative_positive_host_rss_growth_bytes"], 10)
        self.assertEqual(g.receipt()["client_peak_rss_bytes"], 10)


if __name__ == "__main__": unittest.main()
