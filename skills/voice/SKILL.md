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

## Skipping, pausing, resuming

"Skip", "stop talking", "next", "cut it short" mean the current utterance, not voice mode — call `mcp__tts__cancel`
with no arguments. Playback stops in ~100ms and the next queued message starts. Add `all: true` when the user wants the
whole backlog gone ("shut up", "stop all of it"), and pass `message_id` only when a specific message was named. Do not
turn voice `off` for a skip: the user is rejecting one utterance, not the mode.

"Pause" / "hold that" / "stop talking but don't lose it" mean `mcp__tts__pause` — playback stops in ~100ms and resumes
from the same point on `mcp__tts__resume`. A paused message stays cancellable. Pause is for "interrupt me but keep the
rest of this utterance" — a meeting, a question — where skip would throw away audio the user still wants.

An MCP call only lands when Claude is between tool calls, so it is the slow path. When the user complains that skipping
arrives too late, point them at `scripts/tts-skip` in the tts-mcp repo (`make skip`) — one curl, bindable to a global
hotkey (Raycast, macOS Shortcuts, skhd), which works no matter what any session is doing. `scripts/tts-pause` /
`scripts/tts-resume` (`make pause` / `make resume`) are the same one-curl wrappers for pause/resume.

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

### First: is it the server, or the tool binding?

Silence has two causes that look identical from the user's side, and **only one of them is fixed by restarting the server**. Check this before running any of the steps below — it takes one call and saves restarting a process that was never broken.

| What you observe | Cause | Fix |
|---|---|---|
| `mcp__tts__say` errors with `No such tool available` | The session's **MCP tool binding** dropped. The server is almost certainly fine. | Restart Claude Code, or start a new session. **No HTTP fallback** — a missing binding is final for the session. |
| `mcp__tts__say` **succeeds** (returns a `message_id`) but nothing is audible | The server bound a **stale audio device**. | The restart steps below. |

A dropped binding does not heal on `launchctl kickstart` — the server respawns healthy, `/health` returns `ok`, and the tool is still missing, because the tool list is owned by the MCP client in the Claude Code session, not by the server process. Restarting into a green health check and declaring victory is the trap here: the check passes and the user still hears nothing.

**No HTTP fallback — MCP or nothing.** The server exposes the same endpoint over HTTP, but this skill deliberately never reaches it as a fallback. A missing `mcp__tts__say` is the session's MCP config in force — and in a Discord-answered session the tts server is removed from the tool set on purpose (`--strict-mcp-config`), so a "dropped binding" there is the guard working, not a fault. Calling the HTTP endpoint routes around exactly that guard: the reply is already spoken into the call by the assistant itself, so the fallback only adds a duplicate voice on the laptop speakers. Observed 2026-09-03 in a live Discord call — the HTTP fallback double-spoke every answer. A dropped binding is final for the session: restart Claude Code, or start a new session.

### Restart steps (stale-device case)

1. `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp` (KeepAlive respawns a fresh process against the current default device).
2. Poll health until ready — `curl -s http://127.0.0.1:12000/health`. `/health` returns `ok` *before* the model is loaded, so also allow the first `say` to lag. Model reload is ~1-3s on `engine: qwen3`, ~15-20s on `engine: voxtral`.
3. Verify: run `/tts-mcp:voice-selfcheck`.
4. If still silent after restart, it's not the device binding — follow the troubleshooting steps in `/tts-mcp:voice-selfcheck` (server unreachable / stuck queue / synth error).

If `/health` answers but you cannot call `mcp__tts__say` at all, stop restarting — re-read the table above; that is the binding case, not a server fault.

Caveats: in-flight messages are dropped across a restart; message IDs reset; one server serves all Claude sessions, so a restart affects every session's relay.

## Speaking playbook (how to speak — not how to write on screen)

Speech is a different channel from the terminal text. When you call `mcp__tts__say`:

- **Voice `ryan`, always** — unless `/tts-mcp:engine` selected a non-qwen3 engine, which overrides this. (`ryan` is a Qwen3-TTS CustomVoice speaker and exists *only* on qwen3; voxtral's voices are separate names such as `casual_male`. Voice names do not overlap across engines and the server rejects a mismatch with a 400, so voice and engine must be chosen together — see the `engine` skill. Check `get_voices` when a voice is rejected.)
- **Pass `sender` on every call, no exceptions.** `mcp__tts__say` takes a `sender` argument — set it to the same short tag you speak (anchor task → parent goal → repo/service), verbatim. Never omit it, never send the session id or a full task title. One server serves every session, so an utterance with no `sender` is unattributable in the web UI (http://127.0.0.1:12000/) and in `GET /state` — which is exactly what the field exists to prevent. No anchor tag? Send the same short description of the work you speak (`"harness config"`, `"inbox triage"`). A call without `sender` is a bug, not a shortcut.
- **English, always.** Speak English even when the user writes German (or any other language) — the input language never switches the spoken output. This matches the on-screen "English Only" rule; voice is not an exception to it. Do not switch voices to match an input language either. Proper nouns keep their native spelling; everything around them stays English.
- **Lead with a throwaway word.** CoreAudio clips the first ~word of each utterance. Start every spoken message with a disposable lead token — `"Okay."`, `"So,"`, `"Right,"` — so the clip eats that, not the real first word. Never let a content word be first.
- **Name the subject, every utterance.** One server serves every Claude session from a shared queue, so with several sessions open their speech interleaves with nothing to tell them apart — an unlabeled utterance is noise. Every spoken message carries a short tag naming what it is about:

    ```
    throwaway lead + tag + content
    "Okay. ORB DE40 — closing posted for next week."
    "So, vault UI — build failed."
    ```

    - **Tag source, in order:** the session's anchor task (the `📌 Task:` line in the closer panel) → its parent goal → the repo or service being worked on. Never invent one.
    - **Shorten it.** 2–4 words, the distinctive part only — `"ORB DE40"`, not `"ORB DE40 W32 Sunday Review and Extend Closing to W33"`. The tag is for telling sessions apart, not for restating the title.
    - **Same tag for the whole session.** Pick it once and reuse it verbatim, so the user learns to recognize it by ear.
    - **Order is fixed:** the tag comes *after* the throwaway word, never first — the clipping rule above eats whatever leads, and a clipped tag is worse than none.
    - **No anchor task?** Use a short description of the work (`"harness config"`, `"inbox triage"`). Never skip the tag — an untagged utterance is exactly the failure this rule exists to prevent.

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

**Never describe speech you did not send.** Writing "spoken now", "I'll say that aloud", or narrating the utterance in your reply is not the channel — only `mcp__tts__say` is. If the tool was not called the user hears nothing, and text claiming otherwise is simply false. Call the tool, or say nothing about speaking. The failure is easy to miss because the reply *reads* correct: the intent to speak gets written down instead of executed, most often on a substantive answer in `narrate` where the spoken gist is owed but the turn ends with text alone.

## Persistence note

- Session toggle **and volume policy** → this skill (`on` / `narrate` / `interview` / `off`). Nothing else owns this.
- Which engine (and therefore which voice) → `/tts-mcp:engine`. That is the other genuinely session-scoped setting: the server holds every declared engine at once, and each `say` names the one to use.
- No always-on default. Voice stays silent until `/voice` is invoked — deliberate, so a session is never noisy without someone asking for it. If you want a persistent default back, add a rule to `~/.claude/CLAUDE.md` naming the mode, but then this file stops being the single source of truth.
- Mechanical enforcement (e.g. a hook that always speaks on Stop) → `settings.json`; use the `update-config` skill if you want that.
