"""
The tariffs page: what a new client gets, and the words that open it.

Two things live here, and they are deliberately not the same thing. A tariff is
a template read once, when a client is created — nothing on this page ever
reaches a client who already exists. A code is a way in: a word, the tariff it
opens, how many activations are left in it, and a switch. A word everybody may
use and a personal invitation are the same object with a different number in
that field, which is why there is one form for both.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import config
import i18n
import tariffs
from admin.deps import templates, require_auth
from admin.rows import client_row
from xui_client import XuiClient, get_shared_client

logger = logging.getLogger(__name__)
router = APIRouter()


def _code_row(code: dict, tariff_names: dict) -> dict:
    """One code, shaped for a table."""
    spent = code["uses_left"] is not None and code["uses_left"] <= 0
    expired = tariffs.is_expired(code)
    return {
        **code,
        "tariff_name": tariff_names.get(code["tariff_id"], ""),
        "used": ", ".join(code["used_by"]),
        "used_count": code["used_total"],
        # Whether the list is the whole story or only its end.
        "used_trimmed": code["used_total"] > len(code["used_by"]),
        "used_kept": len(code["used_by"]),
        "unlimited": code["uses_left"] is None,
        "created": datetime.fromtimestamp(code["created_at"] / 1000).strftime("%d.%m.%Y"),
        "expires": (datetime.fromtimestamp(code["expires_at"] / 1000).strftime("%d.%m.%Y")
                    if code["expires_at"] else ""),
        # The form wants the date back in the shape <input type="date"> speaks.
        "expires_input": (datetime.fromtimestamp(code["expires_at"] / 1000).strftime("%Y-%m-%d")
                          if code["expires_at"] else ""),
        # Four states worth telling apart: still open, used up, out of date, and
        # switched off by hand. Only the last is a decision; the middle two are
        # a code that did its job or a date that passed.
        "state": ("off" if not code["enabled"]
                  else "spent" if spent
                  else "expired" if expired
                  else "live"),
    }


def _code_rows(codes: list, tariff_names: dict) -> list:
    """Codes newest first."""
    return [_code_row(c, tariff_names)
            for c in sorted(codes, key=lambda c: c["created_at"], reverse=True)]


def _names() -> dict:
    return {t["id"]: t["name"] for t in tariffs.all_tariffs()}


def _rows():
    """Every tariff with its codes and the inbounds it hands out."""
    known = {ib["id"]: ib for ib in (get_shared_client().get_inbounds() or [])}
    names = _names()
    rows = []
    for tariff in tariffs.all_tariffs():
        codes = _code_rows(tariffs.codes_for(tariff["id"]), names)
        rows.append({
            **tariff,
            "codes": codes,
            "live_codes": [c for c in codes if c["state"] == "live"],
            "arrived": sum(c["used_count"] for c in codes),
            "inbounds": [
                {
                    "id": i,
                    "name": (known.get(i) or {}).get("remark") or f"inbound {i}",
                    "known": i in known,
                    "enable": (known.get(i) or {}).get("enable", True),
                }
                for i in tariff["inbound_ids"]
            ],
        })
    return rows


def _clients_on(tariff_name: str) -> list:
    """
    Everybody 3x-ui holds in this tariff's group.

    Read from the panel on every view rather than kept anywhere: the group is
    3x-ui's field, somebody may have changed it there, and a cached answer would
    be the one thing on the page that could be wrong.
    """
    rows = [client_row(c) for c in (get_shared_client().get_all_clients() or [])]
    return sorted((r for r in rows if r["tariff"] == tariff_name),
                  key=lambda r: (not r["enable"], (r["remark"] or r["bare_email"]).lower()))


def _arrivals(code: dict) -> list:
    """
    The people who came in through a code, matched against the clients that
    exist now.

    An address in `used_by` that no client answers to any more is not dropped:
    somebody deleted that client, and the code's history is still a fact.
    """
    known = {}
    for client in (get_shared_client().get_all_clients() or []):
        row = client_row(client)
        # By address, and by the identifier 3x-ui holds as well: a client added
        # by hand may carry something that is not an address at all, and one
        # renamed since would otherwise read as deleted.
        for key in (row["bare_email"], (row["remark"] or "").strip().lower()):
            if key:
                known.setdefault(key, row)
    out = []
    for address in code["used_by"]:
        row = known.get(str(address).strip().lower())
        out.append({"address": address, "client": row})
    return out


def _inbound_picker(selected):
    """The inbound list with the tariff's own ones ticked."""
    chosen = set(selected or [])
    inbounds = get_shared_client().get_inbounds() or []
    for ib in inbounds:
        ib["selected"] = ib["id"] in chosen
    return inbounds


