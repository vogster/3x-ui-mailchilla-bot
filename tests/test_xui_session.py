"""
Telling an expired 3x-ui session from an address, and an outage from "no".

Two mistakes of the same family, both invisible until somebody is bitten. The
client took any URL with "login" in it for the sign-in page — and the client's
address is part of the URL, so everybody called Loginov was a session that had
expired. And a panel that could not be reached read as a panel that knew no
such client, so a registered person asking for their status during an outage
was told they had never registered.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

import requests

from xui_client import XuiClient, XuiUnavailable

BASE = "http://panel.example:2053"
LOGINOV = "/panel/api/clients/traffic/loginov%40mail.ru"


class FakeResponse:
    def __init__(self, url, status=200, history=(), payload=None):
        self.url = url
        self.status_code = status
        self.history = list(history)
        self.text = ""
        self._payload = payload if payload is not None else {"success": True, "obj": {}}

    def json(self):
        return self._payload


class FakeSession:
    """Hands back the prepared responses in order and remembers what was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("LOGIN", url))
        return FakeResponse(url, payload={"success": True})


def client(responses, token=""):
    xui = XuiClient()
    xui.base_url = BASE
    xui.api_token = token
    xui.is_logged_in = True
    xui.session = FakeSession(responses)
    return xui


class LoginPage(unittest.TestCase):
    def test_an_address_with_login_in_it_is_not_the_sign_in_page(self):
        xui = client([FakeResponse(BASE + LOGINOV)], token="secret")
        # Used to raise "Access denied ... Check XUI_API_TOKEN".
        response = xui._request("GET", LOGINOV)
        self.assertEqual(response.status_code, 200)

    def test_nor_does_it_sign_in_again_on_a_cookie_session(self):
        xui = client([FakeResponse(BASE + LOGINOV)])
        xui._request("GET", LOGINOV)
        self.assertEqual(xui.session.calls, [("GET", BASE + LOGINOV)])

    def test_a_redirect_to_the_sign_in_page_still_signs_in_again(self):
        bounced = FakeResponse(BASE + "/login", history=[FakeResponse(BASE + LOGINOV, 302)])
        xui = client([bounced, FakeResponse(BASE + LOGINOV)])
        xui._request("GET", LOGINOV)
        self.assertEqual([call[0] for call in xui.session.calls], ["GET", "LOGIN", "GET"])

    def test_and_with_a_token_it_is_still_a_refusal(self):
        bounced = FakeResponse(BASE + "/login", history=[FakeResponse(BASE + LOGINOV, 302)])
        xui = client([bounced], token="secret")
        with self.assertRaises(Exception):
            xui._request("GET", LOGINOV)

    def test_a_redirect_to_the_same_page_elsewhere_is_not_the_sign_in_page(self):
        # http to https, say: a redirect happened and "login" is in the URL,
        # yet it is the page that was asked for.
        moved = FakeResponse("https://panel.example:2053" + LOGINOV,
                             history=[FakeResponse(BASE + LOGINOV, 301)])
        xui = client([moved], token="secret")
        self.assertEqual(xui._request("GET", LOGINOV).status_code, 200)


class Lookup(unittest.TestCase):
    def setUp(self):
        self.xui = client([])

    def test_an_outage_is_not_an_answer_when_asked_strictly(self):
        self.xui.get_all_clients = lambda: None
        with self.assertRaises(XuiUnavailable):
            self.xui.find_client_by_email("ben@example.com", strict=True)

    def test_an_empty_panel_is_an_answer(self):
        self.xui.get_all_clients = lambda: []
        self.assertIsNone(self.xui.find_client_by_email("ben@example.com", strict=True))

    def test_the_panel_pages_still_get_none(self):
        self.xui.get_all_clients = lambda: None
        self.assertIsNone(self.xui.find_client_by_email("ben@example.com"))

    def test_a_panel_that_answers_null_for_no_clients_is_empty_not_down(self):
        # Go writes an empty list as null as often as not.
        xui = client([FakeResponse(BASE + "/panel/api/clients/list",
                                   payload={"success": True, "obj": None})], token="secret")
        self.assertEqual(xui.get_all_clients(), [])
        xui.session.responses.append(FakeResponse(BASE + "/panel/api/clients/list",
                                                  payload={"success": True, "obj": None}))
        self.assertIsNone(xui.find_client_by_email("ben@example.com", strict=True))

    def test_a_client_that_is_there_is_found(self):
        self.xui.get_all_clients = lambda: [{"email": "ben@example.com", "subId": "s"}]
        found = self.xui.find_client_by_email("Ben@Example.com", strict=True)
        self.assertEqual(found["subId"], "s")



