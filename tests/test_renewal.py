"""
Auto-renewal: a tariff may say that 3x-ui renews the client by itself.

Three things can go wrong here quietly, and each has its tests.

The panel keeps a renewal as three numbers that are three ways of saying one
thing, and refuses some of their combinations outright — so here it is one
mode and one value, and nothing but `panel_fields` knows the panel's shape.

A panel from before the fields takes the client and drops them without a word.
The letter must then not say that the subscription renews: somebody would plan
by it, and learn otherwise on the day they are cut off.

And an edit must not wipe a renewal. `update/:email` replaces the row, so a
field that was not sent back would be gone — which is the worry this task
began with, and was checked against a live panel before any of this was
written: the row read before an edit does carry the fields.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import logging
import os
import sys
import unittest

import config
import email_bot
import email_texts
import renewal
import tariffs
import templates
from admin.rows import client_row
from test_devices import StorageCase, panel
from test_panel_routes import CLIENTS, PanelCase

DAY_MS = 86400000
SOON = 1_900_000_000_000

EVERY_30 = {"mode": "days", "value": 30, "max": 0}
ON_THE_15TH = {"mode": "monthday", "value": 15, "max": 0}
ON_MONDAYS = {"mode": "weekday", "value": 1, "max": 0}


def row(**fields):
    """A client the way clients/list hands one over, renewal fields included."""
    return {"uuid": "u1", "id": 1, "email": "ben@example.com", "enable": True,
            "expiryTime": SOON, "reset": 0, "resetDay": 0, "resetWeekday": 0, "resetMax": 0,
            "traffic": {"up": 1, "down": 2, "resetCount": 0}, **fields}


class OneSettingNotFour(unittest.TestCase):
    def test_off_is_off_whatever_came_with_it(self):
        # A value left in the form beside "off" is a leftover, not an instruction.
        self.assertEqual(renewal.clean({"mode": "", "value": 30, "max": 3}, 0), renewal.none())
        self.assertEqual(renewal.clean(None, 30), renewal.none())

    def test_each_mode_takes_its_own_range(self):
        for mode, good, bad in (("days", (1, 30, 3650), (0, -1, 3651)),
                                ("monthday", (1, 31), (0, 32)),
                                ("weekday", (1, 7), (0, 8))):
            for value in good:
                with self.subTest(mode=mode, value=value):
                    self.assertEqual(renewal.clean({"mode": mode, "value": value}, 30),
                                     {"mode": mode, "value": value, "max": 0})
            for value in bad:
                with self.subTest(mode=mode, value=value):
                    with self.assertRaises(ValueError):
                        renewal.clean({"mode": mode, "value": value}, 30)

    def test_what_is_not_a_number_is_refused_rather_than_guessed_at(self):
        for raw in ({"mode": "days", "value": "soon"}, {"mode": "days", "value": "1.5"},
                    {"mode": "days", "value": 30, "max": "many"},
                    {"mode": "days", "value": 30, "max": -1},
                    {"mode": "days", "value": 30, "max": 1001},
                    {"mode": "fortnightly", "value": 14}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    renewal.clean(raw, 30)

    def test_it_needs_a_term(self):
        # 3x-ui renews at the moment of expiry. A client with no expiry and a
        # renewal is taken without a word and then never renewed.
        for term in (0, None, ""):
            with self.subTest(term=term):
                with self.assertRaises(ValueError):
                    renewal.clean(EVERY_30, term)

    def test_the_form_gives_the_value_of_the_mode_it_names(self):
        # One field per mode, so a number typed under one is never read as
        # another's after the mode was changed.
        form = {"renew_mode": "monthday", "renew_days": "30", "renew_monthday": "15",
                "renew_weekday": "1", "renew_max": "3"}
        self.assertEqual(renewal.clean(renewal.from_form(form), 30),
                         {"mode": "monthday", "value": 15, "max": 3})
        self.assertEqual(renewal.clean(renewal.from_form({**form, "renew_mode": ""}), 0),
                         renewal.none())

    def test_the_panel_is_handed_exactly_one_way_of_saying_when(self):
        self.assertEqual(renewal.panel_fields({**EVERY_30, "max": 3}),
                         {"reset": 30, "resetDay": 0, "resetWeekday": 0, "resetMax": 3})
        self.assertEqual(renewal.panel_fields(ON_THE_15TH),
                         {"reset": 0, "resetDay": 15, "resetWeekday": 0, "resetMax": 0})
        self.assertEqual(renewal.panel_fields(ON_MONDAYS),
                         {"reset": 0, "resetDay": 0, "resetWeekday": 1, "resetMax": 0})

    def test_no_renewal_is_no_keys_unless_switching_one_off(self):
        self.assertEqual(renewal.panel_fields(None), {})
        self.assertEqual(renewal.panel_fields(renewal.none()), {})
        self.assertEqual(renewal.panel_fields(renewal.none(), always=True),
                         {"reset": 0, "resetDay": 0, "resetWeekday": 0, "resetMax": 0})


class WhatTheClientHas(unittest.TestCase):
    def test_a_panel_that_does_not_say_is_not_a_client_without_one(self):
        for client in ({"email": "ben@example.com", "expiryTime": SOON}, {}, None):
            with self.subTest(client=client):
                self.assertIsNone(renewal.of_client(client))
        self.assertEqual(renewal.of_client(row())["mode"], "")
        self.assertFalse(renewal.of_client(row())["active"])

    def test_a_panel_with_only_the_oldest_of_the_keys_does_not_know_renewal(self):
        # `reset` is far older in 3x-ui than the calendar modes and the cap.
        # Counted as knowing renewal, such a panel would be offered a day of
        # the month, drop the key, and leave the client with no renewal at all.
        for missing in ("resetDay", "resetWeekday", "resetMax"):
            with self.subTest(missing=missing):
                client = row(reset=30)
                del client[missing]
                self.assertIsNone(renewal.of_client(client))
        self.assertIsNone(renewal.of_client({"email": "ben@example.com", "reset": 30}))

    def test_each_way_of_saying_when_is_read_back(self):
        for fields, mode, value in (({"reset": 30}, "days", 30),
                                    ({"resetDay": 15}, "monthday", 15),
                                    ({"resetWeekday": 7}, "weekday", 7)):
            with self.subTest(fields=fields):
                kept = renewal.of_client(row(**fields))
                self.assertEqual((kept["mode"], kept["value"], kept["active"]), (mode, value, True))

    def test_the_panels_own_precedence_is_kept(self):
        # A day of the month wins over a period when a client carries both —
        # which one made in 3x-ui itself may.
        self.assertEqual(renewal.of_client(row(reset=30, resetDay=15))["mode"], "monthday")

    def test_the_cap_is_counted_down(self):
        kept = renewal.of_client(row(reset=30, resetMax=3,
                                     traffic={"resetCount": 1}))
        self.assertEqual((kept["max"], kept["used"], kept["left"], kept["active"]), (3, 1, 2, True))
        self.assertIsNone(renewal.of_client(row(reset=30))["left"])

    def test_a_spent_cap_renews_nothing_more(self):
        kept = renewal.of_client(row(reset=30, resetMax=3, traffic={"resetCount": 3}))
        self.assertEqual((kept["left"], kept["active"]), (0, False))

    def test_nor_does_a_client_with_no_expiry(self):
        self.assertFalse(renewal.of_client(row(reset=30, expiryTime=0))["active"])

    def test_junk_reads_as_nothing_rather_than_breaking_the_page(self):
        kept = renewal.of_client(row(reset="soon", resetDay=None, resetWeekday=-3,
                                     resetMax="x", traffic="nonsense"))
        self.assertEqual(kept["mode"], "")

    def test_two_are_the_same_by_what_they_ask_for(self):
        self.assertTrue(renewal.same(EVERY_30, renewal.of_client(row(reset=30))))
        self.assertTrue(renewal.same(renewal.none(), None))
        self.assertTrue(renewal.same(renewal.none(), renewal.of_client(row())))
        self.assertFalse(renewal.same(EVERY_30, {**EVERY_30, "max": 3}))
        self.assertFalse(renewal.same(EVERY_30, ON_THE_15TH))

    def test_in_a_few_words(self):
        saved = config.PANEL_LANG
        config.PANEL_LANG = "en"
        try:
            self.assertEqual(renewal.describe(EVERY_30), "every 30 d")
            self.assertEqual(renewal.describe(ON_THE_15TH), "monthly, on day 15")
            self.assertEqual(renewal.describe(ON_MONDAYS), "weekly, on Mondays")
            self.assertEqual(renewal.describe({**EVERY_30, "max": 3}),
                             "every 30 d, renewals: up to 3")
            self.assertEqual(renewal.describe({**EVERY_30, "max": 3}, cap=False), "every 30 d")
            self.assertEqual(renewal.describe(renewal.none()), "")
            self.assertEqual(renewal.describe(None), "")
        finally:
            config.PANEL_LANG = saved

    def test_every_word_of_it_is_in_the_russian_catalogue(self):
        # The strings in renewal.py are built in Python, where the pass over
        # the templates does not look.
        from lang.ru import TEXTS
        wanted = list(renewal._WEEKDAYS) + [
            "every {days} d", "monthly, on day {day}", "weekly, {weekday}",
            "renewals: up to {count}", "renewed {used} of {max}",
            "does nothing while the subscription has no expiry"]
        for mode, value in (("fortnightly", 1), ("days", 0), ("monthday", 0), ("weekday", 0)):
            try:
                renewal.clean({"mode": mode, "value": value}, 30)
            except ValueError:
                pass
        self.assertEqual([s for s in wanted if s not in TEXTS], [])
        saved = config.PANEL_LANG
        config.PANEL_LANG = "ru"
        try:
            for raw, term in (({"mode": "fortnightly"}, 30), ({"mode": "days", "value": 0}, 30),
                              ({"mode": "monthday", "value": 0}, 30),
                              ({"mode": "weekday", "value": 0}, 30),
                              ({"mode": "days", "value": "x"}, 30),
                              ({"mode": "days", "value": 1, "max": -1}, 30), (EVERY_30, 0)):
                with self.subTest(raw=raw):
                    with self.assertRaises(ValueError) as caught:
                        renewal.clean(raw, term)
                    self.assertRegex(str(caught.exception), "[а-я]")
        finally:
            config.PANEL_LANG = saved


class TheTariffHoldsIt(StorageCase):
    def tariff(self, **extra):
        return tariffs.save_tariff({"name": "Monthly", "limit_gb": 100, "expire_days": 30,
                                    "inbound_ids": [1], **extra})

    def test_it_is_saved_and_read_back(self):
        saved = self.tariff(renew={"mode": "days", "value": "30", "max": "3"})
        tariffs.load()
        self.assertEqual(tariffs.get(saved["id"])["renew"],
                         {"mode": "days", "value": 30, "max": 3})

    def test_a_tariff_from_before_the_field_has_none(self):
        self.assertEqual(self.tariff()["renew"], renewal.none())

    def test_a_renewal_without_a_term_is_refused(self):
        with self.assertRaises(ValueError):
            self.tariff(expire_days=0, renew=EVERY_30)

    def test_a_file_edited_into_nonsense_costs_one_tariff_not_the_file(self):
        # The loader skips a tariff it cannot read and says so; what it must
        # not do is fail to read the file, which would refuse every word.
        import json
        good = self.tariff()
        state = tariffs.snapshot()
        state["tariffs"].append({**good, "id": "broken01", "name": "Broken", "renew": "monthly"})
        state["tariffs"].append({**good, "id": "broken02", "name": "Endless", "expire_days": 0,
                                 "renew": EVERY_30})
        with open(tariffs.TARIFFS_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        with self.assertLogs("tariffs", level="ERROR"):
            tariffs.load()
        self.assertEqual([t["name"] for t in tariffs.all_tariffs()], ["Monthly"])

    def test_and_so_is_taking_the_term_away_from_one_that_has_it(self):
        saved = self.tariff(renew=EVERY_30)
        with self.assertRaises(ValueError):
            tariffs.save_tariff({**saved, "expire_days": 0})
        self.assertEqual(tariffs.get(saved["id"])["expire_days"], 30)


class WhatThePanelIsSent(unittest.TestCase):
    def test_a_new_client_carries_it(self):
        xui = panel()
        xui.add_client(email="ben@example.com", limit_gb=10, expire_days=30, inbound_ids=[1],
                       renew={"mode": "days", "value": 30, "max": 3})
        sent = xui.session.calls[0][2]["client"]
        self.assertEqual({key: sent[key] for key in ("reset", "resetDay", "resetWeekday", "resetMax")},
                         {"reset": 30, "resetDay": 0, "resetWeekday": 0, "resetMax": 3})

    def test_no_renewal_sends_none_of_the_keys(self):
        # A panel from before them is then asked exactly what it always was.
        for renew in (None, renewal.none()):
            xui = panel()
            xui.add_client(email="ben@example.com", limit_gb=10, expire_days=30,
                           inbound_ids=[1], renew=renew)
            sent = xui.session.calls[0][2]["client"]
            self.assertEqual([key for key in sent if key.startswith("reset")], [])

    def test_an_edit_that_says_nothing_about_it_does_not_wipe_it(self):
        # update/:email replaces the row. The renewal survives because the row
        # is sent back as the panel returned it — which this pins down, since
        # one field dropped on the way would switch somebody's renewal off.
        xui = panel()
        xui.update_client("u1", new_comment="Ben", total_gb=50,
                          client_obj=row(reset=30, resetMax=3))
        body = xui.session.calls[0][2]
        self.assertEqual((body["reset"], body["resetDay"], body["resetWeekday"], body["resetMax"]),
                         (30, 0, 0, 3))

    def test_changing_the_mode_clears_the_other_two(self):
        # The panel refuses a weekly renewal that finds a period beside it.
        xui = panel()
        xui.update_client("u1", renew=ON_MONDAYS, client_obj=row(reset=30, resetMax=3))
        body = xui.session.calls[0][2]
        self.assertEqual((body["reset"], body["resetDay"], body["resetWeekday"], body["resetMax"]),
                         (0, 0, 1, 0))

    def test_switching_it_off_is_four_noughts(self):
        xui = panel()
        xui.update_client("u1", renew=renewal.none(), client_obj=row(resetDay=15, resetMax=2))
        body = xui.session.calls[0][2]
        self.assertEqual((body["reset"], body["resetDay"], body["resetWeekday"], body["resetMax"]),
                         (0, 0, 0, 0))


class Registration(StorageCase):
    """The renewal travels from the tariff to the client, and the letter says what arrived."""

    def setUp(self):
        super().setUp()
        self.real = {name: getattr(email_bot, name) for name in
                     ("get_shared_client", "send_welcome_email", "send_gotify_notification")}
        self.added = []
        self.letters = []
        # What the panel hands back for the new client: the fields, or None
        # for a panel that has never heard of them.
        self.kept = {"reset": 30, "resetDay": 0, "resetWeekday": 0, "resetMax": 3}
        outer = self

        class FakeXui:
            def find_client_by_email(self, addr, strict=False):
                if not outer.added:
                    return None
                return {"subId": "sub01", "email": addr, "expiryTime": SOON,
                        "traffic": {"resetCount": 0}, **(outer.kept or {})}

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

    def register(self, renew=None):
        tariff = tariffs.save_tariff({"name": "Monthly", "limit_gb": 100, "expire_days": 30,
                                      "inbound_ids": [1], "renew": renew})
        tariffs.save_code({"word": "MONTHLY", "tariff_id": tariff["id"],
                           "uses_left": None, "enabled": True})
        matched = tariffs.match("MONTHLY")
        email_bot.handle_registration("ben@example.com", "Ben", matched[0], matched[1])

    def warnings(self, renew):
        seen = []
        handler = logging.Handler(level=logging.WARNING)
        handler.emit = seen.append
        logging.getLogger("email_bot").addHandler(handler)
        try:
            self.register(renew)
        finally:
            logging.getLogger("email_bot").removeHandler(handler)
        return [record.getMessage() for record in seen]

    def test_the_client_is_created_with_the_tariffs_renewal(self):
        self.register({"mode": "days", "value": 30, "max": 3})
        self.assertEqual(self.added[0]["renew"], {"mode": "days", "value": 30, "max": 3})

    def test_the_letter_is_told_what_the_panel_holds(self):
        self.assertEqual(self.warnings({"mode": "days", "value": 30, "max": 3}), [])
        told = self.letters[0]["renew"]
        self.assertEqual((told["mode"], told["value"], told["left"], told["active"]),
                         ("days", 30, 3, True))

    def test_a_panel_that_dropped_it_is_noticed_and_the_letter_promises_nothing(self):
        self.kept = None
        lines = self.warnings({"mode": "days", "value": 30, "max": 3})
        self.assertTrue(any("does not know auto-renewal" in line for line in lines), lines)
        self.assertIsNone(self.letters[0]["renew"])

    def test_a_panel_that_kept_something_else_is_a_different_fault(self):
        # It knows the fields and holds no renewal: too old for the calendar
        # modes, or a creation that failed part of the way.
        self.kept = {"reset": 0, "resetDay": 0, "resetWeekday": 0, "resetMax": 0}
        lines = self.warnings(ON_THE_15TH)
        self.assertFalse(any("does not know auto-renewal" in line for line in lines))
        self.assertTrue(any("3x-ui holds" in line for line in lines), lines)
        self.assertEqual(self.letters[0]["renew"]["mode"], "")

    def test_somebody_already_registered_is_told_what_they_have_not_what_the_word_gives(self):
        # The panel holds them already, with a renewal of their own; the word
        # they wrote opens a tariff with a different one.
        self.added.append({})
        self.assertEqual(self.warnings(ON_THE_15TH), [])
        self.assertEqual(len(self.added), 1, "an existing client was created again")
        told = self.letters[0]["renew"]
        self.assertEqual((told["mode"], told["value"]), ("days", 30))

    def test_a_tariff_without_one_raises_no_alarm(self):
        self.kept = None
        self.assertEqual(self.warnings(None), [])
        self.assertIsNone(self.added[0]["renew"]["mode"] or None)


class TheLetters(unittest.TestCase):
    URL = "https://example.com/sub/abc"

    def setUp(self):
        self.saved = config.MAIL_LANG
        config.MAIL_LANG = "ru"
        email_texts.load()

    def tearDown(self):
        config.MAIL_LANG = self.saved
        email_texts.load()

    def kept(self, **fields):
        return renewal.of_client(row(**fields))

    def welcome(self, renew):
        return templates.get_welcome_email(self.URL, 30, 100, tariff="Monthly", renew=renew)

    def test_the_welcome_letter_says_it_renews(self):
        mail = self.welcome(self.kept(reset=30))
        self.assertIn("Продление: автоматически, каждые 30 дней\n", mail.text)
        self.assertIn("Продление", mail.html)
        self.assertIn("автоматически, каждые 30 дней", mail.html)

    def test_by_the_calendar(self):
        self.assertIn("Продление: автоматически, 15-го числа каждого месяца\n",
                      self.welcome(self.kept(resetDay=15)).text)
        self.assertIn("Продление: автоматически, по понедельникам\n",
                      self.welcome(self.kept(resetWeekday=1)).text)
        self.assertIn("Продление: автоматически, по воскресеньям\n",
                      self.welcome(self.kept(resetWeekday=7)).text)

    def test_a_cap_is_stated_as_what_is_left(self):
        mail = self.welcome(self.kept(reset=30, resetMax=3, traffic={"resetCount": 1}))
        self.assertIn("Продление: автоматически, каждые 30 дней — осталось продлений: 2\n",
                      mail.text)

    def test_no_renewal_is_no_line(self):
        for renew in (None, self.kept(), renewal.none()):
            with self.subTest(renew=renew):
                mail = self.welcome(renew)
                self.assertNotIn("Продление", mail.text)
                self.assertNotIn("Продление", mail.html)

    def test_nor_is_one_that_will_not_fire_again(self):
        # A line about renewal is a promise that the subscription carries on.
        for renew in (self.kept(reset=30, resetMax=3, traffic={"resetCount": 3}),
                      self.kept(reset=30, expiryTime=0)):
            with self.subTest(renew=renew):
                self.assertNotIn("Продление", self.welcome(renew).text)

    def test_the_lines_round_it_stay_where_they_were(self):
        # trim_blocks eats the newline after a tag: the term above and the
        # limit below must each keep a line of its own, with or without.
        for renew in (None, self.kept(reset=30)):
            text = self.welcome(renew).text
            self.assertIn("Срок действия: 30 дней\n", text)
            self.assertIn("\nЛимит трафика: 100 ГБ\n", text)
            link = text.index(self.URL)
            intro = text.rfind("\n", 0, text.rfind("\n", 0, link))
            self.assertEqual(text[intro - 1:intro + 1], "\n\n", "no blank line above the link")

    def test_a_list_of_days_somebody_broke_is_not_counted_into(self):
        # Six words cannot say which of seven days: the shipped list is used.
        email_texts._overrides = {"ru": {"common.weekdays": "пн, вт, ср, чт, пт, сб"}}
        self.assertIn("автоматически, по воскресеньям", self.welcome(self.kept(resetWeekday=7)).text)
        email_texts._overrides = {"ru": {"common.weekdays": "пн, вт, ср, чт, пт, сб, вс"}}
        self.assertIn("автоматически, вс\n", self.welcome(self.kept(resetWeekday=7)).text)

    def status(self, renew):
        return templates.get_status_email("ben@example.com", True, 1, 2, 100 * 1024 ** 3,
                                          SOON, tariff="Monthly", renew=renew)

    def test_the_status_letter_says_it_beside_the_date(self):
        mail = self.status(self.kept(reset=30, resetMax=3, traffic={"resetCount": 1}))
        lines = mail.text.splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("Действует до"))
        self.assertEqual(lines[at + 1],
                         "Продление: автоматически, каждые 30 дней — осталось продлений: 2")
        self.assertTrue(lines[at + 2].startswith("Использовано"))
        self.assertIn("осталось продлений: 2", mail.html)

    def test_and_nothing_when_there_is_none_to_come(self):
        for renew in (None, self.kept(), self.kept(reset=30, resetMax=1, traffic={"resetCount": 1})):
            with self.subTest(renew=renew):
                mail = self.status(renew)
                self.assertNotIn("Продление", mail.text)
                lines = mail.text.splitlines()
                at = next(i for i, line in enumerate(lines) if line.startswith("Действует до"))
                self.assertTrue(lines[at + 1].startswith("Использовано"))

    def test_english_letters_have_the_words_too(self):
        config.MAIL_LANG = "en"
        email_texts.load()
        text = self.welcome(self.kept(resetWeekday=1, resetMax=2)).text
        self.assertIn("Renewal: automatic, on Mondays — renewals left: 2\n", text)


class StatusAsksNothingMore(unittest.TestCase):
    """The row the status handler already holds carries the renewal."""

    def test_the_letter_is_given_what_the_row_says(self):
        real = {name: getattr(email_bot, name) for name in ("get_shared_client", "send_email_reply")}
        told = {}
        real_letter = templates.get_status_email

        class FakeXui:
            def find_client_by_email(self, addr, strict=False):
                return row(reset=30, resetMax=3, traffic={"resetCount": 2}, group="Monthly")

            def get_client_traffic(self, email):
                return {"up": 1, "down": 2, "total": 0, "expiryTime": SOON, "enable": True}

        def letter(*args, **kwargs):
            told.update(kwargs)
            return real_letter(*args, **kwargs)

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_email_reply = lambda *a, **kw: None
        templates.get_status_email = letter
        try:
            email_bot.handle_status("ben@example.com")
        finally:
            templates.get_status_email = real_letter
            for name, value in real.items():
                setattr(email_bot, name, value)
        self.assertEqual((told["renew"]["mode"], told["renew"]["left"]), ("days", 1))


class TheCard(unittest.TestCase):
    def setUp(self):
        self.saved = config.PANEL_LANG
        config.PANEL_LANG = "en"

    def tearDown(self):
        config.PANEL_LANG = self.saved

    def test_a_panel_that_does_not_say_offers_nothing(self):
        cells = client_row({"uuid": "u1", "email": "ben@example.com", "enable": True})
        self.assertFalse(cells["renew_known"])
        self.assertEqual(cells["renew_text"], "")

    def test_a_cap_is_shown_as_a_count(self):
        cells = client_row(row(reset=30, resetMax=3, traffic={"resetCount": 1}))
        self.assertTrue(cells["renew_known"])
        self.assertEqual((cells["renew_text"], cells["renew_note"]),
                         ("every 30 d", "renewed 1 of 3"))

    def test_a_renewal_with_nothing_to_act_on_says_so(self):
        cells = client_row(row(reset=30, expiryTime=0))
        self.assertFalse(cells["renew"]["active"])
        self.assertIn("no expiry", cells["renew_note"])


class OnThePanel(PanelCase):
    def with_renewal(self, address="ben@example.com", **fields):
        """The stand-in panel's clients as a panel that knows renewal returns them."""
        saved = [dict(client, traffic=dict(client["traffic"])) for client in CLIENTS]
        for client in CLIENTS:
            client.update({"reset": 0, "resetDay": 0, "resetWeekday": 0, "resetMax": 0,
                           "expiryTime": SOON})
            client["traffic"]["resetCount"] = 0
            if client["email"] == address:
                client["traffic"]["resetCount"] = fields.pop("resetCount", 0)
                client.update(fields)

        def restore():
            CLIENTS[:] = saved

        self.addCleanup(restore)

    def save_tariff(self, **extra):
        data = {"tariff_id": self.tariff["id"], "name": "Basic", "limit_gb": "100",
                "expire_days": "90", "inbounds_present": "1", "inbound_ids": ["1"]}
        data.update(extra)
        return self.client.post("/tariffs/save", data=data, follow_redirects=False)

    # ---- the tariff

    def test_the_tariff_form_has_the_fields_and_saves_them(self):
        body = self.page(f"/tariffs/{self.tariff['id']}/edit")
        # renew_present among them: "off" is an empty value, which arrives as
        # a field that was never sent, so the mode cannot say the fields were
        # on the form — and a form without them must change nothing.
        for name in ("renew_present", "renew_mode", "renew_days", "renew_monthday",
                     "renew_weekday", "renew_max"):
            self.assertIn(f'name="{name}"', body)
        response = self.save_tariff(renew_present="1", renew_mode="monthday", renew_days="30",
                                    renew_monthday="15", renew_max="6")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tariffs.get(self.tariff["id"])["renew"],
                         {"mode": "monthday", "value": 15, "max": 6})
        # And the form opens on what was saved.
        body = self.page(f"/tariffs/{self.tariff['id']}/edit")
        self.assertIn('<option value="monthday" selected>', body)
        self.assertIn('name="renew_monthday" min="1" max="31" step="1"\n'
                      '           placeholder="1" value="15"', body)

    def test_the_list_and_the_card_say_it(self):
        self.save_tariff(renew_present="1", renew_mode="days", renew_days="30", renew_max="3")
        self.assertIn("every 30 d, renewals: up to 3", self.page("/tariffs"))
        self.assertIn("every 30 d, renewals: up to 3", self.page(f"/tariffs/{self.tariff['id']}"))

    def test_a_tariff_without_one_says_nothing_about_it(self):
        self.assertNotIn("Auto-renewal:", self.page("/tariffs"))

    def test_a_renewal_without_a_term_comes_back_with_the_reason(self):
        response = self.save_tariff(expire_days="0", renew_present="1", renew_mode="days", renew_days="30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("auto-renewal needs a term", response.text)
        # With what was typed still in the form.
        self.assertIn('<option value="days" selected>', response.text)
        self.assertEqual(tariffs.get(self.tariff["id"])["renew"], renewal.none())

    def test_a_form_from_before_the_field_switches_nothing_off(self):
        self.save_tariff(renew_present="1", renew_mode="days", renew_days="30")
        self.assertEqual(self.save_tariff(name="Basic").status_code, 303)
        self.assertEqual(tariffs.get(self.tariff["id"])["renew"], EVERY_30)

    # ---- a client made by hand

    def new_client(self, **extra):
        data = {"email": "new@example.com", "comment": "", "limit_gb": "10", "expire_days": "30",
                "tariff_id": self.tariff["id"], "inbound_ids": ["1"]}
        data.update(extra)
        return self.client.post("/clients/new", data=data, follow_redirects=False)

    def test_the_create_forms_offer_the_tariffs_renewal(self):
        self.save_tariff(renew_present="1", renew_mode="weekday", renew_weekday="5", renew_max="4")
        for url in ("/clients/new", "/clients"):
            with self.subTest(url=url):
                body = self.page(url)
                self.assertIn('name="renew_mode"', body)
                self.assertIn('data-renew-mode="weekday"', body)
                self.assertIn('data-renew-value="5"', body)
                self.assertIn('data-renew-max="4"', body)
                # The first tariff is the form's starting point.
                self.assertIn('<option value="weekday" selected>', body)
                self.assertIn('<option value="5" selected>on Fridays</option>', body)

    def test_a_client_made_by_hand_gets_what_the_form_says(self):
        response = self.new_client(renew_present="1", renew_mode="days", renew_days="45", renew_max="2")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.fake.added["renew"], {"mode": "days", "value": 45, "max": 2})

    def welcome_letters(self):
        """What the welcome letter is told, for each one sent from here on."""
        told = []
        real = email_bot.send_welcome_email
        email_bot.send_welcome_email = lambda *args, **kwargs: told.append(kwargs)
        self.addCleanup(setattr, email_bot, "send_welcome_email", real)
        return told

    def a_panel_that_keeps_renewal(self):
        """The stand-in panel as it stands drops the fields, as an old 3x-ui does."""
        real = self.fake.add_client

        def add(**kwargs):
            result = real(**kwargs)
            made = self.fake.created[-1]
            made.update({**renewal.panel_fields(kwargs.get("renew"), always=True),
                         "expiryTime": SOON})
            made["traffic"]["resetCount"] = 0
            return result

        self.fake.add_client = add

    def test_the_letter_about_a_client_made_by_hand_says_what_the_panel_kept(self):
        told = self.welcome_letters()
        self.a_panel_that_keeps_renewal()
        response = self.new_client(send_email="on", renew_present="1", renew_mode="days",
                                   renew_days="45", renew_max="2")
        self.assertNotIn("lost=", response.headers["location"])
        self.assertEqual((told[0]["renew"]["mode"], told[0]["renew"]["value"],
                          told[0]["renew"]["left"]), ("days", 45, 2))

    def test_a_renewal_the_panel_dropped_is_said_on_the_page_and_not_in_the_letter(self):
        # The create form cannot know beforehand whether this 3x-ui keeps a
        # renewal. The card draws none for such a panel, so without a word on
        # the page the setting would have vanished between two screens.
        told = self.welcome_letters()
        response = self.new_client(send_email="on", renew_present="1", renew_mode="days",
                                   renew_days="45")
        self.assertIn("lost=renewal", response.headers["location"])
        self.assertIsNone(told[0]["renew"])
        self.assertIn("created without the auto-renewal", self.page(response.headers["location"]))

    def test_nothing_is_said_to_have_been_lost_when_nothing_was_asked_for(self):
        response = self.new_client()
        self.assertNotIn("lost=", response.headers["location"])
        self.assertNotIn("created without the auto-renewal",
                         self.page(response.headers["location"]))

    def test_the_invitation_sent_again_says_what_the_client_has(self):
        told = self.welcome_letters()
        self.with_renewal(resetWeekday=1, resetMax=4, resetCount=1)
        response = self.client.post("/clients/c0ffee01/resend", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual((told[0]["renew"]["mode"], told[0]["renew"]["left"]), ("weekday", 3))

    def test_and_none_when_the_form_says_none(self):
        self.assertEqual(self.new_client().status_code, 303)
        self.assertEqual(self.fake.added["renew"], renewal.none())

    def test_a_renewal_for_a_client_with_no_term_is_refused_before_anything_is_created(self):
        response = self.new_client(expire_days="0", renew_present="1", renew_mode="days", renew_days="30")
        self.assertEqual(response.status_code, 400)
        self.assertIn("auto-renewal needs a term", response.text)
        self.assertFalse(hasattr(self.fake, "added"))

    # ---- the client's card and the edit form

    def test_the_card_shows_it(self):
        self.with_renewal(reset=30, resetMax=3, resetCount=1)
        body = self.page("/clients/c0ffee01")
        self.assertIn("Auto-renewal", body)
        self.assertIn("every 30 d", body)
        self.assertIn("renewed 1 of 3", body)

    def test_a_client_without_one_has_no_such_card(self):
        self.with_renewal()
        self.assertNotIn("Auto-renewal", self.page("/clients/c0ffee01"))

    def test_a_panel_that_does_not_say_offers_nothing(self):
        # The stand-in panel's rows carry no renewal fields as they stand.
        self.assertNotIn("Auto-renewal", self.page("/clients/c0ffee01"))
        self.assertNotIn('name="renew_mode"', self.page("/clients/c0ffee01/edit"))

    def test_the_edit_form_opens_on_what_the_client_has(self):
        self.with_renewal(resetDay=15, resetMax=2)
        body = self.page("/clients/c0ffee01/edit")
        self.assertIn('<option value="monthday" selected>', body)
        self.assertIn('placeholder="1" value="15"', body)
        self.assertIn('name="renew_max" min="0" step="1"\n           value="2"', body)

    def edit(self, **extra):
        data = {"limit_gb": "0", "expire_days": "30", "enable": "on", "comment": "Ben",
                "inbound_ids": ["1"]}
        data.update(extra)
        return self.client.post("/clients/c0ffee01/edit", data=data, follow_redirects=False)

    def test_an_untouched_renewal_is_not_sent_again(self):
        self.with_renewal(reset=30, resetMax=3)
        self.assertEqual(self.edit(renew_present="1", renew_mode="days", renew_days="30", renew_max="3").status_code, 303)
        self.assertIsNone(self.fake.updated["renew"])

    def test_a_changed_one_is(self):
        self.with_renewal(reset=30, resetMax=3)
        self.edit(renew_present="1", renew_mode="weekday", renew_weekday="1", renew_days="30", renew_max="0")
        self.assertEqual(self.fake.updated["renew"], ON_MONDAYS)

    def test_switching_it_off_is_sent_as_off(self):
        self.with_renewal(reset=30)
        self.edit(renew_present="1", renew_mode="", renew_days="30")
        self.assertEqual(self.fake.updated["renew"], renewal.none())

    def test_a_form_without_the_fields_says_nothing_about_it(self):
        # What a panel that knows no renewal is drawn: no fields, so no
        # instruction — and above all not "switch it off".
        self.with_renewal(reset=30)
        self.edit()
        self.assertIsNone(self.fake.updated["renew"])

    def test_a_renewal_cannot_be_set_while_taking_the_term_away(self):
        self.with_renewal()
        response = self.edit(expire_days="0", renew_present="1", renew_mode="days", renew_days="30")
        self.assertEqual(response.status_code, 303)
        self.assertIn("/edit?error=", response.headers["location"])
        self.assertFalse(hasattr(self.fake, "updated"))

    def test_but_one_left_alone_does_not_stand_in_the_way_of_the_rest(self):
        # The form opens with the renewal the client has. Saving a new name
        # for somebody whose term is being taken away must not be refused over
        # a field nobody touched.
        self.with_renewal(reset=30)
        response = self.edit(expire_days="0", renew_present="1", renew_mode="days",
                             renew_days="30", renew_max="0")
        self.assertNotIn("error=", response.headers["location"])
        self.assertIsNone(self.fake.updated["renew"])

    def test_one_set_in_the_panel_itself_past_what_the_form_offers_can_still_be_left_alone(self):
        # The form opens on what the client has. Checked for its range before
        # being recognised as untouched, such a renewal made the client
        # impossible to edit at all.
        self.with_renewal(reset=4000, resetMax=1500)
        response = self.edit(renew_present="1", renew_mode="days", renew_days="4000",
                             renew_max="1500")
        self.assertNotIn("error=", response.headers["location"])
        self.assertIsNone(self.fake.updated["renew"])
        # Changed, it is checked like any other.
        response = self.edit(renew_present="1", renew_mode="days", renew_days="5000",
                             renew_max="1500")
        self.assertIn("/edit?error=", response.headers["location"])

    def test_moving_to_a_tariff_offers_its_renewal(self):
        self.with_renewal()
        self.save_tariff(renew_present="1", renew_mode="days", renew_days="30", renew_max="3")
        body = self.page("/clients/c0ffee01/edit")
        self.assertIn('data-renew-mode="days"', body)
        self.assertIn('data-renew-value="30"', body)

    # ---- the term, which every save used to move

    def test_a_term_nobody_touched_is_not_sent_back(self):
        # "Days from now" sent back is an expiry moved to this hour of the
        # day. The panel renews at the expiry, and a renewal by the calendar
        # runs midnight to midnight: nudged to the afternoon before, it fires
        # a day early and spends one of a capped plan's renewals on one day.
        self.with_renewal(resetDay=1, resetMax=12)
        body = self.page("/clients/c0ffee01/edit")
        was = body.split('name="expire_days_was" value="', 1)[1].split('"', 1)[0]
        self.assertGreater(int(was), 0)
        self.assertIn(f'name="expire_days" min="0" step="1" value="{was}"', body)
        self.edit(expire_days=was, expire_days_was=was, comment="Benjamin")
        self.assertIsNone(self.fake.updated["expire_days"])

    def test_a_changed_one_is(self):
        self.with_renewal()
        self.edit(expire_days="45", expire_days_was="30")
        self.assertEqual(self.fake.updated["expire_days"], 45)
        # And nought typed over a number is still "never expires".
        self.edit(expire_days="0", expire_days_was="30")
        self.assertEqual(self.fake.updated["expire_days"], 0)

    def test_a_subscription_that_ran_out_is_not_made_endless_by_saving_a_comment(self):
        # The field opened on 0 for it, and 0 is this form's word for "never
        # expires": any save handed an expired client a subscription for good,
        # and with it switched a renewal off by taking away what it acts on.
        self.with_renewal(reset=30, expiryTime=1_600_000_000_000)
        body = self.page("/clients/c0ffee01/edit")
        self.assertIn('name="expire_days" min="0" step="1" value=""', body)
        self.assertIn('name="expire_days_was" value=""', body)
        self.edit(expire_days="", expire_days_was="", comment="Benjamin")
        self.assertIsNone(self.fake.updated["expire_days"])

    # ---- the sample letters

    def test_the_sample_letters_show_the_line(self):
        sent = []
        real = email_bot.send_email_reply
        email_bot.send_email_reply = lambda to, subject, message, **kw: sent.append(message.text)
        try:
            for group in ("welcome", "status"):
                response = self.client.post("/settings/texts/test", data={
                    "group": group, "email": "admin@example.com"})
                self.assertEqual(response.status_code, 200)
        finally:
            email_bot.send_email_reply = real
        self.assertEqual(len(sent), 2)
        for text in sent:
            self.assertRegex(text, r"(Renewal|Продление): ")


class TheStand(unittest.TestCase):
    """The stand's panel answers as one that knows renewal, or the stand shows none of this."""

    def setUp(self):
        docs = os.path.join(sandbox.ROOT, "docs")
        if docs not in sys.path:
            sys.path.insert(0, docs)
        import stand_panel
        self.panel = stand_panel.Panel()

    def test_every_demo_client_carries_the_fields(self):
        kept = [renewal.of_client(client) for client in self.panel.clients]
        self.assertTrue(all(one is not None for one in kept))
        self.assertTrue({"days", "monthday"} <= {one["mode"] for one in kept})

    def test_a_client_made_on_it_keeps_what_it_was_given(self):
        self.panel.add_client(email="new@example.com", inbound_ids=[1], expire_days=30,
                              renew={"mode": "days", "value": 30, "max": 3})
        kept = renewal.of_client(self.panel.find("new@example.com"))
        self.assertEqual((kept["mode"], kept["value"], kept["left"], kept["active"]),
                         ("days", 30, 3, True))

    def test_an_edit_leaves_it_or_changes_it(self):
        self.panel.add_client(email="new@example.com", inbound_ids=[1], expire_days=30,
                              renew=EVERY_30)
        made = self.panel.find("new@example.com")
        self.panel.update_client(made["uuid"], new_comment="New")
        self.assertEqual(renewal.of_client(made)["mode"], "days")
        self.panel.update_client(made["uuid"], renew=renewal.none())
        self.assertEqual(renewal.of_client(made)["mode"], "")


if __name__ == "__main__":
    unittest.main()
