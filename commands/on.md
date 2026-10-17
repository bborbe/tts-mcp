---
description: Turn TTS voice on for this session — attention signals, answers to your questions, plus an ACTION-gated gist. Persists until /tts-mcp:off.
---

Enable spoken output for **this session** and make it stick.

Sets mode **`on`** — attention signals, an answer to any question the user asks, plus a short spoken gist of a turn that passes the ACTION test in `Attention Costs Operator` (`~/.claude/CLAUDE.md`): is there something the user must do? A direct question from the user is answered aloud (1–3 sentences, gist first) in **every** active mode, this one included; the ACTION test gates only what is spoken *unprompted*, so a turn that asks nothing of the user stays on screen. That is the level for following the work away from the screen. For unprompted speech cut back to the attention signal alone, use `/tts-mcp:attention`; to have *your* questions read aloud as well (driving by voice), use `/tts-mcp:interview`.

## Steps

1. **Write the state file** — one Bash call:

   ```bash
   mkdir -p ~/.claude/state/voice && printf '%s' '{"mode":"on"}' > ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
   ```

2. **Speak the confirmation** via `mcp__tts__say` (voice `ryan`, lead with a throwaway word): `"Okay. Voice on."`

3. **Print one status line:** `🔊 voice: on (ryan) — persists until /tts-mcp:off`

## Why a hook, not just this command

A slash command runs once. The mode only holds because `~/.claude/hooks/voice-mode.py` re-reads the state file on every prompt and re-states the mode in context — which is also why it survives `/compact`.

Do not "re-arm" by re-running this command. If voice went silent, check the state file:

```bash
cat ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
```

Absent → the mode was never written, or `/tts-mcp:off` cleared it. Present with the right mode → the silence is elsewhere; read `references/voice-playbook.md` and `/tts-mcp:restart`.

## Full rules

`references/voice-playbook.md` — how to speak (voice, `sender` tag, throwaway lead, terseness), what never to speak, and skip/pause/resume.
