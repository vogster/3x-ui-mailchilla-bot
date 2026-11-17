"""
The second step of signing in, as the panel uses it.

`totp.py` is the arithmetic and knows nothing of the panel. This is what the
panel adds around it: where the secret comes from, how many wrong codes are
put up with, and the secret that is on its way in — shown as a QR code, not yet
proved by a code from the app, and so not yet written anywhere.

Shared by the sign-in form in `admin/app.py` and the page in
`admin/routes_totp.py`, which is why it is neither of them.
"""
import logging
import threading
import time

import config
import settings
import totp

logger = logging.getLogger(__name__)

OK, WRONG, LOCKED = "ok", "wrong", "locked"

# --- Guessing at the code ---
# A code is six digits and three of them are right at any moment, so each guess
# is a three-in-a-million chance. The sign-in form's own ceiling — ten a minute
# for an address — would leave that at four in a hundred over a day, for
# somebody who by then already has the password; and they are not held to one
# address. So wrong codes are counted on their own and for everybody at once:
# this many in this long, and until that has passed no code is looked at, the
# right one included.
#
# The same reasoning as the password's ceiling, and the same shape: it lifts by
# itself. Only somebody who got the password right can run it out, and what it
# costs the administrator then is five minutes — while the log says, in as many
# words, that somebody else knows the password.
CODE_ATTEMPTS = 5
CODE_WINDOW = 300

# The two places a code is asked for keep separate counts. With one between
# them, a session left open on somebody else's computer could post wrong codes
# at the switch-off form and by that alone keep every code typed at the
# sign-in form from being looked at — a way to hold the administrator out that
# needs neither the password nor a code.
SIGN_IN, SWITCH_OFF = "sign-in", "switch-off"

_lock = threading.Lock()
_wrong = {SIGN_IN: [], SWITCH_OFF: []}      # door -> when its guesses were charged

# --- The secret on its way in ---
# Kept here rather than in a hidden field of the form: a secret that travels to
# the browser and back is one more place it has been. A restart loses it, which
# costs a second look at the page.
#
# One for each session that is setting the second step up, never one for the
# process. Two-step sign-in is switched on above all by somebody who suspects
# the password has got out — which is exactly when a second session, signed in
# with that password, may be open somewhere. With a secret shared between them
# that session had only to open the same page to be shown what the
# administrator's app was about to be given, and could make the codes for good.
PENDING_SECONDS = 900
# Sessions setting it up at once. More than one is rare; more than this many is
# somebody making sessions, and the oldest go.
PENDING_KEPT = 8
_pending = {}        # the session's setup id -> (secret, good until)
# Of those, the sessions bringing a new secret in while the second step is on:
# each got here by giving a code for the secret in force. The old secret goes
# on working until the new one is proved — replacing by switching off first
# would leave the panel on its password for as long as the setting up took,
# and for good if it was abandoned half-way.
_replacing = set()


def _reset():
    """Forgets what is kept in memory, as a restart does. For the tests."""
    with _lock:
        for door in _wrong:
            _wrong[door] = []
        _pending.clear()
        _replacing.clear()


def on() -> bool:
    """Whether signing in asks for a code."""
    return settings.two_step_on()


def check(code, door: str = SIGN_IN) -> str:
    """
    OK, WRONG or LOCKED for a code somebody typed at one of the two doors.

    OK when there is no second step to pass. A right code is used up by this:
    see totp.accept. The charge for a guess is made before the code is looked
    at and handed back when it turns out right, for the reason the password's
    ceiling gives — checking first and counting afterwards lets a burst of
    parallel guesses through before any of them has been counted.

    Handed back, and no more than that: a right code does not wipe the wrong
    ones before it. Unlike the password's count, this one is everybody's, and
    the administrator signing in would otherwise be giving whoever is guessing
    alongside a fresh five.
    """
    secret = config.TOTP_SECRET
    if not secret:
        return OK
    now = time.time()
    with _lock:
        recent = [t for t in _wrong[door] if now - t < CODE_WINDOW]
        _wrong[door] = recent
        if len(recent) >= CODE_ATTEMPTS:
            return LOCKED
        recent.append(now)
        closing = len(recent) >= CODE_ATTEMPTS
    if totp.accept(secret, code):
        with _lock:
            if now in _wrong[door]:
                _wrong[door].remove(now)
        return OK
    if closing:
        logger.warning(f"{CODE_ATTEMPTS} wrong codes at the {door} form within {CODE_WINDOW} "
                       f"seconds; no code is looked at there until that has passed.")
    return WRONG


def pending(owner: str) -> str:
    """
    The secret this session is setting up, made now if it has none or its last
    one has lapsed. `owner` is the session's setup id — see routes_totp.
    """
    now = time.time()
    with _lock:
        return _pending_locked(owner, now)


def _pending_locked(owner: str, now: float) -> str:
    for stale in [key for key, (_, until) in _pending.items() if now >= until]:
        del _pending[stale]
        _replacing.discard(stale)
    if owner not in _pending:
        while len(_pending) >= PENDING_KEPT:
            oldest = min(_pending, key=lambda key: _pending[key][1])
            del _pending[oldest]
            _replacing.discard(oldest)
        _pending[owner] = (totp.new_secret(), now + PENDING_SECONDS)
    return _pending[owner][0]


def begin_replacing(owner: str):
    """
    Starts setting up a new secret beside the one in force. The caller has
    checked a code for the old one — that check is what this session's right
    to bring a new secret in rests on.
    """
    now = time.time()
    with _lock:
        _pending.pop(owner, None)
        _pending_locked(owner, now)
        _replacing.add(owner)


def replacing(owner: str) -> bool:
    """Whether this session is in the middle of that, with a secret still good."""
    with _lock:
        return bool(owner) and owner in _replacing and time.time() < _pending.get(owner, ("", 0.0))[1]


def drop_pending(owner: str = None):
    """
    The setting up was called off: the secret that was shown is worth nothing.
    Without an owner, everybody's — the second step has just been switched on
    or off, and a secret still on somebody's screen belongs to a state of
    affairs that is over.
    """
    with _lock:
        if owner is None:
            _pending.clear()
            _replacing.clear()
        else:
            _pending.pop(owner, None)
            _replacing.discard(owner)


def confirm(owner: str, code) -> bool:
    """
    Switches the second step on — or, for a session that is replacing the
    secret, over to the new one — if the code is one the app made from the
    secret this session was shown.

    That check is the whole point of asking: a secret written down before the
    app has proved it holds the same one is how somebody locks themself out
    with a QR code that never scanned. Raises what settings.save raises when
    the file cannot be written.
    """
    with _lock:
        secret, until = _pending.get(owner or "", ("", 0.0))
        if time.time() >= until:
            secret = ""
        # With a secret in force, only a session that gave a code for it may
        # bring another in. The routes ask the same; this is the lock on the
        # door rather than the sign beside it.
        if config.TOTP_SECRET and owner not in _replacing:
            secret = ""
    if not secret or not totp.accept(secret, code):
        return False
    settings.save_two_step(secret)
    drop_pending()
    return True


def switch_off():
    """Takes the second step off. The caller has checked a code."""
    settings.save_two_step("")
    drop_pending()
