"""Group lookup + membership regression tests (no real credentials; dscl/dsmemberutil simulated)."""
import os, sys, unittest
from unittest import mock
from zai_devdashd import auth

class GroupTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop(getattr(auth, "FAKE_ENV", "ZAI_DEVDASHD_FAKE_AUTH"), None)
    def test_group_checked_per_call_not_cached(self):
        with mock.patch.object(auth, "_run", return_value=(1, "")):
            self.assertFalse(auth.group_exists("zai-devdashd"))
        with mock.patch.object(auth, "_run", return_value=(0, "PrimaryGroupID: 710")):
            self.assertTrue(auth.group_exists("zai-devdashd"))
    def test_member_ok(self):
        with mock.patch.object(auth, "_run", side_effect=[(0, ""), (0, ""), (0, "user is a member of the group")]):
            self.assertEqual(auth.authenticate("testuser", "x", "zai-devdashd"), "ok")
    def test_non_member_denied(self):
        with mock.patch.object(auth, "_run", side_effect=[(0, ""), (0, ""), (0, "user is not a member of the group")]):
            self.assertEqual(auth.authenticate("testuser", "x", "zai-devdashd"), "not-member")
    def test_bad_password_denied_before_membership(self):
        with mock.patch.object(auth, "_run", side_effect=[(0, ""), (1, "")]) as run:
            self.assertEqual(auth.authenticate("testuser", "x", "zai-devdashd"), "bad-password")
            self.assertEqual(run.call_count, 2)
    def test_membership_output_is_explicit(self):
        self.assertTrue(auth.membership_ok("user is a member of the group"))
        self.assertFalse(auth.membership_ok("user is not a member of the group"))
        self.assertFalse(auth.membership_ok("group lookup unavailable"))

if __name__ == "__main__":
    unittest.main()
