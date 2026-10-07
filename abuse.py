"""
Keeping one address, or a crowd of new ones, from running the bot ragged.

An email address costs nothing to make, which is the difference between this
bot and one in a messenger: a free trial can be claimed again and again from
throwaway mailboxes, and anybody can make the bot write letters by writing
letters to it. Three limits, each with its own setting:

- **Letters per hour from one address.** Past it, a letter is read and not
  answered: every answer would be one more letter from this installation's
  address to somebody who is flooding it. The administrator is never limited.
  A letter is counted once however often it is looked at — one left unread to
  wait its turn is not a second letter.
- **Throwaway domains** get no free registration. Buying stays open to them:
  a paid subscription is not a trial anybody can farm.
- **New free registrations per hour**, across the whole installation. Past
  it, the letter is left unread and handled when the hour has room again —
  a queue, not a refusal — and the administrator is told once.

All of it lives in the process. A restart forgets the counts, which costs at
most one more hour's worth of letters, and keeps nothing about anybody on disk.
"""
import logging
import os
import time
from collections import deque

import config

logger = logging.getLogger(__name__)

DOMAINS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "disposable_domains.txt")
HOUR = 3600

_letters = {}          # address -> deque of (time, letter key)
_registrations = deque()
_limit_reported = False
_shipped = None


def _shipped_domains() -> set:
    global _shipped
    if _shipped is None:
        try:
            with open(DOMAINS_PATH, encoding="utf-8") as f:
                _shipped = {line.strip().lower() for line in f
                            if line.strip() and not line.startswith("#")}
        except OSError as e:
            logger.warning(f"Could not read {DOMAINS_PATH}: {e}; only the panel's list applies.")
            _shipped = set()
    return _shipped


def blocked_domains() -> set:
    extra = getattr(config, "BLOCKED_DOMAINS", "") or ""
    mine = {d.strip().lower().lstrip("@") for d in extra.replace(",", "\n").splitlines() if d.strip()}
    return mine | (_shipped_domains() if getattr(config, "BLOCK_DISPOSABLE", True) else set())


def is_blocked_domain(address: str) -> bool:
    """The address is at a throwaway domain, or a subdomain of one."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    blocked = blocked_domains()
    parts = domain.split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts) - 1))


def allow_letter(address: str, key, now: float = None) -> bool:
    """Whether a letter from this address may be answered, counting it if new."""
    limit = int(getattr(config, "LETTERS_PER_HOUR", 0) or 0)
    if limit <= 0:
        return True
    admin = (getattr(config, "ADMIN_EMAIL", "") or "").strip().lower()
    if admin and address == admin:
        return True
    now = now or time.time()
    if len(_letters) > 10000:
        # Addresses whose hour has passed hold nothing worth keeping; without
        # this the map would grow by every address that ever wrote.
        for stale in [a for a, d in _letters.items() if not d or now - d[-1][0] > HOUR]:
            del _letters[stale]
    seen = _letters.setdefault(address, deque())
    while seen and now - seen[0][0] > HOUR:
        seen.popleft()
    if any(k == key for _, k in seen):
        return True
    if len(seen) >= limit:
        return False
    seen.append((now, key))
    return True


def registration_allowed(now: float = None) -> bool:
    """Whether a new free registration fits in the hour; says so once when it does not."""
    global _limit_reported
    limit = int(getattr(config, "REGISTRATIONS_PER_HOUR", 0) or 0)
    if limit <= 0:
        return True
    now = now or time.time()
    while _registrations and now - _registrations[0] > HOUR:
        _registrations.popleft()
    if len(_registrations) < limit:
        _limit_reported = False
        return True
    if not _limit_reported:
        _limit_reported = True
        logger.warning(f"{limit} new registrations in the last hour; the rest wait their turn.")
        import i18n
        import notify
        notify.push("registration_limit", i18n.t("Registrations are queueing"),
                    i18n.t("{n} new clients in the last hour — the limit. Further letters wait "
                           "unread and are handled as the hour frees up.", n=limit))
    return False


def note_registration(now: float = None):
    _registrations.append(now or time.time())
