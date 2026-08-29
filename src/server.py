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
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.tts import (
    OUTPUT_DIR,
    QWEN3,
    AudioPlayer,
    AudioSettings,
    EngineRegistry,
    EngineSpec,
    LoadedEngine,
    PlaybackJob,
    build_engine,
    clean_text,
    default_output_device_id,
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

# How long a finished message's status survives before evict_expired() drops it.
# This is the real ceiling on replayable history: the web UI renders a Replay
# button per entry in GET /state, so a message that has been evicted cannot be
# replayed — its text, voice and engine are gone from memory. Raised from 1h to
# 24h on 2026-08-29 so a full working day stays replayable; MessageStatus holds
# only short strings, so even a chatty day costs a few hundred KB.
STATUS_TTL_SECONDS: int = 24 * 60 * 60

CANCELLABLE_STATUSES: frozenset[str] = frozenset({"queued", "loading", "playing", "paused"})
"""Statuses a message can still be cancelled from — everything not yet finished."""

RECENT_HISTORY_LIMIT: int = 25
"""How many finished messages GET /state returns, newest first.

The web UI renders one Replay button per entry, so this is also how far back a
message stays re-speakable. Raised from 10 on 2026-08-29 because 10 covers only a
few minutes of a chatty session. Note STATUS_TTL_SECONDS is the harder ceiling:
an evicted message has no entry regardless of this value.
"""

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "error", "cancelled"})
"""Statuses that are final: once reported, nothing may overwrite them."""


@dataclasses.dataclass
class WorkItem:
    """A queued work item for the audio worker."""

    message_id: str
    text: str
    voice: str
    instruct: str | None
    engine: str


@dataclasses.dataclass
class MessageStatus:
    """Tracks the lifecycle of a queued message."""

    message_id: str
    status: str
    text: str
    audio_file: str | None
    error: str | None
    completed_at: float | None
    engine: str | None = None
    sender: str | None = None
    # Resolved voice (after the default-voice fallback), not the raw request value.
    # Carried so a replay re-speaks in the voice the listener actually heard rather
    # than whatever the default happens to be at replay time.
    voice: str | None = None
    # True when this message was queued by the web UI's Replay button. A replay is
    # the same utterance heard again, not a new message, so it is excluded from
    # GET /state's recent list — otherwise replays would evict the very messages
    # the listener is replaying to catch up on. Playback itself is unaffected.
    is_replay: bool = False