@router.get("/tariffs", response_class=HTMLResponse)
def tariffs_list(request: Request, saved: str = "", error: str = "", issued: str = "",
                 tab: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    names = _names()
    # Groups in 3x-ui whose tariff is gone. Deleting a tariff deliberately
    # leaves its clients alone — they carry their own limits and a label is not
    # a limit — so the labels outlive it, and the page says so instead of
    # leaving them to be noticed in the client list one day.
    live = set(names.values())
    orphans = {}
    for client in (get_shared_client().get_all_clients() or []):
        group = (client.get("group") or "").strip()
        if group and group not in live:
            orphans[group] = orphans.get(group, 0) + 1

    return templates.TemplateResponse(
        "tariffs.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "tariffs": _rows(),
            "codes": _code_rows(tariffs.all_codes(), names),
            "tariff_options": [{"id": i, "name": n} for i, n in names.items()],
            "saved": saved,
            "error": error,
            # A code just made is shown once, large, with a copy button: a
            # generated one has to be carried to somebody, and hunting for it in
            # a list of ten similar strings is how the wrong one gets sent.
            "issued": issued,
            # Which tab to open on arrival, when a redirect wants to say.
            "tab": tab,
            "orphans": sorted(orphans.items()),
        },
    )


# ---------------------------------------------------------------- tariffs ---

def _form_context(request: Request, tariff: dict = None, error: str = ""):
    return {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "tariff": tariff or {"id": "", "name": "", "limit_gb": config.LIMIT_GB,
                             "expire_days": config.EXPIRE_DAYS, "inbound_ids": []},
        "inbounds": _inbound_picker((tariff or {}).get("inbound_ids")),
        "error": error,
    }


@router.get("/tariffs/new", response_class=HTMLResponse)
def tariff_new(request: Request):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    return templates.TemplateResponse("tariff_edit.html", _form_context(request))


@router.get("/tariffs/{tariff_id}/edit", response_class=HTMLResponse)
def tariff_edit(request: Request, tariff_id: str):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    tariff = tariffs.get(tariff_id)
    if not tariff:
        return RedirectResponse("/tariffs", status_code=303)
    return templates.TemplateResponse("tariff_edit.html", _form_context(request, tariff))


@router.post("/tariffs/save", response_class=HTMLResponse)
async def tariff_save(request: Request,
                      tariff_id: str = Form(""),
                      name: str = Form(""),
                      limit_gb: str = Form("0"),
                      expire_days: str = Form("0")):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    values = {
        "id": tariff_id or None,
        "name": name,
        "limit_gb": limit_gb or 0,
        "expire_days": expire_days or 0,
        # The checkboxes are absent when 3x-ui did not answer with a list; an
        # edit made while the panel is unreachable then keeps what it had
        # rather than quietly emptying the tariff.
        "inbound_ids": (form.getlist("inbound_ids") if form.get("inbounds_present")
                        else (tariffs.get(tariff_id) or {}).get("inbound_ids", [])),
    }
    fresh = not tariff_id
    was_named = (tariffs.get(tariff_id) or {}).get("name", "")
    try:
        saved = tariffs.save_tariff(values)
    except (ValueError, TypeError) as e:
        broken = dict(values)
        broken["id"] = tariff_id
        broken["inbound_ids"] = [int(i) for i in values["inbound_ids"] if str(i).isdigit()]
        return templates.TemplateResponse(
            "tariff_edit.html", _form_context(request, broken, error=str(e)))
    except OSError as e:
        logger.error(f"Could not write tariffs.json: {e}")
        return templates.TemplateResponse(
            "tariff_edit.html",
            _form_context(request, values,
                          error=i18n.t("Could not write the settings file: {error}", error=e)))

    # The tariff's name is the client group in 3x-ui, so renaming one has to
    # carry the clients across. An empty group — a tariff nobody has registered
    # on — is not a row in the panel and the call simply reports nothing to do.
    if was_named and was_named != saved["name"]:
        get_shared_client().rename_group(was_named, saved["name"])

    # A brand-new tariff has no way into it yet, and saying so later is worse
    # than offering the code form now, with the tariff already chosen.
    if fresh:
        return RedirectResponse(f"/tariffs/codes/new?tariff={saved['id']}", status_code=303)
    return RedirectResponse(f"/tariffs?saved={saved['name']}", status_code=303)


@router.post("/tariffs/{tariff_id}/delete")
def tariff_delete(request: Request, tariff_id: str):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if tariffs.get(tariff_id):
        try:
            tariffs.delete_tariff(tariff_id)
        except OSError as e:
            logger.error(f"Could not write tariffs.json: {e}")
            return RedirectResponse(f"/tariffs?error={e}", status_code=303)
    return RedirectResponse("/tariffs", status_code=303)


# ------------------------------------------------------------------ codes ---

def _end_of_day(value: str) -> int:
    """
    A date from the form as milliseconds at the end of that day, or 0.

    The end rather than the start: somebody writing "valid until the 20th"
    means the 20th counts, and a code that stopped working at midnight on the
    19th would be a small, annoying surprise.
    """
    value = (value or "").strip()
    if not value:
        return 0
    # ISO from the field's hidden half, and the shape a person types when there
    # is no JavaScript to translate it.
    for shape in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            day = datetime.strptime(value, shape)
        except ValueError:
            continue
        return int(day.replace(hour=23, minute=59, second=59).timestamp() * 1000)
    return 0


