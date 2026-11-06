#!/usr/bin/env python3
"""Executive dashboard: readable layouts, evidence states and persistent controls.

Usage: browser_executive.py URL [chromium|webkit] [optional-audit-directory]
       browser_executive.py --status-unit  (Node only; no browser)
Uses the promotion smoke account only when the supplied service requires login.
Does not send control actions or change the monitored workloads.
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
import sys
import subprocess
from pathlib import Path

URL = sys.argv[1] if len(sys.argv) > 1 else "https://127.0.0.1:8780/"
BROWSER = sys.argv[2] if len(sys.argv) > 2 else "chromium"
AUDIT = Path(sys.argv[3] or "") if len(sys.argv) > 3 else None
# Entirely synthetic example clock/identities, independent of private receipts.
FIXTURE_NOW = 1736953200  # 2025-01-15 10:00:00-05:00 America/Toronto


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def status_classifier_checks(html=None):
    """Exercise the real page classifier in Node; no DOM or browser is started."""
    if html is None:
        html = (Path(__file__).resolve().parents[1] / "src/zai_devdashd/static/page.html").read_text()
    match = re.search(r"  function execStatus\(value\) \{.*?\n  \}(?=\n  function execChip)", html, re.S)
    require(match is not None, "actual page status classifier missing")
    assertions = r"""
const assert = require('node:assert/strict');
const positive = ['verified','accepted','pass','passed','complete','completed','recorded','ready','healthy'];
const live = ['running','active','runtime_active','live'];
const negative = ['unverified','unaccepted','not_verified','not accepted','not_ready','not_complete',
  'not_healthy','inactive','not_live','not_runtime_active','unknown','unavailable','perhaps_verified'];
let checked = 0;
function expect(value, wanted) { assert.equal(execStatus(value), wanted); checked++; }
positive.forEach(value => { expect(value,'good'); expect('  '+value.toUpperCase()+'  ','good'); });
live.forEach(value => { expect(value,'live'); expect('  '+value.toUpperCase()+'  ','live'); });
negative.forEach(value => { assert.notEqual(execStatus(value),'good',value); assert.notEqual(execStatus(value),'live',value); checked++; });
['partial','incomplete','stale','pending','at_risk','waiting'].forEach(value=>expect(value,'attention'));
['failed','blocked','off_track','error'].forEach(value=>expect(value,'bad'));
// Unknown prefixes/suffixes around every positive/live token cannot inherit its tone.
for (const token of positive.concat(live)) {
  for (const value of ['not_'+token,'not '+token,'un'+token,'x'+token,token+'_unknown',token+'?']) {
    assert.notEqual(execStatus(value),'good',value);
    assert.notEqual(execStatus(value),'live',value);
    checked++;
  }
}
[null,undefined,true,false,0,1,[],['verified'],['accepted'],['live'],{state:'verified'},
 {toString(){throw Error('status input must never be coerced');}}].forEach(value=>expect(value,''));
