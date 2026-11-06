#!/usr/bin/env python3
"""Overview lines: one reason per pull request, and a plain working title."""

from __future__ import annotations

import unittest

from zai_devdashd import labels as board


class OverviewTests(unittest.TestCase):
    def test_work_line_uses_the_pull_request_title(self) -> None:
        line = board.work_line(
            "All macOS unit tests pass and the phone Integration tests Test passes after fixing root causes.",
            "Goal: All macOS unit tests pass",
            17,
            "Step 4 of 5: Integration tests, running 4m",
            "Pipeline",
            "Fix the PDF export so turn 3 includes the Research more answer",
        )
        lane = board.work_line(
            "L-A · Fix #17: ensure all macOS unit tests pass and Tablet101 Integration tests Test passes",
            "Goal: ensure all macOS unit tests pass",
            17,
            "Reading the project",
            "L-A",
            "Fix the PDF export so turn 3 includes the Research more answer",
        )
        self.assertEqual(
            lane,
            "#17 · Fix the PDF export so turn 3 includes the Research more answer — Reading the project",
        )
        self.assertNotIn("L-A", lane)
        self.assertEqual(
            line,
            "#17 · Fix the PDF export so turn 3 includes the Research more answer"
            " — Step 4 of 5: Integration tests, running 4m",
        )
        self.assertNotIn("current step", line)
        self.assertFalse(line.lower().startswith("phone"))

    def test_device_names_the_test(self) -> None:
        title, _goal, step = board.device_lines("Tablet101", "diesel xcodebuild test eng-17")
        self.assertEqual(title, "#17 · integration tests on Tablet101")
        self.assertIn("Tablet101", step)
        self.assertNotIn("phone", title.lower())
        bare, _goal, bare_step = board.device_lines("phone", "")
        self.assertEqual(bare, "Work on Phone")
        self.assertEqual(bare_step, "Running on Phone")
        self.assertFalse(bare.lower().startswith("phone"))

    def test_one_line_per_pull_request(self) -> None:
        head = {
            "number": 17,
            "title": "Use only the exact diesel prompts",
            "coderabbit": "CHANGES_REQUESTED",
            "checks": {"fail": 0, "pending": 1},
            "hold": "UNIT-BLOCKED(conflicts with #17)",
        }
        behind = {
            "number": 20,
            "title": "Preserve strict FM probe behavior",
            "coderabbit": "CHANGES_REQUESTED",
            "checks": {"fail": 0, "pending": 0},
            "hold": "BUILD-OK/UNIT-BLOCKED(conflicts with #17)",
        }
        failed = {
            "number": 2163,
            "title": "Resolve Metal compiler",
            "coderabbit": "COMMENTED",
            "checks": {"fail": 2, "pending": 0},
            "hold": "UNIT-BLOCKED(conflicts with #17)",
        }
        clear = {
            "number": 21,
            "title": "Fix the two iOS filesystem test failures",
            "coderabbit": "APPROVED",
            "checks": {"fail": 0, "pending": 0},
            "hold": "",
        }
        lists = board.overview_lists([], [], [head, clear, behind, failed], [])
        texts = [row["text"] for row in lists["waiting"]]
        self.assertEqual(len(texts), 3)
        self.assertIn("CodeRabbit", texts[0])
        self.assertNotIn("pipeline", texts[0])
        self.assertIn("the merge order, behind #17", texts[1])
        self.assertIn("pipeline", texts[2])
        self.assertEqual(lists["waiting"][2]["tone"], "stop")
        self.assertTrue(all(text.count("#17") <= 2 for text in texts))

    def test_top_six_then_the_rest(self) -> None:
        prs = []
        for number in (17, 20, 22, 23, 24, 2250, 2163, 25):
            prs.append({
                "number": number,
                "title": "Change %s" % number,
                "coderabbit": "REVIEW_REQUIRED",
                "checks": {"fail": 0, "pending": 0},
                "hold": "",
            })
        lists = board.overview_lists([], [], prs, [])
        self.assertEqual(len(lists["waiting"]), 6)
        self.assertEqual(len(lists["waiting_more"]), 2)
        self.assertTrue(lists["waiting"][0]["text"].startswith("#17"))
        self.assertTrue(lists["waiting_more"][0]["text"].startswith("#2163"))


if __name__ == "__main__":
    unittest.main()
