---
description: Report the current TTS voice mode and voice for this session.
---

Report the voice state for **this session**. Read-only — writes nothing.

## Steps

1. **Read the state file:**

   ```bash
   cat ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json 2>/dev/null || echo "(no state file — voice is off)"
   ```

2. **Report one line**, naming all three parts:

   - **Mode** — `on` / `interview`, or off if the file is absent.
   - **Voice** — `ryan` unless `/tts-mcp:engine` selected a non-qwen3 engine.
   - **Persistence** — whether `~/.claude/hooks/voice-mode.py` is registered under `UserPromptSubmit` in `~/.claude/settings.json`. Without that registration the mode still applies this turn but will not survive to the next, and that is the single most common reason a session that should be speaking is not.

   Example: `🔊 voice: on (ryan) — hook registered, persists until /tts-mcp:off`

3. **Do not speak the report.** `status` is a diagnostic; read it on screen. If the user asked for it aloud, they will say so.

## If the mode is set but nothing is being spoken

The mode is only half the model. The other half is the playbook's *"Never speak"* list and the caller's own rules — a session can be correctly in `on` mode and still legitimately silent. See `references/voice-playbook.md`, then `/tts-mcp:restart` if the tool itself is failing.
