"""Duck other applications' audio while the TTS speaks.

The duck itself is performed by the TTSDuck helper, not by this process: a
CoreAudio process tap requires the kTCCServiceAudioCapture grant, and macOS
only grants that to a code-signed app bundle. A Python process launched through
uv cannot hold it, and a process spawned *by* that Python inherits the parent's
TCC identity, so it cannot hold it either — which is why the helper is launched
separately (launchd or LaunchServices) and driven over a unix socket.

Ducking is best-effort by design. If the helper is not running, or the socket
is stale, playback proceeds unducked: a missing convenience must never stop the
voice.
"""

import os
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

MAX_FADE_MS = 5000
"""Longest ramp the helper accepts; it clamps to the same bound."""

MAX_HOLD_MS = 60000
"""Longest the duck may outlive the last utterance before releasing."""


@dataclass(frozen=True)
class DuckConfig:
    """Resolved ducking settings.

    Attributes:
        socket_path: Unix socket the TTSDuck helper listens on.
        level: Gain applied to other apps while speaking, 0.0-1.0.
        fade_down_ms: Attack — how fast other audio drops when speech starts.
            Keep it short so the voice's first words are clear.
        fade_up_ms: Release — how gently other audio returns afterwards.
            Both fades travel with every duck, so these values shape the ramp.
        hold_ms: How long the duck outlives an utterance. A new utterance
            inside this window keeps the music down instead of letting it pump
            up and back between sentences.
    """

    socket_path: str
    level: float
    fade_down_ms: int
    fade_up_ms: int
    hold_ms: int

    def __post_init__(self) -> None:
        """Validate the level, fade and hold ranges.

        Raises:
            ValueError: If level is outside 0.0-1.0 or a fade or hold is outside
                its range.
        """
        if not 0.0 <= self.level <= 1.0:
            msg = f"duck level must be between 0.0 and 1.0, got {self.level}"
            raise ValueError(msg)
        for name, value in (("fade_down_ms", self.fade_down_ms), ("fade_up_ms", self.fade_up_ms)):
            if not 0 <= value <= MAX_FADE_MS:
                msg = f"duck {name} must be between 0 and {MAX_FADE_MS}, got {value}"
                raise ValueError(msg)
        if not 0 <= self.hold_ms <= MAX_HOLD_MS:
            msg = f"duck hold_ms must be between 0 and {MAX_HOLD_MS}, got {self.hold_ms}"
            raise ValueError(msg)


class Ducker(Protocol):
    """Lowers other applications' audio for the duration of an utterance."""

    def duck(self) -> None:
        """Ramp other applications' audio down to the configured level."""
        ...

    def unduck(self) -> None:
        """Ramp other applications' audio back to full volume."""
        ...


class NullDucker:
    """Ducker used when the feature is disabled; does nothing."""

    def duck(self) -> None:
        """Do nothing."""

    def unduck(self) -> None:
        """Do nothing."""