def _code_form_context(request: Request, code: dict = None, error: str = "",
                       was: str = ""):
    return {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "code": code or {},
        "was": was,
        "tariff_options": [{"id": t["id"], "name": t["name"]} for t in tariffs.all_tariffs()],
        # The generate button works in the browser, from the same alphabet, so
        # that pressing it does not cost a round trip.
        "alphabet": tariffs.GENERATED_ALPHABET,
        "length": tariffs.GENERATED_LENGTH,
        "error": error,
    }


@router.get("/tariffs/codes/new", response_class=HTMLResponse)
def code_new(request: Request, tariff: str = "", generated: str = "1"):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    known = tariffs.all_tariffs()
    if not known:
        return RedirectResponse("/tariffs", status_code=303)
    return templates.TemplateResponse(
        "code_edit.html",
        _code_form_context(request, {
            "word": tariffs.generate_word() if generated == "1" else "",
            "tariff_id": tariff or known[0]["id"],
            "uses_left": 1,
            "expires_input": "",
            "note": "",
            "enabled": True,
        }))


@router.get("/tariffs/codes/{word}/edit", response_class=HTMLResponse)
def code_edit(request: Request, word: str):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    code = tariffs.get_code(word)
    if not code:
        return RedirectResponse("/tariffs?tab=codes", status_code=303)
    return templates.TemplateResponse(
        "code_edit.html",
        _code_form_context(request, _code_row(code, _names()), was=code["word"]))


@router.post("/tariffs/codes/save", response_class=HTMLResponse)
def code_save(request: Request,
              was: str = Form(""),
              word: str = Form(""),
              tariff_id: str = Form(""),
              uses_left: str = Form(""),
              expires_on: str = Form(""),
              note: str = Form(""),
              enabled: str = Form("")):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    values = {
        "word": (word or "").strip(),
        "tariff_id": tariff_id,
        # A date names a day, and a code good "until Sunday" has to work all
        # Sunday — so it runs out at the end of the day chosen, not at its
        # first second.
        "expires_at": _end_of_day(expires_on),
        # An empty field means "no limit": that is the public word everybody
        # writes, and it is the one case where a blank is a real answer rather
        # than something left unfilled.
        "uses_left": (uses_left or "").strip() or None,
        "note": note,
        "enabled": enabled == "on",
    }
    try:
        saved = tariffs.save_code(values, was=was or None)
    except (ValueError, TypeError) as e:
        broken = dict(values)
        broken["expires_input"] = (expires_on or "").strip()
        return templates.TemplateResponse(
            "code_edit.html", _code_form_context(request, broken, error=str(e), was=was))
    except OSError as e:
        logger.error(f"Could not write tariffs.json: {e}")
        return templates.TemplateResponse(
            "code_edit.html",
            _code_form_context(request, values, was=was,
                               error=i18n.t("Could not write the settings file: {error}", error=e)))

    # A freshly made code is shown once, in full, on the page it lands on.
    if not was:
        return RedirectResponse(f"/tariffs?issued={saved['word']}", status_code=303)
    return RedirectResponse("/tariffs?tab=codes", status_code=303)


@router.get("/tariffs/codes/{word}", response_class=HTMLResponse)
def code_card(request: Request, word: str):
    """One code: what it is, and who came in through it."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    code = tariffs.get_code(word)
    if not code:
        return RedirectResponse("/tariffs?tab=codes", status_code=303)
    return templates.TemplateResponse(
        "code_card.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "code": _code_row(code, _names()),
            "tariff": tariffs.get(code["tariff_id"]),
            "arrivals": _arrivals(code),
        },
    )


@router.post("/tariffs/codes/{word}/{action}")
def code_action(request: Request, word: str, action: str):
    """Putting a code out, bringing it back, or removing it from the list."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    if not tariffs.get_code(word):
        return RedirectResponse("/tariffs?tab=codes", status_code=303)
    try:
        if action == "off":
            tariffs.set_code_enabled(word, False)
        elif action == "on":
            tariffs.set_code_enabled(word, True)
        elif action == "delete":
            tariffs.delete_code(word)
    except OSError as e:
        logger.error(f"Could not write tariffs.json: {e}")
        return RedirectResponse(f"/tariffs?error={e}", status_code=303)
    return RedirectResponse("/tariffs?tab=codes", status_code=303)


@router.get("/tariffs/{tariff_id}", response_class=HTMLResponse)
def tariff_card(request: Request, tariff_id: str):
    """
    One tariff: what it hands out, the codes that open it, and who is on it.

    Declared last, so that /tariffs/new and everything under /tariffs/codes/
    are matched by their own routes first.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    tariff = tariffs.get(tariff_id)
    if not tariff:
        return RedirectResponse("/tariffs", status_code=303)

    known = {ib["id"]: ib for ib in (get_shared_client().get_inbounds() or [])}
    return templates.TemplateResponse(
        "tariff_card.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "tariff": tariff,
            "codes": _code_rows(tariffs.codes_for(tariff_id), _names()),
            "clients": _clients_on(tariff["name"]),
            "inbounds": [
                {
                    "id": i,
                    "name": (known.get(i) or {}).get("remark") or f"inbound {i}",
                    "known": i in known,
                }
                for i in tariff["inbound_ids"]
            ],
        },
    )