class Flaky:
    """A session that answers with whatever it was given, in order; an exception is raised."""

    def __init__(self, *answers, login=None):
        self.answers = list(answers)
        self.login_answer = login
        self.calls = 0
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, url, **kwargs):
        if isinstance(self.login_answer, Exception):
            raise self.login_answer
        return self.login_answer


LIST = "/panel/api/clients/list"


def with_session(session, token="token"):
    xui = XuiClient()
    xui.base_url = BASE
    xui.api_token = token
    xui.is_logged_in = bool(token)
    xui.session = session
    return xui


class AnOutageIsAskedAboutOnce(unittest.TestCase):
    """
    A dead panel costs ten seconds a request. With twenty unread letters that
    was twenty of them in a row, each to learn what the first had found out.
    """

    def test_the_first_failure_answers_for_the_lookups_right_after_it(self):
        session = Flaky(requests.ConnectionError("down"))
        xui = with_session(session)
        for _ in range(3):
            with self.assertRaises(XuiUnavailable):
                xui.find_client_by_email("ben@example.com", strict=True)
        self.assertEqual(session.calls, 1)

    def test_a_plain_lookup_is_never_answered_from_memory(self):
        # It answers None for "could not ask" — the same None as "no such
        # client" — and the panel's pages act on it. Held back, the page that
        # had just created a client read it back as never created.
        found = FakeResponse(BASE + LIST, payload={"success": True, "obj": [{"email": "ben@example.com"}]})
        session = Flaky(requests.ConnectionError("down"), found)
        xui = with_session(session)
        self.assertIsNone(xui.find_client_by_email("ben@example.com"))
        self.assertTrue(xui.recently_failed())
        self.assertEqual(xui.find_client_by_email("ben@example.com"), {"email": "ben@example.com"})
        self.assertEqual(session.calls, 2)

    def test_any_answer_from_the_panel_ends_the_hold(self):
        # A client was created a line ago: that request is itself the news
        # that the panel is back, and the read-back after it must ask.
        created = FakeResponse(BASE + "/panel/api/clients/add", payload={"success": True})
        found = FakeResponse(BASE + LIST, payload={"success": True, "obj": [{"email": "ben@example.com"}]})
        session = Flaky(requests.ConnectionError("down"), created, found)
        xui = with_session(session)
        with self.assertRaises(XuiUnavailable):
            xui.find_client_by_email("ben@example.com", strict=True)
        xui._request("POST", "/panel/api/clients/add")
        self.assertFalse(xui.recently_failed())
        self.assertEqual(xui.find_client_by_email("ben@example.com", strict=True),
                         {"email": "ben@example.com"})

    def test_a_little_later_it_is_asked_again(self):
        found = FakeResponse(BASE + LIST, payload={"success": True, "obj": [{"email": "ben@example.com"}]})
        session = Flaky(requests.ConnectionError("down"), found)
        xui = with_session(session)
        with self.assertRaises(XuiUnavailable):
            xui.find_client_by_email("ben@example.com", strict=True)
        xui._hold_until = 0.0
        self.assertEqual(xui.find_client_by_email("ben@example.com", strict=True),
                         {"email": "ben@example.com"})
        # And an answer clears what was remembered about the failure.
        self.assertIsNone(xui.last_failure)
        self.assertEqual(session.calls, 2)

    def test_asking_for_the_list_itself_is_never_held_back(self):
        # The hold is for lookups made on a letter's behalf; a page that wants
        # the list asks for it.
        session = Flaky(requests.ConnectionError("down"), requests.ConnectionError("down"))
        xui = with_session(session)
        self.assertIsNone(xui.get_all_clients())
        self.assertIsNone(xui.get_all_clients())
        self.assertEqual(session.calls, 2)


