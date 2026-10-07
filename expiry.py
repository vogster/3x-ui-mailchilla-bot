"""
Telling a client their subscription is about to end, and that it has.

Two letters: one some days before the end, one when it comes. Both carry a way
to extend — a button that opens a letter to the bot with /buy already in it —
while something can be bought, and say whom to ask while nothing can.

Who has been told is not written down anywhere, and does not need to be. The
sweep looks at a window of time rather than at a list of people: everybody
whose end fell between the last sweep and this one gets the letter for it. The
windows of consecutive sweeps meet without overlapping, so each end is crossed
exactly once — and all that has to survive a restart is when the last sweep
ran, which settings.json already keeps for the other scheduled jobs.

A client whose end moved — a purchase, an edit in 3x-ui — is simply looked at
again where the end now is.
"""
import logging
import math
import time
from datetime import datetime

import config
import mailer
import templates
from xui_client import XuiClient, get_shared_client

logger = logging.getLogger(__name__)

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS

# How often the sweep looks. An hour is fine-grained enough that a letter
# about an end arrives on the right day, and coarse enough that the client
# list is not fetched on every poll.
EVERY_MS = HOUR_MS

# How far back the "it has ended" letter reaches on the very first sweep,
# after the switch is turned on. Without a limit everybody whose subscription
# ever ran out would be written to at once.
FIRST_LOOK_BACK_MS = DAY_MS


def _now_ms() -> int:
    return int(time.time() * 1000)


def _last_ms() -> int:
    try:
        return int(float(getattr(config, "EXPIRY_REMINDER_LAST_AT", 0) or 0) * 1000)
    except (TypeError, ValueError):
        return 0


def due(now_ms: int = None) -> bool:
    if not getattr(config, "EXPIRY_REMINDER_ENABLED", False):
        return False
    return (now_ms or _now_ms()) - _last_ms() >= EVERY_MS


def _days_before() -> int:
    try:
        return max(1, int(getattr(config, "EXPIRY_REMINDER_DAYS", 3) or 3))
    except (TypeError, ValueError):
        return 3


def windows(now_ms: int, last_ms: int):
    """
    The two spans of time whose ends get a letter in this sweep, as
    ((soon_from, soon_to), (ended_from, ended_to)), each open on the left.

    "Soon" is the stretch that has come within reach of the warning since the
    last sweep, but never reaching back past now: after a long stop, an end
    that is already over gets the letter saying so, not one saying it is near.
    On the first sweep, "soon" is everything within reach, so whoever ends in
    the next few days is warned the moment the switch is turned on.
    """
    ahead = _days_before() * DAY_MS
    if not last_ms:
        return (now_ms, now_ms + ahead), (now_ms - FIRST_LOOK_BACK_MS, now_ms)
    return (max(last_ms + ahead, now_ms), now_ms + ahead), (last_ms, now_ms)


def bot_address() -> str:
    """Where a client writes to reach the bot: the mailbox it reads."""
    for value in (getattr(config, "IMAP_USER", ""), getattr(config, "SMTP_USER", "")):
        if value and "@" in value:
            return value.strip()
    return ""


def _barred(client, now_ms: int) -> bool:
    # The same rule a purchase follows; imported here because purchases pulls
    # in the providers, which this module otherwise has no use for.
    from purchases import is_barred
    return is_barred(client, now_ms)


def owed(now_ms: int = None, last_ms: int = None):
    """
    The letters this sweep owes, as (address, kind, end, days, tariff) — or None
    when 3x-ui could not be asked, so that the window is looked at again at
    the next poll rather than skipped.
    """
    now_ms = now_ms or _now_ms()
    last_ms = _last_ms() if last_ms is None else last_ms
    (soon_from, soon_to), (ended_from, ended_to) = windows(now_ms, last_ms)

    clients = get_shared_client().get_all_clients()
    if clients is None:
        logger.warning("Expiry reminders: 3x-ui did not answer; trying again at the next poll.")
        return None

    letters = []
    for client in clients:
        end = int(client.get("expiryTime") or 0)
        if end <= 0:
            # No end, or one counted from a first connection not yet made.
            continue
        bare_email = XuiClient.extract_bare_email(client.get("email", "") or "")
        if not bare_email or _barred(client, now_ms):
            continue
        if soon_from < end <= soon_to:
            kind = "soon"
        elif ended_from < end <= ended_to:
            kind = "ended"
        else:
            continue
        days = max(1, math.ceil((end - now_ms) / DAY_MS))
        letters.append((bare_email, kind, end, days, (client.get("group") or "").strip()))
    return letters


def send(letters) -> int:
    sent = 0
    for bare_email, kind, end, days, tariff in letters:
        try:
            mailer.send_email_reply(
                bare_email, templates.expiry_subject(kind, days),
                templates.get_expiry_email(kind, end, days, tariff, bot_address()))
            sent += 1
        except Exception as e:
            logger.error(f"Could not send the expiry letter to {bare_email}: {e}")
    if sent:
        logger.info(f"Expiry reminders: {sent} letter(s) sent.")
    return sent


def _remember(when_ms: int):
    try:
        import settings
        settings.save({"EXPIRY_REMINDER_LAST_AT": when_ms / 1000})
    except Exception as e:
        # The next sweep then reaches back over this one's window and repeats
        # its letters. Worth a line in the log, not worth stopping for.
        logger.warning(f"Could not write down when the expiry reminders ran: {e}")


def run_if_due():
    """
    The bot loop's entry point: sweeps when an hour has passed since the last one.

    The sweep is written down before the letters go, not after: a restart in
    the middle of sending would otherwise send them all again. A letter
    missed is cheaper than one sent twice — the same order the inactivity
    reminder keeps.
    """
    now_ms = _now_ms()
    if not due(now_ms):
        return
    letters = owed(now_ms)
    if letters is None:
        return
    _remember(now_ms)
    send(letters)


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")
