# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.10.0

- feat: Add a **Replay** button to each recent-message entry — re-queues that message with its recorded `text`, `voice`, and `engine`, so it sounds like the original rather than falling back to whatever the defaults are at replay time. `MessageStatus` and `StatusResponse` now carry the resolved `voice` (only `engine` and `sender` were tracked before), which is what makes a faithful replay possible. The replay is attributed as `replay · <sender>` so history stays honest about which entries were replays.
- feat: Show the last **25** finished messages instead of 10, via a named `RECENT_HISTORY_LIMIT`. Paired with the TTL change below, so the 25 are actually reachable.
- feat: Raise `STATUS_TTL_SECONDS` from 1 hour to 24, so a full working day of messages stays replayable. This is the real ceiling on Replay: `evict_expired()` drops a finished message's status once the TTL passes, and with it the text/voice/engine the Replay button needs — an evicted message has no entry and therefore no button. `MessageStatus` holds only short strings, so a chatty day costs a few hundred KB.
- fix: A disabled Pause/Skip button no longer looks pressable. At `opacity: .45` the accent blue still read as live and `cursor` stayed `default`, so clicking Pause while idle swallowed the click with no feedback — indistinguishable from a broken Pause (reported 2026-08-29). Disabled buttons now use the muted background and `cursor: not-allowed`. Pause/resume itself was verified working end-to-end and is unchanged.

## v0.9.0

### Added

- feat: speech can now be paused and resumed. `POST /pause` and `POST /resume` (plus the `pause`/`resume` MCP tools and `scripts/tts-pause`/`scripts/tts-resume`, aka `make pause`/`make resume`) hold the current utterance where it is and continue from the exact same point — no position bookkeeping, the player simply stops writing between its ~100ms slices. Pause never blocks cancel: a paused message stays cancellable, and a cancel lands within one slice even while paused. This closes the gap the skip-cancel work (v0.8.0) left open: stale narration could be skipped but not temporarily held, so a message that suddenly matters either played over the interruption or was lost entirely.
- feat: messages are now attributable. `POST /say` (and the `say` MCP tool) accept an optional `sender`, stored on the message and surfaced in `GET /status/{id}` and the new `GET /state`. The `voice` skill auto-tags every utterance with the session tag, so with several local and cloud sessions interleaving it is clear which session said what.
- feat: a `GET /state` endpoint reports what is playing/paused now, the recent message history (with sender), and the queue depth — the data behind both new surfaces.
- feat: a small web UI served at `/` on the server (open `http://127.0.0.1:12000/`) polls `/state` and shows the current status, message history with senders, and Pause/Resume/Skip buttons.
- feat: a Hammerspoon menu bar module (`~/.hammerspoon/tts.lua`) polls `/state` and offers a Pause/Resume toggle, the current/last message and sender, and a link to open the web UI — quick control without dropping into a terminal or waiting for an agent to be idle.

## v0.8.2

### Changed

- docs: the `voice` skill's `restart` procedure now distinguishes a dropped MCP tool binding from a stale audio device before restarting anything. Both present identically — voice simply stops — but only the device case is fixed by `launchctl kickstart`, and the old procedure assumed it: all four steps targeted the server. When the binding is what dropped, the restart "succeeds" (fresh process, `/health` returns `ok` in about a second) while `mcp__tts__say` is still missing, because the tool list belongs to the MCP client in the Claude Code session, not to the server process — so a green health check reads as a fix while the user still hears nothing. The skill now leads with a one-call triage (`No such tool available` → binding, restart Claude Code; a `message_id` returned but silence → device, restart the server) and documents the `POST /say` HTTP fallback that keeps speech working for the rest of a session whose binding has dropped. Observed 2026-08-22.

## v0.8.1

### Changed

- docs: the `voice` skill now forbids describing speech that was never sent. It already required a spoken gist for every substantive answer in `narrate`, and listed what must never be spoken aloud, but nothing covered the inverse failure — writing "spoken now", or narrating the utterance in the reply, while `mcp__tts__say` is never called. That reads correct on screen and is silent to a user who has walked away from it, which is exactly the case `narrate` exists for. The intent to speak gets written down instead of executed, so the reply looks right and nothing is heard. Observed in a live session and corrected by the user with "Always use the voice tool."

## v0.8.0

### Added

