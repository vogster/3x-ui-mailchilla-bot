"""
The second step of signing in, as the panel keeps it and asks for it.

tests/test_totp.py is the arithmetic. This is everything around it: that the
secret is written only through its own door and never read back, that the form
asks for a code and will not say which half of a sign-in was wrong, that wrong
codes run out, and that neither switching it on nor switching it off happens
without a code from the app.

settings.SETTINGS_PATH is moved to a temporary directory for every test here:
these save for real, and the real file is an installation's passwords.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import json
import logging
import os
import re
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fastapi.testclient import TestClient

import config
import settings
import totp
from test_panel_routes import PanelCase, USER, PASSWORD


class _SettingsInATempFile:
    """Moves settings.json somewhere harmless, and puts everything back after."""

    def keep_settings(self):
        self.settings_dir = tempfile.mkdtemp(prefix="mailchilla-two-step-")
        self.real_settings_path = settings.SETTINGS_PATH
        self.real_stored = dict(settings._stored)
        self.real_secret = config.TOTP_SECRET
        settings.SETTINGS_PATH = os.path.join(self.settings_dir, "settings.json")
        settings._stored.pop("TOTP_SECRET", None)
        config.TOTP_SECRET = ""
        totp._accepted.clear()

    def restore_settings(self):
        settings.SETTINGS_PATH = self.real_settings_path
        settings._stored = self.real_stored
        config.TOTP_SECRET = self.real_secret
        totp._accepted.clear()

    def written(self) -> dict:
        with open(settings.SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)


class TheSecretHasOneDoor(_SettingsInATempFile, unittest.TestCase):
    def setUp(self):
        self.keep_settings()

    def tearDown(self):
        self.restore_settings()

    def test_off_until_a_secret_is_set(self):
        self.assertFalse(settings.two_step_on())

    def test_its_own_door_switches_it_on_and_off(self):
        secret = totp.new_secret()
        settings.save_two_step(secret)
        self.assertTrue(settings.two_step_on())
        self.assertEqual(config.TOTP_SECRET, secret)
        self.assertEqual(self.written()["TOTP_SECRET"], secret)
        settings.save_two_step("")
        self.assertFalse(settings.two_step_on())
        self.assertEqual(self.written()["TOTP_SECRET"], "")

    def test_the_ordinary_save_will_not_take_it(self):
        # The settings form and the Letters tab hand save() whatever arrived in
        # a request. Neither may switch the second step off, or swap the secret
        # for one of the sender's own.
        settings.save_two_step(totp.new_secret())
        before = config.TOTP_SECRET
        for value in ("", totp.new_secret()):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settings.save({"TOTP_SECRET": value})
                self.assertEqual(config.TOTP_SECRET, before)
                self.assertEqual(self.written()["TOTP_SECRET"], before)

    def test_a_guarded_key_spoils_the_whole_save(self):
        # Not "everything but": a request that tried is not one to half obey.
        name = config.SERVICE_NAME
        with self.assertRaises(ValueError):
            settings.save({"SERVICE_NAME": "Somebody else's", "TOTP_SECRET": ""})
        self.assertEqual(config.SERVICE_NAME, name)

    def test_saving_something_else_keeps_it(self):
        secret = totp.new_secret()
        settings.save_two_step(secret)
        settings.save({"UPDATE_CHECK_ENABLED": False})
        self.assertEqual(config.TOTP_SECRET, secret)
        self.assertEqual(self.written()["TOTP_SECRET"], secret)

    def test_it_is_not_written_before_it_exists(self):
        # An installation that never switched it on carries no such key, so a
        # copy rolled back to an older version has nothing to warn about.
        settings.save({"UPDATE_CHECK_ENABLED": False})
        self.assertNotIn("TOTP_SECRET", self.written())

    def test_what_is_not_a_secret_is_refused(self):
        for junk in ("123456", "not base32!", "ABC"):
            with self.subTest(junk=junk):
                with self.assertRaises(ValueError):
                    settings.save_two_step(junk)
        self.assertFalse(settings.two_step_on())

    def test_it_is_never_read_back(self):
        settings.save_two_step(totp.new_secret())
        with self.assertRaises(ValueError):
            settings.secret("TOTP_SECRET")
        shown = settings.describe()["TOTP_SECRET"]
        self.assertEqual(shown["value"], "")
        self.assertTrue(shown["isset"])
        self.assertEqual(set(shown["preview"]), {"•"})

    def test_it_survives_a_restart(self):
        secret = totp.new_secret()
        settings.save_two_step(secret)
        config.TOTP_SECRET = ""
        settings.load()
        self.assertEqual(config.TOTP_SECRET, secret)

    def test_a_spoilt_secret_in_the_file_is_said_and_is_not_a_locked_door(self):
        # A code would be asked for and no code would ever be right. Whoever
        # can spoil the file can empty it, so nothing is given away by reading
        # it as empty — but it is said, loudly.
        with open(settings.SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({"TOTP_SECRET": "!!! not a secret !!!"}, f)
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        logging.getLogger("settings").addHandler(handler)
        try:
            settings.load()
        finally:
            logging.getLogger("settings").removeHandler(handler)
        self.assertFalse(settings.two_step_on())
        self.assertTrue(any(r.levelno >= logging.ERROR and "TOTP_SECRET" in r.getMessage()
                            for r in seen))


class TwoStepCase(_SettingsInATempFile, PanelCase):
    """A signed-in panel whose settings.json is a temporary one."""

    def setUp(self):
        self.keep_settings()
        super().setUp()
        import admin.app as appmod
        from admin import twostep
        self.appmod, self.twostep = appmod, twostep
        appmod._failed_logins.clear()
        twostep._reset()

    def tearDown(self):
        self.appmod._failed_logins.clear()
        self.twostep._reset()
        super().tearDown()
        self.restore_settings()

    def switch_on(self) -> str:
        secret = totp.new_secret()
        settings.save_two_step(secret)
        return secret

    def sign_in(self, password=PASSWORD, code=None):
        data = {"username": USER, "password": password}
        if code is not None:
            data["code"] = code
        return TestClient(self.app).post("/login", data=data, follow_redirects=False)

    def a_window_later(self):
        """Ages the wrong codes past their window, without waiting for one."""
        wrong = self.twostep._wrong
        for door in wrong:
            wrong[door] = [t - self.twostep.CODE_WINDOW - 1 for t in wrong[door]]

    @staticmethod
    def shown(body: str) -> str:
        """The secret a setup page shows for typing by hand, as it is stored."""
        found = re.search(r"(?:[A-Z2-7]{4} ){7}[A-Z2-7]{4}", body)
        return totp.normalise(found.group(0)) if found else ""

    def signed_in_elsewhere(self) -> TestClient:
        """A second session with the same password: another browser, or somebody else's."""
        other = TestClient(self.app)
        response = other.post("/login", data={"username": USER, "password": PASSWORD},
                              follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return other

    @staticmethod
    def next_code(secret: str) -> str:
        """A code that is right now and is not the one the clock shows — for a
        second code inside the same thirty seconds."""
        return totp.code(secret, at=time.time() + totp.STEP)


class SigningIn(TwoStepCase):
    def test_without_it_the_form_asks_for_no_code(self):
        body = TestClient(self.app).get("/login").text
        self.assertNotIn('name="code"', body)
        self.assertEqual(self.sign_in().status_code, 303)

    def test_with_it_the_form_asks(self):
        self.switch_on()
        body = TestClient(self.app).get("/login").text
        self.assertIn('name="code"', body)
        self.assertIn('autocomplete="one-time-code"', body)

    def test_the_password_alone_no_longer_opens_the_panel(self):
        self.switch_on()
        for code in (None, "", "000000x"):
            with self.subTest(code=code):
                refused = self.sign_in(code=code)
                self.assertEqual(refused.status_code, 401)
                self.assertIn('name="code"', refused.text)

    def test_the_password_and_the_code_do(self):
        secret = self.switch_on()
        visitor = TestClient(self.app)
        response = visitor.post("/login", follow_redirects=False, data={
            "username": USER, "password": PASSWORD, "code": totp.code(secret)})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(visitor.get("/settings", follow_redirects=False).status_code, 200)

    def test_a_code_typed_with_a_space_in_it(self):
        secret = self.switch_on()
        code = totp.code(secret)
        self.assertEqual(self.sign_in(code=f"{code[:3]} {code[3:]}").status_code, 303)

    def test_a_code_opens_the_panel_once(self):
        # Whoever watched it typed has it for the rest of its minute and a half.
        secret = self.switch_on()
        code = totp.code(secret)
        self.assertEqual(self.sign_in(code=code).status_code, 303)
        self.assertEqual(self.sign_in(code=code).status_code, 401)

    def test_the_refusal_does_not_say_which_half_was_wrong(self):
        secret = self.switch_on()
        wrong_code = self.sign_in(code="000000" if totp.code(secret) != "000000" else "111111")
        wrong_password = self.sign_in(password="wrong", code=self.next_code(secret))
        self.assertEqual(wrong_code.status_code, 401)
        self.assertEqual(wrong_password.status_code, 401)
        message = "The login, the password or the code is wrong"
        self.assertIn(message, wrong_code.text)
        self.assertIn(message, wrong_password.text)

    def test_a_wrong_password_does_not_spend_the_code(self):
        # A stranger guessing at the password, with any code at all in the
        # field, must not be able to use up the administrator's.
        secret = self.switch_on()
        code = totp.code(secret)
        self.assertEqual(self.sign_in(password="wrong", code=code).status_code, 401)
        self.assertEqual(self.sign_in(code=code).status_code, 303)

    def test_a_right_password_with_a_wrong_code_is_said_in_the_log(self):
        # The one line that means the password is no longer a secret.
        self.switch_on()
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        logging.getLogger("admin.app").addHandler(handler)
        try:
            self.sign_in(code="")
            self.sign_in(password="wrong", code="")
        finally:
            logging.getLogger("admin.app").removeHandler(handler)
        told = [r.getMessage() for r in seen if "the password was right" in r.getMessage()]
        self.assertEqual(len(told), 1)
        self.assertNotIn(PASSWORD, " ".join(r.getMessage() for r in seen))


class WrongCodesRunOut(TwoStepCase):
    def test_after_so_many_no_code_is_looked_at(self):
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS):
            self.assertEqual(self.sign_in(code="").status_code, 401)
        # The right one included, or the ceiling would stop nothing.
        refused = self.sign_in(code=totp.code(secret))
        self.assertEqual(refused.status_code, 429)
        self.assertIn("Too many wrong codes", refused.text)

    def test_and_the_code_that_was_not_looked_at_is_not_spent(self):
        secret = self.switch_on()
        code = totp.code(secret)
        for _ in range(self.twostep.CODE_ATTEMPTS):
            self.sign_in(code="")
        self.sign_in(code=code)
        self.a_window_later()
        self.assertEqual(self.sign_in(code=code).status_code, 303)

    def test_it_lifts_by_itself(self):
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS):
            self.sign_in(code="")
        self.a_window_later()
        self.assertEqual(self.sign_in(code=totp.code(secret)).status_code, 303)

    def test_a_few_slips_cost_nothing(self):
        secret = self.switch_on()
        self.sign_in(code="")
        self.sign_in(code="")
        self.assertEqual(self.sign_in(code=totp.code(secret)).status_code, 303)

    def test_a_right_code_does_not_wipe_the_wrong_ones_before_it(self):
        # The count is everybody's: the administrator signing in must not be
        # handing whoever is guessing alongside a fresh five.
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS - 1):
            self.sign_in(code="")
        self.assertEqual(self.sign_in(code=totp.code(secret)).status_code, 303)
        self.assertEqual(len(self.twostep._wrong[self.twostep.SIGN_IN]),
                         self.twostep.CODE_ATTEMPTS - 1)
        self.assertEqual(self.sign_in(code="").status_code, 401)
        self.assertEqual(self.sign_in(code=self.next_code(secret)).status_code, 429)

    def test_wrong_passwords_do_not_count_against_the_codes(self):
        # Or anybody at all could keep the administrator out five minutes at a
        # time, without knowing a thing.
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS + 2):
            self.sign_in(password="wrong", code="000000")
        self.appmod._failed_logins.clear()
        self.assertEqual(self.sign_in(code=totp.code(secret)).status_code, 303)


