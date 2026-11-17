"""
What the poll loop refuses to answer, and what it refuses to believe.

The bot replies to whatever arrives, which makes three things dangerous that a
person reading the mailbox would never fall for: a machine on the other end
that answers every reply, a sender who never stops, and a From header that
anybody can type. None of these breaks loudly — the symptom is a mailbox
talking to itself all night, or a broadcast nobody on the team sent.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import email
import logging
import os
import sys
import unittest
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import email_bot
import inbox
import mailer
import requests
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


class ANamedServer(unittest.TestCase):
    """
    A mail server of one's own, named in the settings.

    The top-most header is the right one to read on Gmail, Yandex or Mail.ru:
    they add one to every letter and say everything in it. A server put
    together from separate filters does neither. It may write nothing at all
    for a letter with no signature, which leaves a header the sender brought
    on top; and it may give its verdict in pieces — DKIM in one header, DMARC
    in another — of which the top-most is only one. Naming the server is the
    answer to both: its headers are read, all of them, and nobody else's.
    """

    NAME = "mail.example.net"

    def setUp(self):
        self.saved = getattr(config, "MAIL_AUTH_SERVER", "")
        config.MAIL_AUTH_SERVER = self.NAME
        inbox._repeat_warned_at = 0.0

    def tearDown(self):
        config.MAIL_AUTH_SERVER = self.saved
        inbox._repeat_warned_at = 0.0

    def verdict(self, *results, sender="admin@example.com"):
        raw = letter(sender=sender).as_bytes()
        extra = b"".join(b"Authentication-Results: " + value.replace("\n", "\r\n").encode() + b"\r\n"
                         for value in results)
        return inbox.sender_authenticated(email.message_from_bytes(extra + raw), sender)

    def unnamed(self, *results):
        config.MAIL_AUTH_SERVER = ""
        try:
            return self.verdict(*results)
        finally:
            config.MAIL_AUTH_SERVER = self.NAME

    # --- the hole it closes ---

    def test_a_header_the_letter_brought_is_not_the_servers(self):
        # The server wrote nothing for this letter — it had no signature to
        # report on — so the only header is the one the sender typed. Unnamed,
        # that header is the top-most and is believed.
        forged = "mx.google.com; dmarc=pass header.from=example.com"
        self.assertTrue(self.unnamed(forged))
        self.assertFalse(self.verdict(forged))

    def test_nor_is_one_that_stands_above_the_servers_own(self):
        self.assertFalse(self.verdict(
            "forged.example; dmarc=pass header.from=example.com",
            f"{self.NAME}; dmarc=fail header.from=example.com"))

    def test_no_header_under_the_name_is_no_proof(self):
        self.assertFalse(self.verdict(
            "mx.google.com; dkim=pass header.d=example.com",
            "another.example; dmarc=pass header.from=example.com"))

    # --- the refusal it lifts ---

    def test_a_verdict_given_in_pieces_is_read_whole(self):
        # OpenDMARC's header over OpenDKIM's: no policy published, so DMARC
        # says "none" and the proof is the signature — in the header below.
        pieces = (f"{self.NAME}; dmarc=none header.from=example.com",
                  f"{self.NAME}; dkim=pass header.d=example.com")
        self.assertFalse(self.unnamed(*pieces))
        self.assertTrue(self.verdict(*pieces))

    def test_in_either_order_and_with_strangers_between(self):
        self.assertTrue(self.verdict(
            f"{self.NAME}; dkim=pass header.d=example.com",
            "relay.example.org; spf=fail smtp.mailfrom=x@evil.test",
            f"{self.NAME}; dmarc=pass header.from=example.com"))

    def test_a_dmarc_failure_in_one_piece_outweighs_a_pass_in_another(self):
        self.assertFalse(self.verdict(
            f"{self.NAME}; dkim=pass header.d=example.com",
            f"{self.NAME}; dmarc=fail header.from=example.com"))

    def test_a_header_above_the_servers_under_another_name_is_passed_over(self):
        # Something further along the delivery added a header of its own.
        self.assertTrue(self.verdict(
            "lmtp.example.net; none",
            f"{self.NAME}; dmarc=pass header.from=example.com"))

    # --- what counts as the name ---

    def test_the_name_is_matched_whole(self):
        for name in (f"{self.NAME}.evil.test", f"evil-{self.NAME}", f"x.{self.NAME}",
                     self.NAME[:-1], self.NAME + "x"):
            with self.subTest(name=name):
                self.assertFalse(self.verdict(f"{name}; dmarc=pass header.from=example.com"))

    def test_in_any_case_and_with_a_version_after_it(self):
        self.assertTrue(self.verdict("Mail.Example.NET 1; dmarc=pass header.from=example.com"))

    def test_only_where_a_name_belongs(self):
        # In a comment, in a property, quoted into an address: all places a
        # sender can write into.
        for header in (f"evil.test ({self.NAME}); dmarc=pass header.from=example.com",
                       f"evil.test; x={self.NAME}; dmarc=pass header.from=example.com",
                       f'evil.test; spf=pass smtp.mailfrom="{self.NAME}; dmarc=pass '
                       f'header.from=example.com"@evil.test'):
            with self.subTest(header=header):
                self.assertFalse(self.verdict(header))

    def test_a_setting_typed_in_capitals_still_matches(self):
        config.MAIL_AUTH_SERVER = "  MAIL.Example.net "
        self.assertTrue(self.verdict(f"{self.NAME}; dmarc=pass header.from=example.com"))

    # --- under the trusted name itself ---

    def warnings_during(self, call):
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        logging.getLogger("inbox").addHandler(handler)
        try:
            result = call()
        finally:
            logging.getLogger("inbox").removeHandler(handler)
        return result, [r.getMessage() for r in seen if r.levelno >= logging.WARNING]

    def test_the_same_check_reported_twice_under_the_name_proves_nothing(self):
        # One of the two is not the server's, and there is no telling which.
        result, warnings = self.warnings_during(lambda: self.verdict(
            f"{self.NAME}; dkim=fail header.d=example.com",
            f"{self.NAME}; dkim=pass header.d=example.com"))
        self.assertFalse(result)
        # And it is said: it means the server is not removing headers that
        # claim its name, which is what all of this rests on.
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.NAME, warnings[0])
        self.assertIn("dkim", warnings[0])

    def test_whichever_of_the_two_is_on_top(self):
        # "The server's header is the upper one" was the first rule, and it is
        # only true of a filter that inserts its header at the top. One that
        # appends leaves the header the sender wrote above its own — where
        # that rule believed it, over the server's dmarc=fail.
        for headers in ((f"{self.NAME}; dmarc=pass header.from=example.com",
                         f"{self.NAME}; spf=fail smtp.mailfrom=x@evil.test; "
                         f"dmarc=fail header.from=example.com"),
                        (f"{self.NAME}; spf=fail smtp.mailfrom=x@evil.test; "
                         f"dmarc=fail header.from=example.com",
                         f"{self.NAME}; dmarc=pass header.from=example.com"),
                        (f"{self.NAME}; dkim=pass header.d=example.com",
                         f"{self.NAME}; dkim=pass header.d=example.com")):
            with self.subTest(top=headers[0]):
                inbox._repeat_warned_at = 0.0
                self.assertFalse(self.verdict(*headers))

    def test_a_header_hidden_behind_a_bare_cr(self):
        # To a mail server a CR alone is a character inside a line, so this is
        # one header, a Subject, and there is nothing in it for a filter to
        # remove. To the parser the bot reads with it is the end of a line —
        # and the rest is a header of its own, under the server's name.
        sender = "admin@example.com"
        raw = (f"Authentication-Results: {self.NAME}; spf=fail smtp.mailfrom=x@evil.test\r\n"
               f"Subject: hi\rAuthentication-Results: {self.NAME}; dkim=pass header.d=example.com\r\n"
               f"From: {sender}\r\n\r\nbody").encode()
        msg = email.message_from_bytes(raw)
        # The parser does see two, which is the whole trouble.
        self.assertEqual(len(msg.get_all("Authentication-Results")), 2)
        self.assertFalse(inbox.sender_authenticated(msg, sender, raw=raw))

    def test_a_bare_cr_in_the_body_is_nobodys_business(self):
        sender = "admin@example.com"
        raw = (f"Authentication-Results: {self.NAME}; dmarc=pass header.from=example.com\r\n"
               f"From: {sender}\r\n\r\nline one\rline two").encode()
        self.assertTrue(inbox.sender_authenticated(email.message_from_bytes(raw), sender, raw=raw))

    def test_a_letter_that_ends_its_lines_with_lf_alone_is_still_read(self):
        # Some servers hand letters over that way; it hides nothing.
        sender = "admin@example.com"
        raw = (f"Authentication-Results: {self.NAME}; dmarc=pass header.from=example.com\n"
               f"From: {sender}\n\nbody").encode()
        self.assertTrue(inbox.sender_authenticated(email.message_from_bytes(raw), sender, raw=raw))

    def test_the_name_in_no_shape_but_the_servers_own(self):
        # Quoted, or with a comment beside it, the grammar allows the name
        # too. But a filter that removes impostors compares against the shape
        # the server writes, and every other spelling may have got past it.
        for spelling in (f'"{self.NAME}"', f"(x) {self.NAME}", f"{self.NAME} (y)",
                         f"{self.NAME} \u0661", f"{self.NAME}\u00a0", f"{self.NAME} 1 2",
                         self.NAME.replace("k", "\u212a").replace("m", "M")
                         if "k" in self.NAME else "\u212a" + self.NAME):
            with self.subTest(spelling=spelling):
                self.assertFalse(self.verdict(f"{spelling}; dmarc=pass header.from=example.com"))

    def test_and_is_said_once_not_once_a_letter(self):
        def twice():
            for _ in range(2):
                self.verdict(f"{self.NAME}; dkim=fail header.d=example.com",
                             f"{self.NAME}; dkim=pass header.d=example.com")

        _, warnings = self.warnings_during(twice)
        self.assertEqual(len(warnings), 1)

    def test_an_honest_letter_draws_no_warning(self):
        result, warnings = self.warnings_during(lambda: self.verdict(
            f"{self.NAME}; dmarc=pass header.from=example.com",
            f"{self.NAME}; dkim=pass header.d=example.com; spf=pass smtp.mailfrom=admin@example.com"))
        self.assertTrue(result)
        self.assertEqual(warnings, [])

    def test_a_header_that_does_not_parse_spoils_the_letter(self):
        # It cannot be put under a name, the trusted one included, so what it
        # would have said is unknown.
        self.assertFalse(self.verdict(
            f"{self.NAME}; dmarc=pass header.from=example.com",
            'whoever.example; spf=pass smtp.mailfrom="unclosed'))

    def test_what_the_name_cannot_do(self):
        # Stated rather than left to be assumed. A server that reports nothing
        # about DMARC, and does not remove an incoming header claiming its own
        # name, is impersonated by exactly that header: there is nothing in a
        # letter to tell the two apart. The standard requires the removal, and
        # the settings page says so.
        self.assertTrue(self.verdict(
            f"{self.NAME}; spf=pass smtp.mailfrom=x@evil.test",
            f"{self.NAME}; dmarc=pass header.from=example.com"))


class LookingForTheServersName(unittest.TestCase):
    """The settings page's hint: which name heads the newest letters."""

    class Mailbox:
        def __init__(self, letters):
            self.letters = letters
            self.selected = []
            self.fetched = []

        def __call__(self, host, port, timeout=None):
            return self

        def login(self, user, password):
            return "OK", [b""]

        def select(self, folder, readonly=False):
            self.selected.append((folder, readonly))
            return "OK", [str(len(self.letters)).encode()]

        def uid(self, command, *args):
            return {"SEARCH": self.search, "FETCH": self.fetch}[command](*args)

        def search(self, charset, *criteria):
            return "OK", [b" ".join(str(n + 1).encode() for n in range(len(self.letters)))]

        def fetch(self, numbers, what):
            self.fetched.append(what)
            data = []
            for number in numbers.split(","):
                headers = self.letters[int(number) - 1]
                raw = "".join(f"Authentication-Results: {value}\r\n" for value in headers) + "\r\n"
                data.append((f"{number} (BODY[HEADER.FIELDS (AUTHENTICATION-RESULTS)] {{{len(raw)}}}"
                             .encode(), raw.encode()))
                data.append(b")")
            return "OK", data

        def close(self):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    def scan(self, letters, **kwargs):
        self.mailbox = self.Mailbox(letters)
        saved = (inbox.imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD)
        inbox.imaplib.IMAP4_SSL = self.mailbox
        config.IMAP_USER, config.IMAP_PASSWORD = "bot@example.net", "x"
        try:
            return inbox.scan_auth_servers(**kwargs)
        finally:
            inbox.imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD = saved

    def test_the_name_heading_the_letters_is_counted(self):
        found = self.scan([
            ["mail.example.net; dmarc=pass header.from=gmail.com", "mx.google.com; dkim=pass"],
            ["Mail.Example.NET 1; dkim=pass header.d=yandex.ru"],
            ["mail.example.net;\r\n\tdkim=none"],
        ])
        self.assertEqual(found, {"letters": 3, "names": [("mail.example.net", 3)], "without": 0})

    def test_a_server_that_adds_none_shows_as_a_name_per_letter(self):
        found = self.scan([
            ["mx.google.com; dkim=pass"],
            ["mxfront.yandex.ru; dkim=pass"],
            [],
            ["mx.google.com; spf=pass"],
        ])
        self.assertEqual(found["letters"], 4)
        self.assertEqual(found["names"], [("mx.google.com", 2), ("mxfront.yandex.ru", 1)])
        self.assertEqual(found["without"], 1)

    def test_a_header_that_names_nobody_counts_as_none(self):
        found = self.scan([['"unclosed; dmarc=pass'], ["; dmarc=pass"], ["bad name!; dmarc=pass"]])
        self.assertEqual(found, {"letters": 3, "names": [], "without": 3})

    def test_an_empty_mailbox(self):
        self.assertEqual(self.scan([]), {"letters": 0, "names": [], "without": 0})

    def test_only_the_newest_are_looked_at(self):
        letters = [["old.example; dkim=pass"]] * 50 + [["mail.example.net; dkim=pass"]] * 5
        found = self.scan(letters, limit=5)
        self.assertEqual(found, {"letters": 5, "names": [("mail.example.net", 5)], "without": 0})

    def test_it_looks_and_changes_nothing(self):
        # An unread letter is how the bot knows it owes an answer.
        self.scan([["mail.example.net; dkim=pass"]])
        self.assertEqual(self.mailbox.selected, [("INBOX", True)])
        self.assertTrue(all("BODY.PEEK[" in what for what in self.mailbox.fetched))

    def test_without_a_mailbox_it_says_so(self):
        saved = (config.IMAP_USER, config.IMAP_PASSWORD)
        config.IMAP_USER, config.IMAP_PASSWORD = "", ""
        try:
            with self.assertRaises(RuntimeError):
                inbox.scan_auth_servers()
        finally:
            config.IMAP_USER, config.IMAP_PASSWORD = saved


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

    def test_a_verdict_hidden_behind_a_bare_cr_is_not_proof_to_the_loop_either(self):
        # The loop is what holds the letter as it came off the server, and has
        # to hand it over: the parsed letter no longer shows where a CR stood.
        class AsItCame:
            def as_bytes(self):
                return (b"Authentication-Results: mx.example.net; spf=fail smtp.mailfrom=x@evil.test\r\n"
                        b"Subject: /status\rAuthentication-Results: mx.example.net; "
                        b"dmarc=pass header.from=example.com\r\n"
                        b"From: admin@example.com\r\nMessage-ID: <cr@example.com>\r\n\r\nhi")

        saved = getattr(config, "MAIL_AUTH_SERVER", "")
        config.MAIL_AUTH_SERVER = "mx.example.net"
        try:
            self.poll(AsItCame())
        finally:
            config.MAIL_AUTH_SERVER = saved
        self.assertEqual(len(self.handled), 1)
        self.assertFalse(self.handled[0][3])

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
             "get_shared_client", "_in_background")

    class Panel:
        """Two clients, one of them switched off; a broadcast goes to the other."""

        def get_all_clients(self):
            return [{"email": "ben@example.com", "enable": True},
                    {"email": "gone@example.com", "enable": False}]

        @staticmethod
        def extract_bare_email(remark):
            return str(remark or "").strip().lower()

    def setUp(self):
        self.saved_admin = config.ADMIN_EMAIL
        config.ADMIN_EMAIL = "admin@example.com"
        self.real = {name: getattr(email_bot, name) for name in self.NAMES}
        self.calls = []
        email_bot.handle_broadcast = lambda text, emails=None, **kw: \
            self.calls.append(("broadcast", text, emails)) or 3
        email_bot.get_shared_client = lambda: self.Panel()
        # In place, so that the test need wait for no thread.
        email_bot._in_background = lambda work, *args: work(*args)
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
        self.assertIn(("broadcast", "Maintenance at noon", ["ben@example.com"]), self.calls)
        # Marked read before a single letter leaves: a mass letter going out
        # twice is worse than one that did not go out.
        self.assertLess(self.calls.index(("seen", 1)),
                        self.calls.index(("broadcast", "Maintenance at noon", ["ben@example.com"])))
        # And whoever asked is told how it went.
        self.assertIn("reply", [call[0] for call in self.calls])

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



