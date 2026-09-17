"""
The mail pages' reading of the mailbox, and the copies that fill the Sent folder.

Most of what is asserted here is about leaving things alone. The bot takes an
unread letter as one it still owes an answer, so the panel opening a letter must
not change a single flag — every test that touches the fake server checks that
it was only ever examined, and only ever peeked at.
"""
import os
import sys
import time
import unittest
from email.message import EmailMessage
from email.utils import formataddr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import imaplib

import config
import inbox
import mailer
import mailfolders
from fake_imap import FakeMailbox

BOT = "bot@example.com"


def letter(frm, subject, text="", html=None, to=BOT, attachments=(), images=(), message_id=None):
    msg = EmailMessage()
    msg["From"] = frm
    if to is not None:
        msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Wed, 16 Sep 2026 10:00:00 +0000"
    msg["Message-ID"] = message_id or f"<{abs(hash((frm, subject)))}@example.com>"
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
        html_part = msg.get_payload()[1]
        for cid, data in images:
            html_part.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return bytes(msg)


class WithMailbox(unittest.TestCase):
    """A fake server behind imaplib, and the mailbox credentials set."""

    def setUp(self):
        self.box = FakeMailbox()
        self.saved = (imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER)
        imaplib.IMAP4_SSL = self.box.connect
        config.IMAP_USER = BOT
        config.IMAP_PASSWORD = "secret"
        config.SMTP_USER = BOT

    def tearDown(self):
        imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER = self.saved
        # The rule every test here is also about.
        self.assertEqual(self.box.writable_selects, [], "a folder was opened for writing")
        self.assertEqual(self.box.unpeeked_fetches, [], "a body was fetched without PEEK")


class Accounts(unittest.TestCase):
    """The bot's own mailbox and Support are read from config at call time,
    through the same Account shape, distinguished only by their key prefix."""

    def setUp(self):
        self.saved = (config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER,
                     config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD, config.SUPPORT_SMTP_USER)

    def tearDown(self):
        (config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER,
         config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD, config.SUPPORT_SMTP_USER) = self.saved

    def test_a_setting_saved_from_the_panel_is_seen_at_once(self):
        # No caching: Account reads config attributes live, so a value changed
        # mid-process (exactly what settings.save() does) is picked up by the
        # very next call — no restart, matching every other setting.
        config.SUPPORT_IMAP_USER = "one@example.com"
        self.assertEqual(mailfolders.SUPPORT.imap_user, "one@example.com")
        config.SUPPORT_IMAP_USER = "two@example.com"
        self.assertEqual(mailfolders.SUPPORT.imap_user, "two@example.com")

    def test_configured_needs_both_imap_fields(self):
        config.SUPPORT_IMAP_USER = ""
        config.SUPPORT_IMAP_PASSWORD = ""
        self.assertFalse(mailfolders.SUPPORT.configured)
        config.SUPPORT_IMAP_USER = "s@example.com"
        self.assertFalse(mailfolders.SUPPORT.configured)
        config.SUPPORT_IMAP_PASSWORD = "secret"
        self.assertTrue(mailfolders.SUPPORT.configured)

    def test_own_addresses_reads_its_own_prefix_only(self):
        config.IMAP_USER, config.SMTP_USER = "bot@example.com", "bot-smtp@example.com"
        config.SUPPORT_IMAP_USER, config.SUPPORT_SMTP_USER = "s@example.com", "s-smtp@example.com"
        self.assertEqual(mailfolders.BOT.own_addresses(), {"bot@example.com", "bot-smtp@example.com"})
        self.assertEqual(mailfolders.SUPPORT.own_addresses(), {"s@example.com", "s-smtp@example.com"})

    def test_the_bot_account_is_always_available_support_only_once_set_up(self):
        config.SUPPORT_IMAP_USER = ""
        config.SUPPORT_IMAP_PASSWORD = ""
        self.assertEqual([a.key for a in mailfolders.accounts_available()], ["bot"])
        config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD = "s@example.com", "secret"
        self.assertEqual([a.key for a in mailfolders.accounts_available()], ["bot", "support"])


