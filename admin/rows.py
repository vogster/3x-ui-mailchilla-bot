"""
Flattening a 3x-ui client into a single row for tables and lists.

It lives apart because both the clients list and the broadcast recipients need
it: each shows status, traffic and the registration date, and those had better
be worked out the same way in both places.
"""
import time
from datetime import datetime

import i18n
from xui_client import XuiClient

GB_FACTOR = 1024 * 1024 * 1024


def _fmt_gb(value):
    """Formats bytes as gigabytes."""
    if not value:
        return 0
    return round(int(value) / GB_FACTOR, 2)


def _fmt_ts(ts_ms):
    """Formats a timestamp (ms epoch), or returns a dash."""
    if not ts_ms:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _fmt_date(ts_ms):
    """A short date for the list column."""
    if not ts_ms:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%d.%m.%Y")
    except Exception:
        return "—"


MINUTE_MS = 60 * 1000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS


def _fmt_last_seen(ts_ms, now_ms=None) -> str:
    """
    How long ago the panel last saw the client, in as few characters as fit a
    table cell.

    The units are abbreviated on purpose. Russian would otherwise need the noun
    agreed with the number — минута, минуты, минут — and i18n.t() keys on an
    English string, so each of the three would be a separate key to keep in
    step. "мин", "ч" and "дн" do not decline, and the exact moment is a
    tooltip away in any case.

    Past a week the elapsed time stops meaning anything and a date says more,
    so the column switches to one.
    """
    ts_ms = int(ts_ms or 0)
    if ts_ms <= 0:
        return "—"
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    # A clock that disagrees with the panel's should not read as the future.
    elapsed = max(now_ms - ts_ms, 0)
    if elapsed < MINUTE_MS:
        return i18n.t("just now")
    if elapsed < HOUR_MS:
        return i18n.t("{n} min ago", n=elapsed // MINUTE_MS)
    if elapsed < DAY_MS:
        return i18n.t("{n} h ago", n=elapsed // HOUR_MS)
    if elapsed < 7 * DAY_MS:
        return i18n.t("{n} d ago", n=elapsed // DAY_MS)
    return _fmt_date(ts_ms)


def _fmt_expiry(expiry_time_ms):
    """Formats the expiry (ms epoch) into something readable."""
    if not expiry_time_ms:
        return i18n.t("No expiry")
    try:
        return datetime.fromtimestamp(int(expiry_time_ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _client_row(client_obj: dict, online: set = None, last_online: dict = None) -> dict:
    """
    Turns a 3x-ui client object into a flat row for the table.

    `online` is the set of identifiers the panel reports as connected. Leaving
    it None means nobody asked — and "nobody asked" is not "nobody is
    connected", so the row then says None rather than False and the table draws
    no dot at all. An older 3x-ui has no such endpoint, and inventing an answer
    for it would be worse than saying nothing.

    `last_online` maps the same identifiers to when each was last seen, and is
    None for the same reason and with the same consequence. A client the panel
    reports as connected is last seen now, whatever the map says: the map is
    written on a heartbeat and can lag behind the connection by a minute.
    """
    remark = client_obj.get("email", "") or ""
    bare_email = XuiClient.extract_bare_email(remark)
    traffic_obj = client_obj.get("traffic") or {}
    up = int(traffic_obj.get("up") or client_obj.get("up") or 0)
    down = int(traffic_obj.get("down") or client_obj.get("down") or 0)
    used = up + down
    total = int(client_obj.get("totalGB", 0) or 0)
    percent = min(round(used / total * 100, 1), 100) if total > 0 else 0
    created_ms = int(client_obj.get("createdAt") or 0)
    is_online = None if online is None else (remark in online)
    now_ms = int(time.time() * 1000)
    if is_online:
        last_seen_ms = now_ms
    else:
        last_seen_ms = int((last_online or {}).get(remark) or 0)
    return {
        "uuid": XuiClient.client_key(client_obj),
        "remark": remark,
        # the raw value for sorting and a ready-made string for display
        "used_bytes": used,
        # 0 means unlimited — that is how the panel itself stores it
        "limit_bytes": total,
        "created_ms": created_ms,
        "created": _fmt_date(created_ms),
        "comment": (client_obj.get("comment") or "").strip(),
        # Which tariff the client is on, as 3x-ui itself holds it: the group is
        # named after the tariff, and clients/list hands it back with the rest.
        # Empty for anyone registered before tariffs existed, or added by hand
        # in 3x-ui — which is a fact about them, not a gap to fill in.
        "tariff": (client_obj.get("group") or "").strip(),
        "bare_email": bare_email,
        "enable": client_obj.get("enable") is True,
        "online": is_online,
        # The raw value for sorting, the short string for the cell, and the
        # exact moment for its tooltip. Zero means the panel has never seen
        # this client — which sorts before everyone who has been seen.
        "last_seen_ms": last_seen_ms,
        "last_seen": i18n.t("now [last seen]") if is_online else _fmt_last_seen(last_seen_ms, now_ms),
        "last_seen_full": _fmt_ts(last_seen_ms) if last_seen_ms else "",
        "used_gb": _fmt_gb(used),
        # The number alone, plus a flag: templates decide how to word "no limit",
        # and the wording is translated rather than compared against.
        "limit_gb": _fmt_gb(total),
        "unlimited": total == 0,
        "percent": percent,
        "expiry": _fmt_expiry(client_obj.get("expiryTime", 0)),
        "sub_id": client_obj.get("subId", ""),
    }
