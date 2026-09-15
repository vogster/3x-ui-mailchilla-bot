"""
The settings page: the mailbox, the panel itself, notifications and the letters.

What a new client gets — traffic, term, inbounds, the word that opens it — used
to live here too. It belongs to a tariff now, on its own page; the keys stay in
settings.MANAGED_KEYS so that an installation rolled back to 0.1.x finds them
where it left them, but nothing reads them any more.
"""
import logging
import smtplib
import socket
import ssl
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse

import config
import email_bot
import email_texts
import i18n
import settings
import templates as mail_templates
from admin.deps import templates, require_auth
from xui_client import get_shared_client

logger = logging.getLogger(__name__)
router = APIRouter()


# These letters have a test version worth sending; the "General" group is only
# the footer, which shows up in any of them.
TESTABLE = {"welcome", "status", "help", "broadcast", "notice"}

# The placeholder addresses from .env.example look real, and must never prefill
# the send field: the letter would go to a stranger.
PLACEHOLDER_EMAILS = {
    "your-personal-email@yandex.ru",
    "your-bot-email@yandex.ru",
    "user@example.com",
}


def _default_test_email() -> str:
    address = (config.ADMIN_EMAIL or "").strip().lower()
    if "@" not in address or address in PLACEHOLDER_EMAILS:
        return ""
    return address


def _text_groups():
    """
    The groups of editable texts for the Letters tab.

    The values are those of the mail language, while the captions around them
    follow the panel's own language: someone running an English panel may well
    be writing the Russian letters.
    """
    def build(entry):
        key, label, kind, hint = entry[:4]
        field = {"key": key, "label": i18n.t(label), "kind": kind,
                 "hint": i18n.t(hint) if hint else ""}
        if kind == email_texts.SWITCH:
            # A settings key, not a text: its value comes from config and
            # there is nothing per-language to have been "changed". The fields
            # it governs travel inside it, and the page draws the lot as one
            # block so that the switch is plainly about them.
            field["value"] = bool(getattr(config, key, False))
            field["changed"] = False
            field["fields"] = [build(inner) for inner in
                               (entry[4] if len(entry) > 4 else ())]
        else:
            field["value"] = email_texts.get(key)
            field["changed"] = email_texts.is_changed(key)
        return field

    groups = []
    for g in email_texts.GROUPS:
        fields = [build(entry) for entry in g["fields"]]
        groups.append({
            "id": g["id"], "title": i18n.t(g["title"]), "hint": i18n.t(g["hint"]),
            "fields": fields,
            "changed": email_texts.changed_count(g["id"]),
            "can_test": g["id"] in TESTABLE,
            # the broadcast has a test letter in four kinds
            "kinds": mail_templates.broadcast_kind_options() if g["id"] == "broadcast" else [],
        })
    return groups


def _cleanup_last() -> str:
    """When the mailbox was last cleared out, for the line under the field."""
    when = float(getattr(config, "MAIL_CLEANUP_LAST_AT", 0) or 0)
    if not when:
        return ""
    return datetime.fromtimestamp(when).strftime("%d.%m.%Y %H:%M")


