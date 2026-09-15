"""The web panel for running the 3x-ui email bot (FastAPI)."""
import secrets
import logging
import time
from datetime import datetime

from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

import applog
import config
import email_bot
import email_texts
import i18n
import settings as app_settings
import tariffs
import updater
from admin.deps import (
    templates,
    check_panel_configured,
    is_authenticated,
    require_auth,
    safe_path,
)

logger = logging.getLogger(__name__)

# Lay settings.json over .env before anything reads config.
app_settings.load()
email_texts.load()
# After the settings: a first run builds the opening tariff out of them.
tariffs.load()
applog.install()
# Idempotent, like the two above: the panel may be imported on its own, without
# run.py having started anything.
updater.start()

app = FastAPI(title="3x-ui Email Bot Admin", docs_url=None, redoc_url=None, openapi_url=None)

# Secret for signing the session cookie. Unset means a random one, so sessions
# do not survive a restart — acceptable for a panel that runs locally.
SESSION_SECRET = config.ADMIN_PANEL_SECRET or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="xui_admin_session",
    max_age=60 * 60 * 12,  # 12 hours
    same_site="lax",
    https_only=False,
)


# ---------------------------------------------------------------------------
# Authentication (templates / require_auth live in admin/deps.py)
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    check_panel_configured()
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "service_name": config.SERVICE_NAME},
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    check_panel_configured()
    if (
        secrets.compare_digest(username.encode("utf-8"), config.ADMIN_PANEL_USER.encode("utf-8"))
        and secrets.compare_digest(password.encode("utf-8"), config.ADMIN_PANEL_PASSWORD.encode("utf-8"))
    ):
        request.session.clear()
        request.session["user"] = username
        logger.info(f"Signed in: user {username}, address {request.client.host if request.client else '?'}.")
        # only local paths: see safe_path on why a leading slash is not enough
        return RedirectResponse(url=safe_path(next), status_code=303)
    logger.warning(
        f"Failed sign-in attempt: login {username!r}, "
        f"address {request.client.host if request.client else '?'}."
    )
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": i18n.t("The login or the password is wrong"),
            "service_name": config.SERVICE_NAME,
        },
        status_code=401,
    )


@app.post("/lang")
def switch_language(request: Request, lang: str = Form(""), next: str = Form("/")):
    """
    Switches the interface language.

    Deliberately open without signing in: the choice belongs on the sign-in
    screen, which has to be readable to whoever is looking at it. All that can
    be done here is to set one of the languages the panel ships with — the code
    is checked against the catalogue, and the return address must be a local
    path, or an unauthenticated visitor could aim the redirect elsewhere.
    """
    try:
        app_settings.save({"PANEL_LANG": lang})
    except (ValueError, TypeError):
        logger.warning(f"Ignored a request to switch to an unknown language: {lang!r}")
    except OSError as e:
        logger.error(f"Could not store the language: {e}")

    return RedirectResponse(url=safe_path(next), status_code=303)


@app.post("/update/dismiss")
def dismiss_update(request: Request, version: str = Form(""), next: str = Form("/")):
    """
    Puts the new-version banner away until a newer one than this is released.

    The version is stored rather than a flag, so dismissing 0.2.0 says nothing
    about 0.3.0 — otherwise one impatient click would silence the banner for
    good.
    """
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    try:
        app_settings.save({"UPDATE_DISMISSED_VERSION": version})
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Could not store the dismissed version: {e}")
    return RedirectResponse(url=safe_path(next), status_code=303)


@app.get("/logout")
def logout(request: Request):
    user = request.session.get("user")
    if user:
        logger.info(f"Signed out: user {user}.")
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# The routes are mounted from separate modules
# ---------------------------------------------------------------------------
from admin.routes_clients import router as clients_router  # noqa: E402
from admin.routes_tariffs import router as tariffs_router  # noqa: E402
from admin.routes_broadcast import router as broadcast_router  # noqa: E402
from admin.routes_settings import router as settings_router  # noqa: E402
from admin.routes_logs import router as logs_router  # noqa: E402
from admin.routes_setup import router as setup_router  # noqa: E402

app.include_router(clients_router)
app.include_router(tariffs_router)
app.include_router(broadcast_router)
app.include_router(settings_router)
app.include_router(logs_router)
app.include_router(setup_router)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
GB = 1024 * 1024 * 1024


def _used_bytes(client_obj):
    traffic = client_obj.get("traffic") or {}
    return int(traffic.get("up") or client_obj.get("up") or 0) + \
           int(traffic.get("down") or client_obj.get("down") or 0)


def _size(value: int) -> str:
    """Bytes as the shortest unit that still reads as a number."""
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"


def _size_pair(section: dict) -> str:
    return f"{_size(section['used'])} / {_size(section['total'])}"


