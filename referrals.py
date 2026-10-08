"""
Inviting friends: a personal word per client, a discount for the friend, days
for the one who invited them.

A client writes /invite and gets a word of their own. A friend who writes it
gets an offer of every tariff at the referral discount; when that friend pays
for the first time, the one who invited them gets bonus days added, by the
same rule a purchase follows. Only a first purchase earns anything — the
discount is a welcome, not a standing price, and the days are a thank-you for
bringing somebody new rather than for a friend renewing.

The words live in referrals.json rather than among the codes in tariffs.json:
there is one per client who asked, and the Code words tab would drown in them.
They share the codes' alphabet and are kept unique against them, so a letter
carrying a word can only ever mean one thing. Who invited whom is recorded
here as well, which is what keeps a reward from being paid twice.

`referrals.json` is one of the installation's state files — see STATE_FILES
in mailchilla.sh.
"""
import json
import logging
import os
import threading
import time

import config
import tariffs

logger = logging.getLogger(__name__)

REFERRALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "referrals.json")

_lock = threading.RLock()
# {word: {"email": …, "created_at": …, "invited": [...], "paid": [...]}}
_words = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def enabled() -> bool:
    return bool(getattr(config, "REFERRAL_ENABLED", False))


def discount_percent() -> int:
    try:
        return min(max(int(getattr(config, "REFERRAL_DISCOUNT", 10) or 0), 0), 99)
    except (TypeError, ValueError):
        return 0


def bonus_days() -> int:
    try:
        return max(int(getattr(config, "REFERRAL_BONUS_DAYS", 7) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _write():
    tmp_path = REFERRALS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"words": _words}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, REFERRALS_PATH)


def load():
    global _words
    with _lock:
        if not os.path.exists(REFERRALS_PATH):
            _words = {}
            return
        try:
            with open(REFERRALS_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            logger.error(f"Could not read {REFERRALS_PATH}: {e}. Referral words are unavailable.")
            _words = {}
            return
        _words = {}
        for word, entry in (data.get("words") or {}).items():
            _words[str(word)] = {
                "email": str(entry.get("email") or "").strip().lower(),
                "created_at": int(entry.get("created_at") or _now_ms()),
                "invited": [str(x).lower() for x in entry.get("invited") or []],
                "paid": [str(x).lower() for x in entry.get("paid") or []],
            }


def words() -> list:
    """Every referral word, for the bot to look for — none while switched off."""
    if not enabled():
        return []
    with _lock:
        return list(_words)


def owner(word: str):
    """The address a referral word belongs to, or None."""
    needle = tariffs.normalise_word(word)
    with _lock:
        for w, entry in _words.items():
            if tariffs.normalise_word(w) == needle:
                return entry["email"]
    return None


def canonical(word: str):
    needle = tariffs.normalise_word(word)
    with _lock:
        return next((w for w in _words if tariffs.normalise_word(w) == needle), None)


def word_for(email: str) -> str:
    """The client's own word, made the first time they ask for it."""
    email = email.strip().lower()
    with _lock:
        for word, entry in _words.items():
            if entry["email"] == email:
                return word
        while True:
            word = tariffs.generate_word()
            if not tariffs.word_owner(word) and canonical(word) is None:
                break
        _words[word] = {"email": email, "created_at": _now_ms(), "invited": [], "paid": []}
        _write()
        logger.info(f"Referral word {word!r} made for {email}.")
        return word


def note_invited(word: str, email: str):
    """Somebody wrote the word and was made an offer: they count as invited."""
    word = canonical(word)
    email = email.strip().lower()
    with _lock:
        entry = _words.get(word)
        if entry and email not in entry["invited"]:
            entry["invited"].append(email)
            _write()


def record_paid(word: str, email: str) -> bool:
    """
    Records a friend's first payment through the word. True the first time
    only — the one place that decides whether a reward is owed, so a reward
    cannot be paid twice whatever is retried around it.
    """
    word = canonical(word)
    email = email.strip().lower()
    with _lock:
        entry = _words.get(word)
        if not entry or email in entry["paid"]:
            return False
        entry["paid"].append(email)
        if email not in entry["invited"]:
            entry["invited"].append(email)
        _write()
        return True


def stats_for(email: str):
    """The client's word and what it has done, for their card, or None."""
    email = email.strip().lower()
    with _lock:
        for word, entry in _words.items():
            if entry["email"] == email:
                return {"word": word, "invited": len(entry["invited"]), "paid": len(entry["paid"])}
    return None
