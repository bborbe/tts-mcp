---
description: Turn TTS voice off for this session — stop calling mcp__tts__say until /tts-mcp:on.
---

End spoken output for **this session**. `/tts-mcp:off` always wins.

## Steps

1. **Delete the state file** — one Bash call:

   ```bash
   rm -f ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
   ```

   The `UserPromptSubmit` hook injects nothing once the file is gone, so the mode stops being re-armed from the next prompt onward.

2. **Stop calling `mcp__tts__say`** for the rest of this session — any earlier voice command is now superseded.

3. **Print one status line:** `🔇 voice: off`

## Do not speak the confirmation

Speaking is exactly what `off` is turning off. Print the status line, say nothing.

## Scope

Per session — it deletes only *this* session's file. A concurrent session with its own `/tts-mcp:on` is unaffected, which is the point of keying the state on the session id rather than a project-wide setting.

Do not run `rm -rf ~/.claude/state/voice/` — that directory is shared by every session, and deleting it turns voice off fleet-wide.