- feat: speech can now be skipped. `POST /cancel`, the `cancel` MCP tool, and `scripts/tts-skip` (aka `make skip`) stop the utterance that is playing and start the next queued message immediately; `all: true` also drops the queue behind it. Previously a long message had to be heard to the end — the audio worker had no interruption path at all, and the player's blocking per-chunk write meant even stopping the queue would not silence the current utterance. Playback now stops within ~100ms (audio is written in slices, and the cancel event also stops MLX generation so no GPU time is spent on audio nobody will hear), and a cancelled message saves no WAV. Cancelling a message that is still queued drops it before it is ever synthesized. Cancel events are owned per message, so a cancel that lands as one utterance ends can never silence the next one. `scripts/tts-skip` is a dependency-free curl wrapper meant for a global hotkey, so skipping does not wait for an agent to be idle enough to call the MCP tool.

### Changed

- refactor: the MCP relay's environment reads and logging moved out of `mcp/tts-mcp.ts` into `mcp/config.ts` and `mcp/logger.ts`. `process.env` was read at module scope, before anything could validate it, so a bad value surfaced as a confusing failure elsewhere; config is now resolved on demand and a missing key names the file it was expected in. Tool handlers log one JSON object per line to stderr instead of unstructured `console.error` text — stdout stays reserved for the MCP JSON-RPC frames, and connection failures are now logged rather than only returned to the caller.

## v0.7.0

### Added

- feat: the `voice` skill now requires every spoken message to carry a short tag naming its subject, placed after the mandatory throwaway lead word (`"Okay. ORB DE40 — closing posted."`). One server serves every Claude Code session from a shared queue, so with several sessions open their speech interleaved with nothing to distinguish them — an unlabeled utterance could not be attributed to a session, task, or repo. The tag is drawn from the session's anchor task, falling back to its parent goal or the repo in play, shortened to 2–4 distinctive words and reused verbatim for the whole session.

## v0.6.1

### Added

- chore: CI can now be triggered by hand via `workflow_dispatch`. Previously the workflow ran only on `push` and `pull_request`, so a run orphaned by a GitHub Actions incident could not be retried at all — `gh run rerun` rejects a run that is not `completed`, and the stuck run reported `queued` indefinitely while `gh run cancel` claimed it was already complete. The only remaining way to get a fresh result on `main` was an otherwise pointless commit.

## v0.6.0

### Added

- feat: per-request engine selection with lazy model loading. `config.yaml` gains an optional `engines:` mapping (with `default_engine:`) declaring several engines at once, and `POST /say` plus the `say` MCP tool gain an `engine` field. Only the default engine is loaded at startup; another engine's model loads the first time a request names it, so a config declaring both families costs one model of memory until someone actually uses the second. Previously the engine was fixed by a single `engine:` key bound at process start, and one server process serves every client — so choosing a backend meant editing config and restarting, dropping in-flight audio for everyone.
- feat: `GET /voices` reports per-engine detail alongside the existing flat `voices`/`default_voice` fields — each engine's voices, language, whether it supports `instruct`, whether its model is resident, and whether it is available at all. The flat `voices` list is now the union across available engines.
- feat: new `loading` message status, set while a request waits on its engine's model. Previously such a request reported `playing` while silent, since the status flipped at dequeue rather than at first audio.
- feat: `/engine` skill — picks which engine Claude speaks with for the rest of the session (`qwen3` / `voxtral` / `default` / `status`). This is the only part of engine choice that can be per-session: one server serves every client and caches each model, so the server itself is never "switched" — the `engine` field on each `say` is. Two sessions can therefore speak with different engines against one server. The skill also pairs the voice to the engine, since voice names do not overlap across families (`ryan` is qwen3-only, `casual_male` voxtral-only) and a mismatch is a 400.

### Changed

- feat: `instruct` paired with an engine that does not support it is now rejected with a 400 at request time. It previously returned 202 and failed inside the worker — after the caller had stopped looking — and would pointlessly load the model first.
- fix: a failed lazy engine load no longer kills the audio worker. The startup load path signals `ready_queue` and stops the worker on failure, which is right for a broken default engine but would have let one unreachable non-default engine brick playback for every session. The two paths are now distinct: lazy failures are recorded on the item and the engine, and the worker keeps serving.
- fix: a failed model load is cached rather than retried. Repeating a load that takes ~20 seconds to fail on every request is worse than surfacing the original error immediately; recovery is a server restart.

### Notes

- The single-engine `engine:`/`model:`/`language:` form keeps working unchanged. Declaring both forms at once is an error rather than one silently taking precedence.
- No eviction: resident models are never dropped. Streaming hands a live generator to the playback path, so releasing a model mid-stream is a use-after-free-shaped bug, and freeing MLX buffers from the wrong thread reintroduces the thread-affinity problem the single-worker design exists to avoid.
- `sample_rate` stays global. A per-engine value is accepted only if it matches; the output stream and loudness meter are built once per worker, so a differing rate would pitch-shift audio and mis-meter loudness.

