"""Tests for ducking other applications' audio around each utterance."""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.tts.duck import (
    DuckConfig,
    NullDucker,
    SocketDucker,
    ducker_from_config,
)


class TestDuckConfig:
    def test_accepts_valid_level_and_fade(self) -> None:
        config = DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=150, hold_ms=0)
        assert config.level == 0.25
        assert config.fade_down_ms == 100
        assert config.fade_up_ms == 150

    def test_accepts_the_extremes(self) -> None:
        assert DuckConfig(socket_path="/tmp/x.sock", level=0.0, fade_down_ms=100, fade_up_ms=0, hold_ms=0).level == 0.0
        assert DuckConfig(socket_path="/tmp/x.sock", level=1.0, fade_down_ms=100, fade_up_ms=0, hold_ms=0).level == 1.0

    def test_rejects_level_above_one(self) -> None:
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            DuckConfig(socket_path="/tmp/x.sock", level=1.5, fade_down_ms=100, fade_up_ms=150, hold_ms=0)

    def test_rejects_negative_level(self) -> None:
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            DuckConfig(socket_path="/tmp/x.sock", level=-0.1, fade_down_ms=100, fade_up_ms=150, hold_ms=0)

    def test_rejects_negative_fade(self) -> None:
        with pytest.raises(ValueError, match="fade_up_ms must be between 0 and 5000"):
            DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=-1, hold_ms=0)

    def test_rejects_hold_outside_its_range(self) -> None:
        with pytest.raises(ValueError, match="hold_ms must be between 0 and 60000"):
            DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=150, hold_ms=60001)

    def test_rejects_attack_fade_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="fade_down_ms must be between 0 and 5000"):
            DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=-1, fade_up_ms=150, hold_ms=0)

    def test_rejects_fade_above_the_helper_cap(self) -> None:
        with pytest.raises(ValueError, match="fade_up_ms must be between 0 and 5000"):
            DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=5001, hold_ms=0)


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
        with pytest.raises(ValueError, match="missing level, fade_down_ms, fade_up_ms, hold_ms"):
            ducker_from_config({"duck": {"enabled": True, "socket": "/tmp/x.sock"}})

    def test_enabled_builds_socket_ducker(self) -> None:
        config = {"duck": {"enabled": True, "socket": "/tmp/x.sock", "level": 0.25, "fade_down_ms": 100, "fade_up_ms": 500, "hold_ms": 0}}
        assert isinstance(ducker_from_config(config), SocketDucker)

    def test_tilde_in_socket_path_is_expanded(self) -> None:
        config = {"duck": {"enabled": True, "socket": "~/duck.sock", "level": 0.25, "fade_down_ms": 100, "fade_up_ms": 500, "hold_ms": 0}}
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
        ducker.unduck_now()


