"""
What the administrator is told about, and the watchdog that notices trouble.

Every push goes through `push(event, …)`, which asks the event's switch first:
an installation that wants to hear about money and problems but not about
every registration turns that one off on the Gotify tab. All of them are on by
default — they reach only the administrator, and somebody who set Gotify up
did so to be told things.

The watchdog is here because nothing else would notice a quiet failure. A
3x-ui that stopped answering shows up only as the next registration failing;
a mailbox that refuses the login shows up as nobody being answered. It looks
every few minutes, says so once when something breaks and once when it is
back, and keeps no state beyond the process: a restart forgets an outage it
had already reported, and reports it again if it is still there.
"""
import logging
import time

import config
import i18n
import mailer

logger = logging.getLogger(__name__)

# (event, setting, caption on the Gotify tab, hint). The order is the order on
# the page: the everyday ones first, the problems after.
EVENTS = [
    ("registration", "NOTIFY_REGISTRATION", "A client registered",
     "By letter with a code word. The text is the one set below."),
    ("payment", "NOTIFY_PAYMENT", "A payment arrived",
     "Who paid, how much, for which tariff — and through which promo code."),
    ("forwarded", "NOTIFY_FORWARDED", "A letter was passed on to support",
     "Only when passing letters on is switched on, on the General tab."),
    ("payment_stuck", "NOTIFY_PAYMENT_STUCK", "A payment could not be applied",
     "The money arrived but 3x-ui refused the change. The bot keeps trying; this says so once."),
    ("xui_down", "NOTIFY_XUI_DOWN", "3x-ui stopped answering, and when it is back",
     "Checked every few minutes. Registrations and payments wait while it is down."),
    ("mail_down", "NOTIFY_MAIL_DOWN", "The mailbox cannot be read, and when it can again",
     "After several polls in a row fail. Nobody is answered while it lasts."),
]
SETTING = {event: key for event, key, _, _ in EVENTS}

# How often the watchdog looks, and how many failures in a row make an
# outage: one missed answer is a blip, not something to wake anybody for.
WATCH_EVERY_S = 5 * 60
XUI_FAILURES = 2
MAIL_FAILURES = 3

_last_watch = 0.0
_xui_failures = 0
_xui_reported = False
_mail_reported = False


def enabled(event: str) -> bool:
    return bool(getattr(config, SETTING[event], True))


def push(event: str, title: str, message: str):
    """Sends a Gotify push for this event, if its switch is on. Never raises."""
    if not enabled(event):
        logger.debug(f"Notification {event!r} is switched off; not sending it.")
        return
    try:
        mailer.send_gotify_notification(title, message)
    except Exception as e:
        logger.error(f"Could not send the {event!r} notification: {e}")


def _gotify_configured() -> bool:
    return bool(config.GOTIFY_URL and config.GOTIFY_TOKEN)


def watch(now: float = None):
    """One look at 3x-ui and the mailbox, at most every WATCH_EVERY_S."""
    global _last_watch, _xui_failures, _xui_reported, _mail_reported
    now = now or time.time()
    if now - _last_watch < WATCH_EVERY_S or not _gotify_configured():
        return
    _last_watch = now

    if enabled("xui_down"):
        from xui_client import get_shared_client
        answered = get_shared_client().get_server_status() is not None
        _xui_failures = 0 if answered else _xui_failures + 1
        if _xui_failures >= XUI_FAILURES and not _xui_reported:
            _xui_reported = True
            logger.warning("3x-ui has not answered for several checks in a row.")
            push("xui_down", i18n.t("3x-ui is not answering"),
                 i18n.t("{url} has not answered for several checks in a row. Registrations and "
                        "payments are waiting.", url=config.XUI_URL))
        elif answered and _xui_reported:
            _xui_reported = False
            push("xui_down", i18n.t("3x-ui is answering again"),
                 i18n.t("{url} is back. Whatever was waiting will go through on its own.",
                        url=config.XUI_URL))

    if enabled("mail_down"):
        import inbox
        health = inbox.mail_health()
        if health["configured"] and health["failures"] >= MAIL_FAILURES and not _mail_reported:
            _mail_reported = True
            push("mail_down", i18n.t("The mailbox cannot be read"),
                 i18n.t("{n} polls in a row failed: {error}", n=health["failures"],
                        error=health["error"] or "—"))
        elif health["failures"] == 0 and _mail_reported:
            _mail_reported = False
            push("mail_down", i18n.t("The mailbox is read again"),
                 i18n.t("Letters that arrived meanwhile are being answered now."))
