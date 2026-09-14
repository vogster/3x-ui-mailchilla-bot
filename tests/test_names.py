"""
Taking names out of the letters.

The reading of the mailbox is awkward to test; the judgement — what counts as a
name, and what counts as a difference worth offering — is not, and that is the
part that decides what somebody is asked to approve.
"""
import unittest

import email_bot


class SenderName(unittest.TestCase):
    def test_a_plain_name(self):
        self.assertEqual(email_bot.sender_name_from("Иван Петров <ivan@example.com>"),
                         ("ivan@example.com", "Иван Петров"))

    def test_an_encoded_name_is_decoded(self):
        self.assertEqual(email_bot.sender_name_from("=?utf-8?B?0JjQstCw0L0=?= <ivan@example.com>"),
                         ("ivan@example.com", "Иван"))

    def test_a_bare_address_carries_no_name(self):
        self.assertEqual(email_bot.sender_name_from("ivan@example.com"),
                         ("ivan@example.com", ""))
        self.assertEqual(email_bot.sender_name_from("<ivan@example.com>"),
                         ("ivan@example.com", ""))

    def test_the_address_repeated_as_a_name_is_not_a_name(self):
        # Some clients put the address in the display name; writing that into
        # the comment would say nothing the email field does not already say.
        self.assertEqual(email_bot.sender_name_from('"ivan@example.com" <ivan@example.com>'),
                         ("ivan@example.com", ""))

    def test_a_folded_header_comes_out_on_one_line(self):
        self.assertEqual(email_bot.sender_name_from("Ivan\r\nPetrov <ivan@example.com>"),
                         ("ivan@example.com", "Ivan Petrov"))

    def test_rubbish_does_not_raise(self):
        for value in ("", None, "не адрес вовсе"):
            address, name = email_bot.sender_name_from(value)
            self.assertIsInstance(address, str)
            self.assertIsInstance(name, str)


class Mismatches(unittest.TestCase):
    FOUND = {
        "anna@example.com": "Анна Иванова",
        "boris@example.com": "Борис",
        "vera@example.com": "Вера",
        "nobody@example.com": "Никто",
    }

    def _rows(self, clients):
        return email_bot.name_mismatches(clients, self.FOUND)

    def test_a_different_name_is_offered(self):
        rows = self._rows([{"uuid": "1", "email": "anna@example.com", "comment": "Аня"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current"], "Аня")
        self.assertEqual(rows[0]["name"], "Анна Иванова")

    def test_an_empty_name_counts_as_a_difference(self):
        # Filling one in is the usual reason for doing this at all.
        rows = self._rows([{"uuid": "3", "email": "vera@example.com", "comment": ""}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current"], "")

    def test_a_matching_name_is_left_alone(self):
        self.assertEqual(self._rows([{"uuid": "2", "email": "boris@example.com",
                                      "comment": "Борис"}]), [])

    def test_whitespace_is_not_a_difference(self):
        self.assertEqual(self._rows([{"uuid": "2", "email": "boris@example.com",
                                      "comment": "  Борис  "}]), [])

    def test_a_client_nobody_has_written_to_is_not_offered(self):
        self.assertEqual(self._rows([{"uuid": "4", "email": "gleb@example.com",
                                      "comment": "Глеб"}]), [])

    def test_an_identifier_that_is_not_an_address_is_skipped(self):
        # Added by hand in 3x-ui: there is no letter that could belong to it.
        self.assertEqual(self._rows([{"uuid": "5", "email": "falcon", "comment": ""}]), [])

    def test_a_name_and_address_identifier_still_matches(self):
        rows = self._rows([{"uuid": "6", "email": "Аня <anna@example.com>", "comment": ""}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "anna@example.com")

    def test_rows_come_back_in_address_order(self):
        rows = self._rows([
            {"uuid": "3", "email": "vera@example.com", "comment": ""},
            {"uuid": "1", "email": "anna@example.com", "comment": ""},
        ])
        self.assertEqual([r["email"] for r in rows],
                         ["anna@example.com", "vera@example.com"])

    def test_no_clients_at_all(self):
        self.assertEqual(email_bot.name_mismatches([], self.FOUND), [])
        self.assertEqual(email_bot.name_mismatches(None, self.FOUND), [])

    def test_an_empty_mailbox_offers_nothing(self):
        self.assertEqual(email_bot.name_mismatches(
            [{"uuid": "1", "email": "anna@example.com", "comment": ""}], {}), [])


if __name__ == "__main__":
    unittest.main()
