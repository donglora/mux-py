set shell := ["bash", "-c"]

default: run

[private]
_ensure_tools:
    @mise trust --yes . 2>/dev/null; mise install --quiet

# Run the mux daemon
run *args: _ensure_tools
    @uv run -m donglora_mux {{args}}

# Run with verbose logging
verbose *args: _ensure_tools
    @uv run -m donglora_mux --verbose {{args}}
