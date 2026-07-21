"""Background TTS worker: streaming and buffered generation-and-playback loops."""

import dataclasses
import queue
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
from mlx_audio.tts.utils import load

from src.tts.generate import generate_chunks, iter_stream_chunks
from src.tts.normalize import normalize_chunks, normalize_stream
from src.tts.player import AudioPlayer, PlaybackJob, play_stream
from src.tts.protocols import TTSModel


@dataclasses.dataclass(frozen=True)
class AudioSettings:
    """Audio generation, normalization, and playback settings for the worker.

    Groups the values that are otherwise threaded through every worker function,
    so the worker API stays small. Built once by the CLI (from CliConfig) and the
    server (from ServerState) and passed down unchanged.

    Attributes:
        sample_rate: Sample rate in Hz.
        lead_silence_ms: Silence written after each audio stream open/reopen.
        normalize_audio: Whether to apply boost-only LUFS normalization (both modes).
        target_lufs: Target integrated loudness in LUFS when normalization is enabled.
        true_peak_ceiling_db: Maximum true-peak level in dBFS after gain.
        min_duration_seconds: Minimum utterance length to attempt normalization.
        meter: Pre-constructed pyloudnorm Meter matching sample_rate.
        stream: Whether to stream playback within each utterance (low latency).
        streaming_interval: Approximate seconds of audio per streamed chunk.
        streaming_warmup_seconds: Seconds buffered to measure the streaming
            normalization gain (see normalize_stream).
    """

    sample_rate: int
    lead_silence_ms: int
    normalize_audio: bool
    target_lufs: float
    true_peak_ceiling_db: float
    min_duration_seconds: float
    meter: pyln.Meter
    stream: bool
    streaming_interval: float
    streaming_warmup_seconds: float


def _generate_worker_chunks(model: TTSModel, text: str, voice: str, settings: AudioSettings) -> list[np.ndarray] | None:
    try:
        generated = generate_chunks(model, text, voice)
        if settings.normalize_audio and generated:
            generated = normalize_chunks(
                generated,
                settings.sample_rate,
                settings.target_lufs,
                settings.true_peak_ceiling_db,
                settings.min_duration_seconds,
                settings.meter,
            )
        return generated
    except (RuntimeError, ValueError) as exc:
        print(f"\n  Error: {exc}", file=sys.stderr)
        return None


def _submit_worker_playback(
    player: AudioPlayer,
    chunks: list[np.ndarray],
    output_path: Path | None,
) -> threading.Event:
    done = threading.Event()

    def on_error(exc: Exception) -> None:
        print(f"\n  Error: {exc}", file=sys.stderr)
        done.set()

    player.submit(
        PlaybackJob(
            chunks=chunks,
            output_path=output_path,
            on_complete=lambda _path: done.set(),
            on_error=on_error,
        )
    )
    return done


def _cli_stream_callbacks(
    done: threading.Event,
) -> tuple[Callable[[Path | None], None], Callable[[Exception], None]]:
    """Build on_complete/on_error callbacks for one streamed CLI utterance.

    Taking ``done`` as a parameter binds it explicitly, so the callbacks do not
    capture a reassigned loop variable (see the per-utterance loop below).
    """

    def on_complete(_path: Path | None) -> None:
        done.set()

    def on_error(exc: Exception) -> None:
        print(f"\n  Error: {exc}", file=sys.stderr)
        done.set()

    return on_complete, on_error


def streaming_chunk_iter(model: TTSModel, text: str, voice: str, settings: AudioSettings) -> Iterator[np.ndarray]:
    """Build the chunk iterator for streaming playback, optionally normalized.

    When settings.normalize_audio is set, the raw generation stream is wrapped
    with normalize_stream (warm-up-window loudness normalization), so streamed
    audio matches the buffered path's loudness without waiting for the whole
    utterance.
    """
    chunks = iter_stream_chunks(model, text, voice, settings.streaming_interval)
    if settings.normalize_audio:
        chunks = normalize_stream(
            chunks,
            settings.sample_rate,
            settings.target_lufs,
            settings.true_peak_ceiling_db,
            settings.min_duration_seconds,
            settings.streaming_warmup_seconds,
            settings.meter,
        )
    return chunks


