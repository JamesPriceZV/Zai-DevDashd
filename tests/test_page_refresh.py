"""Exercise the shipped polling function without a browser or network access."""

from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


PAGE = Path(__file__).resolve().parents[1] / "src/zai_devdashd/static/page.html"


def run_polling_case(case):
    html = PAGE.read_text()
    match = re.search(r"^  function startPoll\(\) \{.*?^  \}", html, re.M | re.S)
    assert match is not None, "actual dashboard polling function is missing"
    harness = r"""
const assert = require('node:assert/strict');
var pollTimer = null;
var live = {textContent: 'live'};
const intervals = [];
const replies = [];
const requests = [];
const rendered = [];
let visible = {version: 'previous'};

function setInterval(callback, milliseconds) {
  intervals.push({callback, milliseconds});
  return intervals.length;
}
function fetch(url, options) {
  requests.push({url, options});
  assert.equal(url, '/api/state');
  assert.equal(options.cache, 'no-store');
  assert.ok(replies.length, 'unexpected polling request');
  return replies.shift()();
}
function render(snapshot) {
  if (snapshot.rejectRender) throw new Error('fixture render failure');
  visible = snapshot;
  rendered.push(snapshot);
}
function response(snapshot, status = 200) {
  return () => Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(snapshot),
  });
}
function failedRequest() {
  return Promise.reject(new Error('fixture connection failure'));
}
function invalidJson() {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.reject(new SyntaxError('fixture invalid JSON')),
  });
}
function settled() {
  return new Promise(resolve => setImmediate(resolve));
}
async function tick() {
  assert.equal(intervals.length, 1);
  intervals[0].callback();
  await settled();
}
function expectFailure() {
  assert.match(live.textContent, /fail|retry|unavailable|error/i,
    'failed polling must report a connection error');
}
function expectPolling() {
  assert.match(live.textContent, /poll/i,
    'recovered polling must identify its update mode');
  assert.doesNotMatch(live.textContent, /fail|retry|unavailable|error/i,
    'successful polling must clear the previous connection error');
}
"""
    script = harness + "\n" + match.group(0) + "\n" + r"""
(async () => {
""" + case + r"""
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "--max-old-space-size=64", "-e", script],
        capture_output=True, text=True, timeout=2,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_polling_failure_recovers_the_status_after_rendering_new_data():
    run_polling_case(r"""
replies.push(failedRequest, response({version: 'recovered'}));
startPoll();
await settled();
expectFailure();
assert.equal(visible.version, 'previous');
assert.equal(rendered.length, 0);
await tick();
assert.equal(visible.version, 'recovered');
assert.equal(rendered.length, 1);
expectPolling();
""")


def test_non_success_response_does_not_replace_the_last_good_snapshot():
    run_polling_case(r"""
replies.push(response({version: 'first'}),
  response({version: 'error-body'}, 503), response({version: 'recovered'}));
startPoll();
await settled();
assert.equal(visible.version, 'first');
await tick();
assert.equal(visible.version, 'first', 'an HTTP error body is not a new snapshot');
assert.equal(rendered.length, 1);
expectFailure();
await tick();
assert.equal(visible.version, 'recovered');
assert.equal(rendered.length, 2);
expectPolling();
""")


def test_invalid_json_keeps_the_snapshot_and_can_recover():
    run_polling_case(r"""
replies.push(response({version: 'first'}), invalidJson,
  response({version: 'recovered'}));
startPoll();
await settled();
await tick();
assert.equal(visible.version, 'first');
assert.equal(rendered.length, 1);
expectFailure();
await tick();
assert.equal(visible.version, 'recovered');
expectPolling();
""")


def test_render_failure_is_not_a_healthy_poll_and_can_recover():
    run_polling_case(r"""
replies.push(response({version: 'first'}),
  response({version: 'unrendered', rejectRender: true}),
  response({version: 'recovered'}));
startPoll();
await settled();
await tick();
assert.equal(visible.version, 'first');
assert.equal(rendered.length, 1);
expectFailure();
await tick();
assert.equal(visible.version, 'recovered');
expectPolling();
""")


def test_start_polling_twice_does_not_duplicate_requests_or_intervals():
    run_polling_case(r"""
replies.push(response({version: 'first'}), response({version: 'second'}));
startPoll();
startPoll();
await settled();
assert.equal(requests.length, 1);
assert.equal(intervals.length, 1);
assert.equal(intervals[0].milliseconds, 5000);
await tick();
startPoll();
await settled();
assert.equal(requests.length, 2);
assert.equal(intervals.length, 1);
assert.equal(visible.version, 'second');
""")


def test_external_decision_target_is_explicit_and_https_only():
    html = PAGE.read_text()
    match = re.search(r"^  function decisionTarget\(value\) \{.*?^  \}", html, re.M | re.S)
    assert match is not None
    script = match.group(0) + r"""
const assert = require('node:assert/strict');
[undefined,null,'',42,'https://','javascript:alert(1)','vnc://example.invalid',
 'https://user:password@example.invalid/','https://example.invalid/\npath',
 ' https://example.invalid/'].forEach(value => assert.equal(decisionTarget(value), ''));