class ServerState:
    """Mutable server state shared between endpoints and the audio worker."""

    def __init__(
        self,
        registry: EngineRegistry,
        voices_by_engine: dict[str, list[str]],
        default_engine: str,
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
            registry: Lazy per-engine model cache. Owned by the audio worker —
                MLX GPU streams are thread-local, so every model it loads is
                only usable on the worker thread that loaded it.
            voices_by_engine: Available voice names per engine kind, discovered
                from disk at startup without loading any model.
            default_engine: Engine used by requests that name none.
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
        self._registry = registry
        self._voices_by_engine = voices_by_engine
        self._default_engine = default_engine
        self._default_voice = default_voice
        self._sample_rate = sample_rate
        self._lead_silence_ms = lead_silence_ms
        self._simplify_punctuation = simplify_punctuation
        self._save_wav = save_wav
        self._normalize_audio = normalize_audio
        self._target_lufs = target_lufs
        self._true_peak_ceiling_db = true_peak_ceiling_db
        self._min_duration_seconds = min_duration_seconds
        self._meter = meter
        self._stream = stream
        self._streaming_interval = streaming_interval
        self._streaming_warmup_seconds = streaming_warmup_seconds
        self.work_queue: queue.Queue[WorkItem | None] = queue.Queue()
        self.ready_queue: queue.Queue[BaseException | None] = queue.Queue()
        self.statuses: dict[str, MessageStatus] = {}
        self.engine_errors: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancelled: set[str] = set()
        self._pause_events: dict[str, threading.Event] = {}
        self._paused: set[str] = set()
        self._loaded_engines: set[str] = set()
        self._status_lock = threading.Lock()
        self._counter = 0
        self._counter_lock = threading.Lock()

    @property
    def registry(self) -> EngineRegistry:
        """Lazy per-engine model cache, owned by the audio worker thread."""
        return self._registry

    @property
    def default_engine(self) -> str:
        """Engine used by requests that name none."""
        return self._default_engine

    @property
    def voices_by_engine(self) -> dict[str, list[str]]:
        """Available voice names per engine kind."""
        return self._voices_by_engine

    @property
    def engines(self) -> list[str]:
        """Engine kinds that discovered their voices and can serve requests."""
        return sorted(self._voices_by_engine)

    @property
    def voices(self) -> list[str]:
        """Available voice names across every available engine."""
        seen: list[str] = []
        for kind in sorted(self._voices_by_engine):
            seen.extend(voice for voice in self._voices_by_engine[kind] if voice not in seen)
        return seen

    def voices_for(self, engine: str) -> list[str]:
        """List the voices one engine offers.

        Args:
            engine: Engine kind to look up.

        Returns:
            That engine's voices, or an empty list when it is unavailable.
        """
        return self._voices_by_engine.get(engine, [])

    def mark_engine_loaded(self, engine: str) -> None:
        """Record that an engine's model is now resident.

        Called by the audio worker; read by /voices on the HTTP thread, so the
        worker's own registry dict is never touched from another thread.

        Args:
            engine: Engine kind whose model finished loading.
        """
        with self._status_lock:
            self._loaded_engines.add(engine)
            self.engine_errors.pop(engine, None)

    def mark_engine_failed(self, engine: str, error: str) -> None:
        """Record that an engine's model failed to load.

        Args:
            engine: Engine kind whose load failed.
            error: Human-readable failure description.
        """
        with self._status_lock:
            self.engine_errors[engine] = error

    def is_engine_loaded(self, engine: str) -> bool:
        """Report whether an engine's model is resident.

        Args:
            engine: Engine kind to check.

        Returns:
            True once the worker has loaded that engine's model.
        """
        with self._status_lock:
            return engine in self._loaded_engines

    def engine_error(self, engine: str) -> str | None:
        """Return the recorded load failure for an engine, if any.

        Args:
            engine: Engine kind to check.

        Returns:
            The failure description, or None.
        """
        with self._status_lock:
            return self.engine_errors.get(engine)

    @property
    def default_voice(self) -> str:
        """Default voice for requests without a voice override."""
        return self._default_voice

    @property
    def sample_rate(self) -> int:
        """Audio sample rate in Hz."""
        return self._sample_rate

    @property
    def lead_silence_ms(self) -> int:
        """Silence written after each audio stream open/reopen."""
        return self._lead_silence_ms

    @property
    def simplify_punctuation(self) -> bool:
        """Whether to simplify punctuation before TTS."""
        return self._simplify_punctuation

    @property
    def save_wav(self) -> bool:
        """Whether to save generated audio to WAV files."""
        return self._save_wav

    @property
    def normalize_audio(self) -> bool:
        """Whether to apply utterance-level loudness normalization."""
        return self._normalize_audio

    @property
    def target_lufs(self) -> float:
        """Target integrated loudness in LUFS."""
        return self._target_lufs

    @property
    def true_peak_ceiling_db(self) -> float:
        """Maximum true-peak level in dBFS after gain."""
        return self._true_peak_ceiling_db

    @property
    def min_duration_seconds(self) -> float:
        """Minimum utterance length to attempt normalization."""
        return self._min_duration_seconds

    @property
    def meter(self) -> pyln.Meter:
        """Pre-constructed pyloudnorm Meter matching sample_rate."""
        return self._meter

    @property
    def stream(self) -> bool:
        """Whether to stream playback within each utterance."""
        return self._stream

    @property
    def streaming_interval(self) -> float:
        """Approximate seconds of audio per streamed chunk."""
        return self._streaming_interval

    @property
    def streaming_warmup_seconds(self) -> float:
        """Seconds buffered to measure the streaming gain."""
        return self._streaming_warmup_seconds

    @property
    def status_lock(self) -> threading.Lock:
        """Guards the statuses dict."""
        return self._status_lock

    def audio_settings(self) -> AudioSettings:
        """Build the worker AudioSettings from this state's fields."""
        return AudioSettings(
            sample_rate=self._sample_rate,
            lead_silence_ms=self._lead_silence_ms,
            normalize_audio=self._normalize_audio,
            target_lufs=self._target_lufs,
            true_peak_ceiling_db=self._true_peak_ceiling_db,
            min_duration_seconds=self._min_duration_seconds,
            meter=self._meter,
            stream=self._stream,
            streaming_interval=self._streaming_interval,
            streaming_warmup_seconds=self._streaming_warmup_seconds,
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
        """Remove completed/errored/cancelled status entries older than TTL."""
        now = time.time()
        with self._status_lock:
            expired = [
                mid for mid, ms in self.statuses.items() if ms.completed_at is not None and (now - ms.completed_at) > STATUS_TTL_SECONDS
            ]
            for mid in expired:
                del self.statuses[mid]
                self._cancelled.discard(mid)
                self._paused.discard(mid)

    def begin_playback(self, message_id: str) -> tuple[threading.Event, threading.Event]:
        """Register the message the worker is about to play and hand back its cancel + pause events.

        The events are created per message rather than kept on the player, so a
        cancel (or pause) that arrives just as one utterance ends can never
        leak into the next one: they are addressed to a message id, and an id
        is played once. A message cancelled while it was still queued gets an
        already-set cancel event, so it is dropped the moment playback would
        have started.

        Args:
            message_id: Message about to be played.

        Returns:
            The (cancel, pause) events for this message.
        """
        with self._status_lock:
            cancel_event = threading.Event()
            pause_event = threading.Event()
            if message_id in self._cancelled:
                cancel_event.set()
            self._cancel_events[message_id] = cancel_event
            self._pause_events[message_id] = pause_event
            return cancel_event, pause_event

    def end_playback(self, message_id: str) -> None:
        """Forget a message's cancel + pause events once it is no longer playable.

        Args:
            message_id: Message that finished, failed, was cancelled, or paused
                then cancelled.
        """
        with self._status_lock:
            self._cancel_events.pop(message_id, None)
            self._pause_events.pop(message_id, None)
            self._cancelled.discard(message_id)
            self._paused.discard(message_id)

    def is_cancelled(self, message_id: str) -> bool:
        """Report whether a message was cancelled before the worker reached it.

        Args:
            message_id: Message the worker just took off the queue.

        Returns:
            True if the message must be skipped without synthesizing it.
        """
        with self._status_lock:
            return message_id in self._cancelled

    def request_cancel(self, message_id: str | None, cancel_all: bool) -> list[str]:
        """Cancel the playing message, one named message, or everything in flight.

        A message that is playing is stopped through its cancel event, and the
        player reports the final ``cancelled`` status once it has actually
        stopped writing audio. A message still sitting in the queue is marked
        here and skipped by the worker, so it is never synthesized at all.

        Args:
            message_id: Message to cancel, or None to cancel what is playing now.
            cancel_all: Cancel the playing message and drop everything queued
                behind it. Overrides message_id.

        Returns:
            The ids that were cancelled, in queue order. Empty when there was
            nothing left to cancel.
        """
        with self._status_lock:
            if cancel_all:
                targets = [mid for mid, ms in self.statuses.items() if ms.status in CANCELLABLE_STATUSES]
            elif message_id is not None:
                status = self.statuses.get(message_id)
                targets = [message_id] if status is not None and status.status in CANCELLABLE_STATUSES else []
            else:
                targets = list(self._cancel_events)

            playing: list[threading.Event] = []
            for mid in targets:
                self._cancelled.add(mid)
                event = self._cancel_events.get(mid)
                if event is not None:
                    # Playing: the player writes the final status from its own
                    # thread once it stops, so it is never reported as stopped
                    # while audio is still coming out of the speakers.
                    playing.append(event)
                    continue
                # Queued: nothing will ever play it, so this is the final status.
                queued = self.statuses.get(mid)
                if queued is not None:
                    queued.status = "cancelled"
                    queued.completed_at = time.time()

        # Woken outside the lock: each event is independent once looked up, and
        # the threads they wake take the same lock to report their final status.
        for event in playing:
            event.set()
        return targets

    def request_pause(self, message_id: str | None) -> list[str]:
        """Pause the playing message, or one named message.

        A message that is playing is paused through its pause event, and the
        player stops between write slices (~100ms). A queued message cannot be
        paused (it has not started) and is ignored. Pausing nothing is not an
        error — the response lists whatever was actually paused.

        Args:
            message_id: Message to pause, or None to pause what is playing now.

        Returns:
            The ids that were paused, in queue order. Empty when nothing was
            playing.
        """
        with self._status_lock:
            targets = ([message_id] if message_id in self._pause_events else []) if message_id is not None else list(self._pause_events)

            playing: list[threading.Event] = []
            for mid in targets:
                self._paused.add(mid)
                event = self._pause_events.get(mid)
                if event is not None:
                    playing.append(event)
                ms = self.statuses.get(mid)
                if ms is not None:
                    ms.status = "paused"

        for event in playing:
            event.set()
        return targets

    def request_resume(self, message_id: str | None) -> list[str]:
        """Resume the paused message, or one named message.

        Mirrors request_pause: the pause event is cleared and the player picks
        up exactly where it stopped (between write slices). A message that is
        not paused is ignored; resuming nothing is not an error.

        Args:
            message_id: Message to resume, or None to resume what is paused now.

        Returns:
            The ids that were resumed, in queue order. Empty when nothing was
            paused.
        """
        with self._status_lock:
            targets = ([message_id] if message_id in self._paused else []) if message_id is not None else list(self._paused)

            paused: list[threading.Event] = []
            for mid in targets:
                self._paused.discard(mid)
                event = self._pause_events.get(mid)
                if event is not None:
                    paused.append(event)
                ms = self.statuses.get(mid)
                if ms is not None:
                    ms.status = "playing"

        for event in paused:
            event.clear()
        return targets

    def mark_skipped(self, message_id: str) -> None:
        """Record a queued message the worker dropped without synthesizing it.

        Args:
            message_id: Message taken off the queue after it was cancelled.
        """
        with self._status_lock:
            status = self.statuses.get(message_id)
            if status is not None:
                status.status = "cancelled"
                status.completed_at = time.time()
            self._cancelled.discard(message_id)


class SayRequest(BaseModel):
    """Request body for POST /say."""

    text: str
    voice: str | None = None
    instruct: str | None = None
    engine: str | None = None
    sender: str | None = None
    # Set by the web UI's Replay button. The message is spoken normally but kept
    # out of GET /state's recent list, since a replay is the same utterance heard
    # again rather than a new one.
    replay: bool = False


class SayResponse(BaseModel):
    """Response body for POST /say."""

    message_id: str
    status: str
    queue_position: int


class CancelRequest(BaseModel):
    """Request body for POST /cancel. Every field is optional; an empty body cancels what is playing."""

    message_id: str | None = None
    all: bool = False


class CancelResponse(BaseModel):
    """Response body for POST /cancel."""

    cancelled: list[str]
    queued: int


class PauseRequest(BaseModel):
    """Request body for POST /pause and POST /resume. Empty body targets what is playing/paused."""

    message_id: str | None = None


class PauseResponse(BaseModel):
    """Response body for POST /pause and POST /resume."""

    paused: list[str]
    queued: int


class StatusResponse(BaseModel):
    """Response body for GET /status/{message_id}."""

    message_id: str
    status: str
    text: str
    audio_file: str | None = None
    error: str | None = None
    engine: str | None = None
    sender: str | None = None
    voice: str | None = None


class StateResponse(BaseModel):
    """Response body for GET /state — the current playback, recent history, and queue depth.

    ``current`` is the message that is playing or paused right now (or None when
    idle). ``recent`` is the tail of finished messages, most recent first, so a
    UI can show "what was said and by whom" without polling every id. ``queued``
    is the number of messages still waiting to play.
    """

    current: StatusResponse | None = None
    recent: list[StatusResponse]
    queued: int


class EngineVoices(BaseModel):
    """One engine's voices and availability, as reported by GET /voices."""

    engine: str
    voices: list[str]
    default_voice: str | None = None
    language: str | None = None
    supports_instruct: bool = False
    loaded: bool = False
    available: bool = True
    error: str | None = None


class VoicesResponse(BaseModel):
    """Response body for GET /voices.

    ``voices`` is the union across available engines, so a caller can pick any
    listed voice and pass it with the matching ``engine``. The first request for
    an engine whose model is not yet resident waits on that load.
    """

    voices: list[str]
    default_voice: str
    default_engine: str = ""
    engines: list[EngineVoices] = []


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str


router = APIRouter()


@router.get("/")
def web_ui() -> FileResponse:
    """Serve the web UI (message history + sender + pause/resume controls)."""
    ui_path = Path(__file__).resolve().parent.parent / "web" / "index.html"
    if not ui_path.exists():
        raise HTTPException(status_code=404, detail="web/index.html not found")
    return FileResponse(ui_path)


@router.get("/health")
def health() -> HealthResponse:
    """Liveness check."""
    return HealthResponse(status="ok")


@router.get("/voices")
def voices(request: Request) -> VoicesResponse:
    """List available voices, flat and grouped per engine."""
    state: ServerState = request.app.state.server

    engines: list[EngineVoices] = []
    for kind in state.registry.kinds():
        spec = state.registry.spec(kind)
        engine_voices = state.voices_for(kind)
        engines.append(
            EngineVoices(
                engine=kind,
                voices=engine_voices,
                default_voice=state.default_voice if kind == state.default_engine else None,
                language=spec.language if spec is not None else None,
                supports_instruct=kind == QWEN3,
                loaded=state.is_engine_loaded(kind),
                available=bool(engine_voices),
                error=state.engine_error(kind),
            )
        )

    return VoicesResponse(
        voices=state.voices,
        default_voice=state.default_voice,
        default_engine=state.default_engine,
        engines=engines,
    )


@router.post("/say", status_code=202)
def say(request: Request, body: SayRequest) -> SayResponse:
    """Queue text for speech synthesis and playback."""
    state: ServerState = request.app.state.server

    cleaned = clean_text(body.text)
    if not cleaned:
        raise HTTPException(status_code=422, detail="Text is empty after cleaning")

    if state.simplify_punctuation:
        cleaned = simplify_punctuation(cleaned)

    engine = body.engine if body.engine else state.default_engine
    if engine not in state.registry.kinds():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown engine '{engine}'. Configured engines: {', '.join(state.registry.kinds())}",
        )

    engine_voices = state.voices_for(engine)
    if not engine_voices:
        error = state.engine_error(engine) or "voice discovery failed"
        raise HTTPException(
            status_code=400,
            detail=f"Engine '{engine}' is unavailable: {error}",
        )

    voice = body.voice if body.voice else state.default_voice
    if voice not in engine_voices:
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{voice}' not available on engine '{engine}'. Available voices: {', '.join(engine_voices)}",
        )

    # Reject instruct here rather than letting the engine raise inside the
    # worker, which would return 202 and only fail after the caller stopped
    # looking — and would pointlessly load the model first.
    if body.instruct is not None and engine != QWEN3:
        raise HTTPException(
            status_code=400,
            detail=f"'instruct' is not supported by the {engine} engine",
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
            engine=engine,
            sender=body.sender,
            voice=voice,
            is_replay=body.replay,
        )

    state.work_queue.put(WorkItem(message_id=message_id, text=cleaned, voice=voice, instruct=body.instruct, engine=engine))

    logger.debug(
        "POST /say request:\n%s",
        json.dumps(
            {"text": body.text, "voice": voice, "engine": engine, "message_id": message_id},
            indent=2,
        ),
    )

    return SayResponse(message_id=message_id, status="queued", queue_position=queue_position)


