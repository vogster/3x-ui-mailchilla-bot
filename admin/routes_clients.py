"""Client routes: the list, search, enable/disable, editing and deletion."""
import logging
import time
import uuid
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Request, Form, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse

import config
import email_bot
import i18n
import templates as mail_templates
import tariffs
from admin.deps import templates, require_auth, is_authenticated
from xui_client import XuiClient, get_shared_client

logger = logging.getLogger(__name__)
router = APIRouter()

from admin.rows import GB_FACTOR, _fmt_gb, _fmt_last_seen, _fmt_ts, _client_row


def _attach_links(rows: list, links: list) -> list:
    """
    Hangs each ready-made URL on the inbound it belongs to.

    The panel returns the links in its own order — by inbound id — while a
    client carries its inbounds in the order they were attached, so the two
    lists cannot simply be zipped. What they do share is the port, which sits
    in the URL's authority; where several inbounds listen on the same one, the
    name the panel writes into the URL fragment tells them apart.
    """
    spare = list(links)
    for row in rows:
        match = None
        for link in spare:
            if link["port"] and link["port"] == row["port"]:
                # the port alone is enough unless another inbound shares it
                same_port = [l for l in spare if l["port"] == row["port"]]
                if len(same_port) > 1:
                    named = [l for l in same_port if l["label"] == row["name"]]
                    match = named[0] if named else link
                else:
                    match = link
                break
        if match is None:
            match = next((l for l in spare if l["label"] == row["name"]), None)
        if match is not None:
            row["url"] = match["url"]
            spare.remove(match)
    return spare


def _client_detail(client_obj: dict, inbounds: list, links: list = None,
                   online: set = None, last_online: dict = None) -> dict:
    """The fuller view of a client, for its own page."""
    row = _client_row(client_obj, online, last_online)
    traffic_obj = client_obj.get("traffic") or {}
    up = int(traffic_obj.get("up") or client_obj.get("up") or 0)
    down = int(traffic_obj.get("down") or client_obj.get("down") or 0)

    expiry_ms = int(client_obj.get("expiryTime", 0) or 0)
    if expiry_ms > 0:
        days_left = round((expiry_ms / 1000 - datetime.now().timestamp()) / 86400)
    else:
        days_left = None

    # The names of the inbounds this client belongs to, plus those configured for
    # new registrations that this client is missing.
    known = {ib["id"]: ib for ib in inbounds}
    client_inbounds = list(client_obj.get("inboundIds") or [])
    sub_id = client_obj.get("subId", "")

    row.update({
        "up_gb": _fmt_gb(up),
        "down_gb": _fmt_gb(down),
        "days_left": days_left,
        "expired": days_left is not None and days_left < 0,
        "sub_url": f"{config.XUI_SUBSCRIPTION_BASE_URL}/{sub_id}" if sub_id else "",
        "inbounds": [
            {
                "id": i,
                "name": (known.get(i) or {}).get("remark") or f"inbound {i}",
                "protocol": (known.get(i) or {}).get("protocol") or "",
                "port": (known.get(i) or {}).get("port"),
                # an inbound may be switched off entirely in the panel
                "enable": (known.get(i) or {}).get("enable", True),
                "known": i in known,
                # filled in below for every inbound the panel gave a link for
                "url": "",
            }
            for i in client_inbounds
        ],
        "flow": client_obj.get("flow") or "—",
        "security": client_obj.get("security") or "—",
        "limit_ip": client_obj.get("limitIp", 0),
        "created_at": _fmt_ts(client_obj.get("createdAt")),
        "updated_at": _fmt_ts(client_obj.get("updatedAt")),
    })
    # Links the panel returned for an inbound the client no longer shows: none
    # are expected, and a lost link is better than a row with no name on it.
    leftover = _attach_links(row["inbounds"], links or [])
    if leftover:
        logger.warning(f"The panel returned {len(leftover)} link(s) for {row.get('email')!r} "
                       f"that match none of its inbounds.")
    return row