assert.equal(decisionTarget('https://example.invalid/decisions'), 'https://example.invalid/decisions');
"""
    result = subprocess.run(["node", "--max-old-space-size=64", "-e", script],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "renderDecisions(state.decisions, state.decision_link)" in html


def test_public_static_assets_are_original_local_svg_files():
    html = PAGE.read_text()
    references = set(re.findall(r'(?:src|href)="(/static/[^"?#]+)"', html))
    assert references == {"/static/logo.svg", "/static/favicon.svg"}
    for reference in references:
        root = ET.fromstring((PAGE.parent / reference.rsplit("/", 1)[-1]).read_text())
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert not any(node.tag.endswith("script") or node.tag.endswith("image") for node in root.iter())
    assert not re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", html, re.I)
    assert not re.search(r"/(?:Users|Volumes)/[^\s\"<>]+", html)


def test_generic_labels_and_configured_settings_do_not_invent_targets():
    from zai_devdashd import labels
    import copy
    cfg = copy.deepcopy(labels.DEFAULT_CONFIG)
    cfg["mini"]["name"] = "Example <local>"
    cfg["worker"]["name"] = "Example worker"
    html = labels.settings_page(cfg, "synthetic-csrf", "", "", "", "").decode()
    assert "Example &lt;local&gt;" in html and "Example worker" in html
    assert "/static/logo.svg" in html and "/static/favicon.svg" in html
    assert 'name="worker_host" type="text" value="" autocomplete="off">' in html
    assert "PHONE_BFT" in labels.CANON_STEPS
    assert labels.device_who("phone") == "Phone"
    assert labels.device_who("Example tablet") == "Example tablet"
    assert labels.match_rfb(["Example worker"], "example-worker.local") == ""
    assert labels.match_rfb(["example-worker"], "example-worker.local") == "example-worker"
    held = labels.pr_wait({"number": 31, "hold": "UNIT-BLOCKED"})
    assert held["text"].count("#") == 1 and "recorded merge hold" in held["text"]


def test_browser_fixture_uses_only_synthetic_current_record_inputs():
    import importlib.util
    path = Path(__file__).with_name("browser_executive.py")
    spec = importlib.util.spec_from_file_location("synthetic_browser_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = module.collector_current_fixture()
    assert current["parent_pr"]["number"] == 10
    assert current["parent_pr"]["draft"] is True
    assert current["reopened_issues"] == [20]


def test_explicit_phone_destination_is_not_classified_as_mac_unit_tests():
    from zai_devdashd import labels
    phone = labels.lane_activity("", "xcodebuild test -destination platform=iOS,name=SyntheticPhone",
                                 "tool", True, None)
    mac = labels.lane_activity("", "xcodebuild test -destination platform=macOS",
                               "tool", True, None)
    assert "Mac unit tests" not in phone and "Running tests" in phone
    assert "Mac unit tests" in mac


def test_missing_external_records_do_not_create_objectives_zeroes_or_source_times():
    html = PAGE.read_text()
    functions = []
    for name in ("renderExecutiveHero", "renderGoalCards"):
        match = re.search(r"^  function " + name + r"\(state\) \{.*?^  \}", html, re.M | re.S)
        assert match is not None
        functions.append(match.group(0))
    count = re.search(r"^  function executiveCount\(value\).*?$", html, re.M)
    script = r"""
const assert = require('node:assert/strict');
class Node {
  constructor(tag,cls,text){this.tag=tag;this.cls=cls;this.textContent=text==null?'':String(text);this.children=[];this.dataset={};}
  appendChild(node){this.children.push(node);return node;}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(){} addEventListener(){}
  querySelectorAll(tag){return this.children.flatMap(n=>[...(n.tag===tag?[n]:[]),...n.querySelectorAll(tag)]);}
}
const document={createElement:tag=>new Node(tag)};
const el=(tag,cls,text)=>new Node(tag,cls,text);
const executiveData=state=>state.executive_dashboard||{};
const executiveHeading=(title,text)=>el('div',null,text);
let executiveJourney='delivery';
function allText(node){return [node.textContent,...node.children.map(allText)].join(' ');}
""" + count.group(0) + "\n" + "\n".join(functions) + r"""
const state={server_time_et:'SYNTHETIC_POLL_TIME',executive_dashboard:{blockers:[],goals:[]}};
const hero=renderExecutiveHero(state);
const values=hero.querySelectorAll('button').map(button=>button.children[1].textContent);
assert.deepEqual(values,['—','—','—','—']);
assert.match(allText(hero),/Source records as of unreported/);
assert.ok(!allText(hero).includes('SYNTHETIC_POLL_TIME'));
const goals=renderGoalCards(state);
assert.match(allText(goals),/No configured workflow objectives/);
assert.equal(goals.querySelectorAll('article').length,0);
"""
    result = subprocess.run(["node", "--max-old-space-size=64", "-e", script],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 0, result.stdout + result.stderr


def test_merge_scope_caption_uses_reported_branches_or_unknown():
    html = PAGE.read_text()
    match = re.search(r"^  function mergedBranchCaption\(coverage\) \{.*?^  \}", html, re.M | re.S)
    assert match is not None
    script = "const assert = require('node:assert/strict');\n" + match.group(0) + r"""
assert.equal(mergedBranchCaption({merged_bases:['trunk','release/v2']}), 'Into trunk / release/v2');
assert.equal(mergedBranchCaption({merged_bases:['trunk','trunk']}), 'Into trunk');
for (const value of [undefined, {}, {merged_bases:[]}, {merged_bases:['']}, {merged_bases:[5]}, {merged_bases:['bad\nbranch']}]) {
  assert.equal(mergedBranchCaption(value), 'Target branches unreported');
}
assert.ok(!mergedBranchCaption({}).includes('main'));
"""
    result = subprocess.run(["node", "--max-old-space-size=64", "-e", script], capture_output=True, text=True, timeout=2)
    assert result.returncode == 0, result.stdout + result.stderr
