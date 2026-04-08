# Changelog

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
