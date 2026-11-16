"""
What the poll loop refuses to answer, and what it refuses to believe.

The bot replies to whatever arrives, which makes three things dangerous that a
person reading the mailbox would never fall for: a machine on the other end
that answers every reply, a sender who never stops, and a From header that
anybody can type. None of these breaks loudly — the symptom is a mailbox
talking to itself all night, or a broadcast nobody on the team sent.
"""
import email
import os
import sys
import unittest
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import email_bot
import inbox
import mailer
from xui_client import XuiUnavailable


def letter(sender="Ben <ben@example.com>", subject="hello", body="hi", **headers):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = headers.pop("message_id", "<1@example.com>")
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return msg


class Automatic(unittest.TestCase):
    def reason(self, **headers):
        sender = headers.pop("sender", "Ben <ben@example.com>")
        return inbox.automatic_reason(letter(sender=sender, **headers),
                                      inbox.sender_name_from(sender)[0])

    def test_a_person_is_answered(self):
        self.assertEqual(self.reason(), "")

    def test_auto_submitted_no_is_a_person_too(self):
        self.assertEqual(self.reason(Auto_Submitted="no"), "")

    def test_an_autoreply_is_not(self):
        self.assertTrue(self.reason(Auto_Submitted="auto-replied"))
        self.assertTrue(self.reason(X_Autoreply="yes"))

    def test_a_request_for_no_out_of_office_reply_is_not_a_machine(self):
        # Software on the sender's side sets this, and a subscription link is
        # not an out-of-office reply: such a person must still be answered.
        self.assertEqual(self.reason(X_Auto_Response_Suppress="OOF, AutoReply"), "")

    def test_nor_a_mailing_list(self):
        self.assertTrue(self.reason(Precedence="bulk"))
        self.assertTrue(self.reason(List_Unsubscribe="<mailto:off@example.com>"))
        self.assertTrue(self.reason(List_Id="<news.example.com>"))

    def test_nor_a_bounce(self):
        self.assertTrue(self.reason(sender="Mail Delivery <MAILER-DAEMON@example.com>"))
        self.assertTrue(self.reason(sender="noreply@example.com"))
        self.assertTrue(self.reason(Return_Path="<>"))

    def test_an_address_that_only_resembles_one_is_a_person(self):
        self.assertEqual(self.reason(sender="bounceback.fan@example.com"), "")


class Throttle(unittest.TestCase):
    def setUp(self):
        inbox._answered.clear()

    def tearDown(self):
        inbox._answered.clear()

    def test_the_limit_is_reached_and_then_forgotten(self):
        for second in range(inbox.REPLY_LIMIT):
            self.assertFalse(inbox.over_limit("ben@example.com", now=1000 + second))
            inbox.note_answered("ben@example.com", now=1000 + second)
        self.assertTrue(inbox.over_limit("ben@example.com", now=1100))
        self.assertFalse(inbox.over_limit("ann@example.com", now=1100),
                         "one sender's letters counted against another")
        self.assertFalse(inbox.over_limit("ben@example.com",
                                          now=1100 + inbox.REPLY_WINDOW))

    def test_forged_letters_do_not_use_up_the_real_senders_answers(self):
        # Ten letters with somebody's address typed into From, and the person
        # themselves writes an eleventh — which their server vouches for.
        for second in range(inbox.REPLY_LIMIT):
            inbox.note_answered("ben@example.com", proven=False, now=1000 + second)
        self.assertTrue(inbox.over_limit("ben@example.com", proven=False, now=1100))
        self.assertFalse(inbox.over_limit("ben@example.com", proven=True, now=1100))


