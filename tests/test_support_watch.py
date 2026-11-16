"""
The push that says a letter has arrived for Support.

Nobody is watching Support's mailbox the way the bot watches its own: a letter
there waits for a person, and the person finds out when they next look. So the
bot looks for them, once a minute, and says so through Gotify.

What can go wrong is mostly a matter of saying too much or too little. Too
much: every letter already in the mailbox announced the first time the watch
runs, or the same letter again after a restart. Too little: a letter that
arrived while the process was down, never mentioned. And one thing it must
never do at all, which is change anything in the mailbox it is looking at.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import imaplib
import os
import sys
import unittest
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config
import mailer
import settings
import support_watch
from fake_imap import FakeMailbox

SUPPORT = "support@example.com"


def letter(sender="Anna Bright <anna@example.com>", subject="My phone will not connect"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, SUPPORT, subject
    msg.set_content("hello")
    return bytes(msg)


class Watching(unittest.TestCase):
    KEYS = ("SUPPORT_IMAP_SERVER", "SUPPORT_IMAP_USER", "SUPPORT_IMAP_PASSWORD",
            "SUPPORT_SMTP_USER", "GOTIFY_URL", "GOTIFY_TOKEN", "SUPPORT_NOTIFY_ENABLED",
            "SUPPORT_NOTIFY_MARK", "SUPPORT_NOTIFY_TITLE", "SUPPORT_NOTIFY_MESSAGE",
            "SERVICE_NAME", "PANEL_LANG")

    def setUp(self):
        self.saved = {key: getattr(config, key) for key in self.KEYS}
        config.SUPPORT_IMAP_SERVER = "imap.support.example.com"
        config.SUPPORT_IMAP_USER = config.SUPPORT_SMTP_USER = SUPPORT
        config.SUPPORT_IMAP_PASSWORD = "secret"
        config.GOTIFY_URL, config.GOTIFY_TOKEN = "https://push.example.com", "token"
        config.SUPPORT_NOTIFY_ENABLED = True
        config.SUPPORT_NOTIFY_MARK = ""
        config.SUPPORT_NOTIFY_TITLE = "A letter to {service} support"
        config.SUPPORT_NOTIFY_MESSAGE = "{sender}: {subject}"
        config.SERVICE_NAME = "Aurora"
        config.PANEL_LANG = "en"

        self.box = FakeMailbox()
        self.real_imap = imaplib.IMAP4_SSL
        imaplib.IMAP4_SSL = self.box.connect

        self.pushes = []
        self.real_push = mailer.send_gotify_notification
        mailer.send_gotify_notification = lambda title, message: self.pushes.append((title, message))

        # Written to config rather than to settings.json: the real one writes
        # the installation's own file.
        self.remembered = []
        self.real_remember = support_watch._remember

        def remember(mark):
            self.remembered.append(mark)
            config.SUPPORT_NOTIFY_MARK = mark

        support_watch._remember = remember
        support_watch._reset()

    def tearDown(self):
        support_watch._remember = self.real_remember
        mailer.send_gotify_notification = self.real_push
        imaplib.IMAP4_SSL = self.real_imap
        for key, value in self.saved.items():
            setattr(config, key, value)
        support_watch._reset()
        # The rule above all the others.
        self.assertEqual(self.box.writable_selects, [], "the mailbox was opened for writing")
        self.assertEqual(self.box.unpeeked_fetches, [], "a body was fetched without PEEK")
        self.assertEqual(self.box.stored, [], "a flag was changed")


class WhatIsAnnounced(Watching):
    def test_what_was_already_there_is_not_news(self):
        # The first time the watch ever runs, or the first after Support was
        # set up: a mailbox with a year of letters in it must not ring a
        # thousand times.
        self.box.add("INBOX", letter())
        self.box.add("INBOX", letter(subject="Another"))
        support_watch.check()
        self.assertEqual(self.pushes, [])
        self.assertEqual(config.SUPPORT_NOTIFY_MARK, "1:2")

    def test_a_letter_that_arrives_is(self):
        self.box.add("INBOX", letter(subject="Old"))
        support_watch.check()
        self.box.add("INBOX", letter())
        support_watch.check()
        self.assertEqual(len(self.pushes), 1)
        title, message = self.pushes[0]
        self.assertIn("Aurora", title)
        self.assertIn("Anna Bright", message)
        self.assertIn("anna@example.com", message)
        self.assertIn("My phone will not connect", message)

    def test_and_only_once(self):
        support_watch.check()
        self.box.add("INBOX", letter())
        support_watch.check()
        support_watch.check()
        support_watch.check()
        self.assertEqual(len(self.pushes), 1)

    def test_an_empty_mailbox_that_gets_its_first_letter(self):
        support_watch.check()
        self.assertEqual(config.SUPPORT_NOTIFY_MARK, "1:0")
        self.box.add("INBOX", letter())
        support_watch.check()
        self.assertEqual(len(self.pushes), 1)

    def test_each_letter_gets_a_push_of_its_own(self):
        support_watch.check()
        for n in range(3):
            self.box.add("INBOX", letter(sender=f"Person {n} <p{n}@example.com>", subject=f"Letter {n}"))
        support_watch.check()
        # Oldest first, so the newest is the last to arrive and the first seen.
        self.assertEqual([message for _, message in self.pushes],
                         [f"Person {n} <p{n}@example.com>: Letter {n}" for n in range(3)])

    def test_more_than_is_worth_a_push_each_are_counted(self):
        # After the process has been down, or when a script finds the address.
        support_watch.check()
        for n in range(7):
            self.box.add("INBOX", letter(sender=f"Person {n} <p{n}@example.com>", subject=f"Letter {n}"))
        support_watch.check()
        self.assertEqual(len(self.pushes), 1 + support_watch.NAMED)
        title, message = self.pushes[0]
        self.assertIn("7", title)
        self.assertIn("2 more", message)
        # The newest are announced and the oldest are the ones only counted.
        announced = " ".join(message for _, message in self.pushes[1:])
        self.assertIn("Letter 6", announced)
        self.assertNotIn("Letter 0", announced)
        self.assertNotIn("Letter 1", announced)
        self.assertIn("Letter 6", self.pushes[-1][1])

    def test_a_letter_with_no_subject_and_no_name_still_says_who(self):
        support_watch.check()
        self.box.add("INBOX", letter(sender="who@example.com", subject=""))
        support_watch.check()
        self.assertIn("who@example.com", self.pushes[0][1])
        self.assertIn("(no subject)", self.pushes[0][1])

    def test_a_letter_from_support_itself_is_not_one_to_it(self):
        # A copy of something sent, filed in the Inbox by a rule or a provider.
        support_watch.check()
        self.box.add("INBOX", letter(sender=f"Support <{SUPPORT}>", subject="Re: Hello"))
        support_watch.check()
        self.assertEqual(self.pushes, [])
        self.assertEqual(config.SUPPORT_NOTIFY_MARK, "1:1", "it would be looked at again every minute")

    def test_what_is_not_the_administrators_wording_is_in_the_panels_language(self):
        config.PANEL_LANG = "ru"
        support_watch.check()
        self.box.add("INBOX", letter(subject=""))
        support_watch.check()
        self.assertIn("(без темы)", self.pushes[0][1])


class TheAdministratorsWording(Watching):
    """The title and the text are written on the Gotify tab, like the registration push's."""

    def arrive(self, **kwargs):
        support_watch.check()
        self.box.add("INBOX", letter(**kwargs))
        support_watch.check()
        return self.pushes[-1]

    def test_the_title_and_the_text_are_the_ones_set(self):
        config.SUPPORT_NOTIFY_TITLE = "[{service}] support"
        config.SUPPORT_NOTIFY_MESSAGE = "{name} | {from} | {subject}"
        self.assertEqual(self.arrive(),
                         ("[Aurora] support",
                          "Anna Bright | anna@example.com | My phone will not connect"))

    def test_a_letter_with_no_name_leaves_the_name_empty(self):
        config.SUPPORT_NOTIFY_MESSAGE = "{name}|{from}|{sender}"
        self.assertEqual(self.arrive(sender="who@example.com")[1],
                         "|who@example.com|who@example.com")

    def test_a_substitution_that_does_not_exist_stays_as_it_was_typed(self):
        config.SUPPORT_NOTIFY_MESSAGE = "{sender} wrote about {topic}"
        self.assertEqual(self.arrive()[1], "Anna Bright <anna@example.com> wrote about {topic}")

    def test_a_text_with_a_stray_brace_still_goes_out(self):
        # A push that does not go because of a typo in a settings field is a
        # letter nobody is told about.
        config.SUPPORT_NOTIFY_MESSAGE = "{sender: {subject}"
        title, message = self.arrive()
        self.assertEqual(message, "{sender: {subject}")
        self.assertIn("Aurora", title)

    def test_what_a_letter_says_is_not_read_as_a_substitution(self):
        # The subject is a stranger's text; braces in it are braces.
        title, message = self.arrive(subject="Is {service} down? {0} {from}")
        self.assertEqual(message, "Anna Bright <anna@example.com>: Is {service} down? {0} {from}")


