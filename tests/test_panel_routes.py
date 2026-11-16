"""
Every page of the panel opens, and the forms that change things do.

These are deliberately shallow: a status code, a redirect, a word that has to be
on the page. What they are for is the failure this project keeps producing —
a template reading a context key the route never sent. Jinja resolves it to
Undefined without a murmur, the page still renders, and the missing part is
found by somebody clicking a week later. A request that renders the whole
template catches it the moment it happens.

3x-ui is stubbed out: these tests must never reach a panel, and a route that
only works when the VPN panel answers is a route that breaks on the first
network hiccup anyway.

The tariffs live in a temporary file, for the reason tests/test_tariffs.py
gives at length: tariffs.TARIFFS_PATH is built from the module's own __file__,
and a test that forgot would write over a real installation's tariffs.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import os
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

import config
import email_bot
import mailer
import mailfolders
import tariffs
import xui_client

USER, PASSWORD = "panel-tests", "panel-tests-password"

# What the stubbed 3x-ui answers with. Two inbounds, two clients, one of them on
# a tariff and one on none — the second is what every installation updating from
# 0.1.x is full of.
INBOUNDS = [
    {"id": 1, "remark": "VLESS-REALITY", "protocol": "vless", "port": 443,
     "enable": True, "clients": 2},
    {"id": 2, "remark": "Shadowsocks", "protocol": "shadowsocks", "port": 8388,
     "enable": True, "clients": 0},
]
CLIENTS = [
    {"uuid": "c0ffee01", "id": 1, "email": "ben@example.com", "comment": "Ben",
     "enable": True, "totalGB": 0, "expiryTime": 0, "subId": "sub01",
     "inboundIds": [1], "group": "Basic", "limitHwid": 2,
     "traffic": {"up": 1, "down": 2}},
    {"uuid": "c0ffee02", "id": 2, "email": "old-timer@example.com", "comment": "",
     "enable": True, "totalGB": 0, "expiryTime": 0, "subId": "sub02",
     "inboundIds": [1], "group": "", "limitHwid": 0,
     "traffic": {"up": 0, "down": 0}},
]
# What the stubbed panel lists for a client with a device limit.
DEVICES = [
    {"id": 7, "firstSeen": 1735000000000, "lastSeen": 1735100000000,
     "userAgent": "Happ/1.0", "deviceOs": "android", "osVersion": "15",
     "deviceModel": "Pixel 9", "fingerprint": "6ad17c93e821"},
]


class FakeXui:
    """Every call the panel makes, answered without a network."""

    # A panel from before device limits returns clients without the field.
    knows_devices = True

    def __init__(self):
        self.devices = [dict(d) for d in DEVICES]
        self.asked_devices = []
        self.forget_ok = True
        self.created = []

    def _shape(self, client):
        client = dict(client)
        if not self.knows_devices:
            client.pop("limitHwid", None)
        return client

    def get_inbounds(self):
        return [dict(i) for i in INBOUNDS]

    def get_all_clients(self):
        return [self._shape(c) for c in CLIENTS + self.created]

    def get_client_devices(self, email):
        self.asked_devices.append(email)
        return None if self.devices is None else [dict(d) for d in self.devices]

    def forget_client_devices(self, email, device_id=None):
        self.forgot = (email, device_id)
        return self.forget_ok

    def add_client(self, **kwargs):
        self.added = kwargs
        self.created.append({
            "uuid": "c0ffee99", "id": 99, "email": kwargs["email"], "comment": "",
            "enable": True, "totalGB": 0, "expiryTime": 0, "subId": "sub99",
            "inboundIds": list(kwargs["inbound_ids"]), "group": kwargs.get("group", ""),
            "limitHwid": kwargs.get("limit_hwid", 0), "traffic": {"up": 0, "down": 0}})
        return "c0ffee99", list(kwargs["inbound_ids"])

    def get_online_emails(self):
        return ["ben@example.com"]

    def get_last_online(self):
        return {"ben@example.com": 1_770_000_000_000}

    def get_server_status(self):
        return None

    def find_client_by_uuid(self, uuid):
        return next((self._shape(c) for c in CLIENTS + self.created
                     if c["uuid"] == uuid or str(c["id"]) == str(uuid)), None)

    def find_client_by_email(self, email, strict=False):
        return next((self._shape(c) for c in CLIENTS + self.created
                     if c["email"] == email), None)

    def get_client_links(self, email):
        return []

    def rename_group(self, old_name, new_name):
        self.renamed = (old_name, new_name)
        return True

    def update_client(self, client_uuid, **kwargs):
        self.updated = {"uuid": client_uuid, **kwargs}
        return True

    def login(self):
        return True


class PanelCase(unittest.TestCase):
    """A signed-in client against the panel, with 3x-ui and the tariffs faked."""

    @classmethod
    def setUpClass(cls):
        cls.saved_user = config.ADMIN_PANEL_USER
        cls.saved_password = config.ADMIN_PANEL_PASSWORD
        config.ADMIN_PANEL_USER = USER
        config.ADMIN_PANEL_PASSWORD = PASSWORD
        # Imported here rather than at module level: importing admin.app runs
        # settings.load() and tariffs.load(), and the credentials have to be in
        # place before anything reads them.
        import admin.app as appmod
        cls.app = appmod.app

    @classmethod
    def tearDownClass(cls):
        config.ADMIN_PANEL_USER = cls.saved_user
        config.ADMIN_PANEL_PASSWORD = cls.saved_password

    def setUp(self):
        # The panel language is a setting, and a developer's own settings.json
        # sets it: these tests read strings off the page, so they have to know
        # which language the page is in. English needs no catalogue, so it is
        # the one that cannot drift. (This was found by CI, where there is no
        # settings.json at all and the pages therefore came out English while
        # the assertions expected Russian.)
        self.saved_lang = config.PANEL_LANG
        config.PANEL_LANG = "en"
        self.dir = tempfile.mkdtemp(prefix="mailchilla-routes-")
        self.real_path = tariffs.TARIFFS_PATH
        tariffs.TARIFFS_PATH = os.path.join(self.dir, "tariffs.json")
        self.saved_state = tariffs.snapshot()
        tariffs._state = {"tariffs": [], "codes": []}
        self.tariff = tariffs.save_tariff(
            {"name": "Basic", "limit_gb": 100, "expire_days": 90, "inbound_ids": [1]})
        tariffs.save_code({"word": "AURORA", "tariff_id": self.tariff["id"],
                           "uses_left": None, "enabled": True})

        self.fake = FakeXui()
        self.real_shared = xui_client.get_shared_client
        xui_client.get_shared_client = lambda: self.fake
        # The routers took their own reference at import time.
        self.patched = []
        for name in ("admin.routes_tariffs", "admin.routes_clients",
                     "admin.routes_broadcast", "admin.routes_settings",
                     "admin.routes_setup", "admin.routes_mail", "admin.app"):
            module = __import__(name, fromlist=["x"])
            if hasattr(module, "get_shared_client"):
                self.patched.append((module, module.get_shared_client))
                module.get_shared_client = lambda: self.fake

        self.client = TestClient(self.app)
        response = self.client.post("/login", data={"username": USER, "password": PASSWORD},
                                    follow_redirects=False)
        self.assertIn(response.status_code, (200, 302, 303))

    def tearDown(self):
        config.PANEL_LANG = self.saved_lang
        xui_client.get_shared_client = self.real_shared
        for module, original in self.patched:
            module.get_shared_client = original
        tariffs.TARIFFS_PATH = self.real_path
        tariffs._state = self.saved_state

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{url} answered {response.status_code}")
        return response.text


class PagesOpen(PanelCase):
    def test_the_pages_of_the_panel(self):
        for url in ("/", "/clients", "/tariffs", "/tariffs/new", "/broadcast",
                    "/settings", "/settings/totp", "/logs", "/setup"):
            with self.subTest(url=url):
                self.page(url)

    def test_the_inactivity_reminder_fields_are_on_the_page(self):
        # It lives on the Letters tab, beside the templates rather than on the
        # Mail tab, so it is saved through the texts form's own field naming.
        body = self.page("/settings")
        self.assertIn('name="switch:INACTIVITY_REMINDER_ENABLED"', body)
        self.assertIn('name="setting:INACTIVITY_REMINDER_DAYS"', body)

    def test_a_switched_block_carries_its_texts_inside_it(self):
        # The switches on the Letters tab are drawn as blocks with the texts
        # they govern inside them; a field that lost its way out of its block
        # would still render, just in the wrong place, so the markup is what
        # this asserts rather than the text alone.
        body = self.page("/settings")
        block = body.index('name="switch:WELCOME_MANUAL_ENABLED"')
        field = body.index('name="text:welcome.button_manual"')
        self.assertLess(block, field)
        self.assertLess(field, body.index('name="switch:WELCOME_SUPPORT_ENABLED"'))

    def test_a_tariff_card_lists_who_is_on_it(self):
        body = self.page(f"/tariffs/{self.tariff['id']}")
        self.assertIn("Basic", body)
        # The client whose group is this tariff, and not the one with none.
        self.assertIn("ben@example.com", body)
        self.assertNotIn("old-timer@example.com", body)

    def test_a_code_card_lists_who_came_through_it(self):
        tariffs.spend("AURORA", "ben@example.com")
        body = self.page("/tariffs/codes/AURORA")
        self.assertIn("AURORA", body)
        self.assertIn("ben@example.com", body)

    def test_the_code_form_opens_with_a_word_ready(self):
        body = self.page("/tariffs/codes/new")
        self.assertIn("Basic", body)

    def test_the_client_list_shows_the_tariff_and_offers_it_as_a_filter(self):
        body = self.page("/clients")
        self.assertIn("ben@example.com", body)
        # The chip on the row and the option in the filter — the filter is fed
        # by its own context key, and a route that forgot it would still render
        # a perfectly good page with the filter silently missing.
        self.assertIn('data-tariff="Basic"', body)
        self.assertIn('<select id="f-tariff"', body)
        self.assertIn('data-name="Basic"', body)

    def test_a_client_card_and_its_edit_form(self):
        self.page("/clients/c0ffee01")
        self.page("/clients/c0ffee01/edit")

    def test_a_page_behind_the_login_redirects_when_signed_out(self):
        fresh = TestClient(self.app)
        response = fresh.get("/tariffs", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/login", response.headers["location"])


class RoutesThatChangeThings(PanelCase):
    """
    The paths where one route could swallow another. /tariffs/codes/new is three
    segments, exactly like /tariffs/<id>/edit, and FastAPI matches in the order
    routes are declared — so this is not a hypothetical.
    """

    def test_saving_a_tariff(self):
        response = self.client.post("/tariffs/save", data={
            "tariff_id": self.tariff["id"], "name": "Basic", "limit_gb": "250",
            "expire_days": "30", "inbounds_present": "1", "inbound_ids": ["1", "2"],
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tariffs.get(self.tariff["id"])["limit_gb"], 250)
        self.assertEqual(tariffs.get(self.tariff["id"])["inbound_ids"], [1, 2])

    def test_renaming_a_tariff_renames_the_group_in_the_panel(self):
        self.client.post("/tariffs/save", data={
            "tariff_id": self.tariff["id"], "name": "Family", "limit_gb": "100",
            "expire_days": "90", "inbounds_present": "1", "inbound_ids": ["1"],
        }, follow_redirects=False)
        self.assertEqual(self.fake.renamed, ("Basic", "Family"))

    def test_a_new_tariff_lands_on_the_code_form(self):
        # A tariff with no way into it is not finished, and saying so later is
        # worse than offering the form now.
        response = self.client.post("/tariffs/save", data={
            "name": "Trial", "limit_gb": "10", "expire_days": "7",
            "inbounds_present": "1", "inbound_ids": ["1"],
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/tariffs/codes/new", response.headers["location"])

    def test_a_duplicate_tariff_name_comes_back_with_the_error(self):
        tariffs.save_tariff({"name": "Family", "limit_gb": 1, "expire_days": 1,
                             "inbound_ids": [1]})
        response = self.client.post("/tariffs/save", data={
            "tariff_id": self.tariff["id"], "name": "Family", "limit_gb": "100",
            "expire_days": "90", "inbounds_present": "1", "inbound_ids": ["1"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Family", response.text)
        self.assertEqual(tariffs.get(self.tariff["id"])["name"], "Basic")

    def test_saving_a_code(self):
        response = self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "SNOWDROP", "tariff_id": self.tariff["id"],
            "uses_left": "1", "note": "for Ben", "enabled": "on",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("issued=SNOWDROP", response.headers["location"])
        self.assertEqual(tariffs.get_code("SNOWDROP")["uses_left"], 1)

    def test_a_code_word_that_is_taken_comes_back_with_the_error(self):
        response = self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "aurora", "tariff_id": self.tariff["id"],
            "uses_left": "", "note": "", "enabled": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(tariffs.all_codes()), 1)

    def test_switching_a_code_off_and_on(self):
        self.client.post("/tariffs/codes/AURORA/off", follow_redirects=False)
        self.assertIsNone(tariffs.match("AURORA"))
        self.client.post("/tariffs/codes/AURORA/on", follow_redirects=False)
        self.assertIsNotNone(tariffs.match("AURORA"))

    def test_removing_a_code(self):
        self.client.post("/tariffs/codes/AURORA/delete", follow_redirects=False)
        self.assertEqual(tariffs.all_codes(), [])

    def test_deleting_a_tariff_takes_its_codes(self):
        self.client.post(f"/tariffs/{self.tariff['id']}/delete", follow_redirects=False)
        self.assertEqual(tariffs.all_tariffs(), [])
        self.assertEqual(tariffs.all_codes(), [])

    def test_the_wizard_writes_the_first_tariff_rather_than_the_settings(self):
        response = self.client.post("/setup/save", data={
            "codeword": "SNOWFALL", "limit_gb": "250", "expire_days": "30",
            "inbounds_present": "1", "inbound_ids": ["2"],
        })
        self.assertEqual(response.status_code, 200)
        first = tariffs.all_tariffs()[0]
        self.assertEqual(first["limit_gb"], 250)
        self.assertEqual(first["inbound_ids"], [2])
        self.assertIsNotNone(tariffs.match("SNOWFALL"))


class GroupsLeftBehind(PanelCase):
    """
    A tariff deleted while people are on it. Their group label in 3x-ui outlives
    it — deleting a template is not a reason to touch the clients stamped from
    it — so both pages have to say so rather than show a tariff that is gone.
    """

    def test_the_tariffs_page_lists_a_group_whose_tariff_is_gone(self):
        # The fake panel holds ben on "Basic"; delete the tariff of that name.
        tariffs.delete_tariff(self.tariff["id"])
        body = self.page("/tariffs")
        self.assertIn("Basic", body)
        self.assertIn("orphan", body)

    def test_the_client_list_marks_such_a_label(self):
        tariffs.delete_tariff(self.tariff["id"])
        body = self.page("/clients")
        self.assertIn("tariff-chip orphan", body)

    def test_a_label_of_a_living_tariff_is_not_marked(self):
        body = self.page("/clients")
        self.assertIn("tariff-chip ", body)
        self.assertNotIn("tariff-chip orphan", body)

    def test_the_clients_are_not_touched_by_the_deletion(self):
        tariffs.delete_tariff(self.tariff["id"])
        # Nothing was asked of 3x-ui beyond reading: the client keeps its group,
        # its limits and its subscription.
        self.assertEqual(self.fake.get_all_clients()[0]["group"], "Basic")


class DashboardTariffs(PanelCase):
    """
    The dashboard counts tariffs from the client list it already has. A count
    that needed its own request would make the page slower on exactly the day
    3x-ui is slow, which is the day the dashboard matters.
    """

    def test_the_block_names_the_tariff_and_counts_its_clients(self):
        body = self.page("/")
        self.assertIn("Basic", body)
        # One client on Basic, one on nothing at all.
        self.assertIn("without a tariff", body.replace("без тарифа", "without a tariff"))

    def test_a_group_whose_tariff_is_gone_is_listed_and_marked(self):
        tariffs.delete_tariff(self.tariff["id"])
        body = self.page("/")
        self.assertIn("Basic", body)


class BroadcastByTariff(PanelCase):
    """Writing to everybody on one tariff is the reason tariffs are on this page."""

    def test_the_picker_offers_every_tariff_and_the_absence_of_one(self):
        body = self.page("/broadcast")
        self.assertIn('<select id="rcpt-tariff"', body)
        self.assertIn('data-name="Basic"', body)

    def test_each_recipient_carries_its_tariff(self):
        body = self.page("/broadcast")
        self.assertIn('data-tariff="Basic"', body)
        # The one with no tariff carries an empty attribute rather than none:
        # "without a tariff" has to be selectable.
        self.assertIn('data-tariff=""', body)


class DashboardWithoutTheInboundTable(PanelCase):
    """
    The inbound table left the dashboard when tariffs arrived: it is 3x-ui's own
    list, and which inbounds a client gets is now a property of their tariff.
    What could not leave with it is the fault it carried — a tariff naming an
    inbound the panel does not have stops registration at that id, silently.
    """

    def test_the_table_is_gone(self):
        body = self.page("/")
        self.assertNotIn("Shadowsocks", body.split("new-client-dialog")[0])

    def test_a_tariff_naming_a_missing_inbound_is_named(self):
        tariffs.save_tariff({**self.tariff, "inbound_ids": [1, 99]})
        body = self.page("/")
        self.assertIn("Basic", body)
        self.assertIn("99", body)

    def test_an_unreachable_panel_does_not_accuse_every_tariff(self):
        # With 3x-ui down the inbound list is empty, and judging tariffs by it
        # would paint the whole page red for a fault that is elsewhere.
        self.fake.get_inbounds = lambda: []
        body = self.page("/")
        self.assertNotIn("Registration breaks off", body)


class ClientCardTariff(PanelCase):
    """The tariff belongs with the status, not among the uuids at the bottom."""

    def test_it_is_a_tile_rather_than_a_row_in_the_technical_table(self):
        body = self.page("/clients/c0ffee01")
        cards = body.split('class="cards"', 1)[1].split("</div>\n</div>", 1)[0]
        self.assertIn("Basic", cards)

    def test_a_client_on_no_tariff_says_so_rather_than_leaving_a_blank(self):
        # Everybody registered before tariffs existed is this client.
        body = self.page("/clients/c0ffee02")
        cards = body.split('class="cards"', 1)[1].split("</div>\n</div>", 1)[0]
        self.assertNotIn("Basic", cards)
        self.assertIn("not set", cards)


class CameInThrough(PanelCase):
    """
    Which word let a client in. History, not a setting: it survives the code
    being switched off or renamed, and it does not follow the client when they
    are moved to another tariff.
    """

    def test_the_card_names_the_code(self):
        tariffs.spend("AURORA", "ben@example.com")
        body = self.page("/clients/c0ffee01")
        self.assertIn("AURORA", body)
        self.assertIn("/tariffs/codes/AURORA", body)

    def test_a_client_nobody_recorded_says_it_is_not_known(self):
        # Everybody registered before this release, and anybody added by hand.
        body = self.page("/clients/c0ffee02")
        self.assertIn("not known", body)

    def test_it_survives_the_code_being_switched_off(self):
        tariffs.spend("AURORA", "ben@example.com")
        tariffs.set_code_enabled("AURORA", False)
        self.assertIn("AURORA", self.page("/clients/c0ffee01"))

    def test_it_does_not_follow_a_move_to_another_tariff(self):
        # The client's group changes; the word they came in through does not.
        tariffs.spend("AURORA", "ben@example.com")
        other = tariffs.save_tariff({"name": "Family", "limit_gb": 500,
                                     "expire_days": 365, "inbound_ids": [1]})
        self.client.post("/clients/c0ffee01/edit", data={
            "limit_gb": "500", "expire_days": "365", "enable": "on",
            "keep_comment": "on", "tariff_id": other["id"], "inbound_ids": ["1"],
        }, follow_redirects=False)
        self.assertIn("AURORA", self.page("/clients/c0ffee01"))


class DashboardFigures(PanelCase):
    """
    The dashboard counts from the same rows every other page uses. It used to
    have helpers of its own for "how much has this client spent" and "what
    percentage of the limit", which is one more answer than the question has —
    and the sort of duplication that drifts quietly.
    """

    def test_the_totals_come_from_the_client_list(self):
        body = self.page("/")
        # Two clients in the fake panel, both enabled, 3 bytes between them.
        self.assertIn(">2<", body)

    def test_a_client_over_the_limit_is_shown_at_the_limit(self):
        # The bar cannot go past full, and the figure beside it must agree with
        # what the client list shows for the same person.
        CLIENTS[0]["totalGB"] = 1024 ** 3
        CLIENTS[0]["traffic"] = {"up": 2 * 1024 ** 3, "down": 0}
        try:
            dashboard = self.page("/")
            listing = self.page("/clients")
            self.assertIn("100", dashboard)
            self.assertIn("width: 100", listing)
        finally:
            CLIENTS[0]["totalGB"] = 0
            CLIENTS[0]["traffic"] = {"up": 1, "down": 2}


class CodeExpiryThroughTheForm(PanelCase):
    """The date as the form speaks it: a day, counted to its end."""

    def test_a_date_saved_from_the_form_lasts_all_that_day(self):
        from datetime import datetime
        response = self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "WEEKEND", "tariff_id": self.tariff["id"],
            "uses_left": "", "expires_on": "2030-03-17", "note": "", "enabled": "on",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        when = datetime.fromtimestamp(tariffs.get_code("WEEKEND")["expires_at"] / 1000)
        self.assertEqual(when.strftime("%Y-%m-%d"), "2030-03-17")
        # The end of that day, not its first second: "until the 17th" includes it.
        self.assertGreaterEqual(when.hour, 23)

    def test_an_empty_date_means_it_never_runs_out(self):
        self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "FOREVER", "tariff_id": self.tariff["id"],
            "uses_left": "", "expires_on": "", "note": "", "enabled": "on",
        }, follow_redirects=False)
        self.assertEqual(tariffs.get_code("FOREVER")["expires_at"], 0)

    def test_the_form_comes_back_with_the_date_in_it(self):
        self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "WEEKEND", "tariff_id": self.tariff["id"],
            "uses_left": "", "expires_on": "2030-03-17", "note": "", "enabled": "on",
        }, follow_redirects=False)
        self.assertIn('value="2030-03-17"', self.page("/tariffs/codes/WEEKEND/edit"))

    def test_an_expired_code_says_so_rather_than_looking_open(self):
        tariffs.save_code({"word": "GONE", "tariff_id": self.tariff["id"],
                           "uses_left": None, "enabled": True,
                           "expires_at": tariffs._now_ms() - 86400 * 1000})
        body = self.page("/tariffs/codes/GONE")
        self.assertIn("out of date", body)


class DatesTypedByHand(PanelCase):
    """
    The date field is a pair: a box a person reads and a hidden ISO value. With
    no JavaScript only the box is posted, so the server takes both shapes — or
    a date typed on a page whose script did not load would be silently dropped.
    """

    def test_a_date_typed_the_way_it_is_read(self):
        from datetime import datetime
        self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "BYHAND", "tariff_id": self.tariff["id"],
            "uses_left": "", "expires_on": "17.03.2030", "note": "", "enabled": "on",
        }, follow_redirects=False)
        when = datetime.fromtimestamp(tariffs.get_code("BYHAND")["expires_at"] / 1000)
        self.assertEqual(when.strftime("%Y-%m-%d"), "2030-03-17")

    def test_something_that_is_not_a_date_leaves_the_code_open(self):
        self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "NONSENSE", "tariff_id": self.tariff["id"],
            "uses_left": "", "expires_on": "завтра", "note": "", "enabled": "on",
        }, follow_redirects=False)
        self.assertEqual(tariffs.get_code("NONSENSE")["expires_at"], 0)

    def test_the_form_shows_the_date_in_both_halves(self):
        self.client.post("/tariffs/codes/save", data={
            "was": "", "word": "WEEKEND", "tariff_id": self.tariff["id"],
            "uses_left": "", "expires_on": "2030-03-17", "note": "", "enabled": "on",
        }, follow_redirects=False)
        body = self.page("/tariffs/codes/WEEKEND/edit")
        self.assertIn('value="2030-03-17"', body)   # the hidden half
        self.assertIn('value="17.03.2030"', body)   # the half that is read


class SignInIsNotGuessable(PanelCase):
    """
    The sign-in form used to answer as many guesses a second as anybody cared
    to send. Harmless while the panel listens on 127.0.0.1 alone, and the first
    thing found once it is put behind a reverse proxy.
    """

    def setUp(self):
        super().setUp()
        import admin.app as appmod
        self.appmod = appmod
        appmod._failed_logins.clear()
        self.visitor = TestClient(self.app)

    def tearDown(self):
        self.appmod._failed_logins.clear()
        super().tearDown()

    def attempt(self, password):
        return self.visitor.post("/login", data={"username": USER, "password": password},
                                 follow_redirects=False)

    def test_wrong_guesses_run_out(self):
        for _ in range(self.appmod.LOGIN_ATTEMPTS):
            self.assertEqual(self.attempt("wrong").status_code, 401)
        refused = self.attempt("wrong")
        self.assertEqual(refused.status_code, 429)
        self.assertIn("Too many failed attempts", refused.text)

    def test_the_right_password_does_not_get_through_a_lock(self):
        # Otherwise the lock would stop nothing: the guess that matters is the
        # one that happens to be right.
        for _ in range(self.appmod.LOGIN_ATTEMPTS):
            self.attempt("wrong")
        self.assertEqual(self.attempt(PASSWORD).status_code, 429)

    def test_the_lock_lifts_by_itself(self):
        for _ in range(self.appmod.LOGIN_ATTEMPTS):
            self.attempt("wrong")
        # A window later, without waiting for one.
        for address, times in self.appmod._failed_logins.items():
            self.appmod._failed_logins[address] = [t - self.appmod.LOGIN_WINDOW - 1 for t in times]
        self.assertEqual(self.attempt(PASSWORD).status_code, 303)

    def test_a_few_slips_cost_nothing(self):
        self.attempt("wrong")
        self.attempt("wrong")
        self.assertEqual(self.attempt(PASSWORD).status_code, 303)


class DeviceLimit(PanelCase):
    """
    How many devices may use one subscription: a figure on the tariff, a field
    on the client, and — because a device stays registered until somebody takes
    it off — a list on the client's card with a way to clear it.
    """

    def save_tariff(self, **extra):
        data = {"tariff_id": self.tariff["id"], "name": "Basic", "limit_gb": "100",
                "expire_days": "90", "inbounds_present": "1", "inbound_ids": ["1"]}
        data.update(extra)
        return self.client.post("/tariffs/save", data=data, follow_redirects=False)

    def test_the_tariff_form_has_the_field_and_saves_it(self):
        self.assertIn('name="limit_hwid"', self.page(f"/tariffs/{self.tariff['id']}/edit"))
        self.assertEqual(self.save_tariff(limit_hwid="3").status_code, 303)
        self.assertEqual(tariffs.get(self.tariff["id"])["limit_hwid"], 3)
        self.assertIn('name="limit_hwid" min="0" step="1" value="3"',
                      self.page(f"/tariffs/{self.tariff['id']}/edit"))

    def test_a_form_without_the_field_does_not_lift_the_limit(self):
        # A tab left open since before the update still posts the old form.
        self.save_tariff(limit_hwid="3")
        self.save_tariff()
        self.assertEqual(tariffs.get(self.tariff["id"])["limit_hwid"], 3)

    def test_the_tariffs_page_and_the_card_show_it(self):
        # The list names it only when there is one; the card always has the
        # tile, so it is the figure that has to be looked for there.
        self.assertNotIn("Devices:", self.page("/tariffs"))
        self.save_tariff(limit_hwid="3")
        self.assertIn('Devices: <span class="mono">3</span>', self.page("/tariffs"))
        card = self.page(f"/tariffs/{self.tariff['id']}")
        self.assertIn('<div class="label">Devices</div>\n        <div class="value">3</div>', card)

    def test_the_create_forms_offer_it_and_take_it_from_the_tariff(self):
        self.save_tariff(limit_hwid="3")
        for url in ("/clients/new", "/clients"):
            body = self.page(url)
            self.assertIn('name="limit_hwid"', body)
            self.assertIn('data-hwid="3"', body)

    def test_a_client_made_by_hand_gets_the_limit_from_the_form(self):
        response = self.client.post("/clients/new", data={
            "email": "new@example.com", "comment": "", "limit_gb": "10", "expire_days": "30",
            "limit_hwid": "2", "tariff_id": self.tariff["id"], "inbound_ids": ["1"],
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.fake.added["limit_hwid"], 2)

    def test_the_card_counts_and_lists_the_devices(self):
        body = self.page("/clients/c0ffee01")
        cards = body.split('class="cards"', 1)[1].split("</div>\n</div>", 1)[0]
        self.assertIn("1 / 2", cards)
        self.assertIn("Pixel 9", body)
        self.assertIn("6ad17c93e821", body)
        self.assertIn("/clients/c0ffee01/devices/7/forget", body)

    def test_a_client_without_a_limit_has_no_device_block(self):
        body = self.page("/clients/c0ffee02")
        self.assertNotIn("Registered devices", body)
        self.assertEqual(self.fake.asked_devices, [], "a request the page had no use for")

    def test_a_panel_that_cannot_list_them_shows_the_limit_alone(self):
        self.fake.devices = None
        body = self.page("/clients/c0ffee01")
        self.assertNotIn("Registered devices", body)
        # The limit without a count beside it: "0 / 2" would be a claim.
        self.assertIn('<div class="label">Devices</div>\n        <div class="value">2</div>', body)

    def test_a_device_reporting_nonsense_does_not_take_the_card_down(self):
        self.fake.devices = [
            {"id": 8, "lastSeen": "yesterday", "deviceModel": "<b>Phone</b>"},
            {"deviceModel": "No id, so nothing to forget it by"},
        ]
        body = self.page("/clients/c0ffee01")
        self.assertIn("&lt;b&gt;Phone&lt;/b&gt;", body)
        self.assertNotIn("<b>Phone</b>", body)
        self.assertNotIn("devices/None/forget", body)
        self.assertNotIn("No id, so nothing", body)

    def test_the_sample_letter_shows_the_devices_line(self):
        # The line is drawn only when there is a limit, so a sample without one
        # would never show the text somebody is editing.
        import admin.routes_settings as routes_settings
        subject, mail = routes_settings._sample_email("welcome")
        self.assertIn("up to 2", mail.text)

    def test_forgetting_every_device(self):
        response = self.client.post("/clients/c0ffee01/devices/forget", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.fake.forgot, ("ben@example.com", None))

    def test_forgetting_one(self):
        response = self.client.post("/clients/c0ffee01/devices/7/forget", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.fake.forgot, ("ben@example.com", "7"))

    def test_a_refusal_comes_back_as_an_error_on_the_card(self):
        self.fake.forget_ok = False
        response = self.client.post("/clients/c0ffee01/devices/forget", follow_redirects=False)
        self.assertIn("error=", response.headers["location"])

    def test_the_edit_form_carries_the_limit(self):
        self.assertIn('name="limit_hwid" min="0" step="1" value="2"',
                      self.page("/clients/c0ffee01/edit"))

    def edit(self, **extra):
        data = {"limit_gb": "0", "expire_days": "0", "enable": "on", "comment": "Ben",
                "inbound_ids": ["1"]}
        data.update(extra)
        return self.client.post("/clients/c0ffee01/edit", data=data, follow_redirects=False)

    def test_a_changed_limit_is_sent_to_the_panel(self):
        self.assertEqual(self.edit(limit_hwid="4").status_code, 303)
        self.assertEqual(self.fake.updated["limit_hwid"], 4)

    def test_an_untouched_limit_is_not(self):
        # The update replaces the whole row from what the panel returned, so
        # the value survives; naming it again would only hand an older panel a
        # field it does not know.
        self.edit(limit_hwid="2")
        self.assertIsNone(self.fake.updated["limit_hwid"])

    def test_moving_to_a_tariff_offers_its_limit(self):
        self.save_tariff(limit_hwid="3")
        self.assertIn('data-hwid="3"', self.page("/clients/c0ffee01/edit"))


class PanelsFromBeforeDeviceLimits(PanelCase):
    """
    A 3x-ui that has never heard of the field returns clients without it. The
    panel then says nothing about devices on a client, rather than offering a
    limit that would not be kept.
    """

    def setUp(self):
        super().setUp()
        self.fake.knows_devices = False

    def test_the_card_has_no_device_tile(self):
        body = self.page("/clients/c0ffee01")
        cards = body.split('class="cards"', 1)[1].split("</div>\n</div>", 1)[0]
        self.assertNotIn("Devices", cards)
        self.assertEqual(self.fake.asked_devices, [])

    def test_the_edit_form_has_no_field(self):
        self.assertNotIn('name="limit_hwid"', self.page("/clients/c0ffee01/edit"))


class DeletingAsksFirst(PanelCase):
    """
    Deleting a tariff and removing a code used to ask nothing. The question was
    written as onsubmit="return confirm({{ ...|tojson }})", and tojson puts the
    text in double quotes inside an attribute that is itself in double quotes:
    the browser read `return confirm(` as the whole handler, could not compile
    it, and submitted the form. The panel has a dialog of its own for this,
    which only wants a data-confirm attribute.
    """

    def test_the_tariff_delete_button_asks(self):
        body = self.page("/tariffs")
        self.assertNotIn("onsubmit=", body)
        form = body.split(f'action="/tariffs/{self.tariff["id"]}/delete"', 1)[1].split(">", 1)[0]
        self.assertIn("data-confirm=", form)

    def test_the_code_remove_button_asks(self):
        body = self.page("/tariffs/codes/AURORA")
        self.assertNotIn("onsubmit=", body)
        form = body.split('action="/tariffs/codes/AURORA/delete"', 1)[1].split(">", 1)[0]
        self.assertIn("data-confirm=", form)


class MailPages(PanelCase):
    """
    The mailbox pages, against an IMAP server in memory. What matters beyond
    the page rendering is the client beside a letter — the reason these pages
    are in the panel at all — and that nothing on them marks a letter read.
    """

    def setUp(self):
        super().setUp()
        import imaplib
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fake_imap import FakeMailbox
        from email.message import EmailMessage

        self.box = FakeMailbox()
        self.saved_mail = (imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD)
        imaplib.IMAP4_SSL = self.box.connect
        config.IMAP_USER, config.IMAP_PASSWORD = "bot@example.com", "secret"

        def letter(frm, subject, body, attachment=None):
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = frm, "bot@example.com", subject
            msg.set_content(body)
            if attachment:
                msg.add_attachment(attachment[1], maintype="application",
                                   subtype="pdf", filename=attachment[0])
            return bytes(msg)

        self.ben = self.box.add("INBOX", letter("Ben <ben@example.com>", "Help me",
                                                "It stopped working", ("log.pdf", b"%PDF")))
        self.stranger = self.box.add("INBOX", letter("who@example.com", "Hello?", "Who are you"))

    def tearDown(self):
        import imaplib
        imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD = self.saved_mail
        self.assertEqual(self.box.writable_selects, [])
        self.assertEqual(self.box.unpeeked_fetches, [])
        super().tearDown()

    def test_unread_in_the_bots_mailbox_is_a_letter_it_still_owes_an_answer(self):
        self.assertIn("the bot has not handled this letter yet",
                      self.page(f"/mail/bot/inbox/{self.stranger}"))
        self.assertIn("Unread — the bot has not handled it yet", self.page("/mail/bot"))

    def test_a_letter_nested_past_reading_gets_a_page_not_a_traceback(self):
        depth = 2000
        raw = "".join(f"Content-Type: multipart/mixed; boundary=b{i}\r\n\r\n--b{i}\r\n"
                      for i in range(depth))
        raw += "Content-Type: text/plain\r\n\r\nhello\r\n"
        raw += "".join(f"--b{i}--\r\n" for i in reversed(range(depth)))
        uid = self.box.add("INBOX", ("From: who@example.com\r\nSubject: deep\r\n" + raw).encode())
        self.assertIn("cannot be read here", self.page(f"/mail/bot/inbox/{uid}"))
        self.assertEqual(self.client.get(f"/mail/bot/inbox/{uid}/attachments/0").status_code, 422)

    def test_a_line_break_in_the_search_box_is_not_a_command(self):
        body = self.page("/mail/bot?q=Help%0D%0AZ1+SELECT+INBOX%0D%0AZ2+UID+FETCH+1:*+BODY[]")
        self.assertNotIn("Could not read the mailbox", body)
        # tearDown asserts the rest: nothing was selected for writing and no
        # body was fetched without PEEK.

    def test_a_letter_too_large_to_frame_says_why_it_is_text(self):
        import mailfolders
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "who@example.com", "bot@example.com", "Big"
        msg.set_content("the text of it")
        msg.add_alternative("<p>" + "word " * (mailfolders.HTML_LIMIT // 5 + 10) + "</p>",
                            subtype="html")
        uid = self.box.add("INBOX", bytes(msg))
        body = self.page(f"/mail/bot/inbox/{uid}")
        self.assertIn("too large to show here", body)
        self.assertIn("the text of it", body)
        self.assertNotIn('id="letter-frame"', body)

    def test_the_list_marks_who_is_a_client(self):
        body = self.page("/mail/bot")
        self.assertIn("Help me", body)
        self.assertIn("Hello?", body)
        # Ben is on Basic; the chip carries the tariff's name.
        self.assertIn('class="client-chip', body)
        self.assertIn(">Basic</span>", body)

    def test_every_folder_opens(self):
        for folder in ("inbox", "sent", "trash"):
            with self.subTest(folder=folder):
                self.page(f"/mail/bot?folder={folder}")

    def test_a_letter_from_a_client_stands_beside_the_client(self):
        body = self.page(f"/mail/bot/inbox/{self.ben}")
        self.assertIn("It stopped working", body)
        self.assertIn("/clients/c0ffee01", body)
        self.assertIn("Basic", body)
        self.assertIn("log.pdf", body)
        self.assertIn("Unread", body)

    def test_a_letter_from_a_stranger_says_so(self):
        body = self.page(f"/mail/bot/inbox/{self.stranger}")
        self.assertIn("Not a client", body)

    def test_an_attachment_is_only_ever_a_download(self):
        body = self.page(f"/mail/bot/inbox/{self.ben}")
        href = re.search(r'href="(/mail/bot/inbox/\d+/attachments/\d+)"', body).group(1)
        response = self.client.get(href)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF")
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_a_letter_that_is_not_there(self):
        self.assertEqual(self.client.get("/mail/bot/inbox/999").status_code, 404)
        self.assertEqual(self.client.get("/mail/bot/spam/1").status_code, 404)

    def test_a_mailbox_that_is_not_set_up_says_so_rather_than_failing(self):
        config.IMAP_USER = ""
        self.assertIn("The mailbox is not set up", self.page("/mail/bot"))

    def test_no_support_tab_until_it_is_set_up(self):
        body = self.page("/mail/bot")
        self.assertNotIn("/mail/support", body)

    def test_the_bot_letter_has_no_reply_form(self):
        body = self.page(f"/mail/bot/inbox/{self.ben}")
        self.assertNotIn("name=\"body\"", body)

    def test_a_reply_to_the_bot_account_is_refused(self):
        response = self.client.post(f"/mail/bot/inbox/{self.ben}/reply", data={"body": "hi"})
        self.assertEqual(response.status_code, 404)


class SupportMailbox(PanelCase):
    """The Support account: a second mailbox, this one with a reply form."""

    def setUp(self):
        super().setUp()
        import imaplib
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fake_imap import FakeMailbox
        from email.message import EmailMessage

        self.box = FakeMailbox()
        self.saved_mail = (imaplib.IMAP4_SSL,
                          config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD,
                          config.SUPPORT_SMTP_USER, config.SUPPORT_SMTP_PASSWORD)
        imaplib.IMAP4_SSL = self.box.connect
        config.SUPPORT_IMAP_USER = "support@example.com"
        config.SUPPORT_IMAP_PASSWORD = "secret"
        config.SUPPORT_SMTP_USER = "support@example.com"
        config.SUPPORT_SMTP_PASSWORD = "secret"

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "Vera <vera@example.com>", "support@example.com", "Не работает"
        msg["Message-ID"] = "<vera-1@example.com>"
        msg.set_content("Что делать?")
        self.vera = self.box.add("INBOX", bytes(msg))

        self.saved_send = mailer.send_email_via
        self.sent = []

        def fake_send(server_host, port, user, password, to_email, subject, message,
                     service_name=None, extra_headers=None, automatic=True):
            built = mailer.build_message(to_email, subject, message, smtp_user=user,
                                         service_name=service_name, extra_headers=extra_headers,
                                         automatic=automatic)
            self.sent.append({"to": to_email, "subject": subject, "extra_headers": extra_headers})
            return built
        mailer.send_email_via = fake_send

        self.saved_copy = mailfolders.save_sent_copy
        self.queued = []
        mailfolders.save_sent_copy = (
            lambda account_key, message_id, raw: self.queued.append(account_key))

    def tearDown(self):
        import imaplib
        mailer.send_email_via = self.saved_send
        mailfolders.save_sent_copy = self.saved_copy
        (imaplib.IMAP4_SSL, config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD,
         config.SUPPORT_SMTP_USER, config.SUPPORT_SMTP_PASSWORD) = self.saved_mail
        super().tearDown()

    def test_unread_here_is_not_the_bots_doing(self):
        # The bot never reads this mailbox, so "the bot has not handled it yet"
        # was a sentence about something that was never going to happen.
        letter = self.page(f"/mail/support/inbox/{self.vera}")
        self.assertIn("answering it from here marks it read and answered", letter)
        self.assertNotIn("the bot has not handled", letter)
        listing = self.page("/mail/support")
        self.assertIn('class="udot" title="Unread"', listing)
        self.assertNotIn("the bot has not handled", listing)

    def flags(self, uid):
        return next(l.flags for l in self.box.folders["INBOX"][1] if l.uid == uid)

    def test_looking_at_a_letter_leaves_it_as_it_was(self):
        self.page(f"/mail/support/inbox/{self.vera}")
        self.page("/mail/support")
        self.assertEqual(self.flags(self.vera), set())
        self.assertEqual(self.box.stored, [])
        self.assertEqual(self.box.writable_selects, [])

    def test_answering_a_letter_marks_it_read_and_answered(self):
        # Otherwise the list cannot tell a letter that was dealt with from one
        # nobody has got to, which is how one gets answered twice or not at all.
        self.client.post(f"/mail/support/inbox/{self.vera}/reply", data={"body": "On it."})
        self.assertEqual(self.flags(self.vera), {"\\Answered", "\\Seen"})
        listing = self.page("/mail/support")
        self.assertIn('title="Answered"', listing)
        self.assertNotIn('class="udot"', listing)
        letter = self.page(f"/mail/support/inbox/{self.vera}")
        self.assertIn("This letter has been answered.", letter)
        self.assertNotIn("Unread.", letter)

    def test_a_reply_that_did_not_go_marks_nothing(self):
        def refuse(*args, **kwargs):
            raise RuntimeError("SMTP refused")
        mailer.send_email_via = refuse
        self.client.post(f"/mail/support/inbox/{self.vera}/reply", data={"body": "On it."})
        self.assertEqual(self.flags(self.vera), set())
        self.assertEqual(self.box.stored, [])

    def test_a_mailbox_that_will_not_be_marked_does_not_cost_the_reply(self):
        real_mark = mailfolders.mark_answered
        mailfolders.mark_answered = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
        try:
            response = self.client.post(f"/mail/support/inbox/{self.vera}/reply",
                                        data={"body": "On it."}, follow_redirects=False)
        finally:
            mailfolders.mark_answered = real_mark
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.sent[-1]["to"], "vera@example.com")

    def test_the_answer_names_the_address_it_goes_to(self):
        # The name alone used to stand there, and a name is whatever its owner
        # typed.
        body = self.page(f"/mail/support/inbox/{self.vera}")
        self.assertIn("Vera &lt;vera@example.com&gt;", body)
        self.assertNotIn("The answer will go to", body)

    def test_an_answer_about_to_go_elsewhere_says_so(self):
        # From a real client, Reply-To the stranger's own — with the client's
        # address for a name. The card beside the letter is the client's.
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = "Ben <ben@example.com>"
        msg["Reply-To"] = '"Ben <ben@example.com>" <stranger@evil.example>'
        msg["To"], msg["Subject"] = "support@example.com", "My subscription"
        msg.set_content("What is my link?")
        uid = self.box.add("INBOX", bytes(msg))
        body = self.page(f"/mail/support/inbox/{uid}")
        self.assertIn("The answer will go to stranger@evil.example, not to ben@example.com", body)
        self.assertIn("&lt;stranger@evil.example&gt;", body)

    def test_a_reply_to_with_a_name_and_no_address_is_passed_over(self):
        # It used to be chosen over the From, for a form that sent nothing.
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"], msg["Reply-To"] = "Ben <ben@example.com>", "Ben <>"
        msg["To"], msg["Subject"] = "support@example.com", "Hello"
        msg.set_content("hi")
        uid = self.box.add("INBOX", bytes(msg))
        body = self.page(f"/mail/support/inbox/{uid}")
        self.assertIn("&lt;ben@example.com&gt;", body)
        self.assertNotIn("The answer will go to", body)
        self.client.post(f"/mail/support/inbox/{uid}/reply", data={"body": "Hello Ben"})
        self.assertEqual(self.sent[-1]["to"], "ben@example.com")

    def test_every_page_tells_the_browser_to_give_no_way_back(self):
        response = self.client.get("/mail/support")
        self.assertEqual(response.headers.get("cross-origin-opener-policy"), "same-origin")

    def test_a_link_in_a_letter_is_given_no_way_back_to_the_panel(self):
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "who@example.com", "support@example.com", "Look"
        msg.set_content("see the page")
        msg.add_alternative('<a href="https://example.org/" rel="opener">page</a>', subtype="html")
        uid = self.box.add("INBOX", bytes(msg))
        # Done on the elements once the frame has loaded; here, that the page
        # carries the code that does it.
        page = self.page(f"/mail/support/inbox/{uid}")
        self.assertIn('setAttribute("rel", "noopener noreferrer")', page)
        # Every link element, not only those linking by a plain href: an SVG
        # link's address is xlink:href.
        self.assertIn('querySelectorAll("a, area")', page)

    def test_the_tab_appears_once_it_is_set_up(self):
        body = self.page("/mail/bot")
        self.assertIn("/mail/support", body)

    def test_the_reply_form_is_offered(self):
        body = self.page(f"/mail/support/inbox/{self.vera}")
        self.assertIn('name="body"', body)
        self.assertIn("vera@example.com", body)
        # required stays for a screen reader, but the form must carry
        # novalidate too — with the browser's own popup left on, a click on
        # Send with an empty box never reaches our own submit handler at all,
        # and the button does nothing with no feedback either way.
        form_start = body.index('id="reply-form"')
        form_tag = body[max(0, form_start - 200):form_start + 50]
        self.assertIn("novalidate", form_tag)

    def test_sending_a_reply_threads_it_and_files_a_copy(self):
        response = self.client.post(f"/mail/support/inbox/{self.vera}/reply",
                                    data={"body": "Проверьте кабель."}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("replied=1", response.headers["location"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], "vera@example.com")
        self.assertEqual(self.sent[0]["subject"], "Re: Не работает")
        self.assertEqual(self.sent[0]["extra_headers"]["In-Reply-To"], "<vera-1@example.com>")
        # Filed the same way any other letter is, under the Support account.
        self.assertEqual(self.queued, ["support"])

    def test_an_empty_reply_sends_nothing(self):
        self.client.post(f"/mail/support/inbox/{self.vera}/reply", data={"body": "   "})
        self.assertEqual(self.sent, [])


class SampleLetterIsNotCorrespondence(PanelCase):
    """
    The "send a sample letter" button on Settings > Letters previews the
    wording; it must not turn up in the Mail page's Sent folder beside real
    registrations and replies.
    """

    def test_the_preview_is_sent_without_keeping_a_copy(self):
        calls = []
        saved = email_bot.send_email_reply
        email_bot.send_email_reply = lambda *a, **k: calls.append(k)
        try:
            response = self.client.post("/settings/texts/test", data={
                "group": "welcome", "email": "admin@example.com", "kind": "info",
            })
        finally:
            email_bot.send_email_reply = saved
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("keep_copy"), False)


class SupportLetterPush(PanelCase):
    """The switch for the push about a letter to Support: on the page, and through the form."""

    def test_the_switch_is_on_the_gotify_tab(self):
        body = self.page("/settings")
        self.assertIn('name="support_notify_enabled"', body)
        self.assertIn("Tell me when a letter arrives for Support", body)

    def save(self, **fields):
        """Posts the settings form with the save itself caught: the real one
        writes the installation's own settings.json."""
        import admin.routes_settings as routes_settings
        caught = {}
        real = routes_settings.settings.save
        routes_settings.settings.save = lambda values: caught.update(values)
        try:
            response = self.client.post("/settings", data=fields, follow_redirects=False)
        finally:
            routes_settings.settings.save = real
        self.assertEqual(response.status_code, 303)
        return caught

    def test_ticked_it_is_saved_on(self):
        self.assertIs(self.save(support_notify_enabled="on")["SUPPORT_NOTIFY_ENABLED"], True)

    def test_unticked_it_is_saved_off(self):
        # A browser sends nothing at all for an unticked box.
        self.assertIs(self.save()["SUPPORT_NOTIFY_ENABLED"], False)

    def test_the_title_and_the_text_are_on_the_page_and_through_the_form(self):
        body = self.page("/settings")
        self.assertIn('name="support_notify_title"', body)
        self.assertIn('name="support_notify_message"', body)
        self.assertIn("A letter to {service} support", body)
        saved = self.save(support_notify_enabled="on", support_notify_title="[{service}] mail",
                          support_notify_message="{from}: {subject}")
        self.assertEqual(saved["SUPPORT_NOTIFY_TITLE"], "[{service}] mail")
        self.assertEqual(saved["SUPPORT_NOTIFY_MESSAGE"], "{from}: {subject}")

    def test_the_form_never_touches_the_mark(self):
        # Which letters have been announced is the bot's own record.
        self.assertNotIn("SUPPORT_NOTIFY_MARK", self.save(support_notify_enabled="on"))

