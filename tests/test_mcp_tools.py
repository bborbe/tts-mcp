"""Tests for the in-server MCP tools mounted at /mcp.

The tools call the same function their HTTP route calls, so the core assertion
here is route equality: for the same state and input, a tool's text output is
the JSON its route returns. A stub tool cannot satisfy that.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

import src.server as server_module
from src.server import (
    mcp_cancel,
    mcp_get_status,
    mcp_get_voices,
    mcp_pause,
    mcp_resume,
    mcp_say,
    mcp_server,
)
from tests.test_server import _make_app, _make_state


@pytest.fixture(autouse=True)
def _isolate_history_path(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.server.HISTORY_PATH", tmp_path / "history.json")


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A test ServerState installed where the MCP tools look for it."""
    s = _make_state()
    monkeypatch.setattr(server_module.app.state, "server", s, raising=False)
    return s


@pytest.fixture
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "101.json").write_text(json.dumps({"sessionId": "aaa", "name": "Session A"}), encoding="utf-8")
    (d / "202.json").write_text(json.dumps({"sessionId": "bbb", "name": "Session B"}), encoding="utf-8")
    monkeypatch.setattr("src.server.SESSIONS_DIR", d)
    return d


def _ctx(headers: dict[str, str] | None) -> Any:
    return SimpleNamespace(headers=headers)


class TestToolSet:
    """The endpoint exposes exactly the six tools the stdio relay exposed."""

    def test_six_tools(self) -> None:
        tools = asyncio.run(mcp_server.list_tools())
        assert sorted(t.name for t in tools) == ["cancel", "get_status", "get_voices", "pause", "resume", "say"]


class TestRouteEquality:
    """Each tool returns what its route returns for the same input."""

    def test_get_voices_equals_route(self, state: Any) -> None:
        route = TestClient(_make_app(state)).get("/voices").json()
        assert json.loads(mcp_get_voices()) == route

    def test_say_then_get_status_equals_route(self, state: Any, sessions_dir: Path) -> None:
        said = json.loads(mcp_say(voice="casual_female", text="hello", ctx=_ctx(None)))
        assert said["status"] == "queued"

        route = TestClient(_make_app(state)).get(f"/status/{said['message_id']}").json()
        assert json.loads(mcp_get_status(message_id=said["message_id"])) == route

    def test_pause_and_resume_equal_route_on_idle(self, state: Any) -> None:
        client = TestClient(_make_app(state))
        assert json.loads(mcp_pause()) == client.post("/pause").json()
        assert json.loads(mcp_resume()) == client.post("/resume").json()

    def test_cancel_all_equals_route_shape(self, state: Any) -> None:
        result = json.loads(mcp_cancel(all=True))
        assert set(result) == {"cancelled", "queued"}
        assert result["queued"] == 0


class TestErrors:
    """A route's HTTP error surfaces as a ToolError carrying the relay's error body."""

    def test_unknown_message_id(self, state: Any) -> None:
        with pytest.raises(ToolError) as info:
            mcp_get_status(message_id="nope")
        body = json.loads(str(info.value))
        assert body["error"] == "http_error"
        assert body["status_code"] == 404
        assert "nope" in body["response"]["detail"]

    def test_unknown_voice(self, state: Any, sessions_dir: Path) -> None:
        with pytest.raises(ToolError) as info:
            mcp_say(voice="not-a-voice", text="hello", ctx=_ctx(None))
        assert json.loads(str(info.value))["status_code"] == 400


class TestSayAttribution:
    """say labels the utterance with the calling session's own name."""

    def _sender_of(self, state: Any, message_id: str) -> str | None:
        with state.status_lock:
            return state.statuses[message_id].sender

    def test_each_session_gets_its_own_name(self, state: Any, sessions_dir: Path) -> None:
        a = json.loads(mcp_say(voice="casual_female", text="one", ctx=_ctx({"x-claude-code-session-id": "aaa"})))
        b = json.loads(mcp_say(voice="casual_female", text="two", ctx=_ctx({"x-claude-code-session-id": "bbb"})))

        assert self._sender_of(state, a["message_id"]) == "Session A"
        assert self._sender_of(state, b["message_id"]) == "Session B"

    def test_session_name_wins_over_sender(self, state: Any, sessions_dir: Path) -> None:
        said = json.loads(
            mcp_say(
                voice="casual_female",
                text="hi",
                sender="worker manager",
                ctx=_ctx({"x-claude-code-session-id": "aaa"}),
            )
        )
        assert self._sender_of(state, said["message_id"]) == "Session A"

    def test_falls_back_to_sender_without_header(self, state: Any, sessions_dir: Path) -> None:
        said = json.loads(mcp_say(voice="casual_female", text="hi", sender="caller", ctx=_ctx({})))
        assert self._sender_of(state, said["message_id"]) == "caller"

    def test_unknown_session_falls_back_to_sender_not_a_peer(self, state: Any, sessions_dir: Path) -> None:
        said = json.loads(mcp_say(voice="casual_female", text="hi", sender="caller", ctx=_ctx({"x-claude-code-session-id": "zzz"})))
        assert self._sender_of(state, said["message_id"]) == "caller"