class Authenticated(unittest.TestCase):
    def verdict(self, *results, sender="admin@example.com"):
        # Through bytes and back, as the poll loop gets it: compat32 headers,
        # folded lines and all.
        raw = letter(sender=sender).as_bytes()
        extra = b"".join(b"Authentication-Results: " + value.replace("\n", "\r\n").encode() + b"\r\n"
                         for value in results)
        msg = email.message_from_bytes(extra + raw)
        return inbox.sender_authenticated(msg, sender)

    def test_no_verdict_is_no_proof(self):
        self.assertFalse(self.verdict())

    def test_dmarc_pass_is_proof(self):
        self.assertTrue(self.verdict(
            "mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=admin@example.com; "
            "dmarc=pass (p=NONE) header.from=example.com"))

    def test_the_headers_the_large_providers_actually_write(self):
        # Copied in shape from real letters: folded, with comments that hold
        # colons, commas and brackets, and a signature that is all punctuation.
        self.assertTrue(self.verdict(
            "mx.google.com;\n"
            "       dkim=pass header.i=@gmail.com header.s=20230601 header.b=\"Ab+C/d==\";\n"
            "       spf=pass (google.com: domain of admin@gmail.com designates 209.85.220.41 "
            "as permitted sender) smtp.mailfrom=admin@gmail.com;\n"
            "       dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com;\n"
            "       dara=pass header.i=@gmail.com",
            sender="admin@gmail.com"))
        self.assertTrue(self.verdict(
            "mxfront.yandex.ru; spf=pass (mxfront.yandex.ru: domain of gmail.com designates "
            "209.85.208.53 as permitted sender, rule=[ip4:209.85.128.0/17]) "
            "smtp.mail=admin@gmail.com; dkim=pass header.i=@gmail.com",
            sender="admin@gmail.com"))
        self.assertTrue(self.verdict(
            "mxs.mail.ru; spf=pass (mx123.i.mail.ru: domain of yandex.ru designates 77.88.28.1 "
            "as permitted sender) smtp.mailfrom=admin@yandex.ru smtp.helo=forward.yandex.net;\n"
            "\t dkim=pass header.d=yandex.ru; dmarc=pass header.from=admin@yandex.ru",
            sender="admin@yandex.ru"))

    def test_a_dmarc_failure_is_not(self):
        self.assertFalse(self.verdict(
            "mx.google.com; spf=softfail smtp.mailfrom=x@evil.test; dmarc=fail header.from=example.com"))

    def test_a_signature_from_the_senders_own_domain_is_proof(self):
        # Yandex reports no dmarc= of its own, only the two it is built from.
        self.assertTrue(self.verdict(
            "mxfront.yandex.ru; spf=pass (comment; with a semicolon) smtp.mail=admin@example.com; "
            "dkim=pass header.i=@example.com"))

    def test_somebody_elses_valid_signature_is_not(self):
        # A spoofed From with a perfectly good signature — the spoofer's own.
        self.assertFalse(self.verdict(
            "mxfront.yandex.ru; spf=pass smtp.mail=x@evil.test; dkim=pass header.i=@evil.test"))

    def test_only_the_receiving_servers_verdict_counts(self):
        # The top-most header is the one our mailbox's server wrote; the one
        # below it came with the letter and says whatever its author liked.
        self.assertFalse(self.verdict(
            "mx.google.com; dmarc=fail header.from=example.com",
            "forged.example; dmarc=pass header.from=example.com"))

    def test_not_even_under_the_servers_own_name(self):
        # A server is supposed to strip an incoming header that claims its
        # name. Not all do, so the name is not what is trusted.
        self.assertFalse(self.verdict(
            "mx.example.net; spf=pass smtp.mailfrom=x@evil.test",
            "mx.example.net; dmarc=pass header.from=example.com"))

    def test_a_pass_for_another_domain_is_not_a_pass_for_this_one(self):
        self.assertFalse(self.verdict(
            "mx.google.com; dmarc=pass header.from=evil.test"))

    def test_a_domain_with_no_policy_falls_back_on_the_signature(self):
        self.assertTrue(self.verdict(
            "mx.example.net; dkim=pass header.d=example.com; dmarc=none header.from=example.com"))

    def test_a_dmarc_failure_outweighs_whatever_else_passed(self):
        self.assertFalse(self.verdict(
            "mx.example.net; dkim=pass header.d=example.com; dmarc=fail header.from=example.com"))

    # What follows is the sender writing the verdict for themselves. The server
    # copies the envelope address into its header as it stands, and an address
    # can be made to spell a great deal.

    def test_an_envelope_address_that_spells_a_verdict(self):
        # MAIL FROM:<dmarc=pass@evil.test> is a perfectly legal address.
        self.assertFalse(self.verdict(
            "mx.google.com; spf=pass (google.com: domain of dmarc=pass@evil.test designates "
            "203.0.113.5 as permitted sender) smtp.mailfrom=dmarc=pass@evil.test; "
            "dmarc=fail (p=NONE) header.from=example.com"))
        self.assertFalse(self.verdict(
            "mxfront.yandex.ru; spf=pass smtp.mail=dmarc=pass@evil.test; "
            "dkim=pass header.i=@evil.test"))

    def test_a_quoted_address_holding_a_whole_verdict(self):
        self.assertFalse(self.verdict(
            'mx.google.com; spf=pass smtp.mailfrom="a; dmarc=pass header.from=example.com b"@evil.test; '
            "dmarc=fail header.from=example.com"))
        self.assertFalse(self.verdict(
            'mxfront.yandex.ru; spf=pass smtp.mail="a; dkim=pass header.d=example.com b"@evil.test'))

    def test_a_quoted_address_that_tries_to_close_the_comment(self):
        self.assertFalse(self.verdict(
            'mx.example.net; spf=pass (domain of "a) ; dkim=pass header.d=example.com ("@evil.test '
            'designates 203.0.113.5) smtp.mailfrom="a) ; dkim=pass header.d=example.com ("@evil.test'))

    def test_a_signers_identity_that_names_the_senders_domain_in_passing(self):
        self.assertFalse(self.verdict(
            'mx.example.net; dkim=pass header.i="x header.d=example.com y"@evil.test'))
        self.assertFalse(self.verdict(
            "mx.example.net; dkim=pass header.d=example.com.evil.test"))

    def test_a_header_that_does_not_parse_proves_nothing(self):
        self.assertFalse(self.verdict(
            'mx.example.net; dmarc=pass header.from=example.com; spf=pass smtp.mailfrom="unclosed'))


