---
description: Verify the TTS audio path end-to-end — speak a test line, confirm the user heard it, troubleshoot on failure.
---

Prove that spoken output actually reaches the user's ears. Run this when you need certainty that voice works — not on every `/tts-mcp:on` (activation assumes a working channel).

Use when:

- Audio went silent or sounds wrong mid-session.
- The output device changed (AirPods connected, headphones unplugged) — usually after `/tts-mcp:restart`.
- The user is about to walk away and rely on voice alerts for background work.

## Steps

1. **Speak a test line** via `mcp__tts__say`, voice `ryan`, e.g. `"Okay, hello — this is a voice test. Did you hear this?"` (lead throwaway word per the playbook — CoreAudio clips the first word).
2. **Ask on screen:** "Did you hear it? (y / no)".
3. **On `y`:** report `🔊 voice channel OK (ryan)` and stop.
4. **On `no`** (or a follow-up "didn't hear it"): the audio path is broken — do NOT keep speaking into the void. Troubleshoot in order:
   - `mcp__tts__get_voices` — is the server reachable at all? Errors → server down.
   - `mcp__tts__get_status` with the test `message_id` — did it reach `playing` / `completed`, or stick at `queued` / `error`? `queued` forever = playback worker wedged; `error` = synth or device failure (read the error field).
   - Re-send one test line — a transient queue hiccup often clears on retry.
   - Still silent → **check which case this is before restarting.** `/tts-mcp:restart` carries the three-way table (dropped binding / wedged relay / stale device); only the last is fixed by a restart, and restarting can *cause* the wedged-relay case. Then re-run this test.
5. **If it still fails after a restart:** report the specific failure (server unreachable / stuck queue / device error), and tell the user voice is unavailable so they don't rely on it. Fall back to on-screen only for the session. Never silently pretend it works.
