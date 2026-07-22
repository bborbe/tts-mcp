---
name: voice
description: Manage TTS voice mode for the current session and apply the spoken-output playbook. Use when the user types /voice, asks to turn voice on/off, wants questions read aloud (interview mode), restart/fix the TTS server after audio goes silent (e.g. switching to AirPods), or asks how the voice should behave. Args: on | off | status | interview | restart.
---

## What this does

Controls whether Claude speaks via `mcp__tts__say` this session, and how. The persistent default lives in `~/.claude/CLAUDE.md` ("Voice: Attention Signals Only … casual_male"); this skill is the **session-scoped** toggle plus the speaking playbook. A skill cannot force behavior across turns on its own — it sets the mode for the rest of this session and reminds Claude of the rules.

## Args

- `on` (default) — enable **attention-signal** voice: speak completion/failure events, decisions, and `AskUserQuestion` prompts. Matches the CLAUDE.md default.
- `interview` — stronger mode: also speak **every question that needs the user's input**, one at a time (used when the user is away from the keyboard / driving by voice).
- `off` — disable: stop calling `mcp__tts__say` for the rest of the session. For a **permanent** disable, relax the "Voice: Attention Signals Only" line in `~/.claude/CLAUDE.md`.
- `status` — report the current mode and voice.
- `restart` (alias `fix`) — restart the TTS server. Use when audio goes silent after switching the Mac's output device (AirPods, headphones): the server binds the default output device once at process init, so a device switch leaves it playing into the void. Runs `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp`, waits for `/health`, then re-handshakes with a test line. See the Restart section below.

On invocation, confirm the new mode in one line (e.g. `🔊 voice: interview (casual_male)` or `🔇 voice: off`).

## Startup handshake (on `on` / `interview`)

Audio silently failing is the worst case — the user walks away trusting voice, and never gets alerted. So **the first thing `on`/`interview` does is prove the channel works**, before relying on it:

1. **Speak a test line immediately** via `mcp__tts__say` (voice `casual_male`), e.g. `"Okay, hello — voice is on. Did you hear this?"` (lead throwaway word per the playbook).
2. **Ask the user to confirm** in the on-screen reply: "Did you hear it? (y / no)".
3. **On "no" (or silence + a follow-up "didn't hear it"):** the audio path is broken — do NOT keep speaking into the void. Troubleshoot, in order:
   - `mcp__tts__get_voices` — is the TTS server reachable at all? (errors → server down)
   - `mcp__tts__get_status` with the test `message_id` — did it reach `playing`/`completed`, or stick at `queued`/`error`? `queued` forever = playback worker wedged; `error` = synth/device failure (read the error field).
   - Re-send one test line (transient queue hiccup often clears on retry).
   - If still silent: report the specific failure (server unreachable / stuck queue / device error) and fall back to **on-screen only** for the session — tell the user voice is unavailable so they don't rely on it. Don't silently pretend it works.
4. **On "y":** proceed in the chosen mode.

Skip the handshake for `off` and `status` (nothing to prove). Re-running `/voice on` mid-session re-handshakes — cheap way to re-test after audio flakes.

## Restart (`restart` / `fix`)

Use when audio silently stops after a device switch (AirPods connect, headphones unplug). The server is a launchd-supervised FastAPI process (`com.bborbe.tts-mcp`) that binds the default output device at init; a switch orphans it.

1. `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp` (KeepAlive respawns a fresh process against the current default device).
2. Poll health until ready (model reload takes ~15–20s; `/health` returns `ok` *before* the model is loaded, so also allow the first `say` to lag): `curl -s http://127.0.0.1:12000/health`.
3. Re-handshake: speak a test line and ask "Did you hear it? (y / no)".
4. If still silent after restart, it's not the device binding — fall through to the startup-handshake troubleshooting (server unreachable / stuck queue / synth error).

Caveats: in-flight messages are dropped across a restart; message IDs reset; one server serves all Claude sessions, so a restart affects every session's relay.

## Speaking playbook (how to speak — not how to write on screen)

Speech is a different channel from the terminal text. When you call `mcp__tts__say`:

- **Voice `casual_male`, always.**
- **Lead with a throwaway word.** CoreAudio clips the first ~word of each utterance. Start every spoken message with a disposable lead token — `"Okay."`, `"So,"`, `"Right,"` — so the clip eats that, not the real first word. Never let a content word be first.
- **Terse.** One idea per sentence. This is a nudge to attention, not a recital of the on-screen text — never read a whole reply aloud.
- **No markup in speech.** No markdown, URLs, file paths, code, or backticks — describe them in words ("the controller Makefile", not "`Makefile.k8s`").
- **Lead with the recommendation and say the word "recommended."** (Standing user rule.)
- **Spell choices out loud:** "option one … option two …" and end with "say one or two."
- **Numbers/IDs:** say them naturally; don't spell long hashes/URLs.

## When to trigger

Speak on:
- A **question that needs the user's input** (in `interview` mode: every one; otherwise: `AskUserQuestion`-level decisions).
- **Completion / failure** of background or long-running work.
- A **decision point** where you're waiting on the user.

Do **not** speak: routine narration, tool-call chatter, or anything the user is clearly watching happen on screen.

## Persistence note

- Session toggle → this skill (`on` / `interview` / `off`).
- Persistent default → `~/.claude/CLAUDE.md` (the always-on rule).
- Mechanical enforcement (e.g. a hook that always speaks on Stop) → `settings.json`; use the `update-config` skill if you want that.
