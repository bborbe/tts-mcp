# Voice Playbook

The canonical rules for **how** Claude speaks. Read this when a `/tts-mcp:*` voice command tells you to.

The commands decide *whether* to speak (`/tts-mcp:on`, `/tts-mcp:off`, `/tts-mcp:interview`); this file decides *how*. Which engine and voice is `/tts-mcp:engine`. Verifying the audio path is `/tts-mcp:selfcheck`.

## The enablement model

Voice is **off until a command turns it on**. Nothing else enables it — not a `~/.claude/CLAUDE.md` rule, not an output style. Two reasons, both paid for:

- A **global rule is fleet-wide**, so it cannot express a per-session toggle, and it silently outranked the mode arg. Tried 2026-09-03, deleted 2026-09-11.
- An **output style is project-scoped** (`outputStyle` lives in `.claude/settings.local.json`), so selecting one makes every concurrent session in that project speak. `Voice Narrate` was deleted 2026-09-15; `Voice On` survives only as the Boss vault's project default.

**The mode is mechanical.** It is written to `~/.claude/state/voice/<session-id>.json` and re-injected on every prompt by a `UserPromptSubmit` hook (`~/.claude/hooks/voice-mode.py`). That is what makes it survive context growth and `/compact` — it does not fade mid-session. The hook holds *whether* to speak; this file is the playbook for *how*. Per session by construction: the file is keyed on the session id, so one session's toggle never reaches another.

`/tts-mcp:off` always wins.

## How much to speak

The screen is the detail channel; voice is the attention channel. A `say()` costs ~2–5s and plays at ~150 wpm — slower than the user can read. That is the whole reason volume is a *setting*: speaking everything is exhausting, speaking nothing defeats walking away from the screen.

| Command | Attention signals | Answers | Every question |
|---|---|---|---|
| `/tts-mcp:on` | ✅ | ✅ 1–3 sentence gist | ❌ decisions only |
| `/tts-mcp:interview` | ✅ | ❌ screen only | ✅ one at a time |
| `/tts-mcp:off` | ❌ | ❌ | ❌ |

**Attention signals** (spoken in every mode except off):

- **Completion / failure** of background or long-running work — `say("PR 42 merged.")`, `say("Build failed in capitalcom-gateway.")`.
- A **decision point** where you're waiting on the user — `say("Needs your input.")`.

⚠️ **`interview` speaks questions, not answers.** One state file holds one mode, so `interview` and `on` are mutually exclusive: while driving, you hear what you are being asked but the reply stays on screen. If you want both, that is a real gap — say so rather than assuming `interview` covers it.

## How to speak

