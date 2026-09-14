"""
Turning a 3x-ui client into something this project can address.

These helpers exist because the panel's `email` field is an identifier with a
pared-down character set rather than a mailbox, and because a client added by
hand may carry no address at all. Every case below is one the panel has actually
produced.
"""
import unittest

import email_bot
from xui_client import XuiClient


class ExtractBareEmail(unittest.TestCase):
    def test_plain_address(self):
        self.assertEqual(XuiClient.extract_bare_email("user@example.com"), "user@example.com")

    def test_name_and_address(self):
        self.assertEqual(XuiClient.extract_bare_email("Иван <ivan@example.com>"), "ivan@example.com")

    def test_case_is_flattened(self):
        self.assertEqual(XuiClient.extract_bare_email("User@Example.COM"), "user@example.com")

    def test_identifier_without_an_address(self):
        # Somebody added by hand in 3x-ui: there is nowhere to write to, and the
        # broadcast has to be able to tell.
        self.assertEqual(XuiClient.extract_bare_email("falcon"), "")
        self.assertEqual(XuiClient.extract_bare_email(""), "")
        self.assertEqual(XuiClient.extract_bare_email(None), "")


class ClientKey(unittest.TestCase):
    def test_uuid_wins(self):
        self.assertEqual(XuiClient.client_key({"uuid": "abc", "id": 7}), "abc")

    def test_numeric_id_is_the_fallback(self):
        # Older or pared-down answers carry no uuid.
        self.assertEqual(XuiClient.client_key({"id": 7}), "7")

    def test_nothing_to_key_on(self):
        self.assertEqual(XuiClient.client_key({}), "")
        self.assertEqual(XuiClient.client_key(None), "")


class BuildClientEmail(unittest.TestCase):
    def test_address_passes_through(self):
        self.assertEqual(email_bot.build_client_email("user@example.com"), "user@example.com")

    def test_name_is_dropped(self):
        # The panel refuses spaces and angle brackets in that field.
        self.assertEqual(email_bot.build_client_email("Иван <ivan@example.com>"), "ivan@example.com")

    def test_forbidden_characters_are_stripped(self):
        self.assertEqual(email_bot.build_client_email("a b@example.com"), "ab@example.com")


class BuildComment(unittest.TestCase):
    def setUp(self):
        self._was = email_bot.config.REMARK_INCLUDE_NAME
        email_bot.config.REMARK_INCLUDE_NAME = True

    def tearDown(self):
        email_bot.config.REMARK_INCLUDE_NAME = self._was

    def test_name_is_kept(self):
        self.assertEqual(email_bot.build_comment("Иван Петров"), "Иван Петров")

    def test_angle_brackets_and_newlines_go(self):
        self.assertEqual(email_bot.build_comment("Иван <ivan@x>\nвторая строка"),
                         "Иван ivan@x вторая строка")

    def test_switched_off(self):
        email_bot.config.REMARK_INCLUDE_NAME = False
        self.assertEqual(email_bot.build_comment("Иван"), "")


if __name__ == "__main__":
    unittest.main()
