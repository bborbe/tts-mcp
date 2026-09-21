---
description: Speak attention signals only for this session — never an answer gist. Persists until /tts-mcp:off.
---

Enable **attention-only** spoken output for **this session** and make it stick.

Sets mode **`attention`** — the attention signals, and nothing else. Unlike `/tts-mcp:on`, an answer is never spoken however substantive it is: the screen carries every answer, voice carries only what you must act on. Use this when you want the channel reserved for the ACTION test in **Attention Costs Operator** (`~/.claude/CLAUDE.md`) with no gist layer on top. To hear answers too (still ACTION-gated), use `/tts-mcp:on`; to have *questions* spoken instead, use `/tts-mcp:interview`.

## Steps

1. **Write the state file** — one Bash call:

   ```bash
   mkdir -p ~/.claude/state/voice && printf '%s' '{"mode":"attention"}' > ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
   ```

2. **Speak the confirmation** via `mcp__tts__say` (voice `ryan`, lead with a throwaway word): `"Okay. Attention mode."`

3. **Print one status line:** `🔔 voice: attention (ryan) — attention signals only; persists until /tts-mcp:off`

## Why a hook, not just this command

A slash command runs once. The mode only holds because `~/.claude/hooks/voice-mode.py` re-reads the state file on every prompt and re-states the mode in context — which is also why it survives `/compact`.

`attention` needs no hook change: the hook validates only `OFF_MODES` and passes any other mode string through unmodified.

Do not "re-arm" by re-running this command. If voice went silent, check the state file:

```bash
cat ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
```

Absent → the mode was never written, or `/tts-mcp:off` cleared it. Present with the right mode → the silence is elsewhere; read `references/voice-playbook.md` and `/tts-mcp:restart`.

## Full rules

`references/voice-playbook.md` — how to speak (voice, `sender` tag, throwaway lead, terseness), what never to speak, and skip/pause/resume.
