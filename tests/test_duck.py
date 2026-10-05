"""Tests for ducking other applications' audio around each utterance."""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.tts.duck import (
    DuckConfig,
    NullDucker,
    SocketDucker,
    ducker_from_config,
)


class TestDuckConfig:
    def test_accepts_valid_level_and_fade(self) -> None:
        config = DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_ms=150)
        assert config.level == 0.25
        assert config.fade_ms == 150

    def test_accepts_the_extremes(self) -> None:
        assert DuckConfig(socket_path="/tmp/x.sock", level=0.0, fade_ms=0).level == 0.0
        assert DuckConfig(socket_path="/tmp/x.sock", level=1.0, fade_ms=0).level == 1.0

    def test_rejects_level_above_one(self) -> None:
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            DuckConfig(socket_path="/tmp/x.sock", level=1.5, fade_ms=150)

    def test_rejects_negative_level(self) -> None:
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            DuckConfig(socket_path="/tmp/x.sock", level=-0.1, fade_ms=150)

    def test_rejects_negative_fade(self) -> None:
        with pytest.raises(ValueError, match="fade_ms must be >= 0"):
            DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_ms=-1)


class TestDuckerFromConfig:
    def test_absent_section_disables_ducking(self) -> None:
        assert isinstance(ducker_from_config({}), NullDucker)

    def test_disabled_section_yields_null_ducker(self) -> None:
        assert isinstance(ducker_from_config({"duck": {"enabled": False}}), NullDucker)

    def test_non_mapping_section_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            ducker_from_config({"duck": "yes"})

    def test_non_boolean_enabled_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a boolean"):
            ducker_from_config({"duck": {"enabled": "true"}})

    def test_enabled_but_incomplete_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing level, fade_ms"):
            ducker_from_config({"duck": {"enabled": True, "socket": "/tmp/x.sock"}})

    def test_enabled_builds_socket_ducker(self) -> None:
        config = {"duck": {"enabled": True, "socket": "/tmp/x.sock", "level": 0.25, "fade_ms": 150}}
        assert isinstance(ducker_from_config(config), SocketDucker)

    def test_tilde_in_socket_path_is_expanded(self) -> None:
        config = {"duck": {"enabled": True, "socket": "~/duck.sock", "level": 0.25, "fade_ms": 150}}
        ducker = ducker_from_config(config)
        assert isinstance(ducker, SocketDucker)

        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()

        connected_to = sock.connect.call_args[0][0]
        assert connected_to.startswith("/")
        assert "~" not in connected_to


class TestNullDucker:
    def test_is_inert(self) -> None:
        ducker = NullDucker()
        ducker.duck()
        ducker.unduck()


class TestSocketDucker:
    def _ducker(self) -> SocketDucker:
        return SocketDucker(DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_ms=150))

    def test_duck_sends_the_level(self) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            self._ducker().duck()

        sock.connect.assert_called_once_with("/tmp/x.sock")
        sock.sendall.assert_called_once_with(f"duck 0.25 {os.getpid()}".encode())

    def test_unduck_sends_the_command(self) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            self._ducker().unduck()

        sock.sendall.assert_called_once_with(b"unduck")

    def test_timeout_scales_with_the_fade(self) -> None:
        # unduck blocks in the helper for the fade duration, so a short timeout
        # would report a false failure on a slow ramp.
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            SocketDucker(DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_ms=500)).unduck()

        sock.settimeout.assert_called_once_with(2.5)

    def test_missing_helper_does_not_raise(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = FileNotFoundError("no such file")
            self._ducker().duck()

        assert "ducking unavailable" in capsys.readouterr().err

    def test_warns_only_once_per_process(self, capsys: pytest.CaptureFixture[str]) -> None:
        ducker = self._ducker()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = FileNotFoundError("no such file")
            ducker.duck()
            ducker.unduck()
            ducker.duck()

        assert capsys.readouterr().err.count("ducking unavailable") == 1

    def test_socket_is_closed_even_on_failure(self) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = FileNotFoundError("no such file")
            self._ducker().duck()

        mock_socket.return_value.__exit__.assert_called_once()

    def test_recovers_after_the_helper_restarts(self) -> None:
        # A cached dead socket would silently duck nothing forever, which is why
        # each call dials afresh.
        ducker = self._ducker()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = [FileNotFoundError("gone"), None]
            ducker.duck()
            sock.connect.side_effect = None
            ducker.duck()

        assert sock.sendall.call_count == 1


class TestPlayerIntegration:
    def test_audio_player_defaults_to_a_no_op_ducker(self) -> None:
        from src.tts.player import AudioPlayer

        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0)
        assert isinstance(player._ducker, NullDucker)

    def test_audio_player_keeps_the_injected_ducker(self) -> None:
        from src.tts.player import AudioPlayer

        ducker = MagicMock()
        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0, ducker=ducker)
        assert player._ducker is ducker
