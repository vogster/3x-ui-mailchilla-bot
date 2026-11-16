"""
A test run leaves the project directory as it found it.

It did not use to: importing the panel wrote tariffs.json and opened
logs/bot.log in the checkout, and a test that posted a form through to a real
save rewrote the installation's own file. tests/sandbox.py is what stops that;
these are the checks that it is in place and that nothing has gone round it.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import glob
import os
import re
import unittest

import config
import email_texts
import settings
import statefile
import tariffs


class TheStateFilesAreElsewhere(unittest.TestCase):
    def test_none_of_them_points_into_the_project(self):
        for path in (settings.SETTINGS_PATH, email_texts.TEXTS_PATH, tariffs.TARIFFS_PATH):
            with self.subTest(path=path):
                self.assertFalse(sandbox.inside_project(path))

    def test_the_log_goes_to_no_file(self):
        # applog.install() opens config.LOG_FILE the moment the panel is
        # imported; empty is how it is told not to.
        self.assertEqual(config.LOG_FILE, "")
        import applog
        self.assertIsNone(applog._handler)

    def test_a_write_into_the_project_is_an_error_and_not_a_file(self):
        target = os.path.join(sandbox.ROOT, "written-by-a-test.json")
        with self.assertRaises(AssertionError) as caught:
            statefile.write_json(target, {"oops": True})
        self.assertIn("written-by-a-test.json", str(caught.exception))
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".tmp"))

    def test_a_write_anywhere_else_is_let_through(self):
        target = os.path.join(sandbox.DIR, "fine.json")
        statefile.write_json(target, {"fine": True})
        self.assertTrue(os.path.exists(target))

    def test_what_counts_as_inside(self):
        self.assertTrue(sandbox.inside_project(os.path.join(sandbox.ROOT, "tariffs.json")))
        self.assertTrue(sandbox.inside_project(os.path.join(sandbox.ROOT, "logs", "bot.log")))
        self.assertFalse(sandbox.inside_project(sandbox.DIR))
        # A neighbour whose name merely starts the same way is not inside.
        self.assertFalse(sandbox.inside_project(sandbox.ROOT + "-backup"))


class EveryTestModuleBringsItIn(unittest.TestCase):
    """
    One module run by itself — `discover -p test_tariffs.py` — imports nothing
    but itself. The sandbox only holds if each of them asks for it, and asks
    first: a module that loads the tariffs on its second line and the sandbox
    on its fifth has already written the file.
    """

    FIRST_IMPORT = re.compile(r"^(?:import|from)\s+(\S+)", re.M)

    def test_and_before_any_other_import(self):
        for path in sorted(glob.glob(os.path.join(sandbox.HERE, "test_*.py"))):
            with self.subTest(module=os.path.basename(path)):
                with open(path, encoding="utf-8") as handle:
                    found = self.FIRST_IMPORT.search(handle.read())
                self.assertIsNotNone(found)
                self.assertEqual(found.group(1), "sandbox")

    def test_so_does_the_package(self):
        # For `python -m unittest tests.test_x`, where the package comes first.
        with open(os.path.join(sandbox.HERE, "__init__.py"), encoding="utf-8") as handle:
            self.assertIn("import sandbox", handle.read())


class NothingIsLeftBehind(unittest.TestCase):
    def test_so_far(self):
        # Whichever tests have run before this one. The whole run is checked
        # again as it ends, and a file found then is named on the way out.
        self.assertEqual(sandbox.left_behind(), [])


if __name__ == "__main__":
    unittest.main()
