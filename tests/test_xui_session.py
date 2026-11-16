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


if __name__ == "__main__":
    unittest.main()
