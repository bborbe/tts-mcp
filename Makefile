include Makefile.variables
include Makefile.precommit

SERVICE = bborbe/tts-mcp

.PHONY: all
all: precommit

.PHONY: install
# Install dependencies (alias for sync)
install: sync

.PHONY: sync
# Sync dependencies
sync:
	@uv sync --all-extras

.PHONY: run
# Run the FastAPI TTS server (foreground)
run:
	uv run -m src.server

.PHONY: chat
# Run the interactive CLI (text-to-speech from the terminal)
chat:
	uv run -m src.main

.PHONY: download
# Download a Voxtral TTS model into data/models/
download:
	bash scripts/download-model.sh

.PHONY: clean-local
# Clean build artifacts (local)
clean-local:
	rm -rf .venv dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
