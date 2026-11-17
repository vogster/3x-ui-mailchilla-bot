"""
Auto-renewal: what a tariff may ask for, and what 3x-ui holds for a client.

3x-ui can renew a client by itself: at the moment the subscription expires it
moves the expiry on and starts the traffic count again. When the next expiry
falls is one of three things — so many days on, the next such day of the month,
or the next such day of the week — and there may be a cap on how many times it
happens, after which the subscription simply ends.

The panel keeps that as four numbers on the client (`reset`, `resetDay`,
`resetWeekday`, `resetMax`), of which the first three are three ways of saying
one thing and may not all be set: a weekly renewal together with either of the
others is refused outright, and a day of the month quietly wins over a period.
Here it is one mode and one value, so that a combination the panel refuses
cannot be written down in the first place, and `panel_fields` is the only
place that knows how the one becomes the other.

Nothing in this module asks the panel anything. `clients/list` already carries
the four numbers on every row and how many renewals have fired in its
`traffic`, and the moment of the next renewal is the expiry itself — which is
why `clients/renewalPreview` is not called from anywhere.
"""
import i18n

DAYS, MONTHDAY, WEEKDAY = "days", "monthday", "weekday"
MODES = ("", DAYS, MONTHDAY, WEEKDAY)

# The largest value each mode takes. Ten years is far past any term somebody
# means; the other two are the calendar's own.
_LARGEST = {DAYS: 3650, MONTHDAY: 31, WEEKDAY: 7}

# Past this a cap is no cap, and the field says so with a nought instead.
MAX_RENEWALS = 1000

# The keys a client row has to carry for the panel to count as knowing renewal.
# All four, not the first: `reset` — a renewal every so many days — is far
# older in 3x-ui than the calendar modes and the cap. A panel with that one
# alone would be offered a day of the month, take the update, drop the key it
# has never heard of, and the client's renewal would be off without a word.
_PANEL_KEYS = ("reset", "resetDay", "resetWeekday", "resetMax")

# Monday first, as 3x-ui counts them: 1 is Monday and 7 is Sunday. Worded as
# "on Mondays" rather than "Monday" because that is the one form that reads
# rightly both in a list to pick from and at the end of a sentence — in
# Russian the bare name would need a case and a gender to agree with.
_WEEKDAYS = ("on Mondays", "on Tuesdays", "on Wednesdays", "on Thursdays",
             "on Fridays", "on Saturdays", "on Sundays")


def none() -> dict:
    """No renewal. A new dict each time: callers keep what they are handed."""
    return {"mode": "", "value": 0, "max": 0}


def _number(value) -> int:
    """A whole number out of a form field or a json value; empty is nought."""
    if value is None or isinstance(value, bool):
        return 0
    text = str(value).strip()
    return int(text) if text else 0


def clean(raw, expire_days) -> dict:
    """
    One renewal setting, checked and coerced, or ValueError saying what is
    wrong in the panel's language.

    `expire_days` is the term it comes with, because a renewal without one is
    not a setting 3x-ui acts on: it renews at the moment of expiry, and a
    subscription that never expires never reaches it. The panel takes such a
    client without a word and then does nothing, which a tariff must not be
    allowed to promise.
    """
    if raw is None or raw == "":
        raw = {}
    if not isinstance(raw, dict):
        # A tariffs.json edited by hand into something else. Said as the
        # ValueError the loader expects, so that it skips the one tariff
        # rather than failing to read the file at all.
        raise ValueError(i18n.t("the auto-renewal mode is not one of those offered"))
    mode = str(raw.get("mode") or "").strip()
    if mode not in MODES:
        raise ValueError(i18n.t("the auto-renewal mode is not one of those offered"))
    if not mode:
        # Whatever else came with it: a value beside "off" is a leftover of
        # the form, not an instruction.
        return none()
    try:
        value = _number(raw.get("value"))
        cap = _number(raw.get("max"))
    except (TypeError, ValueError):
        raise ValueError(i18n.t("the auto-renewal fields hold something that is not a whole number"))
    if not 1 <= value <= _LARGEST[mode]:
        raise ValueError({
            DAYS: i18n.t("the renewal period is a number of days from 1 to 3650"),
            MONTHDAY: i18n.t("the day of the month is a number from 1 to 31"),
            WEEKDAY: i18n.t("the day of the week is not one of the seven"),
        }[mode])
    if not 0 <= cap <= MAX_RENEWALS:
        raise ValueError(i18n.t("the number of renewals is from 0 to 1000, and 0 is no limit"))
    try:
        term = int(expire_days or 0)
    except (TypeError, ValueError):
        term = 0
    if term <= 0:
        raise ValueError(i18n.t("auto-renewal needs a term: a subscription that never "
                                "expires is never renewed"))
    return {"mode": mode, "value": value, "max": cap}