@router.get("/clients", response_class=HTMLResponse)
def clients_list(request: Request, q: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    all_clients = xui.get_all_clients() or []
    # One question for the whole table rather than one per row. None means the
    # panel could not say, and the column then keeps quiet.
    online_emails = xui.get_online_emails()
    online = None if online_emails is None else set(online_emails)
    # Likewise one question for the whole table. An older panel does not know
    # the route, and the column is then left out rather than filled with dashes.
    last_online = xui.get_last_online()
    rows = [_client_row(c, online, last_online) for c in all_clients]

    query = (q or "").strip().lower()
    if query:
        rows = [
            r for r in rows
            if query in (r["remark"] or "").lower()
            or query in (r["bare_email"] or "").lower()
            or query in (r["comment"] or "").lower()
        ]

    # Active ones first, then by address — by whatever leads the table row
    rows.sort(key=lambda r: (not r["enable"], (r["remark"] or r["bare_email"]).lower()))

    context = {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "clients": rows,
        "q": q,
        "total": len(rows),
        # Whether to draw the dots and offer the filter at all.
        "online_known": online is not None,
        # Whether the last-online column has anything to stand on.
        "last_seen_known": last_online is not None,
        # The tariffs a client could be on, for the filter. Taken from the
        # tariffs themselves rather than from the rows, so a tariff nobody is
        # on yet is still offered — and a group left behind by a deleted tariff
        # still filters, since it is in the rows.
        "tariff_names": sorted({t["name"] for t in tariffs.all_tariffs()}
                               | {r["tariff"] for r in rows if r["tariff"]}),
        # Names that no tariff answers to any more: the tariff was deleted and
        # its clients kept the label, because deleting a template has never
        # been a reason to touch the people stamped from it. The list marks
        # those rather than pretending the tariff is still there.
        "live_tariffs": sorted(t["name"] for t in tariffs.all_tariffs()),
    }
    context.update(new_dialog_context())
    return templates.TemplateResponse("clients.html", context)


def tariff_choices() -> list:
    """
    The tariffs a hand-made client can be stamped from, shaped for the form.

    Picking one fills the fields in and leaves them editable: a tariff is a
    template here exactly as it is for a letter, and an administrator creating
    one client by hand may well want a different limit for them.
    """
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "limit_gb": t["limit_gb"],
            "expire_days": t["expire_days"],
            "inbound_ids": t["inbound_ids"],
        }
        for t in tariffs.all_tariffs()
    ]


def default_tariff() -> dict:
    """
    What an untouched create form starts from: the first tariff, or nothing.

    With no tariffs at all the form simply opens empty rather than refusing —
    a client can still be made by hand, and the tariffs page says what is missing.
    """
    choices = tariff_choices()
    return choices[0] if choices else {"id": "", "name": "", "limit_gb": 0,
                                       "expire_days": 0, "inbound_ids": []}


def new_dialog_context():
    """
    The context for the create-client dialog. Both the list and the dashboard
    need it, so it is assembled separately: the inbounds ticked according to the
    tariff chosen, plus the default values.
    """
    first = default_tariff()
    configured = set(first["inbound_ids"])
    inbounds = get_shared_client().get_inbounds()
    for ib in inbounds:
        ib["selected"] = ib["id"] in configured
    return {
        "inbounds": inbounds,
        "tariffs": tariff_choices(),
        "new_defaults": {
            "tariff_id": first["id"],
            "limit_gb": first["limit_gb"],
            "expire_days": first["expire_days"],
            "flow": config.XUI_FLOW,
        },
    }


def _new_client_context(request: Request, form: dict = None, error: str = ""):
    """The context for the create form: the defaults come from the first tariff."""
    form = form or {}
    first = default_tariff()
    selected = form.get("inbound_ids")
    if selected is None:
        selected = list(first["inbound_ids"])

    inbounds = get_shared_client().get_inbounds()
    for ib in inbounds:
        ib["selected"] = ib["id"] in selected

    return {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "inbounds": inbounds,
        "tariffs": tariff_choices(),
        "error": error,
        "form": {
            "email": form.get("email", ""),
            "comment": form.get("comment", ""),
            "tariff_id": form.get("tariff_id", first["id"]),
            "limit_gb": form.get("limit_gb", first["limit_gb"]),
            "expire_days": form.get("expire_days", first["expire_days"]),
            "xui_flow": form.get("xui_flow", config.XUI_FLOW),
            "send_email": form.get("send_email", True),
        },
    }


