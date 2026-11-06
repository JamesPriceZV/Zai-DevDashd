#!/usr/bin/env python3
"""Device Hub launches only for a physical id on the current list."""

from __future__ import annotations

import unittest

from zai_devdashd import labels as board

ARGV = ["/usr/bin/open", "-b", "com.apple.dt.Devices"]
UDID = "00000000-0000000000000101"


def physical(**extra) -> dict:
    item = {
        "name": "D102",
        "tile_name": "D102",
        "udid": UDID,
        "identifier": "core-1",
        "reality": "physical",
        "placeholder": False,
    }
    item.update(extra)
    return item


class Recorder:
    def __init__(self, code: int = 0, err: str = "") -> None:
        self.calls: list[list[str]] = []
        self.code = code
        self.err = err

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return self.code, b"", self.err


class DeviceConsoleTests(unittest.TestCase):
    def test_path_token_does_not_launch(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console([physical()], "bogus-../../x", launch)
        self.assertEqual(message, "unknown device")
        self.assertEqual(status, 400)
        self.assertEqual(launch.calls, [])

    def test_unknown_well_formed_id(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console([physical()], "00008140-DEADBEEF", launch)
        self.assertEqual((message, status), ("unknown device", 400))
        self.assertEqual(launch.calls, [])

    def test_simulated_device_is_rejected(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console(
            [physical(reality="simulated")], UDID, launch
        )
        self.assertEqual((message, status), ("unknown device", 400))
        self.assertEqual(launch.calls, [])

    def test_placeholder_is_rejected(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console(
            [physical(placeholder=True)], UDID, launch
        )
        self.assertEqual((message, status), ("unknown device", 400))
        self.assertEqual(launch.calls, [])

    def test_name_is_not_an_id(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console(
            [physical(name="D101", tile_name="D101")], "D101", launch
        )
        self.assertEqual((message, status), ("unknown device", 400))
        self.assertEqual(launch.calls, [])

    def test_matching_udid_uses_list_label(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console(
            [physical(name="Evil", tile_name="D102")], UDID, launch
        )
        self.assertEqual(status, 200)
        self.assertIn("D102", message)
        self.assertNotIn("Evil", message)
        self.assertNotIn(UDID, message)
        self.assertEqual(launch.calls, [ARGV])

    def test_matching_coredevice_id(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console(
            [physical(udid="", identifier="core-1", tile_name="D101")], "core-1", launch
        )
        self.assertEqual(status, 200)
        self.assertIn("D101", message)
        self.assertEqual(launch.calls, [ARGV])

    def test_empty_token(self) -> None:
        launch = Recorder()
        message, status = board.open_device_console([physical()], "", launch)
        self.assertEqual((message, status), ("unknown device", 400))
        self.assertEqual(launch.calls, [])

    def test_open_failure_omits_client_token(self) -> None:
        launch = Recorder(code=1, err="open failed")
        message, status = board.open_device_console([physical()], UDID, launch)
        self.assertEqual(status, 200)
        self.assertTrue(message.startswith("Could not open Device Hub"))
        self.assertNotIn(UDID, message)
        self.assertEqual(launch.calls, [ARGV])


if __name__ == "__main__":
    unittest.main()
