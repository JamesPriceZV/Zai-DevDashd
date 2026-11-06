"""The working line comes from a lane jsonl, not from the last git commit."""
import re
import unittest

from zai_devdashd import labels as board

NOW = 1_762_000_000

TOOL_JSONL = """
{"type":"text","data":"Looking at the timeout helper. "}
{"type":"tool_call","title":"run_terminal_command","toolName":"run_terminal_command","rawInput":{"command":"xcodebuild test -scheme ExampleApp -destination platform=macOS"}}
"""

TEXT_JSONL = """
{"type":"tool_call","title":"grep","toolName":"grep","rawInput":{"description":"find the timeout"}}
{"type":"text","data":"Fixing a unit-test timeout in AnswerProgressTests. "}
{"type":"text","data":"The helper waits on the run loop."}
{"type":"thought","data":"next"}
{"type":"text","data":"cf"}
"""

PHONE_JSONL = """
{"type":"tool_call","title":"run_terminal_command","toolName":"run_terminal_command","rawInput":{"command":"xcodebuild test -scheme ExampleApp -destination 'platform=iOS,name=Tablet101'"}}
"""


class LaneActivity(unittest.TestCase):
    def test_tool_jsonl_is_mac_unit_tests(self):
        found = board.scan_lane_jsonl(TOOL_JSONL)
        self.assertEqual(found["newest"], "tool")
        line = board.lane_activity(
            found["assistant"], found["tool"], found["newest"], True, NOW - 240, NOW,
        )
        self.assertTrue(line.startswith("Running Mac unit tests"), line)
        self.assertIn("last activity 4m", line)
        self.assertNotIn("last commit", line)
        self.assertRegex(line, r"\(\d{2}:\d{2} ET\)")

    def test_assistant_sentence_beats_a_short_fragment(self):
        found = board.scan_lane_jsonl(TEXT_JSONL)
        self.assertEqual(found["newest"], "text")
        self.assertTrue(found["assistant"].startswith("Fixing a unit-test timeout"), found["assistant"])
        line = board.lane_activity(
            found["assistant"], found["tool"], found["newest"], True, NOW - 240, NOW,
        )
        self.assertTrue(line.startswith("Fixing a unit-test timeout"), line)
        self.assertIn("last activity 4m", line)
        self.assertNotIn("last commit", line)

    def test_idle_since(self):
        line = board.lane_activity("", "", "", False, NOW - 720, NOW)
        clock = board.clock_hhmm(NOW - 720)
        self.assertEqual(line, "Idle since %s ET" % clock)
        self.assertNotIn("12m", line)
        self.assertNotRegex(line, r"Idle since \d+[smh]")
        self.assertNotIn("last commit", line)

    def test_idle_under_a_minute_is_just_finished(self):
        fresh = board.lane_activity("", "", "", False, NOW - 2, NOW)
        self.assertEqual(fresh, "Just finished")
        self.assertNotIn("Idle since", fresh)
        almost = board.lane_activity("", "", "", False, NOW - 59, NOW)
        self.assertEqual(almost, "Just finished")
        minute = board.lane_activity("", "", "", False, NOW - 60, NOW)
        self.assertRegex(minute, r"^Idle since \d{2}:\d{2} ET$")

    def test_phone_test_is_not_called_a_mac_unit_test(self):
        found = board.scan_lane_jsonl(PHONE_JSONL)
        line = board.lane_activity(
            found["assistant"], found["tool"], found["newest"], True, NOW - 45, NOW,
        )
        self.assertNotIn("Running Mac unit tests", line)
        self.assertIn("last activity 45s", line)


if __name__ == "__main__":
    unittest.main()
