"""
Reminding a client who has not connected in a while.

Modelled on tests/test_cleanup.py: the schedule is checked the same way the
mailbox cleanup is, and what matters about the sweep itself is who it leaves
alone as much as who it mails.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import inactivity


DAY = 86400
DAY_MS = DAY * 1000


class FakeXui:
    """None for either list is 3x-ui not answering, the way XuiClient reports it."""

    def __init__(self, clients, last_online=None):
        self._clients = clients
        self._last_online = {} if last_online is None else last_online

    def get_all_clients(self):
        return None if self._clients is None else [dict(c) for c in self._clients]

    def get_last_online(self):
        return None if self._last_online is NO_ANSWER else dict(self._last_online)


NO_ANSWER = object()


class WhenItIsDue(unittest.TestCase):
    def setUp(self):
        self.saved = (config.INACTIVITY_REMINDER_ENABLED, config.INACTIVITY_REMINDER_DAYS,
                      config.INACTIVITY_REMINDER_LAST_AT)
        config.INACTIVITY_REMINDER_ENABLED = True
        config.INACTIVITY_REMINDER_DAYS = 30
        config.INACTIVITY_REMINDER_LAST_AT = 0.0

    def tearDown(self):
        (config.INACTIVITY_REMINDER_ENABLED, config.INACTIVITY_REMINDER_DAYS,
         config.INACTIVITY_REMINDER_LAST_AT) = self.saved

    def test_switched_off_means_never(self):
        config.INACTIVITY_REMINDER_ENABLED = False
        config.INACTIVITY_REMINDER_LAST_AT = 0.0
        self.assertFalse(inactivity.sweep_due(now=10 ** 9))

    def test_the_first_poll_after_switching_it_on_runs_it(self):
        self.assertTrue(inactivity.sweep_due(now=10 ** 9))

    def test_it_waits_out_the_interval(self):
        now = 10 ** 9
        config.INACTIVITY_REMINDER_LAST_AT = now - 29 * DAY
        self.assertFalse(inactivity.sweep_due(now=now))
        config.INACTIVITY_REMINDER_LAST_AT = now - 30 * DAY
        self.assertTrue(inactivity.sweep_due(now=now))

    def test_a_shorter_interval_comes_round_sooner(self):
        now = 10 ** 9
        config.INACTIVITY_REMINDER_DAYS = 1
        config.INACTIVITY_REMINDER_LAST_AT = now - 2 * DAY
        self.assertTrue(inactivity.sweep_due(now=now))


class WhoGetsMailed(unittest.TestCase):
    def setUp(self):
        self.saved_days = config.INACTIVITY_REMINDER_DAYS
        config.INACTIVITY_REMINDER_DAYS = 30
        self.now_ms = int(1_800_000_000 * 1000)
        self.real_time = inactivity.time.time
        inactivity.time.time = lambda: self.now_ms / 1000

        self.sent = []
        self.real_send = inactivity.mailer.send_email_reply
        inactivity.mailer.send_email_reply = lambda to, subject, message: self.sent.append(to)

        self.real_shared = inactivity.get_shared_client

    def tearDown(self):
        config.INACTIVITY_REMINDER_DAYS = self.saved_days
        inactivity.time.time = self.real_time
        inactivity.mailer.send_email_reply = self.real_send
        inactivity.get_shared_client = self.real_shared

    def _run(self, clients, last_online=None):
        fake = FakeXui(clients, last_online)
        inactivity.get_shared_client = lambda: fake
        return inactivity.send(inactivity.overdue())

    def _client(self, **over):
        base = {"email": "somebody@example.com", "enable": True,
                "createdAt": self.now_ms - 400 * DAY_MS}
        base.update(over)
        return base

    def test_a_client_gone_longer_than_the_threshold_is_mailed(self):
        old = self.now_ms - 40 * DAY_MS
        count = self._run([self._client(email="gone@example.com")],
                          last_online={"gone@example.com": old})
        self.assertEqual(count, 1)
        self.assertEqual(self.sent, ["gone@example.com"])

    def test_a_client_seen_recently_is_left_alone(self):
        recent = self.now_ms - 5 * DAY_MS
        count = self._run([self._client(email="here@example.com")],
                          last_online={"here@example.com": recent})
        self.assertEqual(count, 0)
        self.assertEqual(self.sent, [])

    def test_never_seen_online_falls_back_to_the_registration_date(self):
        # No last-online entry at all: a client added by hand, or one who
        # registered and never connected once.
        client = self._client(email="ghost@example.com",
                              createdAt=self.now_ms - 45 * DAY_MS)
        count = self._run([client], last_online={})
        self.assertEqual(count, 1)

    def test_a_brand_new_client_with_no_last_online_is_not_overdue(self):
        client = self._client(email="fresh@example.com",
                              createdAt=self.now_ms - 2 * DAY_MS)
        count = self._run([client], last_online={})
        self.assertEqual(count, 0)

    def test_a_disabled_client_is_left_alone(self):
        old = self.now_ms - 40 * DAY_MS
        client = self._client(email="blocked@example.com", enable=False)
        count = self._run([client], last_online={"blocked@example.com": old})
        self.assertEqual(count, 0)
        self.assertEqual(self.sent, [])

    def test_a_client_with_no_address_is_skipped(self):
        # Made by hand in 3x-ui: nowhere to mail such a row.
        old = self.now_ms - 40 * DAY_MS
        client = self._client(email="not-an-address", createdAt=old)
        count = self._run([client], last_online={})
        self.assertEqual(count, 0)

    def test_one_failed_letter_does_not_stop_the_rest(self):
        old = self.now_ms - 40 * DAY_MS

        def fail_then_record(to, subject, message):
            if to == "first@example.com":
                raise RuntimeError("SMTP is briefly down")
            self.sent.append(to)

        inactivity.mailer.send_email_reply = fail_then_record
        clients = [self._client(email="first@example.com"),
                   self._client(email="second@example.com")]
        count = self._run(clients, last_online={
            "first@example.com": old, "second@example.com": old,
        })
        self.assertEqual(count, 1)
        self.assertEqual(self.sent, ["second@example.com"])


class When3xuiDoesNotAnswer(WhoGetsMailed):
    """
    The bug this guards against: a missing last-online map read as an empty
    one made every client fall back to the registration date, and mailed
    everybody registered longer ago than the threshold — online or not.
    """

    def test_no_last_online_map_mails_nobody(self):
        fake = FakeXui([self._client(email="online-now@example.com")], NO_ANSWER)
        inactivity.get_shared_client = lambda: fake
        self.assertIsNone(inactivity.overdue())

    def test_no_client_list_mails_nobody(self):
        fake = FakeXui(None, {})
        inactivity.get_shared_client = lambda: fake
        self.assertIsNone(inactivity.overdue())


class TheRun(unittest.TestCase):
    """What run_if_due writes down, and when."""

    def setUp(self):
        self.events = []
        self.real = (inactivity.sweep_due, inactivity.overdue, inactivity.send,
                     inactivity._remember)
        inactivity.sweep_due = lambda: True
        inactivity._remember = lambda when: self.events.append("remember")
        inactivity.send = lambda found: self.events.append(("send", list(found)))

    def tearDown(self):
        (inactivity.sweep_due, inactivity.overdue, inactivity.send,
         inactivity._remember) = self.real

    def test_the_run_is_written_down_before_the_first_letter(self):
        # A restart in the middle of sending must not mail everybody again.
        inactivity.overdue = lambda: [("gone@example.com", 40)]
        inactivity.run_if_due()
        self.assertEqual(self.events, ["remember", ("send", [("gone@example.com", 40)])])

    def test_nothing_is_written_down_when_3xui_could_not_be_asked(self):
        # Otherwise an outage would push the next sweep a whole period away.
        inactivity.overdue = lambda: None
        inactivity.run_if_due()
        self.assertEqual(self.events, [])

    def test_a_sweep_that_finds_nobody_still_counts(self):
        inactivity.overdue = lambda: []
        inactivity.run_if_due()
        self.assertEqual(self.events, ["remember", ("send", [])])

    def test_not_due_does_nothing(self):
        inactivity.sweep_due = lambda: False
        inactivity.overdue = lambda: self.fail("asked 3x-ui while not due")
        inactivity.run_if_due()
        self.assertEqual(self.events, [])


class ThePeriod(unittest.TestCase):
    def setUp(self):
        self.saved = config.INACTIVITY_REMINDER_DAYS

    def tearDown(self):
        config.INACTIVITY_REMINDER_DAYS = self.saved

    def test_an_unreadable_period_falls_back_to_the_default(self):
        import periodic
        for value in (None, 0, "", "nonsense"):
            config.INACTIVITY_REMINDER_DAYS = value
            self.assertEqual(periodic.period_days("INACTIVITY_REMINDER_DAYS"), 30)

    def test_a_negative_period_is_still_a_day(self):
        import periodic
        config.INACTIVITY_REMINDER_DAYS = -5
        self.assertEqual(periodic.period_days("INACTIVITY_REMINDER_DAYS"), 1)


class TheSetting(unittest.TestCase):
    def test_the_interval_is_at_least_a_day(self):
        import settings
        self.assertEqual(settings._coerce("INACTIVITY_REMINDER_DAYS", "7"), 7)
        with self.assertRaises(ValueError):
            settings._coerce("INACTIVITY_REMINDER_DAYS", "0")

    def test_an_unreadable_timestamp_means_not_yet(self):
        import settings
        self.assertEqual(settings._coerce("INACTIVITY_REMINDER_LAST_AT", "nonsense"), 0.0)
        self.assertEqual(settings._coerce("INACTIVITY_REMINDER_LAST_AT", "12.5"), 12.5)

    def test_it_is_off_until_somebody_asks(self):
        import settings
        self.assertFalse(settings.BASE["INACTIVITY_REMINDER_ENABLED"])


if __name__ == "__main__":
    unittest.main()
