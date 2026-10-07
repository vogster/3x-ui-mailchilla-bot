"""
Reminding a client who has not connected in a while.

Lives apart from inbox.py because it never touches the mailbox connection: it
asks 3x-ui who is there, decides who has gone quiet, and sends a letter over
SMTP — the same last step a registration takes, just triggered by a clock
rather than by an incoming letter. Bundling it into inbox.py's poll would tie
a check that needs no IMAP connection at all to one that exists only because a
connection is already open.
"""
import logging
import time

import config
import mailer
import periodic
import templates
from xui_client import XuiClient, get_shared_client

logger = logging.getLogger(__name__)

# Whether the last attempt already said that 3x-ui could not be asked. The
# sweep is retried on every poll until it can be, and an older panel without
# the last-online route never can: one warning, then quiet until it works.
_warned = False


def sweep_due(now=None) -> bool:
    """Whether it is time to look for inactive clients again."""
    return periodic.due("INACTIVITY_REMINDER_ENABLED", "INACTIVITY_REMINDER_DAYS",
                        "INACTIVITY_REMINDER_LAST_AT", now=now)


def _last_seen_ms(client: dict, last_online: dict) -> int:
    """
    When the client was last seen, 0 for "never".

    `last_online` carries nothing for a client 3x-ui has not seen connect even
    once — which is not the same as "seen just now", or nobody who registered
    by hand, or before this feature existed, and has not connected since would
    ever be found overdue. The registration date stands in for that missing
    moment instead, the same way a brand-new client is not overdue on day one.
    """
    remark = client.get("email", "") or ""
    seen = int(last_online.get(remark) or 0)
    return seen or int(client.get("createdAt") or 0)


def overdue():
    """
    Who has gone quiet for too long, as (address, days away) pairs.

    None when 3x-ui could not say — and that has to stay distinct from an
    empty answer. A missing last-online map read as "nobody has ever
    connected" would fall back to the registration date for every client, and
    mail everybody registered longer ago than the threshold, the people online
    right now included.
    """
    global _warned
    xui = get_shared_client()
    clients = xui.get_all_clients()
    last_online = xui.get_last_online()
    if clients is None or last_online is None:
        log = logger.debug if _warned else logger.warning
        log("Inactivity reminder: 3x-ui did not say who was online when, "
            "nobody is mailed. It will be asked again on the next poll.")
        _warned = True
        return None
    _warned = False

    threshold_ms = periodic.period_days("INACTIVITY_REMINDER_DAYS") * 86400 * 1000
    now_ms = int(time.time() * 1000)
    found = []
    for client in clients:
        if client.get("enable") is not True:
            continue
        # A client made by hand in 3x-ui may carry no address at all; there is
        # nowhere to mail such a row.
        bare_email = XuiClient.extract_bare_email(client.get("email", "") or "")
        if not bare_email:
            continue
        seen_ms = _last_seen_ms(client, last_online)
        if not seen_ms or now_ms - seen_ms < threshold_ms:
            continue
        found.append((bare_email, (now_ms - seen_ms) // 86400000))
    return found


def send(found) -> int:
    """Mails the reminder to each (address, days) pair. Returns how many were sent."""
    sent = 0
    for bare_email, days in found:
        try:
            mailer.send_email_reply(
                bare_email, templates.text("inactivity.subject"),
                templates.get_inactivity_email(bare_email, days))
            sent += 1
        except Exception as e:
            logger.error(f"Could not send the inactivity reminder to {bare_email}: {e}")

    if sent:
        logger.info(f"Inactivity reminder: {sent} letter(s) sent.")
    else:
        logger.info("Inactivity reminder: nobody was overdue.")
    return sent


def _remember(when: float):
    """Writes down when the sweep ran, so a restart keeps the schedule."""
    try:
        import settings
        settings.save({"INACTIVITY_REMINDER_LAST_AT": when})
    except Exception as e:
        # Costs at most one repeated sweep, which is what writing it down first
        # is there to prevent — worth a line in the log, not worth stopping for.
        logger.warning(f"Could not write down when the inactivity sweep ran: {e}")


def run_if_due():
    """
    Runs the sweep and records it, but only when it is actually due.

    The run is written down before the first letter goes, not after the last.
    Sending is one SMTP connection per letter and can take minutes; a restart
    in the middle of it — `mailchilla update` is one — would otherwise find no
    record, and mail everybody already reminded a second time. A reminder
    missed is cheaper than one sent twice. When 3x-ui could not be asked,
    nothing is written down, and the next poll tries again.

    The interval is how often the sweep looks, not how long somebody has to
    have been away, so a client still gone next time is reminded again rather
    than only once, ever.
    """
    if not sweep_due():
        return
    found = overdue()
    if found is None:
        return
    _remember(time.time())
    send(found)
