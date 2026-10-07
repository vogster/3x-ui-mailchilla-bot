"""
The three limits in abuse.py, and how the bot behaves at each of them.

All in memory and all about time, so each test sets the clock and the
counters itself and leaves them as it found them.
"""
import os
import sys
import unittest
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import abuse
import config
import email_bot
import templates

T = 1_000_000.0


class Case(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(config, k) for k in
                      ("LETTERS_PER_HOUR", "BLOCK_DISPOSABLE", "BLOCKED_DOMAINS",
                       "REGISTRATIONS_PER_HOUR", "ADMIN_EMAIL")}
        self.state = (abuse._letters, abuse._registrations, abuse._limit_reported)
        abuse._letters, abuse._registrations, abuse._limit_reported = {}, deque(), False
        config.ADMIN_EMAIL = "admin@example.com"
        self.real_notify = abuse.__dict__.get("notify")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        abuse._letters, abuse._registrations, abuse._limit_reported = self.state


class LettersFromOneAddress(Case):
    def test_past_the_limit_a_letter_is_not_answered(self):
        config.LETTERS_PER_HOUR = 3
        answers = [abuse.allow_letter("ann@example.com", n, T + n) for n in range(5)]
        self.assertEqual(answers, [True, True, True, False, False])

    def test_the_hour_moves_on(self):
        config.LETTERS_PER_HOUR = 1
        self.assertTrue(abuse.allow_letter("ann@example.com", "a", T))
        self.assertFalse(abuse.allow_letter("ann@example.com", "b", T + 60))
        self.assertTrue(abuse.allow_letter("ann@example.com", "c", T + 3601))

    def test_a_letter_looked_at_again_is_not_counted_again(self):
        # One left unread to wait for room in the registrations hour comes
        # round on every poll; it is still one letter.
        config.LETTERS_PER_HOUR = 1
        for poll in range(10):
            self.assertTrue(abuse.allow_letter("ann@example.com", "same", T + poll))

    def test_the_administrator_is_never_limited(self):
        config.LETTERS_PER_HOUR = 1
        for n in range(5):
            self.assertTrue(abuse.allow_letter("admin@example.com", n, T + n))

    def test_zero_is_no_limit(self):
        config.LETTERS_PER_HOUR = 0
        self.assertTrue(all(abuse.allow_letter("ann@example.com", n, T) for n in range(50)))


class ThrowawayDomains(Case):
    def test_a_known_one_and_its_subdomains(self):
        config.BLOCK_DISPOSABLE, config.BLOCKED_DOMAINS = True, ""
        self.assertTrue(abuse.is_blocked_domain("x@mailinator.com"))
        self.assertTrue(abuse.is_blocked_domain("x@eu.mailinator.com"))
        self.assertFalse(abuse.is_blocked_domain("x@gmail.com"))
        self.assertFalse(abuse.is_blocked_domain("x@notmailinator.com"))

    def test_switched_off_only_the_panels_own_list_applies(self):
        config.BLOCK_DISPOSABLE, config.BLOCKED_DOMAINS = False, "spam.example\n@junk.example"
        self.assertFalse(abuse.is_blocked_domain("x@mailinator.com"))
        self.assertTrue(abuse.is_blocked_domain("x@spam.example"))
        self.assertTrue(abuse.is_blocked_domain("x@junk.example"))


class RegistrationsPerHour(Case):
    def test_past_the_limit_registrations_wait(self):
        config.REGISTRATIONS_PER_HOUR = 2
        import notify
        real = notify.push
        pushed = []
        notify.push = lambda *a, **kw: pushed.append(a[0])
        try:
            for n in range(2):
                self.assertTrue(abuse.registration_allowed(T + n))
                abuse.note_registration(T + n)
            self.assertFalse(abuse.registration_allowed(T + 10))
            self.assertFalse(abuse.registration_allowed(T + 20))
            self.assertTrue(abuse.registration_allowed(T + 3700))
        finally:
            notify.push = real
        self.assertEqual(pushed, ["registration_limit"])


class TheBotAtTheLimits(Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.real = {n: getattr(email_bot, n) for n in ("get_shared_client", "send_email_reply")}
        email_bot.send_email_reply = lambda to, subject, message, reply_to=None: self.sent.append(subject)
        email_bot.get_shared_client = lambda: type("X", (), {"find_client_by_email": lambda self, e: None})()

    def tearDown(self):
        for n, v in self.real.items():
            setattr(email_bot, n, v)
        super().tearDown()

    def test_a_throwaway_address_is_told_and_not_registered(self):
        config.BLOCK_DISPOSABLE = True
        result = email_bot.handle_registration("x@yopmail.com", tariff={"name": "Trial"})
        self.assertIsNone(result)
        self.assertEqual(self.sent, [templates.notice_subject("domain_blocked")])

    def test_a_full_hour_leaves_the_letter_for_later(self):
        config.BLOCK_DISPOSABLE = False
        config.REGISTRATIONS_PER_HOUR = 1
        abuse._limit_reported = True  # no push from a test
        abuse.note_registration()
        result = email_bot.handle_registration("ann@example.com", tariff={"name": "Trial"})
        self.assertEqual(result, email_bot.DEFERRED)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
