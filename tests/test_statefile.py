"""
The files that hold the installation's secrets.

settings.json carries the mailbox passwords. It was written with a plain
open(), which under systemd means world-readable — and nothing ever shows it:
the panel works, the bot works, and any other account on the server can read
the password.
"""
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statefile


class WriteJson(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "settings.json")

    def tearDown(self):
        self.dir.cleanup()

    def mode(self):
        return stat.S_IMODE(os.stat(self.path).st_mode)

    def test_what_is_written_is_what_is_read(self):
        statefile.write_json(self.path, {"SERVICE_NAME": "Сервис", "IMAP_PORT": 993})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"SERVICE_NAME": "Сервис", "IMAP_PORT": 993})

    def test_nothing_is_left_beside_it(self):
        statefile.write_json(self.path, {})
        self.assertEqual(os.listdir(self.dir.name), ["settings.json"])

    @unittest.skipUnless(os.name == "posix", "permission bits mean nothing on Windows")
    def test_only_the_owner_can_read_it(self):
        old = os.umask(0o022)   # what systemd hands a service
        try:
            statefile.write_json(self.path, {"IMAP_PASSWORD": "secret"})
        finally:
            os.umask(old)
        self.assertEqual(self.mode(), 0o600)

    @unittest.skipUnless(os.name == "posix", "permission bits mean nothing on Windows")
    def test_a_file_from_an_older_version_is_closed_on_the_next_save(self):
        with open(self.path, "w") as f:
            f.write("{}")
        os.chmod(self.path, 0o644)
        statefile.write_json(self.path, {"IMAP_PASSWORD": "secret"})
        self.assertEqual(self.mode(), 0o600)

    @unittest.skipUnless(os.name == "posix", "permission bits mean nothing on Windows")
    def test_nor_does_an_abandoned_temporary_file_open_it_up(self):
        with open(self.path + ".tmp", "w") as f:
            f.write("half a write")
        os.chmod(self.path + ".tmp", 0o644)
        statefile.write_json(self.path, {"IMAP_PASSWORD": "secret"})
        self.assertEqual(self.mode(), 0o600)


if __name__ == "__main__":
    unittest.main()
