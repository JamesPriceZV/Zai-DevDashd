"""Pure feed/forecast regressions: no Engine, models, network or device calls."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
MODULE = Path(__file__).resolve().parents[1] / 'src/zai_devdashd/executive_live.py'
spec = importlib.util.spec_from_file_location('executive_live_test', MODULE)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
NOW = datetime(2026, 11, 6, 18, tzinfo=timezone.utc).timestamp()
TEST_PRODUCER = 'synthetic-coordinator'
SHA = 'a' * 40
DIGEST = 'b' * 64

def history(kind='build', count=5):
    return [{'kind': kind, 'machine': 'Mini', 'profile': 'release', 'outcome': 'passed', 'source_sha': SHA, 'receipt_sha256': format(i + 1, '064x'), 'started_at': NOW - 10000 - i * 1000, 'completed_at': NOW - 10000 - i * 1000 + 100 * (i + 1)} for i in range(count)]

def operation(identity='build', state='queued'):
    return {'id': identity, 'kind': 'build', 'machine': 'Mini', 'profile': 'release', 'title': 'Prepare the tester build', 'state': state, 'depends_on': []}

def document():
    return {'schema': live.SCHEMA, 'producer_chat_id': TEST_PRODUCER, 'observed_at': NOW, 'candidate': {'sha': SHA, 'build': '3545.0'}, 'features': [], 'release_checks': [], 'operations': [], 'history': history(), 'blockers': []}

class ExecutiveLiveTests(unittest.TestCase):

    def test_storage_completion_preserves_receipt_without_inventing_git_identity(self):
        for kind, expected in (('storage', 'completed'), ('maintenance', 'completed'), ('build', 'unreported'), ('upload', 'unreported')):
            doc = document()
            doc['operations'] = [dict(operation(state='completed'), kind=kind, receipt_sha256=DIGEST)]
            row = live.project(doc, NOW, producer=TEST_PRODUCER)['operations'][0]
            self.assertEqual(row['state'], expected)
            self.assertIsNone(row['source_sha'])
            if expected == 'completed':
                self.assertEqual(row['receipt_sha256'], DIGEST)
                self.assertIsNone(row['estimate']['remaining_high_s'])
        doc['operations'][0].update(kind='storage', receipt_sha256='missing')
        self.assertEqual(live.project(doc, NOW, producer=TEST_PRODUCER)['operations'][0]['state'], 'unreported')

    def test_terminal_operations_never_forecast_future_work(self):
        for state in ('completed', 'failed', 'cancelled'):
            result = live.estimate(operation(state=state), history(), NOW)
            self.assertEqual(result['state'], state)
            self.assertIsNone(result['remaining_high_s'])
            self.assertIsNone(result['finish_high'])
            self.assertNotIn('once', result['condition'])

    def test_queued_duration_does_not_imply_a_scheduled_finish(self):
        result = live.estimate(operation(), history(), NOW)
        self.assertEqual(result['samples'], 5)
        self.assertEqual((result['remaining_low_s'], result['remaining_high_s']), (300, 500))
        self.assertIsNone(result['finish_high'])
        self.assertEqual(result['state'], 'conditional')

    def test_only_comparable_unique_successful_bound_runs_count(self):
        rows = history()
        rows += [dict(rows[0]), dict(rows[1], outcome='failed'), dict(rows[2], machine='Worker'), dict(rows[3], profile='debug'), dict(rows[4], receipt_sha256='missing')]
        result = live.estimate(operation(), rows, NOW)
        self.assertEqual(result['samples'], 5)
        self.assertEqual(result['remaining_high_s'], 500)

    def test_sparse_history_and_overrun_remain_uncertain(self):
        self.assertEqual(live.estimate(operation(), history(count=2), NOW)['state'], 'unavailable')
        running = dict(operation(state='running'), started_at=NOW - 600)
        result = live.estimate(running, history(), NOW)
        self.assertEqual(result['state'], 'overrun')
        self.assertIsNone(result['remaining_high_s'])

    def test_confirmed_schedule_and_dependency_ranges_are_separate(self):
        doc = document()
        first = dict(operation(), state='running', started_at=NOW - 100)
        second = dict(operation('upload', 'scheduled'), depends_on=['build'], schedule_confirmed=True, scheduled_at='2026-11-06T13:01:00-05:00')
        doc['operations'] = [first, second]
        rows = live.project(doc, NOW, producer=TEST_PRODUCER)['operations']
        self.assertEqual(rows[0]['estimate']['finish_low'], NOW + 200)
        self.assertEqual(rows[0]['estimate']['finish_high'], NOW + 400)
        self.assertEqual(rows[1]['estimate']['finish_low'], NOW + 500)
        self.assertEqual(rows[1]['estimate']['finish_high'], NOW + 900)

    def test_unconfirmed_expired_missing_or_cyclic_dependency_has_no_date(self):
        for changes in ({}, {'schedule_confirmed': True, 'scheduled_at': '2026-11-06T12:00:00-05:00'}, {'schedule_confirmed': True, 'scheduled_at': '2026-11-06T14:00:00-05:00', 'depends_on': ['absent']}, {'schedule_confirmed': True, 'scheduled_at': '2026-11-06T14:00:00-05:00', 'depends_on': ['build']}):
            doc = document()
            doc['operations'] = [dict(operation(state='scheduled'), **changes)]
            self.assertIsNone(live.project(doc, NOW, producer=TEST_PRODUCER)['operations'][0]['estimate']['finish_high'])

    def test_completed_shared_dependency_satisfies_both_children(self):
        doc = document()
        done = dict(operation('done', 'completed'), source_sha=SHA, receipt_sha256=DIGEST)
        children = [dict(operation(k, 'scheduled'), depends_on=['done'], schedule_confirmed=True, scheduled_at='2026-11-06T13:01:00-05:00') for k in ('one', 'two')]
        doc['operations'] = [done] + children
        rows = live.project(doc, NOW, producer=TEST_PRODUCER)['operations']
        self.assertTrue(all((row['estimate']['finish_high'] for row in rows[1:])))

    def test_historical_and_unbound_checks_cannot_pass_current_candidate(self):
        doc = document()
        doc['release_scope_complete'] = True
        doc['release_checks'] = [{'id': 'old', 'required': True, 'state': 'passed', 'source_sha': 'c' * 40, 'receipt_sha256': DIGEST}, {'id': 'bare', 'required': True, 'state': 'passed', 'source_sha': SHA}, {'id': 'current', 'required': True, 'state': 'passed', 'source_sha': SHA, 'receipt_sha256': DIGEST}]
        result = live.project(doc, NOW, producer=TEST_PRODUCER)
        self.assertEqual([r['state'] for r in result['release_checks']], ['other_revision', 'unreported', 'passed'])
        self.assertEqual(result['current_checks_reported_passed'], 1)
        self.assertEqual(result['required_checks'], 3)
        self.assertIn('Owner-recorded', result['readiness'])

    def test_duplicate_truncated_or_unapproved_scopes_never_create_denominator(self):
        doc = document()
        doc['scope_complete'] = True
        feature = {'id': 'research', 'state': 'accepted', 'source_sha': SHA, 'receipt_sha256': DIGEST}
        doc['features'] = [feature, feature]
        self.assertIsNone(live.project(doc, NOW, producer=TEST_PRODUCER)['feature_total'])
        doc['features'] = [dict(feature, id='f' + str(i)) for i in range(65)]
        self.assertIsNone(live.project(doc, NOW, producer=TEST_PRODUCER)['feature_total'])
        doc['features'] = [feature]
        doc['scope_complete'] = False
        self.assertIsNone(live.project(doc, NOW, producer=TEST_PRODUCER)['feature_total'])
        doc['release_scope_complete'] = True
        doc['release_checks'] = [{'id': 'same', 'required': True}] * 2
        self.assertIsNone(live.project(doc, NOW, producer=TEST_PRODUCER)['required_checks'])

    def test_stale_source_withholds_absolute_forecasts(self):
        doc = document()
        doc['observed_at'] = NOW - 601
        doc['operations'] = [dict(operation(state='running'), started_at=NOW - 100)]
        result = live.project(doc, NOW, producer=TEST_PRODUCER)
        self.assertTrue(result['stale'])
        self.assertIsNone(result['operations'][0]['estimate']['finish_high'])

    def test_planning_outlook_is_distinct_from_empirical_eta_and_inventory_progress(self):
        doc = document()
        doc['inventory'] = {'common': 2, 'B': 11, 'C': 2, 'D': 60, 'conditional': 4, 'total': 79}
        doc['planning_forecasts'] = [{'id': 'B', 'label': 'Tester delivery', 'start_date': '2026-11-07', 'end_date': '2026-11-08', 'confidence': 'low', 'conditions': ['Required checks pass']}]
        result = live.project(doc, NOW, producer=TEST_PRODUCER)
        self.assertEqual(result['inventory']['total'], 79)
        self.assertIsNone(result['feature_total'])
        self.assertEqual(result['planning_forecasts'][0]['confidence'], 'low')
        self.assertIn('not an empirical', result['planning_forecasts'][0]['basis'])
        doc['inventory']['total'] = 78
        self.assertIsNone(live.project(doc, NOW, producer=TEST_PRODUCER)['inventory'])

    def test_wrong_owner_and_missing_timezone_are_rejected(self):
        for changes in ({'producer_chat_id': 'some-other-chat'}, {'observed_at': '2026-11-06T13:00:00'}):
            with self.assertRaises(ValueError):
                live.project(dict(document(), **changes), NOW, producer=TEST_PRODUCER)

    def test_failed_read_retains_evidence_but_not_healthy_state_or_eta(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'owner.json'
            doc = document()
            doc['operations'] = [dict(operation(state='running'), started_at=NOW - 100)]
            path.write_text(json.dumps(doc))
            path.chmod(384)
            cache = live.Cache(path, producer=TEST_PRODUCER)
            first = cache.get(NOW)
            self.assertFalse(first['stale'])
            path.write_text('invalid')
            failed = cache.get(NOW + 6)
            again = cache.get(NOW + 7)
            for result in (failed, again):
                self.assertTrue(result['stale'])
                self.assertTrue(result['error'])
                self.assertEqual(result['observed_at'], NOW)
                self.assertIsNone(result['operations'][0]['estimate']['finish_high'])
            path.write_text(json.dumps(doc))
            path.chmod(384)
            self.assertIsNone(cache.get(NOW + 12)['error'])

    def test_private_file_custody_duplicates_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'owner.json'
            path.write_text(json.dumps(document()))
            path.chmod(420)
            with self.assertRaises(ValueError):
                live.read_document(path)
            path.chmod(384)
            path.write_text('{"schema":1,"schema":2}')
            with self.assertRaises(ValueError):
                live.read_document(path)
            link = Path(temp) / 'alias.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                live.read_document(link)
if __name__ == '__main__':
    unittest.main()
