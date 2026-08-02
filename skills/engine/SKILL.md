---
name: engine
description: 'Choose which TTS engine Claude speaks with for the rest of this session — qwen3 (9 speakers, emotion instruct) or voxtral (20 voices, 9 languages). Use when the user types /engine, asks to switch engine or model family, asks which engine is running, or wants a voice the current engine does not offer. Session-scoped: the server holds both, this picks per request. Args: qwen3 | voxtral | status | default.'
argument-hint: "[qwen3|voxtral|status|default]"
---

## What this does

Sets which engine Claude passes to `mcp__tts__say` for the rest of this session.

**This is genuinely per-session, and it is the only part of engine choice that can be.** One server process serves every Claude session, and each declared engine's model is loaded once and cached — so the server cannot be "switched". What *is* per-request is the `engine` field on `say`, and that is what this skill sets. Two sessions can speak with different engines against the same server; neither disturbs the other.

## Args

- `qwen3` — Qwen3-TTS CustomVoice. 9 named speakers, 10 languages, supports free-text emotion `instruct`. Default voice `ryan`.
- `voxtral` — Mistral Voxtral 4B. 20 voices across 9 languages, **no** `instruct` support. Default voice `casual_male`.
- `default` — stop passing `engine` at all; the server's own `default_engine` applies.
- `status` — report the session's engine and voice, and what the server has loaded.

On invocation, confirm in one line, e.g. `🎛 engine: voxtral (casual_male)`. If voice mode is on, also speak it — lead with a throwaway word, per the voice playbook.

## Voice follows the engine

Voice names are per-engine and do **not** overlap: `ryan` exists only on qwen3, `casual_male` only on voxtral. The server rejects a mismatch with a 400 rather than guessing. So switching engine **must** switch voice:

| Engine | Voice to use | `instruct` |
|---|---|---|
| `qwen3` | `ryan` | ✅ supported |
| `voxtral` | `casual_male` | ❌ rejected with 400 |

This overrides the "Voice `ryan`, always" line in the voice skill's playbook for as long as a non-qwen3 engine is selected — that line assumes the qwen3 default. Everything else in the playbook still applies unchanged: English always, lead with a throwaway word, terse, no markup.

`casual_male` is the voice this setup used on voxtral historically, before the server moved to qwen3 and `ryan`.

## First use of an engine is slow

The server loads each model lazily — only the default engine is resident at startup. The first `say` on a newly selected engine waits on that load (roughly 15-20 seconds) and reports status `loading` until audio starts. Every later call is immediate.

So after switching, say something short first if the user is waiting on a real answer. Do not treat the pause as a failure and do not retry — the request is queued and will play.

Both models stay resident once loaded; nothing is evicted until the server restarts. Switching back and forth is free after the first use of each.

## When the engine is not available

`/say` returns 400 when the engine is unknown, or is declared but unusable (typically its model was never downloaded). Do not silently fall back to another engine — the user asked for this one. Report what the server said, and check `mcp__tts__get_voices`, whose per-engine entries carry `available`, `loaded`, and `error`.

A server running the older single-engine config has no second engine at all. In that case `get_voices` reports one engine; say so plainly rather than trying to switch.

## Verifying

`mcp__tts__get_voices` is the source of truth for what exists: it lists each engine's voices, language, whether it supports `instruct`, whether its model is loaded, and whether it is available. Prefer it over assuming — the server's config decides which engines exist, and it may declare only one.
