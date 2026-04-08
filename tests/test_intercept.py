"""Tests for mux interception logic — ported from mux-rs/src/intercept.rs tests."""

from __future__ import annotations

import asyncio

from donglora.protocol import RADIO_CONFIG_SIZE
from donglora_mux import (
    CMD_TAG_SET_CONFIG,
    CMD_TAG_START_RX,
    CMD_TAG_STOP_RX,
    ERROR_INVALID_CONFIG,
    TAG_ERROR,
    TAG_OK,
    Client,
    MuxDaemon,
)


def _make_daemon() -> MuxDaemon:
    """Create a MuxDaemon with no real serial port (for interception tests only)."""
    daemon = MuxDaemon.__new__(MuxDaemon)
    daemon.serial_port = "/dev/null"
    daemon.socket_path = "/tmp/test-mux.sock"
    daemon.tcp_addr = None
    daemon.ser = None
    daemon.clients = {}
    daemon.cmd_queue = asyncio.Queue()
    daemon.pending_response = None
    daemon.locked_config = None
    daemon._shutdown = asyncio.Event()
    return daemon


def _make_client(daemon: MuxDaemon) -> Client:
    """Add a mock client to the daemon and return it."""
    reader = asyncio.StreamReader()
    # We can't easily create a StreamWriter without a transport, so we'll
    # test the interception logic directly via _maybe_intercept
    client = Client.__new__(Client)
    client.id = Client._next_id
    Client._next_id += 1
    client.reader = reader
    client.writer = None  # type: ignore[assignment]
    client.rx_interested = False
    client.send_queue = asyncio.Queue(maxsize=256)
    client._sender_task = None
    daemon.clients[client.id] = client
    return client


class TestSetConfigIntercept:
    def test_single_client_forwards(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        cmd = bytes([CMD_TAG_SET_CONFIG]) + bytes(RADIO_CONFIG_SIZE)
        result = asyncio.get_event_loop().run_until_complete(daemon._maybe_intercept(c1.id, cmd))
        assert result is None  # should forward to dongle

    def test_first_with_multiple_forwards(self) -> None:
        daemon = _make_daemon()
        _make_client(daemon)
        c2 = _make_client(daemon)
        cmd = bytes([CMD_TAG_SET_CONFIG]) + bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        result = asyncio.get_event_loop().run_until_complete(daemon._maybe_intercept(c2.id, cmd))
        assert result is None  # no locked config yet, forward

    def test_matching_returns_ok(self) -> None:
        daemon = _make_daemon()
        _make_client(daemon)
        c2 = _make_client(daemon)
        config_bytes = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        daemon.locked_config = config_bytes
        cmd = bytes([CMD_TAG_SET_CONFIG]) + config_bytes
        result = asyncio.get_event_loop().run_until_complete(daemon._maybe_intercept(c2.id, cmd))
        assert result == bytes([TAG_OK])

    def test_conflicting_returns_error(self) -> None:
        daemon = _make_daemon()
        _make_client(daemon)
        c2 = _make_client(daemon)
        daemon.locked_config = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        cmd = bytes([CMD_TAG_SET_CONFIG]) + bytes([99, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        result = asyncio.get_event_loop().run_until_complete(daemon._maybe_intercept(c2.id, cmd))
        assert result == bytes([TAG_ERROR, ERROR_INVALID_CONFIG])


class TestStartRxIntercept:
    def test_first_client_forwards(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        result = asyncio.get_event_loop().run_until_complete(
            daemon._maybe_intercept(c1.id, bytes([CMD_TAG_START_RX]))
        )
        assert result is None  # first interested client, forward

    def test_already_interested_returns_ok(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        c1.rx_interested = True
        result = asyncio.get_event_loop().run_until_complete(
            daemon._maybe_intercept(c1.id, bytes([CMD_TAG_START_RX]))
        )
        assert result == bytes([TAG_OK])

    def test_others_interested_marks_and_returns_ok(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        c2 = _make_client(daemon)
        c1.rx_interested = True
        result = asyncio.get_event_loop().run_until_complete(
            daemon._maybe_intercept(c2.id, bytes([CMD_TAG_START_RX]))
        )
        assert result == bytes([TAG_OK])
        assert c2.rx_interested is True


class TestStopRxIntercept:
    def test_not_interested_returns_ok(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        result = asyncio.get_event_loop().run_until_complete(
            daemon._maybe_intercept(c1.id, bytes([CMD_TAG_STOP_RX]))
        )
        assert result == bytes([TAG_OK])

    def test_others_remain_returns_ok(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        c2 = _make_client(daemon)
        c1.rx_interested = True
        c2.rx_interested = True
        result = asyncio.get_event_loop().run_until_complete(
            daemon._maybe_intercept(c1.id, bytes([CMD_TAG_STOP_RX]))
        )
        assert result == bytes([TAG_OK])
        assert c1.rx_interested is False

    def test_last_interested_forwards(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        c1.rx_interested = True
        result = asyncio.get_event_loop().run_until_complete(
            daemon._maybe_intercept(c1.id, bytes([CMD_TAG_STOP_RX]))
        )
        assert result is None  # last client, forward to dongle
        assert c1.rx_interested is False


class TestOtherCommands:
    def test_not_intercepted(self) -> None:
        daemon = _make_daemon()
        c1 = _make_client(daemon)
        for tag in [0, 1, 5, 6, 7, 8]:
            result = asyncio.get_event_loop().run_until_complete(
                daemon._maybe_intercept(c1.id, bytes([tag]))
            )
            assert result is None
