"""
The device limit: how many devices may use one subscription.

3x-ui counts devices by the hardware id an app sends when it fetches the
subscription, and holds the limit on the client as `limitHwid`. Here it is a
figure on the tariff, read once at registration like the traffic and the term.

Most of what can go wrong is quiet. A panel too old to know the field takes the
client and drops the limit without a word, so a tariff goes on promising two
devices that nothing enforces — which is why the registration reads the client
back and says so.
"""
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import email_bot
import email_texts
import tariffs
import templates
from xui_client import XuiClient

BASE = "http://panel.example:2053"


class StorageCase(unittest.TestCase):
    """A tariffs.json of its own, for the reason tests/test_tariffs.py gives."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-devices-")
        self.real_path = tariffs.TARIFFS_PATH
        tariffs.TARIFFS_PATH = os.path.join(self.dir, "tariffs.json")
        self.saved_state = tariffs.snapshot()
        tariffs._state = {"tariffs": [], "codes": []}

    def tearDown(self):
        tariffs.TARIFFS_PATH = self.real_path
        tariffs._state = self.saved_state

    def make(self, devices=2):
        tariff = tariffs.save_tariff({"name": "Family", "limit_gb": 100, "expire_days": 30,
                                      "inbound_ids": [1], "limit_hwid": devices})
        tariffs.save_code({"word": "FAMILY", "tariff_id": tariff["id"],
                           "uses_left": None, "enabled": True})
        return tariff


class TheTariffHoldsIt(StorageCase):
    def test_it_is_saved_and_read_back(self):
        tariff = self.make(devices=3)
        tariffs.load()
        self.assertEqual(tariffs.get(tariff["id"])["limit_hwid"], 3)

    def test_a_tariff_from_before_the_limit_existed_has_none(self):
        # Every tariffs.json written by 0.2.0 so far carries no such key.
        tariff = tariffs.save_tariff({"name": "Old", "limit_gb": 0, "expire_days": 0,
                                      "inbound_ids": [1]})
        self.assertEqual(tariff["limit_hwid"], 0)

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(ValueError):
            tariffs.save_tariff({"name": "Odd", "limit_gb": 0, "expire_days": 0,
                                 "inbound_ids": [1], "limit_hwid": -1})

    def test_an_empty_field_is_no_limit(self):
        tariff = tariffs.save_tariff({"name": "Blank", "limit_gb": 0, "expire_days": 0,
                                      "inbound_ids": [1], "limit_hwid": ""})
        self.assertEqual(tariff["limit_hwid"], 0)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.url = BASE
        self.history = []
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"success": True, "obj": None}
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return FakeResponse(self.payload)


def panel(payload=None):
    xui = XuiClient()
    xui.base_url = BASE
    xui.api_token = "token"
    xui.is_logged_in = True
    xui.session = FakeSession(payload)
    return xui


class WhatThePanelIsSent(unittest.TestCase):
    def test_a_new_client_carries_the_limit(self):
        xui = panel()
        xui.add_client(email="ben@example.com", limit_gb=10, expire_days=30,
                       inbound_ids=[1], limit_hwid=2)
        self.assertEqual(xui.session.calls[0][2]["client"]["limitHwid"], 2)

    def test_no_limit_sends_no_field(self):
        # A panel from before HWID limits is then asked exactly what it always
        # was, rather than handed a field it has never heard of.
        xui = panel()
        xui.add_client(email="ben@example.com", limit_gb=10, expire_days=30, inbound_ids=[1])
        self.assertNotIn("limitHwid", xui.session.calls[0][2]["client"])

    def test_an_edit_changes_it_and_keeps_the_rest(self):
        xui = panel()
        client = {"uuid": "u1", "id": 1, "email": "ben@example.com", "totalGB": 5,
                  "limitHwid": 2, "comment": "Ben"}
        xui.update_client("u1", limit_hwid=4, client_obj=client)
        method, url, body = xui.session.calls[0]
        self.assertEqual(body["limitHwid"], 4)
        self.assertEqual(body["comment"], "Ben")

    def test_an_edit_that_says_nothing_about_it_sends_what_was_there(self):
        xui = panel()
        client = {"uuid": "u1", "id": 1, "email": "ben@example.com", "limitHwid": 2}
        xui.update_client("u1", new_comment="Ben", client_obj=client)
        self.assertEqual(xui.session.calls[0][2]["limitHwid"], 2)


class TheDeviceList(unittest.TestCase):
    DEVICE = {"id": 7, "firstSeen": 1735000000000, "lastSeen": 1735100000000,
              "userAgent": "Happ/1.0", "deviceOs": "android", "osVersion": "15",
              "deviceModel": "Pixel 9", "fingerprint": "6ad17c93e821"}

    def test_it_is_asked_for_by_the_clients_identifier(self):
        xui = panel({"success": True, "obj": [self.DEVICE]})
        devices = xui.get_client_devices("ben@example.com")
        self.assertEqual(xui.session.calls[0][:2],
                         ("POST", BASE + "/panel/api/clients/hwids/ben%40example.com"))
        self.assertEqual(devices[0]["id"], 7)
        self.assertEqual(devices[0]["deviceModel"], "Pixel 9")

    def test_nobody_registered_is_an_empty_list(self):
        self.assertEqual(panel({"success": True, "obj": None}).get_client_devices("b@e.c"), [])

    def test_a_panel_that_cannot_say_is_not_an_empty_list(self):
        # An older build has no such route. "No devices" would be a claim.
        self.assertIsNone(panel({"success": False, "msg": "404"}).get_client_devices("b@e.c"))

    def test_forgetting_them_all(self):
        xui = panel({"success": True})
        self.assertTrue(xui.forget_client_devices("ben@example.com"))
        self.assertEqual(xui.session.calls[0][:2],
                         ("DELETE", BASE + "/panel/api/clients/hwids/ben%40example.com"))

    def test_forgetting_one(self):
        xui = panel({"success": True})
        self.assertTrue(xui.forget_client_devices("ben@example.com", device_id=7))
        self.assertEqual(xui.session.calls[0][:2],
                         ("DELETE", BASE + "/panel/api/clients/hwids/ben%40example.com/7"))

    def test_a_refusal_is_reported(self):
        self.assertFalse(panel({"success": False, "msg": "no"}).forget_client_devices("b@e.c"))


class Registration(StorageCase):
    """The limit travels from the tariff to the client, and the letter says so."""

    def setUp(self):
        super().setUp()
        self.real = {name: getattr(email_bot, name) for name in
                     ("get_shared_client", "send_welcome_email", "send_gotify_notification")}
        self.added = []
        self.letters = []
        self.kept = 2
        outer = self

        class FakeXui:
            def find_client_by_email(self, addr, strict=False):
                if not outer.added:
                    return None
                client = {"subId": "sub01", "email": addr}
                if outer.kept is not None:
                    client["limitHwid"] = outer.kept
                return client

            def add_client(self, **kwargs):
                outer.added.append(kwargs)
                return ("uuid", [1])

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_welcome_email = lambda *a, **kw: self.letters.append(kw)
        email_bot.send_gotify_notification = lambda **kw: None

    def tearDown(self):
        for name, value in self.real.items():
            setattr(email_bot, name, value)
        super().tearDown()

    def register(self, devices=2):
        tariff = self.make(devices=devices)
        matched = tariffs.match("FAMILY")
        email_bot.handle_registration("ben@example.com", "Ben", matched[0], matched[1])
        return tariff

    def test_the_client_is_created_with_the_tariffs_limit(self):
        self.register(devices=2)
        self.assertEqual(self.added[0]["limit_hwid"], 2)

    def test_the_letter_is_told(self):
        self.register(devices=2)
        self.assertEqual(self.letters[0]["limit_hwid"], 2)

    def test_the_letter_states_what_the_panel_kept_not_what_the_tariff_says(self):
        # A panel that knows the field and kept something else: a creation
        # that failed on one of its inbounds leaves the limit unset.
        self.kept = 0
        with self.assertLogs("email_bot", level="WARNING") as logs:
            self.register(devices=2)
        self.assertEqual(self.letters[0]["limit_hwid"], 0)
        # And it is not put down to the panel's age, which is a different fault
        # with a different remedy.
        self.assertFalse(any("does not know device limits" in line for line in logs.output))
        self.assertTrue(any("every inbound" in line for line in logs.output))

    def test_a_panel_that_dropped_the_limit_is_noticed(self):
        # An older 3x-ui takes the client and ignores the field. Nothing fails;
        # the tariff just promises a limit nobody enforces.
        self.kept = None
        with self.assertLogs("email_bot", level="WARNING") as logs:
            self.register(devices=2)
        self.assertTrue(any("does not know device limits" in line for line in logs.output))
        # And the letter does not promise what the panel will not keep.
        self.assertEqual(self.letters[0]["limit_hwid"], 0)

    def test_a_tariff_without_a_limit_raises_no_alarm(self):
        # By hand rather than with assertNoLogs, which Python 3.9 does not have.
        self.kept = None
        seen = []
        handler = logging.Handler(level=logging.WARNING)
        handler.emit = seen.append
        logging.getLogger("email_bot").addHandler(handler)
        try:
            self.register(devices=0)
        finally:
            logging.getLogger("email_bot").removeHandler(handler)
        self.assertEqual([r.getMessage() for r in seen], [])


class TheLetter(unittest.TestCase):
    def setUp(self):
        self.saved = config.MAIL_LANG
        config.MAIL_LANG = "ru"
        email_texts.load()

    def tearDown(self):
        config.MAIL_LANG = self.saved
        email_texts.load()

    def test_a_limit_is_stated_beside_the_traffic(self):
        mail = templates.get_welcome_email("https://example.com/sub/abc", 30, 100,
                                           tariff="Family", limit_hwid=2)
        for part in (mail.html, mail.text):
            self.assertIn("Устройств", part)
            self.assertIn("до 2", part)
        self.assertIn("Устройств: до 2\n", mail.text)

    def test_no_limit_means_no_line(self):
        # "Devices: unlimited" is a line about a restriction that is not there.
        mail = templates.get_welcome_email("https://example.com/sub/abc", 30, 100)
        self.assertNotIn("Устройств", mail.html)
        self.assertNotIn("Устройств", mail.text)

    def test_the_link_keeps_its_blank_line_above_either_way(self):
        # trim_blocks eats the newline after a tag, so the {% endif %} that
        # closes the devices line took the blank line under it along — in every
        # text letter, device limit or not.
        for devices in (0, 2):
            text = templates.get_welcome_email("https://example.com/sub/abc", 30, 100,
                                               limit_hwid=devices).text
            link = text.index("https://example.com/sub/abc")
            intro = text.rfind("\n", 0, text.rfind("\n", 0, link))
            self.assertEqual(text[intro - 1:intro + 1], "\n\n",
                             f"no blank line above the link with a limit of {devices}")


class TheStatusLetter(unittest.TestCase):
    """
    /status is what somebody sends when something is wrong, and "the new phone
    will not connect" is exactly that. So the letter says how many devices are
    registered against the limit, which ones, and — when every slot is taken —
    what to do about it.
    """

    GB = 1024 ** 3

    def setUp(self):
        self.saved = config.MAIL_LANG
        config.MAIL_LANG = "ru"
        email_texts.load()

    def tearDown(self):
        config.MAIL_LANG = self.saved
        email_texts.load()

    def letter(self, **devices):
        return templates.get_status_email("ben@example.com", True, 8 * self.GB, 34 * self.GB,
                                          100 * self.GB, 1800000000000, tariff="Family",
                                          **devices)

    def test_the_count_stands_beside_the_limit(self):
        mail = self.letter(device_limit=2, devices=["Pixel 9"])
        for part in (mail.html, mail.text):
            self.assertIn("Устройства", part)
            self.assertIn("1 из 2", part)
        self.assertIn("Устройства: 1 из 2\n", mail.text)

    def test_the_devices_are_named(self):
        mail = self.letter(device_limit=3, devices=["Pixel 9", "iPhone 15"])
        for part in (mail.html, mail.text):
            self.assertIn("Pixel 9, iPhone 15", part)

    def test_a_device_that_did_not_say_what_it_is_still_counts(self):
        mail = self.letter(device_limit=3, devices=["Pixel 9", ""])
        self.assertIn("2 из 3", mail.text)
        self.assertIn("Pixel 9", mail.text)
        self.assertNotIn("Pixel 9, ", mail.text)

    def test_every_slot_taken_says_what_to_do(self):
        full = self.letter(device_limit=2, devices=["Pixel 9", "iPhone 15"])
        room = self.letter(device_limit=2, devices=["Pixel 9"])
        note = email_texts.get("status.devices_full")
        self.assertTrue(note)
        for part in (full.html, full.text):
            self.assertIn(note.replace("**", "")[:40], part.replace("<b style=\"color: #ededef;\">", "")
                          .replace("</b>", ""))
        self.assertNotIn(note.replace("**", "")[:40], room.text)

    def test_a_panel_that_cannot_list_them_states_the_limit_alone(self):
        # "0 из 2" would be a claim; "до 2" is what is actually known.
        mail = self.letter(device_limit=2, devices=None)
        self.assertIn("Устройства: до 2\n", mail.text)
        self.assertNotIn("из 2", mail.text)

    def test_nobody_registered_yet_is_a_count_of_nought(self):
        mail = self.letter(device_limit=2, devices=[])
        self.assertIn("Устройства: 0 из 2\n", mail.text)

    def test_no_limit_means_no_line(self):
        mail = self.letter()
        self.assertNotIn("Устройств", mail.html)
        self.assertNotIn("Устройств", mail.text)

    def test_a_letter_without_devices_reads_as_it_always_did(self):
        # The lines are added between ones that were already there, in a
        # template where a tag's own line is not a line of the letter.
        text = self.letter().text
        self.assertIn("Использовано: 42.0 ГБ из 100.0 ГБ\nИзрасходовано 42.0% лимита\nЧтобы ", text)

    def test_a_device_name_cannot_bring_markup_into_the_letter(self):
        mail = self.letter(device_limit=2, devices=["<img src=x onerror=alert(1)>"])
        self.assertNotIn("<img", mail.html)
        self.assertIn("&lt;img", mail.html)


class StatusAsksThePanel(unittest.TestCase):
    """The handler: who is asked for what, and what happens when they cannot say."""

    def setUp(self):
        self.saved = config.MAIL_LANG
        config.MAIL_LANG = "ru"
        email_texts.load()
        self.real = {name: getattr(email_bot, name)
                     for name in ("get_shared_client", "send_email_reply")}
        self.sent = []
        self.asked = []
        self.limit = 2
        self.devices = [{"id": 7, "deviceModel": "Pixel 9", "deviceOs": "android",
                         "osVersion": "15"},
                        {"id": 8, "deviceOs": "windows", "osVersion": "11"}]
        outer = self

        class FakeXui:
            def find_client_by_email(self, addr, strict=False):
                client = {"email": addr, "enable": True, "group": "Family", "subId": "s"}
                if outer.limit is not None:
                    client["limitHwid"] = outer.limit
                return client

            def get_client_traffic(self, email):
                return {"up": 0, "down": 0, "total": 0, "expiryTime": 0, "enable": True}

            def get_client_devices(self, email):
                outer.asked.append(email)
                return outer.devices

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_email_reply = lambda addr, subject, message: self.sent.append(message)

    def tearDown(self):
        for name, value in self.real.items():
            setattr(email_bot, name, value)
        config.MAIL_LANG = self.saved
        email_texts.load()

    def test_the_letter_counts_and_names_the_devices(self):
        email_bot.handle_status("ben@example.com")
        text = self.sent[0].text
        self.assertIn("Устройства: 2 из 2\n", text)
        # A phone says what it is; a desktop only says its system.
        self.assertIn("Pixel 9, windows 11", text)

    def test_a_client_without_a_limit_costs_no_extra_request(self):
        self.limit = 0
        email_bot.handle_status("ben@example.com")
        self.assertEqual(self.asked, [])
        self.assertNotIn("Устройств", self.sent[0].text)

    def test_nor_does_a_panel_that_knows_no_limits(self):
        self.limit = None
        email_bot.handle_status("ben@example.com")
        self.assertEqual(self.asked, [])
        self.assertNotIn("Устройств", self.sent[0].text)

    def test_a_list_the_panel_would_not_give_does_not_cost_the_letter(self):
        self.devices = None
        email_bot.handle_status("ben@example.com")
        self.assertIn("Устройства: до 2\n", self.sent[0].text)


if __name__ == "__main__":
    unittest.main()
