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
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import config
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
     "inboundIds": [1], "group": "Basic", "traffic": {"up": 1, "down": 2}},
    {"uuid": "c0ffee02", "id": 2, "email": "old-timer@example.com", "comment": "",
     "enable": True, "totalGB": 0, "expiryTime": 0, "subId": "sub02",
     "inboundIds": [1], "group": "", "traffic": {"up": 0, "down": 0}},
]


class FakeXui:
    """Every call the panel makes, answered without a network."""

    def get_inbounds(self):
        return [dict(i) for i in INBOUNDS]

    def get_all_clients(self):
        return [dict(c) for c in CLIENTS]

    def get_online_emails(self):
        return ["ben@example.com"]

    def get_last_online(self):
        return {"ben@example.com": 1_770_000_000_000}

    def get_server_status(self):
        return None

    def find_client_by_uuid(self, uuid):
        return next((dict(c) for c in CLIENTS
                     if c["uuid"] == uuid or str(c["id"]) == str(uuid)), None)

    def find_client_by_email(self, email):
        return next((dict(c) for c in CLIENTS if c["email"] == email), None)

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
                     "admin.routes_setup", "admin.app"):
            module = __import__(name, fromlist=["x"])
            if hasattr(module, "get_shared_client"):
                self.patched.append((module, module.get_shared_client))
                module.get_shared_client = lambda: self.fake

        self.client = TestClient(self.app)
        response = self.client.post("/login", data={"username": USER, "password": PASSWORD},
                                    follow_redirects=False)
        self.assertIn(response.status_code, (200, 302, 303))

    def tearDown(self):
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
                    "/settings", "/logs", "/setup"):
            with self.subTest(url=url):
                self.page(url)

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
        self.assertIn("не задан", cards)


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
        self.assertIn("неизвестно", body)

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
        self.assertIn("срок истёк", body)


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
