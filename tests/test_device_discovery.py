"""Device discovery: every physical iPhone/iPad from devicectl appears; simulators and accessories don't."""
import unittest

from zai_devdashd import probes as monitor


def dev(name, dtype, reality="physical", state="unavailable"):
    return {"name": name, "device_type": dtype, "reality": reality, "state": state, "model": dtype + " model", "os_label": "iOS 27.2"}


class Discovery(unittest.TestCase):
    def test_filter(self):
        self.assertTrue(monitor.want_device("TestPhone105", "iPhone"))
        self.assertTrue(monitor.want_device("Anything", "iPad"))
        self.assertFalse(monitor.want_device("Living Room", "HomePod"))
        self.assertFalse(monitor.want_device("Build-Mac", "Mac"))
        self.assertFalse(monitor.want_device("Watch", "appleWatch"))

    def test_present_all_physical(self):
        items = [dev("TestPhone101", "iPad", state="available (paired)"), dev("TestPhone102", "iPhone", state="connected"),
                 dev("TestPhone105", "iPhone"), dev("TestPhone106", "iPhone"), dev("Synthetic iPad simulator", "iPad", reality="simulated"),
                 dev("Studio iPad", "iPad")]
        tiles = {d["tile_name"] for d in monitor.present_devices(items)}
        for t in ("TestPhone101", "TestPhone102", "TestPhone105", "TestPhone106", "Studio iPad"):
            self.assertIn(t, tiles)
        self.assertNotIn("Synthetic iPad simulator", tiles)


if __name__ == "__main__":
    unittest.main()
