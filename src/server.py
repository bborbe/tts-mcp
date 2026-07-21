"""FastAPI TTS server with queued sequential playback."""

import contextlib
import dataclasses
import datetime
import json
import logging
import queue
import threading
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import numpy as np
import pyloudnorm as pyln
from fastapi import APIRouter, FastAPI, HTTPException, Request
from mlx_audio.tts.utils import load
from pydantic import BaseModel

from src.tts import (
    OUTPUT_DIR,
    AudioPlayer,
    AudioSettings,
    PlaybackJob,
    TTSModel,
    clean_text,
    default_output_device_id,
    discover_voices,
    generate_chunks,
    load_config,
    make_output_path,
    normalize_chunks,
    play_stream,
    restart_process_on_device_change,
    simplify_punctuation,
    start_output_device_change_watcher,
    streaming_chunk_iter,
)

logger = logging.getLogger("tts-server")

STATUS_TTL_SECONDS: int = 3600


@dataclasses.dataclass
class WorkItem:
    """A queued work item for the audio worker."""

    message_id: str
    text: str
    voice: str


@dataclasses.dataclass
class MessageStatus:
    """Tracks the lifecycle of a queued message."""

    message_id: str
    status: str
    text: str
    audio_file: str | None
    error: str | None
    completed_at: float | None


class ServerState:
    """Mutable server state shared between endpoints and the audio worker."""

    def __init__(
        self,
        model: TTSModel | None,
        model_path: str,
        voices: list[str],
        default_voice: str,
        sample_rate: int,
        lead_silence_ms: int,
        simplify_punctuation: bool,
        save_wav: bool,
        normalize_audio: bool,
        target_lufs: float,
        true_peak_ceiling_db: float,
        min_duration_seconds: float,
        meter: pyln.Meter,
        stream: bool,
        streaming_interval: float,
        streaming_warmup_seconds: float,
    ) -> None:
        """Initialize server state.

        Args:
            model: Pre-loaded TTS model, or None to have the audio worker load
                it on its own thread. MLX GPU streams are thread-local, so the
                model must be loaded on the same thread that calls generate.
            model_path: Filesystem path to the model, used by the audio worker
                to load the model on its own thread when model is None.
            voices: Available voice names.
            default_voice: Default voice for requests without voice override.
            sample_rate: Audio sample rate in Hz.
            lead_silence_ms: Silence written after each audio stream open/reopen.
            simplify_punctuation: Whether to simplify punctuation before TTS.
            save_wav: Whether to save generated audio to WAV files.
            normalize_audio: Whether to apply utterance-level loudness normalization.
            target_lufs: Target integrated loudness in LUFS.
            true_peak_ceiling_db: Maximum true-peak level in dBFS after gain.
            min_duration_seconds: Minimum utterance length to attempt normalization.
            meter: Pre-constructed pyloudnorm Meter matching sample_rate.
            stream: Whether to stream playback within each utterance (low latency).
            streaming_interval: Approximate seconds of audio per streamed chunk.
            streaming_warmup_seconds: Seconds buffered to measure the streaming
                normalization gain (see normalize_stream).
        """
        self.model: TTSModel | None = model
        self.model_path = model_path
        self.voices = voices
        self.default_voice = default_voice
        self.sample_rate = sample_rate
        self.lead_silence_ms = lead_silence_ms
        self.simplify_punctuation = simplify_punctuation
        self.save_wav = save_wav
        self.normalize_audio = normalize_audio
        self.target_lufs = target_lufs
        self.true_peak_ceiling_db = true_peak_ceiling_db
        self.min_duration_seconds = min_duration_seconds
        self.meter = meter
        self.stream = stream
        self.streaming_interval = streaming_interval
        self.streaming_warmup_seconds = streaming_warmup_seconds
        self.work_queue: queue.Queue[WorkItem | None] = queue.Queue()
        self.ready_queue: queue.Queue[BaseException | None] = queue.Queue()
        self.statuses: dict[str, MessageStatus] = {}
        self.status_lock = threading.Lock()
        self._counter = 0
        self._counter_lock = threading.Lock()

    def audio_settings(self) -> AudioSettings:
        """Build the worker AudioSettings from this state's fields."""
        return AudioSettings(
            sample_rate=self.sample_rate,
            lead_silence_ms=self.lead_silence_ms,
            normalize_audio=self.normalize_audio,
            target_lufs=self.target_lufs,
            true_peak_ceiling_db=self.true_peak_ceiling_db,
            min_duration_seconds=self.min_duration_seconds,
            meter=self.meter,
            stream=self.stream,
            streaming_interval=self.streaming_interval,
            streaming_warmup_seconds=self.streaming_warmup_seconds,
        )

    def next_message_id(self) -> str:
        """Generate a unique message ID.

        Returns:
            Message ID in format msg_YYYYMMDD_HHMMSS_NNN.
        """
        with self._counter_lock:
            self._counter += 1
            counter = self._counter
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"msg_{ts}_{counter:03d}"

    def evict_expired(self) -> None:
        """Remove completed/errored status entries older than TTL."""
        now = time.time()
        with self.status_lock:
            expired = [
                mid for mid, ms in self.statuses.items() if ms.completed_at is not None and (now - ms.completed_at) > STATUS_TTL_SECONDS
            ]
            for mid in expired:
                del self.statuses[mid]