class TheWordingIsASetting(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(settings._stored), config.PANEL_LANG)

    def tearDown(self):
        settings._stored = self.saved[0]
        settings._apply()
        config.PANEL_LANG = self.saved[1]

    def test_an_untouched_title_follows_the_panels_language(self):
        settings._stored = {"PANEL_LANG": "ru"}
        settings._apply()
        self.assertEqual(config.SUPPORT_NOTIFY_TITLE, "Письмо в поддержку {service}")
        settings._stored = {"PANEL_LANG": "en"}
        settings._apply()
        self.assertEqual(config.SUPPORT_NOTIFY_TITLE, "A letter to {service} support")

    def test_one_that_was_written_is_left_exactly_as_written(self):
        settings._stored = {"PANEL_LANG": "ru", "SUPPORT_NOTIFY_TITLE": "Почта {service}"}
        settings._apply()
        self.assertEqual(config.SUPPORT_NOTIFY_TITLE, "Почта {service}")

    def test_neither_may_be_empty(self):
        for key in ("SUPPORT_NOTIFY_TITLE", "SUPPORT_NOTIFY_MESSAGE"):
            with self.assertRaises(ValueError):
                settings._coerce(key, "   ")
            self.assertEqual(settings._coerce(key, " {sender} "), "{sender}")


