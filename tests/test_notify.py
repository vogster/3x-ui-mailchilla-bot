"""
What reaches Gotify, and the watchdog's one-message-per-outage promise.

The network is never touched: the push itself is replaced, so these tests
cannot send anything to a real Gotify whatever the settings say.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import inbox
import notify


class FakeXui:
    def __init__(self):
        self.up = True

    def get_server_status(self):
        return {"ok": 1} if self.up else None


class Case(unittest.TestCase):
    def setUp(self):
        self.pushed = []
        self.real_send = notify.mailer.send_gotify_notification
        notify.mailer.send_gotify_notification = lambda title, message: self.pushed.append(title)
        self.saved = {k: getattr(config, k) for k in ("GOTIFY_URL", "GOTIFY_TOKEN", *notify.SETTING.values())}
        config.GOTIFY_URL, config.GOTIFY_TOKEN = "https://gotify.example", "tok"
        for key in notify.SETTING.values():
            setattr(config, key, True)
        self.state = (notify._last_watch, notify._xui_failures, notify._xui_reported, notify._mail_reported)
        notify._last_watch, notify._xui_failures, notify._xui_reported, notify._mail_reported = 0.0, 0, False, False

        import xui_client
        self.xui = FakeXui()
        self.real_shared = xui_client.get_shared_client
        xui_client.get_shared_client = lambda: self.xui
        self.health = {"configured": True, "failures": 0, "error": "", "last_ok_at": 0}
        self.real_health = inbox.mail_health
        inbox.mail_health = lambda: dict(self.health)
        self.clock = 1_000_000.0

    def tearDown(self):
        import xui_client
        notify.mailer.send_gotify_notification = self.real_send
        for k, v in self.saved.items():
            setattr(config, k, v)
        notify._last_watch, notify._xui_failures, notify._xui_reported, notify._mail_reported = self.state
        xui_client.get_shared_client = self.real_shared
        inbox.mail_health = self.real_health

    def tick(self):
        self.clock += notify.WATCH_EVERY_S
        notify.watch(self.clock)


class Switches(Case):
    def test_a_switched_off_event_is_not_sent(self):
        config.NOTIFY_PAYMENT = False
        notify.push("payment", "Paid", "x")
        notify.push("registration", "Registered", "x")
        self.assertEqual(self.pushed, ["Registered"])


class TheWatchdog(Case):
    def test_one_missed_answer_is_not_an_outage(self):
        self.xui.up = False
        self.tick()
        self.assertEqual(self.pushed, [])

    def test_an_outage_is_reported_once_and_its_end_once(self):
        self.xui.up = False
        for _ in range(5):
            self.tick()
        self.xui.up = True
        self.tick()
        self.tick()
        self.assertEqual(len(self.pushed), 2)

    def test_it_does_not_look_more_often_than_it_should(self):
        self.xui.up = False
        self.tick()
        notify.watch(self.clock + 1)
        self.assertEqual(notify._xui_failures, 1)

    def test_the_mailbox_is_reported_after_several_failures_and_once(self):
        self.health["failures"] = notify.MAIL_FAILURES
        self.tick()
        self.tick()
        self.health["failures"] = 0
        self.tick()
        self.assertEqual(len(self.pushed), 2)

    def test_without_gotify_it_asks_nothing(self):
        config.GOTIFY_URL = ""
        self.xui.up = False
        for _ in range(3):
            self.tick()
        self.assertEqual(notify._xui_failures, 0)


if __name__ == "__main__":
    unittest.main()