## v0.5.1

### Fixed

- fix: `/voice` now declares `argument-hint` frontmatter, so the CLI shows `[on|narrate|interview|off|status|restart]` while typing. The skill has always taken args and the modes were listed in the description, but without the dedicated field the picker rendered no hint — leaving the six modes discoverable only by opening SKILL.md.
- fix: the skill's `description` is now YAML-quoted. It ends with `Args: on | narrate | …`, and a bare `": "` inside an unquoted plain scalar is invalid YAML — `yaml.safe_load` rejected the whole frontmatter block with "mapping values are not allowed here". Claude Code's own parser is lenient enough to load it anyway, so the breakage was invisible in normal use, but any strict-YAML consumer (linter, audit tooling, generator) choked on the file. Present since the description first gained an `Args:` suffix.

## v0.5.0

### Added

- feat: `narrate` mode — `/voice narrate` speaks a 1–3 sentence gist of every substantive answer on top of the attention signals `on` already covers. Fills the gap that made `/voice on … explain X` land the explanation on screen only: the user turned voice on precisely because they wanted the answer in their ears. Never speaks the reply verbatim; the screen still carries the detail.

### Changed

- feat: the skill is now the **sole authority on spoken-output volume**. The rationale and prohibitions previously carried by an always-on `~/.claude/CLAUDE.md` rule (screen is the detail channel, voice the attention channel; ~150 wpm playback; no acks, no tool-call chatter, no reading code aloud) now live in a `## How much to speak` section with a per-mode capability table. Two sources of truth meant the memory rule silently outranked whatever mode the user had just selected.
- docs: voice is off until `/voice` is invoked — there is no always-on default any more. Documented in the persistence note, along with how to reinstate one and what that costs.

## v0.4.0

### Changed

- feat: `/voice on` and `/voice interview` now speak a one-line confirmation ("Okay. Voice mode activated.") via `mcp__tts__say`. v0.3.0 removed the activation handshake wholesale, which also removed any audible sign that the mode took — activation went completely silent and looked broken. The utterance is fire-and-forget: no "did you hear it?" gate, no `get_status` poll, no blocking. `/voice off` and `/voice status` stay silent; proof-of-audio still lives in `/tts-mcp:voice-selfcheck`.
- docs: the speaking playbook now states that spoken output is **always English**, regardless of the language the user writes in, and that the input language never selects a different voice. The playbook previously covered voice, pacing, and markup but left output language implicit, so a German prompt could drift the spoken line into German while the on-screen text stayed English.

## v0.3.1

- chore: opt this fork into auto-release via `release.allowFork` in `.maintainer.yaml`. `github-release-watcher` previously dropped forked repos during repo listing — upstream of the `.maintainer.yaml` trust gate — so this repo's `autoRelease: true` was never read and v0.3.0 had to be tagged by hand. Fixed in watcher v0.3.1 (`maintainer` v0.48.0 adds the `allowFork` field, `github-releaser-agent` v0.3.2 accepts it under strict config parsing); forks now reach the filter chain and release only with this explicit opt-in. This release is itself the end-to-end proof: it is the first tag on this repo cut automatically rather than by hand.

## v0.3.0

### Added

- Pluggable TTS engines. A new required `engine:` key in `config.yaml` selects the model family: `voxtral` (Mistral Voxtral 4B, 20 voices, 9 languages) or `qwen3` (Qwen3-TTS CustomVoice, 9 named speakers, 10 languages, free-text emotion control). `src/tts/engine.py` holds the `TTSEngine` protocol plus `VoxtralEngine` and `Qwen3Engine`, which encapsulate the two differences between the families: how voices are discovered, and how generation is invoked (`generate(voice=…)` versus `generate_custom_voice(speaker=…, language=…, instruct=…)`). The worker, normalization, and playback layers are untouched — both families stream, so nothing below the engine seam needed to change.
- `POST /say` and the MCP `say` tool accept an optional `instruct` field carrying a free-text emotion/style instruction (e.g. `"Very happy and excited."`). Supported by the `qwen3` engine; the `voxtral` engine fails the request rather than silently ignoring it, per the project's no-silent-fallback rule.
- `language:` config key, required by the `qwen3` engine and rejected by `voxtral`. Qwen3 declares 10 languages plus 2 dialects in its `config.json`, including German.
- `scripts/download-model.sh` offers the two Qwen3-TTS CustomVoice quantizations (8-bit ~3.1 GB, 4-bit ~1.8 GB) alongside the three Voxtral variants, and prints the matching `engine:` / `language:` / `default_voice:` lines to paste into `config.yaml`.
- `tests/test_engine.py` covers engine construction and config validation, Qwen3 speaker discovery from `config.json`, per-family generation dispatch, and cross-family model rejection.