@router.post("/cancel")
def cancel(request: Request, body: CancelRequest | None = None) -> CancelResponse:
    """Stop speech that is playing and let the next queued message start.

    With no body the message currently playing is stopped. With ``message_id``
    only that message is affected — cancelling one that is still queued drops it
    before it is ever synthesized. With ``all`` the playing message is stopped
    and everything queued behind it is dropped.
    """
    state: ServerState = request.app.state.server
    args = body if body is not None else CancelRequest()

    if args.message_id is not None:
        with state.status_lock:
            known = args.message_id in state.statuses
        if not known:
            raise HTTPException(status_code=404, detail=f"Unknown message ID: {args.message_id}")

    cancelled = state.request_cancel(args.message_id, args.all)
    logger.debug("POST /cancel cancelled %s", cancelled)

    return CancelResponse(cancelled=cancelled, queued=state.work_queue.qsize())


@router.post("/pause")
def pause(request: Request, body: PauseRequest | None = None) -> PauseResponse:
    """Hold the message that is playing where it is; resume later with POST /resume.

    With no body the message currently playing is paused. With ``message_id``
    only that message is paused. A paused message is still cancellable (the
    pause never blocks a cancel). Pausing nothing is not an error.
    """
    state: ServerState = request.app.state.server
    args = body if body is not None else PauseRequest()

    if args.message_id is not None:
        with state.status_lock:
            known = args.message_id in state.statuses
        if not known:
            raise HTTPException(status_code=404, detail=f"Unknown message ID: {args.message_id}")

    paused = state.request_pause(args.message_id)
    logger.debug("POST /pause paused %s", paused)

    return PauseResponse(paused=paused, queued=state.work_queue.qsize())