@router.get("/clients/online")
def clients_online(request: Request):
    """
    Who is connected, so the list can refresh its dots without a reload.

    Declared above /clients/{client_uuid}: routes are matched in the order they
    are written, and "online" would otherwise be taken for an identifier.

    A 502 when the panel cannot say. The page then leaves the dots as they are
    rather than blanking them — a failed request is not evidence that everybody
    went offline.

    The last-online column rides along, already formatted: the same strings the
    list was rendered with come from _fmt_last_seen, and working them out here
    keeps the wording, the units and the week-old cutoff in one place instead of
    a second copy in JavaScript. Clients the panel reports as connected are left
    out of the map — the page has its own word for those.
    """
    if not is_authenticated(request):
        return JSONResponse({"error": i18n.t("You need to sign in")}, status_code=401)
    xui = get_shared_client()
    emails = xui.get_online_emails()
    if emails is None:
        return JSONResponse({"ok": False}, status_code=502)
    payload = {"ok": True, "online": emails}
    last_online = xui.get_last_online()
    if last_online is not None:
        now_ms = int(time.time() * 1000)
        connected = set(emails)
        payload["last_seen"] = {
            remark: {"text": _fmt_last_seen(ms, now_ms), "title": _fmt_ts(ms), "ms": ms}
            for remark, ms in last_online.items()
            if remark not in connected
        }
    return JSONResponse(payload)


@router.get("/clients/new", response_class=HTMLResponse)
def client_new_form(request: Request):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    return templates.TemplateResponse("client_new.html", _new_client_context(request))


@router.post("/clients/new", response_class=HTMLResponse)
def client_new_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(""),
    comment: str = Form(""),
    limit_gb: str = Form(""),
    expire_days: str = Form(""),
    send_email: str = Form(""),
    xui_flow: str = Form(""),
    tariff_id: str = Form(""),
    inbound_ids: list[int] = Form(default=[]),
):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    raw = {
        "email": (email or "").strip(),
        "comment": (comment or "").strip(),
        "limit_gb": (limit_gb or "").strip(),
        "expire_days": (expire_days or "").strip(),
        "inbound_ids": inbound_ids,
        "xui_flow": (xui_flow or "").strip(),
        "tariff_id": (tariff_id or "").strip(),
        "send_email": send_email == "on",
    }

    # The dialog posts the form through fetch and expects JSON, so it can show
    # the error in place without losing what was typed. An ordinary form submit,
    # with no JS, still gets a page carrying the error and the filled fields.
    wants_json = request.headers.get("x-requested-with") == "fetch"

    def fail(message):
        if wants_json:
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        return templates.TemplateResponse(
            "client_new.html", _new_client_context(request, raw, message), status_code=400
        )

    client_email = email_bot.build_client_email(raw["email"])
    if not client_email:
        return fail(i18n.t("Give an email address."))
    if raw["send_email"] and "@" not in client_email:
        return fail(i18n.t("Sending a letter needs a real address, with an @ in it."))
    if not raw["inbound_ids"]:
        return fail(i18n.t("Choose at least one inbound."))

    def parse_int(value, name):
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            raise ValueError(i18n.t("The {name} field holds something that is not a number", name=name))
        if number < 0:
            raise ValueError(i18n.t("The {name} field cannot be negative", name=name))
        return number

    try:
        total_gb = parse_int(raw["limit_gb"], i18n.t("Limit, GB"))
        days = parse_int(raw["expire_days"], i18n.t("Term, days"))
    except ValueError as e:
        return fail(str(e))

    xui = get_shared_client()
    if xui.find_client_by_email(client_email):
        return fail(i18n.t("A client with the address {email} is already in the panel.", email=client_email))

    # The chosen tariff names the client's group. The numbers may well have been
    # edited away from it by hand, and that is allowed: the group says which
    # tariff this client belongs to, not that every figure still matches it.
    chosen = tariffs.get(raw["tariff_id"]) if raw["tariff_id"] else None
    logger.info(f"Panel: creating client {client_email!r} (name {raw['comment']!r}), "
                f"limit {total_gb} GB, {days} days, flow {raw['xui_flow']!r}, "
                f"inbounds {raw['inbound_ids']}"
                + (f", tariff {chosen['name']!r}." if chosen else "."))
    created_uuid, _ = xui.add_client(
        email=client_email,
        client_uuid=str(uuid.uuid4()),
        limit_gb=total_gb,
        expire_days=days,
        inbound_ids=raw["inbound_ids"],
        comment=raw["comment"],
        flow=raw["xui_flow"],
        group=chosen["name"] if chosen else "",
    )

    client_obj = xui.find_client_by_email(client_email)
    if not client_obj:
        return fail(i18n.t("The 3x-ui panel did not create the client — the details are in the logs."))
    if not created_uuid:
        logger.warning(f"The panel returned an error while creating {client_email}, "
                       f"yet the client appeared — probably partially. Check its inbounds.")

    key = XuiClient.client_key(client_obj)
    if raw["send_email"]:
        sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client_obj.get('subId')}"
        background_tasks.add_task(_send_welcome, client_email, sub_url, days, total_gb)

    target = f"/clients/{key}?created=1"
    if wants_json:
        return JSONResponse({"ok": True, "redirect": target})
    return RedirectResponse(url=target, status_code=303)