def _run_streaming_worker(
    work_queue: queue.Queue[str | None],
    model: TTSModel,
    voice: str,
    output_path: Path | None,
    player: AudioPlayer,
    settings: AudioSettings,
) -> None:
    """Process the work queue in streaming mode: play each utterance as it generates.

    Each message is generated and streamed to the player chunk-by-chunk, so audio
    starts after the first chunk rather than after the whole utterance. Messages
    are handled serially (the within-utterance streaming is where the latency win
    is). When normalize_audio is set, warm-up-window loudness normalization is
    applied (see normalize_stream) so streamed loudness matches the buffered path.
    """
    while True:
        text = work_queue.get()
        if text is None:
            break

        done = threading.Event()
        on_complete, on_error = _cli_stream_callbacks(done)

        try:
            play_stream(
                player,
                streaming_chunk_iter(model, text, voice, settings),
                output_path,
                on_complete,
                on_error,
            )
        except (RuntimeError, ValueError) as exc:
            print(f"\n  Error: {exc}", file=sys.stderr)
            done.set()

        done.wait()
        work_queue.task_done()


def _run_buffered_worker(
    work_queue: queue.Queue[str | None],
    model: TTSModel,
    voice: str,
    output_path: Path | None,
    player: AudioPlayer,
    settings: AudioSettings,
) -> None:
    """Process the work queue in buffered mode with cross-utterance lookahead.

    Generates the full utterance (and optional loudness normalization) before
    playback, generating the next message while the current one is still playing.
    """
    pending_chunks: list[np.ndarray] | None = None
    playback_done: threading.Event | None = None

    while True:
        if pending_chunks is not None:
            if playback_done is not None:
                playback_done.wait()
            chunks_to_play = pending_chunks
            pending_chunks = None
            playback_done = _submit_worker_playback(player, chunks_to_play, output_path)

        text = work_queue.get()
        if text is None:
            if playback_done is not None:
                playback_done.wait()
            break

        generated = _generate_worker_chunks(model, text, voice, settings)
        if generated is not None:
            pending_chunks = generated
        work_queue.task_done()

    if pending_chunks is not None:
        if playback_done is not None:
            playback_done.wait()
        playback_done = _submit_worker_playback(player, pending_chunks, output_path)
        playback_done.wait()


def audio_worker(
    work_queue: queue.Queue[str | None],
    model: TTSModel,
    voice: str,
    output_path: Path | None,
    settings: AudioSettings,
) -> None:
    """Background worker that generates and plays TTS audio.

    In streaming mode (settings.stream=True) each utterance is played
    chunk-by-chunk as it generates, for low latency. In buffered mode it generates
    the full utterance (with loudness normalization) before playback, generating
    the next message while the current one plays so there is no gap between
    sentences.

    Args:
        work_queue: Queue of text strings to synthesize. None signals shutdown.
        model: Loaded TTS model.
        voice: Voice to use for synthesis.
        output_path: Path to save generated audio, or None to skip saving.
        settings: Audio generation, normalization, and playback settings.
    """
    player = AudioPlayer(settings.sample_rate, settings.lead_silence_ms)
    try:
        if settings.stream:
            _run_streaming_worker(work_queue, model, voice, output_path, player, settings)
        else:
            _run_buffered_worker(work_queue, model, voice, output_path, player, settings)
    finally:
        player.close()


def audio_worker_from_model_id(
    work_queue: queue.Queue[str | None],
    model_id: str,
    voice: str,
    output_path: Path | None,
    settings: AudioSettings,
    ready_queue: queue.Queue[BaseException | None] | None,
) -> None:
    """Load the model in the worker thread, then process queued TTS work.

    MLX GPU streams are thread-local. Loading the model and calling generate on
    different Python threads can raise "no Stream(gpu, N) in current thread".
    """
    try:
        model = load(model_id)
        if not hasattr(model, "generate") or model.generate is None:
            msg = f"Model {model_id} does not support generation"
            raise RuntimeError(msg)
    except BaseException as exc:
        if ready_queue is None:
            raise
        ready_queue.put(exc)
        return

    if ready_queue is not None:
        ready_queue.put(None)

    audio_worker(work_queue, model, voice, output_path, settings)