@router.post("/resume")
def resume(request: Request, body: PauseRequest | None = None) -> PauseResponse:
    """Continue a paused message from exactly where it stopped.

    With no body the message currently paused is resumed. With ``message_id``
    only that message is resumed. Resuming nothing is not an error.
    """
    state: ServerState = request.app.state.server
    args = body if body is not None else PauseRequest()

    if args.message_id is not None:
        with state.status_lock:
            known = args.message_id in state.statuses
        if not known:
            raise HTTPException(status_code=404, detail=f"Unknown message ID: {args.message_id}")

    resumed = state.request_resume(args.message_id)
    logger.debug("POST /resume resumed %s", resumed)

    return PauseResponse(paused=resumed, queued=state.work_queue.qsize())


@router.get("/state")
def state_endpoint(request: Request) -> StateResponse:
    """Report what is playing/paused now, the recent message history, and the queue depth."""
    state: ServerState = request.app.state.server

    state.evict_expired()

    with state.status_lock:
        current: MessageStatus | None = None
        for ms in state.statuses.values():
            if ms.status in ("playing", "paused"):
                current = ms
                break
        recent_msgs = sorted(
            (ms for ms in state.statuses.values() if ms.completed_at is not None and not ms.is_replay),
            key=lambda ms: ms.completed_at or 0.0,
            reverse=True,
        )[:RECENT_HISTORY_LIMIT]

        def to_response(ms: MessageStatus) -> StatusResponse:
            return StatusResponse(
                message_id=ms.message_id,
                status=ms.status,
                text=ms.text,
                audio_file=ms.audio_file,
                error=ms.error,
                engine=ms.engine,
                sender=ms.sender,
                voice=ms.voice,
            )

        return StateResponse(
            current=to_response(current) if current is not None else None,
            recent=[to_response(ms) for ms in recent_msgs],
            queued=state.work_queue.qsize(),
        )


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
        engine=ms.engine,
        sender=ms.sender,
    )


