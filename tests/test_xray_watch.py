"""
The push that says Xray has stopped, and the one that says it is back.

Xray not running is every client disconnected at once, and 3x-ui says so on a
page somebody has to open. The bot asks once a minute and says through Gotify
what has changed.

What can go wrong is, again, saying too much or too little. Too much: a
notification a minute for as long as Xray stays down; one on every restart of
the bot to say that all is well; a failure and a recovery announced because a
look landed in the second 3x-ui takes to restart Xray; a panel that did not
answer reported as Xray having stopped. Too little: a stop that is real and
goes unmentioned.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import logging
import unittest

import config
import mailer
import xray_watch


class Panel:
    """Stands in for the 3x-ui client: answers with whatever it was last told to."""

    def __init__(self):
        self.answer = self.status("running")
        self.asked = 0
        self.quiet = []

    @staticmethod
    def status(state, error=""):
        return {"xray_state": state, "xray_error": error, "xray_version": "25.8.3"}

    def get_server_status(self, quiet=False):
        self.asked += 1
        self.quiet.append(quiet)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Watching(unittest.TestCase):
    KEYS = ("GOTIFY_URL", "GOTIFY_TOKEN", "XRAY_NOTIFY_ENABLED", "XRAY_NOTIFY_TITLE",
            "XRAY_NOTIFY_MESSAGE", "SERVICE_NAME", "PANEL_LANG")

    def setUp(self):
        self.saved = {key: getattr(config, key) for key in self.KEYS}
        config.GOTIFY_URL, config.GOTIFY_TOKEN = "https://push.example.com", "token"
        config.XRAY_NOTIFY_ENABLED = True
        config.XRAY_NOTIFY_TITLE = "Xray · {service}"
        config.XRAY_NOTIFY_MESSAGE = "{status}"
        config.SERVICE_NAME = "Aurora"
        config.PANEL_LANG = "en"

        self.panel = Panel()
        self.real = (xray_watch.get_shared_client, mailer.send_gotify_notification)
        xray_watch.get_shared_client = lambda: self.panel
        self.pushed = []
        mailer.send_gotify_notification = lambda title, message: self.pushed.append((title, message))
        xray_watch._reset()

    def tearDown(self):
        xray_watch.get_shared_client, mailer.send_gotify_notification = self.real
        for key, value in self.saved.items():
            setattr(config, key, value)
        xray_watch._reset()

    def look(self, state=None, error="", times=1):
        """`times` looks with the panel answering `state`; None is a panel that does not answer."""
        self.panel.answer = None if state is None else Panel.status(state, error)
        for _ in range(times):
            xray_watch.check()

    def texts(self):
        return [message for _, message in self.pushed]


class WhatIsSaid(Watching):
    def test_xray_running_when_the_bot_starts_is_not_news(self):
        self.look("running", times=5)
        self.assertEqual(self.pushed, [])

    def test_stopped_and_back_is_two_notifications_and_no_more(self):
        self.look("running")
        self.look("stop", times=10)
        self.look("running", times=10)
        self.assertEqual(self.texts(), ["Xray has stopped", "Xray is running again"])
        self.assertEqual(self.pushed[0][0], "Xray · Aurora")

    def test_not_one_a_minute_while_it_stays_down(self):
        self.look("running")
        self.look("stop", times=120)
        self.assertEqual(len(self.pushed), 1)

    def test_a_failure_carries_what_the_panel_says_went_wrong(self):
        self.look("running")
        self.look("error", error="failed to start: listen tcp :443: bind: address already in use",
                  times=2)
        self.assertEqual(self.texts(),
                         ["Xray has failed: failed to start: listen tcp :443: bind: "
                          "address already in use"])

    def test_down_when_the_bot_starts_is_said(self):
        # A restart forgets what was said. Saying it again is the better of
        # the two mistakes.
        self.look("stop", times=2)
        self.assertEqual(self.texts(), ["Xray has stopped"])

    def test_a_stop_that_turns_into_a_failure_is_said_as_one(self):
        self.look("running")
        self.look("stop", times=2)
        self.look("error", error="config is broken", times=2)
        self.assertEqual(self.texts(), ["Xray has stopped", "Xray has failed: config is broken"])

    def test_a_state_this_code_has_not_met_is_said_as_the_panel_names_it(self):
        self.look("running")
        self.look("Updating", times=2)
        self.assertEqual(self.texts(), ["Xray is not running (updating)"])

    def test_a_panel_that_says_nothing_about_xray_raises_no_alarm(self):
        self.look("running")
        self.look("", times=5)
        self.assertEqual(self.pushed, [])


class ARestartIsNotAFault(Watching):
    """
    3x-ui restarts Xray whenever its configuration changes, which adding a
    client does. A look that lands in that second must not announce a failure
    and, a minute later, a recovery.
    """

    def test_a_stop_seen_once_is_not_believed(self):
        self.look("running")
        self.look("stop")
        self.look("running")
        self.assertEqual(self.pushed, [])

    def test_seen_twice_running_it_is(self):
        self.look("running")
        self.look("stop", times=xray_watch.LOOKS_TO_BELIEVE)
        self.assertEqual(self.texts(), ["Xray has stopped"])

    def test_two_single_stops_with_running_between_are_still_nothing(self):
        self.look("running")
        for _ in range(4):
            self.look("stop")
            self.look("running")
        self.assertEqual(self.pushed, [])

    def test_a_xray_that_keeps_failing_differently_is_still_down(self):
        # Restarted by 3x-ui, failing again: stopped, failed, stopped, failed.
        # Counted by state, it was never the same thing twice running — and
        # was never announced at all.
        self.look("running")
        self.look("stop")
        self.look("error", error="boom")
        self.assertEqual(self.texts(), ["Xray has failed: boom"])

    def test_and_is_then_not_announced_on_every_turn_of_it(self):
        self.look("running")
        for _ in range(30):
            self.look("stop")
            self.look("error", error="boom")
        self.assertEqual(len(self.pushed), 1)

    def test_good_news_is_believed_at_once(self):
        self.look("running")
        self.look("stop", times=2)
        self.look("running")
        self.assertEqual(self.texts()[-1], "Xray is running again")


class ThePanelNotAnswering(Watching):
    """Not the same thing as Xray having stopped, and not said as if it were."""

    def test_a_look_or_two_unanswered_is_nothing(self):
        self.look("running")
        self.look(None, times=xray_watch.SILENT_LOOKS - 1)
        self.look("running")
        self.assertEqual(self.pushed, [])

    def test_several_in_a_row_is_said_in_its_own_words(self):
        self.look("running")
        self.look(None, times=xray_watch.SILENT_LOOKS)
        self.assertEqual(self.texts(),
                         ["3x-ui does not answer, so whether Xray is running is not known"])
        self.assertNotIn("stopped", self.texts()[0])

    def test_and_once_however_long_it_lasts(self):
        self.look("running")
        self.look(None, times=200)
        self.assertEqual(len(self.pushed), 1)

    def test_when_it_is_back_that_is_said_too(self):
        self.look("running")
        self.look(None, times=5)
        self.look("running")
        self.assertEqual(self.texts()[-1], "3x-ui answers again, and Xray is running")

    def test_back_with_xray_down_is_said_as_that_and_not_as_a_stop_just_now(self):
        # When it stopped is exactly what could not be seen.
        self.look("running")
        self.look(None, times=5)
        self.look("stop", times=2)
        self.assertEqual(self.texts()[-1], "3x-ui answers again, but Xray is not running (stop)")

    def test_down_before_the_silence_and_down_after_it_is_not_a_second_stop(self):
        self.look("stop", times=2)
        self.look(None, times=3)
        self.look("stop", times=2)
        self.assertEqual(self.texts(), [
            "Xray has stopped",
            "3x-ui does not answer, so whether Xray is running is not known",
            "3x-ui answers again, but Xray is not running (stop)"])

    def test_back_and_saying_nothing_about_xray_is_still_back(self):
        # Announced as away, it is owed the news that it is not.
        self.look("running")
        self.look(None, times=3)
        self.look("", times=3)
        self.assertEqual(self.texts()[-1], "3x-ui answers again")
        self.assertEqual(len(self.pushed), 2)

    def test_a_stop_is_not_confirmed_across_a_silence(self):
        # Seen once, then nothing heard: the second sighting has to be a
        # second sighting, not the first one remembered.
        self.look("running")
        self.look("stop")
        self.look(None)
        self.look("stop")
        self.assertEqual(self.pushed, [])
        self.look("stop")
        self.assertEqual(self.texts(), ["Xray has stopped"])

    def test_a_client_that_raises_is_a_panel_that_did_not_answer(self):
        self.look("running")
        self.panel.answer = RuntimeError("boom")
        for _ in range(xray_watch.SILENT_LOOKS):
            xray_watch.check()
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("does not answer", self.texts()[0])

    def test_the_client_is_asked_not_to_warn_about_it_every_minute(self):
        self.look(None, times=3)
        self.assertEqual(self.panel.quiet, [True, True, True])


class TheSwitchAndTheWords(Watching):
    def test_switched_off_the_panel_is_not_even_asked(self):
        config.XRAY_NOTIFY_ENABLED = False
        self.look("stop", times=5)
        self.assertEqual((self.panel.asked, self.pushed), (0, []))

    def test_nor_with_nowhere_to_push(self):
        for key in ("GOTIFY_URL", "GOTIFY_TOKEN"):
            with self.subTest(missing=key):
                saved = getattr(config, key)
                setattr(config, key, "")
                try:
                    self.look("stop", times=5)
                finally:
                    setattr(config, key, saved)
                self.assertEqual(self.panel.asked, 0)

    def test_switched_on_again_it_starts_from_what_it_finds(self):
        # What was true a week ago is not a thing to compare with.
        self.look("running")
        self.look("stop", times=2)
        config.XRAY_NOTIFY_ENABLED = False
        self.look("stop")
        config.XRAY_NOTIFY_ENABLED = True
        self.pushed.clear()
        self.look("running", times=3)
        self.assertEqual(self.pushed, [], "running, on a first look, is not news")

    def test_the_title_and_the_text_are_the_administrators(self):
        config.XRAY_NOTIFY_TITLE = "[{service}] {state}"
        config.XRAY_NOTIFY_MESSAGE = "{status} / {error} / {state}"
        self.look("running")
        self.look("error", error="boom", times=2)
        self.assertEqual(self.pushed, [("[Aurora] error", "Xray has failed: boom / boom / error")])

    def test_a_stray_brace_in_a_setting_does_not_cost_the_notification(self):
        config.XRAY_NOTIFY_MESSAGE = "{status} {nothing} {"
        self.look("running")
        self.look("stop", times=2)
        self.assertEqual(len(self.pushed), 1)

    def test_nor_does_a_setting_that_parses_and_then_asks_for_what_is_not_there(self):
        # {status.x} is a legal format string; it raises when it is filled in,
        # and not one of the errors a malformed one raises.
        for template in ("{status.x}", "{status[a]}", "{service!z}", "{error:>q}"):
            with self.subTest(template=template):
                xray_watch._reset()
                self.pushed.clear()
                config.XRAY_NOTIFY_TITLE = config.XRAY_NOTIFY_MESSAGE = template
                self.look("running")
                self.look("stop", times=2)
                self.look("running")
                self.assertEqual(len(self.pushed), 2)

    def test_a_brace_in_what_the_panel_says_is_only_a_brace(self):
        self.look("running")
        self.look("error", error="unexpected token { at {service}", times=2)
        self.assertEqual(self.texts(), ["Xray has failed: unexpected token { at {service}"])

    def test_the_words_follow_the_panels_language(self):
        config.PANEL_LANG = "ru"
        self.look("running")
        self.look("stop", times=2)
        self.look("running")
        self.assertEqual(self.texts(), ["Xray остановлен", "Xray снова работает"])

    def test_the_log_is_told_as_well(self):
        seen = []
        watcher = logging.Handler()
        watcher.emit = seen.append
        log = logging.getLogger("xray_watch")
        was = log.level
        # The recovery is said at INFO, which a test run does not otherwise let through.
        log.setLevel(logging.DEBUG)
        log.addHandler(watcher)
        try:
            self.look("running")
            self.look("stop", times=2)
            self.look("running")
        finally:
            log.removeHandler(watcher)
            log.setLevel(was)
        levels = [(record.levelno, record.getMessage()) for record in seen]
        self.assertEqual([level for level, _ in levels], [logging.WARNING, logging.INFO])
        self.assertIn("state 'stop'", levels[0][1])

    def test_a_push_that_fails_is_not_tried_again_every_minute(self):
        # What was said — or tried — is remembered before it is sent.
        def failing(title, message):
            self.pushed.append((title, message))
            raise RuntimeError("Gotify is down")

        mailer.send_gotify_notification = failing
        self.look("running")
        for _ in range(5):
            self.panel.answer = Panel.status("stop")
            try:
                xray_watch.check()
            except RuntimeError:
                pass
        self.assertEqual(len(self.pushed), 1)

    def test_and_what_happened_is_in_the_log_even_so(self):
        def failing(title, message):
            raise RuntimeError("Gotify is down")

        mailer.send_gotify_notification = failing
        seen = []
        watcher = logging.Handler()
        watcher.emit = seen.append
        logging.getLogger("xray_watch").addHandler(watcher)
        try:
            self.look("running")
            self.look("stop", times=2)          # must not raise into the bot loop
        finally:
            logging.getLogger("xray_watch").removeHandler(watcher)
        said = [record.getMessage() for record in seen]
        self.assertTrue(any("Xray is not running" in line for line in said))
        self.assertTrue(any("could not be sent" in line for line in said))
        self.assertFalse(any("a notification was sent" in line for line in said))


class TheSchedule(Watching):
    def test_it_asks_once_a_minute_however_often_the_loop_turns(self):
        for second in range(0, 200, 5):
            xray_watch.run_if_due(now=1000.0 + second)
        # 0, 60, 120, 180
        self.assertEqual(self.panel.asked, 4)

    def test_a_clock_put_back_does_not_stop_the_asking(self):
        # A server whose time was corrected an hour back asked nothing for
        # that hour.
        xray_watch.run_if_due(now=5000.0)
        xray_watch.run_if_due(now=1400.0)
        self.assertEqual(self.panel.asked, 2)

    def test_the_first_time_is_whenever_it_is_called(self):
        xray_watch.run_if_due(now=0.0)
        xray_watch.run_if_due(now=1.0)
        self.assertEqual(self.panel.asked, 1)


if __name__ == "__main__":
    unittest.main()
