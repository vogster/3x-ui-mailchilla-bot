"""
The last-online column: when the panel was last in touch with a client.

Two things here are easy to get wrong and invisible when they are. The panel's
timestamps arrive in seconds from one build and milliseconds from another, and a
row misread by a factor of a thousand lands in 1970 rather than showing an
error. And the wording has to stay short enough for a table cell while a client
nobody has ever seen must read as a dash, not as "just now".
"""
import unittest

import config
import i18n
from admin.rows import DAY_MS, HOUR_MS, MINUTE_MS, client_row, _fmt_last_seen
from xui_client import epoch_ms

NOW = 1_770_000_000_000


class EpochMs(unittest.TestCase):
    def test_seconds_become_milliseconds(self):
        self.assertEqual(epoch_ms(1_700_000_000), 1_700_000_000_000)

    def test_milliseconds_are_left_alone(self):
        self.assertEqual(epoch_ms(1_700_000_000_000), 1_700_000_000_000)

    def test_nothing_is_zero(self):
        # A client the panel has never seen, and everything else that is not a
        # timestamp: the row shows a dash rather than the epoch.
        for empty in (0, -1, None, "", "nonsense", []):
            self.assertEqual(epoch_ms(empty), 0)

    def test_a_numeric_string_still_counts(self):
        self.assertEqual(epoch_ms("1700000000"), 1_700_000_000_000)


class FormatLastSeen(unittest.TestCase):
    def setUp(self):
        self.lang = config.PANEL_LANG
        config.PANEL_LANG = "en"

    def tearDown(self):
        config.PANEL_LANG = self.lang

    def fmt(self, ago_ms):
        return _fmt_last_seen(NOW - ago_ms, NOW)

    def test_never_seen(self):
        self.assertEqual(_fmt_last_seen(0, NOW), "—")

    def test_seconds(self):
        self.assertEqual(self.fmt(20 * 1000), "just now")

    def test_minutes(self):
        self.assertEqual(self.fmt(5 * MINUTE_MS), "5 min ago")

    def test_hours(self):
        self.assertEqual(self.fmt(3 * HOUR_MS), "3 h ago")

    def test_days(self):
        self.assertEqual(self.fmt(2 * DAY_MS), "2 d ago")

    def test_past_a_week_it_is_a_date(self):
        # Eight days back the elapsed figure says nothing a date does not say
        # better, and the cell has the room for one.
        self.assertRegex(self.fmt(8 * DAY_MS), r"^\d{2}\.\d{2}\.\d{4}$")

    def test_a_clock_ahead_of_the_panel_is_not_the_future(self):
        # The server's own clock and the panel's need not agree to the second,
        # and "in −3 minutes" would be a strange thing to read.
        self.assertEqual(_fmt_last_seen(NOW + 3 * MINUTE_MS, NOW), "just now")

    def test_russian_units_do_not_decline(self):
        config.PANEL_LANG = "ru"
        self.assertEqual(self.fmt(2 * MINUTE_MS), "2 мин назад")
        self.assertEqual(self.fmt(5 * MINUTE_MS), "5 мин назад")


class RowLastSeen(unittest.TestCase):
    """What the row carries, which is what the table sorts and draws by."""

    def row(self, online=None, last_online=None):
        client = {"email": "user@example.com", "id": 1, "enable": True}
        return client_row(client, online, last_online)

    def test_a_connected_client_is_seen_now(self):
        # The heartbeat map lags behind the connection, so somebody the panel
        # reports as online says "now" whatever the map has for them.
        row = self.row(online={"user@example.com"},
                       last_online={"user@example.com": NOW - 3 * HOUR_MS})
        self.assertEqual(row["last_seen"], i18n.t("now [last seen]"))
        self.assertGreater(row["last_seen_ms"], NOW)

    def test_an_unknown_client_carries_a_dash_and_sorts_first(self):
        row = self.row(online=set(), last_online={})
        self.assertEqual(row["last_seen"], "—")
        self.assertEqual(row["last_seen_ms"], 0)
        self.assertEqual(row["last_seen_full"], "")

    def test_nobody_asked(self):
        # Neither map was fetched: the column is not drawn at all, and the row
        # must not claim the client has never been seen either way.
        row = self.row()
        self.assertIsNone(row["online"])
        self.assertEqual(row["last_seen_ms"], 0)
