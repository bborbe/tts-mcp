"""Resolve a Claude session id to its WezTerm pane, and activate that pane.

Two directions, deliberately different in cost:

- `resolve_panes` is the **list** path. `GET /state` serves up to
  `RECENT_HISTORY_LIMIT` rows and the web UI polls it, so this reads the session
  registry once and shells out to `wezterm cli list` once for the whole
  response, then matches every row in memory. It does not run a subprocess per
  row.
- `pane_for` is the **click** path. One session, re-resolved at the moment the
  operator clicks, so a `/rename` since the list was rendered — or a pane id
  recycled by a WezTerm restart — cannot leave a stale target.

A session that resolves to no pane is `None`, never an exception and never a
guess: the UI renders no jump control for that row rather than one that fails
on click. That is a real answer, not a swallowed error.

Activation goes through the fleet-jump server, which holds the shared
credential. The credential never reaches the browser — the browser asks this
server, and this server asks the jump server.
"""

import http.client
import json
import logging
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

# The fleet-jump server: a loopback process that activates the WezTerm pane it
# is told to. It requires a shared credential, which is why every jump is
# proxied here rather than attempted from the page.
JUMP_SERVER_URL = "http://127.0.0.1:1337/jump"
JUMP_TIMEOUT_SECONDS = 5.0
WEZTERM_TIMEOUT_SECONDS = 5.0

# The default urllib opener honours `http_proxy` / `no_proxy` from the
# environment, so on a machine with a proxy configured and no loopback bypass
# this request — whose query string carries the shared credential — would be
# sent to that proxy instead of to 127.0.0.1. An empty ProxyHandler keeps it on
# the host by construction rather than by the environment's good behaviour.
_JUMP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def strip_status_glyph(text: str) -> str:
    """A name without Claude Code's leading status glyph.

    Claude Code prefixes a pane title with a status glyph (✳ ◐ ◑ ◒ ◓ ⠿ …) and a
    session's own name may carry one too, so the strip is applied to *both*
    sides of the ownership comparison. Mirrors `who-needs-me.py`'s
    `strip_status_glyph` so the display path and this check cannot drift on
    what "the name" is.
    """
    text = (text or "").strip()
    while text and not (text[0].isalnum() or text[0] in "/~._-"):
        text = text[1:].lstrip()
    return text


def read_registry(sessions_dir: Path) -> dict[str, str]:
    """`{session_id: name}` for every registered session, or `{}` when unreadable.

    Read once per request and shared across rows. Unreadable or malformed
    entries are skipped rather than fatal: one corrupt file in the registry must
    not take the jump control down for every other session.
    """
    if not sessions_dir.is_dir():
        logger.warning("session registry missing: %s", sessions_dir)
        return {}

    names: dict[str, str] = {}
    for path in sessions_dir.glob("*.json"):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            logger.debug("skipping unreadable session registry entry %s: %s", path, err)
            continue
        if not isinstance(parsed, dict):
            continue
        entry = cast("dict[str, object]", parsed)
        session_id = entry.get("sessionId")
        name = entry.get("name")
        if isinstance(session_id, str) and isinstance(name, str) and name.strip():
            names[session_id] = name.strip()
    return names


def list_panes() -> dict[str, str] | None:
    """`{pane_id: title}` for every WezTerm pane, or `None` when the query failed.

    `None`, never `{}`: a failed `wezterm cli list` cannot prove a pane is gone
    any more than it can prove one is live, so no caller may read it as "no
    panes exist". `{}` is reserved for a WezTerm that answered with zero panes —
    a real empty answer that must keep working.
    """
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=WEZTERM_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        logger.warning("wezterm cli list failed: %s", err)
        return None
    if result.returncode != 0:
        logger.warning("wezterm cli list exited %d: %s", result.returncode, result.stderr.strip())
        return None
    try:
        panes = json.loads(result.stdout)
    except ValueError as err:
        logger.warning("wezterm cli list returned unparsable JSON: %s", err)
        return None
    if not isinstance(panes, list):
        logger.warning("wezterm cli list returned %s, expected a list", type(panes).__name__)
        return None

    titles: dict[str, str] = {}
    for raw in cast("list[object]", panes):
        if not isinstance(raw, dict):
            continue
        record = cast("dict[str, object]", raw)
        pane_id = record.get("pane_id")
        title = record.get("title")
        if isinstance(pane_id, int):
            titles[str(pane_id)] = title if isinstance(title, str) else ""
    return titles


