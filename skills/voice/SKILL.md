---
name: voice
description: Manage TTS voice mode for the current session and apply the spoken-output playbook. Use when the user types /voice, asks to turn voice on/off, wants questions read aloud (interview mode), restart/fix the TTS server after audio goes silent (e.g. switching to AirPods), or asks how the voice should behave. Activation speaks a one-line confirmation but runs no selftest — use /tts-mcp:voice-selfcheck to verify the audio path. Args: on | off | status | interview | restart.
---

## What this does

Controls whether Claude speaks via `mcp__tts__say` this session, and how. The persistent default lives in `~/.claude/CLAUDE.md` ("Voice Attention Only … `ryan`"); this skill is the **session-scoped** toggle plus the speaking playbook. A skill cannot force behavior across turns on its own — it sets the mode for the rest of this session and reminds Claude of the rules.

## Args

- `on` (default) — enable **attention-signal** voice: speak completion/failure events, decisions, and `AskUserQuestion` prompts. Matches the CLAUDE.md default.
- `interview` — stronger mode: also speak **every question that needs the user's input**, one at a time (used when the user is away from the keyboard / driving by voice).
- `off` — disable: stop calling `mcp__tts__say` for the rest of the session. For a **permanent** disable, relax the "Voice: Attention Signals Only" line in `~/.claude/CLAUDE.md`.
- `status` — report the current mode and voice.
- `restart` (alias `fix`) — restart the TTS server. Use when audio goes silent after switching the Mac's output device (AirPods, headphones): the server binds the default output device once at process init, so a device switch leaves it playing into the void. Runs `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp`, waits for `/health`, then verifies via `/tts-mcp:voice-selfcheck`. See the Restart section below.

On invocation, confirm the new mode in one line (e.g. `🔊 voice: interview (ryan)` or `🔇 voice: off`).

## Spoken confirmation on activation

`on` / `interview` **also speak the confirmation** via `mcp__tts__say` (voice `ryan`) — the first thing the new mode does is use itself:

- `on` → `"Okay. Voice mode activated."`
- `interview` → `"Okay. Interview mode activated."`

Fire-and-forget: say it, print the one-line status, done. Do **not** ask "did you hear it?", do not poll `get_status`, do not block on the result. If the user hears nothing, they say so and you fall through to `/tts-mcp:voice-selfcheck` — but activation itself never gates on the answer.

Skip the utterance for `off` and `status` (speaking is exactly what `off` is turning off).

## No selftest on activation

The spoken line above is a courtesy signal, **not** a selftest — no confirmation gate, no round-trip. The channel is assumed healthy; in practice it is, and the old handshake cost a reply on every activation.

When you actually need proof the audio path works — silence mid-session, a device switch, or the user about to walk away and rely on voice alerts — run `/tts-mcp:voice-selfcheck`, which speaks a test line, confirms, and troubleshoots on failure.

## Restart (`restart` / `fix`)

Use when audio silently stops after a device switch (AirPods connect, headphones unplug). The server is a launchd-supervised FastAPI process (`com.bborbe.tts-mcp`) that binds the default output device at init; a switch orphans it.

1. `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp` (KeepAlive respawns a fresh process against the current default device).
2. Poll health until ready — `curl -s http://127.0.0.1:12000/health`. `/health` returns `ok` *before* the model is loaded, so also allow the first `say` to lag. Model reload is ~1-3s on `engine: qwen3`, ~15-20s on `engine: voxtral`.
3. Verify: run `/tts-mcp:voice-selfcheck`.
4. If still silent after restart, it's not the device binding — follow the troubleshooting steps in `/tts-mcp:voice-selfcheck` (server unreachable / stuck queue / synth error).

Caveats: in-flight messages are dropped across a restart; message IDs reset; one server serves all Claude sessions, so a restart affects every session's relay.

## Speaking playbook (how to speak — not how to write on screen)

Speech is a different channel from the terminal text. When you call `mcp__tts__say`:

- **Voice `ryan`, always.** (`ryan` is a Qwen3-TTS CustomVoice speaker. If the server runs `engine: voxtral`, its voices are Voxtral names such as `casual_male` instead — check `get_voices` when a voice is rejected.)
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
