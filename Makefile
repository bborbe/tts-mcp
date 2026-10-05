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

HELPER_APP := build/TTSDuck.app
HELPER_HOME := $(HOME)/Applications/TTSDuck.app

.PHONY: duck-helper
# Build and ad-hoc sign the TTSDuck helper app bundle
duck-helper:
	rm -rf $(HELPER_APP)
	mkdir -p $(HELPER_APP)/Contents/MacOS
	cp helper/Info.plist $(HELPER_APP)/Contents/Info.plist
	swiftc -swift-version 5 -O -o $(HELPER_APP)/Contents/MacOS/TTSDuck helper/main.swift -framework CoreAudio -framework Foundation
	codesign -s - --force --deep $(HELPER_APP)
	@echo "built $(HELPER_APP)"

.PHONY: duck-helper-install
# Install the helper to the stable path its audio-capture grant binds to.
# Rebuilding changes the cdhash and macOS will ask for the grant again.
duck-helper-install: duck-helper
	mkdir -p $(HOME)/Applications
	rm -rf $(HELPER_HOME)
	cp -R $(HELPER_APP) $(HELPER_HOME)
	@echo "installed $(HELPER_HOME)"
	@echo "grant it once: System Settings > Privacy & Security > Screen & System Audio Recording"

.PHONY: clean-local
# Clean build artifacts (local)
clean-local:
	rm -rf .venv dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