class TestSocketDucker:
    def _ducker(self) -> SocketDucker:
        return SocketDucker(DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=150, hold_ms=0))

    def test_duck_sends_the_level(self) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            self._ducker().duck()

        sock.connect.assert_called_once_with("/tmp/x.sock")
        sock.sendall.assert_called_once_with(f"duck 0.25 100 150 {os.getpid()}".encode())

    @staticmethod
    def _wait_for_release(ducker: SocketDucker) -> None:
        timer = ducker._release
        assert timer is not None
        timer.join(timeout=5)

    def test_unduck_sends_the_command_after_the_hold(self) -> None:
        ducker = self._ducker()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()
            ducker.unduck()
            self._wait_for_release(ducker)

        assert sock.sendall.call_args_list[-1].args == (b"unduck",)
        assert sock.sendall.call_count == 2

    def test_unduck_now_sends_the_command_without_the_hold(self) -> None:
        # The hold bridges back-to-back utterances; a pause is not that gap, so
        # the release must not wait it out. A hold far longer than the test
        # would otherwise pass only if unduck_now skipped it.
        ducker = SocketDucker(DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=150, hold_ms=60000))
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()
            ducker.unduck_now()
            self._wait_for_release(ducker)

        assert sock.sendall.call_args_list[-1].args == (b"unduck",)
        assert sock.sendall.call_count == 2

    def test_duck_after_a_completed_release_re_ducks(self) -> None:
        # Deterministic counterpart to the race below: once the release has
        # actually landed, a resume must send a fresh duck.
        ducker = self._ducker()
        duck_command = f"duck 0.25 100 150 {os.getpid()}".encode()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()
            ducker.unduck_now()
            self._wait_for_release(ducker)
            ducker.duck()

        sent = [call.args[0] for call in sock.sendall.call_args_list]
        assert sent == [duck_command, b"unduck", duck_command]

    def test_duck_racing_a_pending_release_ends_ducked(self) -> None:
        # A resume landing while the zero-delay release is still in flight must
        # not leave the audio up — the mirror of the bug this feature fixes.
        # Whichever side wins the ducker lock the end state is ducked, so the
        # duck is the last command sent. Whether an unduck was transmitted at
        # all depends on that race, so this asserts the end state rather than a
        # command sequence; test_duck_after_a_completed_release_re_ducks pins
        # the sequence.
        ducker = self._ducker()
        duck_command = f"duck 0.25 100 150 {os.getpid()}".encode()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()
            ducker.unduck_now()
            ducker.duck()

        assert sock.sendall.call_args_list[-1].args == (duck_command,)
        assert ducker._ducked is True

    def test_unduck_without_a_duck_sends_nothing(self) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            self._ducker().unduck()

        sock.sendall.assert_not_called()

    def test_back_to_back_utterances_keep_the_duck(self) -> None:
        # The second duck lands inside the hold window, so the music never
        # pumps up between sentences: one duck sent, no unduck at all.
        ducker = SocketDucker(DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=150, hold_ms=60000))
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()
            ducker.unduck()
            ducker.duck()
            ducker.unduck()
            ducker.duck()

        assert sock.sendall.call_count == 1
        assert ducker._release is None

    def test_ducks_again_after_the_release(self) -> None:
        ducker = self._ducker()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            ducker.duck()
            ducker.unduck()
            self._wait_for_release(ducker)
            ducker.duck()

        sent = [call.args[0] for call in sock.sendall.call_args_list]
        assert sent == [f"duck 0.25 100 150 {os.getpid()}".encode(), b"unduck", f"duck 0.25 100 150 {os.getpid()}".encode()]

    def test_failed_duck_is_retried_on_the_next_utterance(self) -> None:
        # A duck that never reached the helper must not be remembered as held.
        ducker = self._ducker()
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = [FileNotFoundError("gone"), None]
            ducker.duck()
            ducker.unduck()
            ducker.duck()

        assert sock.sendall.call_count == 1

    def test_timeout_scales_with_the_fade(self) -> None:
        with patch("src.tts.duck.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            SocketDucker(DuckConfig(socket_path="/tmp/x.sock", level=0.25, fade_down_ms=100, fade_up_ms=500, hold_ms=0)).duck()

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


class TestPlayerDuckPairing:
    """AudioPlayer._run must unduck on every exit path, or music stays quiet."""

    @staticmethod
    def _recording_ducker(events: list[str]) -> MagicMock:
        ducker = MagicMock()
        ducker.duck.side_effect = lambda: events.append("duck")
        ducker.unduck.side_effect = lambda: events.append("unduck")
        ducker.unduck_now.side_effect = lambda: events.append("unduck_now")
        return ducker

    @patch("src.tts.player.sd")
    def test_ducks_before_audio_and_unducks_after(self, mock_sd: MagicMock) -> None:
        from src.tts.player import AudioPlayer, PlaybackJob

        events: list[str] = []
        mock_stream = MagicMock()
        mock_stream.write.side_effect = lambda _frames: events.append("write")
        mock_sd.OutputStream.return_value = mock_stream
        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0, ducker=self._recording_ducker(events))

        player.submit(PlaybackJob(chunks=[np.ones(100, dtype=np.float32)] * 2, output_path=None))
        player.close()

        assert events[0] == "duck"
        assert events[-1] == "unduck"
        assert events.count("duck") == 1
        assert events.count("unduck") == 1
        assert "write" in events

    @patch("src.tts.player.sd")
    def test_unducks_when_playback_is_cancelled(self, mock_sd: MagicMock) -> None:
        from src.tts.player import AudioPlayer, PlaybackJob

        events: list[str] = []
        cancel = threading.Event()
        mock_stream = MagicMock()
        mock_stream.write.side_effect = lambda _frames: cancel.set()
        mock_sd.OutputStream.return_value = mock_stream
        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0, ducker=self._recording_ducker(events))

        cancelled: list[bool] = []
        player.submit(
            PlaybackJob(
                chunks=[np.ones(100, dtype=np.float32)] * 4,
                output_path=None,
                on_cancel=lambda: cancelled.append(True),
                cancel=cancel,
            )
        )
        player.close()

        assert cancelled == [True]
        assert events == ["duck", "unduck"]

    @patch("src.tts.player.sd")
    def test_unducks_when_playback_fails(self, mock_sd: MagicMock) -> None:
        from src.tts.player import AudioPlayer, PlaybackJob

        events: list[str] = []
        mock_stream = MagicMock()
        mock_stream.write.side_effect = RuntimeError("device gone")
        mock_sd.OutputStream.return_value = mock_stream
        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0, ducker=self._recording_ducker(events))

        errors: list[Exception] = []
        player.submit(
            PlaybackJob(
                chunks=[np.ones(100, dtype=np.float32)],
                output_path=None,
                on_error=errors.append,
            )
        )
        player.close()

        assert len(errors) == 1
        assert events == ["duck", "unduck"]

    @patch("src.tts.player.sd")
    def test_releases_the_duck_while_paused_and_re_ducks_on_resume(self, mock_sd: MagicMock) -> None:
        """A paused voice is not audible, so other audio must not stay ducked.

        Pause parks the player thread inside the job, so the ``finally`` that
        releases the duck never runs — the release has to happen where the
        player actually waits.
        """
        from src.tts.player import AudioPlayer, PlaybackJob

        events: list[str] = []
        pause = threading.Event()
        during_pause: list[str] = []
        writes = 0

        def on_write(_frames: object) -> None:
            nonlocal writes
            writes += 1
            events.append("write")
            if writes == 3:
                pause.set()

        mock_stream = MagicMock()
        mock_stream.write.side_effect = on_write
        mock_sd.OutputStream.return_value = mock_stream
        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0, ducker=self._recording_ducker(events))

        parked: list[bool] = []

        def wait_for_event(name: str, timeout: float = 5.0) -> None:
            """Wait for a recorded ducker event, so the test needs no fixed sleep."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and name not in events:
                time.sleep(0.005)

        def observe_then_resume() -> None:
            try:
                parked.append(pause.wait(timeout=5))
                wait_for_event("unduck_now")
                during_pause.extend(events)
            finally:
                # Always release the pause. If this thread dies first,
                # player.close() blocks forever on the job queue and hangs the
                # suite instead of failing it.
                pause.clear()

        controller = threading.Thread(target=observe_then_resume, daemon=True)
        controller.start()
        player.submit(PlaybackJob(chunks=[np.ones(100, dtype=np.float32)] * 40, output_path=None, pause=pause))
        controller.join(timeout=5)
        player.close()

        assert parked == [True], "the player never parked on the pause"
        assert "unduck_now" in during_pause, f"pause left the duck applied: {during_pause}"
        assert events.count("duck") == 2, f"resume did not re-duck: {events}"
        assert events.count("unduck") == 1
        assert events[-1] == "unduck"

    @patch("src.tts.player.sd")
    def test_cancel_while_paused_does_not_re_duck(self, mock_sd: MagicMock) -> None:
        """Leaving the pause by cancelling must not put the duck back on."""
        from src.tts.player import AudioPlayer, PlaybackJob

        events: list[str] = []
        pause = threading.Event()
        cancel = threading.Event()
        writes = 0

        def on_write(_frames: object) -> None:
            nonlocal writes
            writes += 1
            events.append("write")
            if writes == 3:
                pause.set()

        mock_stream = MagicMock()
        mock_stream.write.side_effect = on_write
        mock_sd.OutputStream.return_value = mock_stream
        player = AudioPlayer(sample_rate=1000, lead_silence_ms=0, ducker=self._recording_ducker(events))

        parked: list[bool] = []

        def wait_for_event(name: str, timeout: float = 5.0) -> None:
            """Wait for a recorded ducker event, so the test needs no fixed sleep."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and name not in events:
                time.sleep(0.005)

        def pause_then_cancel() -> None:
            try:
                parked.append(pause.wait(timeout=5))
                wait_for_event("unduck_now")
            finally:
                # Cancel in a finally, so a timeout here fails the assertions
                # below rather than hanging player.close() on the job queue.
                cancel.set()

        cancelled: list[bool] = []
        controller = threading.Thread(target=pause_then_cancel, daemon=True)
        controller.start()
        player.submit(
            PlaybackJob(
                chunks=[np.ones(100, dtype=np.float32)] * 40,
                output_path=None,
                on_cancel=lambda: cancelled.append(True),
                cancel=cancel,
                pause=pause,
            )
        )
        controller.join(timeout=5)
        player.close()

        assert parked == [True], "the player never parked on the pause"
        assert cancelled == [True]
        assert "unduck_now" in events
        assert events.count("duck") == 1, f"cancel re-ducked the audio: {events}"
        assert events[-1] == "unduck"