class MailServersName(PanelCase):
    """The name of a mail server of one's own, on the Mail tab, and the button that suggests it."""

    def test_the_field_is_on_the_page(self):
        body = self.page("/settings")
        self.assertIn('name="mail_auth_server"', body)
        self.assertIn("Proof of the sender", body)

    def test_it_goes_through_the_form(self):
        import admin.routes_settings as routes_settings
        caught = {}
        real = routes_settings.settings.save
        routes_settings.settings.save = lambda values: caught.update(values)
        try:
            response = self.client.post("/settings", data={"mail_auth_server": "mail.example.net"},
                                        follow_redirects=False)
        finally:
            routes_settings.settings.save = real
        self.assertEqual(response.status_code, 303)
        self.assertEqual(caught["MAIL_AUTH_SERVER"], "mail.example.net")

    def scan(self, answer):
        saved = (email_bot.scan_auth_servers, config.IMAP_USER, config.IMAP_PASSWORD)
        config.IMAP_USER, config.IMAP_PASSWORD = "bot@example.net", "x"

        def stub():
            if isinstance(answer, Exception):
                raise answer
            return answer

        email_bot.scan_auth_servers = stub
        try:
            return self.client.post("/settings/mail/authserver/scan")
        finally:
            email_bot.scan_auth_servers, config.IMAP_USER, config.IMAP_PASSWORD = saved

    def test_the_button_is_told_which_names_head_the_letters(self):
        response = self.scan({"letters": 30, "without": 2,
                              "names": [("mail.example.net", 27), ("mx.google.com", 1)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "letters": 30, "without": 2,
            "names": [{"name": "mail.example.net", "letters": 27},
                      {"name": "mx.google.com", "letters": 1}]})

    def test_a_mailbox_out_of_reach_is_an_answer_too(self):
        response = self.scan(RuntimeError("the mailbox went away"))
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])
        self.assertNotIn("went away", response.text)

    def test_without_a_mailbox_there_is_nothing_to_look_in(self):
        saved = (config.IMAP_USER, config.IMAP_PASSWORD)
        config.IMAP_USER, config.IMAP_PASSWORD = "", ""
        try:
            response = self.client.post("/settings/mail/authserver/scan")
        finally:
            config.IMAP_USER, config.IMAP_PASSWORD = saved
        self.assertEqual(response.status_code, 400)

    def test_not_without_signing_in(self):
        response = TestClient(self.app).post("/settings/mail/authserver/scan",
                                             follow_redirects=False)
        self.assertEqual(response.status_code, 401)

