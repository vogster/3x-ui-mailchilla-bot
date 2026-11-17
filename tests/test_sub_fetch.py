"""
When a client's app last fetched the subscription.

3x-ui keeps that moment with the client's traffic counters. It answers a
question "last online" cannot: not whether the app is connected, but whether
it was ever set up. Somebody who registered and never fetched the subscription
has not got as far as that — and is not helped by a reminder to check a
subscription they have never used.

The mistake to make here is the one made about device limits before: reading
a panel that does not say as one that says "never". A 3x-ui from before the
field existed would then show every client as somebody who never set the app
up, and write to all of them as such.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import unittest

import config
import email_texts
import inactivity
import templates
from admin.rows import DAY_MS, HOUR_MS, client_row
from test_panel_routes import CLIENTS, PanelCase
from xui_client import XuiClient

NOW_MS = 1_800_000_000_000


def client(**traffic):
    """A row the way clients/list hands it over, with these traffic fields."""
    return {"uuid": "c0ffee01", "email": "ben@example.com", "enable": True,
            "createdAt": NOW_MS - 400 * DAY_MS, "traffic": {"up": 1, "down": 2, **traffic}}


class WhatThePanelSays(unittest.TestCase):
    def test_a_moment_is_a_moment(self):
        self.assertEqual(XuiClient.last_sub_fetch(client(lastSubFetch=1_794_870_727_491)),
                         1_794_870_727_491)

    def test_never_is_nought(self):
        self.assertEqual(XuiClient.last_sub_fetch(client(lastSubFetch=0)), 0)

    def test_a_panel_that_does_not_say_is_not_a_panel_that_says_never(self):
        for row in (client(), {"email": "ben@example.com"}, {"traffic": None},
                    {"traffic": "nonsense"}, {}, None):
            with self.subTest(row=row):
                self.assertIsNone(XuiClient.last_sub_fetch(row))

    def test_seconds_are_read_as_the_same_moment(self):
        # One build of the panel writes seconds, another milliseconds.
        self.assertEqual(XuiClient.last_sub_fetch(client(lastSubFetch=1_794_870_727)),
                         1_794_870_727_000)

    def test_what_is_not_a_time_reads_as_never_rather_than_breaking_the_page(self):
        for junk in (-5, "nonsense", None, []):
            with self.subTest(junk=junk):
                self.assertEqual(XuiClient.last_sub_fetch(client(lastSubFetch=junk)), 0)


class TheRow(unittest.TestCase):
    def setUp(self):
        self.saved_lang = config.PANEL_LANG
        config.PANEL_LANG = "en"
        import admin.rows as rows
        self.rows, self.real_time = rows, rows.time.time
        rows.time.time = lambda: NOW_MS / 1000

    def tearDown(self):
        self.rows.time.time = self.real_time
        config.PANEL_LANG = self.saved_lang

    def test_a_panel_that_does_not_say_leaves_the_row_saying_nothing(self):
        row = client_row(client())
        self.assertFalse(row["sub_fetch_known"])

    def test_never_fetched_is_said_in_a_word(self):
        row = client_row(client(lastSubFetch=0))
        self.assertTrue(row["sub_fetch_known"])
        self.assertEqual((row["sub_fetch_ms"], row["sub_fetch"], row["sub_fetch_full"]),
                         (0, "never", ""))
        self.assertEqual(row["sub_fetch_state"], "0")

    def test_but_not_of_somebody_the_panel_has_seen_connect(self):
        # Set up before the panel began recording fetches. Listed under
        # "never", somebody online this minute would stand among the people
        # who registered and got no further.
        remark = "ben@example.com"
        for name, online, last_online in (
                ("online now", {remark}, {}),
                ("seen last month", set(), {remark: NOW_MS - 30 * DAY_MS})):
            with self.subTest(case=name):
                row = client_row(client(lastSubFetch=0), online, last_online)
                self.assertEqual(row["sub_fetch"], "no record")
                self.assertEqual(row["sub_fetch_state"], "")

    def test_the_three_answers_the_filter_matches_against(self):
        self.assertEqual(client_row(client(lastSubFetch=NOW_MS - HOUR_MS))["sub_fetch_state"], "1")
        self.assertEqual(client_row(client(lastSubFetch=0))["sub_fetch_state"], "0")
        self.assertEqual(client_row(client())["sub_fetch_state"], "")

    def test_a_recent_fetch_is_how_long_ago(self):
        row = client_row(client(lastSubFetch=NOW_MS - 3 * HOUR_MS))
        self.assertEqual(row["sub_fetch"], "3 h ago")
        self.assertTrue(row["sub_fetch_full"])

    def test_an_old_one_is_a_date(self):
        row = client_row(client(lastSubFetch=NOW_MS - 40 * DAY_MS))
        self.assertRegex(row["sub_fetch"], r"\d")
        self.assertNotIn("ago", row["sub_fetch"])


class WhoNeverSetItUp(unittest.TestCase):
    """Which letter somebody who has gone quiet is owed."""

    def never(self, row, last_online=None):
        return inactivity._never_set_up(row, last_online or {})

    def test_never_fetched_and_never_seen_is_never_set_up(self):
        self.assertTrue(self.never(client(lastSubFetch=0)))

    def test_seen_online_once_is_somebody_who_did(self):
        # The panel began recording fetches at some version. A client set up
        # before it, who has not opened the app since, still reads as never
        # having fetched — but was connected once.
        self.assertFalse(self.never(client(lastSubFetch=0),
                                    {"ben@example.com": NOW_MS - 90 * DAY_MS}))

    def test_a_fetch_is_a_fetch_whatever_the_panel_saw_after(self):
        self.assertFalse(self.never(client(lastSubFetch=NOW_MS - 90 * DAY_MS)))

    def test_a_panel_that_does_not_say_is_no_ground_for_saying_so(self):
        self.assertFalse(self.never(client()))


class TheReminder(unittest.TestCase):
    class Panel:
        def __init__(self, clients, last_online):
            self.clients, self.last_online = clients, last_online

        def get_all_clients(self):
            return [dict(c) for c in self.clients]

        def get_last_online(self):
            return dict(self.last_online)

    def setUp(self):
        self.saved = (config.INACTIVITY_REMINDER_DAYS, config.MAIL_LANG, config.SERVICE_NAME,
                      config.SUPPORT_EMAIL)
        config.INACTIVITY_REMINDER_DAYS, config.MAIL_LANG = 30, "en"
        config.SERVICE_NAME, config.SUPPORT_EMAIL = "Aurora", ""
        self.real = (inactivity.time.time, inactivity.mailer.send_email_reply,
                     inactivity.get_shared_client)
        inactivity.time.time = lambda: NOW_MS / 1000
        self.sent = {}
        inactivity.mailer.send_email_reply = \
            lambda to, subject, message: self.sent.__setitem__(to, message.text)

    def tearDown(self):
        (inactivity.time.time, inactivity.mailer.send_email_reply,
         inactivity.get_shared_client) = self.real
        (config.INACTIVITY_REMINDER_DAYS, config.MAIL_LANG, config.SERVICE_NAME,
         config.SUPPORT_EMAIL) = self.saved

    def run_sweep(self, clients, last_online=None):
        panel = self.Panel(clients, last_online or {})
        inactivity.get_shared_client = lambda: panel
        found = inactivity.overdue()
        inactivity.send(found)
        return found

    def somebody(self, address, **traffic):
        return {"email": address, "enable": True, "createdAt": NOW_MS - 45 * DAY_MS,
                "traffic": {"up": 0, "down": 0, **traffic}}

    def test_each_gets_the_letter_that_is_about_them(self):
        found = self.run_sweep(
            [self.somebody("never@example.com", lastSubFetch=0),
             self.somebody("lapsed@example.com", lastSubFetch=NOW_MS - 44 * DAY_MS),
             self.somebody("oldpanel@example.com")],
            last_online={"lapsed@example.com": NOW_MS - 40 * DAY_MS})
        self.assertEqual({address: never for address, _, never in found},
                         {"never@example.com": True, "lapsed@example.com": False,
                          "oldpanel@example.com": False})
        self.assertIn("never set up", self.sent["never@example.com"])
        self.assertNotIn("/status", self.sent["never@example.com"])
        for address in ("lapsed@example.com", "oldpanel@example.com"):
            self.assertIn("/status", self.sent[address])
            self.assertNotIn("never set up", self.sent[address])

    def test_how_long_it_has_been_is_in_both(self):
        self.run_sweep([self.somebody("never@example.com", lastSubFetch=0),
                        self.somebody("oldpanel@example.com")])
        for text in self.sent.values():
            self.assertIn("45 days", text)


class TheLetter(unittest.TestCase):
    def setUp(self):
        self.saved = (config.MAIL_LANG, config.SERVICE_NAME, config.SUPPORT_EMAIL)
        config.SERVICE_NAME = "Aurora"

    def tearDown(self):
        config.MAIL_LANG, config.SERVICE_NAME, config.SUPPORT_EMAIL = self.saved

    def test_the_two_texts_differ_in_both_languages(self):
        for lang in ("en", "ru"):
            with self.subTest(lang=lang):
                config.MAIL_LANG, config.SUPPORT_EMAIL = lang, ""
                usual = templates.get_inactivity_email("ben@example.com", 30)
                unused = templates.get_inactivity_email("ben@example.com", 30, never_set_up=True)
                self.assertNotEqual(usual.text, unused.text)
                self.assertNotEqual(usual.html, unused.html)
                self.assertIn("/help", unused.text)
                self.assertIn("30", unused.text)
                self.assertIn("Aurora", unused.text)

    def test_the_support_line_is_under_either(self):
        config.MAIL_LANG, config.SUPPORT_EMAIL = "en", "help@example.com"
        for never in (False, True):
            with self.subTest(never_set_up=never):
                mail = templates.get_inactivity_email("ben@example.com", 30, never_set_up=never)
                self.assertIn("help@example.com", mail.text)

    def test_the_stand_ins_agree_with_the_panel_about_a_new_client(self):
        # A panel that records fetches says nought for a new client; the
        # stand's must too, or everyone registered on it reads as "the panel
        # does not say".
        import os
        import sys
        sys.path.insert(0, os.path.join(sandbox.ROOT, "docs"))
        import stand_panel
        panel = stand_panel.Panel()
        panel.add_client(email="new@example.com", inbound_ids=[1])
        self.assertEqual(XuiClient.last_sub_fetch(panel.find("new@example.com")), 0)

    def test_the_text_is_one_the_administrator_can_rewrite(self):
        keys = [key for group in email_texts.GROUPS if group["id"] == "inactivity"
                for key, *_ in email_texts.walk(group["fields"])]
        self.assertIn("inactivity.text_unused", keys)


class OnThePanel(PanelCase):
    """The card and the list — and a panel that does not say, which shows nothing."""

    def with_fetches(self, **by_address):
        """The stand-in panel's clients, with these fetch times in their traffic."""
        saved = [dict(row, traffic=dict(row["traffic"])) for row in CLIENTS]
        for row in CLIENTS:
            if row["email"] in by_address:
                row["traffic"]["lastSubFetch"] = by_address[row["email"]]

        def restore():
            CLIENTS[:] = saved

        self.addCleanup(restore)

    def test_a_panel_that_does_not_say_shows_neither_the_card_nor_the_filter(self):
        self.assertNotIn("Subscription fetched", self.page("/clients/c0ffee01"))
        self.assertNotIn('data-filter="sub"', self.page("/clients"))

    def test_the_card_says_when(self):
        import time
        self.with_fetches(**{"ben@example.com": int(time.time() * 1000) - 2 * 3600 * 1000})
        body = self.page("/clients/c0ffee01")
        self.assertIn("Subscription fetched", body)
        self.assertIn("2 h ago", body)

    def test_and_says_never_for_somebody_who_never_did(self):
        # The stand-in panel has never seen this one connect either.
        self.with_fetches(**{"old-timer@example.com": 0})
        body = self.page("/clients/c0ffee02")
        self.assertIn("Subscription fetched", body)
        self.assertRegex(body, r'class="muted">never<')

    def test_but_no_record_for_somebody_who_is_connected(self):
        # Ben is online on the stand-in panel. "Never", beside "online", would
        # be the card contradicting itself — and the reminder, which would not
        # write to him as somebody who never set the app up.
        self.with_fetches(**{"ben@example.com": 0})
        body = self.page("/clients/c0ffee01")
        self.assertIn("Subscription fetched", body)
        self.assertRegex(body, r'class="muted">no record<')
        self.assertNotRegex(body, r'class="muted">never<')
        self.assertIn("has no record of a fetch", body)

    def test_the_sample_letter_can_be_either_text(self):
        # The second goes only to somebody who never set the app up — nobody
        # the administrator can become for the sake of looking at it.
        import email_bot
        body = self.page("/settings")
        self.assertIn("Never set the app up", body)
        sent = []
        real = email_bot.send_email_reply
        email_bot.send_email_reply = lambda to, subject, message, **kw: sent.append(message.text)
        try:
            for kind in ("quiet", "unused", "nonsense"):
                response = self.client.post("/settings/texts/test", data={
                    "group": "inactivity", "email": "admin@example.com", "kind": kind})
                self.assertEqual(response.status_code, 200)
        finally:
            email_bot.send_email_reply = real
        usual, unused, fallback = sent
        self.assertIn("/status", usual)
        self.assertIn("never set up", unused)
        # A kind the page made up gets the usual text, not an error and not a guess.
        self.assertEqual(fallback, usual)

    def test_the_list_offers_the_filter_and_marks_each_row(self):
        import time
        self.with_fetches(**{"ben@example.com": int(time.time() * 1000) - 3600 * 1000,
                             "old-timer@example.com": 0})
        body = self.page("/clients")
        self.assertIn('data-filter="sub"', body)
        self.assertIn('data-sub="1"', body)
        self.assertIn('data-sub="0"', body)

    def test_a_row_the_panel_says_nothing_about_matches_neither_answer(self):
        # One client with the field and one without: the second is neither
        # "fetched" nor "never", and the filter must not put it under either.
        self.with_fetches(**{"old-timer@example.com": 0})
        body = self.page("/clients")
        self.assertIn('data-sub="0"', body)
        self.assertIn('data-sub=""', body)

    def test_nor_does_somebody_connected_whose_fetch_was_never_recorded(self):
        # Both rows carry the field and both say nought; only the one never
        # seen online is somebody who never set the app up.
        self.with_fetches(**{"ben@example.com": 0, "old-timer@example.com": 0})
        body = self.page("/clients")
        self.assertEqual(body.count('data-sub="0"'), 1)
        self.assertEqual(body.count('data-sub=""'), 1)


if __name__ == "__main__":
    unittest.main()
