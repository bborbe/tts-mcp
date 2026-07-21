# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Collapsed the worker's parameter bloat into an `AudioSettings` frozen dataclass (sample rate, lead silence, normalization params, meter, streaming params). `audio_worker` / `audio_worker_from_model_id` / `streaming_chunk_iter` now take `(…, settings)` instead of 10–14 positional args; the CLI builds it from `CliConfig` and the server from `ServerState.audio_settings()`. No behaviour change.
- Split the `src/tts.py` god-module (~1300 LOC) into a `src/tts/` package with focused submodules — `text`, `config`, `protocols`, `generate`, `normalize`, `device` (CoreAudio HAL), `player` (sounddevice playback), `worker` — re-exporting the same public API from `__init__.py`, so `server.py`/`main.py` and their imports are unchanged. Behaviour is identical (no logic changes); the largest module is now ~360 LOC. Test mock targets were updated to patch the submodule where each dependency is used (`src.tts.player.sd`, `src.tts.generate.load`).
- Conformed the build tooling to the standard `bborbe/python-skeleton` layout: replaced the `justfile` with a `Makefile` (`+ Makefile.variables`, `Makefile.precommit`) exposing `sync` / `run` / `chat` / `download` / `format` / `lint` / `typecheck` / `check` / `test` / `precommit`. The toolchain is now lean — ruff, mypy, pyright, and pytest (including the `tests/architecture/` import-rule tests) — dropping semgrep, bandit, deptry, codespell, pip-audit, and pygount. This also resolved the dependency vulnerabilities: removing semgrep (which hard-pinned `click~=8.1.8` and `mcp==1.23.3`) let `click` float to a patched release and dropped the vulnerable `mcp` entirely, and `pillow` was bumped to 12.3.0 — cutting the locked dependency set from 152 to 84 packages.

### Added

- GitHub Actions CI (`.github/workflows/ci.yml`) running `make precommit` on push/PR to `main`/`master`, on a macOS Apple-Silicon runner (`mlx-audio` and `sounddevice` are macOS/arm64-only and imported at module load).

- Low-latency streaming playback within a single utterance, toggled by three new required `config.yaml` keys: `stream` (bool), `streaming_interval` (seconds of audio per chunk, e.g. `1.0`), and `streaming_warmup_seconds` (warm-up window for streaming loudness normalization, e.g. `2.0`). When `stream: true`, audio is written to the output device chunk-by-chunk as the model generates it (`model.generate(stream=True)`), so playback starts after the first chunk instead of after the whole utterance is generated and normalized — the previous behavior effectively buffered the entire WAV before any sound (measured: time-to-first-sound dropped from ~2.6s to ~0.4s on an 8s utterance). When `stream: false`, the prior buffered path with cross-utterance lookahead is used unchanged.
- Warm-up-window loudness normalization for streaming mode: whole-signal LUFS can't be measured before playback, so the first `streaming_warmup_seconds` of audio are buffered, a single boost-only, true-peak-capped gain is measured on that window (ITU-R BS.1770-4, same as the buffered path), and that gain is applied to every streamed chunk (later chunks hard-limited to the true-peak ceiling to avoid clipping). This restores loudness parity with the buffered path for quiet voices while keeping most of the latency win — instead of streamed audio playing many dB quieter than buffered. The gain computation is shared with the buffered path via the extracted `boost_gain` helper.
- Config file location is now resolved in precedence order: `$TTS_MCP_CONFIG` → `$XDG_CONFIG_HOME/tts-mcp/config.yaml` (defaults to `~/.config/tts-mcp/config.yaml`) → `./config.yaml` (project-root fallback). Both the Python server/CLI and the TypeScript MCP relay honor the same order, so machine-local config can live outside the repo. `model:`/`models_dir:`/data paths stay relative to the working directory, not the config file.

### Changed

