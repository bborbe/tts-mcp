---
name: voice
description: 'Manage TTS voice mode for the current session and apply the spoken-output playbook. Use when the user types /voice, asks to turn voice on/off, wants answers spoken aloud (narrate mode) or questions read aloud (interview mode), restart/fix the TTS server after audio goes silent (e.g. switching to AirPods), or asks how much the voice should say. Sole authority on spoken-output volume — no always-on rule competes with it. Activation speaks a one-line confirmation but runs no selftest — use /tts-mcp:voice-selfcheck to verify the audio path. Args: on | narrate | interview | off | status | restart.'
argument-hint: "[on|narrate|interview|off|status|restart]"
---

## What this does

Controls whether Claude speaks via `mcp__tts__say` this session, and how much. **This skill is the sole authority on spoken-output volume** — there is no competing always-on rule in `~/.claude/CLAUDE.md`. Voice is off until someone invokes `/voice`; the chosen arg sets the level for the rest of the session.

A skill cannot force behavior across turns on its own — it sets the mode for the rest of this session and reminds Claude of the rules.

## Args

- `on` (default) — **attention-signal** voice: speak completion/failure events, decisions, and `AskUserQuestion` prompts. Substantive answers stay on screen.
- `narrate` — `on` **plus a spoken gist of every substantive answer**: 1–3 sentences of headline, never the reply verbatim. For when the user is away from the screen but still wants to follow the work.
- `interview` — `on` **plus every question that needs the user's input**, one at a time (used when the user is away from the keyboard / driving by voice).
- `off` — disable: stop calling `mcp__tts__say` for the rest of the session.
- `status` — report the current mode and voice.
- `restart` (alias `fix`) — restart the TTS server. Use when audio goes silent after switching the Mac's output device (AirPods, headphones): the server binds the default output device once at process init, so a device switch leaves it playing into the void. Runs `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp`, waits for `/health`, then verifies via `/tts-mcp:voice-selfcheck`. See the Restart section below.

On invocation, confirm the new mode in one line (e.g. `🔊 voice: interview (ryan)` or `🔇 voice: off`).

## Spoken confirmation on activation

`on` / `narrate` / `interview` **also speak the confirmation** via `mcp__tts__say` (voice `ryan`) — the first thing the new mode does is use itself:

- `on` → `"Okay. Voice mode activated."`
- `narrate` → `"Okay. Narrate mode activated."`
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
- **English, always.** Speak English even when the user writes German (or any other language) — the input language never switches the spoken output. This matches the on-screen "English Only" rule; voice is not an exception to it. Do not switch voices to match an input language either. Proper nouns keep their native spelling; everything around them stays English.
- **Lead with a throwaway word.** CoreAudio clips the first ~word of each utterance. Start every spoken message with a disposable lead token — `"Okay."`, `"So,"`, `"Right,"` — so the clip eats that, not the real first word. Never let a content word be first.
- **Terse.** One idea per sentence. This is a nudge to attention, not a recital of the on-screen text — never read a whole reply aloud.
- **No markup in speech.** No markdown, URLs, file paths, code, or backticks — describe them in words ("the controller Makefile", not "`Makefile.k8s`").
- **Lead with the recommendation and say the word "recommended."** (Standing user rule.)
- **Spell choices out loud:** "option one … option two …" and end with "say one or two."
- **Numbers/IDs:** say them naturally; don't spell long hashes/URLs.

## How much to speak

The screen is the detail channel; voice is the attention channel. A `say()` costs ~2–5s and plays at ~150 wpm — slower than the user can read. That's the whole reason volume is a *setting* and not a constant: speaking everything is exhausting, speaking nothing defeats walking away from the screen.

| Mode | Attention signals | Answers | Every question |
|---|---|---|---|
| `on` | ✅ | ❌ screen only | ❌ decisions only |
| `narrate` | ✅ | ✅ 1–3 sentence gist | ❌ decisions only |
| `interview` | ✅ | ❌ screen only | ✅ one at a time |
| `off` | ❌ | ❌ | ❌ |

**Attention signals** (spoken in every mode except `off`):

- **Completion / failure** of background or long-running work — `say("PR 42 merged.")`, `say("Build failed in capitalcom-gateway.")`.
- A **decision point** where you're waiting on the user — `say("Needs your input.")`.

**Never speak, in any mode:**

- Routine acks ("ok", "done") after something the user just watched happen in 0.3s.
- Tool-call chatter ("Let me check that file.").
- Code, long lists, or file dumps — voice is the wrong shape for them.
- The reply verbatim. Even in `narrate`, speak the headline and let the screen carry the detail; never let voice and text be the same content at the same length.
- Anything at all when the user is clearly sitting there watching it scroll by.

`narrate` is the mode to reach for when the user asks a question *and* turns voice on in the same breath — that pairing means they want the answer in their ears, not just on screen.

## Persistence note

- Session toggle **and volume policy** → this skill (`on` / `narrate` / `interview` / `off`). Nothing else owns this.
- No always-on default. Voice stays silent until `/voice` is invoked — deliberate, so a session is never noisy without someone asking for it. If you want a persistent default back, add a rule to `~/.claude/CLAUDE.md` naming the mode, but then this file stops being the single source of truth.
- Mechanical enforcement (e.g. a hook that always speaks on Stop) → `settings.json`; use the `update-config` skill if you want that.
