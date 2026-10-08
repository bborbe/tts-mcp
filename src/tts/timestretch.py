"""Pitch-preserving time stretching (WSOLA) for playback speed control.

Changing playback speed by resampling changes pitch with it: a 1.5x resample
turns a voice into a chipmunk. Keeping the pitch while changing the duration
needs a time-stretch, which this module provides via WSOLA (Waveform Similarity
Overlap-Add).

WSOLA reconstructs the signal from overlapping frames, but instead of taking
each frame from a fixed analysis position it searches a small window around the
nominal position for the segment that best continues the audio already emitted.
Matching the waveform at the seam is what keeps the result free of the clicks
and phase artefacts a naive overlap-add produces.

The implementation is streaming: `TimeStretcher.feed` accepts audio in chunks
and returns whatever stretched audio is ready, so the server's low-latency
streaming playback path keeps working — the only cost is a small algorithmic
latency (one frame plus the search radius, ~50ms at the default settings),
because a frame can only be emitted once its search window is available.
"""

from __future__ import annotations

from typing import cast

import numpy as np

DEFAULT_FRAME_LEN: int = 1024
"""Frame length in samples. At 24kHz this is ~43ms — long enough to hold a
pitch period of a low voice, short enough that the search stays cheap."""

DEFAULT_SEARCH_RADIUS: int = 256
"""How far (in samples) around the nominal analysis position to search for the
best-matching segment. ~10ms at 24kHz: wide enough to find a waveform match,
narrow enough that the result does not sound like it is skipping."""

MIN_SPEED: float = 0.25
MAX_SPEED: float = 4.0
"""Accepted range for the speed factor. Outside it the output degrades into
audible repetition or skipping, so an out-of-range value is rejected rather
than silently producing bad audio."""


def _periodic_hann(length: int) -> np.ndarray:
    """Return a periodic Hann window of the given length.

    Periodic (not symmetric) matters: with a 50% synthesis hop, adjacent
    periodic Hann windows sum to exactly 1.0, so overlap-add reconstructs the
    signal at unity gain and no extra normalization pass is needed. The
    symmetric ``np.hanning`` variant does not sum to a constant.

    Args:
        length: Window length in samples.

    Returns:
        float32 window of ``length`` samples, peaking at 1.0.
    """
    n = np.arange(length, dtype=np.float32)
    return (0.5 * (1.0 - np.cos(2.0 * np.pi * n / length))).astype(np.float32)


