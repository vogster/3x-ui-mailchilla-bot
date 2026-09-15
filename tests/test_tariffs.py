"""
Tariffs, the words that open them, and finding one in a letter.

Two kinds of mistake are covered here, and both are the sort that go unnoticed
until somebody gets the wrong subscription. A word matched as a substring
registers people who never asked — START lives inside RESTART, and the welcome
letter quotes the word back at everybody who replies to it. And a tariff whose
word collides with another tariff's makes what a letter gets depend on the
order the file happens to be written in.

The storage is exercised against a temporary file: tariffs.TARIFFS_PATH is
built from the module's own __file__, so a test that forgot to redirect it
would write over the installation's real tariffs.
"""
import json
import os
import tempfile
import unittest

import config
import email_bot
import tariffs


class StorageCase(unittest.TestCase):
    """Every test here works on a tariffs.json of its own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-tariffs-")
        self.path = os.path.join(self.dir, "tariffs.json")
        self.real_path = tariffs.TARIFFS_PATH
        tariffs.TARIFFS_PATH = self.path
        self.saved_state = tariffs.snapshot()
        tariffs._state = {"tariffs": [], "codes": []}

    def tearDown(self):
        tariffs.TARIFFS_PATH = self.real_path
        tariffs._state = self.saved_state

    def make(self, name="Basic", word="AURORA", limit=100, days=90, inbounds=(1, 2)):
        """A tariff and, unless `word` is empty, the open word that opens it."""
        tariff = tariffs.save_tariff(
            {"name": name, "limit_gb": limit, "expire_days": days,
             "inbound_ids": list(inbounds)})
        if word:
            tariffs.save_code({"word": word, "tariff_id": tariff["id"],
                               "uses_left": None, "enabled": True})
        return tariff

    def invite(self, tariff_id, uses=1, note=""):
        """A code with a limited number of activations — what used to be its own kind."""
        return tariffs.save_code({
            "word": tariffs.generate_word(), "tariff_id": tariff_id,
            "uses_left": uses, "note": note, "enabled": True})


class Seeding(StorageCase):
    def test_the_first_run_builds_a_tariff_out_of_the_old_settings(self):
        # An installation that predates tariffs has one code word and one set of
        # defaults. They must come back as a tariff, or 34 people stop being
        # able to register the morning after an update.
        saved = (config.CODEWORD, config.LIMIT_GB, config.EXPIRE_DAYS, config.XUI_INBOUND_IDS)
        config.CODEWORD, config.LIMIT_GB = "SNOWFALL", 250
        config.EXPIRE_DAYS, config.XUI_INBOUND_IDS = 30, [4, 7]
        try:
            tariffs.load()
        finally:
            (config.CODEWORD, config.LIMIT_GB,
             config.EXPIRE_DAYS, config.XUI_INBOUND_IDS) = saved

        only = tariffs.all_tariffs()
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["limit_gb"], 250)
        self.assertEqual(only[0]["expire_days"], 30)
        self.assertEqual(only[0]["inbound_ids"], [4, 7])
        matched = tariffs.match("snowfall")
        self.assertIsNotNone(matched)
        self.assertEqual(matched[0]["id"], only[0]["id"])

    def test_the_seed_is_written_so_the_next_start_reads_it(self):
        tariffs.load()
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["tariffs"]), 1)

    def test_a_second_load_does_not_seed_again(self):
        tariffs.load()
        first = tariffs.all_tariffs()[0]["id"]
        tariffs.load()
        self.assertEqual([t["id"] for t in tariffs.all_tariffs()], [first])


class Words(StorageCase):
    def test_a_word_opens_its_tariff_whatever_the_case(self):
        made = self.make(word="Aurora")
        self.assertEqual(tariffs.match("AURORA")[0]["id"], made["id"])
        self.assertEqual(tariffs.match("  aurora ")[0]["id"], made["id"])

    def test_two_tariffs_cannot_share_a_word(self):
        self.make(name="Basic", word="AURORA")
        with self.assertRaises(ValueError):
            self.make(name="Family", word="aurora")

    def test_a_tariff_may_have_no_word_at_all(self):
        # Reachable by invitation only, which is what a closed tariff is.
        made = self.make(name="Staff", word="")
        self.assertEqual(tariffs.codes_for(made["id"]), [])
        self.assertEqual(tariffs.live_words(), [])

    def test_renaming_a_word_leaves_one_code_behind(self):
        made = self.make(word="AURORA")
        tariffs.save_code({"word": "BOREALIS", "tariff_id": made["id"],
                           "uses_left": None, "enabled": True}, was="AURORA")
        self.assertEqual([c["word"] for c in tariffs.codes_for(made["id"])], ["BOREALIS"])
        self.assertIsNone(tariffs.match("AURORA"))

    def test_renaming_keeps_who_came_in_through_it(self):
        # The record is history, not a setting: a word changed after a leak
        # must not take the list of people with it.
        made = self.make(word="AURORA")
        tariffs.spend("AURORA", "ben@example.com")
        renamed = tariffs.save_code({"word": "BOREALIS", "tariff_id": made["id"],
                                     "uses_left": None, "enabled": True}, was="AURORA")
        self.assertEqual(renamed["used_by"], ["ben@example.com"])

    def test_a_tariff_may_have_several_words(self):
        made = self.make(word="AURORA")
        tariffs.save_code({"word": "AURORA-2026", "tariff_id": made["id"],
                           "uses_left": None, "enabled": True})
        self.assertEqual(tariffs.match("AURORA")[0]["id"], made["id"])
        self.assertEqual(tariffs.match("AURORA-2026")[0]["id"], made["id"])

    def test_editing_a_tariff_leaves_its_codes_alone(self):
        made = self.make(word="AURORA")
        self.invite(made["id"])
        tariffs.save_tariff({**made, "limit_gb": 300})
        self.assertEqual(len(tariffs.codes_for(made["id"])), 2)
        self.assertIsNotNone(tariffs.match("AURORA"))

    def test_a_revoked_word_opens_nothing(self):
        made = self.make(word="AURORA")
        tariffs._state["codes"][0]["enabled"] = False
        self.assertIsNone(tariffs.match("AURORA"))
        self.assertEqual(tariffs.live_words(), [])
        self.assertTrue(tariffs.get(made["id"]))

    def test_deleting_a_tariff_takes_its_words_and_leaves_its_clients(self):
        made = self.make(word="AURORA")
        tariffs.delete_tariff(made["id"])
        self.assertIsNone(tariffs.match("AURORA"))
        self.assertEqual(tariffs.all_codes(), [])


class Spending(StorageCase):
    def test_a_permanent_word_records_who_came_and_stays_open(self):
        self.make(word="AURORA")
        tariffs.spend("AURORA", "ben@example.com")
        code = tariffs.all_codes()[0]
        self.assertEqual(code["used_by"], ["ben@example.com"])
        self.assertIsNone(code["uses_left"])
        self.assertIsNotNone(tariffs.match("AURORA"))

    def test_the_same_address_is_recorded_once(self):
        self.make(word="AURORA")
        tariffs.spend("AURORA", "ben@example.com")
        tariffs.spend("AURORA", "ben@example.com")
        self.assertEqual(tariffs.all_codes()[0]["used_by"], ["ben@example.com"])


class ReadableText(unittest.TestCase):
    """What part of a letter counts as written by its sender."""

    def test_the_subject_is_part_of_it(self):
        self.assertIn("AURORA", email_bot.readable_text("AURORA", "hello"))

    def test_a_quoted_reply_is_cut_off(self):
        # The welcome letter carries the word that registered them, so a reply
        # to it quotes that word straight back.
        body = "Thanks, it works!\n\n> Write AURORA to this address\n> and you are in"
        self.assertNotIn("AURORA", email_bot.readable_text("Re: welcome", body))

    def test_an_outlook_style_quote_is_cut_off(self):
        body = "thanks\n\nOn Tuesday, 3 March 2026, Aurora VPN wrote:\nwelcome, your word is AURORA"
        self.assertNotIn("AURORA", email_bot.readable_text("Re:", body))

    def test_a_russian_quote_header_is_cut_off(self):
        body = "спасибо\n\nвт, 3 мар. 2026 г. в 10:12, Aurora VPN пишет:\nваше слово AURORA"
        self.assertNotIn("AURORA", email_bot.readable_text("Re:", body))

    def test_a_forwarded_block_is_cut_off(self):
        body = "look at this\n\n---------- Forwarded message ----------\nFrom: bot\nAURORA"
        self.assertNotIn("AURORA", email_bot.readable_text("Fwd:", body))


class FindWords(unittest.TestCase):
    def test_a_word_inside_another_word_does_not_count(self):
        self.assertEqual(email_bot.find_words("please RESTART the router", ["START"]), [])

    def test_a_word_of_its_own_counts(self):
        self.assertEqual(email_bot.find_words("please START now", ["START"]), ["START"])

    def test_punctuation_around_the_word_is_fine(self):
        self.assertEqual(email_bot.find_words("«AURORA», please", ["AURORA"]), ["AURORA"])

    def test_a_word_containing_punctuation_matches_whole(self):
        self.assertEqual(email_bot.find_words("AURORA-2026 please", ["AURORA-2026"]),
                         ["AURORA-2026"])

    def test_case_does_not_matter(self):
        self.assertEqual(email_bot.find_words("aurora", ["AURORA"]), ["AURORA"])

    def test_several_words_all_come_back(self):
        # The caller refuses to guess when this happens; it must be able to see
        # that it happened.
        self.assertEqual(email_bot.find_words("AURORA and FAMILY", ["AURORA", "FAMILY"]),
                         ["AURORA", "FAMILY"])

    def test_the_same_word_twice_counts_once(self):
        self.assertEqual(email_bot.find_words("AURORA aurora", ["AURORA", "aurora"]),
                         ["AURORA"])

    def test_a_cyrillic_word_keeps_its_boundaries(self):
        self.assertEqual(email_bot.find_words("хочу подписку", ["подписка"]), [])
        self.assertEqual(email_bot.find_words("слово: подписка", ["подписка"]), ["подписка"])


class Dispatch(StorageCase):
    """
    What a letter actually does, with the handlers stubbed out.

    The dispatch is where a mistake is expensive and invisible: registering
    somebody on the wrong tariff hands out the wrong limits, and the only sign
    of it is a client who quietly has more than they should.
    """

    def setUp(self):
        super().setUp()
        self.calls = []
        self.real = {name: getattr(email_bot, name) for name in
                     ("handle_registration", "handle_status", "handle_help",
                      "handle_unknown", "send_email_reply", "_mark_seen")}
        email_bot.handle_registration = lambda addr, name="", tariff=None, code=None: \
            self.calls.append(("register", addr, tariff["name"] if tariff else None))
        email_bot.handle_status = lambda addr: self.calls.append(("status", addr, None))
        email_bot.handle_help = lambda addr: self.calls.append(("help", addr, None))
        email_bot.handle_unknown = lambda addr, subject: self.calls.append(("unknown", addr, None))
        email_bot.send_email_reply = lambda addr, subject, message: \
            self.calls.append(("reply", addr, subject))
        email_bot._mark_seen = lambda conn, num: None

    def tearDown(self):
        for name, value in self.real.items():
            setattr(email_bot, name, value)
        super().tearDown()

    def letter(self, subject, body=""):
        self.calls = []
        email_bot.process_message(1, "ben@example.com", subject, body, None)
        return self.calls

    def test_a_word_registers_on_its_own_tariff(self):
        self.make(name="Basic", word="AURORA")
        self.make(name="Family", word="FAMILY", limit=500)
        self.assertEqual(self.letter("FAMILY"), [("register", "ben@example.com", "Family")])

    def test_two_words_in_one_letter_ask_rather_than_guess(self):
        self.make(name="Basic", word="AURORA")
        self.make(name="Family", word="FAMILY")
        kinds = [call[0] for call in self.letter("AURORA", "or maybe FAMILY")]
        self.assertEqual(kinds, ["reply"])

    def test_a_quoted_word_is_not_a_registration(self):
        self.make(name="Basic", word="AURORA")
        calls = self.letter("Re: welcome", "thanks!\n\n> your word is AURORA")
        self.assertEqual([c[0] for c in calls], ["unknown"])

    def test_status_still_works(self):
        self.make(word="AURORA")
        self.assertEqual([c[0] for c in self.letter("/status")], ["status"])

    def test_a_quoted_command_is_not_a_command(self):
        self.make(word="AURORA")
        calls = self.letter("Re:", "ok\n\n> write /status to check your traffic")
        self.assertEqual([c[0] for c in calls], ["unknown"])

    def test_start_still_works_when_there_is_one_tariff(self):
        # How it behaved before tariffs existed, and nothing about a single
        # tariff makes the question ambiguous.
        self.make(name="Basic", word="AURORA")
        self.assertEqual(self.letter("/start"), [("register", "ben@example.com", "Basic")])

    def test_start_asks_when_there_are_several_tariffs(self):
        self.make(name="Basic", word="AURORA")
        self.make(name="Family", word="FAMILY")
        self.assertEqual([c[0] for c in self.letter("/start")], ["unknown"])

    def test_a_revoked_word_registers_nobody(self):
        self.make(word="AURORA")
        tariffs._state["codes"][0]["enabled"] = False
        self.assertEqual([c[0] for c in self.letter("AURORA")], ["unknown"])


class RegistrationUsesTheTariff(StorageCase):
    """
    The limits a new client is created with come from the tariff, not from the
    settings the tariff replaced. This is the mistake nobody would notice: the
    client exists, the letter goes out, and only the traffic figure is wrong.
    """

    def setUp(self):
        super().setUp()
        self.added = []
        self.real = {name: getattr(email_bot, name) for name in
                     ("get_shared_client", "send_welcome_email", "send_gotify_notification")}
        outer = self

        class FakeXui:
            def find_client_by_email(self, addr):
                return {"subId": "sub01"} if outer.added else None

            def add_client(self, **kwargs):
                outer.added.append(kwargs)
                return ("uuid", [1])

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_welcome_email = lambda *a, **kw: outer.added.append(("letter", a, kw))
        email_bot.send_gotify_notification = lambda **kw: None

    def tearDown(self):
        for name, value in self.real.items():
            setattr(email_bot, name, value)
        super().tearDown()

    def test_the_client_is_created_with_the_tariff_values(self):
        tariff = self.make(name="Family", word="FAMILY", limit=500, days=365, inbounds=(1, 3))
        email_bot.handle_registration("ben@example.com", "Ben", tariff,
                                      tariffs.codes_for(tariff["id"])[0])
        call = self.added[0]
        self.assertEqual(call["limit_gb"], 500)
        self.assertEqual(call["expire_days"], 365)
        self.assertEqual(call["inbound_ids"], [1, 3])

    def test_the_code_records_who_came_through_it(self):
        tariff = self.make(word="FAMILY")
        email_bot.handle_registration("ben@example.com", "Ben", tariff,
                                      tariffs.codes_for(tariff["id"])[0])
        self.assertEqual(tariffs.all_codes()[0]["used_by"], ["ben@example.com"])

    def test_without_a_tariff_nothing_is_created(self):
        # The caller resolves the word; arriving here without one is a bug, and
        # inventing limits would be the worst possible answer to it.
        email_bot.handle_registration("ben@example.com", "Ben")
        self.assertEqual(self.added, [])


class LimitedCodes(StorageCase):
    """
    Codes with a limited number of activations — a personal invitation is one
    with a single activation. The failure that matters is one that keeps
    working after it has been used: it was handed to one person, and a word
    that outlives its use is a public word nobody meant to publish.
    """

    def test_an_invitation_opens_its_tariff_once(self):
        tariff = self.make(name="Family", word="FAMILY")
        invite = self.invite(tariff["id"], note="for Ben")
        matched = tariffs.match(invite["word"])
        self.assertIsNotNone(matched)
        self.assertEqual(matched[0]["id"], tariff["id"])

        tariffs.spend(invite["word"], "ben@example.com")
        self.assertIsNone(tariffs.match(invite["word"]))
        self.assertNotIn(invite["word"], tariffs.live_words())

    def test_a_spent_invitation_remembers_who_used_it(self):
        tariff = self.make()
        invite = self.invite(tariff["id"])
        tariffs.spend(invite["word"], "ben@example.com")
        spent = [c for c in tariffs.all_codes() if c["word"] == invite["word"]][0]
        self.assertEqual(spent["used_by"], ["ben@example.com"])
        self.assertEqual(spent["uses_left"], 0)

    def test_an_invitation_can_be_good_for_several_people(self):
        tariff = self.make()
        invite = self.invite(tariff["id"], uses=3)
        for who in ("a@example.com", "b@example.com"):
            tariffs.spend(invite["word"], who)
        self.assertIsNotNone(tariffs.match(invite["word"]))
        tariffs.spend(invite["word"], "c@example.com")
        self.assertIsNone(tariffs.match(invite["word"]))

    def test_revoking_shuts_an_invitation_without_deleting_it(self):
        tariff = self.make()
        invite = self.invite(tariff["id"], note="for Ben")
        tariffs.set_code_enabled(invite["word"], False)
        self.assertIsNone(tariffs.match(invite["word"]))
        # still on the list, with its note, so the administrator can see what
        # was revoked and bring it back
        kept = [c for c in tariffs.all_codes() if c["word"] == invite["word"]][0]
        self.assertEqual(kept["note"], "for Ben")
        tariffs.set_code_enabled(invite["word"], True)
        self.assertIsNotNone(tariffs.match(invite["word"]))

    def test_an_invitation_is_readable_off_a_screen(self):
        # It gets typed by hand from a phone: no 0/O, no 1/I/l.
        tariff = self.make()
        word = tariffs.generate_word()
        self.assertEqual(len(word), tariffs.GENERATED_LENGTH)
        self.assertFalse(set(word) & set("01OIL"))

    def test_invitations_do_not_repeat(self):
        tariff = self.make()
        words = {self.invite(tariff["id"])["word"] for _ in range(50)}
        self.assertEqual(len(words), 50)

    def test_a_tariff_with_no_word_is_reachable_by_invitation(self):
        # The case the list calls "invitation only": no public word at all.
        tariff = self.make(name="Staff", word="")
        invite = self.invite(tariff["id"])
        self.assertEqual(tariffs.match(invite["word"])[0]["id"], tariff["id"])

    def test_deleting_the_tariff_takes_its_invitations(self):
        tariff = self.make()
        invite = self.invite(tariff["id"])
        tariffs.delete_tariff(tariff["id"])
        self.assertIsNone(tariffs.match(invite["word"]))

    def test_registering_through_an_invitation_spends_it(self):
        # The whole point of a one-shot code, checked through the real path
        # rather than by calling spend() directly.
        added = []
        real = {name: getattr(email_bot, name) for name in
                ("get_shared_client", "send_welcome_email", "send_gotify_notification")}

        class FakeXui:
            def find_client_by_email(self, addr):
                return {"subId": "sub01"} if added else None

            def add_client(self, **kwargs):
                added.append(kwargs)
                return ("uuid", [1])

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_welcome_email = lambda *a, **kw: None
        email_bot.send_gotify_notification = lambda **kw: None
        try:
            tariff = self.make(name="Family", word="FAMILY")
            invite = self.invite(tariff["id"])
            matched = tariffs.match(invite["word"])
            email_bot.handle_registration("ben@example.com", "Ben", matched[0], matched[1])
            self.assertEqual(added[0]["limit_gb"], 100)
            self.assertIsNone(tariffs.match(invite["word"]))
        finally:
            for name, value in real.items():
                setattr(email_bot, name, value)

    def test_an_invitation_used_by_somebody_already_registered_is_not_burned(self):
        # They get their link again and nothing else — so the invitation is
        # still there for the person it was actually meant for.
        real = {name: getattr(email_bot, name) for name in
                ("get_shared_client", "send_welcome_email")}

        class FakeXui:
            def find_client_by_email(self, addr):
                return {"subId": "sub01"}

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_welcome_email = lambda *a, **kw: None
        try:
            tariff = self.make()
            invite = self.invite(tariff["id"])
            matched = tariffs.match(invite["word"])
            email_bot.handle_registration("ben@example.com", "Ben", matched[0], matched[1])
            self.assertIsNotNone(tariffs.match(invite["word"]))
        finally:
            for name, value in real.items():
                setattr(email_bot, name, value)


class TheGroupIsTheTariff(StorageCase):
    """
    Which tariff a client is on lives in 3x-ui, in the client's group, named
    after the tariff. That keeps the answer where the client is and out of a
    file of ours that could disagree with it — and it is why two tariffs may
    not share a name.
    """

    def test_two_tariffs_cannot_share_a_name(self):
        self.make(name="Family", word="FAMILY")
        with self.assertRaises(ValueError):
            self.make(name="family", word="OTHER")

    def test_a_tariff_can_keep_its_own_name(self):
        made = self.make(name="Family", word="FAMILY")
        tariffs.save_tariff({**made, "limit_gb": 700})
        self.assertEqual(tariffs.get(made["id"])["limit_gb"], 700)

    def test_registration_stamps_the_tariff_name_as_the_group(self):
        added = []
        real = {name: getattr(email_bot, name) for name in
                ("get_shared_client", "send_welcome_email", "send_gotify_notification")}

        class FakeXui:
            def find_client_by_email(self, addr):
                return {"subId": "sub01"} if added else None

            def add_client(self, **kwargs):
                added.append(kwargs)
                return ("uuid", [1])

        email_bot.get_shared_client = lambda: FakeXui()
        email_bot.send_welcome_email = lambda *a, **kw: None
        email_bot.send_gotify_notification = lambda **kw: None
        try:
            tariff = self.make(name="Family", word="FAMILY")
            matched = tariffs.match("FAMILY")
            email_bot.handle_registration("ben@example.com", "Ben", matched[0], matched[1])
            self.assertEqual(added[0]["group"], "Family")
        finally:
            for name, value in real.items():
                setattr(email_bot, name, value)