def resolve_panes(session_ids: Iterable[str], sessions_dir: Path) -> dict[str, str | None]:
    """`{session_id: pane_id | None}` for a whole response, in one pass.

    One registry read and one `wezterm cli list` regardless of how many ids are
    asked for, because the caller polls this endpoint. The per-row answer is
    kept rather than a presence flag because a row whose session has no pane
    must render no control at all; the recent list is capped, so one batched
    read is affordable.
    """
    wanted = [sid for sid in dict.fromkeys(session_ids) if sid]
    if not wanted:
        return {}

    names = read_registry(sessions_dir)
    titles = list_panes()
    if titles is None:
        # The query failed, so no row can be said to have a pane. Every row
        # renders no control rather than a control that would fail on click.
        return {sid: None for sid in wanted}

    by_title: dict[str, list[str]] = {}
    for pane_id, title in titles.items():
        by_title.setdefault(strip_status_glyph(title), []).append(pane_id)

    resolved: dict[str, str | None] = {}
    for session_id in wanted:
        name = names.get(session_id)
        if name is None:
            logger.debug("session %s has no registry entry; no pane", session_id)
            resolved[session_id] = None
            continue
        stripped = strip_status_glyph(name)
        if not stripped:
            # A name made only of glyphs strips to the empty string, and
            # `list_panes` files a pane with no title under that same empty key
            # — so the lookup would match it and jump to an arbitrary pane,
            # which is exactly what the ambiguity guard below exists to refuse.
            logger.debug("session %s (%r) has no usable name; no jump target", session_id, name)
            resolved[session_id] = None
            continue
        matches = by_title.get(stripped, [])
        if len(matches) != 1:
            # Zero is "no pane"; more than one is ambiguous and must not be
            # guessed at, since jumping to the wrong pane is worse than none.
            logger.debug("session %s (%r) matched %d panes; no jump target", session_id, name, len(matches))
            resolved[session_id] = None
            continue
        resolved[session_id] = matches[0]
    return resolved


def pane_for(session_id: str, sessions_dir: Path) -> str | None:
    """The pane for one session, resolved now. `None` when it cannot be resolved."""
    return resolve_panes([session_id], sessions_dir).get(session_id)


def read_jump_token(path: Path) -> str | None:
    """The shared fleet-jump credential, stripped, or `None` when unavailable.

    Never logged: the path and the exception are safe, the token is not.

    `ValueError` is caught alongside `OSError` because `read_text(encoding=...)`
    raises `UnicodeDecodeError` — a `ValueError`, not an `OSError` — on a file
    that is not valid UTF-8. Unhandled, that would surface as a 500 where the
    caller's contract promises a 503.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as err:
        logger.debug("cannot read jump token %s: %s", path, err)
        return None
    token = content.strip()
    return token or None


def perform_jump(pane_id: str, token: str) -> None:
    """Ask the fleet-jump server to activate `pane_id`.

    Both query values are percent-encoded: a token containing `&` or `=` would
    otherwise silently truncate the query and the jump server would see a
    different token than the one on disk.
    """
    query = urllib.parse.urlencode({"pane": pane_id, "t": token})
    url = f"{JUMP_SERVER_URL}?{query}"
    try:
        with _JUMP_OPENER.open(url, timeout=JUMP_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"jump server returned HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"jump server unreachable: {err.reason}") from err
    except (OSError, http.client.HTTPException) as err:
        # urllib wraps only the connect-and-send in URLError. Failures raised by
        # `getresponse()` escape raw: a read timeout is a `TimeoutError`, a
        # dropped connection a `RemoteDisconnected`, a malformed status line an
        # `http.client.HTTPException`. Unmapped, each would surface as a 500
        # where the route promises a 502 for every transport failure.
        raise RuntimeError(f"jump server call failed: {err}") from err
    if status != 200:
        raise RuntimeError(f"jump server returned HTTP {status}")
