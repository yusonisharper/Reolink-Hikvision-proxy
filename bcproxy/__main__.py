"""python -m bcproxy -c config.yaml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import discovery, hik, logsetup, watchdog
from .camera import VirtualCamera
from .config import AppConfig, load
from .session import BcSession

log = logging.getLogger("main")


async def start_camera(cam: VirtualCamera, trace: bool, servers: list) -> None:
    """Ask the source camera what it offers, then listen: a client that logs in earlier would be
    told no PTZ, no playback. A camera that is offline only delays its own port."""
    cc = cam.cfg
    try:
        await cam.start()

        async def on_conn(r, w):
            await BcSession(cam, r, w, trace=trace).run()

        servers.append(await asyncio.start_server(on_conn, cc.listen_host, cc.listen_port))
    except OSError as e:      # the other cameras keep running
        log.error("%s: cannot listen on %s:%d: %s", cc.name, cc.listen_host, cc.listen_port, e)
        return
    log.info("%s: Reolink (Baichuan) endpoint on %s:%d -> %s %s",
             cc.name, cc.listen_host, cc.listen_port, cam.vendor_label, cc.hik_host or "-")


async def main_async(cfg: AppConfig, trace: bool | None) -> bool:
    """Returns True when a restart is needed."""
    if trace is not None:
        cfg.trace = trace
    cams = [VirtualCamera(cc, cfg.templates_dir, cfg.unknown_reply, cfg.state_dir) for cc in cfg.cameras]
    servers: list = []
    starting = [asyncio.create_task(start_camera(cam, cfg.trace, servers)) for cam in cams]

    if cfg.listen_port:
        by_ip = {c.cfg.hik_host: c for c in cams if c.cfg.event_source == "listen"}

        def route(peer: str, alert: hik.HikAlert) -> None:
            cam = by_ip.get(peer)
            if cam:
                cam.on_alert(alert)
            else:
                log.debug("push from unknown camera %s ignored", peer)

        servers.append(await hik.listen_server(cfg.listen_host, cfg.listen_port, route))
        log.info("Hikvision HTTP push receiver on %s:%d", cfg.listen_host, cfg.listen_port)

    if cfg.discovery and cams:
        try:
            servers.append(await discovery.serve(cams, *cfg.discovery))
            log.info("Reolink LAN search responder on udp %s:%d", *cfg.discovery)
        except OSError as e:
            log.warning("LAN search disabled, cannot bind udp %s:%d: %s", *cfg.discovery, e)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    watchdog.notify("READY=1")
    restart = await watchdog.supervise(cams, stop)
    watchdog.notify("STOPPING=1")
    for t in starting:
        t.cancel()
    for s in servers:
        s.close()
    for c in cams:
        await c.stop()
    return restart


def main() -> None:
    ap = argparse.ArgumentParser(description="Reolink Baichuan emulator for Hikvision and Dahua cameras")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--trace", action="store_true", default=None,
                    help="log every decrypted Baichuan message (for building templates)")
    ap.add_argument("--log-dir", help="also write rotating log files here (bcproxy.log, errors.log, crash.log)")
    a = ap.parse_args()
    # logging comes first, so that a broken config.yaml ends up in the log files too
    logsetup.setup("debug" if (a.verbose or a.trace) else "info", a.log_dir)
    log.info("starting, pid %d, python %s", os.getpid(), sys.version.split()[0])
    try:
        cfg = load(a.config)
    except Exception:
        log.critical("cannot load %s", a.config, exc_info=True)
        sys.exit(2)
    if not (a.verbose or a.trace):
        logging.getLogger().setLevel(cfg.log_level.upper())
    try:
        restart = asyncio.run(main_async(cfg, a.trace))
    except Exception:
        log.critical("fatal error", exc_info=True)
        sys.exit(1)
    if restart:
        log.error("exiting so that systemd restarts the service")
        sys.exit(1)
    log.info("stopped")


if __name__ == "__main__":
    main()
