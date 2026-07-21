"""Shared TTS engine: config, model/voice discovery, audio generation, playback, and saving."""

import ctypes
import ctypes.util
import dataclasses
import datetime
import math
import os
import queue
import re
import sys
import threading
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import pyloudnorm as pyln
import sounddevice as sd
import yaml
from mlx_audio.tts.utils import load
from scipy.signal import resample_poly

OUTPUT_DIR = Path("data/output")

# Config file resolution. In precedence order:
#   1. $TTS_MCP_CONFIG (explicit override)
#   2. $XDG_CONFIG_HOME/tts-mcp/config.yaml (defaults to ~/.config/tts-mcp/config.yaml)
#   3. ./config.yaml in the current working directory (project-root fallback / back-compat)
# Data and model paths inside the config remain relative to the process working
# directory, not to the config file, so moving the config out of the repo does
# not change how `model:` / `models_dir:` resolve.
DEFAULT_CONFIG_PATH = Path("config.yaml")


def config_env_var() -> str:
    """Name of the environment variable that overrides the config file location."""
    return "TTS_MCP_CONFIG"


def xdg_config_path() -> Path:
    """Path to config.yaml under XDG config home (~/.config/tts-mcp/config.yaml by default)."""
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_home) if xdg_home else Path.home() / ".config"
    return base / "tts-mcp" / "config.yaml"


def resolve_config_path() -> Path:
    """Resolve the config file location following the documented precedence order."""
    override = os.environ.get(config_env_var())
    if override:
        return Path(override)
    xdg_path = xdg_config_path()
    if xdg_path.exists():
        return xdg_path
    return DEFAULT_CONFIG_PATH


class GenerationResult(Protocol):
    """Protocol for a single TTS generation result chunk."""

    @property
    def audio(self) -> np.ndarray:
        """Audio samples for this chunk."""
        ...


class TTSModel(Protocol):
    """Protocol for a TTS model that supports streaming generation."""

    def generate(self, text: str, voice: str, **kwargs: object) -> Iterator[GenerationResult]:
        """Generate speech audio chunks from text.

        Additional keyword arguments are forwarded to the underlying model. In
        particular ``stream=True`` makes the model yield intermediate audio chunks
        during generation (roughly ``streaming_interval`` seconds of audio each)
        for low-latency playback; without it the model yields a single result once
        the whole utterance has been decoded.
        """
        ...


class AudioOutputStream(Protocol):
    """Runtime methods used from sounddevice.OutputStream."""

    def start(self) -> object:
        """Start the stream."""
        ...

    def stop(self) -> object:
        """Stop the stream."""
        ...

    def close(self) -> object:
        """Close the stream."""
        ...

    def write(self, data: np.ndarray) -> object:
        """Write audio frames to the stream."""
        ...


