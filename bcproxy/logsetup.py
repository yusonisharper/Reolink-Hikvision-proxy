"""Logging: console, plus rotating files that survive a restart when a log directory is given.

    bcproxy.log   everything at the configured level
    errors.log    WARNING and up: this code reports most real failures (ffmpeg exits, camera
                  unreachable) as warnings, so ERROR alone would miss them
    crash.log     Python stacks from a fatal signal, including the SIGABRT a systemd watchdog
                  sends: it shows where a hung event loop was stuck
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5         # at most 60 MB in total: these files live on the device's flash

log = logging.getLogger("main")

# ffmpeg errors and unhandled NVR messages can carry credentials; files outlive the console
_SECRETS = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@"
    r"|(?P<key>\b(?:password|passwd|pwd)=)[^&\s]+"
    r"|(?P<tag><(?:password|passwd)>)[^<]+",
    re.IGNORECASE)


def redact(text: str) -> str:
    def repl(m: re.Match) -> str:
        return f"{m['scheme']}***@" if m["scheme"] else f"{m['key'] or m['tag']}***"
    return _SECRETS.sub(repl, text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _file_handler(path: Path, level: int, formatter: logging.Formatter) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(formatter)
    return h


def setup(level: str = "info", log_dir: str | None = None) -> list[logging.Handler]:
    """Returns the handlers it added. Failing to open a log file never stops the proxy."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    formatter = RedactingFormatter(FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    # under systemd the files have it all: don't write everything to the journal as well
    if log_dir and os.environ.get("JOURNAL_STREAM"):
        console.setLevel(logging.WARNING)
    handlers: list[logging.Handler] = [console]
    problem = None
    if log_dir:
        try:
            d = Path(log_dir)
            d.mkdir(parents=True, exist_ok=True)
            handlers.append(_file_handler(d / "bcproxy.log", logging.NOTSET, formatter))
            handlers.append(_file_handler(d / "errors.log", logging.WARNING, formatter))
            # kept open on purpose: a fatal signal handler cannot open files
            faulthandler.enable(file=open(d / "crash.log", "a", encoding="utf-8"), all_threads=True)
        except OSError as e:
            problem = e
    for h in handlers:
        root.addHandler(h)
    if problem:
        log.error("cannot write logs to %s: %s", log_dir, problem)

    def excepthook(tp, value, tb):
        log.critical("uncaught exception", exc_info=(tp, value, tb))
    sys.excepthook = excepthook
    return handlers
