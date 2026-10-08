"""
Words carrying bonus days: added to a client, a term for somebody new, once
per address.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_bot
import purchases
import tariffs
import templates

DAY = 86400 * 1000


class FakeXui:
    def __init__(self, client=None):
        self.client = client
        self.updates = []

    def find_client_by_email(self, email):
        return dict(self.client) if self.client else None

    @staticmethod
    def client_key(client):
        return client.get("uuid")

    def update_client(self, key, **kwargs):
        self.updates.append(kwargs)
        return True


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-bonus-")
        self.real_path = tariffs.TARIFFS_PATH
        tariffs.TARIFFS_PATH = os.path.join(self.dir, "tariffs.json")
        self.saved_state = tariffs.snapshot()
        tariffs._state = {"tariffs": [], "codes": []}
        self.tariff = tariffs.save_tariff({"name": "Trial", "limit_gb": 10, "expire_days": 3,
                                           "inbound_ids": [1]})
        self.code = tariffs.save_code({"word": "GIFTWEEK", "tariff_id": self.tariff["id"],
                                       "uses_left": None, "enabled": True, "bonus_days": 7})
        self.sent, self.registered = [], []
        self.real = {n: getattr(email_bot, n) for n in
                     ("get_shared_client", "send_email_reply", "handle_registration")}
        email_bot.send_email_reply = lambda to, subject, message, reply_to=None: self.sent.append(subject)
        email_bot.handle_registration = lambda addr, name, tariff, code: \
            self.registered.append((addr, tariff["expire_days"], code["word"]))
        self.now = purchases._now_ms()

    def tearDown(self):
        for n, v in self.real.items():
            setattr(email_bot, n, v)
        tariffs.TARIFFS_PATH = self.real_path
        tariffs._state = self.saved_state

    def use(self, client):
        fake = FakeXui(client)
        email_bot.get_shared_client = lambda: fake
        return fake

    def write(self):
        email_bot.handle_bonus("ann@example.com", "", self.tariff, tariffs.get_code("GIFTWEEK"))


class Bonus(Case):
    def test_a_client_gets_the_days_added(self):
        xui = self.use({"uuid": "u", "email": "ann@example.com", "expiryTime": self.now + 10 * DAY})
        self.write()
        self.assertAlmostEqual(xui.updates[0]["expiry_ms"], self.now + 17 * DAY, delta=60_000)
        self.assertTrue(xui.updates[0]["enable"])
        self.assertEqual(self.sent, [templates.notice_subject("bonus_added")])

    def test_only_once_per_address(self):
        xui = self.use({"uuid": "u", "email": "ann@example.com", "expiryTime": self.now})
        self.write()
        self.write()
        self.assertEqual(len(xui.updates), 1)
        self.assertEqual(self.sent[-1], templates.notice_subject("bonus_used"))

    def test_somebody_new_is_registered_for_the_bonus_days(self):
        self.use(None)
        self.write()
        self.assertEqual(self.registered, [("ann@example.com", 7, "GIFTWEEK")])

    def test_a_subscription_without_an_end_keeps_it(self):
        xui = self.use({"uuid": "u", "email": "ann@example.com", "expiryTime": 0})
        self.write()
        self.assertEqual(xui.updates[0]["expiry_ms"], 0)

    def test_a_client_switched_off_by_hand_gets_nothing(self):
        xui = self.use({"uuid": "u", "email": "ann@example.com", "enable": False,
                        "expiryTime": self.now + 10 * DAY})
        self.write()
        self.assertEqual(xui.updates, [])


class TheCode(Case):
    def test_not_together_with_a_discount(self):
        tariffs.save_tariff({**self.tariff, "price": 100})
        with self.assertRaises(ValueError):
            tariffs.save_code({"word": "BOTH", "tariff_id": self.tariff["id"],
                               "discount": 10, "bonus_days": 7})

    def test_start_never_takes_a_bonus_word(self):
        self.assertEqual(tariffs.live_words(free_only=True), [])


if __name__ == "__main__":
    unittest.main()
