"""
A push when Xray stops, and another when it is back.

3x-ui reports the state of Xray with the rest of the server's status, and the
dashboard shows it — to whoever happens to have the dashboard open. Xray not
running is every client of the service disconnected at once, and the way that
was found out was a client writing to say so.

So once a minute the bot asks, and says through Gotify what has changed since
it last said anything: Xray stopped, Xray failed, Xray running again. Only
what has changed. A notification a minute for as long as it stays down would
be the surest way to have the notifications switched off.

Two things are told apart that are easy to take for each other.

A panel that does not answer is not Xray having stopped. 3x-ui restarting, or
the network between the two machines, says nothing about whether clients are
connected — so that is said in its own words, and only after several looks in
a row have gone unanswered.

And a stop that lasts a moment is not a fault. 3x-ui restarts Xray whenever
its configuration changes, which adding a client does; a look that lands in
that second would announce a failure and, a minute later, a recovery. Xray is
believed to be down when it has been found not running twice running.

What has been said is kept in memory. A restart of the bot forgets it, and
then a Xray that is down is announced again — which is the better of the two
mistakes on offer.
"""
import logging
import time

import config
import i18n
import mailer
from xui_client import get_shared_client

logger = logging.getLogger(__name__)

# How often the panel is asked. Checked on every poll of the mailbox but due
# far less often.
CHECK_SECONDS = 60
# Xray is believed to be down once it has been found not running on this many
# looks in a row. See above: it is restarted as a matter of routine.
LOOKS_TO_BELIEVE = 2
# The panel not answering is announced after this many looks in a row.
SILENT_LOOKS = 3

RUNNING = "running"
# Not a state of Xray's but ours: the panel could not be asked.
AWAY = "away"

# When the panel was last asked, by a clock that only goes forward; None
# before the first time.
_last_check = None
# What was last announced — or, on the first look, simply found: a state of
# Xray's, AWAY, or None while nothing is known.
_known = None
# Looks in a row on which Xray was found not running, whatever it was instead;
# and the state it was last found in, with how many looks in a row it has been
# that one.
_down_looks = 0
_seen, _seen_looks = None, 0
# Looks in a row the panel has left unanswered.
_silent = 0


def _reset():
    """Forgets what is kept in memory, as a restart does. For the tests."""
    global _last_check, _known, _down_looks, _seen, _seen_looks, _silent
    _last_check, _known, _down_looks, _seen, _seen_looks, _silent = None, None, 0, None, 0, 0


def enabled() -> bool:
    """Whether there is anything to do: the switch, and somewhere to push."""
    return bool(getattr(config, "XRAY_NOTIFY_ENABLED", False)
                and config.GOTIFY_URL and config.GOTIFY_TOKEN)


def _down(state) -> bool:
    """Whether this is Xray found not running, as against running, unknown or unasked."""
    return state not in (None, RUNNING, AWAY)


def _in_words(state, error: str, was) -> str:
    """What happened, as a sentence in the panel's language."""
    if state == AWAY:
        return i18n.t("3x-ui does not answer, so whether Xray is running is not known")
    if state is None:
        # The panel is back and says nothing about Xray either way.
        return i18n.t("3x-ui answers again")
    if state == RUNNING:
        if was == AWAY:
            return i18n.t("3x-ui answers again, and Xray is running")
        return i18n.t("Xray is running again")
    if was == AWAY:
        # Not "has stopped": when it did is exactly what could not be seen.
        words = i18n.t("3x-ui answers again, but Xray is not running ({state})", state=state)
    elif state == "stop":
        words = i18n.t("Xray has stopped")
    elif state == "error":
        words = i18n.t("Xray has failed")
    else:
        # A state this code has not met: said as the panel names it.
        words = i18n.t("Xray is not running ({state})", state=state)
    return f"{words}: {error}" if error else words


def _for_the_log(state, error: str, was) -> str:
    """The same, for the log, which is in English whatever the panel speaks."""
    if state == AWAY:
        return f"3x-ui has not answered {SILENT_LOOKS} checks in a row"
    if state is None:
        return "3x-ui answers again and reports no state for Xray"
    if state == RUNNING:
        return "3x-ui answers again and Xray is running" if was == AWAY else "Xray is running again"
    return f"Xray is not running (state {state!r})" + (f": {error}" if error else "")