class SayRequest(BaseModel):
    """Request body for POST /say."""

    text: str
    voice: str | None = None


class SayResponse(BaseModel):
    """Response body for POST /say."""

    message_id: str
    status: str
    queue_position: int


class StatusResponse(BaseModel):
    """Response body for GET /status/{message_id}."""

    message_id: str
    status: str
    text: str
    audio_file: str | None
    error: str | None


class VoicesResponse(BaseModel):
    """Response body for GET /voices."""

    voices: list[str]
    default_voice: str


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str


router = APIRouter()


@router.get("/health")
def health() -> HealthResponse:
    """Liveness check."""
    return HealthResponse(status="ok")


@router.get("/voices")
def voices(request: Request) -> VoicesResponse:
    """List available voices."""
    state: ServerState = request.app.state.server
    return VoicesResponse(voices=state.voices, default_voice=state.default_voice)


@router.post("/say", status_code=202)
def say(request: Request, body: SayRequest) -> SayResponse:
    """Queue text for speech synthesis and playback."""
    state: ServerState = request.app.state.server

    cleaned = clean_text(body.text)
    if not cleaned:
        raise HTTPException(status_code=422, detail="Text is empty after cleaning")

    if state.simplify_punctuation:
        cleaned = simplify_punctuation(cleaned)

    voice = body.voice if body.voice else state.default_voice
    if voice not in state.voices:
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{voice}' not available. Available voices: {', '.join(state.voices)}",
        )

    message_id = state.next_message_id()
    state.evict_expired()

    queue_position = state.work_queue.qsize()
    with state.status_lock:
        state.statuses[message_id] = MessageStatus(
            message_id=message_id,
            status="queued",
            text=cleaned,
            audio_file=None,
            error=None,
            completed_at=None,
        )

    state.work_queue.put(WorkItem(message_id=message_id, text=cleaned, voice=voice))

    logger.debug(
        "POST /say request:\n%s",
        json.dumps({"text": body.text, "voice": voice, "message_id": message_id}, indent=2),
    )

    return SayResponse(message_id=message_id, status="queued", queue_position=queue_position)


@router.get("/status/{message_id}")
def status(request: Request, message_id: str) -> StatusResponse:
    """Check the status of a queued/playing/completed message."""
    state: ServerState = request.app.state.server

    state.evict_expired()

    with state.status_lock:
        ms = state.statuses.get(message_id)

    if ms is None:
        raise HTTPException(status_code=404, detail=f"Unknown message ID: {message_id}")

    return StatusResponse(
        message_id=ms.message_id,
        status=ms.status,
        text=ms.text,
        audio_file=ms.audio_file,
        error=ms.error,
    )


def _fail_item(state: ServerState, message_id: str, error: str) -> None:
    """Mark a work item as failed in the status dict.

    Args:
        state: Server state with status dict and lock.
        message_id: ID of the failed message.
        error: Error description.
    """
    with state.status_lock:
        ms = state.statuses.get(message_id)
        if ms is not None:
            ms.status = "error"
            ms.error = error
            ms.completed_at = time.time()


