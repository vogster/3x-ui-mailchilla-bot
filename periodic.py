"""
"Every so many days" for the jobs the bot runs by itself.

The mailbox cleanup and the inactivity reminder are both a switch, a period in
days and the unix time of the last run, all three panel settings read off
`config` at call time. They had grown a copy each of the same few lines, and
the copies had already begun to drift — one read an unset period as a day,
another as thirty — so the reading lives here once.
"""
import time

import config


def period_days(key: str, default: int = 30) -> int:
    """A period setting in whole days, never below one."""
    try:
        return max(1, int(getattr(config, key, default) or default))
    except (TypeError, ValueError):
        return default


def last_run(key: str) -> float:
    """The unix time a job last ran, 0.0 for never."""
    try:
        return float(getattr(config, key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def due(enabled_key: str, days_key: str, last_key: str, now=None) -> bool:
    """Whether a job is switched on and its period has passed since it last ran."""
    if not getattr(config, enabled_key, False):
        return False
    last = last_run(last_key)
    if not last:
        # Never run, which is also the first poll after somebody switched it
        # on: the job runs now rather than a whole period later.
        return True
    return (now or time.time()) - last >= period_days(days_key) * 86400
