"""
One sign-in to SMTP for a whole broadcast, and what happens when the line drops.

Every letter used to open a connection, sign in, send and hang up — a sign-in
for each recipient of a broadcast, which is slow and which a mail server may
count against the account. mailer.SmtpSession keeps one connection for the
run. What cannot be tried against a real server here is tried against one that
is scripted: a connection that dies on the third letter, a server that says it
is closing the channel, a recipient it will not take.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import email
import smtplib
import socket
import ssl
import unittest

import broadcasts
import config
import email_bot
import mailer


class Server:
    """A connection to a scripted SMTP server; remembers everything asked of it."""

    def __init__(self, world):
        self.world = world
        self.number = len(world.servers) + 1
        self.logged_in = False
        self.sent = []          # (envelope, recipients, text)
        self.quit_called = False
        self.closed = False

    def login(self, user, password):
        if self.world.refuse_login:
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")
        self.logged_in = True

    def sendmail(self, envelope, recipients, text):
        self.world.attempts += 1
        failure = self.world.failures.pop(self.world.attempts, None)
        if failure is not None:
            raise failure
        if self.closed:
            raise smtplib.SMTPServerDisconnected("please run connect() first")
        self.sent.append((envelope, list(recipients), text))

    def quit(self):
        self.quit_called = True
        self.closed = True

    def close(self):
        self.closed = True


class World:
    """Stands in for mailer.open_smtp: every call is a new connection, and they are counted."""

    def __init__(self):
        self.servers = []
        self.attempts = 0
        # attempt number (1-based, over every connection) -> the exception that attempt raises
        self.failures = {}
        self.refuse_login = False
        self.refuse_connections = 0
        self.dials = 0          # every attempt to connect, successful or not

    def __call__(self, host, port, timeout=10):
        self.dials += 1
        if self.refuse_connections:
            self.refuse_connections -= 1
            raise ConnectionRefusedError("connection refused")
        server = Server(self)
        self.servers.append(server)
        return server

    def delivered(self) -> list:
        """Who got a letter, in order, across every connection."""
        return [recipients[0] for server in self.servers for _, recipients, _ in server.sent]


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.saved = (mailer.open_smtp, mailer._keep_copy, config.SMTP_SERVER, config.SMTP_PORT,
                      config.SMTP_USER, config.SMTP_PASSWORD)
        mailer.open_smtp = self.world
        # The copy for the Sent folder is another module's business and goes
        # off on a thread of its own.
        mailer._keep_copy = lambda msg: None
        config.SMTP_SERVER, config.SMTP_PORT = "smtp.example.com", 465
        config.SMTP_USER, config.SMTP_PASSWORD = "Aurora <bot@example.com>", "secret"

    def tearDown(self):
        (mailer.open_smtp, mailer._keep_copy, config.SMTP_SERVER, config.SMTP_PORT,
         config.SMTP_USER, config.SMTP_PASSWORD) = self.saved

    @staticmethod
    def addresses(count):
        return [f"user{n}@example.com" for n in range(1, count + 1)]

    def send(self, session, to):
        mailer.send_email_reply(to, "Maintenance", "<p>Tonight.</p>", session=session)


class OneSignInForTheRun(SessionCase):
    def test_twenty_letters_take_one_connection(self):
        with mailer.SmtpSession() as session:
            for to in self.addresses(20):
                self.send(session, to)
            self.assertEqual(session.connections, 1)
        (server,) = self.world.servers
        self.assertTrue(server.logged_in)
        self.assertEqual(self.world.delivered(), self.addresses(20))
        # And the goodbye is said once, when the run is over.
        self.assertTrue(server.quit_called)

    def test_without_a_session_every_letter_still_gets_its_own(self):
        # A registration, a status: one letter, sent and done with.
        for to in self.addresses(3):
            mailer.send_email_reply(to, "Welcome", "<p>hi</p>")
        self.assertEqual(len(self.world.servers), 3)
        self.assertTrue(all(server.quit_called for server in self.world.servers))

    def test_each_letter_is_to_one_address_and_names_no_other(self):
        # One connection must not become one letter: nobody's address is
        # anybody else's business.
        with mailer.SmtpSession() as session:
            for to in self.addresses(4):
                self.send(session, to)
        for envelope, recipients, text in self.world.servers[0].sent:
            self.assertEqual(envelope, "bot@example.com")
            self.assertEqual(len(recipients), 1)
            message = email.message_from_string(text)
            self.assertEqual(message["To"], recipients[0])
            for other in self.addresses(4):
                if other != recipients[0]:
                    self.assertNotIn(other, text)

    def test_nothing_is_opened_until_there_is_a_letter(self):
        with mailer.SmtpSession():
            pass
        self.assertEqual(self.world.servers, [])

    def test_the_connection_is_closed_however_the_run_ends(self):
        with self.assertRaises(RuntimeError):
            with mailer.SmtpSession() as session:
                self.send(session, "a@example.com")
                raise RuntimeError("the run broke off")
        self.assertTrue(self.world.servers[0].quit_called)

    def test_a_long_run_changes_connections_before_the_server_makes_it(self):
        with mailer.SmtpSession() as session:
            session.LETTERS_PER_CONNECTION = 3
            for to in self.addresses(7):
                self.send(session, to)
            self.assertEqual(session.connections, 3)
        self.assertEqual([len(server.sent) for server in self.world.servers], [3, 3, 1])
        self.assertTrue(all(server.quit_called for server in self.world.servers))
        self.assertEqual(self.world.delivered(), self.addresses(7))


class WhenTheLineDrops(SessionCase):
    def run_of(self, count, session=None):
        """Sends to `count` addresses; returns the ones that could not be sent to."""
        failed = []
        with (session or mailer.SmtpSession()) as live:
            self.session = live
            for to in self.addresses(count):
                try:
                    self.send(live, to)
                except Exception:
                    failed.append(to)
        return failed

    def test_the_letter_is_sent_again_on_a_new_connection_and_the_run_goes_on(self):
        self.world.failures[3] = smtplib.SMTPServerDisconnected("connection unexpectedly closed")
        failed = self.run_of(6)
        self.assertEqual(failed, [])
        self.assertEqual(self.world.delivered(), self.addresses(6))
        self.assertEqual([len(server.sent) for server in self.world.servers], [2, 4])
        self.assertEqual(self.session.connections, 2)
        # The dead one is dropped, not said goodbye to: there is nobody there.
        self.assertFalse(self.world.servers[0].quit_called)
        self.assertTrue(self.world.servers[0].closed)

    def test_every_way_a_line_drops(self):
        for failure in (smtplib.SMTPServerDisconnected("closed"),
                        ConnectionResetError("reset by peer"),
                        BrokenPipeError("broken pipe"),
                        socket.timeout("timed out"),
                        TimeoutError("timed out"),
                        ssl.SSLError("bad record mac"),
                        smtplib.SMTPDataError(421, b"closing connection"),
                        smtplib.SMTPSenderRefused(421, b"too many messages", "bot@example.com"),
                        smtplib.SMTPRecipientsRefused({"user2@example.com": (421, b"try later")})):
            with self.subTest(failure=type(failure).__name__):
                self.world = World()
                mailer.open_smtp = self.world
                self.world.failures[2] = failure
                self.assertEqual(self.run_of(3), [])
                self.assertEqual(self.world.delivered(), self.addresses(3))
                self.assertEqual(len(self.world.servers), 2)

    def test_a_letter_the_server_will_not_take_is_not_the_lines_fault(self):
        # The answer would be the same on any connection, and this one is fine.
        for failure in (smtplib.SMTPRecipientsRefused({"user2@example.com": (550, b"no such user")}),
                        smtplib.SMTPDataError(554, b"message refused"),
                        smtplib.SMTPSenderRefused(553, b"sender refused", "bot@example.com")):
            with self.subTest(failure=type(failure).__name__):
                self.world = World()
                mailer.open_smtp = self.world
                self.world.failures[2] = failure
                self.assertEqual(self.run_of(4), ["user2@example.com"])
                self.assertEqual(self.world.delivered(),
                                 ["user1@example.com", "user3@example.com", "user4@example.com"])
                self.assertEqual(len(self.world.servers), 1)

    def test_a_letter_is_tried_twice_and_no_more(self):
        self.world.failures[2] = smtplib.SMTPServerDisconnected("closed")
        self.world.failures[3] = smtplib.SMTPServerDisconnected("closed again")
        failed = self.run_of(4)
        self.assertEqual(failed, ["user2@example.com"])
        # The rest of the run is not given up on with it.
        self.assertEqual(self.world.delivered(),
                         ["user1@example.com", "user3@example.com", "user4@example.com"])
        self.assertEqual(len(self.world.servers), 3)

    def test_a_server_that_cannot_be_reached_is_not_dialled_for_every_letter(self):
        # Ten seconds a try. A broadcast to two hundred used to be two hundred
        # tries — and, with a second try for each, four hundred.
        self.world.refuse_connections = 99
        self.assertEqual(self.run_of(20), self.addresses(20))
        self.assertEqual(self.world.dials, 1)

    def test_but_is_dialled_again_after_a_pause(self):
        self.world.refuse_connections = 1
        with mailer.SmtpSession() as session:
            with self.assertRaises(ConnectionRefusedError):
                self.send(session, "a@example.com")
            with self.assertRaises(ConnectionRefusedError):
                self.send(session, "b@example.com")
            self.assertEqual(self.world.dials, 1)
            session._retry_at = 0.0           # the pause has passed
            self.send(session, "c@example.com")
        self.assertEqual(self.world.delivered(), ["c@example.com"])
        self.assertEqual(self.world.dials, 2)

    def test_a_line_that_drops_and_cannot_be_got_back_costs_one_try(self):
        self.world.failures[2] = ConnectionResetError("reset by peer")
        with mailer.SmtpSession() as session:
            self.send(session, "a@example.com")
            self.world.refuse_connections = 99
            with self.assertRaises(ConnectionRefusedError):
                self.send(session, "b@example.com")
            with self.assertRaises(ConnectionRefusedError):
                self.send(session, "c@example.com")
        # The first connection, and one attempt at another.
        self.assertEqual(self.world.dials, 2)

    def test_a_password_the_server_will_not_take_is_tried_once_for_the_whole_run(self):
        # One sign-in a broadcast exists not to hammer the server with them,
        # and a wrong password hammered is how an account gets locked.
        self.world.refuse_login = True
        with mailer.SmtpSession() as session:
            for to in self.addresses(5):
                with self.assertRaises(smtplib.SMTPAuthenticationError):
                    self.send(session, to)
        (server,) = self.world.servers
        self.assertEqual(self.world.dials, 1)
        # And the half-open connection is not left for the server to time out on.
        self.assertTrue(server.closed)

    def test_what_counts_as_the_line(self):
        lost = mailer._connection_lost
        self.assertTrue(lost(smtplib.SMTPServerDisconnected("x")))
        self.assertTrue(lost(OSError("network is unreachable")))
        self.assertTrue(lost(smtplib.SMTPResponseException(421, b"closing")))
        self.assertFalse(lost(smtplib.SMTPResponseException(550, b"no")))
        self.assertFalse(lost(smtplib.SMTPAuthenticationError(535, b"no")))
        self.assertFalse(lost(smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no")})))
        # Every smtplib exception is an OSError; that alone must not make it one.
        self.assertFalse(lost(smtplib.SMTPException("something else")))
        self.assertFalse(lost(ValueError("not about the network")))


class ABroadcastUsesOne(SessionCase):
    def setUp(self):
        super().setUp()
        broadcasts._reset()
        self.real_sleep = email_bot.time.sleep
        email_bot.time.sleep = lambda seconds: None

    def tearDown(self):
        email_bot.time.sleep = self.real_sleep
        broadcasts._reset()
        super().tearDown()

    def test_twenty_five_recipients_one_sign_in(self):
        sent = email_bot.handle_broadcast("Tonight.", subject="Maintenance",
                                          emails=self.addresses(25))
        self.assertEqual(sent, 25)
        self.assertEqual(len(self.world.servers), 1)
        self.assertEqual(self.world.delivered(), self.addresses(25))
        (job,) = broadcasts.recent()
        self.assertEqual((job["sent"], job["failed"], job["done"]), (25, 0, True))

    def test_it_lives_through_the_line_dropping_half_way(self):
        self.world.failures[10] = ConnectionResetError("reset by peer")
        sent = email_bot.handle_broadcast("Tonight.", subject="Maintenance",
                                          emails=self.addresses(25))
        self.assertEqual(sent, 25)
        self.assertEqual(len(self.world.servers), 2)
        self.assertEqual(self.world.delivered(), self.addresses(25))

    def test_a_recipient_the_server_refuses_is_counted_and_the_rest_go_out(self):
        self.world.failures[3] = smtplib.SMTPRecipientsRefused(
            {"user3@example.com": (550, b"no such user")})
        sent = email_bot.handle_broadcast("Tonight.", subject="Maintenance",
                                          emails=self.addresses(5))
        self.assertEqual(sent, 4)
        (job,) = broadcasts.recent()
        self.assertEqual(job["failed_addresses"], ["user3@example.com"])
        self.assertEqual(len(self.world.servers), 1)

    def test_the_pause_between_letters_is_kept(self):
        pauses = []
        email_bot.time.sleep = pauses.append
        email_bot.handle_broadcast("Tonight.", subject="Maintenance", emails=self.addresses(4))
        self.assertEqual(pauses, [0.5] * 4)


if __name__ == "__main__":
    unittest.main()