def _start_playback(
    state: ServerState,
    player: AudioPlayer,
    pending: tuple[WorkItem, list[np.ndarray]],
    playback_done: threading.Event | None,
) -> threading.Event:
    """Wait for any prior playback, then queue a new playback job.

    Args:
        state: Server state with status dict and lock.
        player: Persistent audio player.
        pending: Tuple of (work_item, audio_chunks) ready for playback.
        playback_done: Completion event for the previous playback, or None.

    Returns:
        Completion event for the newly queued playback.
    """
    if playback_done is not None:
        playback_done.wait()

    work_item, chunks = pending
    done = threading.Event()
    on_complete, on_error = _playback_status_callbacks(state, work_item, done)

    with state.status_lock:
        state.statuses[work_item.message_id].status = "playing"

    output_path = make_output_path(OUTPUT_DIR) if state.save_wav else None
    player.submit(
        PlaybackJob(
            chunks=chunks,
            output_path=output_path,
            on_complete=on_complete,
            on_error=on_error,
        )
    )
    return done


def _load_worker_model(state: ServerState) -> TTSModel | None:
    """Load the model on the calling (worker) thread if not already loaded.

    MLX GPU streams are thread-local, so the model must be loaded on the same
    thread that later calls generate; loading on one thread and generating on
    another raises "no Stream(gpu, N) in current thread" (the same failure
    fixed for the CLI in audio_worker_from_model_id). The load outcome is
    reported through state.ready_queue so startup failures surface on the
    caller's thread.

    Args:
        state: Server state holding the (optional) model and its path.

    Returns:
        The loaded model, or None if loading failed.
    """
    model = state.model
    if model is None:
        try:
            model = load(state.model_path)
            if not hasattr(model, "generate") or model.generate is None:
                msg = f"Model {state.model_path} does not support generation"
                raise RuntimeError(msg)
            state.model = model
        except BaseException as exc:
            logger.error("Model load failed in audio worker: %s", exc)
            state.ready_queue.put(exc)
            return None
    state.ready_queue.put(None)
    return model


def _generate_item(state: ServerState, model: TTSModel, item: WorkItem) -> list[np.ndarray] | None:
    """Generate and optionally normalize audio chunks for one work item.

    Args:
        state: Server state with normalization settings and meter.
        model: Loaded TTS model (loaded on the worker thread).
        item: Work item to synthesize.

    Returns:
        Audio chunks for the item, or None if generation failed (the failure is
        recorded on the item's status).
    """
    try:
        chunks = generate_chunks(model, item.text, item.voice)
        if state.normalize_audio and chunks:
            chunks = normalize_chunks(
                chunks,
                state.sample_rate,
                state.target_lufs,
                state.true_peak_ceiling_db,
                state.min_duration_seconds,
                state.meter,
            )
        return chunks
    except (RuntimeError, ValueError) as exc:
        logger.error("TTS generation failed for %s: %s", item.message_id, exc)
        _fail_item(state, item.message_id, str(exc))
        return None


def _playback_status_callbacks(
    state: ServerState,
    item: WorkItem,
    done: threading.Event,
) -> tuple[Callable[[Path | None], None], Callable[[Exception], None]]:
    """Build on_complete/on_error callbacks that record playback status for one item."""

    def on_complete(output_path: Path | None) -> None:
        with state.status_lock:
            ms = state.statuses[item.message_id]
            ms.status = "completed"
            ms.audio_file = str(output_path) if output_path is not None else None
            ms.completed_at = time.time()
        logger.debug("Playback completed for %s -> %s", item.message_id, output_path)
        done.set()

    def on_error(exc: Exception) -> None:
        logger.error("Playback failed for %s: %s", item.message_id, exc)
        with state.status_lock:
            ms = state.statuses[item.message_id]
            ms.status = "error"
            ms.error = str(exc)
            ms.completed_at = time.time()
        done.set()

    return on_complete, on_error