- **Voice `ryan`, always** — unless `/tts-mcp:engine` selected a non-qwen3 engine, which overrides this. (`ryan` is a Qwen3-TTS CustomVoice speaker and exists *only* on qwen3; voxtral's voices are separate names such as `casual_male`. Voice names do not overlap across engines and the server rejects a mismatch with a 400, so voice and engine must be chosen together. Check `get_voices` when a voice is rejected.)
- **Pass `sender` on every call, no exceptions.** Set it to the same short tag you speak (anchor task → parent goal → repo/service), verbatim. Never omit it, never send the session id or a full task title. One server serves every session, so an utterance with no `sender` is unattributable in the web UI (http://127.0.0.1:12000/) and in `GET /state` — which is exactly what the field exists to prevent. No anchor tag? Send the same short description of the work you speak (`"harness config"`, `"inbox triage"`). A call without `sender` is a bug, not a shortcut.
- **English, always.** Speak English even when the user writes German (or any other language) — the input language never switches the spoken output. This matches the on-screen "English Only" rule; voice is not an exception to it. Do not switch voices to match an input language either. Proper nouns keep their native spelling; everything around them stays English.
- **Lead with a throwaway word.** CoreAudio clips the first ~word of each utterance. Start every spoken message with a disposable lead token — `"Okay."`, `"So,"`, `"Right,"` — so the clip eats that, not the real first word. Never let a content word be first.
- **Name the subject, every utterance.** One server serves every Claude session from a shared queue, so with several sessions open their speech interleaves with nothing to tell them apart — an unlabeled utterance is noise. Every spoken message carries a short tag naming what it is about:

    ```
    throwaway lead + tag + content
    "Okay. ORB DE40 — closing posted for next week."
    "So, vault UI — build failed."
    ```

    - **Tag source, in order:** the session's anchor task (the `📌 Task:` line in the closer panel) → its parent goal → the repo or service being worked on. Never invent one.
    - **Shorten it.** 2–4 words, the distinctive part only — `"ORB DE40"`, not `"ORB DE40 W32 Sunday Review and Extend Closing to W33"`.
    - **Same tag for the whole session.** Pick it once and reuse it verbatim, so the user learns to recognize it by ear.
    - **Order is fixed:** the tag comes *after* the throwaway word, never first — the clipping rule above eats whatever leads, and a clipped tag is worse than none.
    - **No anchor task?** Use a short description of the work (`"harness config"`, `"inbox triage"`). Never skip the tag.

- **Terse.** One idea per sentence. This is a nudge to attention, not a recital of the on-screen text — never read a whole reply aloud.
- **No markup in speech.** No markdown, URLs, file paths, code, or backticks — describe them in words ("the controller Makefile", not "`Makefile.k8s`").
- **Lead with the recommendation and say the word "recommended."** (Standing user rule.)
- **Spell choices out loud:** "option one … option two …" and end with "say one or two."
- **Numbers/IDs:** say them naturally; don't spell long hashes/URLs.

## Never speak, in any mode

- Routine acks ("ok", "done") after something the user just watched happen in 0.3s.
- Tool-call chatter ("Let me check that file.").
- Code, long lists, or file dumps — voice is the wrong shape for them.
- The reply verbatim. Even in `on`, speak the headline and let the screen carry the detail; never let voice and text be the same content at the same length.
- Nothing on account of the user appearing present. An explicit voice command **is** the request — speak even while they watch.

## Never describe speech you did not send

Writing "spoken now", "I'll say that aloud", or narrating the utterance in your reply is not the channel — only `mcp__tts__say` is. If the tool was not called the user hears nothing, and text claiming otherwise is simply false. Call the tool, or say nothing about speaking. The failure is easy to miss because the reply *reads* correct: the intent to speak gets written down instead of executed, most often on a substantive answer where the spoken gist is owed but the turn ends with text alone.

## Skipping, pausing, resuming

"Skip", "stop talking", "next", "cut it short" mean the current utterance, not voice mode — call `mcp__tts__cancel` with no arguments. Playback stops in ~100ms and the next queued message starts. Add `all: true` when the user wants the whole backlog gone ("shut up", "stop all of it"), and pass `message_id` only when a specific message was named. Do not turn voice `off` for a skip: the user is rejecting one utterance, not the mode.

"Pause" / "hold that" / "stop talking but don't lose it" mean `mcp__tts__pause` — playback stops in ~100ms and resumes from the same point on `mcp__tts__resume`. A paused message stays cancellable. Pause is for "interrupt me but keep the rest of this utterance" — a meeting, a question — where skip would throw away audio the user still wants.

An MCP call only lands when Claude is between tool calls, so it is the slow path. When the user complains that skipping arrives too late, point them at `scripts/tts-skip` in the tts-mcp repo (`make skip`) — one curl, bindable to a global hotkey (Raycast, macOS Shortcuts, skhd), which works no matter what any session is doing. `scripts/tts-pause` / `scripts/tts-resume` (`make pause` / `make resume`) are the same one-curl wrappers.

## Spoken confirmation on activation

`/tts-mcp:on` and `/tts-mcp:interview` **also speak the confirmation** — the first thing the new mode does is use itself:

- `on` → `"Okay. Voice on."`
- `interview` → `"Okay. Interview mode activated."`

Fire-and-forget: say it, print the one-line status, done. Do **not** ask "did you hear it?", do not poll `get_status`, do not block on the result. Skip the utterance for `off` and `status` — speaking is exactly what `off` is turning off.

The spoken line is a courtesy signal, **not** a selftest — no confirmation gate, no round-trip. When you actually need proof the audio path works — silence mid-session, a device switch, or the user about to walk away and rely on voice alerts — run `/tts-mcp:selfcheck`.

## Prerequisites

- `mcp__tts__say` bound in this session. If it errors `No such tool available`, the binding is gone and a session restart is the only fix — see `/tts-mcp:restart`.
- `~/.claude/hooks/voice-mode.py` registered under `UserPromptSubmit` in `~/.claude/settings.json`. That registration is what makes the mode persist; without it a command still speaks this turn, but the mode will not survive to the next.
- Server reachable — launchd `com.bborbe.tts-mcp`, HTTP `127.0.0.1:12000`.
- A writable `~/.claude/state/voice/`.
