"""Per-client session state and bounded send queue.

Each connected client gets a :class:`ClientSession` tracking its id,
RX-interest flag, a per-client TX cache (used by the §13.4 loopback
fan-out), and a bounded asyncio queue of outgoing wire-encoded frames.

Mirrors ``mux-rs/src/session.rs``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging

log = logging.getLogger("donglora-mux")

# Bounded send queue capacity per client. Matches mux-rs SEND_QUEUE_CAP.
SEND_QUEUE_CAP = 256

# Emit a drop-count summary after this many drops per client — a flood of
# RX while the client is slow shouldn't produce one log line per frame.
DROP_LOG_EVERY = 32

# Monotonic client-id source. Shared across every session in the process.
_next_client_id = itertools.count()


class ClientSession:
    """State carried for a single connected client.

    Does NOT own the underlying stream; the daemon spawns a per-client
    reader/writer pair of asyncio tasks that do I/O directly. This type
    is the shared rendez-vous point those tasks enqueue frames through.
    """

    __slots__ = ("_queue", "drops", "id", "rx_interested", "tx_cache")

    def __init__(self) -> None:
        self.id: int = next(_next_client_id)
        self.rx_interested: bool = False
        # Keyed by the client's upstream tag. Populated when a TX is
        # forwarded to the dongle; consumed on TX_DONE(TRANSMITTED) by
        # the reader so it can synthesize a loopback RX for other
        # clients without asking the sender to re-buffer the payload.
        self.tx_cache: dict[int, bytes] = {}
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=SEND_QUEUE_CAP)
        self.drops: int = 0

    @property
    def label(self) -> str:
        return f"client-{self.id}"

    async def next_outgoing(self) -> bytes:
        """Await the next frame to send. Intended for the client-writer task."""
        return await self._queue.get()

    def enqueue(self, frame: bytes) -> None:
        """Best-effort non-blocking enqueue of a wire frame.

        Full queue → drop the frame, bump the counter, occasionally log.
        This prevents one slow client from back-pressuring the whole mux.
        """
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.drops += 1
            if self.drops == 1 or self.drops % DROP_LOG_EVERY == 0:
                log.warning("%s: send queue full, dropped %d frames so far", self.label, self.drops)

    def close(self, writer: asyncio.StreamWriter | None = None) -> None:
        """Best-effort close. The daemon normally drives shutdown via
        task cancellation + stream close; this is a safety net."""
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