def _announce(state, error: str = ""):
    """
    Takes a state as believed: says it if it differs from what was last said.

    The first look is the exception. Xray running when the bot starts is not
    news, and saying so on every restart would teach nobody anything.

    What was said is remembered before it is sent, not after. A push that
    fails must not be the reason the same thing is tried again every minute
    from now on; one notification lost is the smaller harm.
    """
    global _known
    if state == _known:
        return
    was, _known = _known, state
    if was is None and state == RUNNING:
        return
    words = _for_the_log(state, error, was)
    level = logging.INFO if state in (RUNNING, None) else logging.WARNING
    try:
        fields = {
            "status": _in_words(state, error, was),
            "state": state or "",
            "error": error,
            "service": config.SERVICE_NAME,
        }
        mailer.send_gotify_notification(
            mailer.render_template(config.XRAY_NOTIFY_TITLE, **fields),
            mailer.render_template(config.XRAY_NOTIFY_MESSAGE, **fields))
    except Exception as e:
        # Said in the log all the same: that is the half of this that cannot fail.
        logger.log(level, f"{words}.")
        logger.error(f"The notification about it could not be sent: {type(e).__name__}: {e}")
        return
    logger.log(level, f"{words}; a notification was sent.")


def check():
    """Asks once and says what has changed. Never raises."""
    global _known, _down_looks, _seen, _seen_looks, _silent
    if not enabled():
        # Switched off, or nowhere to push. Whatever was known goes: switched
        # on again a week later, what was true then is not a thing to compare
        # with.
        _known, _down_looks, _seen, _seen_looks, _silent = None, 0, None, 0, 0
        return
    try:
        # quiet: a panel that is away is asked once a minute for as long as it
        # is, and the client's own warning each time would be all the log held.
        status = get_shared_client().get_server_status(quiet=True)
    except Exception as e:
        logger.debug(f"Could not ask 3x-ui for the state of Xray: {type(e).__name__}: {e}")
        status = None

    if status is None:
        _silent += 1
        # A silence is not a sighting: what was half seen before it has to be
        # seen again.
        _down_looks, _seen, _seen_looks = 0, None, 0
        if _silent >= SILENT_LOOKS:
            _announce(AWAY)
        return
    _silent = 0

    state = str(status.get("xray_state") or "").strip().lower()
    if not state:
        # A panel that says nothing about Xray: nothing to take for a failure.
        # But if it had been announced as away, it is back, and that is owed.
        _down_looks, _seen, _seen_looks = 0, None, 0
        if _known == AWAY:
            _announce(None)
        return
    if state == RUNNING:
        # Good news is believed at once.
        _down_looks, _seen, _seen_looks = 0, None, 0
        _announce(RUNNING)
        return

    error = str(status.get("xray_error") or "").strip()
    _down_looks += 1
    if state == _seen:
        _seen_looks += 1
    else:
        _seen, _seen_looks = state, 1
    if not _down(_known):
        # Counted whatever it is found as each time. Counted by state, a Xray
        # that keeps being restarted and keeps failing — stopped, failed,
        # stopped, failed — was never the same thing twice running, and was
        # never announced at all.
        if _down_looks >= LOOKS_TO_BELIEVE:
            _announce(state, error)
    elif state != _known and _seen_looks >= LOOKS_TO_BELIEVE:
        # Already said to be down. A different way of being down is said only
        # once it has settled, or the same alternation would be a notification
        # a minute.
        _announce(state, error)


def run_if_due(now: float = None):
    """
    Called on every poll of the bot loop; asks only when a minute has passed.

    By a clock that does not go backwards. By the wall clock, a server whose
    time was corrected an hour back asked nothing for that hour.
    """
    global _last_check
    now = time.monotonic() if now is None else now
    if _last_check is not None and 0 <= now - _last_check < CHECK_SECONDS:
        return
    _last_check = now
    check()
