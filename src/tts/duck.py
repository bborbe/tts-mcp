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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

MAX_FADE_MS = 5000
"""Longest ramp the helper accepts; it clamps to the same bound."""


@dataclass(frozen=True)
class DuckConfig:
    """Resolved ducking settings.

    Attributes:
        socket_path: Unix socket the TTSDuck helper listens on.
        level: Gain applied to other apps while speaking, 0.0-1.0.
        fade_ms: Ramp duration in each direction, in milliseconds. Sent to the
            helper with every duck, so this value is what shapes the ramp.
    """

    socket_path: str
    level: float
    fade_ms: int

    def __post_init__(self) -> None:
        """Validate the level and fade range.

        Raises:
            ValueError: If level is outside 0.0-1.0 or fade_ms is negative.
        """
        if not 0.0 <= self.level <= 1.0:
            msg = f"duck level must be between 0.0 and 1.0, got {self.level}"
            raise ValueError(msg)
        if not 0 <= self.fade_ms <= MAX_FADE_MS:
            msg = f"duck fade_ms must be between 0 and {MAX_FADE_MS}, got {self.fade_ms}"
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
    """

    def __init__(self, config: DuckConfig) -> None:
        """Store the resolved configuration.

        Args:
            config: Resolved ducking settings.
        """
        self._config = config
        self._warned = False

    def duck(self) -> None:
        """Ask the helper to ramp other audio down, sparing this process.

        The pid matters: the helper's tap is global, so without it the voice
        itself would be tapped and ducked along with the music.
        """
        self._send(f"duck {self._config.level} {self._config.fade_ms} {os.getpid()}")

    def unduck(self) -> None:
        """Ask the helper to ramp other audio back up."""
        self._send("unduck")

    def _send(self, command: str) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._config.fade_ms / 1000.0 + 2.0)
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

    missing = [key for key in ("socket", "level", "fade_ms") if key not in section]
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

    fade_value = section["fade_ms"]
    if isinstance(fade_value, bool) or not isinstance(fade_value, int):
        msg = f"config duck.fade_ms: must be an integer, got {fade_value!r}"
        raise ValueError(msg)

    return SocketDucker(
        DuckConfig(
            socket_path=str(Path(socket_value).expanduser()),
            level=float(level_value),
            fade_ms=fade_value,
        )
    )