class WhatIsRemembered(Watching):
    def test_a_restart_does_not_repeat_what_was_said(self):
        support_watch.check()
        self.box.add("INBOX", letter())
        support_watch.check()
        support_watch._reset()              # the process starts again
        support_watch.check()
        self.assertEqual(len(self.pushes), 1)

    def test_a_letter_that_came_while_the_bot_was_down_is_still_announced(self):
        support_watch.check()
        support_watch._reset()
        self.box.add("INBOX", letter())     # nobody was looking
        support_watch.check()
        self.assertEqual(len(self.pushes), 1)

    def test_the_mark_is_written_before_the_push_goes(self):
        # A push that fails must not be the reason the same letter is announced
        # on every check from now on.
        support_watch.check()
        self.box.add("INBOX", letter())
        order = []
        support_watch._remember = lambda mark: order.append("remembered") or \
            setattr(config, "SUPPORT_NOTIFY_MARK", mark)
        mailer.send_gotify_notification = lambda title, message: order.append("pushed")
        support_watch.check()
        self.assertEqual(order, ["remembered", "pushed"])

    def test_nothing_new_writes_nothing(self):
        support_watch.check()
        written = len(self.remembered)
        support_watch.check()
        support_watch.check()
        self.assertEqual(len(self.remembered), written,
                         "settings.json would be rewritten once a minute for ever")

    def test_a_mailbox_that_was_rebuilt_starts_over_quietly(self):
        # UIDVALIDITY changes when a server renumbers a folder, or when the
        # settings are pointed at another mailbox altogether. The numbers kept
        # then mean nothing, and announcing everything "above" them would be
        # announcing the whole mailbox.
        self.box.add("INBOX", letter())
        support_watch.check()
        self.box.uidvalidity = 7
        self.box.add("INBOX", letter(subject="Another"))
        support_watch.check()
        self.assertEqual(self.pushes, [])
        self.assertEqual(config.SUPPORT_NOTIFY_MARK, "7:2")


class WhenItDoesNothing(Watching):
    def nothing_was_asked(self):
        self.assertEqual(self.box.connections, 0)
        self.assertEqual(self.pushes, [])

    def test_switched_off(self):
        config.SUPPORT_NOTIFY_ENABLED = False
        self.box.add("INBOX", letter())
        support_watch.check()
        self.nothing_was_asked()

    def test_gotify_not_set_up(self):
        config.GOTIFY_TOKEN = ""
        support_watch.check()
        self.nothing_was_asked()

    def test_support_not_set_up(self):
        config.SUPPORT_IMAP_PASSWORD = ""
        support_watch.check()
        self.nothing_was_asked()

    def test_a_mailbox_out_of_reach_is_not_an_error_in_the_loop(self):
        def refuse(*args, **kwargs):
            raise OSError("no route to host")
        imaplib.IMAP4_SSL = refuse
        with self.assertLogs("support_watch", level="WARNING") as first:
            support_watch.check()
        self.assertEqual(len(first.output), 1)
        # Said once, not once a minute for as long as it lasts.
        seen = []
        import logging
        handler = logging.Handler(level=logging.WARNING)
        handler.emit = seen.append
        logging.getLogger("support_watch").addHandler(handler)
        try:
            support_watch.check()
            support_watch.check()
        finally:
            logging.getLogger("support_watch").removeHandler(handler)
        self.assertEqual(seen, [])
        self.assertEqual(self.pushes, [])

    def test_it_looks_once_a_minute_not_on_every_poll(self):
        support_watch.run_if_due(now=1000.0)
        support_watch.run_if_due(now=1010.0)
        support_watch.run_if_due(now=1030.0)
        self.assertEqual(self.box.connections, 1)
        support_watch.run_if_due(now=1000.0 + support_watch.CHECK_SECONDS)
        self.assertEqual(self.box.connections, 2)


class TheMarkIsASetting(unittest.TestCase):
    def test_it_is_kept_out_of_the_file_until_there_is_one(self):
        self.assertIn("SUPPORT_NOTIFY_MARK", settings.WRITE_ON_DEMAND)
        self.assertIn("SUPPORT_NOTIFY_MARK", settings.MANAGED_KEYS)

    def test_anything_that_is_not_a_mark_reads_as_none(self):
        self.assertEqual(settings._coerce("SUPPORT_NOTIFY_MARK", "3857529045:412"), "3857529045:412")
        for junk in ("", None, "nonsense", "1:x", "1:2:3", 12):
            self.assertEqual(settings._coerce("SUPPORT_NOTIFY_MARK", junk), "")

    def test_the_switch_is_a_switch(self):
        self.assertIs(settings._coerce("SUPPORT_NOTIFY_ENABLED", "on"), True)
        self.assertIs(settings._coerce("SUPPORT_NOTIFY_ENABLED", False), False)


if __name__ == "__main__":
    unittest.main()