def clean_text(text: str) -> str:
    """Clean text by stripping and collapsing whitespace.

    Args:
        text: Raw input text.

    Returns:
        Cleaned text. Empty string if input was only whitespace.
    """
    text = text.strip()
    text = re.sub(r"\t", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = text.strip()
    return text


def simplify_punctuation(text: str) -> str:
    """Simplify punctuation by removing commas and replacing other marks with periods.

    Handles ASCII punctuation, smart quotes, em/en dashes, and ellipsis.
    CJK and other script-specific punctuation is passed through unchanged.

    Args:
        text: Input text (should be pre-cleaned with clean_text).

    Returns:
        Text with simplified punctuation.
    """
    text = text.replace(",", "")
    text = text.replace("\uff0c", "")

    text = text.replace("...", ".")
    text = text.replace("--", ".")

    for ch in "!?;:()[]{}\"'`\u2014\u2013\u2026\u201c\u201d\u2018\u2019":
        text = text.replace(ch, ".")

    text = re.sub(r"\.\s*(?:\.\s*)+", ".", text)
    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\.(?=[^\s.\d])", ". ", text)
    text = re.sub(r"^[\s.]+", "", text)
    text = text.rstrip()

    return text


def load_config() -> dict[str, Any]:
    """Load configuration from the resolved config path (see resolve_config_path).

    Returns:
        Configuration dictionary.

    Raises:
        FileNotFoundError: If the resolved config file does not exist.
        ValueError: If the config file is empty or invalid.
    """
    config_path = resolve_config_path()
    if not config_path.exists():
        msg = f"Configuration file not found: {config_path}"
        raise FileNotFoundError(msg)

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        msg = f"Invalid config.yaml: expected a mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    return cast(dict[str, Any], raw)


def discover_models(models_dir: Path) -> list[Path]:
    """Discover downloaded TTS models in the models directory.

    Args:
        models_dir: Base directory containing model subdirectories.

    Returns:
        Sorted list of model directory paths that contain model.safetensors.

    Raises:
        FileNotFoundError: If models_dir does not exist or has no models.
    """
    if not models_dir.exists():
        msg = f"Models directory does not exist: {models_dir}"
        raise FileNotFoundError(msg)

    models = sorted(p.parent for p in models_dir.glob("*/model.safetensors"))
    if not models:
        msg = f"No models found in {models_dir}. Run ./scripts/download-model.sh first."
        raise FileNotFoundError(msg)

    return models


def discover_voices(model_dir: Path) -> list[str]:
    """Discover available voices from the model's voice_embedding directory.

    Args:
        model_dir: Path to the model directory.

    Returns:
        Sorted list of available voice names.

    Raises:
        FileNotFoundError: If voice_embedding directory does not exist or has no voices.
    """
    voice_dir = model_dir / "voice_embedding"
    if not voice_dir.exists():
        msg = f"No voice_embedding directory found in {model_dir}"
        raise FileNotFoundError(msg)

    voices = sorted(p.stem for p in voice_dir.glob("*.safetensors"))
    if not voices:
        msg = f"No voice files found in {voice_dir}"
        raise FileNotFoundError(msg)

    return voices


def generate_speech(model_id: str, text: str, voice: str) -> np.ndarray:
    """Generate speech audio from text using Voxtral TTS.

    Args:
        model_id: The MLX model identifier to load.
        text: The text to convert to speech.
        voice: The voice to use for synthesis.

    Returns:
        Audio samples as a numpy array at 24kHz.

    Raises:
        RuntimeError: If no audio was generated.
    """
    model = load(model_id)

    if not hasattr(model, "generate") or model.generate is None:
        msg = f"Model {model_id} does not support generation"
        raise RuntimeError(msg)

    audio_chunks: list[np.ndarray] = []
    for result in model.generate(text=text, voice=voice):
        chunk = np.array(result.audio)
        audio_chunks.append(chunk)

    if not audio_chunks:
        msg = "No audio was generated by the model"
        raise RuntimeError(msg)

    return np.concatenate(audio_chunks)


def iter_stream_chunks(model: TTSModel, text: str, voice: str, streaming_interval: float) -> Iterator[np.ndarray]:
    """Yield audio chunks incrementally as the model streams them.

    Unlike generate_chunks (which drains the whole utterance into a list before
    returning), this drives the model in streaming mode and yields each chunk as
    soon as it is decoded, so playback can start after the first chunk instead of
    after the entire utterance. Zero-length chunks (the streaming final marker
    when all frames were already emitted) are skipped.

    Args:
        model: Loaded TTS model.
        text: The text to convert to speech.
        voice: The voice to use for synthesis.
        streaming_interval: Approximate seconds of audio per streamed chunk.

    Yields:
        Audio chunks as float32 numpy arrays.
    """
    for result in model.generate(text=text, voice=voice, stream=True, streaming_interval=streaming_interval):
        chunk = np.array(result.audio, dtype=np.float32)
        if chunk.size > 0:
            yield chunk


def play_audio(audio: np.ndarray, sample_rate: int) -> None:
    """Play audio samples through the default audio device.

    Args:
        audio: Audio samples as a numpy array.
        sample_rate: Sample rate in Hz.
    """
    sd.play(audio, sample_rate)
    sd.wait()


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


class _AudioObjectPropertyAddress(ctypes.Structure):
    """CoreAudio AudioObjectPropertyAddress: (selector, scope, element)."""

    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


def default_output_device_id() -> int | None:
    """Return the macOS system default output device ID, or None if unavailable.

    Reads kAudioHardwarePropertyDefaultOutputDevice directly from the CoreAudio HAL
    via ctypes. Unlike sounddevice/PortAudio (which snapshots its device list at
    initialization), this reflects a live default-device switch immediately and
    without tearing PortAudio down. Used to detect when the warm output stream must
    be reopened on a newly-selected device, so PortAudio is re-initialized only on an
    actual change rather than on every utterance. Returns None off macOS or on any
    query failure, in which case the caller keeps the current warm stream.
    """
    if sys.platform != "darwin":
        return None
    try:
        lib_path = ctypes.util.find_library("CoreAudio")
        if lib_path is None:
            return None
        core_audio = ctypes.CDLL(lib_path)
    except OSError as exc:
        print(f"\n  CoreAudio load failed, skipping device-change detection: {exc}", file=sys.stderr)
        return None

    # FourCC codes: 'dOut' = default output device selector, 'glob' = global scope; element 0 = main.
    address = _AudioObjectPropertyAddress(0x644F7574, 0x676C6F62, 0)
    system_object = ctypes.c_uint32(1)  # kAudioObjectSystemObject
    device_id = ctypes.c_uint32(0)
    data_size = ctypes.c_uint32(ctypes.sizeof(device_id))

    get_property = core_audio.AudioObjectGetPropertyData
    get_property.restype = ctypes.c_int32
    status = get_property(
        system_object,
        ctypes.byref(address),
        ctypes.c_uint32(0),
        None,
        ctypes.byref(data_size),
        ctypes.byref(device_id),
    )
    if status != 0:
        print(f"\n  CoreAudio default-output query returned status {status}", file=sys.stderr)
        return None
    return int(device_id.value)


def restart_process_on_device_change(_new_device: int) -> None:
    """Default watcher handler: exit so launchd (KeepAlive) respawns a fresh process.

    Restarting yields a clean CoreAudio HAL bound to the new default output device,
    avoiding the in-process PortAudio re-init that degrades the HAL (see ``AudioPlayer``).
    """
    os._exit(0)


def start_output_device_change_watcher(
    poll_interval_s: float,
    get_device: Callable[[], int | None],
    on_change: Callable[[int], None],
    stop_event: threading.Event,
) -> threading.Thread:
    """Restart the process when the macOS default output device changes.

    An in-process PortAudio re-init (``sd._terminate``/``_initialize``) after a
    live device switch degrades the CoreAudio HAL and distorts playback (see
    ``AudioPlayer``). Instead, this background daemon thread polls the HAL for the
    default output device and, on a change from the boot device, calls ``on_change``
    (in production ``restart_process_on_device_change``, which exits so launchd
    ``KeepAlive`` respawns a fresh process with a clean HAL bound to the new device).
    Trade-off: the fresh process reloads the TTS model (~15-20s of no voice),
    acceptable for infrequent plug/unplug switches.

    The poll compares against the boot device only, so transient/aggregate device
    ids the HAL reports mid-playback (which flip back within milliseconds) do not
    trigger a restart; only a sustained switch does.

    Args:
        poll_interval_s: Seconds between HAL polls.
        get_device: Returns the current default output device id (injectable for tests).
        on_change: Called with the new device id on a sustained change.
        stop_event: When set, the watch loop returns (used by tests).

    Returns:
        The started daemon thread.
    """

    def _run() -> None:
        boot_device = get_device()
        while not stop_event.wait(poll_interval_s):
            current = get_device()
            if current is not None and boot_device is not None and current != boot_device:
                print(
                    f"audio: default output device changed ({boot_device} -> {current}); restarting process for a clean CoreAudio HAL",
                    file=sys.stderr,
                )
                on_change(current)
                return

    thread = threading.Thread(target=_run, name="device-change-watcher", daemon=True)
    thread.start()
    return thread


def _write_lead_silence(stream: AudioOutputStream, sample_rate: int, lead_silence_ms: int) -> None:
    if lead_silence_ms < 0:
        msg = f"lead_silence_ms must be >= 0, got {lead_silence_ms}"
        raise ValueError(msg)

    silence_frames = int(sample_rate * lead_silence_ms / 1000)
    if silence_frames == 0:
        return

    stream.write(np.zeros((silence_frames, 1), dtype=np.float32))


@dataclasses.dataclass(frozen=True)
class PlaybackJob:
    """One playback request for the persistent audio player."""

    chunks: list[np.ndarray]
    output_path: Path | None
    on_complete: Callable[[Path | None], None] | None = None
    on_error: Callable[[Exception], None] | None = None


@dataclasses.dataclass(frozen=True)
class StreamingPlaybackJob:
    """A streaming playback request whose chunks arrive incrementally.

    The generation thread pushes chunks onto ``chunk_source`` as the model
    produces them and pushes ``None`` to signal end of utterance; the player
    thread writes each chunk to the warm output stream as it arrives. This
    starts playback after the first chunk instead of buffering the whole
    utterance first (see PlaybackJob for the buffered variant).
    """

    chunk_source: "queue.Queue[np.ndarray | None]"
    output_path: Path | None
    on_complete: Callable[[Path | None], None] | None = None
    on_error: Callable[[Exception], None] | None = None


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
                stream.write(chunk.reshape(-1, 1))
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
                stream.write(chunk.reshape(-1, 1))
                if job.output_path is not None:
                    collected.append(chunk)
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


def generate_chunks(model: TTSModel, text: str, voice: str) -> list[np.ndarray]:
    """Generate audio chunks from text without playing.

    Args:
        model: Loaded TTS model.
        text: The text to convert to speech.
        voice: The voice to use for synthesis.

    Returns:
        List of audio chunks as numpy arrays.
    """
    return [np.array(result.audio, dtype=np.float32) for result in model.generate(text=text, voice=voice)]


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

    if len(audio) < int(min_duration_seconds * sample_rate):
        return chunks

    if float(np.max(np.abs(audio))) == 0.0:
        return chunks

    integrated = float(meter.integrated_loudness(audio))
    if math.isinf(integrated) or math.isnan(integrated):
        return chunks

    if integrated >= target_lufs:
        return chunks

    gain_wanted_db = target_lufs - integrated

    oversampled = resample_poly(audio, up=4, down=1)
    peak = float(np.max(np.abs(oversampled)))
    if peak <= 0.0:
        return chunks
    tp_db = 20.0 * math.log10(peak)

    gain_max_db = true_peak_ceiling_db - tp_db
    if gain_max_db <= 0.0:
        return chunks

    gain_db = min(gain_wanted_db, gain_max_db)
    if gain_db <= 0.0:
        return chunks

    scalar = float(10.0 ** (gain_db / 20.0))
    audio *= scalar

    split_idx = np.cumsum(lens[:-1])
    out = np.split(audio, split_idx)
    return [part.astype(np.float32, copy=False) for part in out]


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
) -> None:
    """Feed streamed generation chunks to the player as they are produced.

    Runs on the generation (MLX) thread: it submits a streaming playback job,
    then drives chunk_iter and pushes each chunk to the player's warm stream.
    The None sentinel is always sent (even if generation raises) so the player
    thread never blocks waiting for a chunk that will not come. Returns once
    generation is exhausted; playback of the tail continues on the player thread
    and completion is signalled through on_complete.

    Args:
        player: Persistent audio player.
        chunk_iter: Iterator yielding audio chunks as they are generated.
        output_path: Path to save the full utterance, or None to skip saving.
        on_complete: Called with output_path when playback finishes.
        on_error: Called if playback fails.
    """
    source: queue.Queue[np.ndarray | None] = queue.Queue()
    player.submit_stream(
        StreamingPlaybackJob(
            chunk_source=source,
            output_path=output_path,
            on_complete=on_complete,
            on_error=on_error,
        )
    )
    try:
        for chunk in chunk_iter:
            source.put(chunk)
    finally:
        source.put(None)


