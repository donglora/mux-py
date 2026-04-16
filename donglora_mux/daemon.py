"""Core mux daemon — DongLoRa Protocol v2 tag-aware frame routing.

Owns the USB serial connection and exposes Unix-domain + TCP listeners.
Every inbound client frame goes through a :class:`TagMapper` that
allocates a fresh device tag and remembers the owning
``(client, upstream_tag)`` so the dongle's reply can be routed back.
Async frames from the dongle (``tag == 0``: ``RX``, async
``ERR(EFRAME)``) fan out to every client.

Additional spec-mandated behaviour:

* **§13.2** — ``SET_CONFIG`` arbitration with ``APPLIED`` /
  ``ALREADY_MATCHED`` / ``LOCKED_MISMATCH`` results and owner tracking.
  Handled in :mod:`donglora_mux.intercept`.
* **§13.4** — TX loopback: on ``TX_DONE(TRANSMITTED)`` the mux
  synthesizes an ``RX`` frame with ``origin = LOCAL_LOOPBACK`` and fans
  it out to every client that didn't send the original TX.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import serial

from donglora.commands import (
    TYPE_PING,
    TYPE_RX_START,
    TYPE_RX_STOP,
    TYPE_SET_CONFIG,
    TYPE_TX,
)
from donglora.errors import ErrorCode
from donglora.events import (
    TYPE_ERR,
    TYPE_OK,
    TYPE_RX,
    TYPE_TX_DONE,
    Owner,
    RxEvent,
    RxOrigin,
    SetConfigResult,
    SetConfigResultCode,
    TxDone,
    TxResult,
    encode_err_payload,
    parse_ok_payload,
)
from donglora.frame import Frame, FrameError, decode_frame, encode_frame
from donglora_mux.intercept import DecisionKind, MuxState, decide, rx_interest_count
from donglora_mux.session import ClientSession
from donglora_mux.tagmap import TagMapper

log = logging.getLogger("donglora-mux")

# ── Timing constants ───────────────────────────────────────────────

# Spec §3.4 inactivity timer is 1 s; ping at 500 ms for 2x margin.
KEEPALIVE_INTERVAL = 0.5

# How long to wait between dongle reconnect attempts. Reset on a clean
# long-running session.
RECONNECT_DELAY = 2.0

# Consecutive serial read errors before declaring the dongle lost.
MAX_CONSECUTIVE_ERRORS = 3

# Sentinel "client id" used by mux-internal commands (keepalive, synthetic
# RX_STOP on last-listener-disconnect). Never collides with a real client
# because ``itertools.count`` in session.py starts at 0 and climbs.
SYNTHETIC_CLIENT_ID = -1


# ── Writer work queue ──────────────────────────────────────────────


@dataclass
class _ForwardWork:
    client_id: int
    type_id: int
    upstream_tag: int
    payload: bytes


@dataclass
class _KeepaliveWork:
    pass


_WriterWork = _ForwardWork | _KeepaliveWork


# ── The daemon ─────────────────────────────────────────────────────


class MuxDaemon:
    def __init__(
        self,
        serial_port: str,
        socket_path: str,
        tcp_addr: tuple[str, int] | None = None,
    ) -> None:
        self.serial_port = serial_port
        self.socket_path = socket_path
        self.tcp_addr = tcp_addr
        self._shutdown = asyncio.Event()

        # Shared state. Accessed from the reader, writer, and each
        # per-client task; asyncio is single-threaded so a plain lock
        # isn't needed for the shared dicts, but the TagMapper and the
        # MuxState are accessed from async methods via direct refs.
        self.sessions: dict[int, ClientSession] = {}
        self.tagmap = TagMapper()
        self.state = MuxState()

        # Work queue: client handlers → dongle writer.
        self._work: asyncio.Queue[_WriterWork] = asyncio.Queue(maxsize=64)

        # Set by the reader/writer tasks when the dongle stops talking.
        self._dongle_lost: asyncio.Event = asyncio.Event()

        # Held file handles for the two flocks (socket + serial). Their
        # descriptors need to stay open for the life of the daemon —
        # closing frees the flock automatically.
        self._socket_lock_fd: int | None = None
        self._serial_lock_fd: int | None = None

        # Per-connection accept-handler tasks. Tracked so the shutdown
        # path can cancel them en masse — otherwise a client blocked in
        # ``reader.read()`` keeps the event loop alive indefinitely.
        self._client_tasks: set[asyncio.Task] = set()

    # ── Entry ─────────────────────────────────────────────────────

    def request_shutdown(self) -> None:
        """Idempotent; safe to call from a signal handler."""
        self._shutdown.set()

    async def run(self) -> None:
        """Run the daemon to completion. Returns when shutdown is signalled."""
        self._acquire_socket_lock()
        sock_path = Path(self.socket_path)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            log.info("removing stale socket %s", self.socket_path)
            with contextlib.suppress(FileNotFoundError):
                sock_path.unlink()

        unix_server = await asyncio.start_unix_server(self._accept_client, path=str(sock_path))
        log.info("Unix socket listening on %s", self.socket_path)

        tcp_server: asyncio.AbstractServer | None = None
        if self.tcp_addr is not None:
            tcp_server = await asyncio.start_server(
                self._accept_client, self.tcp_addr[0], self.tcp_addr[1]
            )
            log.info("TCP listening on %s:%d", *self.tcp_addr)

        try:
            await self._reconnect_loop()
        finally:
            log.info("shutting down...")
            # Stop accepting new clients first so nothing new piles up
            # while we're tearing down in-flight ones.
            unix_server.close()
            if tcp_server is not None:
                tcp_server.close()
            # Cancel every accepted-client handler. Without this, a
            # client blocked in ``reader.read()`` pins the event loop
            # open and ^C appears to hang.
            for task in list(self._client_tasks):
                task.cancel()
            if self._client_tasks:
                await asyncio.gather(*self._client_tasks, return_exceptions=True)
            await unix_server.wait_closed()
            if tcp_server is not None:
                await tcp_server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                sock_path.unlink()
            self._release_locks()
            log.info("stopped.")

    # ── Dongle reconnect loop ─────────────────────────────────────

    async def _reconnect_loop(self) -> None:
        while not self._shutdown.is_set():
            log.info("opening dongle on %s", self.serial_port)
            ser = await self._open_dongle_with_retry()
            if ser is None:  # shutdown requested mid-retry
                return

            self._dongle_lost.clear()
            reader_task = asyncio.create_task(self._dongle_reader(ser), name="dongle-reader")
            writer_task = asyncio.create_task(self._dongle_writer(ser), name="dongle-writer")
            keepalive_task = asyncio.create_task(self._keepalive_loop(), name="keepalive")

            # Wait for either loss or shutdown.
            await self._race([self._dongle_lost.wait(), self._shutdown.wait()])

            reader_task.cancel()
            writer_task.cancel()
            keepalive_task.cancel()
            await asyncio.gather(reader_task, writer_task, keepalive_task, return_exceptions=True)

            # Drain queued work.
            while not self._work.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._work.get_nowait()

            # The reader/writer tore down their view of `tagmap`'s
            # pending entries; reset so reconnect starts clean.
            self.tagmap = TagMapper()

            # Close the serial port.
            with contextlib.suppress(Exception):
                ser.close()

            if not self._shutdown.is_set():
                log.info("reconnecting in %.1fs...", RECONNECT_DELAY)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=RECONNECT_DELAY)
                    return  # shutdown fired
                except asyncio.TimeoutError:
                    pass

    async def _open_dongle_with_retry(self) -> serial.Serial | None:
        while not self._shutdown.is_set():
            ser = await asyncio.to_thread(self._open_and_ping)
            if ser is not None:
                return ser
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=RECONNECT_DELAY)
                return None  # shutdown fired
            except asyncio.TimeoutError:
                continue
        return None

    def _open_and_ping(self) -> serial.Serial | None:
        """Blocking: acquire the serial flock, open the port, PING.

        Returns the serial handle on success or ``None`` on failure.
        """
        try:
            self._acquire_serial_lock()
        except OSError as e:
            log.warning("serial port %s is locked by another process: %s", self.serial_port, e)
            return None

        try:
            ser = serial.Serial(self.serial_port, 115_200, timeout=2.0, exclusive=True)
        except (serial.SerialException, OSError) as e:
            log.warning("could not open %s: %s", self.serial_port, e)
            self._release_serial_lock()
            return None

        try:
            ser.reset_input_buffer()
            ser.write(encode_frame(TYPE_PING, 1, b""))
            ser.flush()
            # Read frames until we see any OK (the PING response).
            deadline = time.monotonic() + 2.0
            buf = bytearray()
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                ser.timeout = remaining
                chunk = ser.read(64)
                if not chunk:
                    continue
                buf.extend(chunk)
                for frame in _drain_frames(buf):
                    if frame.type_id == TYPE_OK:
                        log.info("dongle responded to PING")
                        ser.timeout = None
                        return ser
            log.warning("dongle did not respond to PING within 2 s")
        except (serial.SerialException, OSError, FrameError) as e:
            log.warning("PING round-trip failed: %s", e)

        with contextlib.suppress(Exception):
            ser.close()
        self._release_serial_lock()
        return None

    # ── Dongle I/O tasks ──────────────────────────────────────────

    async def _dongle_reader(self, ser: serial.Serial) -> None:
        """Pump bytes from the dongle; dispatch decoded frames."""
        buf = bytearray()
        errors = 0
        while not self._shutdown.is_set() and not self._dongle_lost.is_set():
            try:
                chunk = await asyncio.to_thread(_serial_read_chunk, ser, 512)
            except (serial.SerialException, OSError) as e:
                errors += 1
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    log.error("dongle read error (giving up after %d): %s", errors, e)
                    self._dongle_lost.set()
                    return
                log.warning("dongle read glitch (%d/%d): %s", errors, MAX_CONSECUTIVE_ERRORS, e)
                await asyncio.sleep(0.1)
                continue
            if not chunk:
                # Timeout — harmless in reader pump; keep reading.
                await asyncio.sleep(0)
                continue
            errors = 0
            buf.extend(chunk)

            # Drain every complete frame we can see.
            frame_errors = 0
            for frame_or_err in _drain_frames_or_errors(buf):
                if isinstance(frame_or_err, Frame):
                    await self._dispatch_device_frame(frame_or_err)
                else:
                    frame_errors += 1
            if frame_errors:
                log.warning("%d corrupt frame(s) from dongle (CRC/COBS)", frame_errors)
                await self._fanout_async_err(ErrorCode.EFRAME)

    async def _dispatch_device_frame(self, frame: Frame) -> None:
        # Async events (tag 0): fan out verbatim.
        if frame.tag == 0:
            wire = encode_frame(frame.type_id, 0, frame.payload)
            for session in self.sessions.values():
                session.enqueue(wire)
            return

        # Tagged: look up pending mapping.
        pending = self.tagmap.peek(frame.tag)
        if pending is None:
            log.warning(
                "unsolicited frame type=0x%02X tag=%d — no pending mapping",
                frame.type_id,
                frame.tag,
            )
            return

        # TX's two-phase completion: intermediate OK keeps the mapping
        # alive; only TX_DONE / ERR frees it.
        terminal = not (pending.cmd_type == TYPE_TX and frame.type_id == TYPE_OK)
        if terminal:
            self.tagmap.take(frame.tag)

        # Install SET_CONFIG lock on APPLIED/MINE.
        if pending.cmd_type == TYPE_SET_CONFIG and frame.type_id == TYPE_OK:
            try:
                parsed = parse_ok_payload(TYPE_SET_CONFIG, frame.payload)
                if (
                    isinstance(parsed, SetConfigResult)
                    and parsed.result == SetConfigResultCode.APPLIED
                    and parsed.owner == Owner.MINE
                ):
                    self.state.locked = (pending.client_id, parsed.current)
                    log.info("SET_CONFIG applied by client %d — lock installed", pending.client_id)
            except Exception as e:  # pragma: no cover — parser errors are logged, not fatal
                log.debug("parse_ok_payload(SET_CONFIG) failed: %s", e)

        # Track RX interest on RX_START/RX_STOP OK.
        if frame.type_id == TYPE_OK:
            session = self.sessions.get(pending.client_id)
            if session is not None:
                if pending.cmd_type == TYPE_RX_START:
                    session.rx_interested = True
                elif pending.cmd_type == TYPE_RX_STOP:
                    session.rx_interested = False

        # TX loopback: on TX_DONE(TRANSMITTED), synthesize an RX to every OTHER
        # client so peers see their neighbour's transmission (spec §13.4).
        tx_loopback_bytes: bytes | None = None
        if pending.cmd_type == TYPE_TX and frame.type_id == TYPE_TX_DONE:
            try:
                td = TxDone.decode(frame.payload)
                session = self.sessions.get(pending.client_id)
                if session is not None:
                    data = session.tx_cache.pop(pending.upstream_tag, None)
                    if data is not None and td.result == TxResult.TRANSMITTED:
                        tx_loopback_bytes = data
            except Exception as e:  # pragma: no cover
                log.debug("TxDone.decode failed: %s", e)

        # Route response to owning client with upstream tag restored.
        wire = encode_frame(frame.type_id, pending.upstream_tag, frame.payload)
        owning = self.sessions.get(pending.client_id)
        if owning is not None:
            owning.enqueue(wire)

        if tx_loopback_bytes is not None:
            rx = RxEvent(
                rssi_dbm=0.0,
                snr_db=0.0,
                freq_err_hz=0,
                timestamp_us=_now_us(),
                crc_valid=True,
                packets_dropped=0,
                origin=RxOrigin.LOCAL_LOOPBACK,
                data=tx_loopback_bytes,
            )
            loop_wire = encode_frame(TYPE_RX, 0, rx.encode())
            for cid, session in self.sessions.items():
                if cid != pending.client_id:
                    session.enqueue(loop_wire)

    async def _fanout_async_err(self, code: ErrorCode) -> None:
        wire = encode_frame(TYPE_ERR, 0, encode_err_payload(code))
        for session in self.sessions.values():
            session.enqueue(wire)

    async def _dongle_writer(self, ser: serial.Serial) -> None:
        while not self._shutdown.is_set() and not self._dongle_lost.is_set():
            try:
                work = await self._work.get()
            except asyncio.CancelledError:
                return

            if isinstance(work, _KeepaliveWork):
                # Synthetic mapping keeps the reply absorption clean.
                device_tag = self.tagmap.alloc(SYNTHETIC_CLIENT_ID, 0, TYPE_PING)
                wire = encode_frame(TYPE_PING, device_tag, b"")
            else:
                # If this is a TX, stash the payload (sans flags byte) so
                # the reader can fan out on TX_DONE(TRANSMITTED).
                if work.type_id == TYPE_TX and len(work.payload) > 1:
                    session = self.sessions.get(work.client_id)
                    if session is not None:
                        session.tx_cache[work.upstream_tag] = work.payload[1:]
                device_tag = self.tagmap.alloc(work.client_id, work.upstream_tag, work.type_id)
                wire = encode_frame(work.type_id, device_tag, work.payload)

            try:
                await asyncio.to_thread(_serial_write_all, ser, wire)
            except (serial.SerialException, OSError) as e:
                log.error("dongle write error: %s", e)
                self._dongle_lost.set()
                return

    async def _keepalive_loop(self) -> None:
        while not self._shutdown.is_set() and not self._dongle_lost.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=KEEPALIVE_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            if self._dongle_lost.is_set():
                return
            # Writer is behind if full; skip this tick rather than block.
            with contextlib.suppress(asyncio.QueueFull):
                self._work.put_nowait(_KeepaliveWork())

    # ── Client accept / handler ───────────────────────────────────

    async def _accept_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Disable Nagle on TCP to keep small DongLoRa Protocol frames snappy.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                import socket as _socket  # local import; only needed here

                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)

        session = ClientSession()
        self.sessions[session.id] = session
        log.info("%s connected (%d total)", session.label, len(self.sessions))

        # Register this task so the daemon's shutdown path can cancel
        # us out of the blocking reader.read().
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)

        writer_task = asyncio.create_task(
            self._client_writer(session, writer), name=f"{session.label}-writer"
        )
        try:
            await self._client_reader(session, reader)
        except asyncio.CancelledError:
            # Shutdown path cancelled us — fall through to cleanup.
            pass
        finally:
            writer_task.cancel()
            with contextlib.suppress(BaseException):
                await writer_task
            with contextlib.suppress(Exception):
                writer.close()
            await self._remove_client(session)
            if task is not None:
                self._client_tasks.discard(task)

    async def _client_reader(self, session: ClientSession, reader: asyncio.StreamReader) -> None:
        buf = bytearray()
        while True:
            try:
                chunk = await reader.read(4096)
            except (ConnectionError, OSError) as e:
                log.debug("%s reader exit: %s", session.label, e)
                return
            if not chunk:
                log.debug("%s reader exit: EOF", session.label)
                return
            buf.extend(chunk)
            for frame_or_err in _drain_frames_or_errors(buf):
                if isinstance(frame_or_err, Frame):
                    await self._handle_client_frame(session, frame_or_err)
                # Corrupt client frame: silently drop. The firmware pattern is
                # ERR(EFRAME, tag=0) back to the client, but the tag is
                # unrecoverable here so best-effort ignore.

    async def _handle_client_frame(self, session: ClientSession, frame: Frame) -> None:
        decision = decide(frame.type_id, frame.payload, session.id, self.state, self.sessions)
        if decision.kind == DecisionKind.SYNTHESIZE:
            assert decision.type_id is not None and decision.payload is not None
            wire = encode_frame(decision.type_id, frame.tag, decision.payload)
            session.enqueue(wire)
            return
        if decision.kind == DecisionKind.DROP:
            return

        # FORWARD — enqueue work for the dongle writer.
        work = _ForwardWork(
            client_id=session.id,
            type_id=frame.type_id,
            upstream_tag=frame.tag,
            payload=frame.payload,
        )
        try:
            await asyncio.wait_for(self._work.put(work), timeout=2.0)
        except asyncio.TimeoutError:
            # Writer is stalled (dongle disconnected) — synthesize EBUSY
            # back to the client with the upstream tag so their session
            # doesn't hang forever waiting.
            wire = encode_frame(TYPE_ERR, frame.tag, encode_err_payload(ErrorCode.EBUSY))
            session.enqueue(wire)

    async def _client_writer(self, session: ClientSession, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                frame = await session.next_outgoing()
            except asyncio.CancelledError:
                return
            try:
                writer.write(frame)
                await writer.drain()
            except (ConnectionError, OSError) as e:
                log.debug("%s writer exit: %s", session.label, e)
                return

    async def _remove_client(self, session: ClientSession) -> None:
        was_interested = session.rx_interested
        self.sessions.pop(session.id, None)
        self.tagmap.drop_client(session.id)

        # Release config lock if this client held it.
        if self.state.locked is not None and self.state.locked[0] == session.id:
            log.info("config lock released (owner %s disconnected)", session.label)
            self.state.locked = None

        if session.drops:
            log.info(
                "%s disconnected (%d remain, %d dropped frames)",
                session.label,
                len(self.sessions),
                session.drops,
            )
        else:
            log.info("%s disconnected (%d remain)", session.label, len(self.sessions))

        # If this was the last RX-interested client, tell the dongle to stop.
        if was_interested and rx_interest_count(self.sessions) == 0:
            with contextlib.suppress(asyncio.QueueFull):
                self._work.put_nowait(
                    _ForwardWork(
                        client_id=SYNTHETIC_CLIENT_ID,
                        type_id=TYPE_RX_STOP,
                        upstream_tag=0,
                        payload=b"",
                    )
                )

    # ── File locking ──────────────────────────────────────────────

    def _acquire_socket_lock(self) -> None:
        lock_path = f"{self.socket_path}.lock"
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise SystemExit(
                f"another donglora-mux is already running (lock held on {lock_path}): {e}"
            ) from e
        self._socket_lock_fd = fd

    def _acquire_serial_lock(self) -> None:
        lock_path = f"/tmp/donglora-serial-{self.serial_port.replace('/', '_')}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise
        # Close any previous serial lock fd (reconnect cycle).
        if self._serial_lock_fd is not None:
            os.close(self._serial_lock_fd)
        self._serial_lock_fd = fd

    def _release_serial_lock(self) -> None:
        if self._serial_lock_fd is not None:
            os.close(self._serial_lock_fd)
            self._serial_lock_fd = None

    def _release_locks(self) -> None:
        self._release_serial_lock()
        if self._socket_lock_fd is not None:
            os.close(self._socket_lock_fd)
            self._socket_lock_fd = None

    # ── Utilities ─────────────────────────────────────────────────

    @staticmethod
    async def _race(aws: list) -> None:
        tasks = [asyncio.ensure_future(a) for a in aws]
        try:
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        finally:
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t


# ── Module-level helpers ───────────────────────────────────────────


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _serial_read_chunk(ser: serial.Serial, n: int) -> bytes:
    """Read up to ``n`` bytes. Blocking — run in a thread."""
    ser.timeout = 0.1  # short poll so the reader task can check shutdown
    return ser.read(n)


def _serial_write_all(ser: serial.Serial, data: bytes) -> None:
    ser.write(data)
    ser.flush()


def _drain_frames(buf: bytearray):
    """Yield every complete :class:`Frame` in *buf*; advances *buf* in place.

    Raises on corrupt frames. Used by the bootstrap PING reader where we
    want to bubble up the error.
    """
    while True:
        try:
            idx = buf.index(0)
        except ValueError:
            return
        encoded = bytes(buf[:idx])
        del buf[: idx + 1]
        if not encoded:
            continue
        yield decode_frame(encoded)


def _drain_frames_or_errors(buf: bytearray):
    """Yield every complete frame (or :class:`FrameError`) in *buf*.

    Used by the long-running reader, which tolerates occasional
    corruption by counting + reporting rather than exploding.
    """
    while True:
        try:
            idx = buf.index(0)
        except ValueError:
            return
        encoded = bytes(buf[:idx])
        del buf[: idx + 1]
        if not encoded:
            continue
        try:
            yield decode_frame(encoded)
        except FrameError as e:
            yield e
