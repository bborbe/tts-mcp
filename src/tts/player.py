"""Audio playback and saving: warm-stream AudioPlayer, playback jobs, and file writing."""

import dataclasses
import queue
import sys
import threading
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import numpy as np
import sounddevice as sd

from src.tts.protocols import AudioOutputStream

WRITE_SLICE_SECONDS: float = 0.1
"""Audio written to the output stream per blocking write.

A blocking ``stream.write`` returns only once the device has taken the audio, so
the slice length is also the worst-case delay between a cancel request and
playback actually stopping. Small enough to feel instant, large enough that the
per-write overhead stays negligible.
"""


def _refresh_audio_devices() -> None:
    """Re-enumerate audio devices so the next stream opens on the current default output.

    PortAudio snapshots the device list when sounddevice is first initialized. A long-lived
    server process therefore keeps targeting whatever output device was default at startup, and
    every stream open after the user switches devices fails with CoreAudio error -10851
    ("Invalid Property Value"). Tearing down and re-initializing PortAudio refreshes the device
    list so the next open picks up the current default without restarting the process.

    sounddevice exposes PortAudio's teardown/re-init hooks only under underscore-prefixed names
    (its documented recipe for refreshing the device list), so the module is reached through a
    dynamically-typed reference here.
    """
    # Logged so an unexpected reopen is visible in the server log: during steady
    # use this should appear once (server startup) and again only on a device change.
    print("audio: re-initializing PortAudio for a (re)opened output stream", file=sys.stderr)
    sounddevice_module: Any = sd
    sounddevice_module._terminate()
    sounddevice_module._initialize()


def _write_lead_silence(stream: AudioOutputStream, sample_rate: int, lead_silence_ms: int) -> None:
    if lead_silence_ms < 0:
        msg = f"lead_silence_ms must be >= 0, got {lead_silence_ms}"
        raise ValueError(msg)

    silence_frames = int(sample_rate * lead_silence_ms / 1000)
    if silence_frames == 0:
        return

    stream.write(np.zeros((silence_frames, 1), dtype=np.float32))