def _generate_worker_chunks(
    model: TTSModel,
    text: str,
    voice: str,
    sample_rate: int,
    normalize_audio: bool,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    meter: pyln.Meter,
) -> list[np.ndarray] | None:
    try:
        generated = generate_chunks(model, text, voice)
        if normalize_audio and generated:
            generated = normalize_chunks(
                generated,
                sample_rate,
                target_lufs,
                true_peak_ceiling_db,
                min_duration_seconds,
                meter,
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


def _run_streaming_worker(
    work_queue: queue.Queue[str | None],
    model: TTSModel,
    voice: str,
    output_path: Path | None,
    player: AudioPlayer,
    streaming_interval: float,
) -> None:
    """Process the work queue in streaming mode: play each utterance as it generates.

    Each message is generated and streamed to the player chunk-by-chunk, so audio
    starts after the first chunk rather than after the whole utterance. Messages
    are handled serially (the within-utterance streaming is where the latency win
    is); loudness normalization is not applied because it needs the whole signal.
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
                iter_stream_chunks(model, text, voice, streaming_interval),
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
    sample_rate: int,
    normalize_audio: bool,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    meter: pyln.Meter,
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

        generated = _generate_worker_chunks(
            model,
            text,
            voice,
            sample_rate,
            normalize_audio,
            target_lufs,
            true_peak_ceiling_db,
            min_duration_seconds,
            meter,
        )
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
    sample_rate: int,
    lead_silence_ms: int,
    normalize_audio: bool,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    meter: pyln.Meter,
    stream: bool,
    streaming_interval: float,
) -> None:
    """Background worker that generates and plays TTS audio.

    In streaming mode (stream=True) each utterance is played chunk-by-chunk as it
    generates, for low latency. In buffered mode it generates the full utterance
    (with loudness normalization) before playback, generating the next message
    while the current one plays so there is no gap between sentences.

    Args:
        work_queue: Queue of text strings to synthesize. None signals shutdown.
        model: Loaded TTS model.
        voice: Voice to use for synthesis.
        output_path: Path to save generated audio, or None to skip saving.
        sample_rate: Sample rate in Hz.
        lead_silence_ms: Silence written after each audio stream open/reopen.
        normalize_audio: Whether to apply boost-only LUFS normalization (buffered mode only).
        target_lufs: Target integrated loudness in LUFS when normalization is enabled.
        true_peak_ceiling_db: Maximum true-peak level in dBFS after gain.
        min_duration_seconds: Minimum utterance length to attempt normalization.
        meter: Pre-constructed pyloudnorm Meter matching sample_rate.
        stream: Whether to stream playback within each utterance (low latency).
        streaming_interval: Approximate seconds of audio per streamed chunk.
    """
    player = AudioPlayer(sample_rate, lead_silence_ms)
    try:
        if stream:
            _run_streaming_worker(work_queue, model, voice, output_path, player, streaming_interval)
        else:
            _run_buffered_worker(
                work_queue,
                model,
                voice,
                output_path,
                player,
                sample_rate,
                normalize_audio,
                target_lufs,
                true_peak_ceiling_db,
                min_duration_seconds,
                meter,
            )
    finally:
        player.close()


def audio_worker_from_model_id(
    work_queue: queue.Queue[str | None],
    model_id: str,
    voice: str,
    output_path: Path | None,
    sample_rate: int,
    lead_silence_ms: int,
    normalize_audio: bool,
    target_lufs: float,
    true_peak_ceiling_db: float,
    min_duration_seconds: float,
    meter: pyln.Meter,
    stream: bool,
    streaming_interval: float,
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

    audio_worker(
        work_queue,
        model,
        voice,
        output_path,
        sample_rate,
        lead_silence_ms,
        normalize_audio,
        target_lufs,
        true_peak_ceiling_db,
        min_duration_seconds,
        meter,
        stream,
        streaming_interval,
    )


def make_output_path(output_dir: Path) -> Path:
    """Generate a timestamped output path for a new audio file.

    Args:
        output_dir: Directory to save audio files.

    Returns:
        Path with a timestamp-based filename.
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"speech_{ts}.wav"
