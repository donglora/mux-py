"""Integration tests for :class:`donglora_mux.daemon.MuxDaemon`.

Drives the daemon's dispatch logic directly without spinning up a real
serial port — we push synthetic dongle frames through
``_dispatch_device_frame`` and inspect what ends up in each client
session's queue.
"""

from __future__ import annotations

import asyncio

import pytest

from donglora.commands import (
    TYPE_PING,
    TYPE_SET_CONFIG,
    TYPE_TX,
    encode_set_config_payload,
    encode_tx_payload,
)
from donglora.events import (
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
)
from donglora.frame import Frame, decode_frame, encode_frame
from donglora.modulation import LoRaBandwidth, LoRaCodingRate, LoRaConfig
from donglora_mux.daemon import MuxDaemon
from donglora_mux.session import ClientSession
from donglora_mux.tagmap import TagMapper


def _lora(freq: int = 910_525_000) -> LoRaConfig:
    return LoRaConfig(
        freq_hz=freq,
        sf=7,
        bw=LoRaBandwidth.KHZ_62_5,
        cr=LoRaCodingRate.CR_4_5,
        preamble_len=16,
        sync_word=0x1424,
        tx_power_dbm=20,
    )


def _make_daemon() -> MuxDaemon:
    # Construct without touching filesystem / serial — we're exercising
    # the dispatch surface directly.
    return MuxDaemon("/dev/null", "/tmp/test-mux.sock", tcp_addr=None)


def _drain_queue(session: ClientSession) -> list[Frame]:
    """Non-blocking: drain whatever is currently in the session's queue."""
    out: list[Frame] = []
    while True:
        try:
            raw = session._queue.get_nowait()  # type: ignore[attr-defined]
        except asyncio.QueueEmpty:
            return out
        # Strip trailing 0x00 sentinel before decode_frame.
        assert raw.endswith(b"\x00")
        out.append(decode_frame(raw[:-1]))


@pytest.fixture
def daemon():
    d = _make_daemon()
    # Two sessions, manually added.
    a = ClientSession()
    b = ClientSession()
    d.sessions[a.id] = a
    d.sessions[b.id] = b
    yield d, a, b


async def test_dispatch_routes_tagged_response_to_owning_client(daemon):
    d, a, b = daemon
    # A issued a PING with upstream tag 0x0042.
    device_tag = d.tagmap.alloc(a.id, 0x0042, TYPE_PING)
    # Dongle replies with OK on the device_tag.
    await d._dispatch_device_frame(Frame(type_id=TYPE_OK, tag=device_tag, payload=b""))

    a_frames = _drain_queue(a)
    b_frames = _drain_queue(b)
    assert len(a_frames) == 1
    assert a_frames[0].type_id == TYPE_OK
    assert a_frames[0].tag == 0x0042
    assert b_frames == []  # Only A gets the routed response.


async def test_dispatch_fans_out_async_rx_to_all_clients(daemon):
    d, a, b = daemon
    rx = RxEvent(
        rssi_dbm=-80.0,
        snr_db=7.5,
        freq_err_hz=0,
        timestamp_us=1_000_000,
        crc_valid=True,
        packets_dropped=0,
        origin=RxOrigin.OTA,
        data=b"hello",
    )
    await d._dispatch_device_frame(Frame(type_id=TYPE_RX, tag=0, payload=rx.encode()))

    a_frames = _drain_queue(a)
    b_frames = _drain_queue(b)
    assert len(a_frames) == 1
    assert len(b_frames) == 1
    assert a_frames[0].type_id == TYPE_RX
    assert a_frames[0].tag == 0


async def test_set_config_applied_installs_lock(daemon):
    d, a, _b = daemon
    cfg = _lora()
    device_tag = d.tagmap.alloc(a.id, 0x0099, TYPE_SET_CONFIG)
    result = SetConfigResult(result=SetConfigResultCode.APPLIED, owner=Owner.MINE, current=cfg)
    await d._dispatch_device_frame(Frame(type_id=TYPE_OK, tag=device_tag, payload=result.encode()))
    # Lock should now be installed with A as owner and the applied cfg.
    assert d.state.locked is not None
    owner_id, locked_cfg = d.state.locked
    assert owner_id == a.id
    assert locked_cfg.freq_hz == cfg.freq_hz