class FakeIMAP:
    """One unread letter, and a record of whether it was marked read."""

    def __init__(self, msg):
        self.raw = msg.as_bytes()
        self.stored = []

    def login(self, user, password):
        return "OK", [b""]

    def select(self, folder):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b"1"]

    def fetch(self, num, what):
        # As a real server does it: asking for the body marks the letter read,
        # unless it was asked for with PEEK. The loop's whole promise — that a
        # letter not dealt with is still unread next time — hangs on that word.
        if "PEEK" not in what:
            self.stored.append(num)
        return "OK", [(b"1 (BODY[] {%d}" % len(self.raw), self.raw), b")"]

    def store(self, num, command, flags):
        self.stored.append(num)
        return "OK", [b""]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]

    def shutdown(self):
        pass


class Poll(unittest.TestCase):
    """The loop itself: what reaches the handler and what is marked read."""

    def setUp(self):
        self.saved = (config.IMAP_USER, config.IMAP_PASSWORD, config.MAIL_CLEANUP_ENABLED)
        config.IMAP_USER, config.IMAP_PASSWORD = "bot@example.com", "secret"
        config.MAIL_CLEANUP_ENABLED = False
        self.real_ssl = inbox.imaplib.IMAP4_SSL
        inbox._answered.clear()
        inbox._attempts.clear()
        self.handled = []

    def tearDown(self):
        inbox.imaplib.IMAP4_SSL = self.real_ssl
        inbox._answered.clear()
        inbox._attempts.clear()
        (config.IMAP_USER, config.IMAP_PASSWORD, config.MAIL_CLEANUP_ENABLED) = self.saved

    def poll(self, msg, handler=None):
        mail = FakeIMAP(msg)
        inbox.imaplib.IMAP4_SSL = lambda *a, **kw: mail

        def record(num, sender, subject, body, conn, name="", authenticated=False):
            self.handled.append((sender, subject, name, authenticated))
            inbox._mark_seen(conn, num)

        inbox.check_mail(handler or record)
        return mail

    def test_a_letter_from_a_person_reaches_the_handler(self):
        mail = self.poll(letter(subject="=?utf-8?b?0J/RgNC40LLQtdGC?="))
        self.assertEqual(self.handled, [("ben@example.com", "Привет", "Ben", False)])
        self.assertEqual(len(mail.stored), 1)

    def test_the_handler_is_told_when_the_sender_is_proven(self):
        self.poll(letter(Authentication_Results="mx.example.net; dmarc=pass header.from=example.com"))
        self.assertTrue(self.handled[0][3])

    def test_an_autoreply_is_marked_read_and_not_answered(self):
        mail = self.poll(letter(Auto_Submitted="auto-replied"))
        self.assertEqual(self.handled, [])
        self.assertEqual(len(mail.stored), 1, "left unread, it would be looked at again every cycle")

    def test_a_sender_past_the_limit_is_marked_read_and_not_answered(self):
        for _ in range(inbox.REPLY_LIMIT):
            inbox.note_answered("ben@example.com")
        mail = self.poll(letter())
        self.assertEqual(self.handled, [])
        self.assertEqual(len(mail.stored), 1)

    def test_a_handled_letter_counts_against_its_sender(self):
        self.poll(letter())
        self.assertEqual(len(inbox._answered[("ben@example.com", False)]), 1)

    def test_a_letter_that_fails_is_still_unread_for_the_next_try(self):
        def broken(*args, **kwargs):
            raise RuntimeError("SMTP refused")

        mail = self.poll(letter(), handler=broken)
        self.assertEqual(mail.stored, [], "read on the first failure, it is never tried again")
        self.assertEqual(inbox._attempts, {"<1@example.com>": 1})

    def test_a_letter_from_nobody_is_not_fetched_for_ever(self):
        mail = self.poll(letter(sender="undisclosed-recipients:;"))
        self.assertEqual(self.handled, [])
        self.assertEqual(len(mail.stored), 1)

    def test_the_dashboard_is_told_while_letters_wait(self):
        def later(*args, **kwargs):
            raise inbox.TryLater("3x-ui is away")

        self.poll(letter(), handler=later)
        self.assertTrue(inbox.mail_health()["waiting"])
        self.poll(letter())
        self.assertFalse(inbox.mail_health()["waiting"])

    def test_a_letter_put_off_stays_unread_and_is_not_held_against_anybody(self):
        def later(*args, **kwargs):
            raise inbox.TryLater("3x-ui is away")

        for _ in range(inbox.MAX_ATTEMPTS + 2):
            mail = self.poll(letter(), handler=later)
            self.assertEqual(mail.stored, [], "an outage must not cost the letter")
        self.assertEqual(inbox._attempts, {})
        self.assertEqual(inbox._answered, {})


