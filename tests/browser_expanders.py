#!/usr/bin/env python3
"""Open every expander and require it to stay open across 6 SSE refreshes.

Login uses the smoke fixture when the page shows the sign-in form.
A lost open state, or a lost non-zero scroll position, exits non-zero.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://127.0.0.1:8780/"
BROWSER = sys.argv[2] if len(sys.argv) > 2 else "chromium"


def check_login_layout(page, width: int, height: int) -> None:
    """The login background has no hero text, and each input stays inside the card."""
    page.set_viewport_size({"width": width, "height": height})
    report = page.evaluate(
        """() => {
            const card = document.querySelector("form").closest(".card");
            const cardStyle = getComputedStyle(card);
            const pad = parseFloat(cardStyle.paddingRight) + parseFloat(cardStyle.borderRightWidth);
            const innerRight = card.getBoundingClientRect().right - pad;
            function edge(id) {
                const node = document.querySelector(id);
                const right = node.getBoundingClientRect().right;
                return {id: id, right: right, innerRight: innerRight, ok: right <= innerRight + 0.5};
            }
            const nodes = [document.body].concat(Array.from(document.querySelectorAll(".card, h1")));
            const images = nodes.map(node => getComputedStyle(node).backgroundImage || "");
            const bodyImage = getComputedStyle(document.body).backgroundImage || "";
            return {
                bodyImage: bodyImage,
                hero: images.some(bg => /url\\s*\\(/i.test(bg)),
                gradient: /gradient/i.test(bodyImage),
                username: edge("#username"),
                password: edge("#password")
            };
        }"""
    )
    print("login layout %sx%s %s" % (width, height, report))
    if report["hero"] or not report["gradient"]:
        raise SystemExit("login background %sx%s: %s" % (width, height, report))
    if not report["username"]["ok"] or not report["password"]["ok"]:
        raise SystemExit("login input overflow %sx%s: %s" % (width, height, report))


def main() -> None:
    with sync_playwright() as playwright:
        browser = getattr(playwright, BROWSER).launch()
        page = browser.new_page(ignore_https_errors=True)
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        host = URL.split("/")[2]
        needs_login = not (
            host.startswith("127.0.0.1") or host.startswith("localhost") or host.startswith("[::1]")
        )
        saw_login = page.locator("#username").count() > 0
        if needs_login and not saw_login:
            raise SystemExit("expected a login form for %s" % host)
        if saw_login:
            for width, height in ((1440, 900), (1280, 800), (390, 844)):
                check_login_layout(page, width, height)
            page.set_viewport_size({"width": 1280, "height": 720})
            page.fill("#username", "smoke")
            page.fill("#password", "smoke-secret")
            page.click("button[type=submit]")
        page.wait_for_selector("#clock", timeout=30000)
        page.wait_for_function(
            """() => {
                const clock = document.querySelector('#clock');
                return !!(clock && clock.textContent && clock.textContent.indexOf('server') >= 0);
            }""",
            timeout=45000,
        )
        page.wait_for_timeout(500)
        title = page.title()
        if "Zai-DevDashd – Developer Dashboard" not in title:
            raise SystemExit("document title: " + title)
        heading = page.text_content("h1") or ""
        if "Zai-DevDashd – Developer Dashboard" not in heading:
            raise SystemExit("header: " + heading)
        page.wait_for_function(
            """() => {
                const img = document.querySelector('header img');
                return !!(img && img.complete && img.naturalWidth > 0);
            }""",
            timeout=20000,
        )
        missing = page.evaluate(
            """() => ['overview', 'active', 'insights', 'systems', 'settings'].filter(
                id => !document.getElementById(id)
            )"""
        )
        if missing:
            raise SystemExit("missing sections: " + ",".join(missing))
        print("header ok")
        print("sections ok")
        details = page.eval_on_selector_all(
            "details",
            "els => els.map(e => e.dataset.key || '')",
        )
        print("details:", len(details))
        job_keys = [key for key in details if key.startswith("job:")]
        tech_keys = [key for key in details if key.startswith("tech:")]
        system_keys = [key for key in details if key.startswith("tech:sys-")]
        insight_keys = [key for key in details if key.startswith("insight:")]
        print("job cards:", len(job_keys))
        print("technical details:", len(tech_keys))
        print("system expanders:", len(system_keys))
        print("insights:", len(insight_keys))
        if job_keys and len(tech_keys) < len(job_keys):
            raise SystemExit(
                "technical details %d for %d jobs" % (len(tech_keys), len(job_keys))
            )
        if not details:
            raise SystemExit("no details to hold open")
        if not job_keys and not system_keys:
            raise SystemExit("no job or system expander")
        page.eval_on_selector_all(
            "details > summary",
            "nodes => nodes.forEach(node => { if (!node.parentElement.open) node.click(); })",
        )
        page.wait_for_timeout(300)
        opened = page.eval_on_selector_all(
            "details[open]",
            "els => els.map(e => e.dataset.key)",
        )
        if not opened:
            raise SystemExit("details did not stay open after click")
        missing_jobs = [key for key in job_keys if key not in opened]
        if missing_jobs:
            raise SystemExit("job cards did not open: " + ",".join(missing_jobs))
        held = [key for key in job_keys + system_keys if key in opened]
        if not held:
            raise SystemExit("no job or system expander stayed open")
        missing_systems = [key for key in system_keys if key not in opened]
        if missing_systems:
            raise SystemExit("system expanders did not open: " + ",".join(missing_systems))
        if insight_keys:
            missing_insights = [key for key in insight_keys if key not in opened]
            if missing_insights:
                raise SystemExit("insights did not open: " + ",".join(missing_insights))
        scrolled = page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('.scroll[data-key], pre[data-key]'));
                const saved = [];
                nodes.forEach(node => {
                    const maxX = node.scrollWidth - node.clientWidth;
                    const maxY = node.scrollHeight - node.clientHeight;
                    let left = 0;
                    let top = 0;
                    if (maxX > 4) {
                        node.scrollLeft = Math.min(24, maxX);
                        left = node.scrollLeft;
                    }
                    if (maxY > 4) {
                        node.scrollTop = Math.min(24, maxY);
                        top = node.scrollTop;
                    }
                    saved.push({key: node.dataset.key, left: left, top: top});
                });
                return saved;
            }"""
        )
        print("scroll areas:", len(scrolled))
        for index in range(6):
            before = page.text_content("#clock") or ""
            page.wait_for_function(
                """(previous) => {
                    const clock = document.querySelector('#clock');
                    return !!(clock && clock.textContent && clock.textContent !== previous);
                }""",
                arg=before,
                timeout=40000,
            )
            page.wait_for_timeout(200)
            now = page.eval_on_selector_all(
                "details[open]",
                "els => els.map(e => e.dataset.key)",
            )
            present = page.eval_on_selector_all(
                "details",
                "els => els.map(e => e.dataset.key)",
            )
            # A job that left the snapshot is not a closed expander. A card that is still
            # on the page and is no longer open is a failure.
            lost = [key for key in opened if key in present and key not in now]
            gone = [key for key in opened if key not in present]
            print(
                "refresh %d: clock=%s lost=%s gone=%s"
                % (index + 1, page.text_content("#clock"), lost, gone)
            )
            if lost:
                raise SystemExit("open state lost: " + ",".join(lost))
            if scrolled:
                still = page.evaluate(
                    """(saved) => saved.map(item => {
                        const node = document.querySelector('[data-key="' + item.key + '"]');
                        if (!node) return {key: item.key, missing: true, left: 0, top: 0};
                        return {key: item.key, missing: false, left: node.scrollLeft, top: node.scrollTop};
                    })""",
                    scrolled,
                )
                for item, current in zip(scrolled, still):
                    if current.get("missing"):
                        raise SystemExit("scroll area missing: " + item["key"])
                    if item["left"] > 0 and current["left"] < 1:
                        raise SystemExit("scrollLeft lost: " + item["key"])
                    if item["top"] > 0 and current["top"] < 1:
                        raise SystemExit("scrollTop lost: " + item["key"])
        browser.close()
        print("expanders ok", BROWSER)


if __name__ == "__main__":
    main()