def save_audio(audio: np.ndarray, output_path: Path, sample_rate: int) -> None:
    """Save audio samples to a WAV file.

    Args:
        audio: Audio samples as a numpy array.
        output_path: Path to save the WAV file.
        sample_rate: Sample rate in Hz.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    audio_int16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(output_path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())


def play_audio(audio: np.ndarray, sample_rate: int) -> None:
    """Play audio samples through the default audio device.

    Args:
        audio: Audio samples as a numpy array.
        sample_rate: Sample rate in Hz.
    """
    sd.play(audio, sample_rate)
    sd.wait()


@dataclasses.dataclass(frozen=True)
class PlaybackJob:
    """One playback request for the persistent audio player.

    Exactly one terminal callback fires per job: ``on_complete`` when the audio
    played out, ``on_error`` when playback failed, ``on_cancel`` when ``cancel``
    was set before the job finished. A cancelled job writes no WAV file, because
    the recording would hold audio the listener never heard.

    Attributes:
        chunks: Fully generated audio for this utterance.
        output_path: Path to save the utterance to, or None to skip saving.
        on_complete: Called with output_path when playback finishes.
        on_error: Called if playback fails.
        on_cancel: Called instead of on_complete when the job is cancelled. When
            it is None, a cancelled job reports through ``on_complete(None)`` so
            a waiting caller is never left hanging.
        cancel: Set by the submitter to stop this job part-way. Owned per job,
            so cancelling the utterance that is playing now can never leak into
            the one that starts next.
        pause: Set by the submitter to hold this job mid-utterance. Playback
            stops at the next slice boundary and resumes from that point when
            the event is cleared. A paused job still honours ``cancel`` — pause
            never blocks cancellation. Owned per job, like cancel.
    """

    chunks: list[np.ndarray]
    output_path: Path | None
    on_complete: Callable[[Path | None], None] | None = None
    on_error: Callable[[Exception], None] | None = None
    on_cancel: Callable[[], None] | None = None
    cancel: threading.Event | None = None
    pause: threading.Event | None = None


@dataclasses.dataclass(frozen=True)
class StreamingPlaybackJob:
    """A streaming playback request whose chunks arrive incrementally.

    The generation thread pushes chunks onto ``chunk_source`` as the model
    produces them and pushes ``None`` to signal end of utterance; the player
    thread writes each chunk to the warm output stream as it arrives. This
    starts playback after the first chunk instead of buffering the whole
    utterance first (see PlaybackJob for the buffered variant).

    Cancellation works as it does for PlaybackJob: exactly one terminal callback
    fires, and a cancelled job saves no WAV. The player stops consuming
    ``chunk_source`` immediately; the producer notices the same event and stops
    generating (see play_stream), so no one waits for the abandoned queue.

    Pause works as it does for PlaybackJob: while ``pause`` is set the player
    holds between write slices and resumes from the same point when it clears.
    The producer keeps generating into the unbounded queue while paused (see
    play_stream); for the pauses this feature serves — a meeting, a question —
    the queue growth is negligible, and a cancel still interrupts a pause.
    """

    chunk_source: "queue.Queue[np.ndarray | None]"
    output_path: Path | None
    on_complete: Callable[[Path | None], None] | None = None
    on_error: Callable[[Exception], None] | None = None
    on_cancel: Callable[[], None] | None = None
    cancel: threading.Event | None = None
    pause: threading.Event | None = None


class AudioPlayer:
    """Serial audio player that keeps one output stream warm across utterances.

    A single stream is opened on the current default output device and reused for
    subsequent utterances, so PortAudio is only re-initialized when the stream is
    (re)opened — not on every utterance. Re-initializing PortAudio repeatedly
    (sd._terminate/_initialize) degrades the CoreAudio HAL over time and produces
    distorted playback, so it is done sparingly.

    Playback still follows a live default-device switch between two *connected*
    devices (which produces no write error, so nothing would otherwise trigger a
    reopen): before each utterance the current default device ID is read from the
    CoreAudio HAL (default_output_device_id); if it changed since the warm stream
    opened, the stream is closed and reopened on the new device. A device
    disconnect surfaces as a write error and reopens on the next-but-one utterance.
    _write_lead_silence absorbs the per-open CoreAudio startup clip.
    """

    def __init__(self, sample_rate: int, lead_silence_ms: int) -> None:
        """Initialize the persistent audio player.

        Args:
            sample_rate: Audio sample rate in Hz.
            lead_silence_ms: Silence written after each stream open/reopen.

        Raises:
            ValueError: If lead_silence_ms is negative.
        """
        if lead_silence_ms < 0:
            msg = f"lead_silence_ms must be >= 0, got {lead_silence_ms}"
            raise ValueError(msg)

        self._sample_rate = sample_rate
        self._lead_silence_ms = lead_silence_ms
        self._slice_frames = max(1, int(sample_rate * WRITE_SLICE_SECONDS))
        self._jobs: queue.Queue[PlaybackJob | StreamingPlaybackJob | None] = queue.Queue()
        self._unhandled_errors: queue.Queue[Exception] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, job: PlaybackJob) -> None:
        """Queue a buffered playback job for serial playback."""
        self._enqueue(job)

    def submit_stream(self, job: StreamingPlaybackJob) -> None:
        """Queue a streaming playback job whose chunks arrive incrementally."""
        self._enqueue(job)

    def _enqueue(self, job: PlaybackJob | StreamingPlaybackJob) -> None:
        if self._closed:
            msg = "AudioPlayer is closed"
            raise RuntimeError(msg)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._jobs.put(job)

    def close(self) -> None:
        """Drain queued playback and close the persistent stream."""
        self._closed = True
        if self._thread is None:
            return

        self._jobs.join()
        self._jobs.put(None)
        self._thread.join()
        self._thread = None
        if not self._unhandled_errors.empty():
            raise self._unhandled_errors.get()

    def _open_stream(self) -> AudioOutputStream:
        _refresh_audio_devices()
        stream = cast(AudioOutputStream, sd.OutputStream(samplerate=self._sample_rate, channels=1, dtype="float32"))
        stream.start()
        _write_lead_silence(stream, self._sample_rate, self._lead_silence_ms)
        return stream

    def _close_stream(self, stream: AudioOutputStream | None) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def _ensure_stream(self, stream: AudioOutputStream | None) -> AudioOutputStream:
        if stream is not None:
            return stream
        return self._open_stream()

    def _write_chunk(
        self,
        stream: AudioOutputStream,
        chunk: np.ndarray,
        cancel: threading.Event | None,
        pause: threading.Event | None,
    ) -> bool:
        """Write one chunk to the output stream, in cancellable and pausable slices.

        ``stream.write`` blocks until the device has taken the audio, so writing
        a whole chunk at once would make cancellation wait out the chunk (a
        second, at the default streaming_interval). Slicing bounds that wait to
        WRITE_SLICE_SECONDS.

        Pause is checked between slices, so it has the same latency as cancel.
        While ``pause`` is set the player waits instead of writing, and resumes
        from the exact slice boundary when it clears — no position bookkeeping
        needed. ``cancel`` is re-checked inside the pause wait, so pausing never
        delays a cancel.

        Args:
            stream: Warm output stream to write to.
            chunk: Audio samples for this chunk.
            cancel: Cancellation event for the current job, or None.
            pause: Pause event for the current job, or None.

        Returns:
            True if the whole chunk was written, False if cancelled part-way.
        """
        frames = chunk.reshape(-1, 1)
        for start in range(0, len(frames), self._slice_frames):
            if cancel is not None and cancel.is_set():
                return False
            while pause is not None and pause.is_set():
                if cancel is not None and cancel.is_set():
                    return False
                pause.wait(WRITE_SLICE_SECONDS)
            stream.write(frames[start : start + self._slice_frames])
        return True

    def _finish_cancelled(self, job: PlaybackJob | StreamingPlaybackJob) -> None:
        """Report a cancelled job through its terminal callback.

        Falls back to ``on_complete(None)`` when no on_cancel was given, so a
        caller waiting on completion is never left blocked by a cancel it did
        not ask to hear about.
        """
        if job.on_cancel is not None:
            job.on_cancel()
        elif job.on_complete is not None:
            job.on_complete(None)

    def _handle_job(self, stream: AudioOutputStream | None, job: PlaybackJob) -> AudioOutputStream | None:
        """Play one job on the warm stream.

        The stream is kept warm across jobs; the default output device is NOT
        re-queried per utterance. Live default-device switches are handled by
        ``start_output_device_change_watcher``, which restarts the whole process
        for a clean CoreAudio HAL rather than re-initializing PortAudio in place
        (repeated in-process re-init degrades the HAL and distorts playback, and
        the HAL query returns transient/aggregate device ids mid-playback that
        would otherwise trigger spurious per-utterance reopens).

        Returns the stream to reuse for the next job, or None if it was closed due
        to an error (the next job reopens it).
        """
        try:
            stream = self._ensure_stream(stream)
            for chunk in job.chunks:
                if not self._write_chunk(stream, chunk, job.cancel, job.pause):
                    self._finish_cancelled(job)
                    return stream
            if job.output_path is not None:
                audio = np.concatenate(job.chunks)
                save_audio(audio, job.output_path, self._sample_rate)
            if job.on_complete is not None:
                job.on_complete(job.output_path)
            return stream
        except Exception as exc:
            self._fail_playback(stream, exc, job.on_error)
            return None

    def _handle_streaming_job(self, stream: AudioOutputStream | None, job: StreamingPlaybackJob) -> AudioOutputStream | None:
        """Play one streaming job, writing chunks to the warm stream as they arrive.

        Consumes chunks from job.chunk_source (fed by the generation thread) until
        the None sentinel, so the first chunk plays without waiting for the whole
        utterance. The queue is unbounded, so an error here never blocks the
        producer: the generation thread's remaining put() calls just accumulate in
        an abandoned queue that is garbage-collected.

        Returns the stream to reuse for the next job, or None if it was closed due
        to an error (the next job reopens it).
        """
        try:
            stream = self._ensure_stream(stream)
            collected: list[np.ndarray] = []
            while True:
                chunk = job.chunk_source.get()
                if chunk is None:
                    break
                if not self._write_chunk(stream, chunk, job.cancel, job.pause):
                    self._finish_cancelled(job)
                    return stream
                if job.output_path is not None:
                    collected.append(chunk)
            # The producer sends the sentinel as soon as it sees the same cancel
            # event, so the loop can also end on a cancel rather than on a
            # finished utterance — that is still a cancellation, not a completion.
            if job.cancel is not None and job.cancel.is_set():
                self._finish_cancelled(job)
                return stream
            if job.output_path is not None and collected:
                save_audio(np.concatenate(collected), job.output_path, self._sample_rate)
            if job.on_complete is not None:
                job.on_complete(job.output_path)
            return stream
        except Exception as exc:
            self._fail_playback(stream, exc, job.on_error)
            return None

    def _fail_playback(
        self,
        stream: AudioOutputStream | None,
        exc: Exception,
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        """Close the failed stream and report the error, so the next job reopens."""
        close_error: Exception | None = None
        try:
            self._close_stream(stream)
        except Exception as stream_close_error:
            close_error = stream_close_error

        playback_error = exc
        if close_error is not None:
            playback_error = RuntimeError(f"{exc}; additionally failed to close audio stream: {close_error}")

        if on_error is not None:
            on_error(playback_error)
        else:
            self._unhandled_errors.put(playback_error)
        return None

    def _run(self) -> None:
        stream: AudioOutputStream | None = None
        try:
            while True:
                job = self._jobs.get()
                try:
                    if job is None:
                        break
                    if isinstance(job, StreamingPlaybackJob):
                        stream = self._handle_streaming_job(stream, job)
                    else:
                        stream = self._handle_job(stream, job)
                finally:
                    self._jobs.task_done()
        finally:
            try:
                self._close_stream(stream)
            except Exception as exc:
                self._unhandled_errors.put(exc)


def play_chunks(chunks: list[np.ndarray], output_path: Path | None, sample_rate: int, lead_silence_ms: int) -> None:
    """Stream audio chunks to speakers and optionally save to file.

    Args:
        chunks: List of audio chunks as numpy arrays.
        output_path: Path to save the generated WAV file, or None to skip saving.
        sample_rate: Sample rate in Hz.
        lead_silence_ms: Silence written after opening the output stream.
    """
    with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
        audio_stream = cast(AudioOutputStream, stream)
        _write_lead_silence(audio_stream, sample_rate, lead_silence_ms)
        for chunk in chunks:
            audio_stream.write(chunk.reshape(-1, 1))

    if output_path is not None:
        audio = np.concatenate(chunks)
        save_audio(audio, output_path, sample_rate)


def play_stream(
    player: AudioPlayer,
    chunk_iter: Iterator[np.ndarray],
    output_path: Path | None,
    on_complete: Callable[[Path | None], None] | None,
    on_error: Callable[[Exception], None] | None,
    on_cancel: Callable[[], None] | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
) -> None:
    """Feed streamed generation chunks to the player as they are produced.

    Runs on the generation (MLX) thread: it submits a streaming playback job,
    then drives chunk_iter and pushes each chunk to the player's warm stream.
    The None sentinel is always sent (even if generation raises) so the player
    thread never blocks waiting for a chunk that will not come. Returns once
    generation is exhausted; playback of the tail continues on the player thread
    and completion is signalled through on_complete.

    When ``cancel`` is set, generation stops at the next chunk boundary — the
    player has already stopped playing by then, so continuing to drive the model
    would burn GPU time producing audio nobody will hear.

    ``pause`` is forwarded to the player job and honoured between write slices
    on the player thread. Generation itself keeps running while paused (chunks
    accumulate in the unbounded source queue) — for the short pauses this serves
    the queue growth is negligible, and it keeps the model warm for an instant
    resume.

    Args:
        player: Persistent audio player.
        chunk_iter: Iterator yielding audio chunks as they are generated.
        output_path: Path to save the full utterance, or None to skip saving.
        on_complete: Called with output_path when playback finishes.
        on_error: Called if playback fails.
        on_cancel: Called instead of on_complete when the job is cancelled.
        cancel: Event that stops both playback and generation when set.
        pause: Event that holds playback mid-utterance when set.
    """
    source: queue.Queue[np.ndarray | None] = queue.Queue()
    player.submit_stream(
        StreamingPlaybackJob(
            chunk_source=source,
            output_path=output_path,
            on_complete=on_complete,
            on_error=on_error,
            on_cancel=on_cancel,
            cancel=cancel,
            pause=pause,
        )
    )
    try:
        for chunk in chunk_iter:
            if cancel is not None and cancel.is_set():
                break
            source.put(chunk)
    finally:
        source.put(None)
