"""
Reminding about the end of a subscription.

Nobody's reminder is recorded anywhere: the sweep looks at windows of time,
and consecutive windows have to meet exactly — a gap is somebody never told,
an overlap is somebody told twice. Most of what is here is about those edges.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import expiry
import templates

HOUR = 3600 * 1000
DAY = 24 * HOUR
NOW = 1_800_000_000_000


class FakeXui:
    def __init__(self, clients):
        self.clients = clients

    def get_all_clients(self):
        return None if self.clients is None else [dict(c) for c in self.clients]


class Case(unittest.TestCase):
    def setUp(self):
        self.saved = (config.EXPIRY_REMINDER_ENABLED, config.EXPIRY_REMINDER_DAYS,
                      config.EXPIRY_REMINDER_LAST_AT)
        config.EXPIRY_REMINDER_ENABLED = True
        config.EXPIRY_REMINDER_DAYS = 3
        config.EXPIRY_REMINDER_LAST_AT = 0.0
        self.real_shared = expiry.get_shared_client

    def tearDown(self):
        (config.EXPIRY_REMINDER_ENABLED, config.EXPIRY_REMINDER_DAYS,
         config.EXPIRY_REMINDER_LAST_AT) = self.saved
        expiry.get_shared_client = self.real_shared

    def clients(self, *clients):
        expiry.get_shared_client = lambda: FakeXui(list(clients) if clients != (None,) else None)

    @staticmethod
    def client(end, email="ann@example.com", **over):
        return {"email": email, "expiryTime": end, "enable": True, **over}


class TheWindows(Case):
    def test_consecutive_sweeps_meet_without_a_gap_or_an_overlap(self):
        first_soon, first_ended = expiry.windows(NOW, NOW - HOUR)
        second_soon, second_ended = expiry.windows(NOW + HOUR, NOW)
        self.assertEqual(first_soon[1], second_soon[0])
        self.assertEqual(first_ended[1], second_ended[0])

    def test_the_first_sweep_warns_everybody_within_reach(self):
        soon, ended = expiry.windows(NOW, 0)
        self.assertEqual(soon, (NOW, NOW + 3 * DAY))
        self.assertEqual(ended, (NOW - DAY, NOW))

    def test_after_a_long_stop_an_ended_subscription_is_not_called_near(self):
        soon, ended = expiry.windows(NOW, NOW - 10 * DAY)
        self.assertEqual(soon[0], NOW)
        self.assertEqual(ended, (NOW - 10 * DAY, NOW))


class WhoIsWritten(Case):
    def owed(self, *clients, last=NOW - HOUR):
        self.clients(*clients)
        return [(e, k) for e, k, *_ in expiry.owed(NOW, last)]

    def test_an_end_coming_within_reach_is_warned(self):
        self.assertEqual(self.owed(self.client(NOW + 3 * DAY - HOUR // 2)),
                         [("ann@example.com", "soon")])

    def test_an_end_that_has_just_passed_is_told(self):
        self.assertEqual(self.owed(self.client(NOW - HOUR // 2)), [("ann@example.com", "ended")])

    def test_an_end_already_warned_about_is_not_warned_again(self):
        self.assertEqual(self.owed(self.client(NOW + 2 * DAY)), [])

    def test_no_end_is_never_written_to(self):
        self.assertEqual(self.owed(self.client(0), self.client(-5 * DAY), last=0), [])

    def test_a_client_switched_off_by_hand_is_not_written_to(self):
        self.assertEqual(self.owed(self.client(NOW + 3 * DAY - HOUR // 2, enable=False)), [])

    def test_a_client_switched_off_for_ending_is(self):
        self.assertEqual(self.owed(self.client(NOW - HOUR // 2, enable=False)),
                         [("ann@example.com", "ended")])

    def test_a_row_with_no_address_is_skipped(self):
        self.assertEqual(self.owed(self.client(NOW - HOUR // 2, email="by-hand")), [])

    def test_no_answer_from_3xui_is_none_not_nobody(self):
        self.clients(None)
        self.assertIsNone(expiry.owed(NOW, NOW - HOUR))


class TheRun(Case):
    def setUp(self):
        super().setUp()
        self.events = []
        self.real = (expiry._remember, expiry.send, expiry._now_ms)
        expiry._remember = lambda when: self.events.append("remember")
        expiry.send = lambda letters: self.events.append(("send", len(letters)))
        expiry._now_ms = lambda: NOW

    def tearDown(self):
        expiry._remember, expiry.send, expiry._now_ms = self.real
        super().tearDown()

    def test_the_sweep_is_written_down_before_the_letters(self):
        self.clients(self.client(NOW + DAY))
        expiry.run_if_due()
        self.assertEqual(self.events, ["remember", ("send", 1)])

    def test_nothing_is_written_down_when_3xui_could_not_be_asked(self):
        self.clients(None)
        expiry.run_if_due()
        self.assertEqual(self.events, [])

    def test_it_waits_out_the_hour(self):
        config.EXPIRY_REMINDER_LAST_AT = (NOW - HOUR // 2) / 1000
        self.clients(self.client(NOW + DAY))
        expiry.run_if_due()
        self.assertEqual(self.events, [])

    def test_switched_off_means_never(self):
        config.EXPIRY_REMINDER_ENABLED = False
        self.clients(self.client(NOW + DAY))
        expiry.run_if_due()
        self.assertEqual(self.events, [])


class TheLetter(unittest.TestCase):
    def setUp(self):
        self.real = templates.selling

    def tearDown(self):
        templates.selling = self.real

    def test_while_selling_it_has_a_button_that_writes_buy_to_the_bot(self):
        templates.selling = lambda: True
        letter = templates.get_expiry_email("soon", NOW + 3 * DAY, 3, "Month", "bot@example.com")
        self.assertIn("mailto:bot@example.com?subject=/buy", letter.html)
        self.assertIn("mailto:bot@example.com?subject=/buy", letter.text)

    def test_while_nothing_is_for_sale_it_says_whom_to_ask(self):
        templates.selling = lambda: False
        letter = templates.get_expiry_email("ended", NOW, 1, "", "bot@example.com")
        self.assertNotIn("mailto:", letter.html)
        self.assertIn(templates._plain(templates.text("expiry.no_sale")), letter.text)


if __name__ == "__main__":
    unittest.main()