def from_form(form) -> dict:
    """
    The renewal as a form posted it, unchecked — `clean` does the checking.

    The form carries one field per mode and this takes the one the mode names.
    They are separate fields so that the form works with no JavaScript to swap
    one input for another, and so that a value typed under one mode is not read
    as another's when the mode is changed.
    """
    mode = str(form.get("renew_mode") or "").strip()
    value = {
        DAYS: form.get("renew_days"),
        MONTHDAY: form.get("renew_monthday"),
        WEEKDAY: form.get("renew_weekday"),
    }.get(mode)
    return {"mode": mode, "value": value or 0, "max": form.get("renew_max") or 0}


def panel_fields(renew, always: bool = False) -> dict:
    """
    The four keys 3x-ui keeps a renewal in.

    Empty for no renewal, so that whoever creates a client can add the result
    to the request as it is: a panel from before the fields is then asked
    exactly what it always was, as with a device limit. `always` is for an
    update that switches a renewal off, where the noughts are the instruction.

    Exactly one of the first three is ever non-zero — which is the whole reason
    the tariff holds a mode and a value rather than these.
    """
    renew = renew or {}
    mode = renew.get("mode") or ""
    if not mode and not always:
        return {}
    value = max(int(renew.get("value") or 0), 0) if mode else 0
    return {
        "reset": value if mode == DAYS else 0,
        "resetDay": value if mode == MONTHDAY else 0,
        "resetWeekday": value if mode == WEEKDAY else 0,
        "resetMax": max(int(renew.get("max") or 0), 0) if mode else 0,
    }


def of_client(client_obj):
    """
    The renewal a client has in 3x-ui, or None for a panel that does not say.

    The two are different answers, as with a device limit: None is a 3x-ui
    whose clients do not carry the fields — all four of them, see _PANEL_KEYS
    — and offering to set a renewal there would be offering something that is
    not kept; a dict with an empty mode is a client who simply has none.

    Besides the mode, the value and the cap:
      used    how many renewals have fired
      left    how many are still to come, None when there is no cap
      active  whether the next expiry will actually renew — false once the cap
              is spent, and false for a client with no expiry, whom the panel
              never gets round to
    """
    if not client_obj or any(key not in client_obj for key in _PANEL_KEYS):
        return None

    def number(source, key) -> int:
        try:
            return max(int((source or {}).get(key) or 0), 0)
        except (TypeError, ValueError):
            return 0

    # In the panel's own order of precedence: a weekly renewal cannot stand
    # beside the others at all, and a day of the month wins over a period.
    weekday = number(client_obj, "resetWeekday")
    monthday = number(client_obj, "resetDay")
    days = number(client_obj, "reset")
    if 1 <= weekday <= 7:
        mode, value = WEEKDAY, weekday
    elif 1 <= monthday <= 31:
        mode, value = MONTHDAY, monthday
    elif days:
        mode, value = DAYS, days
    else:
        return {**none(), "used": 0, "left": None, "active": False}

    cap = number(client_obj, "resetMax")
    used = number(client_obj.get("traffic"), "resetCount")
    left = max(cap - used, 0) if cap else None
    try:
        expires = int(client_obj.get("expiryTime") or 0) != 0
    except (TypeError, ValueError):
        expires = False
    return {"mode": mode, "value": value, "max": cap, "used": used, "left": left,
            "active": expires and (left is None or left > 0)}


def same(one, other) -> bool:
    """Whether two renewals ask for the same thing, whatever else rides on them."""
    def key(renew):
        renew = renew or {}
        mode = renew.get("mode") or ""
        if not mode:
            return ("", 0, 0)
        return (mode, int(renew.get("value") or 0), int(renew.get("max") or 0))
    return key(one) == key(other)


def weekday_options() -> list:
    """The seven days as (number, words) pairs in the panel's language, Monday first."""
    return [(number, i18n.t(words)) for number, words in enumerate(_WEEKDAYS, start=1)]


def describe(renew, cap: bool = True) -> str:
    """
    A renewal in a few words of the panel's language; empty for none.

    For the tariff list and the client's card. A letter says the same thing in
    its own language and its own editable texts — see templates._renewal_text.
    """
    renew = renew or {}
    mode = renew.get("mode") or ""
    value = int(renew.get("value") or 0)
    if mode == DAYS:
        words = i18n.t("every {days} d", days=value)
    elif mode == MONTHDAY:
        words = i18n.t("monthly, on day {day}", day=value)
    elif mode == WEEKDAY and 1 <= value <= 7:
        words = i18n.t("weekly, {weekday}", weekday=i18n.t(_WEEKDAYS[value - 1]))
    else:
        return ""
    limit = int(renew.get("max") or 0)
    if cap and limit:
        words += ", " + i18n.t("renewals: up to {count}", count=limit)
    return words