- Handle default-output-device switches by restarting the whole process (via a background watcher that polls `kAudioHardwarePropertyDefaultOutputDevice` and, on a sustained change from the boot device, exits so launchd `KeepAlive` respawns a fresh process with a clean CoreAudio HAL) instead of re-initializing PortAudio in place. The warm stream is no longer re-queried and reopened per utterance: the HAL reports transient/aggregate device ids mid-playback that flip back within milliseconds, and reopening on them tore down and rebuilt the PortAudio stream every utterance — the repeated in-process re-init degraded the HAL and produced distorted playback. The watcher compares against the boot device only, so those transient blips are ignored and only a real sustained switch triggers a restart.
- Keep one output stream warm across utterances and re-initialize PortAudio only when the default output device actually changes, detected by reading `kAudioHardwarePropertyDefaultOutputDevice` from the CoreAudio HAL via ctypes (a live signal that does not require tearing PortAudio down). This replaces the previous per-utterance `sd._terminate()/_initialize()`, which degraded the CoreAudio HAL over time and produced distorted playback after the server had been running a while, while still following a live switch between two connected devices without a restart.
- Raised default `lead_silence_ms` from 200 to 400 to absorb Bluetooth link-up latency on the first utterance after a stream open.

### Fixed

- Fixed CLI playback failure on current MLX by loading models inside worker threads.
- Fixed server TTS failure by loading the model inside the audio worker thread.
- Fixed playback failing with CoreAudio error -10851 after switching the default output device: the audio player now re-enumerates PortAudio devices on each stream open, so a long-running server recovers on the next utterance instead of requiring a restart.
- Fixed silent playback after switching the default output between two *connected* devices (e.g. AirPods → wired headset): the previous warm stream stayed bound to the old device with no write error to trigger a reopen. Opening a fresh stream per utterance now routes to the current default without a restart.

## 2026-06-13

### Security

- Updated dependencies to resolve security vulnerabilities.

## 2026-06-12

### Changed

- Enabled WAV file output by default (`save_wav: true`).

## 2026-04-13

### Changed

- Renamed `just run` to `just chat` across justfile, README, and CLAUDE.md
- Interactive chat now requires pressing Enter twice to submit text, allowing multi-line input
- Interactive chat now requires pressing ESC twice to quit instead of once
- Empty enter no longer quits the interactive chat
- Improved `clean_text` input sanitization: tabs replaced with spaces, consecutive spaces and newlines collapsed independently without merging different whitespace types

### Added

- Utterance-level loudness normalization using ITU-R BS.1770-4 integrated LUFS measurement via `pyloudnorm`. Boost-only and asymmetric: quiet voices are lifted toward `target_lufs`, loud voices are left unchanged. Gain is capped by the 4x-oversampled true-peak measured with `scipy.signal.resample_poly` so the configured `true_peak_ceiling_db` is never exceeded. Controlled by four new `config.yaml` keys: `normalize_audio`, `target_lufs`, `true_peak_ceiling_db`, `min_duration_seconds`. A single `pyloudnorm.Meter` is constructed once per worker startup and reused for every utterance.
- Type stubs for `pyloudnorm` and `scipy.signal` under `stubs/` to satisfy pyright strict mode
- TTS engine core with streaming audio generation, playback, and WAV file saving using Voxtral models via mlx-audio
- Interactive CLI frontend with voice/model selection, raw terminal input, and background audio worker
- FastAPI TTS server with queued sequential playback, message status tracking, and automatic status eviction
- MCP server bridge for AI agent integration via Model Context Protocol
- `save_wav` config parameter to toggle WAV file saving on/off without impacting playback
- Interactive model download script with support for 4-bit, 6-bit, and bf16 quantizations
- `just download` target for manual model downloads; `just init` auto-triggers download when no model exists
- Justfile with build, run, serve, stop, status, and comprehensive CI recipes
- Unit tests and architecture import rule tests with 80% coverage threshold
- Load testing utility script for server benchmarking
- Application config with linter rules, static analysis (ruff, mypy, pyright, bandit, semgrep, deptry, codespell), and security scanning
- Type stubs for mlx_audio and sounddevice
- Project documentation (README, CLAUDE.md, QUICKSTART)
- MIT license, gitignore, and data directory scaffold

[Unreleased]: https://github.com/florianbuetow/tts-mcp/commits/main
