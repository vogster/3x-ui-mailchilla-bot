"""
Which broadcasts have gone out since the process started, and how far each got.

A broadcast runs for minutes — every letter waits on somebody else's mail
server — and for those minutes there used to be one place that knew about it:
the status page of that one broadcast, reachable only by the redirect that
followed pressing Send. Close the tab and the panel had nothing to say about
whether letters were still going out; and a broadcast started by mail, with
`/broadcast`, never had a page at all. Which is how the same letter gets sent
twice.

So the record is kept here, below both ways of starting one, and written by
the code that does the sending rather than by whoever asked for it: a new way
of starting a broadcast cannot forget to be listed.

In memory, like the log's ring buffer. The panel has one user and there is
nothing here that has to outlive a restart — a broadcast cut off by one is
simply over, and the log has its last line.
"""
import secrets
import threading
from datetime import datetime

# How many are remembered. The page shows fewer; the rest are what a link to
# an older status page still resolves to.
KEPT = 20
# How many addresses a letter did not reach are kept by name. Past that the
# count goes on and the names are in the log — a mailbox that has started
# refusing everything would otherwise be the whole client list, once per
# broadcast.
FAILED_KEPT = 50

# Where it was started from.
PANEL, MAIL = "panel", "mail"

_lock = threading.Lock()
_jobs = {}


def _reset():
    """Forgets everything, as a restart does. For the tests."""
    with _lock:
        _jobs.clear()


def start(subject: str, message: str, total: int, kind: str = "plain",
          source: str = PANEL) -> str:
    """Opens the record of a broadcast about to be sent; returns its id."""
    job_id = secrets.token_urlsafe(8)
    with _lock:
        _make_room()
        _jobs[job_id] = {
            "id": job_id,
            "subject": subject,
            "message": message,
            "kind": kind,
            "source": source,
            "total": total,
            "sent": 0,
            "failed": 0,
            "failed_addresses": [],
            "done": False,
            "error": "",
            "started_at": datetime.now(),
            "finished_at": None,
        }
    return job_id


def _make_room():
    """
    Drops the oldest records past KEPT — finished ones first. One still being
    sent is the last thing to forget: it is the only record that somebody may
    be about to press Send a second time for.
    """
    while len(_jobs) >= KEPT:
        finished = [j for j in _jobs.values() if j["done"]]
        oldest = min(finished or _jobs.values(), key=lambda j: j["started_at"])
        del _jobs[oldest["id"]]


def progress(job_id: str, sent: int, failed: int, failed_address: str = ""):
    """After each recipient: the counts so far, and the address if this one failed."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["sent"], job["failed"] = sent, failed
        if failed_address and len(job["failed_addresses"]) < FAILED_KEPT:
            job["failed_addresses"].append(failed_address)


def finish(job_id: str, error: str = ""):
    """
    Closes the record. Safe to call twice, which is how it is called: by the
    sending itself when it ends, and by whoever started it in case the sending
    never got that far. The first call's time stands, and an error given by
    either is kept.
    """
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        if error and not job["error"]:
            job["error"] = error
        if not job["done"]:
            job["done"] = True
            job["finished_at"] = datetime.now()


def get(job_id: str):
    """A copy of one record, or None."""
    with _lock:
        job = _jobs.get(job_id)
        return _copy(job) if job else None


def recent(limit: int = None) -> list:
    """
    The records, those still being sent first and the newest first within
    each — so that cutting the list short never hides one that is running.
    """
    with _lock:
        rows = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)
        rows.sort(key=lambda j: j["done"])
        return [_copy(job) for job in (rows[:limit] if limit else rows)]


def running() -> int:
    """How many are being sent right now."""
    with _lock:
        return sum(1 for job in _jobs.values() if not job["done"])


def _copy(job: dict) -> dict:
    return dict(job, failed_addresses=list(job["failed_addresses"]))