def _fail_item(state: ServerState, message_id: str, error: str) -> None:
    """Mark a work item as failed in the status dict.

    A message that already reached a terminal state keeps it. The worker's
    recovery handler reports any unexpected exception through here, including
    one raised after the message was already settled — a message the caller was
    told is ``cancelled`` must not later report ``error`` because the bookkeeping
    that followed the cancellation tripped.

    Args:
        state: Server state with status dict and lock.
        message_id: ID of the failed message.
        error: Error description.
    """
    with state.status_lock:
        ms = state.statuses.get(message_id)
        if ms is not None and ms.status not in TERMINAL_STATUSES:
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
    on_complete, on_error, on_cancel = _playback_status_callbacks(state, work_item, done)

    cancel, pause = state.begin_playback(work_item.message_id)
    with state.status_lock:
        state.statuses[work_item.message_id].status = "playing"

    output_path = make_output_path(OUTPUT_DIR) if state.save_wav else None
    player.submit(
        PlaybackJob(
            chunks=chunks,
            output_path=output_path,
            on_complete=on_complete,
            on_error=on_error,
            on_cancel=on_cancel,
            cancel=cancel,
            pause=pause,
        )
    )
    return done


def _skip_if_cancelled(state: ServerState, item: WorkItem) -> bool:
    """Drop a message that was cancelled while it waited in the queue.

    Checked before the engine is resolved, so a cancelled message costs neither
    a model load nor a single generated sample.

    Args:
        state: Server state holding the cancellation bookkeeping.
        item: Work item just taken off the queue.

    Returns:
        True if the item was cancelled and must not be synthesized.
    """
    if not state.is_cancelled(item.message_id):
        return False
    logger.debug("Skipping cancelled message %s", item.message_id)
    state.mark_skipped(item.message_id)
    return True


def _load_default_engine(state: ServerState) -> LoadedEngine | None:
    """Load the default engine's model on the calling (worker) thread.

    MLX GPU streams are thread-local, so the model must be loaded on the same
    thread that later calls generate; loading on one thread and generating on
    another raises "no Stream(gpu, N) in current thread" (the same failure
    fixed for the CLI in audio_worker_from_model_id). The load outcome is
    reported through state.ready_queue so startup failures surface on the
    caller's thread and abort startup.

    Only the default engine is loaded here. Other engines load lazily on first
    use — see _resolve_engine, which must NOT signal ready_queue and must NOT
    kill the worker.

    Args:
        state: Server state holding the engine registry.

    Returns:
        The loaded default engine, or None if loading failed.
    """
    try:
        loaded = state.registry.get(state.default_engine)
    except BaseException as exc:
        logger.error("Model load failed in audio worker: %s", exc)
        state.mark_engine_failed(state.default_engine, str(exc))
        state.ready_queue.put(exc)
        return None
    state.mark_engine_loaded(state.default_engine)
    state.ready_queue.put(None)
    return loaded


