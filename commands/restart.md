---
description: Restart the TTS server after audio goes silent — but only after ruling out the two cases a restart cannot fix.
---

Use when audio silently stops — most often after a device switch (AirPods connect, headphones unplug). The server is a launchd-supervised FastAPI process (`com.bborbe.tts-mcp`) that binds the default output device at init; a switch orphans it.

## First: which of the three cases is this?

Silence has **three** causes that look identical from the user's side, and **only one is fixed by restarting the server**. Check this before running any step below — it takes one call and saves restarting a process that was never broken.

| What you observe | Cause | Fix |
|---|---|---|
| `mcp__tts__say` errors with `No such tool available` | The session's **MCP tool binding** dropped. The server is almost certainly fine. | Restart Claude Code, or start a new session. **No HTTP fallback** — a missing binding is final for the session. |
| `mcp__tts__say` errors with `health_check_unreachable` **while `curl <url>/health` from a shell returns `{"status":"ok"}`** | The session's MCP **relay is wedged** against a restarted server — the tool exists, the server is healthy, the connection between them is stale. | **`/mcp`** to reconnect. Restarting the server does not fix this; it is what caused it. The HTTP fallback does work here (unlike the missing-binding row), because the server itself is fine. |
| `mcp__tts__say` **succeeds** (returns a `message_id`) but nothing is audible | The server bound a **stale audio device**. | The restart steps below. |

**Restarting the server can *cause* the third case.** Observed 2026-09-06: a restart ran cleanly, `/health` returned `ok`, and `mcp__tts__say` still failed — with `health_check_unreachable`, naming a URL that `curl` reached successfully in the same turn. That contradiction (the tool says unreachable, a shell says healthy) is the signature of a wedged relay, not a dead server, and it cost a server restart, a health-poll loop, an HTTP-fallback detour and process forensics before `/mcp` fixed it in one call. **When the tool and a shell disagree about the server's health, reach for `/mcp` before restart.**

A dropped binding does not heal on `launchctl kickstart` — the server respawns healthy, `/health` returns `ok`, and the tool is still missing, because the tool list is owned by the MCP client in the Claude Code session, not by the server process. Restarting into a green health check and declaring victory is the trap here: the check passes and the user still hears nothing.

**No HTTP fallback — MCP or nothing.** The server exposes the same endpoint over HTTP, but this never reaches it as a fallback. A missing `mcp__tts__say` is the session's MCP config in force — and in a Discord-answered session the tts server is removed from the tool set on purpose (`--strict-mcp-config`), so a "dropped binding" there is the guard working, not a fault. Calling the HTTP endpoint routes around exactly that guard: the reply is already spoken into the call by the assistant itself, so the fallback only adds a duplicate voice on the laptop speakers. Observed 2026-09-03 in a live Discord call — the HTTP fallback double-spoke every answer.

## Restart steps (stale-device case only)

1. `launchctl kickstart -k gui/$(id -u)/com.bborbe.tts-mcp` (KeepAlive respawns a fresh process against the current default device).
2. Poll health until ready — `curl -s http://127.0.0.1:12000/health`. `/health` returns `ok` *before* the model is loaded, so also allow the first `say` to lag. Model reload is ~1-3s on `engine: qwen3`, ~15-20s on `engine: voxtral`.
3. Verify: run `/tts-mcp:voice-selfcheck`.
4. If still silent after restart, it is not the device binding — re-read the table above, then follow the troubleshooting in `/tts-mcp:voice-selfcheck` (server unreachable / stuck queue / synth error).

Caveats: in-flight messages are dropped across a restart; message IDs reset; **one server serves all Claude sessions, so a restart affects every session's relay** — including wedging their relays against the fresh process, which is the third case above.

## This does not touch the voice mode

Restarting the server does not change whether this session speaks. The mode lives in `~/.claude/state/voice/<session-id>.json`; check it with `/tts-mcp:status`.
