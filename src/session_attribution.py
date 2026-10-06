"""Resolve which Claude Code session a request to the in-server MCP endpoint came from.

The speech server is shared by every session, so the MCP endpoint mounted on it
cannot learn the caller from its own process tree the way the old per-session
stdio relay did (that relay sat underneath its session's ``claude`` process).
The identity has to come from the request instead: Claude Code sends the session
id in the ``X-Claude-Code-Session-Id`` header, and the session's display name is
read from the same registry the fleet views trust,
``~/.claude/sessions/<pid>.json``, by matching that file's ``sessionId``.

The name is best-effort attribution metadata, not a required value. A miss
degrades to the caller's own ``sender`` string. That is a deliberate, scoped
carve-out from AGENTS.md's "fail fast — never swallow errors": a ``say`` must
never fail because a label could not be resolved. The carve-out is not silent —
an unresolved name is logged at error level — and the ``say`` path itself still
propagates every request failure.
"""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

SESSION_ID_HEADER = "x-claude-code-session-id"


def resolve_session_name(session_id: str, sessions_dir: Path) -> str | None:
    """Return the display name Claude Code recorded for ``session_id``, or None.

    Scans ``sessions_dir`` for the registry entry whose ``sessionId`` matches.
    Re-read on every call rather than cached: a session's name changes when the
    operator runs ``/rename``, and a stale cached name is exactly the
    bad-but-present label this module exists to avoid.

    Unreadable or malformed entries are skipped, not fatal — one corrupt file in
    the registry must not take attribution down for every other session.
    """
    if not sessions_dir.is_dir():
        logger.error("session registry missing: %s", sessions_dir)
        return None

    for path in sessions_dir.glob("*.json"):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            logger.debug("skipping unreadable session registry entry %s: %s", path, err)
            continue
        if not isinstance(parsed, dict):
            continue
        entry = cast("dict[str, object]", parsed)
        if entry.get("sessionId") != session_id:
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        logger.error("session %s has a registry entry but no usable name (%s)", session_id, path)
        return None

    logger.error("session %s has no registry entry in %s", session_id, sessions_dir)
    return None


def session_id_from_headers(headers: Mapping[str, str] | None) -> str | None:
    """Pull the Claude Code session id out of request headers, or None when absent."""
    if headers is None:
        return None
    value = headers.get(SESSION_ID_HEADER)
    if value is None or not value.strip():
        return None
    return value.strip()


def attribution_label(session_name: str | None, sender: str | None) -> str | None:
    """The label to display: the resolved session name wins, ``sender`` is only a fallback.

    Precedence is the whole point — defaulting only when ``sender`` is *absent*
    would change nothing, because the failure this guards against is a caller
    passing a bad value, not omitting one. Kept identical to the TypeScript
    relay's ``attributionLabel`` so the port does not change labelling.
    """
    if session_name is not None:
        return session_name
    return sender
