"""
The test stand's stand-ins (docs/stand_panel.py), without starting the stand.

A stand is worth having only while it behaves like what it stands in for, and
it has one quiet way of stopping: XuiClient gains a method that makes a request,
nobody teaches the stand to answer it, and the page that calls it either breaks
on the stand alone or — worse — reaches for whatever panel a stray .env names.
The first test here is the one that notices.
"""
import inspect
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "docs"))

import stand
import stand_panel
from xui_client import XuiClient


class OnTheStand(unittest.TestCase):
    """XuiClient is one class for the whole process, so every test puts it back."""

    def setUp(self):
        self.panel = stand_panel.Panel()
        self.replaced = stand_panel.install(self.panel)
        self.xui = XuiClient()

    def tearDown(self):
        stand_panel.uninstall(self.replaced)


class NothingReachesARealPanel(OnTheStand):
    def test_every_method_that_makes_a_request_has_a_stand_in(self):
        # The two that are the request itself are answered by replacing
        # _request, and the constructor only builds the session; everything
        # else that touches the network has to be named.
        transport = {"_request", "_request_locked", "__init__"}
        unanswered = []
        for name, member in self.replaced.items():
            self.assertIsNot(XuiClient.__dict__[name], member, name)
        for name, member in vars(XuiClient).items():
            original = self.replaced.get(name, member)
            if not isinstance(original, types.FunctionType) or name in transport:
                continue
            source = inspect.getsource(original)
            if ("self._request(" in source or "self.session." in source) \
                    and name not in self.replaced:
                unanswered.append(name)
        self.assertEqual(unanswered, [],
                         "these make a request the stand has no answer for — "
                         "add them to stand_panel.install()")

    def test_a_request_that_slipped_through_is_refused_not_sent(self):
        with self.assertRaises(RuntimeError):
            self.xui._request("GET", "/panel/api/something/new")


class ThePanelKeepsWhatIsDoneToIt(OnTheStand):
    def test_a_client_that_is_created_is_there_afterwards(self):
        created, inbounds = self.xui.add_client(
            email="new@example.com", limit_gb=10, expire_days=30, inbound_ids=[1],
            comment="New", group="Standard", limit_hwid=2)
        self.assertTrue(created)
        found = self.xui.find_client_by_email("new@example.com")
        self.assertEqual(found["limitHwid"], 2)
        self.assertEqual(found["group"], "Standard")
        self.assertEqual(found["totalGB"], 10 * 1024 ** 3)

    def test_a_second_client_under_the_same_identifier_is_refused(self):
        taken = self.panel.clients[0]["email"]
        self.assertEqual(self.xui.add_client(email=taken, inbound_ids=[1]), (None, []))

    def test_an_edit_changes_what_it_names_and_nothing_else(self):
        client = self.panel.clients[0]
        before = dict(client)
        self.assertTrue(self.xui.update_client(client["uuid"], limit_hwid=4))
        self.assertEqual(client["limitHwid"], 4)
        self.assertEqual(client["comment"], before["comment"])
        self.assertEqual(client["totalGB"], before["totalGB"])

    def test_what_the_panel_hands_out_is_a_copy(self):
        # The real one answers over HTTP; a caller that changed the object it
        # was given and so changed the panel would be a bug only the stand has.
        self.xui.get_all_clients()[0]["comment"] = "changed by the caller"
        self.assertNotEqual(self.panel.clients[0]["comment"], "changed by the caller")

    def test_inbounds_are_changed_by_the_real_method(self):
        client = self.panel.clients[0]
        self.assertTrue(self.xui.set_client_inbounds(client["uuid"], [2, 3]))
        self.assertEqual(sorted(client["inboundIds"]), [2, 3])

    def test_renaming_a_group_nobody_is_in_reports_failure_as_3x_ui_does(self):
        self.assertFalse(self.xui.rename_group("No such group", "Other"))
        self.assertTrue(self.xui.rename_group("Family", "Household"))
        self.assertTrue(any(c["group"] == "Household" for c in self.panel.clients))

    def test_a_deleted_client_is_gone_with_its_devices(self):
        limited = next(c for c in self.panel.clients if c["limitHwid"])
        ok, _ = self.xui.delete_client(limited["uuid"])
        self.assertTrue(ok)
        self.assertIsNone(self.xui.find_client_by_uuid(limited["uuid"]))
        self.assertNotIn(limited["email"], self.panel.devices)


