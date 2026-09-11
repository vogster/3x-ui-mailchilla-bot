"""
Log collection for viewing from the web panel.

Everything written through the standard logging module lands in an in-memory
ring buffer, which the /logs page reads, and — when enabled — in a rotating
file, so the history survives a restart of the process.

The buffer is in memory on purpose: the panel has a single user, and that is
enough for catching a fresh error, without parsing a text file back into level
and time fields.
"""
import logging
import logging.handlers
import os
import threading
from collections import deque
from datetime import datetime

import config

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_lock = threading.RLock()
_buffer = None
_handler = None
_installed = False

# The record number up to which the admin has marked things read. It lives
# exactly as long as the buffer does: numbering restarts with the process, so
# persisting the marker would hide new records instead of old ones.
_ack_seq = 0


class RingBufferHandler(logging.Handler):
    """Keeps the last N records in memory, already broken into fields."""

    def __init__(self, capacity: int):
        super().__init__()
        self.records = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record):
        try:
            message = record.getMessage()
            exc_text = ""
            if record.exc_info:
                exc_text = logging.Formatter().formatException(record.exc_info)
            with _lock:
                self._seq += 1
                self.records.append({
                    "seq": self._seq,
                    "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                    "level": record.levelname,
                    "levelno": record.levelno,
                    "logger": record.name,
                    "message": message,
                    "exc": exc_text,
                })
        except Exception:
            # A log handler has no business bringing down what it is logging.
            self.handleError(record)


def install():
    """Attaches the buffer (and file) to the root logger. Calling it twice is safe."""
    global _buffer, _handler, _installed
    with _lock:
        if _installed:
            return _buffer

        _buffer = RingBufferHandler(config.LOG_BUFFER_SIZE)
        _buffer.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(_buffer)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)

        if config.LOG_FILE:
            try:
                directory = os.path.dirname(os.path.abspath(config.LOG_FILE))
                if directory:
                    os.makedirs(directory, exist_ok=True)
                _handler = logging.handlers.RotatingFileHandler(
                    config.LOG_FILE, maxBytes=config.LOG_FILE_MAX_BYTES,
                    backupCount=config.LOG_FILE_BACKUPS, encoding="utf-8",
                )
                _handler.setFormatter(logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                ))
                root.addHandler(_handler)
            except Exception as e:
                logging.getLogger(__name__).error(
                    f"Could not enable log writing to {config.LOG_FILE}: {e}"
                )

        _installed = True
        logging.getLogger(__name__).info(
            f"Log collection enabled: a buffer of {config.LOG_BUFFER_SIZE} records"
            + (f", file {config.LOG_FILE}" if config.LOG_FILE and _handler else ", no file")
        )
        return _buffer


def entries(level: str = "", query: str = "", limit: int = 300, unread_only: bool = False):
    """
    Records from the buffer, newest first.
    :param level: minimum level (for instance "WARNING"), or "" for all of them
    :param query: substring to look for in the message and the logger name
    :param unread_only: only what has appeared since the read marker
    """
    if _buffer is None:
        return []

    min_levelno = getattr(logging, level, 0) if level in LEVELS else 0
    needle = (query or "").strip().lower()

    with _lock:
        snapshot = list(_buffer.records)
        ack = _ack_seq

    result = []
    for item in reversed(snapshot):
        if unread_only and item["seq"] <= ack:
            continue
        if item["levelno"] < min_levelno:
            continue
        if needle and needle not in item["message"].lower() \
                and needle not in item["logger"].lower() \
                and needle not in item["exc"].lower():
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def stats():
    """A summary of the buffer: how many records of each level have piled up."""
    if _buffer is None:
        return {"total": 0, "capacity": config.LOG_BUFFER_SIZE, "by_level": {}}
    with _lock:
        snapshot = list(_buffer.records)
    by_level = {name: 0 for name in LEVELS}
    for item in snapshot:
        by_level[item["level"]] = by_level.get(item["level"], 0) + 1
    return {
        "total": len(snapshot),
        "capacity": _buffer.records.maxlen,
        "by_level": by_level,
        "file": config.LOG_FILE if _handler else "",
    }


def unread_count(level: str = "WARNING") -> int:
    """How many records of the given level have appeared since the read marker."""
    if _buffer is None:
        return 0
    min_levelno = getattr(logging, level, 0) if level in LEVELS else 0
    with _lock:
        return sum(1 for item in _buffer.records
                   if item["seq"] > _ack_seq and item["levelno"] >= min_levelno)


def mark_read():
    """Marks everything so far as read. New records will show up again."""
    global _ack_seq
    with _lock:
        if _buffer is not None and _buffer.records:
            _ack_seq = _buffer.records[-1]["seq"]
        return _ack_seq


def clear():
    """Empties the buffer, leaving the file alone."""
    global _ack_seq
    if _buffer is None:
        return
    with _lock:
        _buffer.records.clear()
        # numbering carries on, but after a clear there is nothing left to hide
        _ack_seq = 0
