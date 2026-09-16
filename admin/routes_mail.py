"""
The mail pages: the folders of the bot's mailbox, one letter, its attachments.

Only for looking. Everything that reaches the server goes through mailfolders,
which opens folders read-only and fetches with PEEK, so opening a letter here
never marks it read — the bot takes an unread letter as one it still owes an
answer.

Beside a letter stands the client it is about, if there is one. That is the
reason this page is in the panel rather than in a mail client: the person who
wrote and what they have are read together.
"""
import logging
import re
from datetime import datetime
from urllib.parse import quote

from markupsafe import Markup, escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

import config
import i18n
import mailfolders
from admin.deps import require_auth, templates
from admin.rows import client_row
from xui_client import XuiClient, get_shared_client

logger = logging.getLogger(__name__)
router = APIRouter()


# Matched in the escaped text, so a quote or an angle bracket around an address
# is already an entity by then — and an entity is where the address stops.
_URL = re.compile(r"https?://(?:(?!&#3[49];|&lt;|&gt;|&quot;)[^\s<>\"'])+")


def linkify(text: str) -> Markup:
    """
    A text letter with its addresses made clickable, and nothing else let through.

    Escaped first and linked second, so what becomes a link is only ever text the
    escaping has already made harmless. A subscription link is the thing most
    often pasted into these letters, and copying one by hand out of a <pre> is
    how a character goes missing.
    """
    escaped = str(escape(text or ""))

    def link(match):
        url = match.group(0)
        # Trailing punctuation belongs to the sentence, not to the address.
        tail = ""
        while url and url[-1] in ".,;:!?)":
            tail = url[-1] + tail
            url = url[:-1]
        return (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>{tail}')

    return Markup(_URL.sub(link, escaped))


templates.env.filters["linkify"] = linkify


def folder_tabs():
    """The tabs above the list, in the panel's language."""
    return [
        {"key": "inbox", "label": i18n.t("Inbox")},
        {"key": "sent", "label": i18n.t("Sent [folder]")},
        {"key": "trash", "label": i18n.t("Trash")},
    ]


def _problem(error: Exception, folder: str) -> str:
    """What went wrong, in words a person can act on."""
    if isinstance(error, mailfolders.MailboxNotSetUp):
        return i18n.t("The mailbox is not set up")
    if isinstance(error, mailfolders.FolderMissing):
        names = {tab["key"]: tab["label"] for tab in folder_tabs()}
        return i18n.t("The mailbox has no folder that can be taken for “{name}”.",
                      name=names.get(folder, folder))
    return i18n.t("Could not read the mailbox") + f": {type(error).__name__}: {error}"


def _clients_by_address():
    """
    Every client, keyed by address, from one request.

    None when 3x-ui cannot say — the pages then leave the client out rather than
    calling everybody a stranger.
    """
    clients = get_shared_client().get_all_clients()
    if clients is None:
        return None
    found = {}
    for client in clients:
        remark = (client.get("email") or "").strip().lower()
        bare = XuiClient.extract_bare_email(remark)
        for key in (bare, remark):
            if key and key not in found:
                found[key] = client
    return found


def _client_panel(client_obj: dict) -> dict:
    """The short account of a client that stands beside a letter."""
    from admin.routes_clients import _came_by

    row = client_row(client_obj)
    expiry_ms = int(client_obj.get("expiryTime", 0) or 0)
    days_left = None
    if expiry_ms > 0:
        days_left = round((expiry_ms / 1000 - datetime.now().timestamp()) / 86400)
    row.update({
        "days_left": days_left,
        "expired": days_left is not None and days_left < 0,
        "came_by": _came_by(client_obj),
    })
    return row


def _return_query(page: int, q: str) -> str:
    """
    The page and search a letter was opened from, carried on its link.

    So that "back" returns to page three of a search rather than to the top of
    the Inbox, without the letter page having to guess.
    """
    parts = []
    if page and page > 1:
        parts.append(f"page={page}")
    if q:
        parts.append(f"q={quote(q)}")
    return ("?" + "&".join(parts)) if parts else ""


def _back_url(folder: str, page: int, q: str) -> str:
    query = _return_query(page, q)
    return f"/mail?folder={folder}" + ("&" + query[1:] if query else "")


@router.get("/mail", response_class=HTMLResponse)
def mail_list(request: Request, folder: str = "inbox", page: int = 1, q: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    if folder not in mailfolders.FOLDERS:
        folder = "inbox"
    q = (q or "").strip()[:200]

    listing = None
    error = ""
    try:
        listing = mailfolders.list_letters(folder, page=page, query=q)
    except Exception as e:
        if not isinstance(e, (mailfolders.MailboxNotSetUp, mailfolders.FolderMissing)):
            logger.warning(f"Could not read the {folder} folder for the panel: {e}")
        error = _problem(e, folder)

    # Which rows are somebody on the client list. Asked only when there are rows
    # to mark: a page that failed to reach the mailbox has no business waiting
    # on 3x-ui as well.
    clients_known = False
    if listing and listing["rows"]:
        by_address = _clients_by_address()
        clients_known = by_address is not None
        for row in listing["rows"]:
            client = (by_address or {}).get(row["person"]["address"])
            row["client"] = client_row(client) if client else None

    return templates.TemplateResponse(
        "mail.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "folder": folder,
            "tabs": folder_tabs(),
            "listing": listing,
            "return_query": _return_query(listing["page"] if listing else 1, q),
            "clients_known": clients_known,
            "q": q,
            "error": error,
        },
    )


@router.get("/mail/{folder}/{uid}", response_class=HTMLResponse)
def mail_letter(request: Request, folder: str, uid: str, images: str = "",
                page: int = 1, q: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if folder not in mailfolders.FOLDERS or not uid.isdigit():
        raise HTTPException(status_code=404, detail=i18n.t("The letter was not found"))

    back = _back_url(folder, page, q)
    context = {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "folder": folder,
        "tabs": folder_tabs(),
        "uid": uid,
        "back": back,
        "page": page,
        "q": q,
        "letter": None,
        "error": "",
    }
    try:
        found = mailfolders.fetch_raw(folder, uid)
    except Exception as e:
        if not isinstance(e, (mailfolders.MailboxNotSetUp, mailfolders.FolderMissing)):
            logger.warning(f"Could not open letter {uid} in {folder} for the panel: {e}")
        context["error"] = _problem(e, folder)
        return templates.TemplateResponse("mail_letter.html", context)
    if not found:
        raise HTTPException(status_code=404, detail=i18n.t("The letter was not found"))

    raw, flags = found
    letter = mailfolders.parse_letter(raw, remote_images=images == "1")
    person, outgoing = mailfolders.correspondent(
        letter["from"], letter["to"] + letter["cc"], mailfolders.own_addresses())

    by_address = _clients_by_address()
    client_obj = (by_address or {}).get((person or {}).get("address", ""))

    context.update({
        "letter": letter,
        "seen": "\\Seen" in flags,
        "person": person,
        "outgoing": outgoing,
        "is_admin": bool(person and config.ADMIN_EMAIL
                         and person["address"] == config.ADMIN_EMAIL.strip().lower()),
        "client": _client_panel(client_obj) if client_obj else None,
        "clients_known": by_address is not None,
    })
    return templates.TemplateResponse("mail_letter.html", context)


@router.get("/mail/{folder}/{uid}/attachments/{index}")
def mail_attachment(request: Request, folder: str, uid: str, index: int):
    """
    One attachment, always as a download.

    Never inline, and never under its own type: an HTML file somebody mailed in
    would otherwise open on the panel's origin, signed in, with its scripts
    running. The browser is told it is bytes and to save them.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if folder not in mailfolders.FOLDERS or not uid.isdigit():
        raise HTTPException(status_code=404)

    try:
        found = mailfolders.fetch_raw(folder, uid)
    except Exception as e:
        logger.warning(f"Could not fetch an attachment of letter {uid} in {folder}: {e}")
        raise HTTPException(status_code=502, detail=_problem(e, folder))
    if not found:
        raise HTTPException(status_code=404)
    part = mailfolders.attachment_from(found[0], index)
    if not part:
        raise HTTPException(status_code=404)

    name, _, payload = part
    fallback = "".join(ch if ch.isascii() and ch.isprintable() and ch not in '"\\;' else "_"
                       for ch in name) or "attachment"
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{fallback}\"; "
                                   # safe="": quote()'s default leaves '/' unescaped, which
                                   # RFC 5987's ext-value grammar does not allow in a filename.
                                   f"filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )
