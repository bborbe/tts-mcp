---
description: Turn TTS voice on in interview mode — every question needing your input is read aloud, one at a time, and your questions are answered aloud.
---

Enable spoken output for **this session**, tuned for being away from the keyboard.

Sets mode **`interview`** — attention signals, **every question that needs the user's input** read aloud one at a time, and an answer to any question the user asks (1–3 sentences, gist first; every active mode answers a direct question). For driving or walking, where you cannot read the screen to see what you are being asked.

## Steps

1. **Write the state file** — one Bash call:

   ```bash
   mkdir -p ~/.claude/state/voice && printf '%s' '{"mode":"interview"}' > ~/.claude/state/voice/$CLAUDE_CODE_SESSION_ID.json
   ```

2. **Speak the confirmation** via `mcp__tts__say` (voice `ryan`, lead with a throwaway word): `"Okay. Interview mode activated."`

3. **Print one status line:** `🔊 voice: interview (ryan) — persists until /tts-mcp:off`

## What this does, and what it does not

**`interview` speaks your questions *and* the user's answers.** A direct question is answered aloud in every active mode, so a hands-free reply no longer stays on screen; `interview` adds the questions *you* are being asked, read aloud one at a time.

One state file holds one mode, so `interview` and `/tts-mcp:on` remain mutually exclusive. What separates them is the unprompted gist: `on` speaks a short gist of a turn that passes the ACTION test, `interview` speaks no such gist — its voice budget goes to questions and answers instead.

## Full rules

`references/voice-playbook.md` — how to speak (voice, `sender` tag, throwaway lead, terseness), what never to speak, and skip/pause/resume.