def _send_welcome(email_addr: str, sub_url: str, expire_days: int, limit_gb: int,
                  renewed: bool = False):
    """The letter leaves after the response: sending takes seconds and has no reason to sit inside the request."""
    try:
        email_bot.send_welcome_email(email_addr, sub_url, expire_days, limit_gb, renewed=renewed)
        logger.info(f"Welcome letter sent to {email_addr}.")
    except Exception as e:
        logger.error(f"Could not send the welcome letter to {email_addr}: {e}")


def _came_by(client_obj: dict):
    """The code a client registered through, shaped for the card."""
    row = _client_row(client_obj)
    code = (tariffs.code_used_by(row["bare_email"])
            or tariffs.code_used_by(row["remark"]))
    if not code:
        return None
    tariff = tariffs.get(code["tariff_id"])
    return {"word": code["word"], "tariff": tariff["name"] if tariff else ""}


@router.get("/clients/{client_uuid}", response_class=HTMLResponse)
def client_detail(request: Request, client_uuid: str, created: str = "", sent: str = "", error: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    client_obj = xui.find_client_by_uuid(client_uuid)
    if not client_obj:
        raise HTTPException(status_code=404, detail=i18n.t("The client was not found"))

    # Same questions the list asks, and the same None when the panel cannot say.
    online_emails = xui.get_online_emails()
    online = None if online_emails is None else set(online_emails)
    last_online = xui.get_last_online()

    return templates.TemplateResponse(
        "client_detail.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            # The links are a separate request; an empty answer only costs the
            # inbound chips their copy button.
            "client": _client_detail(client_obj, xui.get_inbounds(),
                                     xui.get_client_links(client_obj.get("email") or ""),
                                     online, last_online),
            "online_known": online is not None,
            "last_seen_known": last_online is not None,
            # Which word let them in, if it is known. A fact about how they
            # arrived, not about what they have now — the two can differ, since
            # a client can be moved to another tariff afterwards.
            "came_by": _came_by(client_obj),
            "kinds": mail_templates.broadcast_kind_options(),
            "created": created == "1",
            "sent": sent,
            "error": error,
        },
    )


def _safe_back(back: str, client_uuid: str) -> str:
    """Where to return after an action: local paths only, otherwise back to the list."""
    if back == "detail":
        return f"/clients/{client_uuid}"
    return "/clients"


def _send_personal(email_addr: str, subject: str, body: str, kind: str = "plain"):
    """Leaves after the response: the receiving server takes seconds."""
    try:
        email_bot.send_personal_email(email_addr, subject, body, kind)
        logger.info(f"Personal letter sent to {email_addr}. Subject: {subject!r}")
    except Exception as e:
        logger.error(f"Could not send the personal letter to {email_addr}: {e}")