### Changed

- Qwen3 speakers are read from `talker_config.spk_id` in the model's `config.json` rather than from a loaded model instance. This keeps voice discovery on the same pre-load, filesystem-based path the Voxtral engine already used, so server startup ordering is unchanged (the MLX model is still loaded lazily on the audio worker thread, since MLX GPU streams are thread-local).
- `discover_voices()` moved off `src/tts/config.py` onto the engines; `generate_chunks()`, `iter_stream_chunks()`, `streaming_chunk_iter()`, `audio_worker()`, and `audio_worker_from_model_id()` all take an engine as their first or second argument.
- Model-capability validation moved from an inline `hasattr(model, "generate")` check in `src/server.py` and `src/tts/worker.py` to `TTSEngine.validate_model()`, so a Qwen3 model is no longer rejected for lacking a Voxtral-style `generate`.
- fix: the `/voice` skill and `/tts-mcp:voice-selfcheck` command now name `ryan` (a Qwen3-TTS CustomVoice speaker) as the standing voice instead of `casual_male` (Voxtral). Voice names are engine-specific, so a Voxtral name is rejected with HTTP 400 once the server runs `engine: qwen3`; both docs now point at `get_voices` when a voice is refused.

### Removed

- `generate_speech(model_id, text, voice)` — an exported helper with no callers outside its own tests, which loaded a model per call and hardcoded the Voxtral call style. Removing it avoided plumbing an engine through dead code. This is a breaking change to the `src.tts` package API; the server and CLI never used it.

## v0.2.0

### Added

- `commands/voice-selfcheck.md` ships a `/tts-mcp:voice-selfcheck` slash command that speaks a test line, asks the user to confirm they heard it, and troubleshoots the audio path on failure (server reachability via `get_voices`, queue state via `get_status`, retry, restart). This is the extracted startup handshake, now invoked deliberately instead of on every activation. Named `voice-selfcheck` rather than `voice-test` because `test` is already taken twice in this repo — `make test` is pytest, and `test` is the required CI status check — so `voice-test` read like "run the repo's tests".

### Fixed

- The wheel shipped no code. `[tool.hatch.build.targets.wheel] packages` pointed at `src/mistral_text_to_spech/`, a leftover `bborbe/python-skeleton` stub whose only file was an unrendered `"""{{project_name}} package."""` placeholder — so the built wheel contained that empty package and none of `src/server.py`, `src/main.py`, or `src/tts/`. Deleted the stub and set `packages = ["src"]`, which matches how the code is actually imported (`src.tts`, `src.server`). Verified: `uv build --wheel` now produces a wheel containing all 12 modules. (`src` as a distribution package name is poor hygiene — the real fix is an `src/<package>/` layout with non-`src.` imports — but this makes the declaration truthful without a 46-reference rename.)
- The pytestarch architecture tests enforced nothing. `tests/architecture/conftest.py` built the graph over that same empty stub directory, and the single test consuming the fixture only asserted `evaluable is not None` — so the dependency existed to check that a graph over an empty package was non-null. Pointed it at `src/` and replaced the vacuous assertion with a real layering rule: no module under `src.tts` (the shared engine) may import `src.server` or `src.main` (its consumers), which the hand-rolled per-file `ast` tests don't cover since they only check `server` ↔ `main`. Confirmed the rule fails on an injected `import src.server` in `src/tts/config.py` (`AssertionError: "src.tts.config" imports "src.server"`), not merely that it passes.

- Bumped the transitive dependency tree in `mcp/` (`npm update`, lockfile-only — no `package.json` change), clearing 6 of 8 npm-audit findings: 2 high (`fast-uri`), 4 moderate (`hono`, `qs`, `ip-address`, `express-rate-limit`), 2 low (`esbuild`, `body-parser`). Verified with `npx tsc --noEmit` and an MCP `initialize` handshake against the relay. Supersedes Dependabot PRs #4–#9, which proposed the same bumps individually and would have needed six serial lockfile rebases.
- The two remaining advisories are `@hono/node-server <2.0.5` (path traversal in `serve-static` via encoded backslash) reached through `@modelcontextprotocol/sdk >=1.25.0`. Left unfixed deliberately: `npm audit fix --force` resolves it only by downgrading the SDK to 1.24.3 (breaking), and the advisory is Windows-specific `serve-static` behaviour while this relay is macOS-only stdio-to-localhost-HTTP and serves no static files.

