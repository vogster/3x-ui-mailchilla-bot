"""
Tariffs and the words that open them.

A tariff is what somebody gets — traffic, term, inbounds. A code is how they
got in: a word that opens one tariff, with a number of activations and a switch
of its own. Keeping the two apart is what lets a leaked word be shut without
touching the tariff behind it, and what lets one tariff have several ways in.

There is only one kind of code. A word anybody may use is one with no limit on
its activations; a personal invitation is the same thing with one activation.
The interface used to present those as two separate things, which taught a
difference the data does not have — the number is the difference, and it is on
the form.

The tariff is a *template*, read at the moment a client is created and never
again: the values then live on the client in 3x-ui, which stays the single
source of truth for everything about them. Editing a tariff therefore changes
nothing for anybody already registered, and the form says so out loud.

`tariffs.json` sits beside `settings.json` and belongs to the installation the
same way: gitignored, backed up by `mailchilla update`, never shipped. On the
first run it is built out of the old single code word and the registration
defaults, so an installation that predates tariffs comes up with one tariff
called "Basic" and carries on as though nothing happened.
"""
import json
import logging
import os
import threading
import time
import uuid

import config
import i18n

logger = logging.getLogger(__name__)

TARIFFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tariffs.json")

# The alphabet a generated code is drawn from. No 0/O and no 1/I/l: the word is
# read off a screen and typed by hand, often from a phone, and a pair that
# cannot be told apart in the reader's font costs somebody a registration.
GENERATED_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GENERATED_LENGTH = 10

_lock = threading.RLock()

# {"tariffs": [...], "codes": [...]}, as held in the file.
_state = {"tariffs": [], "codes": []}


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _now_ms() -> int:
    return int(time.time() * 1000)


def normalise_word(word) -> str:
    """The form a code word is compared in. Case and edge spaces never matter."""
    return str(word or "").strip().casefold()


def _clean_tariff(raw: dict) -> dict:
    """One tariff, with every field coerced to the type the rest of the code expects."""
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(i18n.t("the tariff needs a name"))
    limit_gb = int(raw.get("limit_gb") or 0)
    expire_days = int(raw.get("expire_days") or 0)
    if limit_gb < 0 or expire_days < 0:
        raise ValueError(i18n.t("the value cannot be negative"))
    ids = raw.get("inbound_ids") or []
    if isinstance(ids, str):
        ids = [p.strip() for p in ids.split(",") if p.strip()]
    # Order matters: a client is added down the list and the first failure
    # stops it, exactly as the old single setting behaved.
    inbound_ids = list(dict.fromkeys(int(i) for i in ids))
    return {
        "id": str(raw.get("id") or _new_id()),
        "name": name,
        "limit_gb": limit_gb,
        "expire_days": expire_days,
        "inbound_ids": inbound_ids,
        "created_at": int(raw.get("created_at") or _now_ms()),
    }


def _clean_code(raw: dict) -> dict:
    """
    One code. `uses_left` of None means it never runs out.

    A `kind` left over from an older file is ignored rather than migrated: the
    number of activations says everything it used to say, and the field simply
    stops being written the next time the file is saved.
    """
    word = str(raw.get("word") or "").strip()
    if not word:
        raise ValueError(i18n.t("the code word cannot be empty"))
    uses_left = raw.get("uses_left")
    if uses_left is not None and str(uses_left).strip() != "":
        uses_left = max(int(uses_left), 0)
    else:
        uses_left = None
    return {
        "word": word,
        "tariff_id": str(raw.get("tariff_id") or ""),
        "enabled": raw.get("enabled") is not False,
        # None: unlimited. A number: how many registrations are left in it.
        "uses_left": uses_left,
        # Whether a letter carrying this code waits for the administrator.
        # The code's own, from the moment it is made: a tariff edited later must
        # never quietly change how a word already handed out behaves.
        "confirm": bool(raw.get("confirm")),
        "used_by": [str(x) for x in (raw.get("used_by") or [])],
        # What the code is for, in the administrator's own words. Never sent
        # anywhere — it is a note on a list, so that a page of codes is not ten
        # anonymous strings.
        "note": str(raw.get("note") or "").strip(),
        "created_at": int(raw.get("created_at") or _now_ms()),
    }


def _write(state: dict):
    """Writes the json atomically, so an interrupted write leaves no broken file."""
    tmp_path = TARIFFS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, TARIFFS_PATH)


def _seed_from_settings() -> dict:
    """
    The first tariff, built out of what the installation already had.

    Before tariffs there was one code word and one set of registration
    defaults. They become a tariff called "Basic", and for the people already
    registered nothing changes at all — the tariff only describes what the next
    client will get, which is what those settings described too.
    """
    tariff = _clean_tariff({
        "name": i18n.t("Basic [tariff]"),
        "limit_gb": getattr(config, "LIMIT_GB", 0),
        "expire_days": getattr(config, "EXPIRE_DAYS", 0),
        "inbound_ids": list(getattr(config, "XUI_INBOUND_IDS", []) or []),
    })
    codes = []
    word = str(getattr(config, "CODEWORD", "") or "").strip()
    if word:
        codes.append(_clean_code({"word": word, "tariff_id": tariff["id"],
                                  "uses_left": None, "confirm": False}))
    return {"tariffs": [tariff], "codes": codes}


