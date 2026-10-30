"""Unit tests for the session-id → WezTerm-pane resolver.

Every server test stubs this module out, so these are the only tests that
exercise its own behaviour: the status-glyph strip, the `None`-vs-`{}` failure
contract, corrupt registry entries, ambiguous pane matches, the credential's
empty/unreadable handling, and the jump call's encoding and error mapping.

`None` rather than `{}` matters more than it looks: `{}` means "WezTerm
answered, and there are no panes", while `None` means "the query failed and
nothing can be concluded". A caller that confuses them renders no jump controls
on a working machine, or keeps them on a broken one.
"""

from __future__ import annotations

import email.message
import http.client
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from src import pane_resolver
from src.pane_resolver import (
    JUMP_SERVER_URL,
    JUMP_TIMEOUT_SECONDS,
    list_panes,
    pane_for,
    perform_jump,
    read_jump_token,
    read_registry,
    resolve_panes,
    strip_status_glyph,
)


class _Completed:
    """Stands in for `subprocess.CompletedProcess` without running anything."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_wezterm(monkeypatch: pytest.MonkeyPatch, result: _Completed | Exception) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> _Completed:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(pane_resolver.subprocess, "run", fake_run)


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _Opener:
    """Records the URL and timeout it was asked to open, then yields a canned result."""

    def __init__(self, result: _Response | Exception) -> None:
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []
        self._result = result

    def open(self, url: str, timeout: float | None = None) -> Any:
        self.calls.append(url)
        self.timeouts.append(timeout)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class TestStripStatusGlyph:
    def test_removes_a_leading_status_glyph(self) -> None:
        assert strip_status_glyph("✳ Fleet Manager") == "Fleet Manager"

    def test_leaves_a_plain_name_alone(self) -> None:
        assert strip_status_glyph("Fleet Manager") == "Fleet Manager"

    def test_tolerates_an_empty_name(self) -> None:
        assert strip_status_glyph("") == ""


class TestListPanes:
    def test_is_none_when_wezterm_cannot_be_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_wezterm(monkeypatch, OSError("no such binary"))
        assert list_panes() is None

    def test_is_none_when_wezterm_exits_non_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_wezterm(monkeypatch, _Completed(1, stderr="no server running"))
        assert list_panes() is None

    def test_is_none_when_the_output_is_not_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_wezterm(monkeypatch, _Completed(0, stdout="not json at all"))
        assert list_panes() is None

    def test_is_none_when_the_output_is_not_a_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_wezterm(monkeypatch, _Completed(0, stdout='{"pane_id": 1}'))
        assert list_panes() is None

    def test_is_an_empty_dict_when_wezterm_answers_with_no_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real empty answer must stay `{}` so a working machine keeps working."""
        _stub_wezterm(monkeypatch, _Completed(0, stdout="[]"))
        assert list_panes() == {}

    def test_skips_records_without_an_integer_pane_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_wezterm(
            monkeypatch,
            _Completed(0, stdout=json.dumps([{"pane_id": 7, "title": "Alpha"}, {"pane_id": "7", "title": "Beta"}])),
        )
        assert list_panes() == {"7": "Alpha"}


class TestReadRegistry:
    def test_skips_a_corrupt_entry(self, tmp_path: Path) -> None:
        (tmp_path / "good.json").write_text(json.dumps({"sessionId": "sid-a", "name": "Alpha"}), encoding="utf-8")
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "nameless.json").write_text(json.dumps({"sessionId": "sid-b"}), encoding="utf-8")
        assert read_registry(tmp_path) == {"sid-a": "Alpha"}

    def test_is_empty_when_the_directory_is_missing(self, tmp_path: Path) -> None:
        assert read_registry(tmp_path / "absent") == {}