async def test_tx_done_transmitted_loopbacks_to_peers(daemon):
    d, a, b = daemon
    # A sent a TX of "hi" with upstream tag 0x00AA.
    payload = encode_tx_payload(b"hi")
    # Stash like the writer would.
    a.tx_cache[0x00AA] = payload[1:]  # strip flags byte
    device_tag = d.tagmap.alloc(a.id, 0x00AA, TYPE_TX)
    # Absorb the intermediate OK (mapping kept, tx_cache still populated).
    await d._dispatch_device_frame(Frame(type_id=TYPE_OK, tag=device_tag, payload=b""))
    # Terminal TX_DONE(TRANSMITTED).
    td = TxDone(result=TxResult.TRANSMITTED, airtime_us=5_000)
    await d._dispatch_device_frame(Frame(type_id=TYPE_TX_DONE, tag=device_tag, payload=td.encode()))

    a_frames = _drain_queue(a)
    b_frames = _drain_queue(b)

    # A got OK (intermediate) + TX_DONE.
    assert [f.type_id for f in a_frames] == [TYPE_OK, TYPE_TX_DONE]
    assert all(f.tag == 0x00AA for f in a_frames)
    # B got a loopback RX (tag=0, origin=LOCAL_LOOPBACK, data="hi").
    assert len(b_frames) == 1
    assert b_frames[0].type_id == TYPE_RX
    assert b_frames[0].tag == 0
    decoded = RxEvent  # noqa: F841 — just a hint for readers
    # Inline decode (RxEvent.decode if available, else pull fields from the wire payload).
    buf = b_frames[0].payload
    assert buf[19] == int(RxOrigin.LOCAL_LOOPBACK)
    assert buf[20:] == b"hi"


async def test_disconnect_releases_lock(daemon):
    d, a, _b = daemon
    d.state.locked = (a.id, _lora())
    # Simulate the post-writer-cancel cleanup step in _remove_client.
    await d._remove_client(a)
    assert d.state.locked is None


async def test_concurrent_tagged_commands_route_independently(daemon):
    """Two concurrent PINGs from different clients must correlate correctly."""
    d, a, b = daemon
    tag_a = d.tagmap.alloc(a.id, 0x0001, TYPE_PING)
    tag_b = d.tagmap.alloc(b.id, 0x0001, TYPE_PING)
    # Dongle replies for B first, then A.
    await d._dispatch_device_frame(Frame(type_id=TYPE_OK, tag=tag_b, payload=b""))
    await d._dispatch_device_frame(Frame(type_id=TYPE_OK, tag=tag_a, payload=b""))
    a_frames = _drain_queue(a)
    b_frames = _drain_queue(b)
    assert len(a_frames) == 1
    assert len(b_frames) == 1
    # Each sees their own upstream tag, not the mux's internal device tag.
    assert a_frames[0].tag == 0x0001
    assert b_frames[0].tag == 0x0001


async def test_unsolicited_tagged_frame_is_dropped(daemon):
    """A frame with a tag that isn't in the pending map should be ignored,
    not routed to a random client or crash the dispatcher."""
    d, a, b = daemon
    # Tag 9999 was never allocated.
    await d._dispatch_device_frame(Frame(type_id=TYPE_OK, tag=9999, payload=b""))
    assert _drain_queue(a) == []
    assert _drain_queue(b) == []


def test_tag_mapper_isolation(daemon):
    """Regression check: tagmap should start empty per daemon instance."""
    d, _a, _b = daemon
    assert isinstance(d.tagmap, TagMapper)
    assert len(d.tagmap) == 0


def test_encode_decode_roundtrip_sanity():
    """Sanity check: encode_frame + strip sentinel + decode_frame are inverses
    for the shapes we push through the mux."""
    payload = encode_set_config_payload(_lora())
    wire = encode_frame(TYPE_SET_CONFIG, 42, payload)
    assert wire.endswith(b"\x00")
    frame = decode_frame(wire[:-1])
    assert frame.type_id == TYPE_SET_CONFIG
    assert frame.tag == 42
    assert frame.payload == payload
