"""python -m bcproxy -c config.yaml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import hik
from .camera import VirtualCamera
from .config import load
from .session import BcSession

log = logging.getLogger("main")


async def main_async(cfg_path: str, trace: bool | None) -> None:
    cfg = load(cfg_path)
    if trace is not None:
        cfg.trace = trace
    cams: list[VirtualCamera] = []
    servers = []
    for cc in cfg.cameras:
        cam = VirtualCamera(cc, cfg.templates_dir, cfg.unknown_reply, cfg.state_dir)
        await cam.start()
        cams.append(cam)

        async def on_conn(r, w, cam=cam):
            await BcSession(cam, r, w, trace=cfg.trace).run()

        srv = await asyncio.start_server(on_conn, cc.listen_host, cc.listen_port)
        servers.append(srv)
        log.info("%s: Reolink (Baichuan) endpoint on %s:%d -> Hikvision %s",
                 cc.name, cc.listen_host, cc.listen_port, cc.hik_host or "-")

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

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    for s in servers:
        s.close()
    for c in cams:
        await c.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Reolink Baichuan emulator for Hikvision cameras")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--trace", action="store_true", default=None,
                    help="log every decrypted Baichuan message (for building templates)")
    a = ap.parse_args()
    from .config import load as _load
    level = "debug" if (a.verbose or a.trace) else _load(a.config).log_level
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    asyncio.run(main_async(a.config, a.trace))


if __name__ == "__main__":
    main()