def load():
    """
    Reads tariffs.json, or builds the first tariff out of the old settings.

    Idempotent: run.py and admin/app.py both call it, and the second call
    simply re-reads the file.
    """
    global _state
    with _lock:
        if not os.path.exists(TARIFFS_PATH):
            _state = _seed_from_settings()
            try:
                _write(_state)
                logger.info(f"tariffs.json did not exist; created the "
                            f"{_state['tariffs'][0]['name']!r} tariff out of the current settings.")
            except OSError as e:
                logger.error(f"Could not write {TARIFFS_PATH}: {e}. "
                             f"Running on the tariff held in memory.")
            return snapshot()

        try:
            with open(TARIFFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            logger.error(f"Could not read {TARIFFS_PATH}: {e}. "
                         f"Registration is refused until this is fixed — guessing at "
                         f"a tariff would hand out the wrong limits.")
            _state = {"tariffs": [], "codes": []}
            return snapshot()

        tariffs, codes = [], []
        for raw in data.get("tariffs") or []:
            try:
                tariffs.append(_clean_tariff(raw))
            except (ValueError, TypeError) as e:
                logger.error(f"A tariff in tariffs.json is invalid ({e}); skipping it.")
        known = {t["id"] for t in tariffs}
        for raw in data.get("codes") or []:
            try:
                code = _clean_code(raw)
            except (ValueError, TypeError) as e:
                logger.error(f"A code in tariffs.json is invalid ({e}); skipping it.")
                continue
            if code["tariff_id"] not in known:
                logger.warning(f"Code {code['word']!r} points at a tariff that is gone; skipping it.")
                continue
            codes.append(code)

        _state = {"tariffs": tariffs, "codes": codes}
        logger.info(f"Tariffs read from tariffs.json: {len(tariffs)}, codes: {len(codes)}.")
        return snapshot()


def snapshot() -> dict:
    """A copy of the whole state, safe to hand to a template."""
    with _lock:
        return json.loads(json.dumps(_state))


def all_tariffs() -> list:
    with _lock:
        return [dict(t) for t in _state["tariffs"]]


def all_codes() -> list:
    with _lock:
        return [dict(c) for c in _state["codes"]]


def get(tariff_id: str):
    with _lock:
        for tariff in _state["tariffs"]:
            if tariff["id"] == tariff_id:
                return dict(tariff)
        return None


def codes_for(tariff_id: str) -> list:
    with _lock:
        return [dict(c) for c in _state["codes"] if c["tariff_id"] == tariff_id]


def word_owner(word: str, ignore_tariff: str = None):
    """
    The tariff a word already belongs to, if any.

    Words have to be unique across every tariff: two tariffs answering to the
    same word would make what a letter gets depend on the order they happen to
    be stored in.
    """
    needle = normalise_word(word)
    with _lock:
        for code in _state["codes"]:
            if normalise_word(code["word"]) != needle:
                continue
            if ignore_tariff and code["tariff_id"] == ignore_tariff:
                continue
            return get(code["tariff_id"])
        return None


def save_tariff(values: dict) -> dict:
    """
    Creates or updates one tariff.

    The words that open it are codes of their own and are not touched here: a
    tariff can have none, one or several, and editing its traffic figure has no
    business switching any of them off.
    """
    with _lock:
        tariff = _clean_tariff(values)
        # The name is the client group in 3x-ui, so two tariffs sharing one
        # would be one group holding both — and no way to tell them apart.
        clash = [t for t in _state["tariffs"]
                 if t["id"] != tariff["id"]
                 and t["name"].strip().casefold() == tariff["name"].strip().casefold()]
        if clash:
            raise ValueError(i18n.t("a tariff named {name} already exists", name=clash[0]["name"]))
        existing = [t for t in _state["tariffs"] if t["id"] == tariff["id"]]
        if existing:
            tariff["created_at"] = existing[0]["created_at"]
            _state["tariffs"] = [tariff if t["id"] == tariff["id"] else t
                                 for t in _state["tariffs"]]
        else:
            _state["tariffs"].append(tariff)
        _write(_state)
        logger.info(f"Tariff {tariff['name']!r} saved.")
        return dict(tariff)


def delete_tariff(tariff_id: str):
    """
    Removes a tariff and every code pointing at it.

    Clients created from it are not touched: they carry their own limits in
    3x-ui, and a tariff is only ever the template they were stamped from.
    """
    with _lock:
        tariff = get(tariff_id)
        if not tariff:
            return False
        _state["tariffs"] = [t for t in _state["tariffs"] if t["id"] != tariff_id]
        _state["codes"] = [c for c in _state["codes"] if c["tariff_id"] != tariff_id]
        _write(_state)
        logger.info(f"Tariff {tariff['name']!r} deleted; the clients already created from it are unaffected.")
        return True


def generate_word() -> str:
    """
    A code nobody could guess, in characters nobody will misread.

    Used for a personal way in, where the word is carried to one person rather
    than remembered by everybody — and typed by hand off a screen, which is why
    the alphabet is missing the pairs that look alike.
    """
    import secrets
    with _lock:
        for _ in range(20):
            word = "".join(secrets.choice(GENERATED_ALPHABET) for _ in range(GENERATED_LENGTH))
            if not word_owner(word):
                return word
    # pragma: no cover — twenty collisions in a 31^10 space
    raise ValueError(i18n.t("could not come up with a free code word"))


def get_code(word: str):
    """One code by its word, or None."""
    needle = normalise_word(word)
    with _lock:
        for code in _state["codes"]:
            if normalise_word(code["word"]) == needle:
                return dict(code)
        return None


def save_code(values: dict, was: str = None) -> dict:
    """
    Creates or updates one code.

    `was` is the word it had before, when an existing code is being renamed —
    the word is the identity here, so a rename has to say which row it means.

    `uses_left` of None is a word anybody may use as often as they like; a
    number is how many registrations are left in it. That number is the whole
    difference between a public word and a personal invitation, which is why it
    is a field on the form and not a kind stored beside it.
    """
    with _lock:
        code = _clean_code(values)
        if not get(code["tariff_id"]):
            raise ValueError(i18n.t("the tariff was not found"))

        previous = get_code(was) if was else None
        clash = word_owner(code["word"])
        if clash and not (previous and normalise_word(previous["word"]) == normalise_word(code["word"])):
            raise ValueError(i18n.t("the word {word} already opens the tariff {name}",
                                    word=code["word"], name=clash["name"]))

        if previous:
            # Whoever came in through it is history, not a setting: a rename
            # must not lose the record of who used the code.
            code["used_by"] = previous["used_by"]
            code["created_at"] = previous["created_at"]
            needle = normalise_word(previous["word"])
            _state["codes"] = [code if normalise_word(c["word"]) == needle else c
                               for c in _state["codes"]]
        else:
            _state["codes"].append(code)
        _write(_state)
        logger.info(f"Code {code['word']!r} saved for the "
                    f"{get(code['tariff_id'])['name']!r} tariff.")
        return dict(code)


def set_code_enabled(word: str, enabled: bool):
    """Puts a code out, or brings one back. The row and its history stay either way."""
    needle = normalise_word(word)
    with _lock:
        for code in _state["codes"]:
            if normalise_word(code["word"]) == needle:
                code["enabled"] = bool(enabled)
                _write(_state)
                return dict(code)
        return None


def delete_code(word: str) -> bool:
    """
    Removes a code outright.

    Putting it out is usually the better answer — it keeps the record of who
    came in through the code — so this is for tidying up codes nobody used.
    """
    needle = normalise_word(word)
    with _lock:
        before = len(_state["codes"])
        _state["codes"] = [c for c in _state["codes"]
                           if normalise_word(c["word"]) != needle]
        if len(_state["codes"]) == before:
            return False
        _write(_state)
        return True


def match(word: str):
    """
    The (tariff, code) a word opens, or None.

    A disabled code and a spent one answer to nothing: the word is then simply
    not a word this installation knows, which is what somebody who got hold of
    a revoked invitation should see.
    """
    needle = normalise_word(word)
    with _lock:
        for code in _state["codes"]:
            if normalise_word(code["word"]) != needle:
                continue
            if not code["enabled"]:
                return None
            if code["uses_left"] is not None and code["uses_left"] <= 0:
                return None
            tariff = get(code["tariff_id"])
            return (tariff, dict(code)) if tariff else None
        return None


def code_used_by(address: str):
    """
    The code somebody came in through, or None.

    Looked up by address across every code's `used_by`. It is history and not a
    setting: the answer does not change when the code is switched off, renamed
    or when the client is moved to another tariff. Nothing has it for a client
    registered before codes existed or added by hand in 3x-ui, and None is the
    honest answer there rather than a guess from their tariff.
    """
    needle = str(address or "").strip().lower()
    if not needle:
        return None
    with _lock:
        for code in _state["codes"]:
            if any(str(x).strip().lower() == needle for x in code["used_by"]):
                return dict(code)
        return None


def live_words() -> list:
    """Every word a letter could carry, for the bot to look for."""
    with _lock:
        return [c["word"] for c in _state["codes"]
                if c["enabled"] and (c["uses_left"] is None or c["uses_left"] > 0)]


def spend(word: str, email: str):
    """
    Marks one use of a code against the address that used it.

    An invitation is spent here and never opens again; a permanent word only
    records who came in through it. Called after the client exists, not before:
    a code burned on a registration that then failed would be worse than one
    used twice.
    """
    needle = normalise_word(word)
    with _lock:
        for code in _state["codes"]:
            if normalise_word(code["word"]) != needle:
                continue
            if email and email not in code["used_by"]:
                code["used_by"].append(email)
            if code["uses_left"] is not None:
                code["uses_left"] = max(code["uses_left"] - 1, 0)
            _write(_state)
            return dict(code)
        return None