class ReplyBuilding(unittest.TestCase):
    def test_re_is_added_once(self):
        self.assertEqual(mailfolders.reply_subject("Help"), "Re: Help")
        self.assertEqual(mailfolders.reply_subject("Re: Help"), "Re: Help")
        self.assertEqual(mailfolders.reply_subject("RE:Help"), "RE:Help")
        self.assertEqual(mailfolders.reply_subject(""), "Re:")

    def test_the_quote_carries_the_sender_and_the_original_text(self):
        parsed = mailfolders.parse_letter(letter(
            formataddr(("Vera", "vera@example.com")), "Не работает", "Что делать?"))
        quoted = mailfolders.quote_body(parsed)
        self.assertIn("Vera", quoted)
        self.assertIn(parsed["when_full"], quoted)
        self.assertIn("> Что делать?", quoted)

    def test_a_letter_with_no_sender_is_quoted_without_one(self):
        quoted = mailfolders.quote_body({"from": [], "text": "hi", "when_full": ""})
        self.assertIn("hi", quoted)  # does not raise, names nobody in particular


class SendingAReply(WithMailbox):
    def setUp(self):
        super().setUp()
        self.saved_support = (config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD, config.SUPPORT_SMTP_USER)

    def tearDown(self):
        (config.SUPPORT_IMAP_USER, config.SUPPORT_IMAP_PASSWORD,
         config.SUPPORT_SMTP_USER) = self.saved_support
        super().tearDown()

    def test_a_reply_is_threaded_quoted_and_filed_as_a_sent_copy(self):
        config.SUPPORT_IMAP_USER = BOT
        config.SUPPORT_IMAP_PASSWORD = "secret"
        config.SUPPORT_SMTP_USER = BOT
        original = mailfolders.parse_letter(letter(
            "vera@example.com", "Не работает", "Что делать?", message_id="<orig@example.com>"))

        sent = {}

        def fake_send_via(server_host, port, user, password, to_email, subject, message,
                          service_name=None, extra_headers=None):
            msg = mailer.build_message(to_email, subject, message, smtp_user=user,
                                       service_name=service_name, extra_headers=extra_headers)
            sent["msg"] = msg
            sent["extra_headers"] = extra_headers
            return msg

        saved_send = mailer.send_email_via
        saved_copy = mailfolders.save_sent_copy
        queued = []
        mailer.send_email_via = fake_send_via
        mailfolders.save_sent_copy = lambda account_key, message_id, raw: queued.append(
            (account_key, message_id, raw))
        try:
            mailfolders.send_reply(mailfolders.SUPPORT, original, "vera@example.com", "Проверьте кабель.")
        finally:
            mailer.send_email_via = saved_send
            mailfolders.save_sent_copy = saved_copy

        self.assertEqual(sent["extra_headers"]["In-Reply-To"], "<orig@example.com>")
        self.assertEqual(sent["extra_headers"]["References"], "<orig@example.com>")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], "support")

    def test_refuses_without_credentials(self):
        config.SUPPORT_IMAP_USER = ""
        config.SUPPORT_IMAP_PASSWORD = ""
        original = mailfolders.parse_letter(letter("vera@example.com", "hi", "text"))
        with self.assertRaises(mailfolders.MailboxNotSetUp):
            mailfolders.send_reply(mailfolders.SUPPORT, original, "vera@example.com", "text")


class FolderNames(unittest.TestCase):
    def test_modified_utf7_comes_back_as_text(self):
        self.assertEqual(inbox.decode_folder_name("&BBoEPgRABDcEOAQ9BDA-"), "Корзина")
        self.assertEqual(inbox.decode_folder_name("INBOX.&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"),
                         "INBOX.Отправленные")
        self.assertEqual(inbox.decode_folder_name("Tom &- Jerry"), "Tom & Jerry")

    def test_a_cyrillic_name_is_found_without_the_attribute(self):
        # The fallback list has always carried Корзина; it could never match
        # while the comparison was against the encoded form.
        class Server:
            def list(self):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                              b'(\\HasNoChildren) "/" "&BBoEPgRABDcEOAQ9BDA-"']
        self.assertEqual(inbox.trash_folder(Server()), "&BBoEPgRABDcEOAQ9BDA-")

    def test_the_sent_folder_by_its_attribute(self):
        class Server:
            def list(self):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                              b'(\\HasNoChildren \\Sent) "/" "Outbox of mine"']
        self.assertEqual(inbox.sent_folder(Server()), "Outbox of mine")

    def test_the_attribute_is_not_looked_for_in_the_name(self):
        class Server:
            def list(self):
                return "OK", [b'(\\HasNoChildren) "/" "\\\\Sent notes"']
        self.assertEqual(inbox.sent_folder(Server()), "")