def _run_streaming_server_loop(state: ServerState, model: TTSModel, player: AudioPlayer) -> None:
    """Process queued requests in streaming mode: play each utterance as it generates.

    Audio starts after the first chunk instead of after the whole utterance. When
    normalization is enabled it is applied via a warm-up window (see
    normalize_stream) rather than over the whole signal. Messages are handled
    serially; each iteration is wrapped so an unexpected error is logged and the
    worker keeps running.
    """
    settings = state.audio_settings()
    while True:
        current_item: WorkItem | None = None
        try:
            item = state.work_queue.get()
            if item is None:
                break
            current_item = item

            with state.status_lock:
                state.statuses[item.message_id].status = "playing"

            done = threading.Event()
            on_complete, on_error = _playback_status_callbacks(state, item, done)
            output_path = make_output_path(OUTPUT_DIR) if state.save_wav else None

            try:
                play_stream(
                    player,
                    streaming_chunk_iter(model, item.text, item.voice, settings),
                    output_path,
                    on_complete,
                    on_error,
                )
            except (RuntimeError, ValueError) as exc:
                logger.error("TTS generation failed for %s: %s", item.message_id, exc)
                _fail_item(state, item.message_id, str(exc))
                done.set()

            done.wait()
            state.work_queue.task_done()

        except Exception as exc:
            logger.error("Audio worker caught unexpected error (recovering): %s", exc, exc_info=True)
            if current_item is not None:
                _fail_item(state, current_item.message_id, f"unexpected worker error: {exc}")
                with contextlib.suppress(ValueError):
                    state.work_queue.task_done()


def _run_buffered_server_loop(state: ServerState, model: TTSModel, player: AudioPlayer) -> None:
    """Process queued requests in buffered mode with cross-message lookahead.

    Generates the full utterance (and optional loudness normalization) before
    playback, generating the next message while the current one still plays.
    """
    pending: tuple[WorkItem, list[np.ndarray]] | None = None
    playback_done: threading.Event | None = None

    while True:
        current_item: WorkItem | None = None
        try:
            if pending is not None:
                playback_done = _start_playback(state, player, pending, playback_done)
                pending = None

            item = state.work_queue.get()
            if item is None:
                if playback_done is not None:
                    playback_done.wait()
                break

            current_item = item

            chunks = _generate_item(state, model, item)
            if chunks is not None:
                pending = (item, chunks)

            state.work_queue.task_done()

        except Exception as exc:
            logger.error(
                "Audio worker caught unexpected error (recovering): %s",
                exc,
                exc_info=True,
            )
            if current_item is not None:
                _fail_item(state, current_item.message_id, f"unexpected worker error: {exc}")
                with contextlib.suppress(ValueError):
                    state.work_queue.task_done()

    if pending is not None:
        playback_done = _start_playback(state, player, pending, playback_done)
        playback_done.wait()


def server_audio_worker(state: ServerState) -> None:
    """Background worker that processes queued TTS requests sequentially.

    Loads the model on this thread when it was not pre-loaded (see
    _load_worker_model), because MLX GPU streams are thread-local. Dispatches to
    the streaming or buffered loop based on state.stream. Both wrap each
    iteration in a top-level handler so unexpected exceptions are logged and the
    worker keeps running instead of dying silently.

    Args:
        state: Server state with work queue, model, and status tracking.
    """
    model = _load_worker_model(state)
    if model is None:
        return

    player = AudioPlayer(state.sample_rate, state.lead_silence_ms)
    try:
        if state.stream:
            _run_streaming_server_loop(state, model, player)
        else:
            _run_buffered_server_loop(state, model, player)
    finally:
        player.close()


@dataclasses.dataclass(frozen=True)
class _ServerConfig:
    """Parsed server configuration from config.yaml."""

    model_path: str
    sample_rate: int
    default_voice: str
    simplify_punctuation: bool
    save_wav: bool
    normalize_audio: bool
    target_lufs: float
    true_peak_ceiling_db: float
    min_duration_seconds: float
    lead_silence_ms: int
    stream: bool
    streaming_interval: float
    streaming_warmup_seconds: float


def _require(config: dict[str, object], key: str) -> object:
    """Fetch a required config key or raise ValueError with a clear message."""
    value = config.get(key)
    if value is None:
        msg = f"Missing required key '{key}' in config.yaml"
        raise ValueError(msg)
    return value