def _resolve_engine(state: ServerState, item: WorkItem) -> LoadedEngine | None:
    """Return the engine for this item, loading its model on first use.

    Unlike _load_default_engine this never touches ready_queue and never kills
    the worker: a bad non-default engine must not take down an already-working
    default. The failure is recorded on the item and on the engine, and the
    registry caches it so later items for the same engine fail immediately
    instead of retrying a load that takes ~20 seconds to fail.

    Args:
        state: Server state holding the engine registry.
        item: Work item naming the engine to resolve.

    Returns:
        The loaded engine, or None if it could not be loaded.
    """
    if not state.registry.is_loaded(item.engine):
        with state.status_lock:
            status = state.statuses.get(item.message_id)
            if status is not None:
                status.status = "loading"

    try:
        loaded = state.registry.get(item.engine)
    except Exception as exc:
        logger.error("Engine '%s' failed to load for %s: %s", item.engine, item.message_id, exc)
        state.mark_engine_failed(item.engine, str(exc))
        _fail_item(state, item.message_id, f"engine '{item.engine}' failed to load: {exc}")
        return None

    state.mark_engine_loaded(item.engine)
    return loaded


def _generate_item(state: ServerState, loaded: LoadedEngine, item: WorkItem) -> list[np.ndarray] | None:
    """Generate and optionally normalize audio chunks for one work item.

    Args:
        state: Server state with normalization settings and meter.
        loaded: Engine and model for this item (loaded on the worker thread).
        item: Work item to synthesize.

    Returns:
        Audio chunks for the item, or None if generation failed (the failure is
        recorded on the item's status).
    """
    try:
        chunks = generate_chunks(loaded.engine, loaded.model, item.text, item.voice, item.instruct, state.streaming_interval)
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
) -> tuple[Callable[[Path | None], None], Callable[[Exception], None], Callable[[], None]]:
    """Build on_complete/on_error/on_cancel callbacks that record playback status for one item.

    Exactly one of them fires per item, and each releases ``done`` so the worker
    moves on to the next queued message.
    """

    def on_complete(output_path: Path | None) -> None:
        with state.status_lock:
            ms = state.statuses[item.message_id]
            ms.status = "completed"
            ms.audio_file = str(output_path) if output_path is not None else None
            ms.completed_at = time.time()
        state.end_playback(item.message_id)
        logger.debug("Playback completed for %s -> %s", item.message_id, output_path)
        done.set()

    def on_error(exc: Exception) -> None:
        logger.error("Playback failed for %s: %s", item.message_id, exc)
        with state.status_lock:
            ms = state.statuses[item.message_id]
            ms.status = "error"
            ms.error = str(exc)
            ms.completed_at = time.time()
        state.end_playback(item.message_id)
        done.set()

    def on_cancel() -> None:
        with state.status_lock:
            ms = state.statuses[item.message_id]
            ms.status = "cancelled"
            ms.completed_at = time.time()
        state.end_playback(item.message_id)
        logger.debug("Playback cancelled for %s", item.message_id)
        done.set()

    return on_complete, on_error, on_cancel


def _run_streaming_server_loop(state: ServerState, player: AudioPlayer) -> None:
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

            if _skip_if_cancelled(state, item):
                state.work_queue.task_done()
                continue

            loaded = _resolve_engine(state, item)
            if loaded is None:
                state.work_queue.task_done()
                continue

            cancel, pause = state.begin_playback(item.message_id)
            with state.status_lock:
                state.statuses[item.message_id].status = "playing"

            done = threading.Event()
            on_complete, on_error, on_cancel = _playback_status_callbacks(state, item, done)
            output_path = make_output_path(OUTPUT_DIR) if state.save_wav else None

            try:
                play_stream(
                    player,
                    streaming_chunk_iter(loaded.engine, loaded.model, item.text, item.voice, item.instruct, settings),
                    output_path,
                    on_complete,
                    on_error,
                    on_cancel=on_cancel,
                    cancel=cancel,
                    pause=pause,
                )
            except (RuntimeError, ValueError) as exc:
                logger.error("TTS generation failed for %s: %s", item.message_id, exc)
                _fail_item(state, item.message_id, str(exc))
                done.set()

            done.wait()
            state.end_playback(item.message_id)
            state.work_queue.task_done()

        except Exception as exc:
            logger.error("Audio worker caught unexpected error (recovering): %s", exc, exc_info=True)
            if current_item is not None:
                _fail_item(state, current_item.message_id, f"unexpected worker error: {exc}")
                state.end_playback(current_item.message_id)
                with contextlib.suppress(ValueError):
                    state.work_queue.task_done()


def _prepare_buffered_item(state: ServerState, item: WorkItem) -> tuple[WorkItem, list[np.ndarray]] | None:
    """Synthesize one queued message, ready for playback.

    Cancellation is checked twice: before any work, so a cancelled message costs
    nothing, and again after generation, because a cancel that lands while the
    model is running still means nobody wants to hear the result.

    Args:
        state: Server state with the engine registry and status tracking.
        item: Work item just taken off the queue.

    Returns:
        The item paired with its audio, or None if it was cancelled or failed.
    """
    if _skip_if_cancelled(state, item):
        return None

    loaded = _resolve_engine(state, item)
    if loaded is None:
        return None

    chunks = _generate_item(state, loaded, item)
    if chunks is None or _skip_if_cancelled(state, item):
        return None

    return (item, chunks)