class SwitchingItOn(TwoStepCase):
    def test_the_settings_page_offers_it(self):
        body = self.page("/settings")
        self.assertIn('href="/settings/totp"', body)
        self.assertIn("Set up two-step sign-in", body)

    def test_the_page_shows_a_secret_as_a_picture_and_as_text(self):
        response = self.client.get("/settings/totp")
        self.assertEqual(response.status_code, 200)
        self.assertIn('src="data:image/png;base64,', response.text)
        secret = self.shown(response.text)
        self.assertTrue(totp.valid_secret(secret))
        self.assertEqual([entry[0] for entry in self.twostep._pending.values()], [secret])
        self.assertIn(USER, response.text)
        # It holds the secret: not for a cache, nor for the back button.
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_looking_twice_shows_the_same_secret(self):
        # Or the app would hold the one from the first look and the form would
        # check against the one from the second.
        first = self.shown(self.client.get("/settings/totp").text)
        self.assertTrue(first)
        self.assertEqual(self.shown(self.client.get("/settings/totp").text), first)

    def test_another_session_is_shown_a_secret_of_its_own(self):
        # The second step is switched on above all when the password is
        # thought to have got out — which is when somebody else may be signed
        # in with it. They must not be shown what the administrator's app is
        # about to be given.
        mine = self.shown(self.client.get("/settings/totp").text)
        other = self.signed_in_elsewhere()
        theirs = self.shown(other.get("/settings/totp").text)
        self.assertTrue(mine and theirs)
        self.assertNotEqual(theirs, mine)
        # Nor does their looking change mine, however often.
        other.get("/settings/totp")
        self.assertEqual(self.shown(self.client.get("/settings/totp").text), mine)

    def test_a_code_for_one_sessions_secret_is_no_use_to_another(self):
        mine = self.shown(self.client.get("/settings/totp").text)
        other = self.signed_in_elsewhere()
        other.get("/settings/totp")
        response = other.post("/settings/totp/confirm", data={"code": totp.code(mine)})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(settings.two_step_on())

    def test_switching_on_throws_away_what_the_others_were_shown(self):
        other = self.signed_in_elsewhere()
        theirs = self.shown(other.get("/settings/totp").text)
        mine = self.shown(self.client.get("/settings/totp").text)
        self.client.post("/settings/totp/confirm", data={"code": totp.code(mine)},
                         follow_redirects=False)
        self.assertEqual(config.TOTP_SECRET, mine)
        self.assertEqual(self.twostep._pending, {})
        other.post("/settings/totp/confirm", data={"code": totp.code(theirs)},
                   follow_redirects=False)
        self.assertEqual(config.TOTP_SECRET, mine)

    def test_sessions_setting_it_up_do_not_pile_up(self):
        for _ in range(self.twostep.PENDING_KEPT + 3):
            self.signed_in_elsewhere().get("/settings/totp")
        self.assertEqual(len(self.twostep._pending), self.twostep.PENDING_KEPT)

    def test_a_confirmation_from_a_session_that_never_looked(self):
        response = self.signed_in_elsewhere().post("/settings/totp/confirm",
                                                   data={"code": "123456"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(settings.two_step_on())

    def test_looking_switches_nothing_on(self):
        self.client.get("/settings/totp")
        self.assertFalse(settings.two_step_on())
        self.assertFalse(os.path.exists(settings.SETTINGS_PATH))

    def test_a_wrong_code_switches_nothing_on(self):
        self.client.get("/settings/totp")
        for code in ("", "000000x", "12345"):
            with self.subTest(code=code):
                response = self.client.post("/settings/totp/confirm", data={"code": code})
                self.assertEqual(response.status_code, 400)
                self.assertIn("That is not the code", response.text)
                self.assertFalse(settings.two_step_on())

    def test_the_right_code_does(self):
        secret = self.shown(self.client.get("/settings/totp").text)
        response = self.client.post("/settings/totp/confirm", data={"code": totp.code(secret)},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(config.TOTP_SECRET, secret)
        body = self.page("/settings/totp?done=on")
        self.assertIn("Two-step sign-in is on", body)
        # Once it is on, the page no longer shows it.
        self.assertNotIn(totp.grouped(secret), body)
        self.assertNotIn("data:image/png", body)
        self.assertIn("Manage two-step sign-in", self.page("/settings"))

    def test_a_secret_left_too_long_is_not_switched_on(self):
        secret = self.shown(self.client.get("/settings/totp").text)
        for owner, (kept, _) in list(self.twostep._pending.items()):
            self.twostep._pending[owner] = (kept, time.time() - 1)
        response = self.client.post("/settings/totp/confirm", data={"code": totp.code(secret)})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(settings.two_step_on())

    def test_calling_it_off_throws_the_secret_away(self):
        secret = self.shown(self.client.get("/settings/totp").text)
        response = self.client.post("/settings/totp/cancel", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.twostep._pending, {})
        again = self.shown(self.client.get("/settings/totp").text)
        self.assertTrue(again)
        self.assertNotEqual(again, secret)

    def test_none_of_it_without_signing_in(self):
        stranger = TestClient(self.app)
        self.assertEqual(stranger.get("/settings/totp", follow_redirects=False).status_code, 303)
        for url in ("/settings/totp/confirm", "/settings/totp/cancel", "/settings/totp/disable"):
            with self.subTest(url=url):
                response = stranger.post(url, data={"code": "000000"}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertIn("/login", response.headers["location"])


class SwitchingItOff(TwoStepCase):
    def test_it_takes_a_code(self):
        self.switch_on()
        for code in ("", "000000x"):
            with self.subTest(code=code):
                response = self.client.post("/settings/totp/disable", data={"code": code})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(settings.two_step_on())

    def test_and_with_one_it_is_off(self):
        secret = self.switch_on()
        response = self.client.post("/settings/totp/disable", data={"code": totp.code(secret)},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertFalse(settings.two_step_on())
        self.assertEqual(self.written()["TOTP_SECRET"], "")
        self.assertEqual(self.sign_in().status_code, 303)

    def test_wrong_codes_run_out_here_as_well(self):
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS):
            self.client.post("/settings/totp/disable", data={"code": ""})
        response = self.client.post("/settings/totp/disable", data={"code": totp.code(secret)})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(settings.two_step_on())

    def test_but_they_do_not_close_the_sign_in_form(self):
        # A session left open on somebody else's computer could otherwise keep
        # the administrator out for as long as it liked, five wrong codes at a
        # time, with neither the password nor a code.
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS + 2):
            self.client.post("/settings/totp/disable", data={"code": ""})
        self.assertEqual(self.sign_in(code=totp.code(secret)).status_code, 303)

    def test_nor_does_the_sign_in_form_close_this_one(self):
        secret = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS + 2):
            self.sign_in(code="")
        response = self.client.post("/settings/totp/disable", data={"code": totp.code(secret)},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertFalse(settings.two_step_on())

    def test_the_reveal_button_does_not_hand_the_secret_back(self):
        secret = self.switch_on()
        response = self.client.get("/settings/secret?key=TOTP_SECRET")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(secret, response.text)

    def test_the_secret_is_on_no_page(self):
        secret = self.switch_on()
        for url in ("/settings", "/settings/totp", "/"):
            with self.subTest(url=url):
                body = self.page(url)
                self.assertNotIn(secret, body)
                self.assertNotIn(totp.grouped(secret), body)

    def test_the_letters_tab_cannot_take_it_off(self):
        # That form saves whatever "setting:" and "switch:" fields arrive.
        secret = self.switch_on()
        for field in ("setting:TOTP_SECRET", "switch:TOTP_SECRET"):
            with self.subTest(field=field):
                self.client.post("/settings/texts", data={field: ""}, follow_redirects=False)
                self.assertEqual(config.TOTP_SECRET, secret)

    def test_the_settings_form_does_not_touch_it(self):
        import admin.routes_settings as routes_settings
        caught = {}
        real = routes_settings.settings.save
        routes_settings.settings.save = lambda values: caught.update(values)
        try:
            self.client.post("/settings", data={"totp_secret": "", "TOTP_SECRET": ""},
                             follow_redirects=False)
        finally:
            routes_settings.settings.save = real
        self.assertNotIn("TOTP_SECRET", caught)


class SettingItUpAgain(TwoStepCase):
    """
    A new phone, or a secret thought to have been seen. Done as "switch off,
    then set up", the panel would stand on its password for as long as the
    setting up took — and for good if it was abandoned half-way. So the old
    secret stays the second step until the new one has produced a code.
    """

    def begin(self, old: str) -> str:
        """Asks for a new secret with a code for the old one; returns the new one shown."""
        response = self.client.post("/settings/totp/replace", data={"code": totp.code(old)},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return self.shown(self.page("/settings/totp"))

    def test_the_page_offers_it(self):
        self.switch_on()
        body = self.page("/settings/totp")
        self.assertIn('formaction="/settings/totp/replace"', body)
        self.assertIn("Set up again", body)

    def test_it_takes_a_code(self):
        old = self.switch_on()
        for code in ("", "000000x"):
            with self.subTest(code=code):
                response = self.client.post("/settings/totp/replace", data={"code": code})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.shown(self.page("/settings/totp")), "")
        self.assertEqual(config.TOTP_SECRET, old)
        self.assertEqual(self.twostep._pending, {})

    def test_a_code_buys_a_new_secret_and_changes_nothing_yet(self):
        old = self.switch_on()
        new = self.begin(old)
        self.assertTrue(totp.valid_secret(new))
        self.assertNotEqual(new, old)
        body = self.page("/settings/totp")
        self.assertIn("data:image/png;base64,", body)
        self.assertIn("Two-step sign-in stays on", body)
        self.assertIn("Replace the entry", body)
        # The old one is still what the door asks for, in memory and on disk.
        self.assertEqual(config.TOTP_SECRET, old)
        self.assertEqual(self.written()["TOTP_SECRET"], old)
        self.assertEqual(self.sign_in(code=self.next_code(old)).status_code, 303)
        self.assertEqual(self.sign_in().status_code, 401)

    def test_a_code_from_the_new_one_puts_it_in_force(self):
        old = self.switch_on()
        new = self.begin(old)
        response = self.client.post("/settings/totp/confirm", data={"code": totp.code(new)},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("done=new", response.headers["location"])
        self.assertEqual(config.TOTP_SECRET, new)
        self.assertEqual(self.written()["TOTP_SECRET"], new)
        body = self.page("/settings/totp?done=new")
        self.assertIn("The new entry is in force", body)
        self.assertEqual(self.shown(body), "")
        # And the old entry makes codes for nothing.
        self.assertEqual(self.sign_in(code=self.next_code(old)).status_code, 401)
        self.assertEqual(self.sign_in(code=self.next_code(new)).status_code, 303)

    def test_a_code_from_the_old_one_does_not(self):
        # The easy slip: the app now holds two entries with the same name.
        old = self.switch_on()
        self.begin(old)
        response = self.client.post("/settings/totp/confirm",
                                    data={"code": self.next_code(old)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config.TOTP_SECRET, old)

    def test_called_off_everything_is_as_it_was(self):
        old = self.switch_on()
        self.begin(old)
        response = self.client.post("/settings/totp/cancel", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/settings/totp")
        self.assertEqual(config.TOTP_SECRET, old)
        self.assertEqual(self.twostep._pending, {})
        self.assertEqual(self.shown(self.page("/settings/totp")), "")

    def test_left_too_long_it_lapses_and_the_old_one_stays(self):
        old = self.switch_on()
        new = self.begin(old)
        for owner, (kept, _) in list(self.twostep._pending.items()):
            self.twostep._pending[owner] = (kept, time.time() - 1)
        response = self.client.post("/settings/totp/confirm", data={"code": totp.code(new)},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(config.TOTP_SECRET, old)
        self.assertEqual(self.shown(self.page("/settings/totp")), "")

    def test_another_session_neither_sees_the_new_secret_nor_can_bring_one_in(self):
        # It gave no code, so it has no say: an open session is not enough to
        # swap the secret for one of its own.
        old = self.switch_on()
        new = self.begin(old)
        other = self.signed_in_elsewhere_with(old)
        body = other.get("/settings/totp").text
        self.assertEqual(self.shown(body), "")
        self.assertNotIn("data:image/png", body)
        response = other.post("/settings/totp/confirm", data={"code": totp.code(new)},
                              follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(config.TOTP_SECRET, old)

    def signed_in_elsewhere_with(self, secret: str) -> TestClient:
        other = TestClient(self.app)
        response = other.post("/login", follow_redirects=False, data={
            "username": USER, "password": PASSWORD, "code": self.next_code(secret)})
        self.assertEqual(response.status_code, 303)
        return other

    def test_the_lock_is_on_the_door_not_only_on_the_page(self):
        # A secret-in-waiting left from before the second step was on must not
        # become the secret because somebody knows a code for it.
        old = self.switch_on()
        stray = totp.new_secret()
        self.twostep._pending["a-session"] = (stray, time.time() + 600)
        self.assertFalse(self.twostep.confirm("a-session", totp.code(stray)))
        self.assertEqual(config.TOTP_SECRET, old)

    def test_switching_off_in_the_middle_ends_it(self):
        old = self.switch_on()
        self.begin(old)
        self.client.post("/settings/totp/disable", data={"code": self.next_code(old)},
                         follow_redirects=False)
        self.assertFalse(settings.two_step_on())
        self.assertEqual(self.twostep._pending, {})
        self.assertEqual(self.twostep._replacing, set())

    def test_wrong_codes_run_out_here_too(self):
        old = self.switch_on()
        for _ in range(self.twostep.CODE_ATTEMPTS):
            self.client.post("/settings/totp/replace", data={"code": ""})
        response = self.client.post("/settings/totp/replace", data={"code": totp.code(old)})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.twostep._pending, {})

    def test_not_without_signing_in(self):
        self.switch_on()
        response = TestClient(self.app).post("/settings/totp/replace", data={"code": "000000"},
                                             follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/login", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
