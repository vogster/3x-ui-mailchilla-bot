"""The broadcast route: the composer form plus background sending to the chosen clients."""
import logging
from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, Response

import broadcasts
import config
import i18n
import tariffs
from admin.deps import templates, require_auth
from admin.rows import client_row
from xui_client import get_shared_client

# Reuse the sending logic that already exists in the bot's own module.
import email_bot
import templates as mail_templates

import urllib.parse

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Background broadcast jobs
#
# A single letter takes seconds to leave (the receiving server is in no hurry),
# so a broadcast to a couple of dozen addresses runs for minutes. An HTTP request
# cannot be held open that long — the tab would time out well before the sending
# finished. So it goes to the background and the browser gets a status page
# straight away, which refreshes itself.
#
# The state lives in the process's memory: the panel has a single user, and there
# is nothing here worth surviving a restart.
#
# The record itself is kept in `broadcasts`, below this module and the bot's
# `/broadcast` command alike, and written by the sending rather than from
# here. It used to be a dictionary of this module's own, which the status page
# of one broadcast could read and nothing else did: close that tab and the
# panel could not say whether letters were still going out, and a broadcast
# started by mail was never in it at all.
# ---------------------------------------------------------------------------

# How many the Broadcast page lists. The record keeps more, for links to an
# older status page.
LISTED = 6


def _run_broadcast(job_id: str, message: str, subject: str, emails: list, kind: str = "plain"):
    """Runs in the background, after the response has already gone to the browser."""
    try:
        email_bot.handle_broadcast(message, subject=subject, emails=emails,
                                   kind=kind, job_id=job_id)
    except Exception as e:
        logger.error(f"Broadcast {job_id} broke off: {e}", exc_info=True)
        broadcasts.finish(job_id, error=str(e))
    finally:
        # The sending closes its own record; this is for a broadcast that broke
        # before it got as far as sending, which would otherwise be shown as
        # running for as long as the process lived.
        broadcasts.finish(job_id)


def _view(job: dict) -> dict:
    """A record as the templates want it: the figures worked out, the times as text."""
    processed = job["sent"] + job["failed"]
    if not job["done"]:
        state = "running"
    elif job["error"]:
        state = "cut"
    elif job["failed"]:
        state = "partial"
    else:
        state = "ok"
    finished = job["finished_at"]
    return dict(
        job,
        processed=processed,
        percent=round(processed / job["total"] * 100) if job["total"] else 100,
        state=state,
        started=job["started_at"].strftime("%d.%m.%Y %H:%M"),
        finished=finished.strftime("%d.%m.%Y %H:%M") if finished else "",
    )


def _jobs_context() -> dict:
    """What the list of broadcasts is drawn from, on the page and in its refresh."""
    return {
        "jobs": [_view(job) for job in broadcasts.recent(LISTED)],
        "jobs_running": broadcasts.running(),
    }


@router.get("/broadcast", response_class=HTMLResponse)
def broadcast_form(request: Request, error: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    xui = get_shared_client()
    all_clients = xui.get_all_clients() or []
    # Asked once for the whole list, as on the client pages. None when the panel
    # cannot say, and then neither the dots nor their filter appear.
    online_emails = xui.get_online_emails()
    online = None if online_emails is None else set(online_emails)

    # The list holds both active and disabled clients: the status shows in the
    # row and the filter can narrow it to either. The only ones dropped are those
    # there is physically nowhere to write to — a client added by hand carries an
    # arbitrary identifier in the panel's email field rather than an address.
    recipients = []
    skipped = 0
    for client_obj in all_clients:
        row = client_row(client_obj, online)
        if not row["bare_email"]:
            skipped += 1
            continue
        name = row["comment"]
        limit_bytes = row["limit_bytes"]
        row.update({
            "name": name,
            # What is left, for the "least remaining" sort. An unlimited client
            # has no remainder — an empty string, and the JS puts those last.
            "left_bytes": (max(limit_bytes - row["used_bytes"], 0)
                           if limit_bytes > 0 else ""),
        })
        recipients.append(row)

    recipients.sort(key=lambda x: x["bare_email"].lower())

    return templates.TemplateResponse(
        "broadcast.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "recipients": recipients,
            "recipient_count": len(recipients),
            "active_count": sum(1 for r in recipients if r["enable"]),
            "skipped_count": skipped,
            "online_known": online is not None,
            # Every tariff a recipient could be on, for the filter. Both the
            # tariffs that exist and the labels of ones that no longer do: a
            # group left behind by a deleted tariff still holds people, and
            # writing to them is exactly what somebody might want.
            "tariff_names": sorted({t["name"] for t in tariffs.all_tariffs()}
                                   | {r["tariff"] for r in recipients if r["tariff"]}),
            "kinds": mail_templates.broadcast_kind_options(),
            "error": error,
            **_jobs_context(),
        },
    )


