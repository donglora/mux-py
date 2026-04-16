"""Mux-level command interception (spec §13.2 / §13.4).

Certain commands can't be blindly forwarded when multiple clients share
one dongle. The mux absorbs them locally and synthesizes a response so
the dongle isn't perturbed. Relevant cases:

* **``SET_CONFIG``** (§13.2) — the first successful call locks the
  config; subsequent calls from *other* clients get
  ``ALREADY_MATCHED`` / ``LOCKED_MISMATCH`` with ``owner = OTHER``.
* **``RX_START`` / ``RX_STOP``** — reference-counted. The mux keeps RX
  running until the last interested client stops.

Mirrors ``mux-rs/src/intercept.rs``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto

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
    Owner,
    SetConfigResult,
    SetConfigResultCode,
    encode_err_payload,
)
from donglora.modulation import Modulation, decode_modulation, encode_modulation
from donglora_mux.session import ClientSession

log = logging.getLogger("donglora-mux")


class DecisionKind(Enum):
    FORWARD = auto()
    SYNTHESIZE = auto()
    DROP = auto()


@dataclass(frozen=True)
class Decision:
    """Outcome of :func:`decide` for a single command frame."""

    kind: DecisionKind
    # Only set when kind == SYNTHESIZE.
    type_id: int | None = None
    payload: bytes | None = None

    @classmethod
    def forward(cls) -> Decision:
        return cls(kind=DecisionKind.FORWARD)

    @classmethod
    def synthesize(cls, type_id: int, payload: bytes) -> Decision:
        return cls(kind=DecisionKind.SYNTHESIZE, type_id=type_id, payload=payload)

    @classmethod
    def drop(cls) -> Decision:
        return cls(kind=DecisionKind.DROP)


class MuxState:
    """Mux-level state shared across sessions."""

    __slots__ = ("locked",)

    def __init__(self) -> None:
        # ``None`` when no client holds the config lock, else
        # ``(client_id, current_modulation)``.
        self.locked: tuple[int, Modulation] | None = None


def rx_interest_count(sessions: dict[int, ClientSession]) -> int:
    return sum(1 for s in sessions.values() if s.rx_interested)


def decide(
    type_id: int,
    payload: bytes,
    client_id: int,
    state: MuxState,
    sessions: dict[int, ClientSession],
) -> Decision:
    """Decide what to do with an incoming client command.

    Returns :attr:`Decision.forward` to pass it to the dongle (after the
    caller rewrites the tag), :meth:`Decision.synthesize` to reply
    directly to the client without touching the dongle, or
    :meth:`Decision.drop` to silently swallow (reserved).

    Unknown commands forward: the firmware returns ``ERR(EUNKNOWN_CMD)``
    with the echoed tag, which keeps the mux forward-compatible with
    minor-version command additions.
    """
    if type_id == TYPE_SET_CONFIG:
        # Parse the modulation once for the lock comparison. A malformed
        # payload goes through and lets the firmware reject it.
        try:
            requested = decode_modulation(payload)
        except Exception:
            return Decision.forward()
        return _decide_set_config(requested, client_id, state, sessions)
    if type_id == TYPE_RX_START:
        return _decide_rx_start(client_id, sessions)
    if type_id == TYPE_RX_STOP:
        return _decide_rx_stop(client_id, sessions)
    # PING, GET_INFO, TX: forward unchanged.
    _ = (TYPE_PING, TYPE_TX)  # referenced for parity with mux-rs pattern
    return Decision.forward()


def _decide_set_config(
    requested: Modulation,
    client_id: int,
    state: MuxState,
    sessions: dict[int, ClientSession],
) -> Decision:
    # Single client: unconditionally forward (scanner mode — a lone
    # client can re-tune freely).
    if len(sessions) <= 1:
        return Decision.forward()

    if state.locked is None:
        # First SET_CONFIG with multiple clients — forward; the daemon
        # installs the lock once APPLIED comes back.
        return Decision.forward()

    owner, current = state.locked
    if owner == client_id:
        # Lock owner is reconfiguring — forward; daemon updates lock on APPLIED.
        return Decision.forward()

    # Another client holds the lock. Synthesize an OK with the
    # appropriate result code.
    matches = _modulations_match(requested, current)
    code = SetConfigResultCode.ALREADY_MATCHED if matches else SetConfigResultCode.LOCKED_MISMATCH
    result = SetConfigResult(result=code, owner=Owner.OTHER, current=current)
    label = sessions[client_id].label if client_id in sessions else f"id-{client_id}"
    log.debug("%s: SET_CONFIG %s (owner=OTHER) — synthesized", label, code.name)
    return Decision.synthesize(type_id=TYPE_OK, payload=result.encode())


def _decide_rx_start(client_id: int, sessions: dict[int, ClientSession]) -> Decision:
    client = sessions.get(client_id)
    if client is None:
        return Decision.forward()
    if client.rx_interested:
        # Already in RX — synthesize empty OK.
        return Decision.synthesize(type_id=TYPE_OK, payload=b"")
    if rx_interest_count(sessions) > 0:
        # Others already have RX running on the dongle; just mark this
        # client interested and synthesize OK.
        client.rx_interested = True
        return Decision.synthesize(type_id=TYPE_OK, payload=b"")
    # First interested client — turn RX on at the dongle.
    return Decision.forward()


def _decide_rx_stop(client_id: int, sessions: dict[int, ClientSession]) -> Decision:
    client = sessions.get(client_id)
    if client is None:
        return Decision.forward()
    if not client.rx_interested:
        # Wasn't listening — nothing to do.
        return Decision.synthesize(type_id=TYPE_OK, payload=b"")
    client.rx_interested = False
    if rx_interest_count(sessions) > 0:
        # Others still interested — don't stop RX.
        return Decision.synthesize(type_id=TYPE_OK, payload=b"")
    # Last interested client — forward so the dongle actually stops.
    return Decision.forward()


def _modulations_match(a: Modulation, b: Modulation) -> bool:
    """Byte-wise equality on the encoded wire form.

    Spec definition of "matches" is "same params on the wire" — this
    dodges any Python-level equality differences between dataclass
    instances that share the same wire representation but differ in
    default-valued fields.
    """
    try:
        return encode_modulation(a) == encode_modulation(b)
    except Exception:
        return False


def synthesize_err(code: ErrorCode | int) -> tuple[int, bytes]:
    """Build an ``ERR(code)`` synthesis payload (type_id + payload bytes)."""
    return (TYPE_ERR, encode_err_payload(code))