class TimeStretcher:
    """Streaming WSOLA time stretcher.

    Feed audio in with ``feed`` and collect the stretched output; call
    ``flush`` once at the end of the utterance to drain the tail. Output is
    returned in whatever sizes the internal frame grid produces — callers must
    not assume a chunk in produces a chunk out.

    A stretcher instance serves exactly one utterance: its overlap-add state
    carries across chunks, so reusing one for a second utterance would smear
    the boundary between them. Create a new instance per utterance.

    Attributes:
        speed: Stretch factor. >1.0 is faster (shorter output), <1.0 slower.
        frame_len: Frame length in samples.
        search_radius: Waveform-similarity search radius in samples.
    """

    def __init__(
        self,
        speed: float,
        frame_len: int = DEFAULT_FRAME_LEN,
        search_radius: int = DEFAULT_SEARCH_RADIUS,
    ) -> None:
        """Initialize the stretcher.

        Args:
            speed: Stretch factor. 1.0 passes audio through unchanged. Must be
                within [MIN_SPEED, MAX_SPEED].
            frame_len: Frame length in samples. Must be even and at least 2.
            search_radius: Waveform-similarity search radius in samples. Must
                be >= 0.

        Raises:
            ValueError: If speed is outside the accepted range, or the frame
                geometry is unusable.
        """
        if not MIN_SPEED <= speed <= MAX_SPEED:
            msg = f"speed must be between {MIN_SPEED} and {MAX_SPEED}, got {speed}"
            raise ValueError(msg)
        if frame_len < 2 or frame_len % 2 != 0:
            msg = f"frame_len must be an even number >= 2, got {frame_len}"
            raise ValueError(msg)
        if search_radius < 0:
            msg = f"search_radius must be >= 0, got {search_radius}"
            raise ValueError(msg)

        self.speed = float(speed)
        self.frame_len = frame_len
        self.search_radius = search_radius

        self._synthesis_hop = frame_len // 2
        # Each frame emits a fixed synthesis hop of output but consumes a
        # variable analysis hop of input; consuming *more* input per frame is
        # what makes the result shorter and therefore faster. Rounding the hop
        # is what makes the ratio approximate: at 1.1x the hop is 563 instead
        # of 563.2, so the realized speed is 0.04% off. Inaudible, and the
        # alternative — carrying a fractional position and interpolating —
        # would resample and cost the pitch accuracy this module exists for.
        self._analysis_hop = max(1, round(self._synthesis_hop * self.speed))
        self._overlap = frame_len - self._synthesis_hop
        self._window = _periodic_hann(frame_len)

        self._buffer = np.zeros(0, dtype=np.float32)
        """Input samples not yet consumed, starting at absolute index
        ``_buffer_start``."""

        self._buffer_start = 0
        """Absolute index of ``_buffer[0]``, so positions stay meaningful after
        consumed input is dropped."""

        self._input_samples = 0
        """Total real input samples fed so far, excluding flush padding. The
        frame count is derived from this, so silence padded on at the end can
        never be mistaken for audio and stretched into extra frames."""

        self._frames_emitted = 0
        """Number of output frames produced so far, i.e. the index of the next
        one."""

        self._previous_start: int | None = None
        """Absolute start index of the previous frame's segment, or None before
        the first frame. Used to derive the natural continuation the next frame
        is matched against."""

        self._pending: np.ndarray | None = None
        """Tail of the previous output frame that has not been returned yet.
        Frames are emitted on a synthesis-hop grid, so each frame's first half
        still belongs to the frame before it."""

        self._flushed = False

    def _emit_frame(self) -> np.ndarray:
        """Produce the next output frame and return the newly finished samples.

        The frame is placed on the synthesis-hop grid and overlap-added onto the
        carry-over tail from the previous frame, so the returned samples are
        final: nothing later will add to them.

        Returns:
            The synthesis-hop worth of finished output samples.
        """
        index = self._frames_emitted
        nominal = index * self._analysis_hop
        start = nominal if index == 0 or self._previous_start is None else nominal + self._best_offset(nominal)

        segment = self._buffer[start - self._buffer_start : start - self._buffer_start + self.frame_len]
        if len(segment) < self.frame_len:
            segment = np.pad(segment, (0, self.frame_len - len(segment)))

        windowed = segment * self._window
        if self._pending is None:
            # First frame: the whole window is new output, and only its first
            # synthesis hop is finished (the rest waits for the next frame's
            # overlap-add).
            self._pending = windowed.copy()
        else:
            # Overlap-add onto the tail carried from the previous frame. The
            # first synthesis hop of `windowed` lands on that tail and is final.
            self._pending[: self._synthesis_hop] += windowed[: self._synthesis_hop]
            self._pending = np.concatenate((self._pending, windowed[self._synthesis_hop :]))

        self._frames_emitted += 1
        self._previous_start = start

        finished = self._pending[: self._synthesis_hop]
        self._pending = self._pending[self._synthesis_hop :]
        return finished

    def _best_offset(self, nominal: int) -> int:
        """Find the search offset whose segment best continues the previous one.

        Correlates candidate segments around ``nominal`` against the segment
        that would naturally follow the previous frame, and returns the offset
        with the highest normalized correlation. Normalizing matters: without
        it the loudest candidate always wins, which is not the same as the most
        similar one.

        Args:
            nominal: Nominal analysis position of the frame being built.

        Returns:
            Offset in samples, within +/- ``search_radius`` of ``nominal``,
            clamped so the candidate segment lies inside the buffer.
        """
        radius = self.search_radius
        previous_start = self._previous_start
        if radius == 0 or previous_start is None:
            return 0

        # The natural continuation is what followed the previous frame's first
        # synthesis hop — i.e. the audio the previous frame's second half was
        # built from. Matching against it keeps the waveform continuous.
        continuation_start = previous_start + self._synthesis_hop - self._buffer_start
        continuation = self._buffer[continuation_start : continuation_start + self._overlap]
        if len(continuation) < self._overlap:
            return 0
        continuation_norm = float(np.linalg.norm(continuation))
        if continuation_norm == 0.0:
            return 0

        lowest = max(-radius, self._buffer_start - nominal)
        highest = min(radius, self._buffer_start + len(self._buffer) - self.frame_len - nominal)
        if highest < lowest:
            return 0

        offsets = np.arange(lowest, highest + 1)
        first = nominal + lowest - self._buffer_start
        last = nominal + highest - self._buffer_start + self._overlap
        candidates = np.lib.stride_tricks.sliding_window_view(self._buffer[first:last], self._overlap)

        scores = candidates @ continuation
        norms = np.linalg.norm(candidates, axis=1) * continuation_norm
        # Silent candidates carry no similarity information; scoring them 0
        # keeps them out of the argmax without a division by zero.
        normalized = np.divide(scores, norms, out=np.zeros_like(scores), where=norms > 0)
        return int(offsets[int(np.argmax(normalized))])

    def _max_frames(self) -> int:
        """Return how many output frames the utterance fed so far needs.

        Each frame emits one synthesis hop and consumes one analysis hop, so
        ``input / analysis_hop`` frames reproduce the input. One frame fewer is
        right: the final frame's window tail is emitted on top of the last hop
        (``frame_len - synthesis_hop`` samples), and dropping one frame is what
        keeps the total at ``input / speed`` rather than overshooting it by that
        tail.

        Returns:
            The frame count, or 0 when no audio has been fed.
        """
        if self._input_samples == 0:
            return 0
        return max(1, round(self._input_samples / self._analysis_hop - 1))

    def _frame_is_ready(self, index: int) -> bool:
        """Report whether the frame at ``index`` can be built from the buffer.

        A frame needs its whole segment plus the full search window on both
        sides, so the buffer must reach ``nominal + radius + frame_len``.

        Args:
            index: Index of the frame to test.

        Returns:
            True when enough input is buffered.
        """
        nominal = index * self._analysis_hop
        needed_end = nominal + self.search_radius + self.frame_len
        return needed_end <= self._buffer_start + len(self._buffer)

    def _drain_ready_frames(self) -> np.ndarray:
        """Emit every frame the buffer can currently support.

        Returns:
            The finished output samples, possibly empty.
        """
        produced: list[np.ndarray] = []
        while self._frames_emitted < self._max_frames() and self._frame_is_ready(self._frames_emitted):
            produced.append(self._emit_frame())
            self._discard_consumed_input()
        if not produced:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(produced)

    def _discard_consumed_input(self) -> None:
        """Drop buffered input that no later frame can reach.

        The search window reaches back ``search_radius`` from the current
        nominal position, so everything before that is dead weight. Dropping it
        keeps memory flat over a long utterance instead of holding the whole
        input.
        """
        if self._previous_start is None:
            return
        keep_from = self._previous_start - self.search_radius
        drop = keep_from - self._buffer_start
        if drop > 0:
            self._buffer = self._buffer[drop:]
            self._buffer_start = keep_from

    def feed(self, chunk: np.ndarray) -> np.ndarray:
        """Add input audio and return whatever stretched audio is ready.

        Args:
            chunk: Input samples, mono float32 (or convertible).

        Returns:
            Stretched samples that are final. May be empty, and is not
            proportional to the input size.

        Raises:
            RuntimeError: If called after ``flush``.
        """
        if self._flushed:
            msg = "feed() called after flush(); a TimeStretcher serves one utterance"
            raise RuntimeError(msg)
        if chunk.size == 0:
            return np.zeros(0, dtype=np.float32)

        samples = chunk.reshape(-1).astype(np.float32, copy=False)
        self._input_samples += len(samples)
        self._buffer = np.concatenate((self._buffer, samples))
        return self._drain_ready_frames()

    def flush(self) -> np.ndarray:
        """Drain the tail of the utterance and finish the stretcher.

        Pads the input with silence so the remaining frames can be built, then
        returns the last output samples. Calling this more than once returns
        empty audio — the tail is emitted exactly once.

        Returns:
            The final stretched samples, possibly empty.
        """
        if self._flushed:
            return np.zeros(0, dtype=np.float32)
        self._flushed = True

        # Silence padding lets the frames that reach past the end of the input
        # be built; they fade out through the window rather than cutting off.
        # One synthesis hop extra covers the frame grid ending between two hops.
        padding = self.search_radius + self.frame_len + self._synthesis_hop
        self._buffer = np.concatenate((self._buffer, np.zeros(padding, dtype=np.float32)))

        produced: list[np.ndarray] = []
        # The loop must run at least once even when feed() emitted no frame —
        # an utterance shorter than one frame arrives here with nothing pending,
        # and its single frame still has to be produced. The frame count is
        # bounded so the padding below is never stretched into extra audio.
        while self._frames_emitted < self._max_frames():
            if not self._frame_is_ready(self._frames_emitted):
                break
            produced.append(self._emit_frame())

        tail = self._pending
        self._pending = None
        if tail is not None and len(tail) > 0:
            produced.append(tail)

        if not produced:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(produced)