def _run_buffered_server_loop(state: ServerState, player: AudioPlayer) -> None:
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
            pending = _prepare_buffered_item(state, item)
            state.work_queue.task_done()

        except Exception as exc:
            logger.error(
                "Audio worker caught unexpected error (recovering): %s",
                exc,
                exc_info=True,
            )
            if current_item is not None:
                _fail_item(state, current_item.message_id, f"unexpected worker error: {exc}")
                state.end_playback(current_item.message_id)
                with contextlib.suppress(ValueError):
                    state.work_queue.task_done()

    if pending is not None:
        playback_done = _start_playback(state, player, pending, playback_done)
        playback_done.wait()


def server_audio_worker(state: ServerState) -> None:
    """Background worker that processes queued TTS requests sequentially.

    Loads the default engine's model on this thread (see _load_default_engine),
    because MLX GPU streams are thread-local; other engines load lazily on this
    same thread when an item first asks for them. Dispatches to the streaming or
    buffered loop based on state.stream. Both wrap each iteration in a top-level
    handler so unexpected exceptions are logged and the worker keeps running
    instead of dying silently.

    Args:
        state: Server state with work queue, engine registry, and status tracking.
    """
    if _load_default_engine(state) is None:
        return

    player = AudioPlayer(state.sample_rate, state.lead_silence_ms)
    try:
        if state.stream:
            _run_streaming_server_loop(state, player)
        else:
            _run_buffered_server_loop(state, player)
    finally:
        player.close()


@dataclasses.dataclass(frozen=True)
class _EngineConfig:
    """One engine declared in config.yaml.

    Attributes:
        kind: Engine identifier — one of ENGINE_KINDS.
        model_path: Model directory for this engine.
        language: Language for the qwen3 engine; must be None for voxtral.
        default_voice: Voice used when a request names this engine but no voice.
    """

    kind: str
    model_path: str
    language: str | None
    default_voice: str


@dataclasses.dataclass(frozen=True)
class _ServerConfig:
    """Parsed server configuration from config.yaml."""

    engines: tuple[_EngineConfig, ...]
    default_engine: str
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


def _reject_per_engine_sample_rates(config: dict[str, object], sample_rate: int) -> None:
    """Reject a per-engine ``sample_rate`` that disagrees with the global one.

    The AudioPlayer and the loudness meter are built once per worker, so a
    second engine at a different rate would be pitch-shifted and mis-metered.
    Supporting that properly means reopening the output stream on every engine
    switch, which the player's own docstring warns degrades the CoreAudio HAL.
    Fail loudly instead of pretending.

    Args:
        config: Raw config mapping.
        sample_rate: The global sample rate every engine must agree with.

    Raises:
        ValueError: If an engine declares a different sample rate.
    """
    engines_block = config.get("engines")
    if not isinstance(engines_block, dict):
        return
    for kind, block in cast(dict[str, object], engines_block).items():
        if not isinstance(block, dict):
            continue
        declared = cast(dict[str, object], block).get("sample_rate")
        if declared is not None and int(cast(int, declared)) != sample_rate:
            msg = (
                f"engines.{kind}.sample_rate ({declared}) differs from the global sample_rate "
                f"({sample_rate}). Per-engine sample rates are not supported."
            )
            raise ValueError(msg)


def _parse_legacy_engine(config: dict[str, object], default_voice: str) -> tuple[_EngineConfig, ...]:
    """Build a single-engine tuple from the flat ``engine:``/``model:`` keys.

    Args:
        config: Raw config mapping.
        default_voice: Top-level default voice, used as this engine's default.

    Returns:
        A one-entry tuple describing the configured engine.

    Raises:
        ValueError: If 'engine' or 'language' has the wrong type.
        FileNotFoundError: If the model directory does not exist.
    """
    model_path = _require(config, "model")
    if not isinstance(model_path, str) or not Path(model_path).exists():
        msg = f"Model directory does not exist: {model_path!r}"
        raise FileNotFoundError(msg)

    kind = _require(config, "engine")
    if not isinstance(kind, str):
        msg = "'engine' in config.yaml must be a string"
        raise ValueError(msg)

    language = config.get("language")
    if language is not None and not isinstance(language, str):
        msg = "'language' in config.yaml must be a string"
        raise ValueError(msg)

    return (_EngineConfig(kind=kind, model_path=model_path, language=language, default_voice=default_voice),)


def _parse_engine_block(kind: str, block: object, default_voice: str) -> _EngineConfig:
    """Parse one entry of the ``engines:`` mapping.

    Args:
        kind: Engine identifier this block is keyed by.
        block: Raw mapping for this engine.
        default_voice: Fallback default voice when the block omits one.

    Returns:
        The parsed engine declaration.

    Raises:
        ValueError: If the block is malformed or a value has the wrong type.
    """
    if not isinstance(block, dict):
        msg = f"engines.{kind} in config.yaml must be a mapping"
        raise ValueError(msg)
    settings = cast(dict[str, object], block)

    model_path = settings.get("model")
    if not isinstance(model_path, str):
        msg = f"engines.{kind}.model in config.yaml must be a string"
        raise ValueError(msg)

    language = settings.get("language")
    if language is not None and not isinstance(language, str):
        msg = f"engines.{kind}.language in config.yaml must be a string"
        raise ValueError(msg)

    voice = settings.get("default_voice", default_voice)
    if not isinstance(voice, str):
        msg = f"engines.{kind}.default_voice in config.yaml must be a string"
        raise ValueError(msg)

    return _EngineConfig(kind=kind, model_path=model_path, language=language, default_voice=voice)