@router.get("/broadcast/jobs", response_class=HTMLResponse)
def broadcast_jobs(request: Request):
    """
    The list of broadcasts alone, for the Broadcast page to re-read while one
    is running.

    A fragment of the page rather than JSON, so the markup exists once. And a
    401 rather than the redirect every other route answers a lapsed session
    with: the page puts whatever comes back into itself, and a followed
    redirect would be the sign-in form sitting inside the Broadcast page.
    """
    if require_auth(request):
        return Response(status_code=401)
    response = templates.TemplateResponse(
        "_broadcast_jobs.html", {"request": request, **_jobs_context()})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/broadcast")
def broadcast_send(
    request: Request,
    background_tasks: BackgroundTasks,
    subject: str = Form(...),
    message: str = Form(...),
    kind: str = Form("plain"),
    emails: List[str] = Form(default=[]),
):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    subject = (subject or "").strip()
    message = (message or "").strip()

    if not message:
        return RedirectResponse(
            url="/broadcast?error=" + urllib.parse.quote(i18n.t("The text of the broadcast is empty")),
            status_code=303)

    selected_emails = [e.strip() for e in emails if e and e.strip()]

    if not selected_emails:
        err_msg = urllib.parse.quote(i18n.t("You have to choose at least one recipient"))
        return RedirectResponse(url=f"/broadcast?error={err_msg}", status_code=303)

    if kind not in mail_templates.BROADCAST_KINDS:
        kind = mail_templates.DEFAULT_KIND

    # The subject the letters will carry, settled here rather than left to
    # the sending: the record is opened before any of it runs, and a
    # broadcast listed with no subject is one nobody can tell from another.
    subject = subject or mail_templates.text("notice.broadcast_default_subject")
    job_id = broadcasts.start(subject, message, len(selected_emails), kind,
                              source=broadcasts.PANEL)
    background_tasks.add_task(_run_broadcast, job_id, message, subject, selected_emails, kind)
    logger.info(f"Broadcast {job_id} queued in the background: {len(selected_emails)} recipients.")

    return RedirectResponse(url=f"/broadcast/status/{job_id}", status_code=303)


@router.get("/broadcast/status/{job_id}", response_class=HTMLResponse)
def broadcast_status(request: Request, job_id: str):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    job = broadcasts.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=i18n.t("That broadcast task was not found"))
    job = _view(job)

    return templates.TemplateResponse(
        "broadcast_result.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "subject": job["subject"],
            "message": job["message"],
            "sent_count": job["sent"],
            "failed_count": job["failed"],
            "total_recipients": job["total"],
            "done": job["done"],
            "error": job["error"],
            "percent": job["percent"],
            "source": job["source"],
            "started": job["started"],
            "finished": job["finished"],
            # The addresses a letter did not reach, and how many more there
            # were than are kept by name.
            "failed_addresses": job["failed_addresses"],
            "failed_unnamed": max(job["failed"] - len(job["failed_addresses"]), 0),
        },
    )