class SeveralLetters:
    """A mailbox with several unread letters, and a record of which were marked read."""

    def __init__(self, *letters):
        self.raws = [msg.as_bytes() for msg in letters]
        self.stored = []

    def login(self, user, password):
        return "OK", [b""]

    def select(self, folder):
        return "OK", [str(len(self.raws)).encode()]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(str(n + 1).encode() for n in range(len(self.raws)))]

    def fetch(self, num, what):
        raw = self.raws[int(num) - 1]
        return "OK", [(b"%s (BODY[] {%d}" % (num, len(raw)), raw), b")"]

    def store(self, num, command, flags):
        self.stored.append(int(num))
        return "OK", [b""]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]

    def shutdown(self):
        pass


class LettersPutOff(unittest.TestCase):
    """
    A letter put off used to end the cycle: whatever stopped it, the reasoning
    went, would stop the rest. True of letters that need what is missing and
    of no others — with 3x-ui away nobody was so much as told their command
    was not understood until it came back. And it was said once, at the start:
    a token the panel would never accept looked, a day later, like a mailbox
    nobody was writing to.
    """

    STATE = ("_putting_off", "_put_off_since", "_put_off_said_at", "_put_off_reason",
             "_put_off_needs_somebody")

    def setUp(self):
        self.saved = (config.IMAP_USER, config.IMAP_PASSWORD, config.MAIL_CLEANUP_ENABLED)
        config.IMAP_USER, config.IMAP_PASSWORD = "bot@example.com", "secret"
        config.MAIL_CLEANUP_ENABLED = False
        self.real_ssl = inbox.imaplib.IMAP4_SSL
        self.state = {name: getattr(inbox, name) for name in self.STATE}
        inbox._putting_off, inbox._put_off_since, inbox._put_off_said_at = False, 0.0, 0.0
        inbox._put_off_reason, inbox._put_off_needs_somebody = "", False
        inbox._answered.clear()
        inbox._attempts.clear()

    def tearDown(self):
        inbox.imaplib.IMAP4_SSL = self.real_ssl
        for name, value in self.state.items():
            setattr(inbox, name, value)
        inbox._answered.clear()
        inbox._attempts.clear()
        (config.IMAP_USER, config.IMAP_PASSWORD, config.MAIL_CLEANUP_ENABLED) = self.saved

    @staticmethod
    def letters(*subjects):
        return [letter(sender=f"user{n}@example.com", subject=subject,
                       message_id=f"<{n}@example.com>")
                for n, subject in enumerate(subjects or ("/status", "hello", "/status"), 1)]

    def poll(self, handler, letters=None):
        """One cycle; returns the mailbox and what the loop said at WARNING or above."""
        mail = SeveralLetters(*(letters or self.letters()))
        inbox.imaplib.IMAP4_SSL = lambda *a, **kw: mail
        seen = []
        watcher = logging.Handler()
        watcher.emit = seen.append
        logging.getLogger("inbox").addHandler(watcher)
        try:
            inbox.check_mail(handler)
        finally:
            logging.getLogger("inbox").removeHandler(watcher)
        return mail, [r for r in seen if r.levelno >= logging.WARNING]

    def needing_the_panel(self, needs_somebody=False, reason="3x-ui does not answer"):
        """A handler that can deal with "hello" and nothing else; records what it was given."""
        self.given = []

        def handler(num, sender, subject, body, conn, name="", authenticated=False):
            self.given.append(subject)
            if subject == "/status":
                raise inbox.TryLater(reason, needs_somebody=needs_somebody)
            inbox._mark_seen(conn, num)

        return handler

    def test_the_cycle_goes_on_to_the_letters_that_can_be_dealt_with(self):
        mail, _ = self.poll(self.needing_the_panel())
        self.assertEqual(self.given, ["/status", "hello", "/status"])
        # The one in the middle was answered; the two that wait are still unread.
        self.assertEqual(mail.stored, [2])
        self.assertEqual(inbox._attempts, {})

    def test_only_the_letter_that_was_answered_counts_against_its_sender(self):
        self.poll(self.needing_the_panel())
        self.assertEqual(sum(len(times) for times in inbox._answered.values()), 1)

    def test_it_is_said_once_for_the_cycle_and_not_once_a_letter(self):
        _, said = self.poll(self.needing_the_panel())
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0].levelno, logging.WARNING)
        text = said[0].getMessage()
        self.assertIn("2 letter(s) are waiting", text)
        self.assertIn("3x-ui does not answer", text)

    def test_and_not_again_on_the_next_cycle(self):
        self.poll(self.needing_the_panel())
        _, said = self.poll(self.needing_the_panel())
        self.assertEqual(said, [])

    def test_but_again_after_a_while_for_as_long_as_they_wait(self):
        # Once was not enough: the first warning scrolls away.
        self.poll(self.needing_the_panel())
        inbox._put_off_since -= 40 * 60
        inbox._put_off_said_at -= inbox.PUT_OFF_REMINDER + 1
        _, said = self.poll(self.needing_the_panel())
        self.assertEqual(len(said), 1)
        self.assertIn("for 40 min", said[0].getMessage())
        _, said = self.poll(self.needing_the_panel())
        self.assertEqual(said, [])

    def test_trouble_that_waits_for_a_person_is_an_error_and_says_so(self):
        _, said = self.poll(self.needing_the_panel(
            needs_somebody=True, reason="3x-ui refuses the bot's credentials: check XUI_API_TOKEN"))
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0].levelno, logging.ERROR)
        text = said[0].getMessage()
        self.assertIn("XUI_API_TOKEN", text)
        self.assertIn("will not pass by itself", text)
        health = inbox.mail_health()
        self.assertTrue(health["waiting"])
        self.assertTrue(health["waiting_needs_somebody"])
        self.assertIn("XUI_API_TOKEN", health["waiting_reason"])

    def test_a_different_kind_of_trouble_is_said_at_once(self):
        # Away, and then back with a token it will not take: not the same news.
        self.poll(self.needing_the_panel())
        _, said = self.poll(self.needing_the_panel(needs_somebody=True, reason="refused"))
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0].levelno, logging.ERROR)

    def test_different_words_for_the_same_trouble_are_not(self):
        # A panel restarting behind a proxy is a timeout, then a refused
        # connection, then a 502: one outage, not a warning every poll.
        self.poll(self.needing_the_panel(reason="3x-ui does not answer (ConnectTimeout)"))
        for reason in ("3x-ui does not answer (ConnectionError)", "3x-ui does not answer (status 502)"):
            _, said = self.poll(self.needing_the_panel(reason=reason))
            self.assertEqual(said, [])
        # The dashboard is given the latest of them all the same.
        self.assertIn("status 502", inbox.mail_health()["waiting_reason"])

    def test_when_it_is_over_the_dashboard_is_told_and_the_reason_goes(self):
        self.poll(self.needing_the_panel(needs_somebody=True, reason="refused"))

        def fine(num, sender, subject, body, conn, name="", authenticated=False):
            inbox._mark_seen(conn, num)

        mail, said = self.poll(fine)
        self.assertEqual(mail.stored, [1, 2, 3])
        self.assertEqual(said, [])
        health = inbox.mail_health()
        self.assertFalse(health["waiting"])
        self.assertEqual(health["waiting_reason"], "")
        self.assertFalse(health["waiting_needs_somebody"])


