"""Device-tag ↔ (client, upstream_tag) mapping.

DongLoRa Protocol v2 identifies each request/response pair with a 16-bit tag chosen
by the host. The mux sits between N clients and one dongle, so each
client picks its own tags but those tag spaces overlap. The mux reserves
a fresh *device tag* for every forwarded command, remembers which client
owned the originating upstream tag, and rewrites tags in both directions.

Mirrors ``mux-rs/src/daemon.rs::TagMapper``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Pending:
    """A command the mux has forwarded to the dongle and is waiting on."""

    client_id: int
    upstream_tag: int
    cmd_type: int


class TagMapper:
    """Monotonic u16 tag allocator with collision probing."""

    def __init__(self) -> None:
        self._next: int = 1  # 0 is reserved for async device→host events
        self._pending: dict[int, Pending] = {}

    def alloc(self, client_id: int, upstream_tag: int, cmd_type: int) -> int:
        """Allocate a fresh device tag and record the mapping.

        Skips 0 (reserved for async events) and wraps at ``u16::MAX``.
        On collision with an in-flight tag, probes forward linearly. In
        practice the pending map is small (tens of entries at most) so
        the probe is cheap.
        """
        while True:
            tag = self._next
            self._next = (self._next + 1) & 0xFFFF
            if self._next == 0:
                self._next = 1
            if tag == 0 or tag in self._pending:
                continue
            self._pending[tag] = Pending(
                client_id=client_id, upstream_tag=upstream_tag, cmd_type=cmd_type
            )
            return tag

    def peek(self, device_tag: int) -> Pending | None:
        """Return the pending mapping without removing it.

        Used for TX's two-phase completion: the intermediate ``OK``
        shouldn't free the slot because ``TX_DONE`` will land on the
        same tag later.
        """
        return self._pending.get(device_tag)

    def take(self, device_tag: int) -> Pending | None:
        """Final-resolve the mapping (remove + return)."""
        return self._pending.pop(device_tag, None)

    def drop_client(self, client_id: int) -> None:
        """Remove every in-flight mapping owned by this client.

        Called on disconnect so stale replies from the dongle don't get
        routed to dead file descriptors.
        """
        self._pending = {tag: p for tag, p in self._pending.items() if p.client_id != client_id}

    def __len__(self) -> int:
        return len(self._pending)
