# Changelog

## 1.0.0 — 2026-04-22

### Breaking

- **Upgraded to the DongLoRa Protocol v2 wire format.** The 0.x line spoke the legacy
  v0.1 protocol with hardcoded tag constants (`TAG_OK=4`, `TAG_ERROR=5`,
  `CMD_TAG_SET_CONFIG=2`, …) that don't exist in v1.0. This release is
  wire-incompatible with 0.x clients and firmware.
- **Pins `donglora>=1.0.0`** and uses its `donglora.frame` /
  `donglora.events` / `donglora.commands` modules as the wire-level
  backbone. No more in-package reimplementation of the wire protocol.
- **Structural rewrite** mirroring the Rust sibling
  [`donglora-mux`](https://crates.io/crates/donglora-mux) one-to-one:
  `donglora_mux/{daemon,intercept,session,tagmap}.py` replace the single
  601-line `__init__.py`.

### Added

- **`TagMapper`** — per-command device-tag allocator. Enables concurrent
  out-of-order command/response correlation for multi-client setups
  (the v0.1-era single-oneshot-response slot is gone).
- **Full `§13.2` multi-client `SET_CONFIG` semantics** — tracks the
  `(client_id, modulation)` lock owner. Cross-client calls synthesize:
  - `OK(APPLIED / MINE, params)` when the caller owns the lock
    (forwarded to dongle).
  - `OK(ALREADY_MATCHED / OTHER, current)` when another client holds
    the lock and the requested params byte-match.
  - `OK(LOCKED_MISMATCH / OTHER, current)` when they differ; `current`
    echoes the lock owner's actual modulation.
  - Lock auto-releases when the owning client disconnects.
- **TX loopback fan-out (`§13.4`)** — on `TX_DONE(TRANSMITTED)` the mux
  synthesizes an `RX` frame with `origin = LOCAL_LOOPBACK` and sends it
  to every connected client except the transmitter.
- **Async `ERR(EFRAME)` fan-out** — if the dongle emits async framing
  errors (tag 0), they're broadcast to all clients rather than swallowed.
- **Per-client TX cache** on `ClientSession` so the loopback fan-out
  doesn't need the sender to re-buffer the payload.
- **Socket and serial file locking** (`fcntl.flock`) to prevent multiple
  mux instances from fighting over the same dongle or socket path.
- **500 ms keepalive** matching `PROTOCOL.md §3.4` (2× margin on the
  1 s inactivity timer). Replaces the 5 s cadence from 0.x.
- **Clean ^C / SIGTERM shutdown** — per-client handler tasks are tracked
  and cancelled on shutdown, so blocked `reader.read()` calls no longer
  pin the event loop open.

### Removed

- Every v0.1 wire constant and the inline `cobs` + raw `serial.Serial`
  frame codec.
- The single `pending_response: asyncio.Future` slot (fundamentally
  incompatible with v1.0's per-tag concurrency).

### Test coverage

- 28 tests (up from 11):
  - `test_tagmap.py` (6) — tag allocator behaviour.
  - `test_intercept.py` (13) — every branch of the `decide()` matrix,
    including the `ALREADY_MATCHED` / `LOCKED_MISMATCH` synthesis paths.
  - `test_multiclient.py` (9) — asyncio-driven daemon dispatch tests:
    concurrent tag routing, `SET_CONFIG` lock installation, TX loopback
    to peer clients, lock release on disconnect, unsolicited-frame drop.

## 0.2.0 — 2026-04-08

### Features

- **PyPI-ready metadata** — full classifiers, URLs, keywords, readme. Publishable
  as `donglora-mux`.
- **Interception test suite** — 11 tests ported from the Rust mux covering
  SetConfig locking, StartRx/StopRx reference counting, and passthrough
  behaviour.
- **GitHub Actions CI** — ruff lint + format, pytest on Python 3.10–3.13, PyPI
  publish on tag.
- **Strict ruff linting** — E, W, F, I, UP, B, C4, ARG, SIM, RUF rules.

### Fixes

- Fixed broken dependency path (`../../clients/python` → `../client-py`).
- Fixed dependency name (`donglora-python` → `donglora`).
- Applied `contextlib.suppress` where ruff/SIM105 flagged try-except-pass.
- Collapsed nested `if` per ruff/SIM102.

## 0.1.0 — 2026-04-06

Initial release.
