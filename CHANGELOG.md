# Changelog

## Unreleased

### Added

- Keepalive ping: detects unresponsive dongles (e.g. UART boards behind a
  CP2102 bridge where the serial port stays open across ESP32 resets). Pings
  every 5 seconds of idle, declares dongle lost after 2-second timeout.
- Radio state restoration after reconnect: saved SetConfig and StartRx are
  re-sent to the dongle so clients don't need to know a reset happened.

### Fixed

- Serial port opened without baud rate (defaulted to 9600 instead of 115200),
  preventing connection to UART boards behind CP2102 bridges.

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
