"""The broadcast route: the composer form plus background sending to the chosen clients."""
import logging
import secrets
import threading
from datetime import datetime
from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

import config
import i18n
import tariffs
from admin.deps import templates, require_auth
from admin.rows import _client_row
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
# ---------------------------------------------------------------------------
_jobs = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 20


def _create_job(subject: str, message: str, total: int) -> str:
    job_id = secrets.token_urlsafe(8)
    with _jobs_lock:
        # Keep the dictionary from growing without end: only the latest jobs stay.
        if len(_jobs) >= _MAX_JOBS:
            for stale in sorted(_jobs, key=lambda k: _jobs[k]["started_at"])[:len(_jobs) - _MAX_JOBS + 1]:
                _jobs.pop(stale, None)
        _jobs[job_id] = {
            "subject": subject,
            "message": message,
            "total": total,
            "sent": 0,
            "failed": 0,
            "done": False,
            "error": "",
            "started_at": datetime.now(),
        }
    return job_id


def _update_job(job_id: str, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def _get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _run_broadcast(job_id: str, message: str, subject: str, emails: list, kind: str = "plain"):
    """Runs in the background, after the response has already gone to the browser."""
    def on_progress(sent, failed):
        _update_job(job_id, sent=sent, failed=failed)

    try:
        email_bot.handle_broadcast(message, subject=subject, emails=emails,
                                   on_progress=on_progress, kind=kind)
    except Exception as e:
        logger.error(f"Broadcast {job_id} broke off: {e}", exc_info=True)
        _update_job(job_id, error=str(e))
    finally:
        _update_job(job_id, done=True)


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
        row = _client_row(client_obj, online)
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
        },
    )


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

    job_id = _create_job(subject, message, len(selected_emails))
    background_tasks.add_task(_run_broadcast, job_id, message, subject, selected_emails, kind)
    logger.info(f"Broadcast {job_id} queued in the background: {len(selected_emails)} recipients.")

    return RedirectResponse(url=f"/broadcast/status/{job_id}", status_code=303)


@router.get("/broadcast/status/{job_id}", response_class=HTMLResponse)
def broadcast_status(request: Request, job_id: str):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=i18n.t("That broadcast task was not found"))

    processed = job["sent"] + job["failed"]
    percent = round(processed / job["total"] * 100) if job["total"] else 100

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
            "percent": percent,
        },
    )