class TheList(WithMailbox):
    def test_newest_first_with_the_flags_as_they_are(self):
        self.box.add("INBOX", letter("ann@example.com", "first"), flags=["\\Seen"])
        self.box.add("INBOX", letter("ben@example.com", "second"))
        listing = mailfolders.list_letters(mailfolders.BOT, "inbox")
        self.assertEqual([r["subject"] for r in listing["rows"]], ["second", "first"])
        self.assertEqual([r["seen"] for r in listing["rows"]], [False, True])
        # And the unread one is still unread on the server.
        self.assertNotIn("\\Seen", self.box.folders["INBOX"][1][1].flags)

    def test_the_other_side_of_a_letter_we_sent_is_its_recipient(self):
        self.box.add("Trash", letter(formataddr(("Mailchilla", BOT)), "welcome", to="ann@example.com"))
        self.box.add("Trash", letter("ben@example.com", "hello"))
        rows = mailfolders.list_letters(mailfolders.BOT, "trash")["rows"]
        self.assertEqual((rows[0]["person"]["address"], rows[0]["outgoing"]), ("ben@example.com", False))
        self.assertEqual((rows[1]["person"]["address"], rows[1]["outgoing"]), ("ann@example.com", True))

    def test_a_letter_from_us_with_no_recipient_is_still_outgoing(self):
        # A malformed or truncated stored copy: sent by us, but nothing to read
        # a recipient back out of. It must not read as an unanswered letter
        # from ourselves, which is what "outgoing" decides in the template.
        self.box.add("Trash", letter(BOT, "no To header", to=None))
        row = mailfolders.list_letters(mailfolders.BOT, "trash")["rows"][0]
        self.assertTrue(row["outgoing"])
        self.assertEqual(row["person"]["address"], "")

    def test_a_letter_with_an_attachment_is_marked(self):
        self.box.add("INBOX", letter("ann@example.com", "files", attachments=[("a.pdf", b"%PDF")]))
        self.box.add("INBOX", letter("ann@example.com", "plain"))
        rows = mailfolders.list_letters(mailfolders.BOT, "inbox")["rows"]
        self.assertEqual([r["attachment"] for r in rows], [False, True])

    def test_pages(self):
        for n in range(7):
            self.box.add("INBOX", letter(f"p{n}@example.com", f"letter {n}"))
        listing = mailfolders.list_letters(mailfolders.BOT, "inbox", page=2, per_page=3)
        self.assertEqual((listing["total"], listing["pages"], listing["page"]), (7, 3, 2))
        self.assertEqual([r["subject"] for r in listing["rows"]], ["letter 3", "letter 2", "letter 1"])
        # A page past the end is the last page, not an empty one.
        self.assertEqual(mailfolders.list_letters(mailfolders.BOT, "inbox", page=9, per_page=3)["page"], 3)

    def test_search_by_address_and_by_a_cyrillic_word(self):
        self.box.add("INBOX", letter("ann@example.com", "Подписка не работает"))
        self.box.add("INBOX", letter("ben@example.com", "hello"))
        found = mailfolders.list_letters(mailfolders.BOT, "inbox", query="ann@")["rows"]
        self.assertEqual([r["person"]["address"] for r in found], ["ann@example.com"])
        found = mailfolders.list_letters(mailfolders.BOT, "inbox", query="подписка")["rows"]
        self.assertEqual([r["person"]["address"] for r in found], ["ann@example.com"])

    def test_no_such_folder(self):
        del self.box.folders["Trash"]
        with self.assertRaises(mailfolders.FolderMissing):
            mailfolders.list_letters(mailfolders.BOT, "trash")

    def test_no_mailbox_at_all(self):
        config.IMAP_USER = ""
        with self.assertRaises(mailfolders.MailboxNotSetUp):
            mailfolders.list_letters(mailfolders.BOT, "inbox")