@router.post("/clients/{client_uuid}/resend")
def client_resend_welcome(request: Request, client_uuid: str, background_tasks: BackgroundTasks):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    client_obj = xui.find_client_by_uuid(client_uuid)
    if not client_obj:
        raise HTTPException(status_code=404, detail=i18n.t("The client was not found"))

    address = XuiClient.extract_bare_email(client_obj.get("email"))
    if not address:
        return RedirectResponse(
            url=f"/clients/{client_uuid}?error=" + quote(i18n.t("The client's email field holds no address, so there is nowhere to send it")),
            status_code=303,
        )
    sub_id = client_obj.get("subId")
    if not sub_id:
        return RedirectResponse(
            url=f"/clients/{client_uuid}?error=" + quote(i18n.t("The client has no subId, so the subscription link cannot be built")),
            status_code=303,
        )

    total_gb = int(client_obj.get("totalGB") or 0)
    expiry = int(client_obj.get("expiryTime") or 0)
    days = max(round((expiry / 1000 - datetime.now().timestamp()) / 86400), 0) if expiry > 0 else 0

    logger.info(f"Panel: resending the invitation to {address}.")
    background_tasks.add_task(
        _send_welcome, address,
        f"{config.XUI_SUBSCRIPTION_BASE_URL}/{sub_id}",
        days, round(total_gb / GB_FACTOR) if total_gb > 0 else 0,
        True,   # the subscription already runs — a subject without "activated!"
    )
    return RedirectResponse(url=f"/clients/{client_uuid}?sent=welcome", status_code=303)


@router.post("/clients/{client_uuid}/message")
def client_send_message(
    request: Request,
    client_uuid: str,
    background_tasks: BackgroundTasks,
    subject: str = Form(""),
    message: str = Form(""),
    kind: str = Form("plain"),
):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    client_obj = xui.find_client_by_uuid(client_uuid)
    if not client_obj:
        raise HTTPException(status_code=404, detail=i18n.t("The client was not found"))

    if kind not in mail_templates.BROADCAST_KINDS:
        kind = mail_templates.DEFAULT_KIND
    address = XuiClient.extract_bare_email(client_obj.get("email"))
    subject = (subject or "").strip()
    message = (message or "").strip()

    if not address:
        problem = i18n.t("The client's email field holds no address, so there is nowhere to send it")
    elif not message:
        problem = i18n.t("The letter has no text")
    else:
        problem = ""
    if problem:
        return RedirectResponse(url=f"/clients/{client_uuid}?error=" + quote(problem), status_code=303)

    if not subject:
        subject = i18n.t("A message from {service}", service=config.SERVICE_NAME)

    logger.info(f"Panel: personal letter to {address}. Subject: {subject!r}")
    background_tasks.add_task(_send_personal, address, subject, message, kind)
    return RedirectResponse(url=f"/clients/{client_uuid}?sent=message", status_code=303)


@router.post("/clients/{client_uuid}/toggle")
def client_toggle(request: Request, client_uuid: str, back: str = Form("")):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    client_obj = xui.find_client_by_uuid(client_uuid)
    if not client_obj:
        raise HTTPException(status_code=404, detail=i18n.t("The client was not found"))

    currently_enabled = client_obj.get("enable") is True
    logger.info(f"Panel: {'disabling' if currently_enabled else 'enabling'} client "
                f"{client_obj.get('email')!r}.")
    ok = xui.set_client_enabled(client_uuid, not currently_enabled, client_obj=client_obj)
    if not ok:
        raise HTTPException(status_code=500, detail=i18n.t("Could not change the client's status"))

    return RedirectResponse(url=_safe_back(back, client_uuid), status_code=303)


@router.get("/clients/{client_uuid}/edit", response_class=HTMLResponse)
def client_edit_form(request: Request, client_uuid: str, error: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    client_obj = get_shared_client().find_client_by_uuid(client_uuid)
    if not client_obj:
        raise HTTPException(status_code=404, detail=i18n.t("The client was not found"))

    row = _client_row(client_obj)
    client_inbounds = [int(i) for i in (client_obj.get("inboundIds") or [])]
    inbounds = get_shared_client().get_inbounds()
    for ib in inbounds:
        ib["selected"] = ib["id"] in client_inbounds

    # The current values, to prefill the form
    total_gb = int(client_obj.get("totalGB", 0) or 0)
    limit_gb_val = round(total_gb / GB_FACTOR, 2) if total_gb > 0 else 0
    expiry_ms = int(client_obj.get("expiryTime", 0) or 0)
    if expiry_ms > 0:
        days_left = max(round((expiry_ms / 1000 - datetime.now().timestamp()) / 86400), 0)
    else:
        days_left = 0

    return templates.TemplateResponse(
        "client_edit.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "client": row,
            "inbounds": inbounds,
            "tariffs": tariff_choices(),
            "limit_gb": limit_gb_val,
            "days_left": days_left,
            "error": error,
        },
    )


