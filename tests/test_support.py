"""
Letters the bot cannot understand, and letters it must not answer at all.

The second matters more than it looks: two automatic mailboxes answering each
other is a loop that only stops when one of them is switched off, and every
turn of it is a letter from this installation's address.
"""
import email
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import email_bot
import inbox
import mailer
import templates


def letter(**headers):
    msg = email.message.EmailMessage()
    msg["From"] = headers.pop("From", "ann@example.com")
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content("hello")
    return msg


class AutomaticLetters(unittest.TestCase):
    def setUp(self):
        self.saved = (config.IMAP_USER, config.SMTP_USER)
        config.IMAP_USER = config.SMTP_USER = "bot@example.com"

    def tearDown(self):
        config.IMAP_USER, config.SMTP_USER = self.saved

    def reason(self, msg, sender="ann@example.com"):
        return inbox.automatic_reason(msg, sender)

    def test_a_person_is_answered(self):
        self.assertEqual(self.reason(letter()), "")
        self.assertEqual(self.reason(letter(Auto_Submitted="no")), "")

    def test_a_vacation_reply_is_not(self):
        self.assertTrue(self.reason(letter(Auto_Submitted="auto-replied")))
        self.assertTrue(self.reason(letter(Precedence="bulk")))
        self.assertTrue(self.reason(letter(X_Autoreply="yes")))

    def test_a_bounce_and_a_no_reply_sender_are_not(self):
        self.assertTrue(self.reason(letter(), "mailer-daemon@example.org"))
        self.assertTrue(self.reason(letter(), "noreply@example.org"))

    def test_a_mailing_list_is_not(self):
        self.assertTrue(self.reason(letter(List_Id="<news.example.org>")))

    def test_the_bot_itself_is_not(self):
        self.assertTrue(self.reason(letter(), "bot@example.com"))

    def test_outlook_asking_not_to_be_auto_answered_is_still_a_person(self):
        self.assertEqual(self.reason(letter(X_Auto_Response_Suppress="All")), "")


class OurLetters(unittest.TestCase):
    def test_say_they_were_written_by_a_program(self):
        msg = mailer.build_message("ann@example.com", "Hi", "<p>hi</p>", smtp_user="bot@example.com")
        self.assertEqual(msg["Auto-Submitted"], "auto-replied")
        self.assertIsNone(msg["Reply-To"])

    def test_a_forwarded_one_is_answered_to_the_client(self):
        msg = mailer.build_message("support@example.com", "Hi", "<p>hi</p>",
                                   smtp_user="bot@example.com", reply_to="ann@example.com")
        self.assertEqual(msg["Reply-To"], "ann@example.com")


class Forwarding(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(config, k) for k in
                      ("SUPPORT_FORWARD_ENABLED", "SUPPORT_EMAIL", "ADMIN_EMAIL", "IMAP_USER", "SMTP_USER")}
        config.SUPPORT_FORWARD_ENABLED = True
        config.SUPPORT_EMAIL = "Help <help@example.com>"
        config.ADMIN_EMAIL = "admin@example.com"
        config.IMAP_USER = config.SMTP_USER = "bot@example.com"
        self.sent = []
        self.real_send = email_bot.send_email_reply
        email_bot.send_email_reply = lambda to, subject, message, reply_to=None: \
            self.sent.append((to, subject, reply_to, message))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        email_bot.send_email_reply = self.real_send

    def test_the_letter_goes_to_support_and_the_client_is_told(self):
        email_bot.handle_unknown("ann@example.com", "My VPN is slow", "It is slow since Monday", "Ann")
        to, subject, reply_to, message = self.sent[0]
        self.assertEqual((to, reply_to), ("help@example.com", "ann@example.com"))
        self.assertIn("My VPN is slow", subject)
        self.assertIn("It is slow since Monday", message.text)
        self.assertIn("Ann <ann@example.com>", message.text)
        self.assertEqual(self.sent[1][:2], ("ann@example.com", templates.notice_subject("forwarded")))

    def test_without_a_support_address_the_administrator_gets_it(self):
        config.SUPPORT_EMAIL = ""
        self.assertEqual(email_bot.support_target("ann@example.com"), "admin@example.com")

    def test_switched_off_it_answers_as_before(self):
        config.SUPPORT_FORWARD_ENABLED = False
        email_bot.handle_unknown("ann@example.com", "hi", "hi")
        self.assertEqual([s[0] for s in self.sent], ["ann@example.com"])
        self.assertEqual(self.sent[0][1], templates.notice_subject("unknown"))

    def test_never_to_the_bot_itself_or_back_to_the_sender(self):
        config.SUPPORT_EMAIL = "bot@example.com"
        self.assertEqual(email_bot.support_target("ann@example.com"), "")
        config.SUPPORT_EMAIL = "help@example.com"
        self.assertEqual(email_bot.support_target("help@example.com"), "")


if __name__ == "__main__":
    unittest.main()