def speed_from_config(config: dict[str, object]) -> float:
    """Read the playback speed from the parsed config.yaml mapping.

    The key is optional so that a config written before this feature existed
    keeps working: an absent ``speed`` means 1.0, i.e. no stretching and no
    change in behaviour. A key that is present must be a number in range — a
    speed that cannot be honoured is a mistake, not a request for the default.

    Args:
        config: The parsed config.yaml mapping.

    Returns:
        The configured speed, or 1.0 when the key is absent.

    Raises:
        ValueError: If the key is present but not a number, or is out of range.
    """
    raw = config.get("speed")
    if raw is None:
        return 1.0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        msg = f"config speed: must be a number, got {raw!r}"
        raise ValueError(msg)
    speed = float(cast(float, raw))
    if not MIN_SPEED <= speed <= MAX_SPEED:
        msg = f"config speed: must be between {MIN_SPEED} and {MAX_SPEED}, got {speed}"
        raise ValueError(msg)
    return speed


def stretch(audio: np.ndarray, speed: float, frame_len: int = DEFAULT_FRAME_LEN, search_radius: int = DEFAULT_SEARCH_RADIUS) -> np.ndarray:
    """Time-stretch a complete buffer in one call.

    Convenience wrapper around :class:`TimeStretcher` for callers that already
    hold the whole utterance. Returns the input unchanged at speed 1.0, so a
    bypass costs nothing.

    Args:
        audio: Input samples, mono.
        speed: Stretch factor. 1.0 returns the input unchanged.
        frame_len: Frame length in samples.
        search_radius: Waveform-similarity search radius in samples.

    Returns:
        Stretched samples as float32.

    Raises:
        ValueError: If speed is outside the accepted range.
    """
    if speed == 1.0:
        return audio.astype(np.float32, copy=False)

    stretcher = TimeStretcher(speed, frame_len=frame_len, search_radius=search_radius)
    out = stretcher.feed(audio)
    tail = stretcher.flush()
    if len(tail) == 0:
        return out
    if len(out) == 0:
        return tail
    return np.concatenate((out, tail))