def _build_context(request: Request, error: str = "", saved: str = ""):
    state = settings.describe()
    return {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "unchanged_marker": settings.UNCHANGED,
        "text_groups": _text_groups(),
        "mail_lang": i18n.mail_lang(),
        "mail_lang_options": i18n.options(i18n.mail_lang()),
        "test_email": _default_test_email(),
        "cleanup_last": _cleanup_last(),
        "state": state,
        "error": error,
        "saved": saved,
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, error: str = "", saved: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    return templates.TemplateResponse("settings.html", _build_context(request, error, saved))


@router.post("/settings")
def settings_submit(
    request: Request,
    panel_lang: str = Form(""),
    service_name: str = Form(""),
    admin_email: str = Form(""),
    support_email: str = Form(""),
    manual_url: str = Form(""),
    xui_subscription_base_url: str = Form(""),
    imap_server: str = Form(""),
    imap_port: str = Form("993"),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    smtp_server: str = Form(""),
    smtp_port: str = Form("465"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    poll_interval_seconds: str = Form("15"),
    mail_cleanup_enabled: str = Form(""),
    mail_cleanup_days: str = Form("30"),
    update_check_enabled: str = Form(""),
    xui_flow: str = Form(""),
    remark_include_name: str = Form(""),
    gotify_url: str = Form(""),
    gotify_token: str = Form(""),
    gotify_priority: str = Form("5"),
    gotify_title: str = Form(""),
    gotify_message: str = Form(""),
    happ_url: str = Form(""),
    incy_url: str = Form(""),
):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    values = {
        "SERVICE_NAME": service_name,
        "ADMIN_EMAIL": admin_email,
        "SUPPORT_EMAIL": support_email,
        "MANUAL_URL": manual_url,
        "XUI_SUBSCRIPTION_BASE_URL": xui_subscription_base_url,
        "IMAP_SERVER": imap_server,
        "IMAP_PORT": imap_port,
        "IMAP_USER": imap_user,
        "IMAP_PASSWORD": imap_password,
        "SMTP_SERVER": smtp_server,
        "SMTP_PORT": smtp_port,
        "SMTP_USER": smtp_user,
        "SMTP_PASSWORD": smtp_password,
        "POLL_INTERVAL_SECONDS": poll_interval_seconds,
        "MAIL_CLEANUP_ENABLED": mail_cleanup_enabled == "on",
        "MAIL_CLEANUP_DAYS": mail_cleanup_days,
        "XUI_FLOW": xui_flow,
        "GOTIFY_URL": gotify_url,
        "GOTIFY_TOKEN": gotify_token,
        "GOTIFY_PRIORITY": gotify_priority,
        "GOTIFY_TITLE": gotify_title,
        "GOTIFY_MESSAGE": gotify_message,
        "HAPP_URL": happ_url,
        "INCY_URL": incy_url,
        "REMARK_INCLUDE_NAME": remark_include_name == "on",
        "UPDATE_CHECK_ENABLED": update_check_enabled == "on",
    }
    # The picker is only drawn when more than one language is on offer.
    if panel_lang:
        values["PANEL_LANG"] = panel_lang
    try:
        settings.save(values)
    except (ValueError, TypeError) as e:
        return templates.TemplateResponse(
            "settings.html",
            _build_context(request, error=i18n.t("Could not save: {error}", error=e)),
            status_code=400,
        )
    except OSError as e:
        logger.error(f"Could not write settings.json: {e}")
        return templates.TemplateResponse(
            "settings.html",
            _build_context(request, error=i18n.t("Could not write the settings file: {error}", error=e)),
            status_code=500,
        )

    return RedirectResponse(url="/settings?saved=1", status_code=303)


def _short_error(e: Exception) -> str:
    """
    A short, intelligible reason for the failure: everything goes to the log, a
    single line to the page.

    The commonest failures are network ones, and their system text ("nodename nor
    servname provided") says nothing about what to fix. Those are translated; the
    rest is shown as it stands, since a mail server's answer usually makes sense.
    """
    if isinstance(e, socket.gaierror):
        return i18n.t("the server was not found — check the address")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return i18n.t("the server did not answer within 10 seconds — check the address and the port")
    if isinstance(e, ConnectionRefusedError):
        return i18n.t("the connection was refused — most likely the wrong port")
    if isinstance(e, ssl.SSLError):
        return i18n.t("a TLS error — the port is probably not an SSL one ({detail})", detail=e.reason or e)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return i18n.t("the login or the password was not accepted ({detail})", detail=_clean(e.smtp_error))
    if isinstance(e, UnicodeEncodeError):
        # Usually a missed keyboard layout: the password was typed in Cyrillic.
        # The system text about an ascii codec says nothing of the sort.
        return i18n.t("the login or the password holds characters outside the Latin alphabet, which is not accepted")

    text = _clean(e)
    if "AUTHENTICATIONFAILED" in text.upper() or "LOGIN FAILED" in text.upper():
        return i18n.t("the login or the password was not accepted ({detail})", detail=text)
    return text


def _clean(value) -> str:
    """The error text without its wrapping: imaplib and smtplib hand back bytes."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = str(value)
    text = text.strip()
    # str(b'...') arrives complete with its quotes and prefix
    if text.startswith("b'") and text.endswith("'"):
        text = text[2:-1]
    if not text:
        text = value.__class__.__name__
    return text if len(text) <= 160 else text[:157] + "…"


@router.post("/settings/mail/test")
def settings_mail_test(
    request: Request,
    imap_server: str = Form(""),
    imap_port: str = Form("993"),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    smtp_server: str = Form(""),
    smtp_port: str = Form("465"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    service_name: str = Form(""),
):
    """
    Checks the mailbox login over IMAP and SMTP and, when an administrator
    address is set, sends a letter to it.

    It checks the values in the form fields rather than the saved ones:
    otherwise the button could not confirm new settings before they were applied
    to the whole bot. Nothing is saved along the way.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return JSONResponse({"error": i18n.t("You need to sign in")}, status_code=401)

    # A field left untouched arrives as the marker — substitute the stored value.
    if imap_password == settings.UNCHANGED:
        imap_password = settings.secret("IMAP_PASSWORD")
    if smtp_password == settings.UNCHANGED:
        smtp_password = settings.secret("SMTP_PASSWORD")

    steps = []

    def step(name, ok, detail, skipped=False):
        steps.append({"name": name, "ok": ok, "detail": detail, "skipped": skipped})

    # --- IMAP ---
    if not imap_server.strip() or not imap_user.strip():
        step("IMAP", False, i18n.t("the server or the login is empty"))
    else:
        try:
            count = email_bot.probe_imap(imap_server.strip(), imap_port,
                                         imap_user.strip(), imap_password)
            step("IMAP", True, i18n.t("signed in; letters in the Inbox: {count}", count=count))
        except Exception as e:
            logger.error(f"The IMAP check failed: {e}")
            step("IMAP", False, _short_error(e))

    # --- SMTP ---
    smtp_ok = False
    if not smtp_server.strip() or not smtp_user.strip():
        step("SMTP", False, i18n.t("the server or the login is empty"))
    else:
        try:
            email_bot.probe_smtp(smtp_server.strip(), smtp_port,
                                 smtp_user.strip(), smtp_password)
            smtp_ok = True
            step("SMTP", True, i18n.t("signed in"))
        except Exception as e:
            logger.error(f"The SMTP check failed: {e}")
            step("SMTP", False, _short_error(e))

    # --- the letter to the administrator ---
    admin_address = (config.ADMIN_EMAIL or "").strip().lower()
    if not smtp_ok:
        step(i18n.t("Letter"), False, i18n.t("not sent: SMTP does not answer"), skipped=True)
    elif not admin_address:
        step(i18n.t("Letter"), False,
             i18n.t("not sent: no administrator address is set on the General tab"),
             skipped=True)
    elif admin_address in PLACEHOLDER_EMAILS:
        step(i18n.t("Letter"), False,
             i18n.t("not sent: the administrator address is a placeholder from .env.example"),
             skipped=True)
    else:
        name = service_name.strip() or config.SERVICE_NAME
        subject = i18n.t("Mail check · {service}", service=name)
        body = i18n.t("This is a test letter from the panel.\n\n"
                      "If it arrived, SMTP accepts the bot's login and letters reach the "
                      "administrator's address. **There is no need to reply to it.**")
        logger.info(f"Panel: mail check, letter to {admin_address}.")
        try:
            email_bot.send_email_via(
                smtp_server.strip(), smtp_port, smtp_user.strip(), smtp_password,
                admin_address, subject,
                mail_templates.get_broadcast_email(subject, body, "info"),
                service_name=name,
            )
            step(i18n.t("Letter"), True, i18n.t("sent to {address}", address=admin_address))
        except Exception as e:
            logger.error(f"The test letter did not go out: {e}")
            step(i18n.t("Letter"), False, _short_error(e))

    ok = all(s["ok"] or s["skipped"] for s in steps)
    return JSONResponse({"ok": ok, "steps": steps})


@router.post("/settings/mail/names/scan")
def settings_names_scan(request: Request):
    """
    Looks through the mailbox and reports the clients whose name has drifted.

    Changes nothing: the letters are read headers-only and with PEEK, so an
    unhandled registration is not swallowed by somebody pressing this button,
    and no client is touched until the next route is called with a choice.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    if not config.IMAP_USER or not config.IMAP_PASSWORD:
        return JSONResponse({"ok": False, "error": i18n.t("The mailbox is not set up")},
                            status_code=400)

    logger.info("Panel: looking through the mailbox for sender names.")
    try:
        found = email_bot.scan_sender_names()
    except Exception as e:
        logger.error(f"Could not read the mailbox for names: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": i18n.t("Could not read the mailbox")},
                            status_code=502)

    clients = get_shared_client().get_all_clients()
    if clients is None:
        return JSONResponse({"ok": False, "error": i18n.t("The panel did not answer")},
                            status_code=502)

    rows = email_bot.name_mismatches(clients, found)
    logger.info(f"Panel: {len(found)} addresses in the mailbox, {len(rows)} names differ.")
    return JSONResponse({"ok": True, "rows": rows, "senders": len(found)})


@router.post("/settings/mail/names/apply")
async def settings_names_apply(request: Request):
    """
    Writes the chosen names into the clients' comments.

    The names come back from the page rather than being looked up again: they
    are what the administrator was shown and agreed to, and a second scan could
    quietly disagree with the table they were reading.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    try:
        chosen = (await request.json()).get("rows") or []
    except Exception:
        return JSONResponse({"ok": False, "error": i18n.t("The request failed")}, status_code=400)

    xui = get_shared_client()
    updated, failed = 0, []
    for row in chosen:
        uuid_value = str(row.get("uuid") or "")
        name = str(row.get("name") or "").strip()
        if not uuid_value or not name:
            continue
        client_obj = xui.find_client_by_uuid(uuid_value)
        if not client_obj:
            # Deleted between the scan and the choice.
            failed.append(row.get("email") or uuid_value)
            continue
        try:
            ok = xui.update_client(uuid_value, new_comment=name, client_obj=client_obj)
        except Exception as e:
            logger.error(f"Could not write the name for {row.get('email')}: {e}")
            ok = False
        if ok:
            updated += 1
        else:
            failed.append(row.get("email") or uuid_value)

    logger.info(f"Panel: names written for {updated} client(s)"
                + (f", {len(failed)} failed: {', '.join(failed)}" if failed else "."))
    return JSONResponse({"ok": True, "updated": updated, "failed": failed})


@router.post("/settings/gotify/test")
def settings_gotify_test(request: Request):
    """Sends a test notification with the current settings and text."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    if not config.GOTIFY_URL or not config.GOTIFY_TOKEN:
        return JSONResponse({"ok": False, "error": i18n.t("There is no address or no token")}, status_code=400)

    fields = {
        "email": "test@example.com",
        "name": i18n.t("Check"),
        "service": config.SERVICE_NAME,
    }
    logger.info("Panel: test notification to Gotify.")
    try:
        email_bot.send_gotify_notification(
            email_bot.render_template(config.GOTIFY_TITLE, **fields),
            email_bot.render_template(config.GOTIFY_MESSAGE, **fields),
        )
    except Exception as e:
        logger.error(f"The test notification to Gotify did not go out: {e}")
        return JSONResponse({"ok": False, "error": i18n.t("It did not go out")}, status_code=502)
    return JSONResponse({"ok": True})


@router.get("/settings/secret")
def settings_secret(request: Request, key: str = ""):
    """
    Hands back the real value of a secret, for the reveal button.

    Secrets never reach the page markup: only the mask does. The request is
    separate and needs the same session as the rest of the panel.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return JSONResponse({"error": i18n.t("You need to sign in")}, status_code=401)

    if key not in settings.SECRET_KEYS:
        return JSONResponse({"error": i18n.t("Unknown key")}, status_code=400)
    logger.info(f"Panel: revealed the value of {key}.")
    return JSONResponse({"value": settings.secret(key)})


# ---------------------------------------------------------------------------
# Letter texts
# ---------------------------------------------------------------------------
@router.post("/settings/texts")
async def settings_texts_save(request: Request):
    """
    Saves the letter texts. The route is async because the field names are not
    known in advance — they are built from the keys — so the form has to be read
    whole. Writing the file is quick, and nothing here calls out to 3x-ui.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    values = {key[len("text:"):]: value
              for key, value in form.items() if key.startswith("text:")}
    # The switches standing among the texts are settings, and go to the other
    # store. A hidden "off" is posted before each one, so an unticked box —
    # which a browser does not send at all — still arrives as a value; the
    # later entry wins, which is the ticked one when there is one.
    switches = {key[len("switch:"):]: value == "on"
                for key, value in form.items() if key.startswith("switch:")}
    try:
        email_texts.save(values)
        if switches:
            settings.save(switches)
    except OSError as e:
        logger.error(f"Could not write the letter texts: {e}")
        return RedirectResponse(url="/settings?error=" + quote(i18n.t("Could not save the texts")),
                                status_code=303)
    except (ValueError, TypeError) as e:
        logger.error(f"A switch on the Letters tab is invalid: {e}")
        return RedirectResponse(url="/settings?error=" + quote(str(e)), status_code=303)
    logger.info(f"Panel: letter texts saved ({len(values)} fields, {len(switches)} switches).")
    return RedirectResponse(url="/settings?saved=texts", status_code=303)


@router.post("/settings/texts/lang")
def settings_texts_lang(request: Request, lang: str = Form("")):
    """
    Switches the language the letters go out in, and with it the texts shown in
    the editor. It is saved at once rather than with the rest of the form: the
    fields below have to be redrawn in the new language, and an unsaved choice
    would leave the picker and the fields disagreeing.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    try:
        settings.save({"MAIL_LANG": lang})
    except (ValueError, TypeError):
        logger.warning(f"Panel: an unknown mail language was asked for: {lang!r}.")
    except OSError as e:
        logger.error(f"Could not write settings.json: {e}")
    # The open tab is remembered in the browser, so this lands back on «Letters».
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/texts/reset")
def settings_texts_reset(request: Request, group: str = Form(...)):
    """Restores the original texts of one letter."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    if group not in email_texts.GROUP_BY_ID:
        return JSONResponse({"ok": False, "error": i18n.t("Unknown group")}, status_code=400)
    email_texts.reset(group)
    logger.info(f"Panel: texts of the {group!r} letter restored to their originals.")
    return JSONResponse({"ok": True})


@router.post("/settings/texts/test")
def settings_texts_test(request: Request, group: str = Form(...), email: str = Form(...),
                        kind: str = Form("info")):
    """Sends a sample letter of the chosen kind to the address given."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    address = (email or "").strip()
    if "@" not in address:
        return JSONResponse({"ok": False, "error": i18n.t("An address with an @ in it is needed")}, status_code=400)
    if address.lower() in PLACEHOLDER_EMAILS:
        return JSONResponse(
            {"ok": False, "error": i18n.t("That is a placeholder address from .env.example; give a real one")},
            status_code=400)

    if kind not in mail_templates.BROADCAST_KINDS:
        kind = "info"
    sample = _sample_email(group, kind)
    if sample is None:
        return JSONResponse({"ok": False, "error": i18n.t("There is no sample for this group")}, status_code=400)

    subject, message = sample
    logger.info(f"Panel: test letter {group!r} to {address}.")
    try:
        email_bot.send_email_reply(address, subject, message)
    except Exception as e:
        logger.error(f"The test letter {group!r} did not go out: {e}")
        return JSONResponse({"ok": False, "error": i18n.t("Could not send it")}, status_code=502)
    return JSONResponse({"ok": True})


def _sample_email(group: str, kind: str = "info"):
    """A sample letter on invented data — no real client is touched."""
    demo_sub = f"{config.XUI_SUBSCRIPTION_BASE_URL}/demoSubscriptionToken"
    demo_mail = "user@example.com"
    now_ms = int(datetime.now().timestamp() * 1000)

    if group == "welcome":
        return (mail_templates.welcome_subject(False),
                mail_templates.get_welcome_email(demo_sub, config.EXPIRE_DAYS, config.LIMIT_GB))
    if group == "status":
        return (mail_templates.text("status.subject"),
                mail_templates.get_status_email(demo_mail, True, 8 * 1024**3, 34 * 1024**3,
                                                100 * 1024**3, now_ms + 21 * 86400 * 1000))
    if group == "help":
        return (mail_templates.text("help.subject"),
                mail_templates.get_help_email(demo_mail, demo_sub))
    if group == "broadcast":
        label = mail_templates.text(mail_templates.BROADCAST_KINDS[kind]["text_key"])
        subject = i18n.t("A sample broadcast — {kind}", kind=label.lower())
        return (subject, mail_templates.get_broadcast_email(
            subject,
            i18n.t("This is what the text of a broadcast looks like.\nLine breaks are kept."),
            kind))
    if group == "notice":
        return (mail_templates.notice_subject("unknown"),
                mail_templates.get_notice("unknown", subject=i18n.t("Hello")))
    return None
