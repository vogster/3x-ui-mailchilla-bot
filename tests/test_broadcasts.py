"""
What the panel knows about broadcasts: the record, who writes it, and the page.

A running broadcast used to be visible on one page only — the one the browser
was redirected to after Send. Close that tab and nothing in the panel said
whether letters were still going out; a broadcast started by mail was never
shown at all. These are the tests of the record that replaced that, of the
sending writing into it whichever way it was started, and of the Broadcast
page saying what it holds.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fastapi.testclient import TestClient

import broadcasts
import email_bot
from test_panel_routes import PanelCase


class TheRecord(unittest.TestCase):
    def setUp(self):
        broadcasts._reset()

    def tearDown(self):
        broadcasts._reset()

    def test_a_broadcast_from_start_to_finish(self):
        job_id = broadcasts.start("Maintenance", "Tonight.", 3, "warning", broadcasts.PANEL)
        job = broadcasts.get(job_id)
        self.assertEqual((job["subject"], job["total"], job["kind"], job["source"]),
                         ("Maintenance", 3, "warning", "panel"))
        self.assertFalse(job["done"])
        self.assertEqual(broadcasts.running(), 1)

        broadcasts.progress(job_id, 1, 0)
        broadcasts.progress(job_id, 1, 1, "bounced@example.com")
        broadcasts.progress(job_id, 2, 1)
        broadcasts.finish(job_id)

        job = broadcasts.get(job_id)
        self.assertEqual((job["sent"], job["failed"]), (2, 1))
        self.assertEqual(job["failed_addresses"], ["bounced@example.com"])
        self.assertTrue(job["done"])
        self.assertIsNotNone(job["finished_at"])
        self.assertEqual(broadcasts.running(), 0)

    def test_finishing_twice_keeps_the_first_time_and_any_error(self):
        # It is called twice on purpose: by the sending, and by whoever started
        # it in case the sending never got that far.
        job_id = broadcasts.start("s", "m", 1)
        broadcasts.finish(job_id, error="RuntimeError: the mailbox went away")
        first = broadcasts.get(job_id)["finished_at"]
        broadcasts.finish(job_id)
        job = broadcasts.get(job_id)
        self.assertEqual(job["finished_at"], first)
        self.assertEqual(job["error"], "RuntimeError: the mailbox went away")

    def test_an_error_given_second_is_kept_too(self):
        job_id = broadcasts.start("s", "m", 1)
        broadcasts.finish(job_id)
        broadcasts.finish(job_id, error="late")
        self.assertEqual(broadcasts.get(job_id)["error"], "late")

    def test_what_is_handed_out_is_a_copy(self):
        job_id = broadcasts.start("s", "m", 1)
        broadcasts.get(job_id)["failed_addresses"].append("x@example.com")
        broadcasts.get(job_id)["sent"] = 99
        job = broadcasts.get(job_id)
        self.assertEqual((job["sent"], job["failed_addresses"]), (0, []))

    def test_an_id_nobody_knows(self):
        self.assertIsNone(broadcasts.get("no-such"))
        broadcasts.progress("no-such", 1, 0)
        broadcasts.finish("no-such")

    def test_running_ones_come_first_then_the_newest(self):
        old = broadcasts.start("old, still going", "m", 5)
        middle = broadcasts.start("middle, finished", "m", 5)
        new = broadcasts.start("new, finished", "m", 5)
        # Made in the same instant; spread them out.
        now = datetime.now()
        for age, job_id in enumerate((new, middle, old)):
            broadcasts._jobs[job_id]["started_at"] = now - timedelta(minutes=age)
        broadcasts.finish(middle)
        broadcasts.finish(new)
        self.assertEqual([j["subject"] for j in broadcasts.recent()],
                         ["old, still going", "new, finished", "middle, finished"])
        # Cut short, the list still shows the one that is running.
        self.assertEqual([j["subject"] for j in broadcasts.recent(1)], ["old, still going"])

    def test_only_so_many_are_remembered_and_never_at_the_cost_of_a_running_one(self):
        running = broadcasts.start("the one still going", "m", 5)
        broadcasts._jobs[running]["started_at"] = datetime.now() - timedelta(days=1)
        for number in range(broadcasts.KEPT + 5):
            broadcasts.finish(broadcasts.start(f"finished {number}", "m", 1))
        self.assertEqual(len(broadcasts.recent()), broadcasts.KEPT)
        self.assertIsNotNone(broadcasts.get(running))

    def test_only_so_many_failed_addresses_are_kept_by_name(self):
        # A mailbox that has started refusing everything would otherwise be the
        # whole client list, once per broadcast.
        job_id = broadcasts.start("s", "m", 500)
        for number in range(broadcasts.FAILED_KEPT + 30):
            broadcasts.progress(job_id, 0, number + 1, f"user{number}@example.com")
        job = broadcasts.get(job_id)
        self.assertEqual(len(job["failed_addresses"]), broadcasts.FAILED_KEPT)
        self.assertEqual(job["failed"], broadcasts.FAILED_KEPT + 30)


class _Sending:
    """Replaces the letter leaving and the pause before it; records who was written to."""

    def stub_sending(self, refuse=()):
        self.written_to = []
        self.real_send = email_bot.send_email_reply
        self.real_sleep = email_bot.time.sleep

        def send(to_email, subject, message, **kwargs):
            if to_email in refuse:
                raise RuntimeError("refused")
            self.written_to.append(to_email)

        email_bot.send_email_reply = send
        email_bot.time.sleep = lambda seconds: None

    def restore_sending(self):
        email_bot.send_email_reply = self.real_send
        email_bot.time.sleep = self.real_sleep


class TheSendingWritesTheRecord(_Sending, unittest.TestCase):
    """Whichever way it was started — that is the point of writing it here."""

    def setUp(self):
        broadcasts._reset()
        self.stub_sending(refuse={"gone@example.com"})

    def tearDown(self):
        self.restore_sending()
        broadcasts._reset()

    def test_one_started_by_mail_is_listed(self):
        # The bot's /broadcast command calls it just so: text, and nothing else
        # but the recipients, which there come from 3x-ui.
        sent = email_bot.handle_broadcast("Tonight.", emails=["a@example.com", "gone@example.com",
                                                              "b@example.com"])
        self.assertEqual(sent, 2)
        (job,) = broadcasts.recent()
        self.assertEqual(job["source"], broadcasts.MAIL)
        self.assertEqual((job["total"], job["sent"], job["failed"]), (3, 2, 1))
        self.assertEqual(job["failed_addresses"], ["gone@example.com"])
        self.assertTrue(job["done"])
        self.assertEqual(job["error"], "")
        self.assertTrue(job["subject"])       # the default one, not an empty line

    def test_one_given_a_record_writes_into_it_and_opens_no_other(self):
        job_id = broadcasts.start("From the panel", "Tonight.", 2, "info", broadcasts.PANEL)
        email_bot.handle_broadcast("Tonight.", subject="From the panel",
                                   emails=["a@example.com", "b@example.com"], job_id=job_id)
        (job,) = broadcasts.recent()
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["source"], broadcasts.PANEL)
        self.assertEqual((job["sent"], job["done"]), (2, True))

    def test_nobody_to_write_to_is_not_a_broadcast(self):
        self.assertEqual(email_bot.handle_broadcast("Tonight.", emails=[]), 0)
        self.assertEqual(broadcasts.recent(), [])

    def test_nobody_to_write_to_still_closes_a_record_that_was_opened(self):
        job_id = broadcasts.start("s", "m", 0)
        email_bot.handle_broadcast("Tonight.", emails=[], job_id=job_id)
        self.assertTrue(broadcasts.get(job_id)["done"])

    def test_a_broadcast_that_breaks_off_says_so(self):
        # Not a letter that failed — the loop itself. Without the error the
        # page would show it as one that simply finished a third of the way.
        def explode(seconds):
            if len(self.written_to) >= 1:
                raise KeyboardInterrupt()

        email_bot.time.sleep = explode
        with self.assertRaises(KeyboardInterrupt):
            email_bot.handle_broadcast("Tonight.", emails=["a@example.com", "b@example.com",
                                                           "c@example.com"])
        (job,) = broadcasts.recent()
        self.assertTrue(job["done"])
        self.assertIn("KeyboardInterrupt", job["error"])
        self.assertEqual(job["sent"], 1)


class ThePage(_Sending, PanelCase):
    def setUp(self):
        super().setUp()
        broadcasts._reset()
        self.stub_sending(refuse={"gone@example.com"})

    def tearDown(self):
        self.restore_sending()
        broadcasts._reset()
        super().tearDown()

    def test_with_nothing_sent_it_says_so(self):
        body = self.page("/broadcast")
        self.assertIn("none is being sent right now", body)
        self.assertIn("No broadcast has been sent since the service started", body)
        self.assertIn('data-running="0"', body)

    def test_one_that_is_running_is_at_the_top_of_the_page(self):
        job_id = broadcasts.start("Maintenance tonight", "Tonight.", 40, "warning")
        broadcasts.progress(job_id, 12, 1, "gone@example.com")
        body = self.page("/broadcast")
        self.assertIn("Sending now", body)
        self.assertIn("Maintenance tonight", body)
        self.assertIn(f'href="/broadcast/status/{job_id}"', body)
        self.assertIn("13 of 40", body)
        self.assertIn("failures 1", body)
        self.assertIn("width: 32%", body)
        self.assertIn('data-running="1"', body)
        # Before the form, where it is seen before Send is.
        self.assertLess(body.index("Maintenance tonight"), body.index('id="broadcast-form"'))

    def test_one_that_is_over_says_how_it_went(self):
        for subject, sent, failed, error in (("All of them", 3, 0, ""),
                                             ("Most of them", 2, 1, ""),
                                             ("Broke off", 1, 0, "RuntimeError: boom")):
            job_id = broadcasts.start(subject, "m", 3)
            broadcasts.progress(job_id, sent, failed, "gone@example.com" if failed else "")
            broadcasts.finish(job_id, error=error)
        body = self.page("/broadcast")
        self.assertIn("none is being sent right now", body)
        self.assertIn("Sent 3 of 3", body)
        self.assertIn("Sent 2 of 3, not delivered 1", body)
        self.assertIn("Cut short: 1 of 3", body)

    def test_one_started_by_mail_says_where_it_came_from(self):
        email_bot.handle_broadcast("Tonight.", emails=["a@example.com"])
        body = self.page("/broadcast")
        self.assertIn("by mail, with /broadcast", body)

    def test_the_list_alone_for_the_page_to_re_read(self):
        job_id = broadcasts.start("Maintenance tonight", "Tonight.", 40)
        response = self.client.get("/broadcast/jobs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Maintenance tonight", response.text)
        self.assertIn('data-running="1"', response.text)
        # A fragment: no page around it, or the page would end up inside itself.
        self.assertNotIn("<html", response.text)
        self.assertNotIn("broadcast-form", response.text)
        broadcasts.finish(job_id)
        self.assertIn('data-running="0"', self.client.get("/broadcast/jobs").text)

    def test_the_list_answers_a_lapsed_session_with_a_refusal_not_a_redirect(self):
        # The page puts the answer into itself. A redirect would be followed,
        # and the sign-in form would be sitting inside the Broadcast page.
        response = TestClient(self.app).get("/broadcast/jobs", follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.text, "")

    def test_a_subject_cannot_bring_markup_into_the_list(self):
        broadcasts.start("<img src=x onerror=alert(1)>", "m", 1)
        for url in ("/broadcast", "/broadcast/jobs"):
            with self.subTest(url=url):
                body = self.client.get(url).text
                self.assertNotIn("<img src=x", body)
                self.assertIn("&lt;img src=x", body)

    def test_sending_from_the_form_is_listed_and_followed_to_its_end(self):
        response = self.client.post("/broadcast", follow_redirects=False, data={
            "subject": "Maintenance tonight", "message": "Tonight.", "kind": "warning",
            "emails": ["ben@example.com", "gone@example.com"]})
        self.assertEqual(response.status_code, 303)
        (job,) = broadcasts.recent()
        self.assertEqual(response.headers["location"], f"/broadcast/status/{job['id']}")
        # The test client runs the background task before it returns.
        self.assertEqual(self.written_to, ["ben@example.com"])
        self.assertEqual((job["source"], job["total"], job["sent"], job["failed"], job["done"]),
                         ("panel", 2, 1, 1, True))

        status = self.page(response.headers["location"])
        self.assertIn("The broadcast is finished", status)
        self.assertIn("from the panel", status)
        self.assertIn("gone@example.com", status)
        self.assertIn("Maintenance tonight", status)
        self.assertIn("Sent 1 of 2, not delivered 1", self.page("/broadcast"))

    def test_a_blank_subject_is_listed_under_the_one_the_letters_carry(self):
        self.client.post("/broadcast", follow_redirects=False, data={
            "subject": "   ", "message": "Tonight.", "emails": ["ben@example.com"]})
        (job,) = broadcasts.recent()
        self.assertTrue(job["subject"].strip())

    def test_a_broadcast_that_broke_before_sending_is_not_left_running(self):
        # Otherwise it would be shown as being sent for as long as the process lived.
        real = email_bot.handle_broadcast

        def broken(*args, **kwargs):
            raise RuntimeError("the template would not render")

        email_bot.handle_broadcast = broken
        try:
            self.client.post("/broadcast", follow_redirects=False, data={
                "subject": "s", "message": "Tonight.", "emails": ["ben@example.com"]})
        finally:
            email_bot.handle_broadcast = real
        (job,) = broadcasts.recent()
        self.assertTrue(job["done"])
        self.assertIn("would not render", job["error"])
        self.assertEqual(broadcasts.running(), 0)
        self.assertIn("Cut short", self.page("/broadcast"))

    def test_the_status_page_of_one_that_is_running(self):
        job_id = broadcasts.start("Maintenance tonight", "Tonight.", 40, source=broadcasts.MAIL)
        broadcasts.progress(job_id, 10, 0)
        body = self.page(f"/broadcast/status/{job_id}")
        self.assertIn("The broadcast is running", body)
        self.assertIn("by mail, with /broadcast", body)
        self.assertIn("To the broadcasts", body)
        self.assertNotIn("Not delivered", body)

    def test_the_status_page_names_who_was_not_reached_and_counts_the_rest(self):
        job_id = broadcasts.start("s", "m", 200)
        for number in range(broadcasts.FAILED_KEPT + 4):
            broadcasts.progress(job_id, 0, number + 1, f"user{number}@example.com")
        broadcasts.finish(job_id)
        body = self.page(f"/broadcast/status/{job_id}")
        self.assertIn("user0@example.com", body)
        self.assertIn("and 4 more", body)

    def test_the_menu_says_so_on_every_page_while_one_is_running(self):
        self.assertNotIn("nav-dot", self.page("/logs").replace(".nav-dot", ""))
        job_id = broadcasts.start("s", "m", 2)
        self.assertIn('class="odot on nav-dot"', self.page("/logs"))
        broadcasts.finish(job_id)
        self.assertNotIn('class="odot on nav-dot"', self.page("/logs"))


if __name__ == "__main__":
    unittest.main()