class OneLetter(WithMailbox):
    def test_opening_it_leaves_it_unread(self):
        uid = self.box.add("INBOX", letter("ann@example.com", "START"))
        raw, flags = mailfolders.fetch_raw(mailfolders.BOT, "inbox", str(uid))
        self.assertIn(b"START", raw)
        self.assertNotIn("\\Seen", flags)
        self.assertEqual(self.box.folders["INBOX"][1][0].flags, set())

    def test_an_unknown_uid(self):
        self.box.add("INBOX", letter("ann@example.com", "START"))
        self.assertIsNone(mailfolders.fetch_raw(mailfolders.BOT, "inbox", "99"))
        self.assertIsNone(mailfolders.fetch_raw(mailfolders.BOT, "inbox", "1 OR 2"))


class Parsing(unittest.TestCase):
    def test_both_parts_are_kept(self):
        parsed = mailfolders.parse_letter(letter("ann@example.com", "hi", "plain words", html="<p>rich</p>"))
        self.assertEqual(parsed["text"].strip(), "plain words")
        self.assertIn("<p>rich</p>", parsed["html"])

    def test_an_html_only_letter_still_has_text(self):
        msg = EmailMessage()
        msg["From"] = "ann@example.com"
        msg.set_content("<div>Hello<br>there</div><style>p{}</style>", subtype="html")
        parsed = mailfolders.parse_letter(bytes(msg))
        self.assertEqual(parsed["text"], "Hello\nthere")

    def test_the_policy_comes_before_the_letter(self):
        parsed = mailfolders.parse_letter(letter(
            "ann@example.com", "hi", html='<!DOCTYPE html><html><body>x</body></html>'))
        head = parsed["html"]
        self.assertTrue(head.startswith("<!DOCTYPE html>"))
        self.assertLess(head.index("Content-Security-Policy"), head.index("<html>"))
        self.assertIn("img-src data:;", head)

    def test_remote_pictures_are_noticed_and_let_through_only_when_asked(self):
        raw = letter("ann@example.com", "hi", html='<img src="https://track.example/p.gif">')
        hidden = mailfolders.parse_letter(raw)
        self.assertTrue(hidden["has_remote"])
        self.assertFalse(hidden["remote_shown"])
        self.assertIn("img-src data:;", hidden["html"])
        shown = mailfolders.parse_letter(raw, remote_images=True)
        self.assertTrue(shown["remote_shown"])
        self.assertIn("img-src data: https: http:", shown["html"])

    def test_a_picture_inside_the_letter_is_drawn_from_data(self):
        raw = letter("ann@example.com", "hi", html='<img src="cid:qr">', images=[("qr", b"\x89PNG")])
        parsed = mailfolders.parse_letter(raw)
        self.assertIn('src="data:image/png;base64,iVBORw=="', parsed["html"])
        self.assertEqual(parsed["attachments"], [])

    def test_a_picture_over_the_budget_is_offered_as_a_download_instead_of_lost(self):
        raw = letter("ann@example.com", "hi", html='<img src="cid:big">',
                     images=[("big", b"\x89PNG" * 1000)])
        saved = mailfolders.INLINE_IMAGES_LIMIT
        mailfolders.INLINE_IMAGES_LIMIT = 100  # smaller than the picture
        try:
            parsed = mailfolders.parse_letter(raw)
        finally:
            mailfolders.INLINE_IMAGES_LIMIT = saved
        # Not drawn inline (the budget said no)...
        self.assertNotIn("base64,", parsed["html"])
        # ...but not gone either: still one click away.
        self.assertEqual(len(parsed["attachments"]), 1)
        self.assertEqual(parsed["attachments"][0]["name"], "big.png")
        self.assertEqual(parsed["attachments"][0]["type"], "image/png")

    def test_less_common_ways_to_reach_out_are_noticed_too(self):
        for markup in ('<svg><image xlink:href="https://track.example/p.png"/></svg>',
                       '<style>@import "https://track.example/f.css";</style>'):
            with self.subTest(markup=markup):
                self.assertTrue(mailfolders.has_remote_content(markup))

    def test_a_refresh_and_a_base_are_cut_out(self):
        raw = letter("ann@example.com", "hi", html=(
            '<meta http-equiv="refresh" content="0;url=https://evil.example">'
            '<base href="https://evil.example/" target="_self"><a href="x">x</a>'))
        html = mailfolders.parse_letter(raw)["html"]
        self.assertNotIn("refresh", html)
        self.assertNotIn("evil.example", html)
        self.assertEqual(html.count("<base"), 1)

    def test_attachments_are_listed_and_can_be_taken_back_out(self):
        raw = letter("ann@example.com", "hi", "see attached",
                     attachments=[("отчёт.pdf", b"%PDF-1.4 body")])
        parsed = mailfolders.parse_letter(raw)
        self.assertEqual([a["name"] for a in parsed["attachments"]], ["отчёт.pdf"])
        name, ctype, payload = mailfolders.attachment_from(raw, parsed["attachments"][0]["index"])
        self.assertEqual((name, payload), ("отчёт.pdf", b"%PDF-1.4 body"))
        self.assertIsNone(mailfolders.attachment_from(raw, 42))

    def test_a_name_with_a_comma_stays_one_person(self):
        raw = letter(formataddr(("Doe, John", "john@example.com"), "utf-8"), "hi")
        self.assertEqual(mailfolders.parse_letter(raw)["from"],
                         [{"name": "Doe, John", "address": "john@example.com"}])
        raw = letter(formataddr(("Иванов, Иван", "ivan@example.com"), "utf-8"), "hi")
        self.assertEqual(mailfolders.parse_letter(raw)["from"],
                         [{"name": "Иванов, Иван", "address": "ivan@example.com"}])

    def test_unencoded_eight_bit_headers_do_not_break_the_page(self):
        raw = ("From: Аня <ann@example.com>\r\nSubject: Привет\r\n\r\nbody").encode("utf-8")
        parsed = mailfolders.parse_letter(raw)
        parsed["subject"].encode("utf-8")
        parsed["from"][0]["name"].encode("utf-8")
        self.assertEqual(parsed["from"][0]["address"], "ann@example.com")


