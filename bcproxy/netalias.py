"""Adds the cameras' listen addresses to a network interface (config: alias_interface).

An NVR tells cameras apart by IP, so each virtual camera needs its own. They are kept here rather
than in the system's network config, which UniFi OS generates and rewrites: the check repeats while
running, because a network reconfiguration can remove addresses it did not create. A listening
socket survives that and works again as soon as the address is back.
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("netalias")


async def _ip(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as e:
        return 1, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


def listen_addresses(cams: list) -> list[str]:
    return sorted({c.listen_host for c in cams if c.listen_host not in ("", "0.0.0.0")})


async def ensure(iface: str, hosts: list[str], level: int = logging.WARNING) -> list[str]:
    """Adds whichever of hosts the interface lacks; returns those it added."""
    rc, out = await _ip("-4", "-o", "addr", "show", "dev", iface)
    if rc:
        log.error("cannot read addresses of %s: %s", iface, out)
        return []
    present = set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
    added = []
    for host in hosts:
        if host in present:
            continue
        # /32: no second route for the subnet, so connections to the cameras keep the main address
        rc, out = await _ip("addr", "add", f"{host}/32", "dev", iface)
        if rc:
            log.error("cannot add %s to %s: %s", host, iface, out)
        else:
            log.log(level, "added listen address %s to %s", host, iface)
            added.append(host)
    return added
