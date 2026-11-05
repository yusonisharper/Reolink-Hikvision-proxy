"""systemd notify protocol (Type=notify, WatchdogSec=) with no extra dependency.

The heartbeat runs on the event loop, so a hung loop stops it and systemd kills the process. It
also stops when a camera's background task has died: the process would look healthy while that
camera's alerts are gone, and nothing else would report it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

log = logging.getLogger("watchdog")


def notify(state: str) -> bool:
    """Sends to systemd. False when not running under it, or when systemd cannot be reached."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):        # abstract socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(state.encode(), addr)
    except OSError as e:
        log.warning("cannot notify systemd: %s", e)
        return False
    return True


def interval() -> float | None:
    """Seconds between heartbeats (half of what systemd allows), None without a watchdog."""
    try:
        usec = int(os.environ.get("WATCHDOG_USEC", ""))
    except ValueError:
        return None
    return usec / 2e6 if usec > 0 else None


async def supervise(cams: list, stop: asyncio.Event, period: float = 10.0) -> bool:
    """Runs until stop is set. True when a camera task died and the service should be restarted."""
    beat = interval()
    reported: set = set()
    while not stop.is_set():
        for cam in cams:
            for task in cam.failed_tasks():
                if task in reported:
                    continue
                reported.add(task)
                log.critical("%s: background task %s died", cam.cfg.name, task.get_name(),
                             exc_info=task.exception())
        if reported and beat:       # under a supervisor a restart repairs it
            return True
        if beat:
            notify("WATCHDOG=1")
        try:
            await asyncio.wait_for(stop.wait(), beat or period)
        except asyncio.TimeoutError:
            pass
    return False