class SentCopies(WithMailbox):
    def test_a_letter_goes_into_the_sent_folder_read(self):
        raw = letter(BOT, "welcome", to="ann@example.com", message_id="<one@example.com>")
        self.assertEqual(mailfolders.append_copies(mailfolders.BOT, [("<one@example.com>", raw)]), 1)
        stored = self.box.folders["Sent"][1]
        self.assertEqual(len(stored), 1)
        self.assertIn("\\Seen", stored[0].flags)

    def test_one_the_provider_already_filed_is_not_filed_again(self):
        raw = letter(BOT, "welcome", to="ann@example.com", message_id="<one@example.com>")
        self.box.add("Sent", raw, flags=["\\Seen"])
        self.assertEqual(mailfolders.append_copies(mailfolders.BOT, [("<one@example.com>", raw)]), 0)
        self.assertEqual(len(self.box.folders["Sent"][1]), 1)

    def test_no_sent_folder_means_no_copy_and_no_error(self):
        del self.box.folders["Sent"]
        raw = letter(BOT, "welcome", to="ann@example.com")
        self.assertEqual(mailfolders.append_copies(mailfolders.BOT, [("<x@example.com>", raw)]), 0)
        self.assertEqual(self.box.appended, [])

    def test_nothing_is_queued_without_a_mailbox(self):
        config.IMAP_USER = ""
        before = mailfolders._copies.qsize()
        mailfolders.save_sent_copy("bot", "<x@example.com>", b"letter")
        self.assertEqual(mailfolders._copies.qsize(), before)

    def test_a_select_failure_still_appends_and_says_why_it_could_not_check(self):
        # "could not tell if it's a duplicate" must not silently become
        # "it isn't" with nothing in the log to explain the gap.
        self.box.fail_select.add("Sent")
        raw = letter(BOT, "welcome", to="ann@example.com", message_id="<x@example.com>")
        with self.assertLogs("mailfolders", level="WARNING") as logs:
            appended = mailfolders.append_copies(mailfolders.BOT, [("<x@example.com>", raw)])
        self.assertEqual(appended, 1)
        self.assertTrue(any("duplicate" in m for m in logs.output))

    def test_a_search_failure_still_appends_that_one_letter(self):
        self.box.fail_search.add("Sent")
        raw = letter(BOT, "welcome", to="ann@example.com", message_id="<y@example.com>")
        with self.assertLogs("mailfolders", level="WARNING") as logs:
            appended = mailfolders.append_copies(mailfolders.BOT, [("<y@example.com>", raw)])
        self.assertEqual(appended, 1)
        self.assertTrue(any("duplicate" in m for m in logs.output))