class TestResolvePanes:
    def test_matches_through_a_glyph_on_the_pane_title(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "a.json").write_text(json.dumps({"sessionId": "sid-a", "name": "Alpha"}), encoding="utf-8")
        _stub_wezterm(monkeypatch, _Completed(0, stdout=json.dumps([{"pane_id": 7, "title": "✳ Alpha"}])))
        assert resolve_panes(["sid-a"], tmp_path) == {"sid-a": "7"}

    def test_matches_through_a_glyph_on_the_registry_name(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "a.json").write_text(json.dumps({"sessionId": "sid-a", "name": "✳ Alpha"}), encoding="utf-8")
        _stub_wezterm(monkeypatch, _Completed(0, stdout=json.dumps([{"pane_id": 7, "title": "Alpha"}])))
        assert resolve_panes(["sid-a"], tmp_path) == {"sid-a": "7"}

    def test_refuses_a_name_that_strips_to_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An all-glyph name strips to `""`, which is also the key an untitled pane lands under.

        Without the guard, that collision returns the untitled pane as a valid
        target — the wrong-pane jump the ambiguity check exists to prevent.
        """
        (tmp_path / "a.json").write_text(json.dumps({"sessionId": "sid-a", "name": "✳"}), encoding="utf-8")
        _stub_wezterm(monkeypatch, _Completed(0, stdout=json.dumps([{"pane_id": 7, "title": ""}])))
        assert resolve_panes(["sid-a"], tmp_path) == {"sid-a": None}

    def test_refuses_an_ambiguous_match_rather_than_guessing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Two panes carrying the same title is no target, not the first one."""
        (tmp_path / "a.json").write_text(json.dumps({"sessionId": "sid-a", "name": "Dup"}), encoding="utf-8")
        _stub_wezterm(
            monkeypatch,
            _Completed(0, stdout=json.dumps([{"pane_id": 1, "title": "Dup"}, {"pane_id": 2, "title": "Dup"}])),
        )
        assert resolve_panes(["sid-a"], tmp_path) == {"sid-a": None}

    def test_gives_every_row_none_when_the_query_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "a.json").write_text(json.dumps({"sessionId": "sid-a", "name": "Alpha"}), encoding="utf-8")
        _stub_wezterm(monkeypatch, OSError("boom"))
        assert resolve_panes(["sid-a", "sid-b"], tmp_path) == {"sid-a": None, "sid-b": None}

    def test_is_empty_when_asked_for_nothing(self, tmp_path: Path) -> None:
        assert resolve_panes([], tmp_path) == {}

    def test_pane_for_is_none_for_an_unknown_session(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _stub_wezterm(monkeypatch, _Completed(0, stdout="[]"))
        assert pane_for("sid-unknown", tmp_path) is None


class TestReadJumpToken:
    def test_strips_surrounding_whitespace(self, tmp_path: Path) -> None:
        path = tmp_path / "tok"
        path.write_text("  secret-value\n", encoding="utf-8")
        assert read_jump_token(path) == "secret-value"

    def test_is_none_for_a_blank_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tok"
        path.write_text("   \n", encoding="utf-8")
        assert read_jump_token(path) is None

    def test_is_none_when_the_file_is_missing(self, tmp_path: Path) -> None:
        assert read_jump_token(tmp_path / "absent") is None

    def test_is_none_for_bytes_that_are_not_utf8(self, tmp_path: Path) -> None:
        """`read_text` raises UnicodeDecodeError — a ValueError, not an OSError.

        Unhandled it would surface as a 500 where the caller promises a 503.
        """
        path = tmp_path / "tok"
        path.write_bytes(b"\xff\xfe\x00")
        assert read_jump_token(path) is None


class TestPerformJump:
    def test_percent_encodes_both_query_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opener = _Opener(_Response(200))
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        perform_jump("395", "a&b=c")

        assert opener.calls == [f"{JUMP_SERVER_URL}?pane=395&t=a%26b%3Dc"]
        assert opener.timeouts == [JUMP_TIMEOUT_SECONDS]

    def test_raises_on_an_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = urllib.error.HTTPError(JUMP_SERVER_URL, 403, "forbidden", email.message.Message(), io.BytesIO())
        opener = _Opener(error)
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        with pytest.raises(RuntimeError, match="HTTP 403"):
            perform_jump("395", "tok")

    def test_raises_when_the_server_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opener = _Opener(urllib.error.URLError("connection refused"))
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        with pytest.raises(RuntimeError, match="unreachable"):
            perform_jump("395", "tok")

    def test_raises_on_a_non_200_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opener = _Opener(_Response(204))
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        with pytest.raises(RuntimeError, match="HTTP 204"):
            perform_jump("395", "tok")

    def test_raises_when_the_read_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A read timeout comes from `getresponse()`, so it is not a `URLError`.

        urllib wraps only the request call; unmapped, this would be a 500 where
        the route promises a 502.
        """
        opener = _Opener(TimeoutError("timed out"))
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        with pytest.raises(RuntimeError, match="timed out"):
            perform_jump("395", "tok")

    def test_raises_on_a_malformed_status_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`http.client.HTTPException` is not an `OSError` either."""
        opener = _Opener(http.client.BadStatusLine("garbage"))
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        with pytest.raises(RuntimeError, match="jump server call failed"):
            perform_jump("395", "tok")

    def test_never_mentions_the_token_in_the_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The failure text is proxied to the browser, so it must not carry the credential."""
        opener = _Opener(urllib.error.URLError("connection refused"))
        monkeypatch.setattr(pane_resolver, "_JUMP_OPENER", opener)

        with pytest.raises(RuntimeError) as raised:
            perform_jump("395", "super-secret-token")

        assert "super-secret-token" not in str(raised.value)