@router.post("/clients/{client_uuid}/edit")
def client_edit_submit(
    request: Request,
    client_uuid: str,
    limit_gb: str = Form(""),
    expire_days: str = Form(""),
    enable: str = Form(""),
    comment: str = Form(""),
    keep_comment: str = Form(""),
    tariff_id: str = Form(""),
    inbound_ids: list[int] = Form(default=[]),
):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    client_obj = xui.find_client_by_uuid(client_uuid)
    if not client_obj:
        raise HTTPException(status_code=404, detail=i18n.t("The client was not found"))

    def parse_int(raw, field_name):
        raw = (raw or "").strip()
        if raw == "":
            return None
        try:
            value = int(float(raw))
            if value < 0:
                raise ValueError
            return value
        except ValueError:
            raise HTTPException(status_code=400,
                            detail=i18n.t("The {name} field holds something that is not a number",
                                          name=field_name))

    total_gb = parse_int(limit_gb, i18n.t("Limit, GB"))
    days = parse_int(expire_days, i18n.t("Term, days"))

    # enable arrives from the checkbox
    new_enable = enable == "on"

    # comment: update it when the "keep_comment" checkbox is NOT ticked.
    # The client's email field is never changed from the UI: it is an identifier
    # with a limited character set, and requests to the panel are keyed by it.
    new_comment = None
    if keep_comment != "on":
        new_comment = (comment or "").strip()

    # An empty tariff_id is "leave it as it is" — the picker's first option —
    # rather than "take the client out of every group", which nothing on this
    # form asks for.
    moved = tariffs.get(tariff_id) if tariff_id else None
    logger.info(
        f"Panel: editing client {client_obj.get('email')!r} — limit={total_gb}, "
        f"days={days}, enabled={new_enable}, name={new_comment!r}"
        + (f", moved to the {moved['name']!r} tariff." if moved else ".")
    )
    ok = xui.update_client(
        client_uuid,
        total_gb=total_gb,
        expire_days=days,
        enable=new_enable,
        new_comment=new_comment,
        group=moved["name"] if moved else None,
        client_obj=client_obj,
    )
    if not ok:
        return RedirectResponse(
            url=f"/clients/{client_uuid}/edit?error=" + quote(i18n.t("Could not save the changes")),
            status_code=303
        )

    # The inbounds change after the fields. They are attached and detached on
    # their own now, so the order no longer decides what a client ends up with —
    # but the fields are the likelier thing to fail, and failing before the
    # inbounds move leaves less half-done.
    current_inbounds = sorted(int(i) for i in (client_obj.get("inboundIds") or []))
    if sorted(inbound_ids) != current_inbounds:
        if not inbound_ids:
            return RedirectResponse(
                url=f"/clients/{client_uuid}/edit?error=" + quote(i18n.t("At least one inbound has to be left")),
                status_code=303,
            )
        fresh = xui.find_client_by_uuid(client_uuid)
        if not xui.set_client_inbounds(client_uuid, inbound_ids, client_obj=fresh):
            return RedirectResponse(
                url=f"/clients/{client_uuid}/edit?error=" + quote(i18n.t("Could not change the set of inbounds")),
                status_code=303,
            )

    return RedirectResponse(url=f"/clients/{client_uuid}", status_code=303)


@router.post("/clients/{client_uuid}/delete")
def client_delete(request: Request, client_uuid: str, keep_traffic: str = Form("")):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    victim = xui.find_client_by_uuid(client_uuid)
    logger.warning(f"Panel: deleting client {(victim or {}).get('email')!r} "
                   f"(keep traffic: {keep_traffic == 'on'}).")
    ok, _ = xui.delete_client(client_uuid, keep_traffic=keep_traffic == "on", client_obj=victim)
    if not ok:
        raise HTTPException(status_code=500, detail=i18n.t("Could not delete the client"))

    return RedirectResponse(url="/clients", status_code=303)
