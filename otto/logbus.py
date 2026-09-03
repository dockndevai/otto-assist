"""Stream logs to the dashboard so failures are visible without opening a log file.

Installs a logging handler that turns each interesting log record into a `log` event on the
EventBus (which the dashboard renders in its Logs drawer) and keeps a bounded ring buffer so a
freshly-opened dashboard sees the recent lines. Otto's own logger streams at INFO; third-party
loggers only at WARNING+ (problems), and a couple of known-noisy lines are dropped.
"""

from __future__ import annotations

import collections
import logging
import time


# RealtimeSTT's transcription worker floods these when its connection closes — benign spam
# that would drown the panel. Drop them.
_NOISE = ("Error receiving data from connection", "poll_connection")


class _LevelByLogger(logging.Filter):
    """Otto's diagnostics at INFO+, everyone else only at WARNING+."""

    def filter(self, record: logging.LogRecord) -> bool:
        if any(n in record.getMessage() for n in _NOISE):
            return False
        if record.name.startswith("otto"):
            return record.levelno >= logging.INFO
        return record.levelno >= logging.WARNING


class BusLogHandler(logging.Handler):
    def __init__(self, bus, buffer: collections.deque) -> None:
        super().__init__(logging.INFO)
        self.bus = bus
        self.buffer = buffer
        self.addFilter(_LevelByLogger())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + self.formatException(record.exc_info)
        except Exception:
            return
        ev = {"type": "log", "level": record.levelname.lower(),
              "logger": record.name, "msg": msg, "ts": time.time()}
        self.buffer.append(ev)
        try:
            self.bus.publish(ev)
        except Exception:
            pass


def install_log_bus(bus, maxlen: int = 400) -> collections.deque:
    """Attach the bus handler to the root logger. Returns the ring buffer of recent `log`
    events (replay it to a newly-connected dashboard)."""
    buffer: collections.deque = collections.deque(maxlen=maxlen)
    root = logging.getLogger()
    # Rebind: drop any prior bus handler so this (bus, buffer) becomes the live one.
    for h in [h for h in root.handlers if isinstance(h, BusLogHandler)]:
        root.removeHandler(h)
    root.addHandler(BusLogHandler(bus, buffer))
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    logging.getLogger("otto").setLevel(logging.INFO)
    return buffer
