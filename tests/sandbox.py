"""
Keeps a test run out of the project directory.

The state files are found from each module's own __file__ — settings.json,
email_texts.json and tariffs.json sit beside the code, and the log goes to
logs/ under wherever the process was started. Nothing about that changes for
a test: the suite imports the same modules, so until this file existed a test
run was an installation of sorts. Importing the panel built a first tariff and
wrote tariffs.json, with the default word in it, into the checkout — where the
next real start found it and never built a tariff out of the real settings.
It opened logs/bot.log and wrote the run into it. And any test that posted a
form through to a real save rewrote the file a developer's own panel reads.
On a server, running the tests meant doing all of that to the installation.

So this is imported first by every test module, and before anything is loaded
it points the three state files at a directory of the run's own and switches
the log file off. Two things behind that, for the test nobody has written yet:
a write through statefile into the project directory is an error rather than a
file, and whatever else has appeared there by the end of the run is named.

Each module still moves the paths about as it likes — tests/test_tariffs.py
and the panel tests give every test a directory of its own. What they put
back afterwards is now this run's path rather than the installation's.
"""
import atexit
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import config  # noqa: E402

# Before applog.install() can be reached: it opens the file the moment the
# panel is imported. The ring buffer the /logs page reads is untouched.
config.LOG_FILE = ""

import email_texts  # noqa: E402
import settings  # noqa: E402
import statefile  # noqa: E402
import tariffs  # noqa: E402

# One directory for the run, gone when it ends.
DIR = tempfile.mkdtemp(prefix="mailchilla-tests-")
atexit.register(shutil.rmtree, DIR, ignore_errors=True)

settings.SETTINGS_PATH = os.path.join(DIR, "settings.json")
email_texts.TEXTS_PATH = os.path.join(DIR, "email_texts.json")
tariffs.TARIFFS_PATH = os.path.join(DIR, "tariffs.json")


def inside_project(path: str) -> bool:
    """Whether a path lies in the project directory."""
    target = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(ROOT)
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        # Another drive altogether.
        return False


_write_json = statefile.write_json


def _guarded_write(path, data, **dump_options):
    """
    statefile.write_json, refusing the project directory.

    Every state file is written through it, so this is where a test that has
    found its way round the paths above is caught — as a failure with the
    file's name in it, rather than as a file somebody finds a week later.
    """
    if inside_project(path):
        raise AssertionError(
            f"a test tried to write {os.path.abspath(path)}, which is inside the project "
            f"directory. State files belong in a temporary one: see tests/sandbox.py.")
    return _write_json(path, data, **dump_options)


statefile.write_json = _guarded_write


def _listing() -> set:
    """What is in the project directory, bytecode caches aside."""
    try:
        return {name for name in os.listdir(ROOT) if name != "__pycache__"}
    except OSError:
        return set()


_before = _listing()


def left_behind() -> list:
    """Whatever has appeared in the project directory since the run began."""
    return sorted(_listing() - _before)


def _say_what_was_left():
    # After the fact and only a warning: by now the tests have reported, and
    # there is no result left to change. But it names the file while the run
    # that made it is still on the screen.
    stray = left_behind()
    if stray:
        sys.stderr.write(
            "\ntests/sandbox.py: this run left something behind in the project directory: "
            + ", ".join(stray) + "\n")


atexit.register(_say_what_was_left)
