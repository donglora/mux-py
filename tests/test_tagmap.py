"""Unit tests for :mod:`donglora_mux.tagmap`.

Mirrors the ``TagMapper`` test cases in ``mux-rs/src/daemon.rs``.
"""

from __future__ import annotations

from donglora.commands import TYPE_PING, TYPE_TX
from donglora_mux.tagmap import TagMapper


def test_alloc_skips_zero():
    m = TagMapper()
    # Allocating many tags in a row, each must be non-zero. Even after
    # many alloc/take cycles, we should never hand out 0 (reserved for
    # async events).
    for _ in range(1024):
        tag = m.alloc(1, 100, TYPE_PING)
        assert tag != 0
        m.take(tag)


def test_take_returns_mapping_and_removes():
    m = TagMapper()
    tag = m.alloc(42, 7, TYPE_TX)
    p = m.take(tag)
    assert p is not None
    assert p.client_id == 42
    assert p.upstream_tag == 7
    assert p.cmd_type == TYPE_TX
    assert len(m) == 0


def test_peek_keeps_mapping():
    m = TagMapper()
    tag = m.alloc(42, 7, TYPE_TX)
    p = m.peek(tag)
    assert p is not None
    assert p.client_id == 42
    assert len(m) == 1


def test_drop_client_removes_only_that_clients_tags():
    m = TagMapper()
    t_a1 = m.alloc(1, 10, TYPE_PING)
    t_a2 = m.alloc(1, 11, TYPE_PING)
    t_b = m.alloc(2, 12, TYPE_PING)
    m.drop_client(1)
    assert m.take(t_a1) is None
    assert m.take(t_a2) is None
    assert m.take(t_b) is not None


def test_concurrent_alloc_yields_unique_tags():
    m = TagMapper()
    tags = {m.alloc(1, i, TYPE_PING) for i in range(100)}
    assert len(tags) == 100
    assert 0 not in tags


def test_allocations_recover_after_take():
    # After a take(), the freed tag can be re-used (post-wrap scenario).
    m = TagMapper()
    first = m.alloc(1, 0, TYPE_PING)
    m.take(first)
    # Keep filling until we wrap and get `first` back. Don't assert
    # eagerly because the allocator is monotonic; just prove it
    # terminates within the u16 space.
    seen = {first}
    while True:
        t = m.alloc(1, 0, TYPE_PING)
        if t in seen:
            break
        seen.add(t)
        m.take(t)
        if len(seen) > 0x10000:  # pragma: no cover — safety
            raise AssertionError("allocator failed to recycle slots")
