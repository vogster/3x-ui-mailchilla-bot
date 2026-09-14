"""
Noticing a newer version.

Version comparison is the part worth pinning down: `sort -V` in the shell gets
pre-release tags backwards, which is why this lives in Python at all.
"""
import unittest

import config
import updater


class Parsing(unittest.TestCase):
    def test_release_tags(self):
        self.assertEqual(updater._parse("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater._parse("1.2.3"), (1, 2, 3))

    def test_anything_that_is_not_a_release_is_not_one(self):
        for text in ("v1.2.3-rc.1", "nightly", "v1.2", "", None, "v1.2.3.4"):
            self.assertIsNone(updater._parse(text), text)


class Comparison(unittest.TestCase):
    def setUp(self):
        self._version = config.APP_VERSION
        self._enabled = config.UPDATE_CHECK_ENABLED
        self._dismissed = config.UPDATE_DISMISSED_VERSION
        config.UPDATE_CHECK_ENABLED = True
        config.UPDATE_DISMISSED_VERSION = ""
        updater._checked_at = 1.0

    def tearDown(self):
        config.APP_VERSION = self._version
        config.UPDATE_CHECK_ENABLED = self._enabled
        config.UPDATE_DISMISSED_VERSION = self._dismissed
        updater._latest = None
        updater._checked_at = 0.0

    def _status(self, installed, latest):
        config.APP_VERSION = installed
        updater._latest = latest
        return updater.status()

    def test_a_newer_one_is_offered(self):
        self.assertTrue(self._status("0.1.2", "0.2.0")["available"])

    def test_numbers_are_numbers_not_text(self):
        # "0.1.10" sorts before "0.1.2" as text and after it as a version.
        self.assertTrue(self._status("0.1.2", "0.1.10")["available"])

    def test_the_same_version_is_not_news(self):
        self.assertFalse(self._status("0.2.0", "0.2.0")["available"])

    def test_an_older_one_is_never_offered(self):
        # A copy running ahead of the newest tag is somebody testing a branch.
        self.assertFalse(self._status("0.2.0", "0.1.9")["available"])

    def test_dismissed_stays_dismissed_until_something_newer(self):
        config.UPDATE_DISMISSED_VERSION = "0.2.0"
        self.assertFalse(self._status("0.1.2", "0.2.0")["available"])
        self.assertTrue(self._status("0.1.2", "0.3.0")["available"])

    def test_switched_off_says_nothing(self):
        config.UPDATE_CHECK_ENABLED = False
        self.assertFalse(self._status("0.1.2", "0.2.0")["available"])

    def test_nothing_learnt_yet_says_nothing(self):
        config.APP_VERSION = "0.1.2"
        updater._latest = None
        updater._checked_at = 0.0
        self.assertFalse(updater.status()["available"])


class Links(unittest.TestCase):
    def test_the_slug_comes_out_of_project_url(self):
        self.assertEqual(updater.repo_slug(), "vogster/3x-ui-mailchilla-bot")

    def test_the_comparison_is_between_the_two_versions_at_hand(self):
        # Not the release page: a tag may have no release, and "what changed
        # since mine" is the question being asked.
        self.assertTrue(updater.compare_url("0.1.2", "0.2.0").endswith("/compare/v0.1.2...v0.2.0"))

    def test_a_v_prefix_is_not_doubled(self):
        self.assertTrue(updater.compare_url("v0.1.2", "v0.2.0").endswith("/compare/v0.1.2...v0.2.0"))


if __name__ == "__main__":
    unittest.main()
