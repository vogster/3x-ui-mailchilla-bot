"""
Clearing out the mailbox.

The one part of the bot that removes something rather than adding it, which is
why the tests here are mostly about what it must leave alone: the unread
letters, and everything at all when the Trash cannot be found.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import inbox
import settings


class FakeIMAP:
    """As much of imaplib as the cleanup touches, and a record of what it did."""

    def __init__(self, folders=None, uids=b"1 2 3", capabilities=("IMAP4REV1", "MOVE")):
        self.folders = folders if folders is not None else [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "Trash"',
        ]
        self.uids = uids
        self.capabilities = capabilities
        self.calls = []

    def list(self):
        return "OK", self.folders

    def uid(self, command, *args):
        self.calls.append((command,) + args)
        if command == "SEARCH":
            return "OK", [self.uids]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("EXPUNGE",))
        return "OK", [b""]

    def moved(self):
        """The uid sets handed to MOVE or COPY, in order."""
        return [call[1] for call in self.calls if call[0] in ("MOVE", "COPY")]

    # The rest of the connection, for the test that drives a whole poll cycle.
    def login(self, user, password):
        return "OK", [b""]

    def select(self, folder):
        return "OK", [b"0"]

    def search(self, charset, *criteria):
        # The poll loop asks for UNSEEN; there are none in these tests, so the
        # cycle goes straight on to the clearing out.
        return "OK", [b""]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "OK", [b""]


class WhenItRuns(unittest.TestCase):
    def setUp(self):
        self.saved = (config.MAIL_CLEANUP_ENABLED, config.MAIL_CLEANUP_DAYS,
                      config.MAIL_CLEANUP_LAST_AT)
        config.MAIL_CLEANUP_ENABLED = True
        config.MAIL_CLEANUP_DAYS = 30
        config.MAIL_CLEANUP_LAST_AT = 0.0

    def tearDown(self):
        (config.MAIL_CLEANUP_ENABLED, config.MAIL_CLEANUP_DAYS,
         config.MAIL_CLEANUP_LAST_AT) = self.saved

    def test_switched_off_means_never(self):
        config.MAIL_CLEANUP_ENABLED = False
        config.MAIL_CLEANUP_LAST_AT = 0.0
        self.assertFalse(inbox.cleanup_due(now=10 ** 9))

    def test_the_first_poll_after_switching_it_on_clears_it(self):
        # Nothing written down yet: somebody has just asked for this, and a
        # mailbox that stayed full for another month would read as broken.
        self.assertTrue(inbox.cleanup_due(now=10 ** 9))

    def test_it_waits_out_the_interval(self):
        now = 10 ** 9
        config.MAIL_CLEANUP_LAST_AT = now - 29 * 86400
        self.assertFalse(inbox.cleanup_due(now=now))
        config.MAIL_CLEANUP_LAST_AT = now - 30 * 86400
        self.assertTrue(inbox.cleanup_due(now=now))

    def test_a_shorter_interval_comes_round_sooner(self):
        now = 10 ** 9
        config.MAIL_CLEANUP_DAYS = 1
        config.MAIL_CLEANUP_LAST_AT = now - 2 * 86400
        self.assertTrue(inbox.cleanup_due(now=now))


class FindingTheTrash(unittest.TestCase):
    def test_the_special_use_attribute_wins(self):
        mail = FakeIMAP(folders=[
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "|" "&BBoEPgRABAcEOAQ9BDA-"',
        ])
        self.assertEqual(inbox.trash_folder(mail), "&BBoEPgRABAcEOAQ9BDA-")

    def test_a_known_name_is_the_fallback(self):
        # A server that marks nothing. The names differ by provider, which is
        # why the attribute is asked for first and this list is second.
        mail = FakeIMAP(folders=[
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "[Gmail]/Trash"',
        ])
        self.assertEqual(inbox.trash_folder(mail), "[Gmail]/Trash")

    def test_a_name_with_a_space_survives(self):
        mail = FakeIMAP(folders=[b'(\\HasNoChildren) "/" "Deleted Items"'])
        self.assertEqual(inbox.trash_folder(mail), "Deleted Items")

    def test_no_trash_at_all(self):
        mail = FakeIMAP(folders=[b'(\\HasNoChildren) "/" "INBOX"'])
        self.assertEqual(inbox.trash_folder(mail), "")


class WhatItMoves(unittest.TestCase):
    def test_only_the_read_letters_are_searched_for(self):
        # The flag is set after the handler has finished, so unread means owed
        # an answer — a registration that has not happened yet, after an outage.
        mail = FakeIMAP()
        inbox.cleanup(mail)
        searches = [call for call in mail.calls if call[0] == "SEARCH"]
        self.assertEqual(len(searches), 1)
        self.assertIn("SEEN", searches[0])
        self.assertNotIn("ALL", searches[0])

    def test_they_go_to_the_trash_and_are_counted(self):
        mail = FakeIMAP(uids=b"1 2 3")
        self.assertEqual(inbox.cleanup(mail), 3)
        self.assertEqual(mail.moved(), ["1,2,3"])
        move = [call for call in mail.calls if call[0] == "MOVE"][0]
        self.assertEqual(move[2], '"Trash"')

    def test_nothing_happens_without_a_trash_folder(self):
        # Deleting outright is not the fallback: the whole point of the Trash is
        # that a mistake in the schedule can be undone.
        mail = FakeIMAP(folders=[b'(\\HasNoChildren) "/" "INBOX"'])
        self.assertEqual(inbox.cleanup(mail), 0)
        self.assertEqual(mail.calls, [])

    def test_an_empty_mailbox_moves_nothing(self):
        mail = FakeIMAP(uids=b"")
        self.assertEqual(inbox.cleanup(mail), 0)
        self.assertEqual(mail.moved(), [])

    def test_a_server_without_move_copies_flags_and_expunges(self):
        mail = FakeIMAP(capabilities=("IMAP4REV1",))
        self.assertEqual(inbox.cleanup(mail), 3)
        commands = [call[0] for call in mail.calls]
        self.assertEqual(commands, ["SEARCH", "COPY", "STORE", "EXPUNGE"])
        store = [call for call in mail.calls if call[0] == "STORE"][0]
        self.assertIn("Deleted", store[3])

    def test_a_full_mailbox_is_moved_in_batches(self):
        # One command carrying ten thousand uids is a line no server enjoys.
        count = inbox.CLEANUP_BATCH * 2 + 5
        uids = " ".join(str(n) for n in range(1, count + 1)).encode()
        mail = FakeIMAP(uids=uids)
        self.assertEqual(inbox.cleanup(mail), count)
        self.assertEqual(len(mail.moved()), 3)


class TheWiring(unittest.TestCase):
    """
    The cleanup is run by the poll loop, on the connection it already has.

    Worth a test of its own: every piece below can be right while nothing ever
    calls them, and the symptom of that is a mailbox that simply stays full.
    """

    def setUp(self):
        self.saved = (config.MAIL_CLEANUP_ENABLED, config.MAIL_CLEANUP_LAST_AT,
                      config.IMAP_USER, config.IMAP_PASSWORD)
        config.MAIL_CLEANUP_ENABLED = True
        config.MAIL_CLEANUP_LAST_AT = 0.0
        config.IMAP_USER = "bot@example.com"
        config.IMAP_PASSWORD = "secret"
        self.mail = FakeIMAP()
        self.real_ssl = inbox.imaplib.IMAP4_SSL
        inbox.imaplib.IMAP4_SSL = lambda *a, **kw: self.mail
        self.written = []
        self.real_remember = inbox._remember_cleanup
        inbox._remember_cleanup = self.written.append

    def tearDown(self):
        inbox.imaplib.IMAP4_SSL = self.real_ssl
        inbox._remember_cleanup = self.real_remember
        (config.MAIL_CLEANUP_ENABLED, config.MAIL_CLEANUP_LAST_AT,
         config.IMAP_USER, config.IMAP_PASSWORD) = self.saved

    def test_a_poll_clears_the_mailbox_out_when_it_is_due(self):
        inbox.check_mail(lambda *a, **kw: None)
        self.assertEqual(self.mail.moved(), ["1,2,3"])
        self.assertEqual(len(self.written), 1, "the run was not written down")

    def test_a_poll_leaves_it_alone_when_it_is_not(self):
        config.MAIL_CLEANUP_ENABLED = False
        inbox.check_mail(lambda *a, **kw: None)
        self.assertEqual(self.mail.moved(), [])
        self.assertEqual(self.written, [])


class TheSetting(unittest.TestCase):
    def test_the_interval_is_at_least_a_day(self):
        self.assertEqual(settings._coerce("MAIL_CLEANUP_DAYS", "7"), 7)
        with self.assertRaises(ValueError):
            settings._coerce("MAIL_CLEANUP_DAYS", "0")

    def test_an_unreadable_timestamp_means_not_yet(self):
        # It is written by the bot, not typed by anybody, so a broken value is a
        # broken file rather than a mistake worth refusing to start over.
        self.assertEqual(settings._coerce("MAIL_CLEANUP_LAST_AT", "nonsense"), 0.0)
        self.assertEqual(settings._coerce("MAIL_CLEANUP_LAST_AT", "12.5"), 12.5)

    def test_it_is_off_until_somebody_asks(self):
        self.assertFalse(settings.BASE["MAIL_CLEANUP_ENABLED"])


if __name__ == "__main__":
    unittest.main()
