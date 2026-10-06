"""Tests for scripts/claude-session-headers, the /mcp headersHelper.

The helper is a POSIX sh script that walks up from its parent pid to the
`claude` ancestor using `ps`. These tests put a stub `ps` first on PATH so each
branch runs against a controlled process tree instead of the real one.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "claude-session-headers"


def _stub_ps(bin_dir: Path, body: str) -> None:
    ps = bin_dir / "ps"
    ps.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


def _run(bin_dir: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)}
    return subprocess.run([str(SCRIPT)], capture_output=True, text=True, env=env, check=True, timeout=30)


def test_unreadable_parent_pid_prints_empty_object_silently(tmp_path: Path) -> None:
    # comm lookups succeed but name no claude process; ppid lookups fail.
    _stub_ps(tmp_path, 'case "$*" in *comm=*) echo /bin/zsh ;; *) exit 1 ;; esac\n')

    result = _run(tmp_path, tmp_path)

    assert json.loads(result.stdout) == {}
    assert result.stderr == ""


def test_claude_ancestor_yields_its_session_id(tmp_path: Path) -> None:
    # The helper's parent is reported as `claude`; its registry entry names the session.
    _stub_ps(tmp_path, 'case "$*" in *comm=*) echo /usr/local/bin/claude ;; *) echo 1 ;; esac\n')
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    parent = os.getpid()
    (sessions / f"{parent}.json").write_text(json.dumps({"sessionId": "abc-123", "name": "x"}), encoding="utf-8")

    result = _run(tmp_path, tmp_path)

    assert json.loads(result.stdout) == {"X-Claude-Code-Session-Id": "abc-123"}
