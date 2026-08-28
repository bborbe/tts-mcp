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

.PHONY: skip
# Stop the utterance that is playing; the next queued message starts immediately
skip:
	bash scripts/tts-skip

.PHONY: pause
# Pause the utterance that is playing; resume later with make resume
pause:
	bash scripts/tts-pause

.PHONY: resume
# Resume the utterance that was paused with make pause
resume:
	bash scripts/tts-resume

.PHONY: download
# Download a Voxtral TTS model into data/models/
download:
	bash scripts/download-model.sh

.PHONY: clean-local
# Clean build artifacts (local)
clean-local:
	rm -rf .venv dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
