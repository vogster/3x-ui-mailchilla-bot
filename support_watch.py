"""
A push when a letter arrives for Support.

The bot reads its own mailbox every few seconds; nobody reads Support's. A
letter there waits for a person, and the person finds out when they next
happen to look — which for a client whose phone will not connect is the wrong
kind of wait. So once a minute the bot looks at that mailbox too, and anything
that has arrived since it last looked goes to Gotify: who wrote, and about
what.

Looking is all it does. The folder is examined, never selected, and only
headers are fetched, with PEEK — the same rule the Mail pages keep, for a
plainer reason here: a letter this marked read is a letter the person it was
for would never notice.

One notification per letter, with a title and a text the administrator writes
on the Gotify tab the way the registration push's are written. When more
arrive at once than is worth a notification each — after the process has been
down, or when somebody's script finds the address — the newest few are
announced and the rest are counted in one notification more.

What "since it last looked" means is kept as a mark in settings.json, the
mailbox's UIDVALIDITY and the highest UID seen, so that a restart neither
repeats what was already said nor loses what arrived while the process was
down. A mailbox met for the first time — or one whose numbering has changed,
which is what a different UIDVALIDITY says — is taken as it stands: its
letters were there before anybody was listening, and announcing them would be
announcing the whole mailbox.
"""
import logging
import re
import time

import config
import i18n
import mailer
import mailfolders

logger = logging.getLogger(__name__)

# How often the mailbox is looked at. Checked on every poll of the bot's own
# mailbox but due far less often: a person answering a letter is not helped by
# the fifteen seconds, and a provider is entitled to mind a login every poll.
CHECK_SECONDS = 60
# How many letters get a notification each in one look; the rest are counted.
NAMED = 5

_MARK = re.compile(r"(\d+):(\d+)\Z")

_last_check = 0.0
# Whether the last look failed, so that a mailbox out of reach is said once
# and not once a minute for as long as it lasts.
_failing = False


def _reset():
    """Forgets what is kept in memory, as a restart does. For the tests."""
    global _last_check, _failing
    _last_check, _failing = 0.0, False


def enabled() -> bool:
    """Whether there is anything to do: the switch, somewhere to push, and a mailbox to look at."""
    return bool(getattr(config, "SUPPORT_NOTIFY_ENABLED", False)
                and config.GOTIFY_URL and config.GOTIFY_TOKEN
                and config.SUPPORT_IMAP_SERVER and config.SUPPORT_IMAP_USER
                and config.SUPPORT_IMAP_PASSWORD)


def _mark():
    """(validity, uid) out of the stored mark, or ("", 0) when there is none."""
    found = _MARK.match(str(getattr(config, "SUPPORT_NOTIFY_MARK", "") or ""))
    return (found.group(1), int(found.group(2))) if found else ("", 0)


def _remember(mark: str):
    """Writes the mark down, so a restart keeps it."""
    try:
        import settings
        settings.save({"SUPPORT_NOTIFY_MARK": mark})
    except Exception as e:
        # Costs a repeated push after the next restart, at worst.
        logger.warning(f"Could not write down which Support letters have been announced: {e}")
        config.SUPPORT_NOTIFY_MARK = mark


def _who(row: dict) -> str:
    person = row.get("person") or {}
    name, address = person.get("name", ""), person.get("address", "")
    if name and address and name.lower() != address:
        return f"{name} <{address}>"
    return address or name or "?"


def _fields(row: dict) -> dict:
    """What the title and the text of a notification may name about a letter."""
    person = row.get("person") or {}
    return {
        "sender": _who(row),
        "name": person.get("name", ""),
        "from": person.get("address", ""),
        # In the panel's language, like everything about a notification that
        # is not the administrator's own wording.
        "subject": row.get("subject") or i18n.t("(no subject)"),
        "service": config.SERVICE_NAME,
    }


def _push(letters: list, count: int):
    """
    A notification for each letter, oldest first, in the administrator's words.

    The count goes first when there are more letters than notifications, so
    that the newest letter is the last thing to arrive and the first thing
    seen. Its wording is fixed: it is about how many there are, and says
    nothing an installation would want to say differently.
    """
    if count > len(letters):
        mailer.send_gotify_notification(
            i18n.t("{count} new letters to {service} support", count=count,
                   service=config.SERVICE_NAME),
            i18n.t("and {count} more", count=count - len(letters)))
    for row in letters:
        fields = _fields(row)
        mailer.send_gotify_notification(
            mailer.render_template(config.SUPPORT_NOTIFY_TITLE, **fields),
            mailer.render_template(config.SUPPORT_NOTIFY_MESSAGE, **fields))


def check():
    """Looks at the mailbox once and announces what is new. Never raises."""
    global _failing
    if not enabled():
        return
    validity, uid = _mark()
    try:
        found = mailfolders.arrivals(mailfolders.SUPPORT, validity, uid, limit=NAMED)
    except Exception as e:
        # A stall is ordinary and the next minute tries again; nothing is
        # marked, so nothing is lost by it.
        logger.log(logging.DEBUG if _failing else logging.WARNING,
                   f"Could not look at the Support mailbox for new letters: "
                   f"{type(e).__name__}: {e}")
        _failing = True
        return
    if _failing:
        logger.info("The Support mailbox answers again.")
        _failing = False

    mark = f"{found['validity']}:{found['top']}"
    if mark != f"{validity}:{uid}":
        # Before the push, not after: a push that fails must not be the reason
        # the same letter is announced again on every check from now on. And
        # only when it has moved — this runs once a minute for ever.
        _remember(mark)
    if found["letters"]:
        logger.info(f"Support mailbox: {found['count']} new letter(s); a notification was sent.")
        _push(found["letters"], found["count"])


def run_if_due(now: float = None):
    """Called on every poll of the bot loop; looks only when a minute has passed."""
    global _last_check
    now = time.time() if now is None else now
    if now - _last_check < CHECK_SECONDS and _last_check:
        return
    _last_check = now
    check()
