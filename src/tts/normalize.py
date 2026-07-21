"""Boost-only LUFS loudness normalization for buffered and streamed audio chunks."""

import math
from collections.abc import Iterator

import numpy as np
import pyloudnorm as pyln
from scipy.signal import resample_poly


def normalize_chunks(
    chunks: list[np.ndarray],
    sample_rate: int,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    meter: pyln.Meter,
) -> list[np.ndarray]:
    """Apply boost-only LUFS normalization to a list of audio chunks.

    Measures integrated loudness of the concatenated audio using ITU-R BS.1770-4.
    If the measured loudness is below target_lufs, applies a positive gain to
    bring it up, capped so the resulting true peak (measured via 4x oversampling)
    does not exceed true_peak_ceiling_db. Never attenuates. Returns the chunks
    unchanged if the audio is shorter than min_duration_seconds, silent, or
    already at or above the target.

    Args:
        chunks: List of float32 audio chunks.
        sample_rate: Sample rate in Hz.
        target_lufs: Target integrated loudness in LUFS (e.g. -20.0).
        true_peak_ceiling_db: Maximum allowed true-peak level in dBFS (e.g. -1.0).
        min_duration_seconds: Minimum duration to attempt normalization (shorter
            utterances are returned unchanged).
        meter: Pre-constructed pyloudnorm Meter matching sample_rate.

    Returns:
        List of chunks with identical lengths and dtype. Either unchanged
        (passthrough) or scaled by a single scalar gain.
    """
    if not chunks:
        return chunks

    lens = [len(c) for c in chunks]
    audio = np.concatenate(chunks).astype(np.float32, copy=True)

    scalar = boost_gain(audio, sample_rate, target_lufs, true_peak_ceiling_db, min_duration_seconds, meter)
    if scalar == 1.0:
        return chunks

    audio *= scalar

    split_idx = np.cumsum(lens[:-1])
    out = np.split(audio, split_idx)
    return [part.astype(np.float32, copy=False) for part in out]


def boost_gain(
    audio: np.ndarray,
    sample_rate: int,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    meter: pyln.Meter,
) -> float:
    """Compute the boost-only, true-peak-capped linear gain for an audio buffer.

    Measures integrated loudness (ITU-R BS.1770-4). If below target_lufs, returns
    the positive linear gain that lifts it toward the target, capped so the
    4x-oversampled true peak stays under true_peak_ceiling_db. Returns 1.0 (no
    change) when the audio is shorter than min_duration_seconds, silent, already
    at/above target, or the peak leaves no headroom — mirroring the buffered
    normalize_chunks passthrough cases. Never attenuates (result is always >= 1.0).

    Args:
        audio: Float32 mono audio buffer.
        sample_rate: Sample rate in Hz.
        target_lufs: Target integrated loudness in LUFS.
        true_peak_ceiling_db: Maximum allowed true-peak level in dBFS.
        min_duration_seconds: Minimum duration to attempt normalization.
        meter: Pre-constructed pyloudnorm Meter matching sample_rate.

    Returns:
        Linear gain scalar (>= 1.0); 1.0 means leave the audio unchanged.
    """
    if len(audio) < int(min_duration_seconds * sample_rate):
        return 1.0

    if float(np.max(np.abs(audio))) == 0.0:
        return 1.0

    integrated = float(meter.integrated_loudness(audio))
    if math.isinf(integrated) or math.isnan(integrated):
        return 1.0

    if integrated >= target_lufs:
        return 1.0

    gain_wanted_db = target_lufs - integrated

    oversampled = resample_poly(audio, up=4, down=1)
    peak = float(np.max(np.abs(oversampled)))
    if peak <= 0.0:
        return 1.0
    tp_db = 20.0 * math.log10(peak)

    gain_max_db = true_peak_ceiling_db - tp_db
    if gain_max_db <= 0.0:
        return 1.0

    gain_db = min(gain_wanted_db, gain_max_db)
    if gain_db <= 0.0:
        return 1.0

    return float(10.0 ** (gain_db / 20.0))


def normalize_stream(
    chunks: Iterator[np.ndarray],
    sample_rate: int,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    warmup_seconds: float,
    meter: pyln.Meter,
) -> Iterator[np.ndarray]:
    """Boost-only loudness normalization for a streamed chunk iterator.

    Whole-signal LUFS can't be measured before playback in streaming mode, so a
    single gain is measured on a warm-up window: the first ``warmup_seconds`` of
    audio are buffered, one boost-only + true-peak-capped gain is computed on that
    window (see boost_gain), and that gain is applied to the buffered chunks and
    every subsequent chunk. Because the gain is capped on the warm-up window only,
    later (possibly louder) chunks are hard-limited to the true-peak ceiling to
    avoid clipping. When no boost is warranted (gain == 1.0) chunks pass through
    untouched. The only added latency is buffering the warm-up window (a few
    hundred ms), far less than generating the whole utterance.

    Args:
        chunks: Iterator of float32 audio chunks as generated.
        sample_rate: Sample rate in Hz.
        target_lufs: Target integrated loudness in LUFS.
        true_peak_ceiling_db: Maximum true-peak level in dBFS after gain.
        min_duration_seconds: Minimum warm-up length to attempt normalization.
        warmup_seconds: Seconds of audio to buffer before measuring the gain.
        meter: Pre-constructed pyloudnorm Meter matching sample_rate.

    Yields:
        Chunks scaled by the measured gain (and peak-limited), or unchanged.
    """
    warmup_frames = int(warmup_seconds * sample_rate)
    ceiling_lin = float(10.0 ** (true_peak_ceiling_db / 20.0))
    buffer: list[np.ndarray] = []
    buffered_samples = 0
    gain: float | None = None

    for chunk in chunks:
        if gain is not None:
            yield _scale_and_limit(chunk, gain, ceiling_lin)
            continue

        buffer.append(chunk)
        buffered_samples += len(chunk)
        if buffered_samples >= warmup_frames:
            gain = boost_gain(
                np.concatenate(buffer),
                sample_rate,
                target_lufs,
                true_peak_ceiling_db,
                min_duration_seconds,
                meter,
            )
            for buffered_chunk in buffer:
                yield _scale_and_limit(buffered_chunk, gain, ceiling_lin)
            buffer = []

    if gain is None and buffer:
        # Utterance ended before the warm-up window filled; measure on what we have.
        gain = boost_gain(
            np.concatenate(buffer),
            sample_rate,
            target_lufs,
            true_peak_ceiling_db,
            min_duration_seconds,
            meter,
        )
        for buffered_chunk in buffer:
            yield _scale_and_limit(buffered_chunk, gain, ceiling_lin)


def _scale_and_limit(chunk: np.ndarray, gain: float, ceiling_lin: float) -> np.ndarray:
    """Apply a linear gain to a chunk and hard-limit to the true-peak ceiling.

    Returns the chunk unchanged when gain is 1.0 (passthrough), matching the
    buffered path's "never touch audio that needs no boost" behavior.
    """
    if gain == 1.0:
        return chunk
    scaled = (chunk * np.float32(gain)).astype(np.float32, copy=False)
    np.clip(scaled, -ceiling_lin, ceiling_lin, out=scaled)
    return scaled
