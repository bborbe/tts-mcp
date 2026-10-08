"""Tests for WSOLA time stretching."""

from __future__ import annotations

import numpy as np
import pytest

from src.tts.timestretch import (
    MAX_SPEED,
    MIN_SPEED,
    TimeStretcher,
    stretch,
)

SAMPLE_RATE = 24000


def _tone(frequency: float, seconds: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Return a mono sine tone as float32."""
    t = np.arange(int(sample_rate * seconds), dtype=np.float32) / sample_rate
    return (0.5 * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def _dominant_frequency(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    """Return the frequency of the strongest spectral component."""
    windowed = audio * np.hanning(len(audio))
    spectrum = np.abs(np.fft.rfft(windowed))
    frequencies = np.fft.rfftfreq(len(audio), 1.0 / sample_rate)
    return float(frequencies[int(np.argmax(spectrum))])


class TestStretchRatio:
    """The realized duration must match the requested factor."""

    @pytest.mark.parametrize("speed", [0.75, 1.25, 1.5, 2.0])
    def test_output_length_matches_requested_speed(self, speed: float) -> None:
        audio = _tone(220.0, 1.0)
        out = stretch(audio, speed)
        expected = len(audio) / speed
        # Tolerance covers the rounded analysis hop plus the frame grid at the
        # edges; the realized ratio is within a fraction of a percent otherwise.
        assert abs(len(out) - expected) / expected < 0.05

    def test_speed_one_returns_input_unchanged(self) -> None:
        audio = _tone(220.0, 0.5)
        out = stretch(audio, 1.0)
        assert len(out) == len(audio)
        assert np.array_equal(out, audio)

    def test_faster_speed_produces_shorter_audio(self) -> None:
        audio = _tone(220.0, 1.0)
        assert len(stretch(audio, 2.0)) < len(stretch(audio, 1.5)) < len(audio)


class TestPitchPreservation:
    """The whole point of a time-stretch rather than a resample."""

    @pytest.mark.parametrize("speed", [0.75, 1.5, 2.0])
    def test_pitch_is_unchanged(self, speed: float) -> None:
        audio = _tone(200.0, 1.0)
        out = stretch(audio, speed)
        assert _dominant_frequency(out) == pytest.approx(200.0, abs=8.0)

    def test_a_resample_would_have_shifted_pitch(self) -> None:
        """Guard against a regression to naive resampling.

        Resampling the same tone to 1.5x raises its pitch to 300Hz; this pins
        the difference so a future "simplification" to resampling fails here.
        """
        audio = _tone(200.0, 1.0)
        indices = np.arange(0, len(audio), 1.5).astype(int)
        resampled = audio[indices]
        assert _dominant_frequency(resampled) == pytest.approx(300.0, abs=15.0)
        assert _dominant_frequency(stretch(audio, 1.5)) == pytest.approx(200.0, abs=8.0)


class TestStreaming:
    """The streaming path must match the one-shot path."""

    def test_chunked_feed_matches_single_feed(self) -> None:
        audio = _tone(220.0, 1.0)
        one_shot = stretch(audio, 1.5)

        stretcher = TimeStretcher(1.5)
        produced = [stretcher.feed(chunk) for chunk in np.array_split(audio, 20)]
        produced.append(stretcher.flush())
        chunked = np.concatenate([part for part in produced if len(part)]) if produced else np.zeros(0)

        # Frame-aligned emission makes the two paths identical in length.
        assert abs(len(chunked) - len(one_shot)) <= 1

    def test_feed_returns_empty_until_enough_input_arrives(self) -> None:
        stretcher = TimeStretcher(1.5)
        assert len(stretcher.feed(np.zeros(64, dtype=np.float32))) == 0

    def test_flush_is_idempotent(self) -> None:
        stretcher = TimeStretcher(1.5)
        stretcher.feed(_tone(220.0, 0.5))
        first = stretcher.flush()
        assert len(first) > 0
        assert len(stretcher.flush()) == 0

    def test_feed_after_flush_raises(self) -> None:
        stretcher = TimeStretcher(1.5)
        stretcher.feed(_tone(220.0, 0.5))
        stretcher.flush()
        with pytest.raises(RuntimeError, match="after flush"):
            stretcher.feed(_tone(220.0, 0.1))

    def test_memory_does_not_grow_with_utterance_length(self) -> None:
        """Consumed input must be dropped, not accumulated."""
        stretcher = TimeStretcher(1.5)
        for _ in range(40):
            stretcher.feed(_tone(220.0, 0.5))
        buffered = len(stretcher._buffer)
        assert buffered < SAMPLE_RATE


class TestEdgeCases:
    """Degenerate input must not crash or emit garbage."""

    def test_silence_in_silence_out(self) -> None:
        out = stretch(np.zeros(SAMPLE_RATE, dtype=np.float32), 1.5)
        assert len(out) > 0
        assert np.all(out == 0.0)

    def test_input_shorter_than_one_frame(self) -> None:
        out = stretch(_tone(220.0, 0.01), 1.5)
        assert len(out) > 0
        assert np.all(np.isfinite(out))

    def test_empty_input(self) -> None:
        assert len(stretch(np.zeros(0, dtype=np.float32), 1.5)) == 0

    def test_output_is_finite_and_float32(self) -> None:
        out = stretch(_tone(220.0, 0.5), 1.5)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))

    def test_output_amplitude_is_preserved(self) -> None:
        """Overlap-add must reconstruct at unity gain, not attenuate."""
        audio = _tone(220.0, 1.0)
        out = stretch(audio, 1.5)
        # Skip the ramped edges; the interior should carry the same level.
        interior = out[len(out) // 4 : -len(out) // 4]
        assert float(np.max(np.abs(interior))) == pytest.approx(0.5, abs=0.05)

    @pytest.mark.parametrize("speed", [0.0, -1.0, MIN_SPEED / 2, MAX_SPEED * 2])
    def test_out_of_range_speed_is_rejected(self, speed: float) -> None:
        with pytest.raises(ValueError, match="speed must be between"):
            TimeStretcher(speed)

    @pytest.mark.parametrize("frame_len", [0, 1, 3, 1023])
    def test_invalid_frame_len_is_rejected(self, frame_len: int) -> None:
        with pytest.raises(ValueError, match="frame_len must be an even number"):
            TimeStretcher(1.5, frame_len=frame_len)

    def test_negative_search_radius_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="search_radius must be"):
            TimeStretcher(1.5, search_radius=-1)

    def test_zero_search_radius_still_stretches(self) -> None:
        """Without a search the output is a plain overlap-add, but the ratio holds."""
        audio = _tone(220.0, 0.5)
        out = stretch(audio, 1.5, search_radius=0)
        assert abs(len(out) - len(audio) / 1.5) / (len(audio) / 1.5) < 0.05