class TheSendKeepsACopy(unittest.TestCase):
    def setUp(self):
        self.saved = (mailer.send_email_via, mailfolders.save_sent_copy)
        self.queued = []
        mailfolders.save_sent_copy = (
            lambda account_key, message_id, raw: self.queued.append((account_key, message_id, raw)))

    def tearDown(self):
        mailer.send_email_via, mailfolders.save_sent_copy = self.saved

    def test_a_sent_letter_is_handed_on_whole(self):
        def sent(*args, **kwargs):
            return mailer.build_message("ann@example.com", "welcome", "<p>hi</p>")
        mailer.send_email_via = sent
        mailer.send_email_reply("ann@example.com", "welcome", "<p>hi</p>")
        self.assertEqual(len(self.queued), 1)
        account_key, message_id, raw = self.queued[0]
        self.assertEqual(account_key, "bot")
        self.assertTrue(message_id.startswith("<"))
        self.assertIn(message_id.encode(), raw)

    def test_keep_copy_false_sends_but_keeps_nothing(self):
        # The sample-letter preview on Settings > Letters: it must go out like
        # any other letter, and just as certainly never reach the Sent folder.
        mailer.send_email_via = lambda *a, **k: mailer.build_message("ann@example.com", "s", "<p>x</p>")
        mailer.send_email_reply("ann@example.com", "preview", "<p>x</p>", keep_copy=False)
        self.assertEqual(self.queued, [])

    def test_a_failed_send_keeps_nothing(self):
        def failed(*args, **kwargs):
            raise OSError("refused")
        mailer.send_email_via = failed
        with self.assertRaises(OSError):
            mailer.send_email_reply("ann@example.com", "welcome", "<p>hi</p>")
        self.assertEqual(self.queued, [])

    def test_a_broken_queue_does_not_fail_the_send(self):
        mailer.send_email_via = lambda *a, **k: mailer.build_message("a@example.com", "s", "<p>x</p>")

        def broken(account_key, message_id, raw):
            raise RuntimeError("queue is gone")
        mailfolders.save_sent_copy = broken
        mailer.send_email_reply("a@example.com", "s", "<p>x</p>")


class TheWorkerThread(WithMailbox):
    """
    Every other sent-copy test monkeypatches save_sent_copy itself, which
    proves what the caller does but nothing about the queue or the background
    thread draining it. This one calls the real thing and waits for the
    daemon thread to actually deliver the letter.
    """

    def setUp(self):
        super().setUp()
        self.saved_delay = mailfolders.COPY_DELAY
        mailfolders.COPY_DELAY = 0.05

    def tearDown(self):
        mailfolders.COPY_DELAY = self.saved_delay
        super().tearDown()

    def test_a_queued_copy_reaches_the_sent_folder_on_its_own(self):
        raw = letter(BOT, "welcome", to="ann@example.com", message_id="<via-worker@example.com>")
        mailfolders.save_sent_copy("bot", "<via-worker@example.com>", raw)

        deadline = time.time() + 5
        while time.time() < deadline and not self.box.folders["Sent"][1]:
            time.sleep(0.02)

        stored = self.box.folders["Sent"][1]
        self.assertEqual(len(stored), 1)
        self.assertIn(b"via-worker@example.com", stored[0].raw)


if __name__ == "__main__":
    unittest.main()
