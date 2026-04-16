"""DongLoRa USB Multiplexer — share one dongle with multiple applications.

Owns the USB serial connection and exposes a Unix domain socket (and
optional TCP listener) that speaks the DongLoRa Protocol v2 wire format. Clients
connect and get:

* **Tag-correlated solicited responses** — concurrent commands from
  multiple clients are routed back to the originator via a mux-owned
  device-tag allocator (``PROTOCOL.md §13.6``).
* **`RX` fan-out** — every `RX` packet is broadcast to all clients.
* **TX loopback** (``PROTOCOL.md §13.4``) — a successful over-the-air
  TX from one client is delivered to every other connected client as
  an `RX` event with ``origin = LOCAL_LOOPBACK``.
* **Smart `SET_CONFIG` arbitration** (``PROTOCOL.md §13.2``) — the
  first successful call locks the config; cross-client calls get
  `ALREADY_MATCHED` / `LOCKED_MISMATCH` with the lock owner echoed in
  ``current``.
* **`RX_START` / `RX_STOP` ref-counting** — the radio stays in RX as
  long as any client wants it.

Usage::

    donglora-mux [--port /dev/ttyACM0] [--socket /tmp/donglora-mux.sock] [--tcp 5741]

See ``PROTOCOL.md`` for the wire protocol and the Rust sibling
``donglora-mux`` (``cargo install donglora-mux``) for a drop-in
compatible implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

import donglora as dl
from donglora_mux.daemon import MuxDaemon

__all__ = ["MuxDaemon", "main"]

log = logging.getLogger("donglora-mux")


def _parse_tcp_addr(value: str) -> tuple[str, int] | None:
    if value.lower() == "none":
        return None
    if ":" in value:
        host, _, port_str = value.rpartition(":")
        return (host, int(port_str))
    return ("0.0.0.0", int(value))


def main() -> None:
    parser = argparse.ArgumentParser(description="DongLoRa USB Multiplexer (DongLoRa Protocol v2)")
    parser.add_argument(
        "--port",
        "-p",
        default=None,
        help="Serial port (auto-detect by USB VID:PID if omitted)",
    )
    parser.add_argument(
        "--socket",
        "-s",
        default=None,
        help="Unix socket path (default: $XDG_RUNTIME_DIR/donglora/mux.sock or /tmp/donglora-mux.sock)",
    )
    parser.add_argument(
        "--tcp",
        "-t",
        default="0.0.0.0:5741",
        metavar="[HOST:]PORT",
        help="TCP listen address (default: 0.0.0.0:5741, 'none' to disable)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    port = args.port or dl.find_port()
    if not port:
        log.info("waiting for DongLoRa device...")
        port = dl.wait_for_device()

    socket_path = args.socket or dl.default_socket_path()
    tcp_addr = _parse_tcp_addr(args.tcp)

    daemon = MuxDaemon(port, socket_path, tcp_addr=tcp_addr)

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.request_shutdown)
    try:
        loop.run_until_complete(daemon.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