class RefusedIsNotAway(unittest.TestCase):
    """
    A panel that does not answer comes back by itself. One that refuses the
    bot's token never will, and has to be said in those words.
    """

    def unavailable(self, xui) -> XuiUnavailable:
        with self.assertRaises(XuiUnavailable) as caught:
            xui.find_client_by_email("ben@example.com", strict=True)
        return caught.exception

    def test_a_panel_that_does_not_answer(self):
        error = self.unavailable(with_session(Flaky(requests.ConnectTimeout("slow"))))
        self.assertFalse(error.denied)
        self.assertIn("does not answer", str(error))
        self.assertIn("ConnectTimeout", str(error))

    def test_a_token_the_panel_will_not_take(self):
        for status in (401, 403):
            with self.subTest(status=status):
                error = self.unavailable(with_session(Flaky(FakeResponse(BASE + LIST, status))))
                self.assertTrue(error.denied)
                self.assertIn("XUI_API_TOKEN", str(error))

    def test_a_password_the_panel_will_not_take(self):
        refused = FakeResponse(BASE + "/login", payload={"success": False, "msg": "wrong password"})
        error = self.unavailable(with_session(Flaky(login=refused), token=""))
        self.assertTrue(error.denied)
        self.assertIn("XUI_USERNAME", str(error))

    def test_a_sign_in_that_did_not_get_through_is_only_away(self):
        error = self.unavailable(with_session(Flaky(login=requests.ConnectionError("down")), token=""))
        self.assertFalse(error.denied)

    def test_a_session_that_expired_and_cannot_be_renewed_with_this_password(self):
        refused = FakeResponse(BASE + "/login", payload={"success": False, "msg": "wrong password"})
        xui = with_session(Flaky(FakeResponse(BASE + LIST, 401), login=refused), token="")
        xui.is_logged_in = True
        self.assertTrue(self.unavailable(xui).denied)

    def test_a_proxy_in_front_of_the_sign_in_is_not_a_wrong_password(self):
        # A bare 403 on /login may be a rate limiter or a firewall, and passes.
        # Only the panel's own "no" is a refusal.
        blocked = FakeResponse(BASE + "/login", 403)
        error = self.unavailable(with_session(Flaky(login=blocked), token=""))
        self.assertFalse(error.denied)

    def test_a_sign_in_that_did_not_get_through_reads_as_not_answering(self):
        error = self.unavailable(with_session(Flaky(login=requests.ConnectTimeout("slow")), token=""))
        self.assertIn("does not answer", str(error))
        self.assertIn("ConnectionError", str(error))

    def test_turned_away_straight_after_signing_in_is_a_refusal(self):
        # Not a session that lapsed: it was renewed a moment ago. A panel that
        # has started asking cookie sessions for a CSRF token looks like this.
        fine = FakeResponse(BASE + "/login", payload={"success": True})
        xui = with_session(Flaky(FakeResponse(BASE + LIST, 401), FakeResponse(BASE + LIST, 401),
                                 login=fine), token="")
        xui.is_logged_in = True
        error = self.unavailable(xui)
        self.assertTrue(error.denied)
        self.assertIn("XUI_API_TOKEN", str(error))

    def test_a_panel_that_answers_with_an_error_is_not_a_refusal(self):
        error = self.unavailable(with_session(Flaky(FakeResponse(BASE + LIST, 500))))
        self.assertFalse(error.denied)
        self.assertIn("status 500", str(error))

    def test_the_bot_passes_the_difference_on_to_the_loop(self):
        import email_bot
        import inbox
        real = email_bot.process_message
        try:
            for denied in (False, True):
                with self.subTest(denied=denied):
                    def raising(*args, **kwargs):
                        raise XuiUnavailable("3x-ui refuses the bot's credentials: x", denied=denied)

                    email_bot.process_message = raising
                    with self.assertRaises(inbox.TryLater) as caught:
                        email_bot._handle_letter(1, "ben@example.com", "/status", "", None)
                    self.assertEqual(caught.exception.needs_somebody, denied)
                    self.assertIn("refuses the bot's credentials", str(caught.exception))
        finally:
            email_bot.process_message = real



class AskedQuietly(unittest.TestCase):
    """
    xray_watch asks for the server's status once a minute. A panel that is
    away for an hour must not be sixty errors in the log saying so: whoever is
    watching says it once.
    """

    def said(self, xui, quiet):
        seen = []
        watcher = logging.Handler()
        watcher.emit = seen.append
        logging.getLogger("xui_client").addHandler(watcher)
        try:
            self.assertIsNone(xui.get_server_status(quiet=quiet))
        finally:
            logging.getLogger("xui_client").removeHandler(watcher)
        return [record for record in seen if record.levelno >= logging.WARNING]

    def cases(self):
        refused = FakeResponse(BASE + "/login", payload={"success": False, "msg": "wrong password"})
        return {
            "the panel is not there": lambda: with_session(Flaky(requests.ConnectionError("down"))),
            "it answers with an error": lambda: with_session(Flaky(FakeResponse(BASE + "/x", 502))),
            "it says the request failed": lambda: with_session(Flaky(
                FakeResponse(BASE + "/x", payload={"success": False, "msg": "busy"}))),
            "the sign-in is refused": lambda: with_session(Flaky(login=refused), token=""),
            "the sign-in does not get through": lambda: with_session(
                Flaky(login=requests.ConnectTimeout("slow")), token=""),
        }

    def test_asked_quietly_nothing_is_said_above_debug(self):
        for name, make in self.cases().items():
            with self.subTest(case=name):
                self.assertEqual(self.said(make(), quiet=True), [])

    def test_asked_the_ordinary_way_it_still_is(self):
        # The dashboard asks this way, and a panel that is away is news there.
        for name, make in self.cases().items():
            with self.subTest(case=name):
                self.assertTrue(self.said(make(), quiet=False))


if __name__ == "__main__":
    unittest.main()
