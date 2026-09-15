---
description: Turn TTS voice on for this session — persists across turns and compaction until /tts-mcp:off.
---

Enable spoken output for **this session** and make it stick.

Voice mode used to live only in conversation context: it faded as a session grew and died outright on `/compact`. `/tts-mcp:on` writes it to a per-session state file instead, and a `UserPromptSubmit` hook re-injects it on every prompt — so it holds until `/tts-mcp:off`, with no re-arming and no silent drop after the first utterance.

## Steps

1. **Write the state file** — one Bash call:

   ```bash
   mkdir -p ~/.claude/state/voice && printf '%s' '{"mode":"narrate"}' > ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
   ```

2. **Speak the confirmation** via `mcp__tts__say` (voice `ryan`, lead with a throwaway word per the playbook): `"Okay. Voice on."`

3. **Print one status line:** `🔊 voice: narrate (ryan) — persists until /tts-mcp:off`

## Which level this sets

`narrate` — attention signals **plus a 1–3 sentence gist of every substantive answer**. That is the level for following the work away from the screen, which is what a bare on/off toggle is for.

The other levels are unchanged and still reachable through `/tts-mcp:voice on|narrate|interview`; nothing collapsed. For the full playbook — what to speak, how to speak, the `sender` tag — read the `/tts-mcp:voice` skill.

## Why a hook, not just this command

A slash command runs once. The mode only holds because `~/.claude/hooks/voice-mode.py` re-reads the state file on every prompt and re-states the mode in context. Do not "re-arm" by re-running this command — if voice went silent, the state file is the thing to check:

```bash
cat ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
```

An empty or missing file means the mode was never written (or `/tts-mcp:off` cleared it). A present file with the right mode means the mode is set and the silence is something else — read the `/tts-mcp:voice` skill's "Never speak" list.
