"""Inert checks of the page's real metadata helpers; no server or probes run."""
import json
from pathlib import Path
import subprocess
import unittest


PAGE = Path(__file__).resolve().parents[1] / 'src/zai_devdashd/static/page.html'


def actual_helpers():
    text = PAGE.read_text()
    start = text.index('  function coverageTime(value) {')
    end = text.index('  function renderAchievementCoverage(', start)
    return text[start:end]


def run_checks(body):
    script = "const assert = require('node:assert/strict');\n" + actual_helpers() + '\n' + body
    result = subprocess.run(['node', '-e', script], check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    return json.loads(result.stdout)


class StatusCoverageTests(unittest.TestCase):
    def test_read_time_does_not_replace_producer_change_time(self):
        result = run_checks(r'''
const now = Date.parse('2026-11-06T12:00:00-05:00') / 1000;
const old = Date.parse('2026-11-02T15:00:00-05:00') / 1000;
const rows = achievementCoverageRows({github:{state:'available',observed_at:now},
  gates:{state:'available',observed_at:now,source_updated_at:old,source_updated_at_kind:'file_mtime'},
  testflight:{state:'available',observed_at:now,source_updated_at:old,source_updated_at_kind:'directory_mtime'}});
assert.deepEqual(rows.map(r=>r.name), ['GitHub','Gate journal','Upload logs']);
assert.equal(rows[0].changed,'Unreported');
assert.equal(rows[1].checked,coverageTime(now));
assert.equal(rows[1].changed,coverageTime(old));
assert.notEqual(rows[1].checked,rows[1].changed);
assert.equal(rows[1].changedLabel,'File changed');
assert.equal(rows[2].changedLabel,'Folder changed');
console.log(JSON.stringify({checks:7}));
''')
        self.assertEqual(result['checks'], 7)

    def test_invalid_and_missing_times_remain_unreported(self):
        result = run_checks(r'''
const bad = [null,undefined,NaN,Infinity,-Infinity,true,false,[],{},'', '1793985817',
 'not a date','2026-11-06T12:00:00','2026-02-30T12:00:00Z','2026-13-01T12:00:00Z',
 '2026-11-06T25:00:00Z',1e20,{valueOf(){throw Error('must not coerce metadata');}}];
bad.forEach(value=>assert.equal(coverageTime(value),'Unreported'));
[null,undefined,[],false,'private/path'].forEach(value=>{
 const rows=achievementCoverageRows(value);
 assert.equal(rows.length,3);
 rows.forEach(row=>{assert.equal(row.checked,'Unreported');assert.equal(row.changed,'Unreported');assert.equal(row.status,'Unreported');});
});
console.log(JSON.stringify({invalidTimes:bad.length}));
''')
        self.assertEqual(result['invalidTimes'], 18)

    def test_error_and_stale_sources_preserve_clocks_without_disclosing_paths(self):
        result = run_checks(r'''
const old = Date.parse('2026-11-02T15:00:00-05:00')/1000;
const now = Date.parse('2026-11-06T12:00:00-05:00')/1000;
const rows = achievementCoverageRows({github:{state:'available',error:'private/path token',source:'private/path',observed_at:old,attempted_at:now},
 gates:{state:'available',stale:true,observed_at:old,source_updated_at:old},
 testflight:{state:'not_available',observed_at:now}});
assert.equal(rows[0].status,'Read error');assert.equal(rows[0].checked,coverageTime(old));
assert.equal(rows[0].attempted,coverageTime(now));assert.equal(rows[0].changed,'Unreported');
assert.equal(rows[1].status,'Stale read');assert.equal(rows[2].status,'Unreported');
assert(!JSON.stringify(rows).includes('private/path'));assert(!JSON.stringify(rows).includes('token'));
console.log(JSON.stringify({checks:8}));
''')
        self.assertEqual(result['checks'], 8)

    def test_explicit_dates_match_epochs_and_dst_is_automatic(self):
        result = run_checks(r'''
const before='2026-10-31T12:00:00-04:00', after='2026-11-06T12:00:00-05:00';
assert.equal(coverageTime(before),coverageTime(Date.parse(before)/1000));
assert.equal(coverageTime(after),coverageTime(Date.parse(after)/1000));
assert(coverageTime(before).includes('UTC-04:00'));assert(coverageTime(after).includes('UTC-05:00'));
assert.notEqual(coverageTime('2024-02-29T12:00:00Z'),'Unreported');
assert.equal(coverageTime('2026-02-29T12:00:00Z'),'Unreported');
console.log(JSON.stringify({checks:6}));
''')
        self.assertEqual(result['checks'], 6)


if __name__ == '__main__':
    unittest.main()