class Devices(OnTheStand):
    def setUp(self):
        super().setUp()
        self.limited = [c for c in self.panel.clients if c["limitHwid"]]

    def test_the_stand_has_both_ends_of_a_limit(self):
        counts = [len(self.xui.get_client_devices(c["email"])) for c in self.limited]
        self.assertIn(0, counts, "nobody with a limit and no device yet")
        self.assertTrue(any(n == c["limitHwid"] for n, c in zip(counts, self.limited)),
                        "nobody with every slot taken")

    def test_a_client_without_a_limit_has_no_devices(self):
        free = next(c for c in self.panel.clients if not c["limitHwid"])
        self.assertEqual(self.xui.get_client_devices(free["email"]), [])

    def test_forgetting_one_leaves_the_others(self):
        client = next(c for c in self.limited if len(self.panel.devices[c["email"]]) >= 2)
        devices = self.xui.get_client_devices(client["email"])
        self.assertTrue(self.xui.forget_client_devices(client["email"], devices[0]["id"]))
        left = self.xui.get_client_devices(client["email"])
        self.assertEqual([d["id"] for d in left], [d["id"] for d in devices[1:]])

    def test_forgetting_all(self):
        client = self.limited[0]
        self.xui.forget_client_devices(client["email"])
        self.assertEqual(self.xui.get_client_devices(client["email"]), [])


class TheOutbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-outbox-")
        self.outbox = stand_panel.Outbox(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_letter_is_kept_in_both_parts(self):
        message = types.SimpleNamespace(html='<img src="cid:subqr"><p>Привет</p>',
                                        text="Привет", images={"subqr": b"\x89PNG"})
        name = self.outbox.keep("ben@example.com", "Тема", message)
        listed = self.outbox.letters()
        self.assertEqual((listed[0]["to"], listed[0]["subject"]), ("ben@example.com", "Тема"))
        self.assertEqual(self.outbox.read(name, "txt"), "Привет")
        # The picture is in the page: a file has no Content-ID to resolve.
        self.assertIn("data:image/png;base64,", self.outbox.read(name, "html"))
        self.assertNotIn("cid:", self.outbox.read(name, "html"))

    def test_a_name_is_not_a_path(self):
        # It comes from a URL.
        secret = os.path.join(os.path.dirname(self.dir), "outside.txt")
        with open(secret, "w") as handle:
            handle.write("not a letter")
        try:
            self.assertIsNone(self.outbox.read("../outside", "txt"))
            self.assertIsNone(self.outbox.read("..\\outside", "txt"))
            self.assertIsNone(self.outbox.read("anything", "json"))
        finally:
            os.remove(secret)


class TheCopy(unittest.TestCase):
    """The stand never runs from a tree: that is the whole reason it is a copy."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-stand-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_checkout_is_refused_as_a_place_for_it(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        with self.assertRaises(SystemExit):
            stand.build(self.dir)
        with self.assertRaises(SystemExit):
            stand.build(stand.ROOT)

    def test_it_takes_the_code_and_leaves_the_state(self):
        # A tree somebody runs a panel from has these beside the code.
        target = os.path.join(self.dir, "stand")
        stand.build(target)
        self.assertTrue(os.path.isfile(os.path.join(target, "xui_client.py")))
        self.assertTrue(os.path.isfile(os.path.join(target, "docs", "stand.py")))
        for left_behind in (".git", ".env", "settings.json", "tariffs.json",
                            "email_texts.json", "logs"):
            self.assertFalse(os.path.exists(os.path.join(target, left_behind)), left_behind)

    def test_a_rebuild_keeps_what_was_saved_on_the_stand_unless_asked_not_to(self):
        target = os.path.join(self.dir, "stand")
        stand.build(target)
        saved = os.path.join(target, "tariffs.json")
        stale = os.path.join(target, "a_module_since_deleted.py")
        for path in (saved, stale):
            with open(path, "w") as handle:
                handle.write("{}")
        stand.build(target)
        self.assertTrue(os.path.exists(saved))
        self.assertFalse(os.path.exists(stale), "a deleted module would go on being imported")
        stand.build(target, fresh=True)
        self.assertFalse(os.path.exists(saved))

    @unittest.skipUnless(os.path.isdir(os.path.join(stand.ROOT, ".git")), "not a checkout")
    def test_the_panel_half_will_not_start_in_a_checkout(self):
        with self.assertRaises(SystemExit):
            stand.serve(types.SimpleNamespace(port=0, lang="en"))


class MailOnTheStand(unittest.TestCase):
    """
    The Mail pages read over IMAP, and a stand must never open a real mailbox —
    nor send to one: every address in the demo data is invented, and the first
    real one typed into the stand would otherwise be mailed.
    """

    def setUp(self):
        import stand_mail
        self.stand_mail = stand_mail
        self.mailboxes = stand_mail.Mailboxes()
        self.replaced = stand_mail.install(self.mailboxes)

    def tearDown(self):
        self.stand_mail.uninstall(self.replaced)

    def test_each_mailbox_is_its_own_server_in_memory(self):
        import mailfolders
        bot = mailfolders.list_letters(mailfolders.BOT, "inbox")
        support = mailfolders.list_letters(mailfolders.SUPPORT, "inbox")
        self.assertIn("/status", [row["subject"] for row in bot["rows"]])
        self.assertIn("My new phone will not connect", [row["subject"] for row in support["rows"]])
        self.assertNotIn("/status", [row["subject"] for row in support["rows"]])

    def test_one_letter_is_still_owed_an_answer_and_looking_does_not_change_that(self):
        import mailfolders
        rows = mailfolders.list_letters(mailfolders.BOT, "inbox")["rows"]
        self.assertEqual(sorted(row["seen"] for row in rows), [False, True])
        self.assertEqual(self.mailboxes.bot.writable_selects, [])
        self.assertEqual(self.mailboxes.bot.unpeeked_fetches, [])

    def test_a_letter_handed_to_the_bot_turns_up_in_its_inbox(self):
        import mailfolders
        self.mailboxes.received_by_the_bot("newcomer@example.com", "Newcomer", "AURORA", "")
        subjects = [row["subject"] for row in mailfolders.list_letters(mailfolders.BOT, "inbox")["rows"]]
        self.assertIn("AURORA", subjects)

    def test_nothing_reaches_an_smtp_server(self):
        import mailer
        with self.assertRaises(RuntimeError):
            mailer.send_email_via("smtp.example.com", 465, "bot@example.com", "x",
                                  "ben@example.com", "Hello", "<p>hi</p>")

    def test_everything_is_put_back(self):
        import imaplib
        import config
        self.stand_mail.uninstall(self.replaced)
        self.assertIs(imaplib.IMAP4_SSL, self.replaced["imap"])
        self.assertEqual(config.SUPPORT_IMAP_USER, self.replaced["config"]["SUPPORT_IMAP_USER"])
        self.replaced = self.stand_mail.install(self.mailboxes)


class TheOutboxAndAPersonsReply(unittest.TestCase):
    def test_a_reply_that_is_text_is_kept_as_text(self):
        import mailer
        directory = tempfile.mkdtemp(prefix="mailchilla-outbox-")
        try:
            outbox = stand_panel.Outbox(directory)
            name = outbox.keep("ben@example.com", "Re: Help",
                               mailer.PlainText("Looking into it.\n\n> <b>Help</b>"))
            self.assertEqual(outbox.read(name, "txt"), "Looking into it.\n\n> <b>Help</b>")
            self.assertIn("&lt;b&gt;Help&lt;/b&gt;", outbox.read(name, "html"))
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
