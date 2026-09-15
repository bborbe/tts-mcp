---
description: Turn TTS voice on in interview mode — every question needing your input is read aloud, one at a time.
---

Enable spoken output for **this session**, tuned for being away from the keyboard.

Sets mode **`interview`** — attention signals plus **every question that needs the user's input**, read aloud one at a time. For driving or walking, where you cannot read the screen to see what you are being asked.

## Steps

1. **Write the state file** — one Bash call:

   ```bash
   mkdir -p ~/.claude/state/voice && printf '%s' '{"mode":"interview"}' > ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
   ```

2. **Speak the confirmation** via `mcp__tts__say` (voice `ryan`, lead with a throwaway word): `"Okay. Interview mode activated."`

3. **Print one status line:** `🔊 voice: interview (ryan) — persists until /tts-mcp:off`

## What this does not do

⚠️ **`interview` speaks questions, not answers.** One state file holds one mode, so this and `/tts-mcp:on` are mutually exclusive — while driving you hear what you are being asked, but the reply stays on screen. That is a real gap for a hands-free session; say so rather than assuming this mode covers it.

## Full rules

`references/voice-playbook.md` — how to speak (voice, `sender` tag, throwaway lead, terseness), what never to speak, and skip/pause/resume.