### Changed

- Bumped the plugin version to `0.2.0` in both `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`. The installed-plugin cache is keyed by that version (`~/.claude/plugins/cache/tts-mcp/tts-mcp/<version>/`), so leaving it at `0.1.0` meant `make update` kept serving the stale `0.1.0` tree — the merged `/voice` change never reached the local install, and the cached copy had no `commands/` directory at all. The release flow rewrites the CHANGELOG and tags but does not touch these two files, so the bump has to be explicit.

- `make check` now also runs `mcp-typecheck` (`npm ci && npx tsc --noEmit` in `mcp/`), and CI pins Node 22 via `actions/setup-node@v4` to support it. The required `test` status check ran `make precommit` — entirely Python (ruff/mypy/pyright/pytest) — so it was blind to every change under `mcp/`: the six Dependabot PRs touching the TypeScript relay all reported green without anything having compiled it. Verified the gate fails on a deliberate type error, not just that it passes.

- `/tts-mcp:voice on` and `interview` no longer run a startup selftest — they flip the mode and report it. The handshake cost a speak-and-confirm round-trip on every activation while the channel was healthy in practice; verification moved to the dedicated `/tts-mcp:voice-selfcheck` command, which `restart` now points at.

## v0.1.0

### Changed

- Documented the fork lineage: `LICENSE` now carries both copyright holders (© 2025 Florian Butow, © 2026 Benjamin Borbe) and the README notes this is an independently-maintained derivative of `florianbuetow/tts-mcp` (MIT).
- Collapsed the worker's parameter bloat into an `AudioSettings` frozen dataclass (sample rate, lead silence, normalization params, meter, streaming params). `audio_worker` / `audio_worker_from_model_id` / `streaming_chunk_iter` now take `(…, settings)` instead of 10–14 positional args; the CLI builds it from `CliConfig` and the server from `ServerState.audio_settings()`. No behaviour change.
- Split the `src/tts.py` god-module (~1300 LOC) into a `src/tts/` package with focused submodules — `text`, `config`, `protocols`, `generate`, `normalize`, `device` (CoreAudio HAL), `player` (sounddevice playback), `worker` — re-exporting the same public API from `__init__.py`, so `server.py`/`main.py` and their imports are unchanged. Behaviour is identical (no logic changes); the largest module is now ~360 LOC. Test mock targets were updated to patch the submodule where each dependency is used (`src.tts.player.sd`, `src.tts.generate.load`).
- Conformed the build tooling to the standard `bborbe/python-skeleton` layout: replaced the `justfile` with a `Makefile` (`+ Makefile.variables`, `Makefile.precommit`) exposing `sync` / `run` / `chat` / `download` / `format` / `lint` / `typecheck` / `check` / `test` / `precommit`. The toolchain is now lean — ruff, mypy, pyright, and pytest (including the `tests/architecture/` import-rule tests) — dropping semgrep, bandit, deptry, codespell, pip-audit, and pygount. This also resolved the dependency vulnerabilities: removing semgrep (which hard-pinned `click~=8.1.8` and `mcp==1.23.3`) let `click` float to a patched release and dropped the vulnerable `mcp` entirely, and `pillow` was bumped to 12.3.0 — cutting the locked dependency set from 152 to 84 packages.

### Added

- `.maintainer.yaml` opting into the maintainer-bot flow (`release.autoRelease: true`, `prReviewer.autoApprove: true`), matching the `bborbe/vault-cli` setup: the `github-releaser-agent` watcher now auto-cuts releases from the `## Unreleased` block and auto-approves PRs. De-bracketed the changelog's `## [Unreleased]` → `## Unreleased` so the bot and `/coding:commit` detect the block.
- Claude Code plugin packaging: `.claude-plugin/{plugin.json,marketplace.json}` expose this repo as an installable marketplace (`tts-mcp@tts-mcp`), and `skills/voice/SKILL.md` ships the `/voice` skill (session TTS toggle: `on` / `interview` / `off` / `status` / `restart`, plus the spoken-output playbook and TTS-server restart/troubleshooting steps) previously kept loose in `~/.claude/skills/voice`. Install with `/plugin marketplace add bborbe/tts-mcp` then `/plugin install tts-mcp@tts-mcp`.
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