def _uptime(seconds: int) -> str:
    """How long the machine has been up, to the nearest sensible unit."""
    seconds = int(seconds or 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return i18n.t("{days} d {hours} h", days=days, hours=hours)
    if hours:
        return i18n.t("{hours} h {minutes} min", hours=hours, minutes=minutes)
    return i18n.t("{minutes} min", minutes=minutes)


def _online_count(clients=None):
    """
    How many clients the panel has seen connected lately, or None when it
    cannot say. Counted against the client list, so the figure cannot outrun
    the ones beside it.
    """
    from xui_client import get_shared_client

    xui = get_shared_client()
    emails = xui.get_online_emails()
    if emails is None:
        return None
    if clients is None:
        clients = xui.get_all_clients() or []
    known = {(c.get("email") or "") for c in clients}
    return sum(1 for e in emails if e in known)


def _mail_block():
    """
    How the mail loop is doing, in a shape the template can show.

    A loop that has quietly stopped — a changed password, a blocked mailbox —
    looks from the outside exactly like a mailbox nobody writes to, and the
    first sign of it used to be somebody complaining that the code word does
    nothing.
    """
    health = email_bot.mail_health()
    if not health["configured"]:
        return {"state": "off", "text": i18n.t("the mailbox is not set up")}
    if health["failures"] >= email_bot.FAILURES_BEFORE_ALARM:
        return {"state": "bad",
                "text": i18n.t("{n} checks in a row failed", n=health["failures"]),
                "detail": health["error"]}
    if health["last_ok_at"] is None:
        return {"state": "wait", "text": i18n.t("no check has got through yet")}
    ago = max(int(time.time() - health["last_ok_at"]), 0)
    if ago < 90:
        text = i18n.t("checked just now")
    elif ago < 3600:
        text = i18n.t("checked {n} min ago", n=ago // 60)
    else:
        text = i18n.t("checked {n} h ago", n=ago // 3600)
    # Long past the polling interval means the loop is not turning, even though
    # no single check has reported a failure.
    stalled = ago > max(config.POLL_INTERVAL_SECONDS * 4, 120)
    return {"state": "bad" if stalled else "ok", "text": text,
            "detail": health["error"] if stalled else ""}


def _server_block():
    """
    The server panel's contents, ready to display.

    Formatting happens here rather than in the browser so the page and the
    refresh that follows it cannot drift apart: both take this.
    """
    from xui_client import get_shared_client

    server = get_shared_client().get_server_status()
    if not server:
        return None
    for section in ("mem", "disk", "swap"):
        server[section]["text"] = _size_pair(server[section])
    server["uptime_text"] = _uptime(server["uptime"])
    return server


@app.get("/server-status")
def server_status(request: Request):
    """The server panel on its own, for the refresh button and the timer."""
    if not is_authenticated(request):
        return JSONResponse({"error": i18n.t("You need to sign in")}, status_code=401)

    server = _server_block()
    if not server:
        return JSONResponse({"ok": False, "error": i18n.t("The panel did not answer")},
                            status_code=502)
    # The online count sits in the row of counters rather than in this block,
    # but it goes stale at the same rate, so it travels with it.
    return JSONResponse({"ok": True, "server": server, "online": _online_count(),
                         "mail": _mail_block()})


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    from xui_client import XuiClient, get_shared_client
    import applog

    xui = get_shared_client()
    clients = xui.get_all_clients() or []

    total = len(clients)
    active = sum(1 for c in clients if c.get("enable") is True)
    blocked = total - active

    # None when the panel cannot say — an older build has no such endpoint —
    # and the card is then left out rather than showing a zero, which would
    # read as "nobody is connected".
    online = _online_count(clients)
    total_traffic_gb = round(sum(_used_bytes(c) for c in clients) / GB, 2)

    now_ms = datetime.now().timestamp() * 1000
    month_ms = 30 * 86400 * 1000

    def row(client_obj, extra=None):
        data = {
            "uuid": XuiClient.client_key(client_obj),
            "email": client_obj.get("email") or "",
            "comment": (client_obj.get("comment") or "").strip(),
            "enable": client_obj.get("enable") is True,
        }
        data.update(extra or {})
        return data

    # Recent registrations — the panel fills createdAt for every client.
    recent = sorted(clients, key=lambda c: c.get("createdAt") or 0, reverse=True)[:8]
    recent_rows = [
        row(c, {"when": datetime.fromtimestamp((c.get("createdAt") or 0) / 1000).strftime("%d.%m.%Y %H:%M")
                if c.get("createdAt") else "—"})
        for c in recent
    ]
    new_this_month = sum(1 for c in clients if (c.get("createdAt") or 0) >= now_ms - month_ms)

    # Who spends the most.
    top = sorted(clients, key=_used_bytes, reverse=True)[:8]
    top_rows = [row(c, {"used_gb": round(_used_bytes(c) / GB, 1)}) for c in top if _used_bytes(c) > 0]

    # Subscriptions running out and clients pressing against their limit: these
    # blocks appear only if anyone has an expiry or a limit set at all.
    expiring = []
    for c in clients:
        expiry = int(c.get("expiryTime") or 0)
        if expiry <= 0:
            continue
        days = (expiry / 1000 - datetime.now().timestamp()) / 86400
        if days <= 14:
            expiring.append(row(c, {
                "days": int(days) if days >= 0 else int(days),
                "expired": days < 0,
                "when": datetime.fromtimestamp(expiry / 1000).strftime("%d.%m.%Y"),
            }))
    expiring.sort(key=lambda r: r["days"])
    expiring = expiring[:8]

    near_limit = []
    for c in clients:
        limit = int(c.get("totalGB") or 0)
        if limit <= 0:
            continue
        percent = round(_used_bytes(c) / limit * 100, 1)
        if percent >= 80:
            near_limit.append(row(c, {"percent": min(percent, 100),
                                      "used_gb": round(_used_bytes(c) / GB, 1),
                                      "limit_gb": round(limit / GB, 1)}))
    near_limit.sort(key=lambda r: r["percent"], reverse=True)
    near_limit = near_limit[:8]

    # Tariffs: how many people are on each and what they have spent. Counted
    # from the client list already in hand rather than asked of
    # /clients/groups — the panel would answer the same numbers, and a page
    # that adds a request per block ends up slow on the day 3x-ui is slow.
    #
    # A group naming a tariff that no longer exists is listed too, marked: the
    # clients are still there and the figure is still real.
    by_group = {}
    for client in clients:
        group = (client.get("group") or "").strip()
        if not group:
            continue
        stats = by_group.setdefault(group, {"clients": 0, "used": 0})
        stats["clients"] += 1
        stats["used"] += _used_bytes(client)

    tariff_rows = []
    for tariff in tariffs.all_tariffs():
        stats = by_group.pop(tariff["name"], {"clients": 0, "used": 0})
        tariff_rows.append({
            "id": tariff["id"],
            "name": tariff["name"],
            "clients": stats["clients"],
            "used_gb": round(stats["used"] / GB, 1),
            "limit_gb": tariff["limit_gb"],
            "expire_days": tariff["expire_days"],
            "orphan": False,
        })
    for name, stats in sorted(by_group.items()):
        tariff_rows.append({
            "id": "", "name": name, "clients": stats["clients"],
            "used_gb": round(stats["used"] / GB, 1),
            "limit_gb": 0, "expire_days": 0, "orphan": True,
        })
    tariff_rows.sort(key=lambda r: (r["orphan"], -r["clients"], r["name"].lower()))

    # The inbound list itself belongs to 3x-ui and to the tariff cards, not
    # here; what is worth saying on a dashboard is the fault. A tariff naming an
    # inbound the panel does not have breaks registration in silence — the panel
    # adds the client down the list and stops at the first id that is not there,
    # never reaching the rest — so each such tariff is named.
    from admin.routes_clients import default_tariff, tariff_choices
    first = default_tariff()
    inbounds = xui.get_inbounds()
    for ib in inbounds:
        # the create dialog starts from the first tariff
        ib["selected"] = ib["id"] in set(first["inbound_ids"])
    known_ids = {ib["id"] for ib in inbounds}
    broken_tariffs = []
    if inbounds:
        # With 3x-ui unreachable the list comes back empty, and every tariff
        # would look broken. Silence beats a page of false alarms.
        for tariff in tariffs.all_tariffs():
            missing = sorted(set(tariff["inbound_ids"]) - known_ids)
            if missing:
                broken_tariffs.append({"name": tariff["name"], "id": tariff["id"],
                                       "missing": missing})

    # The machine behind the panel. None when it does not answer — the block
    # is then simply left out.
    server = _server_block()

    problems = applog.entries(level="WARNING", limit=6, unread_only=True)
    problems_total = applog.unread_count("WARNING")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "total": total,
            "active": active,
            "online": online,
            "blocked": blocked,
            "total_traffic_gb": total_traffic_gb,
            "new_this_month": new_this_month,
            "recent": recent_rows,
            "top": top_rows,
            "expiring": expiring,
            "near_limit": near_limit,
            "inbounds": inbounds,
            "server": server,
            "mail": _mail_block(),
            "broken_tariffs": broken_tariffs,
            "problems": problems,
            "problems_total": problems_total,
            "tariffs": tariff_choices(),
            "tariff_rows": tariff_rows,
            # Anybody carrying no group at all: registered before tariffs
            # existed, or made by hand in 3x-ui.
            "without_tariff": sum(1 for c in clients if not (c.get("group") or "").strip()),
            "new_defaults": {
                "tariff_id": first["id"],
                "limit_gb": first["limit_gb"],
                "expire_days": first["expire_days"],
                "flow": config.XUI_FLOW,
            },
        },
    )