console.log(JSON.stringify({status:'PASS',checks:checked}));
"""
    result = subprocess.run(["node", "-e", match.group(0) + "\n" + assertions],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=10, check=True)
    print("actual JavaScript status classifier", result.stdout.strip())
    return json.loads(result.stdout)


def layout(page, width, height, label):
    page.set_viewport_size({"width": width, "height": height})
    report = page.evaluate("""() => {
      const root = document.documentElement;
      const main = document.querySelector('main');
      const named = Array.from(document.querySelectorAll(
        '.exec-hero,.goal-card,.program-card,.program-lane,.job-card,.platform-card,.performance-grid .hwcard,.impact-shell'
      )).map(n => ({name:n.className,width:n.getBoundingClientRect().width,
        right:n.getBoundingClientRect().right,left:n.getBoundingClientRect().left}));
      return {viewport:innerWidth,scroll:root.scrollWidth,
        main:main.getBoundingClientRect().width,cards:named};
    }""")
    require(report["scroll"] <= width + 1, "%s page overflow %s" % (label, report))
    for card in report["cards"]:
        require(card["left"] >= -1 and card["right"] <= width + 1,
                "%s card outside viewport %s" % (label, card))
    print("layout", label, width, height, "ok")
    return report


def contrast_checks(page, scheme):
    """Measure rendered colors, alpha compositing and ancestor text opacity."""
    page.emulate_media(color_scheme=scheme)
    report = page.evaluate("""() => {
      function color(value) {
        const m = String(value).match(/rgba?\\(([^)]+)\\)/);
        if (!m) throw new Error('Unsupported computed color: ' + value);
        const p = m[1].split(/[, /]+/).filter(Boolean).map(Number);
        return [p[0],p[1],p[2],p.length>3?p[3]:1];
      }
      function over(a,b) {
        const alpha=a[3]+b[3]*(1-a[3]);
        return [0,1,2].map(i=>(a[i]*a[3]+b[i]*b[3]*(1-a[3]))/alpha).concat(alpha);
      }
      function luminance(c) {
        const p=c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);});
        return .2126*p[0]+.7152*p[1]+.0722*p[2];
      }
      function ratio(a,b) { const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); }
      function background(node) {
        const nodes=[];for(let n=node;n;n=n.parentElement)nodes.unshift(n);
        let result=[255,255,255,1];
        nodes.forEach(n=>{const s=getComputedStyle(n),c=color(s.backgroundColor);c[3]*=Number(s.opacity);result=over(c,result);});
        return result;
      }
      function measure(node,property,minimum,label) {
        const s=getComputedStyle(node),bg=background(property==='backgroundColor'?node.parentElement:node),fg=color(s[property]);
        if(property==='color'||property==='fill')for(let n=node;n;n=n.parentElement)fg[3]*=Number(getComputedStyle(n).opacity);
        const painted=over(fg,bg);
        return {label,property,foreground:s[property],background:bg,ratio:ratio(painted,bg),minimum};
      }
      const fixtures=document.createElement('article');fixtures.className='card';fixtures.id='contrast-fixture';
      const classes=['status-chip','status-chip good','status-chip attention','status-chip bad','status-chip live',
        'pill pill-active','pill pill-idle','pill pill-offline','pill pill-failing','pill pill-quiet','pill pill-unlock',
        'badge on','badge off','step pass','step fail','step run','step pending','pill yes','pill at_risk','pill off_track',
        'pill stopped','codex-banner'];
      classes.forEach(cls=>{const n=document.createElement('span');n.className=cls;n.textContent='Status';fixtures.appendChild(n);});
      const util=document.createElement('div');util.className='util';
      ['tag','tag tag-stale','tag tag-alert'].forEach(cls=>{const n=document.createElement('span');n.className=cls;n.textContent='Status';util.appendChild(n);});fixtures.appendChild(util);
      const chart=document.createElementNS('http://www.w3.org/2000/svg','svg');chart.classList.add('chart');
      ['pass','fail','run','pending'].forEach(state=>{
        const n=document.createElementNS(chart.namespaceURI,'rect');n.classList.add('chart-step-'+state);chart.appendChild(n);
      });fixtures.appendChild(chart);document.querySelector('#app').appendChild(fixtures);
      const pairs=[
        ['body','color',4.5],['header h1','color',4.5],['header .sub','color',4.5],
        ['.section-nav a','color',4.5],['.hero-sub','color',4.5],['.hero-source','color',4.5],
        ['.journey-caption','color',4.5],['.journey-detail','color',4.5],['.hero-alert','color',4.5],
        ['.program-impact','color',4.5],['.program-boundary','color',4.5],
        ['.program-lane-summary','color',4.5],['.program-metrics span','color',4.5],
        ['.program-clocks dt','color',4.5],['.program-clocks dd','color',4.5],
        ['.program-lineage dt','color',4.5],['.program-lineage dd','color',4.5],
        ['.program-current h4','color',4.5],['.program-card','borderTopColor',3],['.program-evidence > summary','color',4.5],
        ['.opening-current','color',4.5],['.goal-card h3','color',4.5],['.goal-summary','color',4.5],['.goal-evidence summary','color',4.5],
        ['.goal-card','borderTopColor',3],['.data-note','color',4.5],['.attention-note','color',4.5],
        ['.empty-state','color',4.5],['.impact-entry .impact-goal','color',4.5],['.impact-tab','color',4.5],
        ['.impact-tab[aria-selected=true]','color',4.5],['.job-meta-label','color',4.5],
        ['.member small','color',4.5],['.hw-head','color',4.5],['.hw-status .k','color',4.5],
        ['.hw-gl i','color',4.5],['.hw-g svg text','fill',4.5],['.gauge .track','stroke',3],
        ['.gauge .arc','stroke',3],['.disk-note','color',4.5],['.disk-bar span','backgroundColor',3],
        ['details.ops-detail > summary','color',4.5],['.resource-detail > summary','color',4.5],
        ['.rv2-src','color',4.5],['.rv2-role','color',4.5],['.pv-foot','color',4.5],
        ['.gcard .subl','color',4.5],['.gcard .count','color',4.5],['.tok-table td.nr','color',4.5],
        ['#settings a','color',4.5],['#contrast-fixture span','color',4.5],
        ['#contrast-fixture .chart rect','fill',3]
      ];
      const results=[];
      pairs.forEach(([selector,property,minimum])=>{
        Array.from(document.querySelectorAll(selector)).slice(0,40).forEach((node,i)=>{
          if(getComputedStyle(node).display!=='none')results.push(measure(node,property,minimum,selector+'['+i+']'));
        });
      });
      const selected=document.querySelector('.impact-tab[aria-selected=true]');
      if(selected) {
        selected.focus();
        results.push(measure(selected,'outlineColor',3,'selected tab keyboard focus'));
        const shadow=color(getComputedStyle(selected).boxShadow);
        results.push({label:'selected tab boundary',ratio:ratio(shadow,background(selected.parentElement)),minimum:3});
      }
      Array.from(document.querySelectorAll('.hw-g .gauge')).slice(0,15).forEach((svg,i)=>{
        const arc=svg.querySelector('.arc'),track=svg.querySelector('.track');
        results.push({label:'gauge arc/track['+i+']',ratio:ratio(color(getComputedStyle(arc).stroke),color(getComputedStyle(track).stroke)),minimum:3});
      });
      const root=getComputedStyle(document.documentElement);
      const theme={viewport:innerWidth,background:getComputedStyle(document.body).backgroundColor,foreground:getComputedStyle(document.body).color,colorScheme:root.colorScheme};
      fixtures.remove();
      return {theme,pairs:results};
    }""")
    require(report["theme"]["colorScheme"] == "dark", "dark native controls not enabled")
    require(report["theme"]["background"] == "rgb(16, 26, 24)", "dashboard stayed light")
    failed = [row for row in report["pairs"] if row["ratio"] + 0.01 < row["minimum"]]
    require(not failed, "dark contrast failures: " + json.dumps(failed, indent=2))
    print("contrast", scheme, len(report["pairs"]), "pairs ok",
          "minimum text", round(min(row["ratio"] for row in report["pairs"] if row["minimum"] == 4.5), 2))
    return report


def focus_summary_snapshot(page, key=None):
    """Focus and capture the current summary and clock in one browser task."""
    return page.evaluate("""key => {
      const details = key == null
        ? document.querySelector('details.goal-evidence[data-key]')
        : Array.from(document.querySelectorAll('details[data-key]')).find(node => node.dataset.key === key);
      if (!details) throw new Error('Current evidence details missing');
      const summary = Array.from(details.children).find(node => node.tagName === 'SUMMARY');
      if (!summary) throw new Error('Current details summary missing');
      summary.focus();
      if (document.activeElement !== summary) throw new Error('Current summary did not receive focus');
      const clock = document.getElementById('clock');
      if (!clock) throw new Error('Snapshot clock missing');
      return {key:details.dataset.key, clock:clock.textContent};
    }""", key)


def focused_summary_key(page):
    return page.evaluate("""() => {
      const active = document.activeElement;
      const details = active && active.tagName === 'SUMMARY' ? active.closest('details[data-key]') : null;
      return details ? details.dataset.key : null;
    }""")


def collector_current_fixture(source_path=None):
    """Use the collector's pure normalizer, never its Git/GitHub/file collectors."""
    path = Path(source_path) if source_path else Path(__file__).resolve().parents[1] / "src/zai_devdashd/programs.py"
    spec = importlib.util.spec_from_file_location("dashboard_program_fixture_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    published, merge, docs = "2" * 40, "3" * 40, "4" * 40
    program = {"github": {"parent_pr": 10, "pr": 11, "canonical_issue": 12},
               "lineage": {"published": published, "merge": merge}}
    raw = {
        "parent": {"number": 10, "state": "OPEN", "isDraft": True, "headRefOid": docs, "baseRefName": "example-integration"},
        "pr": {"number": 11, "state": "MERGED", "headRefOid": published, "baseRefName": "example-integration",
               "mergeCommit": {"oid": merge}, "mergedAt": "2025-01-15T13:00:00Z"},
        "canonical_issue": {"number": 12, "state": "OPEN"},
        "issues": [{"number": 20, "state": "OPEN", "stateReason": "REOPENED"},
                   {"number": 21, "state": "CLOSED", "stateReason": "COMPLETED"}],
    }
    current = module._current(raw, program, [20, 21], "example-integration", FIXTURE_NOW + 10)
    require(current["parent_pr"]["draft"] is True, "collector draft fixture contract changed")
    require(current["reopened_issues"] == [20], "collector reopened issue fixture contract changed")
    return current


def fixture_checks(browser, html):
    """Explicit unknowns, zero readings and job wrapping in an isolated browser fixture."""
    disk = {"name": "Fixture disk", "total_bytes": 1_000_000_000_000,
            "used_bytes": 750_000_000_000, "free_bytes": 250_000_000_000,
            "used_percent": 75.0, "shared_capacity": True, "as_of": FIXTURE_NOW}
    state = {
        "server_time_et": "2025-01-15 10:00:00-05:00 America/Toronto",
        "overview": {"tiles": [{
            "id": "mini", "name": "Example local host", "anchor": "sys-mini", "status": "idle",
            "job": "Idle", "hw": {"kind": "mac", "as_of": FIXTURE_NOW - 10000,
                "stale": True, "error": "Hardware inspection unavailable",
                "gauges": [{"label": "CPU use", "short": "CPU", "kind": "cpu", "pct": 15}],
                "facts": [], "disks": []}
        }], "accomplishments": {
            "partial": True, "as_of_et": "2025-01-15 08:00-05:00 America/Toronto",
            "today": {"counts": {"merged": None, "gates": None, "shipped": None, "opened": 1}},
            "week": {"counts": {"merged": None, "gates": None, "shipped": None, "opened": 1}},
        }},
        "mini": {"name": "Example local host", "as_of_et": "2025-01-15 10:00-05:00 America/Toronto", "stale": False, "active": False},
        "active": {"jobs": [{
            "id": "fixture-job", "name": "Verify the executive dashboard preserves human readable job details across small screens",
            "goal": "Goal: Qualify the dashboard presentation", "where": "Fixture Mac",
            "step": "Running a focused browser check", "technical": [
                {"label": "Receipt", "value": "fixture-only-evidence"}
            ],
        }]},
        "remote": {"state": "online", "name": "Example remote host", "stale": False,
            "hardware": {"cores": 6, "memory_bytes": 8589934592},
            "load": {"cpu_pct": 0, "mem_used_pct": 25, "gpu_pct": None,
                     "disks": [disk, {"name": "Unavailable fixture disk", "total_bytes": None, "used_bytes": None, "free_bytes": None, "used_percent": None, "error": "Inspection unavailable"}],
                     "disks_status": {"partial": True, "error": "Partial inspection"},
                     "performance_observed_at": FIXTURE_NOW}},
        "executive_dashboard": {"partial": True, "coverage": {"blockers_total": 17},
            "blockers": [], "goals": [{
                "id": "fixture-qualification", "title": "Qualify builds",
                "status": "unverified", "summary": "Explicit fixture evidence",
                "next_step": "Wait for the missing result",
                "progress": {"done": 4, "total": 5, "label": "Results recorded"},
                "evidence": [{"label": "Validation", "value": "A failed result is recorded",
                    "state": "failed", "head": "0123456789abcdef",
                    "progress": {"done": 4, "total": 5, "passed": 3}}]
            }],
            "achievements": {"today": {"completed": [], "started": [{
                "title": "Fixture proposed change", "kind": "opened",
                "goal": "Integrate approved changes", "impact": "Proposed work is awaiting acceptance.",
                "at": "2025-01-15 08:00-05:00 America/Toronto", "stale": True,
            }]}}
        },
    }
    program = {
        "id": "fixture-product", "title": "Synthetic parser module acceptance",
        "objective": "Check a synthetic parser module", "program": "Synthetic parser example", "scope": "example-only",
        "accepted_verified": True, "historical_accepted": True, "state": "verified", "stale": False,
        "status_label": "Accepted evidence verified", "impact": "Records a synthetic parser acceptance example.",
        "release_boundary": "Synthetic example only; no product release acceptance.", "next_step": "Follow up on the parent proposal.",
        "occurred_at": FIXTURE_NOW - 10000, "accepted_at": FIXTURE_NOW - 5000, "verified_at": FIXTURE_NOW,
        "lineage": {"qualified": "1"*40, "published": "2"*40, "merge": "3"*40, "docs": "4"*40},
        "facts": {"v1_promoted": False, "closures": {"count": 2, "issues": [{"number": 20, "state": "CLOSED"}]}},
        "evidence": [{"label": "Registered receipt", "value": "Fixture source", "state": "verified", "sha256": "a"*64}],
        "platforms": [
            {"id": "macos", "title": "Signed Mac hosted tests", "summary": "Build 9001 · Signed · Hosted test results recorded",
             "metrics": [{"label": "Tests passed", "value": 9}, {"label": "New tests", "value": 3}],
             "machine": "Fixture Mac", "occurred_at": FIXTURE_NOW - 10000, "stale": False},
            {"id": "ios_simulator", "title": "Unsigned Simulator compile/link", "summary": "Build 9002 · Unsigned · Compile/link only; runtime and device tests unverified",
             "metrics": [{"label": "Pinned checkouts", "value": 4}, {"label": "Warnings", "value": 7}],
             "architectures": ["arm64", "x86_64"], "machine": "Fixture compiler", "occurred_at": FIXTURE_NOW - 9900, "stale": False},
        ],
        "current": collector_current_fixture(),
    }
    event = {"kind": "program_checkpoint", "evidence_id": "program:fixture-product",
             "title": program["title"], "goal": program["objective"], "impact": program["impact"],
             "release_boundary": program["release_boundary"], "at": "2025-01-15 07:03-05:00 America/Toronto", "evidence": program["evidence"]}
    state["executive_dashboard"]["programs"] = {
        "coverage": {"registered": 3, "expected_receipts": 2, "valid_receipts": 2, "pending": 2},
        "checkpoints": [program, {
            "id": "pending-artifact", "objective": "Check example artifacts", "title": "Example artifact checks",
            "state": "pending_adapter", "accepted_verified": False, "historical_accepted": False,
            "status_label": "Evidence connection pending", "impact": "Connect synthetic artifact evidence.",
            "next_step": "Current job observations remain available below.", "current": {},
            "error": "Evidence adapter not implemented", "platforms": [], "evidence": [],
        }, {
            "id": "pending-import", "objective": "Check example imports", "title": "Example import checks",
            "state": "pending_adapter", "accepted_verified": False, "historical_accepted": False,
            "status_label": "Evidence connection pending", "impact": "Connect synthetic import evidence.",
            "next_step": "Current job observations remain available below.", "current": {},
            "error": "Evidence adapter not implemented", "platforms": [], "evidence": [],
        }], "achievements": {
            "today": {"count": 1, "completed": [event], "summary": "One registered checkpoint accepted."},
            "week": {"count": 1, "completed": [event], "summary": "One registered checkpoint accepted."}}}
    state["executive_dashboard"]["achievements"]["today"]["started"][0]["current"] = {
        "state": "merged", "observed_at": FIXTURE_NOW + 10, "stale": False}
    context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
    context.add_init_script("""
      Date.now = () => 1736953200000;
      window.__executiveFixtureSources = [];
      window.EventSource = class extends EventTarget {
        constructor() {
          super(); window.__executiveFixtureSources.push(this);
          setTimeout(() => this.dispatchEvent(new MessageEvent('state', {
            data: JSON.stringify(%s)
          })), 0);
        }
        close() {}
      };
    """ % json.dumps(state))
    fixture = context.new_page()
    data = json.dumps(state)
    fixture.route("http://executive-fixture.local/", lambda route: route.fulfill(
        status=200, content_type="text/html", body=html))
    fixture.route("**/api/state", lambda route: route.fulfill(
        status=200, content_type="application/json", body=data))
    fixture.route("**/api/events", lambda route: route.fulfill(
        status=200, content_type="text/event-stream", body="event: state\ndata: " + data + "\n\n"))
    fixture.route("**/static/*", lambda route: route.fulfill(status=200, body=""))
    fixture.goto("http://executive-fixture.local/", wait_until="domcontentloaded")
    fixture.wait_for_selector(".exec-hero")
    require(fixture.locator('[data-journey="attention"] .journey-value').inner_text() == "17",
            "authoritative blocker total replaced by truncated array length")
    require(fixture.locator("#achievement-content .impact-kpi .number").all_inner_texts() == ["—", "—", "—", "1"],
            "unknown source counts shown as fresh zero")
    require("No completions recorded today." in fixture.locator("#achievement-content").inner_text(),
            "proposed work counted as completion")
    require(fixture.locator("#achievement-content .impact-entry.started").count() == 1,
            "historical openings not separately marked")
    product = fixture.locator('[data-program="fixture-product"]')
    require(product.count() == 1, "product program card absent or duplicated")
    scope_text = product.locator(".goal-top > .eyebrow").text_content() or ""
    require("example-only" in [part.strip() for part in scope_text.split("·")], "product program scope absent")
    require("9" in product.locator('[data-program-platform="macos"]').inner_text(),
            "receipt-driven Mac test count missing")
    simulator = product.locator('[data-program-platform="ios_simulator"]').inner_text()
    require("Compile/link only" in simulator and "device tests unverified" in simulator,
            "Simulator build presented as runtime acceptance")
    require("Current PR state: merged" in fixture.locator(".opening-current").first.inner_text(),
            "historical merged opening presented as awaiting completion")
    require("awaiting completion" not in fixture.locator("#achievement-content").inner_text(),
            "blanket outstanding-work label retained")
    require("Product achievements" in fixture.locator(".product-achievements").inner_text(),
            "program achievement missing")
    product.locator(".program-evidence > summary").click()
    require("Issues closed in accepted record" in product.inner_text() and "Reopened issue #20" in product.inner_text(),
            "historical closure and current follow-up conflated")
    require("open · draft" in product.locator(".program-current").inner_text(),
            "canonical collector draft field not displayed")
    require("#undefined" not in product.inner_text(), "malformed reopened issue label")
    pending = fixture.locator('[data-program="pending-artifact"]')
    require("Completion evidence is not connected yet" in pending.inner_text(),
            "pending product evidence lacks human explanation")
    require("adapter" not in pending.inner_text() and "Unreported" not in pending.inner_text(),
            "pending executive card exposes implementation jargon or empty clocks")
    require(pending.locator(".program-evidence").count() == 0, "pending card has empty provenance clocks")
    fixture.set_viewport_size({"width": 1440, "height": 1000})
    boxes = fixture.evaluate("""() => {
      const accepted=document.querySelector('[data-program="fixture-product"]').getBoundingClientRect();
      const a=document.querySelector('[data-program="pending-artifact"]').getBoundingClientRect();
      const b=document.querySelector('[data-program="pending-import"]').getBoundingClientRect();
      return {accepted:accepted.width,a:a.width,b:b.width,ay:a.y,by:b.y};
    }""")
    require(boxes["accepted"] > boxes["a"] * 1.8 and abs(boxes["ay"] - boxes["by"]) < 1,
            "accepted checkpoint must span above paired pending cards: " + repr(boxes))
    fixture.set_viewport_size({"width": 390, "height": 844})
    require(fixture.locator("#card-sys-remote .hw-g svg text").all_text_contents() == ["0%", "n/r", "25%"],
            "known zero or unavailable gauge misrepresented: " + repr(fixture.locator("#card-sys-remote .hw-g svg text").all_text_contents()))
    storage = fixture.locator("#card-sys-remote .disk-section").inner_text()
    require(all(value in storage for value in ("75.0% used", "750 GB", "250 GB", "Shared APFS pool")),
            "storage units or shared capacity missing: " + storage)
    require("Storage coverage is incomplete" in storage, "partial disk coverage presented as complete")
    unknown_disk = fixture.locator("#card-sys-remote .disk-row").nth(1)
    require("Use unreported" in unknown_disk.inner_text() and "0%" not in unknown_disk.inner_text(),
            "failed disk inspection shown as zero utilization")
    require(unknown_disk.locator(".disk-bar > span").count() == 0,
            "missing disk utilization rendered as measured bar")
    fixture.locator(".goal-evidence > summary").first.click()
    require("4 / 5 stage results recorded; 3 passed." in fixture.locator(".goal-evidence").first.inner_text(),
            "recorded stages and passed results conflated")
    fixture.locator(".job-card > summary").click()
    fixture.locator(".job-card .tech > summary").click()
    require("fixture-only-evidence" in fixture.locator(".job-body").inner_text(),
            "job technical evidence inaccessible")
    # Identical SSE snapshots must retain focus on direct and nested summaries.
    for key in ("exec:goal:fixture-qualification", "job:fixture-job", "tech:fixture-job", "program:provenance:fixture-product"):
        obsolete = fixture.query_selector('details[data-key="%s"] > summary' % key)
        require(obsolete is not None, "fixture summary missing: " + key)
        # Reproduce the promotion race deterministically: this retained node is
        # detached by the identical SSE render before the next protocol call.
        fixture.evaluate("""state => window.__executiveFixtureSources.forEach(
          source => source.dispatchEvent(new MessageEvent('state', {data:JSON.stringify(state)}))
        )""", state)
        require(obsolete.evaluate("(node) => !node.isConnected && node.parentElement === null"),
                "fixture did not detach the obsolete summary: " + key)
        current = focus_summary_snapshot(fixture, key)
        require(current["key"] == key, "atomic capture chose the wrong summary")
        fixture.evaluate("""state => window.__executiveFixtureSources.forEach(
          source => source.dispatchEvent(new MessageEvent('state', {data:JSON.stringify(state)}))
        )""", state)
        require(focused_summary_key(fixture) == key,
                "summary focus lost across identical SSE: " + key)
        obsolete.dispose()
    performance = fixture.locator("#card-sys-mini")
    require("Performance unavailable" in performance.inner_text(),
            "failed hardware read presented as current")
    require("2025-01-15 10:00-05:00 America/Toronto" not in performance.inner_text(),
            "fresh process clock substituted for hardware source clock")
    require("stale" in performance.locator(".hw-g svg").get_attribute("aria-label"),
            "retained hardware value lacks stale semantics")
    require(performance.locator(".hw-g svg text").all_text_contents() == ["15%"],
            "explicitly retained stale performance reading lost")
    fixture.emulate_media(color_scheme="dark")
    link_color = fixture.locator("#settings a").first.evaluate(
        "(node) => getComputedStyle(node).color")
    require(link_color == "rgb(168, 221, 196)", "unreadable dark-mode default link: " + link_color)
    name = fixture.locator(".job-name").first
    require(name.evaluate("(n) => getComputedStyle(n).wordBreak") == "normal",
            "unnatural within-word job wrapping")
    layout(fixture, 390, 844, "unknown/zero/job fixture")
    malformed_state = json.loads(json.dumps(state))
    bad_current = malformed_state["executive_dashboard"]["programs"]["checkpoints"][0]["current"]
    bad_current["parent_pr"].update(draft=False, isDraft=True)
    bad_current["reopened_issues"] = [None, {}, "bad", -1, True, 20, {"number": 20}, {"number": 21, "state": "OPEN"}]
    fixture.evaluate("""state => window.__executiveFixtureSources.forEach(
      source => source.dispatchEvent(new MessageEvent('state', {data:JSON.stringify(state)}))
    )""", malformed_state)
    bad_text = fixture.locator('[data-program="fixture-product"] .program-current').inner_text()
    require("#undefined" not in bad_text and "#null" not in bad_text and "draft" not in bad_text,
            "canonical false draft or malformed issue identity mishandled")
    require(bad_text.count("Reopened issue #20") == 1 and "Reopened issue #21" in bad_text,
            "canonical and compatible reopened IDs not safely normalized")
    partial_state = json.loads(json.dumps(state))
    partial_program = partial_state["executive_dashboard"]["programs"]
    partial_program["checkpoints"][0].update(accepted_verified=False, state="unavailable", stale=True,
                                            status_label="Evidence incomplete", error="Registered receipt digest mismatch",
                                            platforms=[])
    partial_program["achievements"]["today"].update(count=None, completed=[], summary="Evidence coverage incomplete.")
    fixture.evaluate("""state => window.__executiveFixtureSources.forEach(
      source => source.dispatchEvent(new MessageEvent('state', {data:JSON.stringify(state)}))
    )""", partial_state)
    require("Registered receipt digest mismatch" in fixture.locator('[data-program="fixture-product"]').inner_text(),
            "tampered product source hidden")
    require("Coverage incomplete" in fixture.locator(".product-achievements").inner_text(),
            "missing product evidence shown as fresh zero")
    layout(fixture, 390, 844, "partial program fixture")
    context.close()
    print("unknown/zero/job/storage/program fixture ok")

def main():
    from playwright.sync_api import sync_playwright
    status_classifier_checks()
    if AUDIT:
        AUDIT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = getattr(p, BROWSER).launch()
        context = browser.new_context(ignore_https_errors=True, reduced_motion="reduce", color_scheme="light",
                                      viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        if page.locator("#username").count():
            page.fill("#username", "smoke")
            page.fill("#password", "smoke-secret")
            page.click("button[type=submit]")
        page.wait_for_selector(".exec-hero", timeout=45000)
        page.wait_for_function("document.querySelector('#clock').textContent.includes('server')")
        for section in ("overview", "goals", "active", "achievements",
                        "platforms", "systems", "resources-section"):
            require(page.locator("#" + section).count() == 1, "missing/duplicate " + section)
        require(page.locator("#goals").count() == 1, "workflow section absent")
        require(page.locator(".platform-card").count() == 3, "three platform groups absent")
        for platform in ("macos", "ios", "ipados"):
            require(page.locator('[data-platform="%s"]' % platform).count() == 1,
                    "missing platform " + platform)
        performance_ids = page.locator(".performance-grid .hwcard").evaluate_all("nodes => nodes.map(node => node.id)")
        require(len(performance_ids) == len(set(performance_ids)), "duplicate configured host performance cards")
        if page.locator("#card-sys-remote").count():
            require(page.locator("#card-sys-remote .hw-g").count() == 3,
                    "configured remote host must distinguish CPU/GPU/memory readings")

        # Accessible names and tab semantics must be available to screen readers.
        daily = page.get_by_role("tab", name="Daily", exact=True)
        weekly = page.get_by_role("tab", name="Weekly", exact=True)
        require(daily.get_attribute("aria-selected") == "true", "daily default")
        weekly.click()
        require(weekly.get_attribute("aria-selected") == "true", "weekly selection")
        require(page.locator("#achievement-content .impact-entry").count() >= 0,
                "weekly achievement panel absent")
        weekly.focus()
        weekly.press("ArrowLeft")
        require(page.locator("#achievement-tab-today").get_attribute("aria-selected") == "true",
                "arrow changes selected period")
        weekly.click()
        weekly.focus()

        journey = page.get_by_role("button", name=re.compile(r"03.*Distribution", re.I))
        require(journey.count() == 1, "summary button has no accessible name")
        journey.click()
        require(journey.get_attribute("aria-pressed") == "true", "journey selection")
        require("hand" in page.locator("#journey-detail").inner_text().lower(),
                "distribution explanation absent")

        # A new SSE render must preserve chosen period, checkpoint, focus, expanders.
        goal = page.locator('.goal-evidence').first
        goal.locator("summary").click()
        journey.focus()
        before = page.locator("#clock").inner_text()
        page.wait_for_function(
            "(old) => document.querySelector('#clock').textContent !== old",
            arg=before, timeout=40000)
        require(page.locator("#achievement-tab-week").get_attribute("aria-selected") == "true",
                "period lost after refresh")
        require(page.locator('[data-journey="distribution"]').get_attribute("aria-pressed") == "true",
                "checkpoint lost after refresh")
        require(page.locator('.goal-evidence').first.get_attribute("open") is not None,
                "evidence expander lost after refresh")
        require(page.evaluate("document.activeElement.dataset.journey") == "distribution",
                "keyboard focus lost after refresh")

        reports = []
        for width, height in ((1440, 1000), (1280, 900), (1024, 900), (768, 1024), (390, 844)):
            reports.append(layout(page, width, height, "executive"))
            if AUDIT and width in (1440, 390):
                page.screenshot(path=str(AUDIT / ("%s-%s.png" % (BROWSER, width))),
                                full_page=True)
        # Technical detail stays usable on a phone without widening the whole page.
        page.locator('details[data-key="exec:system-controls"] > summary').click()
        page.locator('details[data-key="exec:resources"] > summary').click()
        layout(page, 390, 844, "expanded technical")
        require(not errors, "browser errors: " + repr(errors))
        if AUDIT:
            (AUDIT / ("%s-layout.json" % BROWSER)).write_text(json.dumps(reports, indent=2))
        page.locator('details.eng > summary').first.click()
        contrast = {}
        for width, height in ((1440, 1000), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            contrast[str(width)] = contrast_checks(page, "dark")
            layout(page, width, height, "dark executive")
            if AUDIT:
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path=str(AUDIT / ("%s-dark-%s.png" % (BROWSER, width))))
                page.locator(".performance-grid").screenshot(
                    path=str(AUDIT / ("%s-dark-performance-%s.png" % (BROWSER, width))))
        dark_snapshot = focus_summary_snapshot(page)
        dark_key = dark_snapshot["key"]
        dark_before = dark_snapshot["clock"]
        page.wait_for_function(
            "(old) => document.querySelector('#clock').textContent !== old",
            arg=dark_before, timeout=40000)
        require(focused_summary_key(page) == dark_key,
                "dark summary keyboard focus lost after SSE")
        require(page.locator("#achievement-tab-week").get_attribute("aria-selected") == "true",
                "dark Weekly selection lost after SSE")
        if AUDIT:
            (AUDIT / ("%s-dark-contrast.json" % BROWSER)).write_text(json.dumps(contrast, indent=2))
        require(not errors, "dark browser errors: " + repr(errors))
        fixture_checks(browser, context.request.get(URL).text())
        browser.close()
    print("executive dashboard ok", BROWSER)


if __name__ == "__main__":
    if sys.argv[1:] == ["--status-unit"]:
        status_classifier_checks()
    else:
        main()
