"""Unit tests for :mod:`donglora_mux.intercept`.

Ports the ``intercept`` test cases from ``mux-rs/src/intercept.rs``,
adapted for Python / asyncio.
"""

from __future__ import annotations

import pytest

from donglora.commands import (
    TYPE_GET_INFO,
    TYPE_PING,
    TYPE_RX_START,
    TYPE_RX_STOP,
    TYPE_SET_CONFIG,
    encode_set_config_payload,
)
from donglora.events import TYPE_OK, Owner, SetConfigResult, SetConfigResultCode
from donglora.modulation import LoRaBandwidth, LoRaCodingRate, LoRaConfig
from donglora_mux.intercept import DecisionKind, MuxState, decide
from donglora_mux.session import ClientSession


def _lora(freq_hz: int = 910_525_000) -> LoRaConfig:
    return LoRaConfig(
        freq_hz=freq_hz,
        sf=7,
        bw=LoRaBandwidth.KHZ_62_5,
        cr=LoRaCodingRate.CR_4_5,
        preamble_len=16,
        sync_word=0x1424,
        tx_power_dbm=20,
    )


def _make_sessions(n: int) -> dict[int, ClientSession]:
    return {s.id: s for s in (ClientSession() for _ in range(n))}


def _first_id(sessions: dict[int, ClientSession]) -> int:
    return min(sessions.keys())


def _second_id(sessions: dict[int, ClientSession]) -> int:
    ids = sorted(sessions.keys())
    return ids[1]


# ── SET_CONFIG ─────────────────────────────────────────────────────


def test_set_config_single_client_forwards():
    sessions = _make_sessions(1)
    state = MuxState()
    cid = _first_id(sessions)
    payload = encode_set_config_payload(_lora())
    d = decide(TYPE_SET_CONFIG, payload, cid, state, sessions)
    assert d.kind == DecisionKind.FORWARD


def test_set_config_first_multi_client_forwards():
    sessions = _make_sessions(2)
    state = MuxState()
    cid = _first_id(sessions)
    payload = encode_set_config_payload(_lora())
    d = decide(TYPE_SET_CONFIG, payload, cid, state, sessions)
    assert d.kind == DecisionKind.FORWARD


def test_set_config_matching_synthesizes_already_matched():
    sessions = _make_sessions(2)
    id_a = _first_id(sessions)
    id_b = _second_id(sessions)
    state = MuxState()
    state.locked = (id_a, _lora())
    payload = encode_set_config_payload(_lora())
    d = decide(TYPE_SET_CONFIG, payload, id_b, state, sessions)
    assert d.kind == DecisionKind.SYNTHESIZE
    assert d.type_id == TYPE_OK
    result = SetConfigResult.decode(d.payload)
    assert result.result == SetConfigResultCode.ALREADY_MATCHED
    assert result.owner == Owner.OTHER


def test_set_config_conflict_synthesizes_locked_mismatch_with_current_echoed():
    sessions = _make_sessions(2)
    id_a = _first_id(sessions)
    id_b = _second_id(sessions)
    state = MuxState()
    # A's lock is 910.525 MHz; B requests 915 MHz.
    state.locked = (id_a, _lora(910_525_000))
    payload = encode_set_config_payload(_lora(915_000_000))
    d = decide(TYPE_SET_CONFIG, payload, id_b, state, sessions)
    assert d.kind == DecisionKind.SYNTHESIZE
    result = SetConfigResult.decode(d.payload)
    assert result.result == SetConfigResultCode.LOCKED_MISMATCH
    assert result.owner == Owner.OTHER
    # The echoed `current` must reflect A's locked modulation, not B's request.
    assert isinstance(result.current, LoRaConfig)
    assert result.current.freq_hz == 910_525_000


def test_set_config_owner_reconfig_forwards():
    sessions = _make_sessions(2)
    id_a = _first_id(sessions)
    state = MuxState()
    state.locked = (id_a, _lora(910_525_000))
    # A re-tunes — should forward, daemon updates lock on APPLIED.
    payload = encode_set_config_payload(_lora(915_000_000))
    d = decide(TYPE_SET_CONFIG, payload, id_a, state, sessions)
    assert d.kind == DecisionKind.FORWARD


# ── RX_START ───────────────────────────────────────────────────────


def test_rx_start_first_forwards():
    sessions = _make_sessions(1)
    state = MuxState()
    cid = _first_id(sessions)
    d = decide(TYPE_RX_START, b"", cid, state, sessions)
    assert d.kind == DecisionKind.FORWARD


def test_rx_start_when_others_listening_marks_and_synthesizes_ok():
    sessions = _make_sessions(2)
    state = MuxState()
    id_a = _first_id(sessions)
    id_b = _second_id(sessions)
    sessions[id_a].rx_interested = True
    d = decide(TYPE_RX_START, b"", id_b, state, sessions)
    assert d.kind == DecisionKind.SYNTHESIZE
    assert d.type_id == TYPE_OK
    assert d.payload == b""
    assert sessions[id_b].rx_interested is True


def test_rx_start_already_interested_synthesizes_ok():
    sessions = _make_sessions(1)
    state = MuxState()
    cid = _first_id(sessions)
    sessions[cid].rx_interested = True
    d = decide(TYPE_RX_START, b"", cid, state, sessions)
    assert d.kind == DecisionKind.SYNTHESIZE
    assert d.type_id == TYPE_OK


# ── RX_STOP ────────────────────────────────────────────────────────


def test_rx_stop_not_interested_synthesizes_ok():
    sessions = _make_sessions(1)
    state = MuxState()
    cid = _first_id(sessions)
    d = decide(TYPE_RX_STOP, b"", cid, state, sessions)
    assert d.kind == DecisionKind.SYNTHESIZE
    assert d.type_id == TYPE_OK


def test_rx_stop_with_others_listening_clears_caller_and_synthesizes_ok():
    sessions = _make_sessions(2)
    state = MuxState()
    id_a = _first_id(sessions)
    id_b = _second_id(sessions)
    sessions[id_a].rx_interested = True
    sessions[id_b].rx_interested = True
    d = decide(TYPE_RX_STOP, b"", id_a, state, sessions)
    assert d.kind == DecisionKind.SYNTHESIZE
    assert d.type_id == TYPE_OK
    assert sessions[id_a].rx_interested is False
    assert sessions[id_b].rx_interested is True


def test_rx_stop_last_interested_forwards_and_clears():
    sessions = _make_sessions(1)
    state = MuxState()
    cid = _first_id(sessions)
    sessions[cid].rx_interested = True
    d = decide(TYPE_RX_STOP, b"", cid, state, sessions)
    assert d.kind == DecisionKind.FORWARD
    assert sessions[cid].rx_interested is False


# ── Other commands ─────────────────────────────────────────────────


@pytest.mark.parametrize("type_id", [TYPE_PING, TYPE_GET_INFO])
def test_other_commands_forward(type_id):
    sessions = _make_sessions(1)
    state = MuxState()
    cid = _first_id(sessions)
    d = decide(type_id, b"", cid, state, sessions)
    assert d.kind == DecisionKind.FORWARD
