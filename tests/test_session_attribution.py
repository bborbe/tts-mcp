"""Tests for resolving the calling Claude Code session from an MCP request."""

import json
from pathlib import Path

from src.session_attribution import (
    attribution_label,
    resolve_session_name,
    session_id_from_headers,
)


def _write_entry(sessions_dir: Path, pid: int, payload: object) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestResolveSessionName:
    """resolve_session_name matches the registry entry by sessionId."""

    def test_returns_name_of_matching_entry(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, 101, {"sessionId": "aaa", "name": "Session A"})
        _write_entry(tmp_path, 202, {"sessionId": "bbb", "name": "Session B"})

        assert resolve_session_name("bbb", tmp_path) == "Session B"
        assert resolve_session_name("aaa", tmp_path) == "Session A"

    def test_never_returns_a_peer_name(self, tmp_path: Path) -> None:
        # The failure the whole module guards against: a wrong-but-present label.
        _write_entry(tmp_path, 101, {"sessionId": "aaa", "name": "Session A"})

        assert resolve_session_name("unknown", tmp_path) is None

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, 101, {"sessionId": "aaa", "name": "  Padded  "})

        assert resolve_session_name("aaa", tmp_path) == "Padded"

    def test_entry_without_usable_name_is_none(self, tmp_path: Path) -> None:
        _write_entry(tmp_path, 101, {"sessionId": "aaa", "name": "   "})
        _write_entry(tmp_path, 202, {"sessionId": "bbb"})

        assert resolve_session_name("aaa", tmp_path) is None
        assert resolve_session_name("bbb", tmp_path) is None

    def test_corrupt_entry_does_not_hide_a_good_one(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "999.json").write_text("{not json", encoding="utf-8")
        _write_entry(tmp_path, 101, ["not", "a", "dict"])
        _write_entry(tmp_path, 202, {"sessionId": "bbb", "name": "Session B"})

        assert resolve_session_name("bbb", tmp_path) == "Session B"

    def test_missing_registry_is_none(self, tmp_path: Path) -> None:
        assert resolve_session_name("aaa", tmp_path / "absent") is None

    def test_rename_is_seen_without_restart(self, tmp_path: Path) -> None:
        # Re-read per call: a /rename must not leave a stale label behind.
        _write_entry(tmp_path, 101, {"sessionId": "aaa", "name": "Old name"})
        assert resolve_session_name("aaa", tmp_path) == "Old name"

        _write_entry(tmp_path, 101, {"sessionId": "aaa", "name": "New name"})
        assert resolve_session_name("aaa", tmp_path) == "New name"


class TestSessionIdFromHeaders:
    """session_id_from_headers reads the Claude Code session header."""

    def test_reads_header(self) -> None:
        assert session_id_from_headers({"x-claude-code-session-id": "aaa"}) == "aaa"

    def test_absent_header_is_none(self) -> None:
        assert session_id_from_headers({"content-type": "application/json"}) is None

    def test_blank_header_is_none(self) -> None:
        assert session_id_from_headers({"x-claude-code-session-id": "  "}) is None

    def test_no_headers_is_none(self) -> None:
        assert session_id_from_headers(None) is None


class TestAttributionLabel:
    """The resolved session name wins; sender is only a fallback."""

    def test_session_name_wins_over_sender(self) -> None:
        assert attribution_label("Session A", "worker manager") == "Session A"

    def test_sender_used_when_no_session_name(self) -> None:
        assert attribution_label(None, "worker manager") == "worker manager"

    def test_none_when_neither(self) -> None:
        assert attribution_label(None, None) is None
