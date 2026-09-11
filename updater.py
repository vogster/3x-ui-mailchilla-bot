"""
Noticing that a newer version has been released.

The panel shows what it is running; this works out whether that is still the
latest and hands the banner what it needs. Updating itself is not done here —
that is `mailchilla update` on the server.

The check runs in a background thread and the page only ever reads what it left
behind, so rendering never waits on the network. A failure is not worth
bothering anybody with: the thread simply tries again later and the banner stays
away.

Only release tags are considered — `v1.2.3`, never `v1.2.3-rc.1`. Somebody who
wants release candidates is not waiting for a banner to tell them.
"""
import logging
import re
import threading
import time

import requests

import config

logger = logging.getLogger(__name__)

# A released version is not news that goes stale, so asking a few times a day is
# plenty. GitHub allows sixty unauthenticated requests an hour from one address;
# this uses four a day.
CHECK_INTERVAL = 6 * 60 * 60
# After a failure — no network, GitHub unreachable, a proxy in the way.
RETRY_INTERVAL = 30 * 60
# The first check waits a little: the panel should come up quickly, and on a
# freshly booted machine the network is often not up yet.
FIRST_DELAY = 30
TIMEOUT = 10

_lock = threading.RLock()
_wake = threading.Event()
_started = False
# The newest version seen, and when it was learnt. Nothing here is persisted:
# it costs one request to find out again, and a stale answer on disk would
# outlive the reason it was right.
_latest = None
_checked_at = 0.0

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse(text):
    """(major, minor, patch), or None for anything that is not a release."""
    match = _VERSION.match(str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def repo_slug() -> str:
    """'owner/name' out of PROJECT_URL, so the address is configured in one place."""
    url = str(getattr(config, "PROJECT_URL", "") or "").rstrip("/")
    parts = url.split("github.com/", 1)
    return parts[1] if len(parts) == 2 else ""


def compare_url(current: str, latest: str) -> str:
    """
    GitHub's comparison between what is installed and what is out.

    Deliberately not the release page: a release may not have been created for a
    tag, and "what changed since my version" is the question actually being
    asked — which the release page cannot answer when several have gone by.
    """
    slug = repo_slug()
    if not slug:
        return ""
    return f"https://github.com/{slug}/compare/v{str(current).lstrip('v')}...v{str(latest).lstrip('v')}"


def releases_url() -> str:
    slug = repo_slug()
    return f"https://github.com/{slug}/releases" if slug else ""


def _fetch_latest():
    """The newest release tag on GitHub, or None. Never raises."""
    slug = repo_slug()
    if not slug:
        logger.debug("PROJECT_URL does not name a GitHub repository; the version check is off.")
        return None
    try:
        response = requests.get(
            f"https://api.github.com/repos/{slug}/tags",
            params={"per_page": 100},
            # GitHub refuses a request without one.
            headers={"User-Agent": f"{config.APP_NAME}/{config.APP_VERSION}",
                     "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            logger.debug(f"The version check got {response.status_code} from GitHub.")
            return None
        versions = []
        for item in response.json() or []:
            parsed = _parse(item.get("name"))
            if parsed:
                versions.append((parsed, str(item.get("name")).lstrip("v")))
        if not versions:
            return None
        return max(versions)[1]
    except Exception as e:
        # Not an error in the log: a server without a route to GitHub is a
        # legitimate way to run this, and one warning every half hour would be
        # noise in the very panel the warning counter feeds.
        logger.debug(f"The version check failed: {e}")
        return None


def _check():
    global _latest, _checked_at
    if not getattr(config, "UPDATE_CHECK_ENABLED", True):
        return False
    latest = _fetch_latest()
    with _lock:
        _checked_at = time.time()
        if latest:
            if latest != _latest:
                logger.info(f"The newest published version is {latest}; this is {config.APP_VERSION}.")
            _latest = latest
    return bool(latest)


def _loop():
    _wake.wait(FIRST_DELAY)
    while True:
        ok = _check()
        _wake.clear()
        _wake.wait(CHECK_INTERVAL if ok else RETRY_INTERVAL)


def start():
    """Starts the background check. Calling it twice is safe."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="update-check", daemon=True).start()


def refresh_soon():
    """Asks the thread to check now rather than at its next turn."""
    _wake.set()


def status() -> dict:
    """
    What the banner needs. Reads memory alone — safe to call on every render.
    """
    if not getattr(config, "UPDATE_CHECK_ENABLED", True):
        return {"available": False}

    with _lock:
        latest = _latest
        checked = _checked_at

    # Switched on after the process started, or the first check has not happened
    # yet: ask for one, and show nothing this time round.
    if not checked:
        refresh_soon()
        return {"available": False}
    if not latest:
        return {"available": False}

    current = _parse(config.APP_VERSION)
    newest = _parse(latest)
    if not current or not newest or newest <= current:
        return {"available": False}

    dismissed = str(getattr(config, "UPDATE_DISMISSED_VERSION", "") or "").lstrip("v")
    if dismissed == latest:
        return {"available": False}

    return {
        "available": True,
        "version": latest,
        "current": str(config.APP_VERSION),
        "changes_url": compare_url(config.APP_VERSION, latest),
        "releases_url": releases_url(),
    }