class Outgoing(unittest.TestCase):
    def test_every_letter_says_a_machine_wrote_it(self):
        msg = mailer.build_message("ben@example.com", "Hello", "<p>hi</p>",
                                   smtp_user="bot@example.com", service_name="Service")
        # What makes the other side's autoresponder keep quiet — and what this
        # bot itself looks for before answering.
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")
        self.assertTrue(inbox.automatic_reason(msg, "bot@example.com"))


class Dispatch(unittest.TestCase):
    """The commands, as far as these guards reach into them."""

    NAMES = ("handle_broadcast", "handle_unknown", "send_email_reply", "_mark_seen",
             "get_shared_client")

    def setUp(self):
        self.saved_admin = config.ADMIN_EMAIL
        config.ADMIN_EMAIL = "admin@example.com"
        self.real = {name: getattr(email_bot, name) for name in self.NAMES}
        self.calls = []
        email_bot.handle_broadcast = lambda text: self.calls.append(("broadcast", text)) or 3
        email_bot.handle_unknown = lambda addr, subject: self.calls.append(("unknown", addr))
        email_bot.send_email_reply = lambda addr, subject, message: \
            self.calls.append(("reply", addr, subject))
        email_bot._mark_seen = lambda conn, num: self.calls.append(("seen", num))

    def tearDown(self):
        for name, value in self.real.items():
            setattr(email_bot, name, value)
        config.ADMIN_EMAIL = self.saved_admin

    def test_a_broadcast_from_a_proven_administrator_goes_out(self):
        email_bot.process_message(1, "admin@example.com", "/broadcast Maintenance at noon",
                                  "", None, authenticated=True)
        self.assertIn(("broadcast", "Maintenance at noon"), self.calls)

    def test_a_broadcast_from_an_unproven_one_does_not(self):
        email_bot.process_message(1, "admin@example.com", "/broadcast Send me your password",
                                  "", None, authenticated=False)
        self.assertNotIn("broadcast", [call[0] for call in self.calls])
        # The real administrator hears of it either way: their own letter that
        # did not go out, or somebody else's that tried to.
        self.assertIn("reply", [call[0] for call in self.calls])
        self.assertIn(("seen", 1), self.calls, "left unread, it would be refused again every cycle")

    def test_an_outage_puts_the_letter_off_instead_of_answering_wrongly(self):
        class Away:
            def find_client_by_email(self, address, strict=False):
                if strict:
                    raise XuiUnavailable("down")
                return None

        email_bot.get_shared_client = lambda: Away()
        # Through the wrapper the poll loop is given, which is where an outage
        # becomes "try this letter again" rather than a strike against it.
        with self.assertRaises(inbox.TryLater):
            email_bot._handle_letter(1, "ben@example.com", "/status", "", None)
        self.assertNotIn("reply", [call[0] for call in self.calls],
                         "a registered client would have been told they are not registered")
        self.assertNotIn(("seen", 1), self.calls)


if __name__ == "__main__":
    unittest.main()
