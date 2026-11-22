.DEFAULT_GOAL := help

PYTHON ?= python3
CARGO_FLAGS ?=
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: help build test check test-integration

help:
	@printf '%s\n' \
	  'make build             Build and install the native Rust helper into bin/' \
	  'make test              Build, run CLI tests and compare the Python reference' \
	  'make check             Check Rust formatting and Clippy warnings' \
	  'make test-integration  Run Rex/fzf tests in disposable sessions (macOS)' \
	  'Add CARGO_FLAGS=--offline to use cached Cargo dependencies'

build:
	./scripts/build.sh $(CARGO_FLAGS)

test: build
	cargo test --all-targets --locked $(CARGO_FLAGS)
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	$(PYTHON) tests/check_equivalence.py

check:
	cargo fmt --check
	cargo clippy --all-targets --locked $(CARGO_FLAGS) -- -D warnings

test-integration: build
	rex config check examples/init.lua --json
	$(PYTHON) tests/integration/check_picker.py
	$(PYTHON) tests/integration/check_tab.py
	$(PYTHON) tests/integration/check_session.py
	$(PYTHON) tests/integration/check_osc52.py
	$(PYTHON) tests/integration/check_zoom.py
	$(PYTHON) tests/integration/check_toggle.py
	$(PYTHON) tests/integration/check_bootstrap.py