def _parse_server_config() -> _ServerConfig:
    """Load and validate server settings from config.yaml. Fails fast on missing keys."""
    config = load_config()

    model_path = _require(config, "model")
    if not isinstance(model_path, str) or not Path(model_path).exists():
        msg = f"Model directory does not exist: {model_path!r}"
        raise FileNotFoundError(msg)

    default_voice = _require(config, "default_voice")
    if not isinstance(default_voice, str):
        msg = "'default_voice' in config.yaml must be a string"
        raise ValueError(msg)

    return _ServerConfig(
        model_path=model_path,
        sample_rate=int(cast(int, _require(config, "sample_rate"))),
        default_voice=default_voice,
        simplify_punctuation=bool(config.get("simplify_punctuation")),
        save_wav=bool(_require(config, "save_wav")),
        normalize_audio=bool(_require(config, "normalize_audio")),
        target_lufs=float(cast(float, _require(config, "target_lufs"))),
        true_peak_ceiling_db=float(cast(float, _require(config, "true_peak_ceiling_db"))),
        min_duration_seconds=float(cast(float, _require(config, "min_duration_seconds"))),
        lead_silence_ms=int(cast(int, _require(config, "lead_silence_ms"))),
        stream=bool(_require(config, "stream")),
        streaming_interval=float(cast(float, _require(config, "streaming_interval"))),
        streaming_warmup_seconds=float(cast(float, _require(config, "streaming_warmup_seconds"))),
    )


def _build_server_state(cfg: _ServerConfig) -> ServerState:
    """Assemble a ServerState from parsed config.

    The MLX model is intentionally not loaded here. It is loaded by the audio
    worker on its own thread (see server_audio_worker), because MLX GPU streams
    are thread-local.
    """
    available_voices = discover_voices(Path(cfg.model_path))
    if cfg.default_voice not in available_voices:
        msg = f"default_voice '{cfg.default_voice}' not found. Available: {', '.join(available_voices)}"
        raise ValueError(msg)

    return ServerState(
        model=None,
        model_path=cfg.model_path,
        voices=available_voices,
        default_voice=cfg.default_voice,
        sample_rate=cfg.sample_rate,
        lead_silence_ms=cfg.lead_silence_ms,
        simplify_punctuation=cfg.simplify_punctuation,
        save_wav=cfg.save_wav,
        normalize_audio=cfg.normalize_audio,
        target_lufs=cfg.target_lufs,
        true_peak_ceiling_db=cfg.true_peak_ceiling_db,
        min_duration_seconds=cfg.min_duration_seconds,
        meter=pyln.Meter(float(cfg.sample_rate)),
        stream=cfg.stream,
        streaming_interval=cfg.streaming_interval,
        streaming_warmup_seconds=cfg.streaming_warmup_seconds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start the audio worker, wait for its in-thread model load, then shut it down on exit.

    The audio worker loads the model on its own thread (MLX GPU streams are
    thread-local) and reports the load outcome via state.ready_queue. Startup
    blocks here until that signal arrives so model-load failures surface
    cleanly instead of after startup completes.
    """
    state = _build_server_state(_parse_server_config())

    worker = threading.Thread(target=server_audio_worker, args=(state,), daemon=True)
    worker.start()

    load_error = state.ready_queue.get()
    if load_error is not None:
        raise load_error

    app.state.server = state

    # Restart the process on a default-output-device switch rather than doing an
    # in-process PortAudio re-init (which degrades the CoreAudio HAL — see AudioPlayer).
    start_output_device_change_watcher(
        poll_interval_s=2.0,
        get_device=default_output_device_id,
        on_change=restart_process_on_device_change,
        stop_event=threading.Event(),
    )

    yield

    state.work_queue.put(None)
    worker.join(timeout=10)


app = FastAPI(lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    _config = load_config()

    _host = _config.get("host")
    if not _host:
        _msg = "Missing required key 'host' in config.yaml"
        raise ValueError(_msg)

    _raw_port = _config.get("port")
    if _raw_port is None:
        _msg = "Missing required key 'port' in config.yaml"
        raise ValueError(_msg)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    uvicorn.run("src.server:app", host=_host, port=int(_raw_port), log_level="debug")
