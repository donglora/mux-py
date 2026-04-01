"""DongLoRa USB Multiplexer — share one dongle with multiple applications.

Owns the USB serial connection and exposes a Unix domain socket that speaks
the same COBS-framed protocol.  Clients connect via the socket (or just call
dl.connect() which auto-detects the mux) and get:

  • Solicited responses routed back to the commanding client
  • RxPacket frames broadcast to ALL connected clients
  • StartRx/StopRx reference-counted so the radio stays in RX as long as
    anyone wants it
  • SetConfig locked once set — subsequent clients must use the same config
    (a single client can change config freely, like a scanner)

Usage:
    donglora-mux [--port /dev/ttyACM0] [--socket /tmp/donglora-mux.sock] [--tcp 5741]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

import serial
from cobs import cobs

import donglora as dl

log = logging.getLogger("donglora-mux")

# ── Protocol constants ───────────────────────────────────────────

TAG_RXPACKET = 2
TAG_OK = 4
TAG_ERROR = 5
CMD_TAG_SET_CONFIG = 2
CMD_TAG_START_RX = 3
CMD_TAG_STOP_RX = 4

ERROR_INVALID_CONFIG = 0


def default_socket_path() -> str:
    """Resolve the mux socket path in priority order."""
    env = os.environ.get("DONGLORA_MUX")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "donglora", "mux.sock")
    return "/tmp/donglora-mux.sock"


# ── Per-client state ─────────────────────────────────────────────

class Client:
    """Tracks one connected client."""

    _next_id = 0

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.id = Client._next_id
        Client._next_id += 1
        self.reader = reader
        self.writer = writer
        self.rx_interested = False  # has this client called StartRx?
        self.send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._sender_task: asyncio.Task | None = None

    @property
    def label(self) -> str:
        return f"client-{self.id}"

    def start_sender(self) -> None:
        self._sender_task = asyncio.create_task(self._sender(), name=f"{self.label}-sender")

    async def _sender(self) -> None:
        """Drain send_queue → socket.  Runs until cancelled or write fails."""
        try:
            while True:
                frame = await self.send_queue.get()
                self.writer.write(frame)
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def enqueue(self, cobs_frame: bytes) -> None:
        """Enqueue a COBS frame for sending.  Drops oldest RxPacket on overflow."""
        try:
            self.send_queue.put_nowait(cobs_frame)
        except asyncio.QueueFull:
            # Drop oldest to make room (best-effort for slow clients)
            try:
                self.send_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.send_queue.put_nowait(cobs_frame)
            except asyncio.QueueFull:
                pass

    def close(self) -> None:
        if self._sender_task:
            self._sender_task.cancel()
        try:
            self.writer.close()
        except Exception:
            pass


# ── Mux daemon ───────────────────────────────────────────────────

class MuxDaemon:
    def __init__(self, serial_port: str, socket_path: str, tcp_addr: tuple[str, int] | None = None):
        self.serial_port = serial_port
        self.socket_path = socket_path
        self.tcp_addr = tcp_addr
        self.ser: serial.Serial | None = None
        self.clients: dict[int, Client] = {}
        self.cmd_queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.pending_response: asyncio.Future[bytes] | None = None
        self.locked_config: bytes | None = None  # raw RadioConfig bytes (13 bytes) once set
        self._shutdown = asyncio.Event()

    # ── Serial I/O (blocking, run in threads) ────────────────────

    def _serial_read_frame(self) -> bytes | None:
        """Blocking read of one COBS frame from dongle."""
        assert self.ser is not None
        return dl.read_frame(self.ser)

    def _serial_write(self, data: bytes) -> None:
        """Blocking write to dongle."""
        assert self.ser is not None
        self.ser.write(data)
        self.ser.flush()

    # ── Dongle reader task ───────────────────────────────────────

    async def dongle_reader(self) -> None:
        """Read frames from the dongle, dispatch solicited vs broadcast."""
        consecutive_errors = 0
        max_retries = 3
        while not self._shutdown.is_set():
            try:
                raw = await asyncio.to_thread(self._serial_read_frame)
            except (serial.SerialException, OSError) as e:
                consecutive_errors += 1
                if consecutive_errors >= max_retries:
                    log.error("Dongle read error (giving up after %d consecutive failures): %s", consecutive_errors, e)
                    self._handle_dongle_disconnect()
                    return
                log.warning("Dongle read glitch (%d/%d): %s", consecutive_errors, max_retries, e)
                await asyncio.sleep(0.1)
                continue

            consecutive_errors = 0

            if raw is None:
                continue

            tag = raw[0]
            cobs_frame = dl.cobs_encode(raw)

            if tag == TAG_RXPACKET:
                # Broadcast to all clients
                for client in list(self.clients.values()):
                    await client.enqueue(cobs_frame)
            else:
                # Solicited response — resolve the pending future
                if self.pending_response and not self.pending_response.done():
                    self.pending_response.set_result(raw)
                else:
                    log.warning("Unsolicited non-RxPacket response (tag %d) — dropped", tag)

    # ── Dongle writer task ───────────────────────────────────────

    async def dongle_writer(self) -> None:
        """Pull commands from queue, send one at a time, route responses."""
        loop = asyncio.get_running_loop()

        while not self._shutdown.is_set():
            client_id, raw_cmd = await self.cmd_queue.get()

            # Check for StartRx/StopRx ref-counting intercept
            intercepted = await self._maybe_intercept(client_id, raw_cmd)
            if intercepted is not None:
                # Send the synthetic response to the client
                if client_id in self.clients:
                    await self.clients[client_id].enqueue(dl.cobs_encode(intercepted))
                continue

            # Create future for the solicited response
            self.pending_response = loop.create_future()

            # Send COBS-encoded command to dongle
            cobs_frame = dl.cobs_encode(raw_cmd)
            try:
                await asyncio.to_thread(self._serial_write, cobs_frame)
            except (serial.SerialException, OSError) as e:
                log.error("Dongle write error: %s", e)
                self.pending_response.set_result(bytes([5, 1]))  # Error(RadioBusy)
                self._handle_dongle_disconnect()
                return

            # Wait for dongle_reader to resolve the future
            try:
                response = await asyncio.wait_for(self.pending_response, timeout=10.0)
            except asyncio.TimeoutError:
                log.error("Dongle response timeout")
                response = bytes([5, 1])  # Error(RadioBusy)
            finally:
                self.pending_response = None

            # Track state on successful forward
            cmd_tag = raw_cmd[0]
            resp_tag = response[0] if response else 0xFF
            if cmd_tag == CMD_TAG_SET_CONFIG and resp_tag == TAG_OK:
                self.locked_config = raw_cmd[1:]
                log.info("Radio config locked: %s", self.locked_config.hex())
            elif cmd_tag == CMD_TAG_START_RX and resp_tag == TAG_OK:
                if client_id in self.clients:
                    self.clients[client_id].rx_interested = True
            elif cmd_tag == CMD_TAG_STOP_RX and resp_tag == TAG_OK:
                if client_id in self.clients:
                    self.clients[client_id].rx_interested = False

            # Route response to commanding client (if still connected)
            if client_id in self.clients:
                await self.clients[client_id].enqueue(dl.cobs_encode(response))

    # ── StartRx / StopRx reference counting ──────────────────────

    def _rx_interest_count(self) -> int:
        return sum(1 for c in self.clients.values() if c.rx_interested)

    async def _maybe_intercept(self, client_id: int, raw_cmd: bytes) -> bytes | None:
        """Intercept SetConfig/StartRx/StopRx for smart multiplexing.

        Returns a synthetic response if intercepted, None to forward normally.
        """
        if not raw_cmd:
            return None
        cmd_tag = raw_cmd[0]
        client = self.clients.get(client_id)

        if cmd_tag == CMD_TAG_SET_CONFIG:
            config_bytes = raw_cmd[1:]  # 13-byte RadioConfig
            if len(self.clients) <= 1:
                # Single client — allow free config changes (scanner mode)
                # Forward to dongle, update locked_config on success
                return None
            if self.locked_config is None:
                # First SetConfig with multiple clients — forward and lock
                return None
            if config_bytes == self.locked_config:
                # Same config — reply Ok without hitting the dongle
                log.debug("%s: SetConfig matches locked config — Ok",
                          client.label if client else f"id-{client_id}")
                return bytes([TAG_OK])
            # Different config — reject
            log.warning("%s: SetConfig rejected (conflicts with locked config)",
                        client.label if client else f"id-{client_id}")
            return bytes([TAG_ERROR, ERROR_INVALID_CONFIG])

        if cmd_tag == CMD_TAG_START_RX:
            if client and client.rx_interested:
                # Already interested — no-op, reply Ok
                return bytes([TAG_OK])
            if self._rx_interest_count() > 0:
                # Someone else is already receiving — just mark this client, reply Ok
                if client:
                    client.rx_interested = True
                return bytes([TAG_OK])
            # First interested client — forward to dongle (return None)
            return None

        if cmd_tag == CMD_TAG_STOP_RX:
            if client and not client.rx_interested:
                # Wasn't interested — no-op, reply Ok
                return bytes([TAG_OK])
            if client:
                client.rx_interested = False
            if self._rx_interest_count() > 0:
                # Others still interested — don't stop, reply Ok
                return bytes([TAG_OK])
            # Last interested client — forward to dongle (return None)
            return None

        return None

    # ── Client handling ──────────────────────────────────────────

    async def client_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(reader, writer)
        self.clients[client.id] = client
        client.start_sender()
        log.info("%s connected (%d total)", client.label, len(self.clients))

        try:
            buf = b""
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                # Extract complete COBS frames (delimited by 0x00)
                while b"\x00" in buf:
                    idx = buf.index(b"\x00")
                    encoded = buf[:idx]
                    buf = buf[idx + 1:]
                    if not encoded:
                        continue
                    try:
                        raw_cmd = cobs.decode(encoded)
                    except cobs.DecodeError:
                        log.warning("%s: bad COBS frame — skipped", client.label)
                        continue
                    await self.cmd_queue.put((client.id, raw_cmd))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await self._remove_client(client)

    async def _remove_client(self, client: Client) -> None:
        """Clean up after a disconnected client."""
        was_interested = client.rx_interested
        client.close()
        self.clients.pop(client.id, None)
        log.info("%s disconnected (%d remain)", client.label, len(self.clients))

        # If this client had RX interest and was the last one, stop RX
        if was_interested and self._rx_interest_count() == 0:
            log.info("Last RX-interested client gone — sending StopRx")
            await self.cmd_queue.put((-1, bytes([CMD_TAG_STOP_RX])))

        # Reset locked config when all clients are gone
        if not self.clients:
            if self.locked_config is not None:
                log.info("All clients gone — config lock released")
                self.locked_config = None

    # ── Dongle disconnect ────────────────────────────────────────

    _dongle_lost: asyncio.Event

    def _handle_dongle_disconnect(self) -> None:
        log.error("Dongle disconnected — will reconnect")
        if self.pending_response and not self.pending_response.done():
            self.pending_response.set_result(bytes([5, 1]))  # Error(RadioBusy)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self._dongle_lost.set()

    # ── Dongle connection ──────────────────────────────────────────

    def _open_dongle(self) -> bool:
        """Open and ping the dongle. Returns True on success."""
        try:
            self.ser = serial.Serial(self.serial_port, timeout=2.0, exclusive=True)
            self.ser.reset_input_buffer()
            self._serial_write(dl.cobs_encode(dl.encode_command("Ping")))
            pong = self._serial_read_frame()
            if not pong or pong[0] != 0:
                log.warning("Dongle did not respond to Ping")
                self.ser.close()
                self.ser = None
                return False
            log.info("Dongle responded to Ping")
            return True
        except (serial.SerialException, OSError) as e:
            log.warning("Could not open dongle: %s", e)
            self.ser = None
            return False

    # ── Main entry ───────────────────────────────────────────────

    async def run(self) -> None:
        self._dongle_lost = asyncio.Event()

        # Prepare socket path
        sock_path = Path(self.socket_path)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()

        # Start servers (these stay up for the lifetime of the daemon)
        server = await asyncio.start_unix_server(self.client_handler, path=str(sock_path))
        log.info("Unix socket listening on %s", self.socket_path)

        tcp_server = None
        if self.tcp_addr:
            tcp_server = await asyncio.start_server(
                self.client_handler, self.tcp_addr[0], self.tcp_addr[1],
            )
            log.info("TCP listening on %s:%d", *self.tcp_addr)

        # Connect/reconnect loop — servers stay up, dongle comes and goes
        try:
            while not self._shutdown.is_set():
                # Connect to dongle (retry until it appears)
                log.info("Opening dongle on %s", self.serial_port)
                while not self._shutdown.is_set():
                    if await asyncio.to_thread(self._open_dongle):
                        break
                    await asyncio.sleep(2)

                if self._shutdown.is_set():
                    break

                # Run dongle reader/writer until dongle is lost
                self._dongle_lost.clear()
                reader_task = asyncio.create_task(self.dongle_reader(), name="dongle-reader")
                writer_task = asyncio.create_task(self.dongle_writer(), name="dongle-writer")

                # Wait for dongle loss or shutdown
                shutdown_task = asyncio.create_task(self._shutdown.wait())
                lost_task = asyncio.create_task(self._dongle_lost.wait())
                await asyncio.wait(
                    [shutdown_task, lost_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                shutdown_task.cancel()
                lost_task.cancel()

                # Tear down dongle tasks
                reader_task.cancel()
                writer_task.cancel()
                try:
                    await asyncio.gather(reader_task, writer_task, return_exceptions=True)
                except asyncio.CancelledError:
                    pass

                # Drain any pending commands with errors
                while not self.cmd_queue.empty():
                    try:
                        self.cmd_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                if not self._shutdown.is_set():
                    log.info("Reconnecting in 2 seconds...")
                    await asyncio.sleep(2)
        finally:
            # Cleanup
            log.info("Shutting down...")
            for client in list(self.clients.values()):
                client.close()
            server.close()
            await server.wait_closed()
            if tcp_server:
                tcp_server.close()
                await tcp_server.wait_closed()
            if self.ser:
                self.ser.close()
            if sock_path.exists():
                sock_path.unlink()
            log.info("Stopped.")


# ── CLI ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DongLoRa USB Multiplexer")
    parser.add_argument("--port", "-p", default=None, help="Serial port (auto-detect if omitted)")
    parser.add_argument("--socket", "-s", default=None, help="Unix socket path")
    parser.add_argument("--tcp", "-t", default="0.0.0.0:5741", metavar="[HOST:]PORT",
                        help="TCP listen address (default: 0.0.0.0:5741, 'none' to disable)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    port = args.port or dl.find_port()
    if not port:
        log.info("Waiting for DongLoRa device...")
        port = dl.wait_for_device()

    socket_path = args.socket or default_socket_path()

    tcp_addr = None
    if args.tcp and args.tcp.lower() != "none":
        if ":" in args.tcp:
            host, _, p = args.tcp.rpartition(":")
            tcp_addr = (host, int(p))
        else:
            tcp_addr = ("0.0.0.0", int(args.tcp))

    daemon = MuxDaemon(port, socket_path, tcp_addr=tcp_addr)

    loop = asyncio.new_event_loop()

    def _signal_handler() -> None:
        daemon._shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        loop.run_until_complete(daemon.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
