# DongLoRa Mux (Python)

USB multiplexer daemon — lets multiple applications share one DongLoRa
dongle simultaneously. Speaks the **DongLoRa Protocol v2** wire protocol
(see [`PROTOCOL.md`](https://github.com/donglora/firmware/blob/main/PROTOCOL.md)).

## What It Does

- Owns the USB serial connection exclusively (flock-protected).
- Exposes a Unix domain socket and optional TCP listener speaking the
  DongLoRa Protocol v2 frame format.
- **Tag-correlated solicited responses** — concurrent commands from
  multiple clients are routed back to the originator via a mux-allocated
  device-tag map (`PROTOCOL.md §13.6`).
- **`RX` fan-out** — every over-the-air RX is broadcast to all clients.
- **TX loopback** (`§13.4`) — a successful TX from one client is
  delivered to every other client as an `RX` event with
  `origin = LOCAL_LOOPBACK`.
- **Smart `SET_CONFIG` arbitration** (`§13.2`) — the first successful
  call locks the config; cross-client calls get `ALREADY_MATCHED` /
  `LOCKED_MISMATCH` with the lock owner's params echoed. A single
  connected client can still retune freely (scanner mode).
- **`RX_START` / `RX_STOP` ref-counting** — the radio stays in RX as
  long as any client wants it.

## Running

```sh
just run                            # start the mux daemon
just verbose                        # start with verbose logging
just run --tcp 5741 --port /dev/ttyACM0   # with options
```

## Depends On

- [`donglora`](https://github.com/donglora/client-py) (>= 1.0) — the
  Python client library. Provides the wire-level frame codec, event
  parsers, and USB discovery helpers this mux is built on.

## Relationship to `donglora-mux` (Rust)

This package is a Python port of the Rust
[`donglora-mux`](https://crates.io/crates/donglora-mux) daemon. The two
implementations are **functionally equivalent** — they speak the same
DongLoRa Protocol v2 wire protocol, implement the same `§13.2` / `§13.4` multi-client
semantics, and are drop-in interchangeable in front of the same dongle.
Pick whichever fits your ops environment:

- **Rust (`cargo install donglora-mux`)** — single binary, no runtime
  deps, ~5 MB release build. Better for production / embedded Linux.
- **Python (`pip install donglora-mux`)** — easy to hack on, integrates
  with a Python toolchain, useful when Rust isn't an option.

If you find a behavioural divergence between the two, that's a bug —
please file it.
