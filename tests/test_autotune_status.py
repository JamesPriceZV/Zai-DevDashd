"""Producer metadata checks only; no controller execution or model calls."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from zai_devdashd import autotune_status as status
NOW = 991838000
POLICY_A = 'a' * 64
POLICY_B = 'b' * 64
TEST_PRODUCER = 'synthetic-autotune'

def document():
    return {'producer_chat_id': TEST_PRODUCER, 'policySHA256': POLICY_A, 'status': 'halted; no retry', 'lastProgressLocal': NOW - 1000, 'completed': 0, 'nativePID': None, 'actualJob': None, 'counts': {'valid': 7, 'historicalFailed': 1, 'newFailed': 0, 'attempted': 8}, 'blockReason': 'Unknown process visibility for PID 40166'}

class AutoTuneStatusTests(unittest.TestCase):

    def test_c4_adopted_lineage_keeps_baseline_and_memory_halt_scope(self):
        doc = document()
        doc.update(policySHA256=POLICY_B, blockReason='memory below admitted floor; private diagnostics', ownedClosure=False, actualJob={'id': 'synthetic-job', 'position': 4})
        doc['counts'].update(attempted=9, newFailed=1)
        result = status.project(doc, POLICY_B, NOW, producer=TEST_PRODUCER, historical_valid=7)
        self.assertEqual(result['historical_valid'], 7)
        self.assertEqual(result['new_valid'], 0)
        self.assertEqual(result['new_failed'], 1)
        self.assertEqual(result['recorded_attempts'], 9)
        self.assertEqual(result['reported_job_id'], 'synthetic-job')
        self.assertIsNone(result['reported_job'])
        self.assertFalse(result['native_execution_verified'])
        self.assertIn('Cleanup has not been confirmed', result['blocker'])
        self.assertNotIn('private diagnostics', str(result))

    def test_halted_wins_without_native_or_benchmark_claim(self):
        result = status.project(document(), POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(result['native_execution_verified'])
        self.assertEqual(result['new_valid'], 0)
        self.assertEqual(result['historical_valid'], 7)
        self.assertEqual(result['historical_failed'], 1)
        self.assertIsNone(result['reported_job'])

    def test_publication_clock_never_becomes_progress_or_read_clock(self):
        first = status.project(document(), POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)
        second = status.project(document(), POLICY_A, NOW + 3600, producer=TEST_PRODUCER, historical_valid=7)
        self.assertEqual(first['status_publication_at'], second['status_publication_at'])
        self.assertNotEqual(first['checked_at'], second['checked_at'])
        self.assertIsNone(second['actual_progress_at'])
        self.assertTrue(second['legacy_publication_clock'])

    def test_lagged_counts_are_not_subtracted_or_cross_epoch_pooled(self):
        doc = document()
        doc['completed'] = 5
        result = status.project(doc, POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)
        self.assertEqual(result['historical_valid'], 7)
        self.assertEqual(result['recorded_valid_total'], 7)
        self.assertEqual(result['new_valid'], 5)
        self.assertTrue(result['counts_may_lag'])
        self.assertFalse(result['inflight_in_counts'])

    def test_missing_optional_counts_stay_unknown(self):
        doc = document()
        del doc['counts']
        result = status.project(doc, POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)
        self.assertIsNone(result['recorded_attempts'])
        self.assertIsNone(result['recorded_valid_total'])
        self.assertIsNone(result['new_failed'])

    def test_unmatched_policy_and_bad_clock_are_refused(self):
        with self.assertRaises(ValueError):
            status.project(document(), 'c' * 64, NOW, producer=TEST_PRODUCER, historical_valid=7)
        doc = document()
        doc['lastProgressLocal'] = '207-11-06T13:9:03'
        with self.assertRaises(ValueError):
            status.project(doc, POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)

    def test_overlay_keeps_owner_clock_and_intent_but_changes_operational_state(self):
        owner = {'observed_at': NOW - 1000, 'operations': [{'id': 'autotune', 'state': 'queued'}]}
        result = status.overlay(owner, status.project(document(), POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7))
        self.assertEqual(result['observed_at'], NOW - 1000)
        self.assertEqual(result['operations'][0]['state'], 'blocked')
        self.assertEqual(result['operations'][0]['owner_reported_state'], 'queued')
        self.assertEqual(owner['operations'][0]['state'], 'queued')
        self.assertIsNone(result['operations'][0]['estimate']['finish_high'])

    def test_old_active_publication_cannot_revive_new_terminal_work(self):
        for state in ('completed', 'cancelled', 'failed'):
            owner = {'observed_at': NOW, 'operations': [{'id': 'autotune', 'state': state}]}
            doc = document()
            doc['status'] = 'running job'
            observed = status.project(doc, POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)
            self.assertTrue(observed['stale'])
            result = status.overlay(owner, observed)
            self.assertEqual(result['operations'][0]['state'], state)
            self.assertFalse(result['operations'][0]['controller_applied'])
            self.assertEqual(result['operational_sources']['autotune']['state'], 'running')

    def test_fresh_publication_still_cannot_override_newer_terminal_owner(self):
        doc = document()
        doc['status'] = 'running job'
        doc['lastProgressLocal'] = NOW - 1
        observed = status.project(doc, POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7)
        self.assertFalse(observed['stale'])
        owner = {'observed_at': NOW, 'operations': [{'id': 'autotune', 'state': 'completed'}]}
        self.assertEqual(status.overlay(owner, observed)['operations'][0]['state'], 'completed')

    def test_stale_active_metadata_cannot_replace_queued_owner_but_halt_is_retained(self):
        doc = document()
        doc['status'] = 'running job'
        owner = {'observed_at': NOW, 'operations': [{'id': 'autotune', 'state': 'queued'}]}
        result = status.overlay(owner, status.project(doc, POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7))
        self.assertEqual(result['operations'][0]['state'], 'queued')
        halted = status.overlay(owner, status.project(document(), POLICY_A, NOW, producer=TEST_PRODUCER, historical_valid=7))
        self.assertEqual(halted['operations'][0]['state'], 'blocked')
        self.assertTrue(halted['operations'][0]['controller']['stale'])
if __name__ == '__main__':
    unittest.main()