class SocketDucker:
    """Drives the TTSDuck helper over its unix socket.

    Each call opens a fresh connection. That is deliberate: the helper may be
    restarted (or granted permission) between utterances, and a cached dead
    socket would silently duck nothing for the rest of the process's life.

    The release is deferred by ``hold_ms``: ``unduck`` only schedules it, and a
    ``duck`` inside the window cancels it. Back-to-back utterances therefore
    keep the music down rather than pumping it up and back, and the playback
    thread never waits on the fade-up. If this process dies with a release
    pending, the helper notices the spared pid is gone and releases on its own.
    """

    def __init__(self, config: DuckConfig) -> None:
        """Store the resolved configuration.

        Args:
            config: Resolved ducking settings.
        """
        self._config = config
        self._warned = False
        self._lock = threading.Lock()
        self._ducked = False
        self._release: threading.Timer | None = None

    def duck(self) -> None:
        """Ask the helper to ramp other audio down, sparing this process.

        The pid matters: the helper's tap is global, so without it the voice
        itself would be tapped and ducked along with the music.
        """
        with self._lock:
            self._cancel_release()
            if self._ducked:
                return
            config = self._config
            self._ducked = self._send(f"duck {config.level} {config.fade_down_ms} {config.fade_up_ms} {os.getpid()}")

    def unduck(self) -> None:
        """Schedule the music to ramp back up once ``hold_ms`` passes quietly."""
        with self._lock:
            self._cancel_release()
            if not self._ducked:
                return
            timer = threading.Timer(self._config.hold_ms / 1000.0, self._release_now)
            timer.daemon = True
            self._release = timer
            timer.start()

    def _release_now(self) -> None:
        with self._lock:
            if self._release is not threading.current_thread():
                return  # superseded by a later duck/unduck
            self._release = None
            self._send("unduck")
            self._ducked = False

    def _cancel_release(self) -> None:
        if self._release is not None:
            self._release.cancel()
            self._release = None

    def _send(self, command: str) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                # unduck blocks in the helper for the release ramp.
                sock.settimeout(max(self._config.fade_down_ms, self._config.fade_up_ms) / 1000.0 + 2.0)
                sock.connect(self._config.socket_path)
                sock.sendall(command.encode())
                sock.recv(256)
        except OSError as exc:
            # Warn once per process rather than per utterance: a missing helper
            # would otherwise print on every sentence the voice ever speaks.
            if not self._warned:
                self._warned = True
                print(
                    f"\n  ducking unavailable ({exc}); speaking at full music volume.",
                    file=sys.stderr,
                )
            return False
        return True


def ducker_from_config(config: dict[str, object]) -> Ducker:
    """Build a ducker from the parsed config.yaml mapping.

    Ducking is opt-in: a config without a `duck:` section yields a NullDucker.
    When the section is present every key is required — a half-configured duck
    is a mistake, not a request for defaults.

    Args:
        config: The parsed config.yaml mapping.

    Returns:
        A SocketDucker when configured and enabled, otherwise a NullDucker.

    Raises:
        ValueError: If the duck section is present but disabled and malformed,
            or enabled while missing a required key.
    """
    raw_section = config.get("duck")
    if raw_section is None:
        return NullDucker()
    if not isinstance(raw_section, dict):
        msg = f"config duck: must be a mapping, got {type(raw_section).__name__}"
        raise ValueError(msg)
    section = cast("dict[str, object]", raw_section)

    enabled = section.get("enabled")
    if not isinstance(enabled, bool):
        msg = f"config duck.enabled: must be a boolean, got {enabled!r}"
        raise ValueError(msg)
    if not enabled:
        return NullDucker()

    missing = [key for key in ("socket", "level", "fade_down_ms", "fade_up_ms", "hold_ms") if key not in section]
    if missing:
        msg = f"config duck: enabled but missing {', '.join(missing)}"
        raise ValueError(msg)

    # Each value is checked rather than coerced: a `level: yes` silently becoming
    # 1.0 would duck nothing, which reads exactly like a broken helper.
    socket_value = section["socket"]
    if not isinstance(socket_value, str):
        msg = f"config duck.socket: must be a string, got {socket_value!r}"
        raise ValueError(msg)

    level_value = section["level"]
    if isinstance(level_value, bool) or not isinstance(level_value, (int, float)):
        msg = f"config duck.level: must be a number, got {level_value!r}"
        raise ValueError(msg)

    return SocketDucker(
        DuckConfig(
            socket_path=str(Path(socket_value).expanduser()),
            level=float(level_value),
            fade_down_ms=_require_int(section, "fade_down_ms"),
            fade_up_ms=_require_int(section, "fade_up_ms"),
            hold_ms=_require_int(section, "hold_ms"),
        )
    )


def _require_int(section: dict[str, object], key: str) -> int:
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"config duck.{key}: must be an integer, got {value!r}"
        raise ValueError(msg)
    return value
