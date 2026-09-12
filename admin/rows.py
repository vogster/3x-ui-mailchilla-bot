"""
Flattening a 3x-ui client into a single row for tables and lists.

It lives apart because both the clients list and the broadcast recipients need
it: each shows status, traffic and the registration date, and those had better
be worked out the same way in both places.
"""
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


def _fmt_expiry(expiry_time_ms):
    """Formats the expiry (ms epoch) into something readable."""
    if not expiry_time_ms:
        return i18n.t("No expiry")
    try:
        return datetime.fromtimestamp(int(expiry_time_ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _client_row(client_obj: dict, online: set = None) -> dict:
    """
    Turns a 3x-ui client object into a flat row for the table.

    `online` is the set of identifiers the panel reports as connected. Leaving
    it None means nobody asked — and "nobody asked" is not "nobody is
    connected", so the row then says None rather than False and the table draws
    no dot at all. An older 3x-ui has no such endpoint, and inventing an answer
    for it would be worse than saying nothing.
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
        "bare_email": bare_email,
        "enable": client_obj.get("enable") is True,
        "online": None if online is None else (remark in online),
        "used_gb": _fmt_gb(used),
        # The number alone, plus a flag: templates decide how to word "no limit",
        # and the wording is translated rather than compared against.
        "limit_gb": _fmt_gb(total),
        "unlimited": total == 0,
        "percent": percent,
        "expiry": _fmt_expiry(client_obj.get("expiryTime", 0)),
        "sub_id": client_obj.get("subId", ""),
    }