def _parse_engines(config: dict[str, object], default_voice: str) -> tuple[tuple[_EngineConfig, ...], str]:
    """Parse the engine declarations, accepting both the flat and mapping forms.

    The flat ``engine:``/``model:``/``language:`` keys and the ``engines:``
    mapping are mutually exclusive — silent precedence between two forms is a
    bug factory, so declaring both is an error.

    Args:
        config: Raw config mapping.
        default_voice: Top-level default voice.

    Returns:
        The declared engines and the default engine's kind.

    Raises:
        ValueError: If both forms are present, if 'engines' is empty or
            malformed, or if 'default_engine' is missing or unknown.
    """
    engines_block = config.get("engines")
    legacy_keys = [key for key in ("engine", "model", "language") if key in config]

    if engines_block is None:
        return _parse_legacy_engine(config, default_voice), cast(str, config["engine"])

    if legacy_keys:
        msg = f"config.yaml declares both 'engines:' and the flat key(s) {', '.join(legacy_keys)}. Use one form or the other, not both."
        raise ValueError(msg)

    if not isinstance(engines_block, dict) or not engines_block:
        msg = "'engines' in config.yaml must be a non-empty mapping of engine name to settings"
        raise ValueError(msg)

    declared_engines = cast(dict[str, object], engines_block)
    engines: tuple[_EngineConfig, ...] = tuple(_parse_engine_block(kind, block, default_voice) for kind, block in declared_engines.items())

    default_engine = config.get("default_engine")
    if default_engine is None:
        if len(engines) > 1:
            msg = "'default_engine' in config.yaml is required when 'engines' declares more than one engine"
            raise ValueError(msg)
        default_engine = next(iter(declared_engines))
    if not isinstance(default_engine, str):
        msg = "'default_engine' in config.yaml must be a string"
        raise ValueError(msg)
    if all(engine.kind != default_engine for engine in engines):
        declared = ", ".join(engine.kind for engine in engines)
        msg = f"default_engine '{default_engine}' is not declared in 'engines'. Declared: {declared}"
        raise ValueError(msg)

    return engines, default_engine


def _parse_server_config() -> _ServerConfig:
    """Load and validate server settings from config.yaml. Fails fast on missing keys."""
    config = load_config()

    default_voice = _require(config, "default_voice")
    if not isinstance(default_voice, str):
        msg = "'default_voice' in config.yaml must be a string"
        raise ValueError(msg)

    engines, default_engine = _parse_engines(config, default_voice)

    sample_rate = int(cast(int, _require(config, "sample_rate")))
    _reject_per_engine_sample_rates(config, sample_rate)

    return _ServerConfig(
        engines=engines,
        default_engine=default_engine,
        sample_rate=sample_rate,
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


def _discover_engine_voices(cfg: _ServerConfig) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Discover each declared engine's voices without loading any model.

    Voice discovery reads only from the model directory, so every engine's
    voices are known up front even though the models load lazily. The default
    engine must work — a broken default is a broken server. A non-default
    engine that cannot be read (typically its model was never downloaded) is
    logged and marked unavailable, so declaring an engine can never take the
    server down for everyone else.

    Args:
        cfg: Parsed server configuration.

    Returns:
        Voices per available engine, and errors per unavailable engine.

    Raises:
        Exception: Whatever the default engine's discovery raised.
    """
    voices_by_engine: dict[str, list[str]] = {}
    errors: dict[str, str] = {}

    for engine_cfg in cfg.engines:
        is_default = engine_cfg.kind == cfg.default_engine
        try:
            engine = build_engine(engine_cfg.kind, engine_cfg.language)
            voices_by_engine[engine_cfg.kind] = engine.discover_voices(Path(engine_cfg.model_path))
        except Exception as exc:
            if is_default:
                raise
            logger.warning(
                "Engine '%s' is unavailable and will be rejected at /say: %s",
                engine_cfg.kind,
                exc,
            )
            errors[engine_cfg.kind] = str(exc)

    return voices_by_engine, errors


def _build_server_state(cfg: _ServerConfig) -> ServerState:
    """Assemble a ServerState from parsed config.

    No MLX model is loaded here. Models are loaded lazily by the audio worker on
    its own thread (see server_audio_worker), because MLX GPU streams are
    thread-local; the default engine is loaded eagerly at startup so first-say
    latency is unchanged.
    """
    voices_by_engine, engine_errors = _discover_engine_voices(cfg)

    default_voices = voices_by_engine[cfg.default_engine]
    if cfg.default_voice not in default_voices:
        msg = f"default_voice '{cfg.default_voice}' not found. Available: {', '.join(default_voices)}"
        raise ValueError(msg)

    for engine_cfg in cfg.engines:
        engine_voices = voices_by_engine.get(engine_cfg.kind)
        if engine_voices is not None and engine_cfg.default_voice not in engine_voices:
            msg = (
                f"default_voice '{engine_cfg.default_voice}' for engine '{engine_cfg.kind}' not found. "
                f"Available: {', '.join(engine_voices)}"
            )
            raise ValueError(msg)

    registry = EngineRegistry(
        {
            engine_cfg.kind: EngineSpec(
                kind=engine_cfg.kind,
                model_path=engine_cfg.model_path,
                language=engine_cfg.language,
            )
            for engine_cfg in cfg.engines
        }
    )

    state = ServerState(
        registry=registry,
        voices_by_engine=voices_by_engine,
        default_engine=cfg.default_engine,
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

    for kind, error in engine_errors.items():
        state.mark_engine_failed(kind, error)

    return state


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