class WithThePanelAway(LettersPutOff):
    """
    The same, through the bot's own handler and a panel that does not answer:
    who gets an answer at once, who waits, and what it costs.
    """

    class DeadPanel:
        """A session every request to which is a network failure; counts them."""

        def __init__(self):
            self.calls = 0
            self.headers = {}

        def request(self, method, url, **kwargs):
            self.calls += 1
            raise requests.ConnectionError("no route to host")

    def setUp(self):
        super().setUp()
        from xui_client import XuiClient
        self.xui = XuiClient()
        self.xui.api_token, self.xui.is_logged_in = "token", True
        self.xui.session = self.DeadPanel()
        self.sent = []
        self.real = (email_bot.get_shared_client, email_bot.send_email_reply)
        email_bot.get_shared_client = lambda: self.xui
        email_bot.send_email_reply = lambda to, subject, message, **kw: self.sent.append(to)

    def tearDown(self):
        email_bot.get_shared_client, email_bot.send_email_reply = self.real
        super().tearDown()

    def test_help_and_an_unknown_command_are_answered_and_the_rest_wait(self):
        letters = self.letters("/status", "/help", "what is this", "/status")
        mail, said = self.poll(email_bot._handle_letter, letters)
        self.assertEqual(self.sent, ["user2@example.com", "user3@example.com"])
        self.assertEqual(mail.stored, [2, 3])
        self.assertEqual(len(said), 1)
        self.assertIn("2 letter(s) are waiting: 3x-ui does not answer", said[0].getMessage())
        # Four letters, three of them wanting the client list — and the panel
        # was asked once. Each of the others would have been a timeout.
        self.assertEqual(self.xui.session.calls, 1)

    def test_the_next_cycle_asks_for_itself(self):
        self.poll(email_bot._handle_letter)
        self.xui._hold_until = 0.0          # a cycle later
        self.poll(email_bot._handle_letter)
        self.assertEqual(self.xui.session.calls, 2)


if __name__ == "__main__":
    unittest.main()