class XrayPush(PanelCase):
    """The push about Xray stopping: its switch and its wording, on the Gotify tab."""

    def save(self, **fields):
        import admin.routes_settings as routes_settings
        caught = {}
        real = routes_settings.settings.save
        routes_settings.settings.save = lambda values: caught.update(values)
        try:
            response = self.client.post("/settings", data=fields, follow_redirects=False)
        finally:
            routes_settings.settings.save = real
        self.assertEqual(response.status_code, 303)
        return caught

    def test_the_switch_and_the_wording_are_on_the_page(self):
        body = self.page("/settings")
        self.assertIn('name="xray_notify_enabled"', body)
        self.assertIn('name="xray_notify_title"', body)
        self.assertIn('name="xray_notify_message"', body)
        self.assertIn("Tell me when Xray stops, and when it is back", body)

    def test_they_go_through_the_form(self):
        saved = self.save(xray_notify_enabled="on", xray_notify_title="[{service}] Xray",
                          xray_notify_message="{status}")
        self.assertIs(saved["XRAY_NOTIFY_ENABLED"], True)
        self.assertEqual(saved["XRAY_NOTIFY_TITLE"], "[{service}] Xray")
        self.assertEqual(saved["XRAY_NOTIFY_MESSAGE"], "{status}")

    def test_unticked_it_is_saved_off(self):
        self.assertIs(self.save()["XRAY_NOTIFY_ENABLED"], False)
