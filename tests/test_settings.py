"""
The layer the panel owns.

`_coerce` is the only thing standing between a form field and a value the rest
of the code will trust, so each branch is checked — including the refusals,
which is where a typo turns into a broken installation rather than an error.
"""
import unittest

import config
import settings


class Coerce(unittest.TestCase):
    def test_inbound_ids_from_a_string(self):
        self.assertEqual(settings._coerce("XUI_INBOUND_IDS", "3, 1 ,3"), [3, 1])

    def test_inbound_ids_keep_their_order(self):
        # The panel adds a client down the list and stops at the first failure,
        # so the order is not decoration.
        self.assertEqual(settings._coerce("XUI_INBOUND_IDS", [5, 2, 9]), [5, 2, 9])

    def test_inbound_ids_may_be_empty(self):
        self.assertEqual(settings._coerce("XUI_INBOUND_IDS", []), [])

    def test_ports(self):
        self.assertEqual(settings._coerce("IMAP_PORT", "993"), 993)
        for bad in ("0", "65536", "-1"):
            with self.assertRaises(ValueError):
                settings._coerce("IMAP_PORT", bad)

    def test_poll_interval_has_a_floor(self):
        self.assertEqual(settings._coerce("POLL_INTERVAL_SECONDS", "15"), 15)
        with self.assertRaises(ValueError):
            settings._coerce("POLL_INTERVAL_SECONDS", "4")

    def test_limits_cannot_be_negative(self):
        self.assertEqual(settings._coerce("LIMIT_GB", "0"), 0)
        with self.assertRaises(ValueError):
            settings._coerce("EXPIRE_DAYS", "-1")

    def test_language_must_be_one_we_ship(self):
        self.assertEqual(settings._coerce("PANEL_LANG", "RU"), "ru")
        with self.assertRaises(ValueError):
            settings._coerce("MAIL_LANG", "fr")

    def test_service_name_and_codeword_cannot_be_blank(self):
        for key in ("SERVICE_NAME", "CODEWORD"):
            with self.assertRaises(ValueError):
                settings._coerce(key, "   ")

    def test_admin_email_wants_an_at(self):
        self.assertEqual(settings._coerce("ADMIN_EMAIL", " Me@Example.COM "), "me@example.com")
        self.assertEqual(settings._coerce("ADMIN_EMAIL", ""), "")
        with self.assertRaises(ValueError):
            settings._coerce("ADMIN_EMAIL", "not-an-address")

    def test_flow_may_be_empty(self):
        # Some inbounds use no flow at all.
        self.assertEqual(settings._coerce("XUI_FLOW", "  "), "")

    def test_booleans_take_what_a_form_sends(self):
        for truthy in ("on", "1", "true", "YES", True):
            self.assertIs(settings._coerce("WELCOME_QR_ENABLED", truthy), True)
        for falsy in ("", "off", "0", "no", False):
            self.assertIs(settings._coerce("REMARK_INCLUDE_NAME", falsy), False)

    def test_unknown_key_is_refused(self):
        with self.assertRaises(ValueError):
            settings._coerce("NOT_A_SETTING", "x")


class Masking(unittest.TestCase):
    def test_a_short_secret_gives_nothing_away(self):
        self.assertEqual(settings.mask("short"), "•" * settings.MASK_WIDTH)

    def test_a_long_one_shows_its_ends(self):
        self.assertEqual(settings.mask("abcd12345678wxyz"), "abcd" + "•" * 8 + "wxyz")

    def test_full_masking_hides_even_a_long_one(self):
        self.assertEqual(settings.mask("abcd12345678wxyz", full=True), "•" * settings.MASK_WIDTH)

    def test_nothing_stays_nothing(self):
        self.assertEqual(settings.mask(""), "")


class TranslatedDefaults(unittest.TestCase):
    """The notification texts are read by the administrator, so they follow the panel."""

    def setUp(self):
        self._stored = dict(settings._stored)

    def tearDown(self):
        settings._stored = self._stored
        settings._apply()

    def test_default_follows_the_panel_language(self):
        settings._stored = {"PANEL_LANG": "ru"}
        settings._apply()
        self.assertEqual(config.GOTIFY_TITLE, "Новая регистрация в {service}")
        settings._stored = {"PANEL_LANG": "en"}
        settings._apply()
        self.assertEqual(config.GOTIFY_TITLE, "New {service} registration")

    def test_a_text_that_was_typed_in_is_left_alone(self):
        settings._stored = {"PANEL_LANG": "ru", "GOTIFY_TITLE": "Мой текст"}
        settings._apply()
        self.assertEqual(config.GOTIFY_TITLE, "Мой текст")


if __name__ == "__main__":
    unittest.main()
