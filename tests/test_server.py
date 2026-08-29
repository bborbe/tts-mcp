"""Tests for the FastAPI TTS server."""

import re
import threading
import time
from typing import Any, cast
from unittest.mock import MagicMock, patch

import numpy as np
import pyloudnorm as pyln
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.server import (
    RECENT_HISTORY_LIMIT,
    STATUS_TTL_SECONDS,
    MessageStatus,
    ServerState,
    WorkItem,
    _fail_item,
    router,
    server_audio_worker,
)
from src.tts import (
    QWEN3,
    VOXTRAL,
    AudioPlayer,
    EngineRegistry,
    EngineSpec,
    LoadedEngine,
    VoxtralEngine,
)

DEFAULT_SPECS = {
    VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="test-model-path", language=None),
}


def _make_state(
    voices: list[str] | None = None,
    default_voice: str = "casual_female",
    sample_rate: int = 24000,
    simplify_punctuation: bool = False,
    save_wav: bool = True,
    lead_silence_ms: int = 200,
    normalize_audio: bool = False,
    target_lufs: float = -20.0,
    true_peak_ceiling_db: float = -1.0,
    min_duration_seconds: float = 0.5,
    meter: pyln.Meter | None = None,
    preload_model: bool = True,
    model_path: str = "test-model-path",
    stream: bool = False,
    streaming_interval: float = 1.0,
    streaming_warmup_seconds: float = 2.0,
    registry: EngineRegistry | None = None,
    voices_by_engine: dict[str, list[str]] | None = None,
    default_engine: str = VOXTRAL,
) -> ServerState:
    """Create a ServerState for testing.

    By default a single voxtral engine is declared with a mock model already in
    the registry cache (preload_model=True). Pass preload_model=False to leave
    the cache empty so the worker loads it on its own thread, mirroring the
    production code path. Pass a registry to declare multiple engines.
    """
    if voices is None:
        voices = ["casual_female", "casual_male"]
    if voices_by_engine is None:
        voices_by_engine = {default_engine: voices}
    if registry is None:
        specs = {VOXTRAL: EngineSpec(kind=VOXTRAL, model_path=model_path, language=None)}
        registry = EngineRegistry(specs, loader=lambda _path: MagicMock())
    if preload_model and not registry.is_loaded(default_engine):
        registry.preload(default_engine, LoadedEngine(engine=VoxtralEngine(), model=MagicMock()))
    if meter is None:
        meter = pyln.Meter(float(sample_rate))
    state = ServerState(
        registry=registry,
        voices_by_engine=voices_by_engine,
        default_engine=default_engine,
        default_voice=default_voice,
        sample_rate=sample_rate,
        lead_silence_ms=lead_silence_ms,
        simplify_punctuation=simplify_punctuation,
        save_wav=save_wav,
        normalize_audio=normalize_audio,
        target_lufs=target_lufs,
        true_peak_ceiling_db=true_peak_ceiling_db,
        min_duration_seconds=min_duration_seconds,
        meter=meter,
        stream=stream,
        streaming_interval=streaming_interval,
        streaming_warmup_seconds=streaming_warmup_seconds,
    )
    if preload_model:
        # Mirror what the worker reports after loading, so /voices sees it.
        state.mark_engine_loaded(default_engine)
    return state


def _model_of(state: ServerState, engine: str | None = None) -> Any:
    """Return the mock model cached for an engine, for stubbing generate()."""
    return cast(Any, state.registry.get(engine or state.default_engine).model)


def _make_app(state: ServerState) -> FastAPI:
    """Create a test FastAPI app with the given state."""
    app = FastAPI()
    app.state.server = state
    app.include_router(router)
    return app


def _finish_if_cancelled(job: Any) -> bool:
    """Mirror the real player's cancellation contract in the fake."""
    if job.cancel is None or not job.cancel.is_set():
        return False
    if job.on_cancel is not None:
        job.on_cancel()
    elif job.on_complete is not None:
        job.on_complete(None)
    return True


class _ImmediateAudioPlayer:
    """Synchronous fake for server worker tests."""

    playback_error: Exception | None = None
    active_count = 0
    max_active_count = 0

    def __init__(self, sample_rate: int, lead_silence_ms: int) -> None:
        self._sample_rate = sample_rate
        self._lead_silence_ms = lead_silence_ms

    def submit(self, job: Any) -> None:
        _ImmediateAudioPlayer.active_count += 1
        _ImmediateAudioPlayer.max_active_count = max(
            _ImmediateAudioPlayer.max_active_count,
            _ImmediateAudioPlayer.active_count,
        )
        try:
            if _ImmediateAudioPlayer.playback_error is not None:
                if job.on_error is not None:
                    job.on_error(_ImmediateAudioPlayer.playback_error)
                return
            if _finish_if_cancelled(job):
                return
            if job.on_complete is not None:
                job.on_complete(job.output_path)
        finally:
            _ImmediateAudioPlayer.active_count -= 1

    def submit_stream(self, job: Any) -> None:
        # Mirror the real player: drain the chunk queue on a background thread so
        # the producer (play_stream) can feed chunks after submit_stream returns.
        def _run() -> None:
            while job.chunk_source.get() is not None:
                pass
            if _ImmediateAudioPlayer.playback_error is not None:
                if job.on_error is not None:
                    job.on_error(_ImmediateAudioPlayer.playback_error)
                return
            if _finish_if_cancelled(job):
                return
            if job.on_complete is not None:
                job.on_complete(job.output_path)

        threading.Thread(target=_run, daemon=True).start()

    def close(self) -> None:
        return


@pytest.fixture(autouse=True)
def _use_immediate_audio_player(monkeypatch: pytest.MonkeyPatch) -> None:
    _ImmediateAudioPlayer.playback_error = None
    _ImmediateAudioPlayer.active_count = 0
    _ImmediateAudioPlayer.max_active_count = 0
    monkeypatch.setattr("src.server.AudioPlayer", _ImmediateAudioPlayer)


class TestHealth:
    """Tests for GET /health."""

    def test_returns_ok(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestVoices:
    """Tests for GET /voices."""

    def test_returns_voices_and_default(self):
        state = _make_state(voices=["casual_female", "neutral_male"], default_voice="casual_female")
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/voices")

        assert response.status_code == 200
        data = response.json()
        assert data["voices"] == ["casual_female", "neutral_male"]
        assert data["default_voice"] == "casual_female"


def _multi_engine_state(
    loader: Any = None,
    preload_default: bool = True,
    stream: bool = False,
) -> ServerState:
    """State declaring both engines, with only the default preloaded."""
    specs = {
        VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="voxtral-path", language=None),
        QWEN3: EngineSpec(kind=QWEN3, model_path="qwen3-path", language="English"),
    }
    registry = EngineRegistry(specs, loader=loader or (lambda _path: MagicMock()))
    return _make_state(
        registry=registry,
        preload_model=preload_default,
        voices_by_engine={
            VOXTRAL: ["casual_female", "casual_male"],
            QWEN3: ["ryan", "serena"],
        },
        default_engine=VOXTRAL,
        default_voice="casual_female",
        stream=stream,
    )


class TestMultiEngineVoices:
    """Tests for GET /voices with more than one engine declared."""

    def test_flat_voices_are_the_union(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        data = client.get("/voices").json()

        assert data["voices"] == ["ryan", "serena", "casual_female", "casual_male"]
        assert data["default_voice"] == "casual_female"
        assert data["default_engine"] == VOXTRAL

    def test_engines_are_grouped(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        engines = {entry["engine"]: entry for entry in client.get("/voices").json()["engines"]}

        assert engines[QWEN3]["voices"] == ["ryan", "serena"]
        assert engines[VOXTRAL]["voices"] == ["casual_female", "casual_male"]
        assert engines[QWEN3]["language"] == "English"
        assert engines[VOXTRAL]["language"] is None

    def test_only_qwen3_supports_instruct(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        engines = {entry["engine"]: entry for entry in client.get("/voices").json()["engines"]}

        assert engines[QWEN3]["supports_instruct"] is True
        assert engines[VOXTRAL]["supports_instruct"] is False

    def test_reports_which_engines_are_resident(self) -> None:
        state = _multi_engine_state()
        client = TestClient(_make_app(state))

        engines = {entry["engine"]: entry for entry in client.get("/voices").json()["engines"]}
        assert engines[VOXTRAL]["loaded"] is True
        assert engines[QWEN3]["loaded"] is False

        state.mark_engine_loaded(QWEN3)

        engines = {entry["engine"]: entry for entry in client.get("/voices").json()["engines"]}
        assert engines[QWEN3]["loaded"] is True

    def test_unavailable_engine_is_reported(self) -> None:
        state = _multi_engine_state()
        state.voices_by_engine.pop(QWEN3)
        state.mark_engine_failed(QWEN3, "model directory missing")
        client = TestClient(_make_app(state))

        engines = {entry["engine"]: entry for entry in client.get("/voices").json()["engines"]}

        assert engines[QWEN3]["available"] is False
        assert engines[QWEN3]["error"] == "model directory missing"
        assert "ryan" not in client.get("/voices").json()["voices"]


class TestSayEngineSelection:
    """Tests for choosing an engine on POST /say."""

    def test_omitted_engine_uses_the_default(self) -> None:
        state = _multi_engine_state()
        client = TestClient(_make_app(state))

        response = client.post("/say", json={"text": "Hello"})

        assert response.status_code == 202
        assert state.work_queue.get_nowait().engine == VOXTRAL

    def test_explicit_engine_is_honored(self) -> None:
        state = _multi_engine_state()
        client = TestClient(_make_app(state))

        response = client.post("/say", json={"text": "Hello", "voice": "ryan", "engine": QWEN3})

        assert response.status_code == 202
        item = state.work_queue.get_nowait()
        assert item.engine == QWEN3
        assert item.voice == "ryan"

    def test_unknown_engine_returns_400(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        response = client.post("/say", json={"text": "Hello", "engine": "piper"})

        assert response.status_code == 400
        assert "Unknown engine 'piper'" in response.json()["detail"]

    def test_unavailable_engine_returns_400(self) -> None:
        state = _multi_engine_state()
        state.voices_by_engine.pop(QWEN3)
        state.mark_engine_failed(QWEN3, "model directory missing")
        client = TestClient(_make_app(state))

        response = client.post("/say", json={"text": "Hello", "voice": "ryan", "engine": QWEN3})

        assert response.status_code == 400
        assert "unavailable" in response.json()["detail"]
        assert "model directory missing" in response.json()["detail"]

    def test_voice_from_another_engine_returns_400(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        response = client.post("/say", json={"text": "Hello", "voice": "ryan", "engine": VOXTRAL})

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "not available on engine 'voxtral'" in detail
        assert "casual_female" in detail

    def test_instruct_is_rejected_for_voxtral_up_front(self) -> None:
        """The engine raises on instruct anyway, but only inside the worker —
        long after the caller got its 202 and stopped looking.
        """
        client = TestClient(_make_app(_multi_engine_state()))

        response = client.post("/say", json={"text": "Hello", "instruct": "Happy.", "engine": VOXTRAL})

        assert response.status_code == 400
        assert "'instruct' is not supported by the voxtral engine" in response.json()["detail"]

    def test_instruct_is_accepted_for_qwen3(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        response = client.post(
            "/say",
            json={"text": "Hello", "voice": "ryan", "engine": QWEN3, "instruct": "Happy."},
        )

        assert response.status_code == 202

    def test_status_reports_the_engine(self) -> None:
        client = TestClient(_make_app(_multi_engine_state()))

        message_id = client.post("/say", json={"text": "Hello", "voice": "ryan", "engine": QWEN3}).json()["message_id"]

        assert client.get(f"/status/{message_id}").json()["engine"] == QWEN3


class TestMessageId:
    """Tests for message ID generation."""

    def test_format_matches_pattern(self):
        state = _make_state()
        msg_id = state.next_message_id()
        assert re.match(r"^msg_\d{8}_\d{6}_\d{3}$", msg_id)

    def test_counter_increments(self):
        state = _make_state()
        id1 = state.next_message_id()
        id2 = state.next_message_id()
        c1 = int(id1.rsplit("_", 1)[1])
        c2 = int(id2.rsplit("_", 1)[1])
        assert c2 == c1 + 1

    def test_ids_are_unique(self):
        state = _make_state()
        ids = {state.next_message_id() for _ in range(100)}
        assert len(ids) == 100


class TestSay:
    """Tests for POST /say."""

    def test_valid_text_returns_202(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={"text": "Hello world"})

        assert response.status_code == 202
        data = response.json()
        assert "message_id" in data
        assert data["status"] == "queued"
        assert data["queue_position"] >= 0

    def test_empty_text_returns_422(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={"text": ""})

        assert response.status_code == 422

    def test_whitespace_only_text_returns_422(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={"text": "   "})

        assert response.status_code == 422

    def test_missing_text_returns_422(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={})

        assert response.status_code == 422

    def test_voice_override_accepted(self):
        state = _make_state(voices=["casual_female", "casual_male"])
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={"text": "Hello", "voice": "casual_male"})

        assert response.status_code == 202

    def test_unknown_voice_returns_400(self):
        state = _make_state(voices=["casual_female"])
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={"text": "Hello", "voice": "nonexistent"})

        assert response.status_code == 400
        assert "nonexistent" in response.json()["detail"]
        assert "casual_female" in response.json()["detail"]

    def test_creates_status_entry(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.post("/say", json={"text": "Hello"})

        msg_id = response.json()["message_id"]
        with state.status_lock:
            assert msg_id in state.statuses
            ms = state.statuses[msg_id]
            assert ms.status == "queued"
            assert ms.text == "Hello"

    def test_queues_work_item(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        client.post("/say", json={"text": "Hello", "voice": "casual_male"})

        item = state.work_queue.get_nowait()
        assert isinstance(item, WorkItem)
        assert item.text == "Hello"
        assert item.voice == "casual_male"

    def test_uses_default_voice_when_not_specified(self):
        state = _make_state(default_voice="casual_female")
        app = _make_app(state)
        client = TestClient(app)

        client.post("/say", json={"text": "Hello"})

        item = state.work_queue.get_nowait()
        assert item is not None
        assert item.voice == "casual_female"

    def test_applies_text_cleaning(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        client.post("/say", json={"text": "  hello   world  "})

        item = state.work_queue.get_nowait()
        assert item is not None
        assert item.text == "hello world"

    def test_applies_simplify_punctuation_when_enabled(self):
        state = _make_state(simplify_punctuation=True)
        app = _make_app(state)
        client = TestClient(app)

        client.post("/say", json={"text": "Hello, world!"})

        item = state.work_queue.get_nowait()
        assert item is not None
        assert item.text == "Hello world."

    def test_multiple_requests_get_sequential_positions(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        r1 = client.post("/say", json={"text": "First"})
        r2 = client.post("/say", json={"text": "Second"})
        r3 = client.post("/say", json={"text": "Third"})

        p1 = r1.json()["queue_position"]
        p2 = r2.json()["queue_position"]
        p3 = r3.json()["queue_position"]
        assert p1 < p2 < p3


class TestStatus:
    """Tests for GET /status/{message_id}."""

    def test_known_queued_message(self):
        state = _make_state()
        with state.status_lock:
            state.statuses["msg_test_001"] = MessageStatus(
                message_id="msg_test_001",
                status="queued",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/status/msg_test_001")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["text"] == "Hello"
        assert data["audio_file"] is None

    def test_known_completed_message(self):
        state = _make_state()
        with state.status_lock:
            state.statuses["msg_test_002"] = MessageStatus(
                message_id="msg_test_002",
                status="completed",
                text="Done",
                audio_file="data/output/speech_20260331_120000.wav",
                error=None,
                completed_at=time.time(),
            )
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/status/msg_test_002")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["audio_file"] == "data/output/speech_20260331_120000.wav"

    def test_known_errored_message(self):
        state = _make_state()
        with state.status_lock:
            state.statuses["msg_test_003"] = MessageStatus(
                message_id="msg_test_003",
                status="error",
                text="Bad",
                audio_file=None,
                error="Model failed",
                completed_at=time.time(),
            )
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/status/msg_test_003")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert data["error"] == "Model failed"

    def test_unknown_message_returns_404(self):
        state = _make_state()
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/status/nonexistent")

        assert response.status_code == 404


class TestEviction:
    """Tests for status entry eviction."""

    def test_expired_completed_entry_evicted(self):
        state = _make_state()
        expired_time = time.time() - STATUS_TTL_SECONDS - 1
        with state.status_lock:
            state.statuses["msg_old"] = MessageStatus(
                message_id="msg_old",
                status="completed",
                text="Old",
                audio_file="out.wav",
                error=None,
                completed_at=expired_time,
            )

        state.evict_expired()

        with state.status_lock:
            assert "msg_old" not in state.statuses

    def test_expired_error_entry_evicted(self):
        state = _make_state()
        expired_time = time.time() - STATUS_TTL_SECONDS - 1
        with state.status_lock:
            state.statuses["msg_err"] = MessageStatus(
                message_id="msg_err",
                status="error",
                text="Err",
                audio_file=None,
                error="failed",
                completed_at=expired_time,
            )

        state.evict_expired()

        with state.status_lock:
            assert "msg_err" not in state.statuses

    def test_queued_entry_never_evicted(self):
        state = _make_state()
        with state.status_lock:
            state.statuses["msg_queued"] = MessageStatus(
                message_id="msg_queued",
                status="queued",
                text="Waiting",
                audio_file=None,
                error=None,
                completed_at=None,
            )

        state.evict_expired()

        with state.status_lock:
            assert "msg_queued" in state.statuses

    def test_recent_completed_not_evicted(self):
        state = _make_state()
        with state.status_lock:
            state.statuses["msg_recent"] = MessageStatus(
                message_id="msg_recent",
                status="completed",
                text="Recent",
                audio_file="out.wav",
                error=None,
                completed_at=time.time(),
            )

        state.evict_expired()

        with state.status_lock:
            assert "msg_recent" in state.statuses

    def test_eviction_triggered_by_status_endpoint(self):
        state = _make_state()
        expired_time = time.time() - STATUS_TTL_SECONDS - 1
        with state.status_lock:
            state.statuses["msg_old"] = MessageStatus(
                message_id="msg_old",
                status="completed",
                text="Old",
                audio_file="out.wav",
                error=None,
                completed_at=expired_time,
            )
        app = _make_app(state)
        client = TestClient(app)

        response = client.get("/status/msg_old")

        assert response.status_code == 404


def _queue_status(state: ServerState, message_id: str, status: str = "queued") -> None:
    """Register a message in the status dict, as POST /say would."""
    with state.status_lock:
        state.statuses[message_id] = MessageStatus(
            message_id=message_id,
            status=status,
            text=f"text for {message_id}",
            audio_file=None,
            error=None,
            completed_at=None,
        )


class TestCancel:
    """Tests for POST /cancel."""

    def test_empty_body_cancels_what_is_playing(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        cancel, _ = state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        response = client.post("/cancel")

        assert response.status_code == 200
        assert response.json()["cancelled"] == ["msg_playing"]
        assert cancel.is_set()

    def test_nothing_playing_cancels_nothing(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.post("/cancel")

        assert response.status_code == 200
        assert response.json()["cancelled"] == []

    def test_queued_message_is_marked_cancelled(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_queued")
        client = TestClient(_make_app(state))

        response = client.post("/cancel", json={"message_id": "msg_queued"})

        assert response.json()["cancelled"] == ["msg_queued"]
        assert state.is_cancelled("msg_queued")
        with state.status_lock:
            assert state.statuses["msg_queued"].status == "cancelled"

    def test_playing_message_keeps_its_status_until_the_player_stops(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        client.post("/cancel", json={"message_id": "msg_playing"})

        # The player owns the final status: reporting "cancelled" here would
        # claim silence while audio is still leaving the speakers.
        with state.status_lock:
            assert state.statuses["msg_playing"].status == "playing"

    def test_all_cancels_playing_and_queued(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        state.begin_playback("msg_playing")
        _queue_status(state, "msg_a")
        _queue_status(state, "msg_b")
        client = TestClient(_make_app(state))

        response = client.post("/cancel", json={"all": True})

        assert sorted(response.json()["cancelled"]) == ["msg_a", "msg_b", "msg_playing"]
        with state.status_lock:
            assert state.statuses["msg_a"].status == "cancelled"
            assert state.statuses["msg_b"].status == "cancelled"

    def test_finished_message_is_not_cancellable(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_done", status="completed")
        client = TestClient(_make_app(state))

        response = client.post("/cancel", json={"message_id": "msg_done"})

        assert response.json()["cancelled"] == []
        with state.status_lock:
            assert state.statuses["msg_done"].status == "completed"

    def test_unknown_message_returns_404(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.post("/cancel", json={"message_id": "msg_nope"})

        assert response.status_code == 404

    def test_reports_remaining_queue_length(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_a")
        state.work_queue.put(WorkItem(message_id="msg_a", text="A", voice="casual_female", instruct=None, engine=VOXTRAL))
        client = TestClient(_make_app(state))

        response = client.post("/cancel", json={"message_id": "msg_a"})

        assert response.json()["queued"] == 1


class TestPause:
    """Tests for POST /pause."""

    def test_empty_body_pauses_what_is_playing(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        _, pause = state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        response = client.post("/pause")

        assert response.status_code == 200
        assert response.json()["paused"] == ["msg_playing"]
        assert pause.is_set()
        with state.status_lock:
            assert state.statuses["msg_playing"].status == "paused"

    def test_nothing_playing_pauses_nothing(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.post("/pause")

        assert response.status_code == 200
        assert response.json()["paused"] == []

    def test_named_message_is_paused(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        _, pause = state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        response = client.post("/pause", json={"message_id": "msg_playing"})

        assert response.json()["paused"] == ["msg_playing"]
        assert pause.is_set()

    def test_queued_message_cannot_be_paused(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_queued")
        client = TestClient(_make_app(state))

        response = client.post("/pause", json={"message_id": "msg_queued"})

        assert response.json()["paused"] == []
        with state.status_lock:
            assert state.statuses["msg_queued"].status == "queued"

    def test_paused_message_is_still_cancellable(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        cancel, _ = state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        client.post("/pause")
        response = client.post("/cancel", json={"message_id": "msg_playing"})

        assert response.json()["cancelled"] == ["msg_playing"]
        assert cancel.is_set()

    def test_unknown_message_returns_404(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.post("/pause", json={"message_id": "msg_nope"})

        assert response.status_code == 404


class TestResume:
    """Tests for POST /resume."""

    def test_empty_body_resumes_what_is_paused(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_paused", status="paused")
        _, pause = state.begin_playback("msg_paused")
        state.request_pause(None)
        client = TestClient(_make_app(state))

        response = client.post("/resume")

        assert response.status_code == 200
        assert response.json()["paused"] == ["msg_paused"]
        assert not pause.is_set()
        with state.status_lock:
            assert state.statuses["msg_paused"].status == "playing"

    def test_nothing_paused_resumes_nothing(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.post("/resume")

        assert response.status_code == 200
        assert response.json()["paused"] == []

    def test_named_message_is_resumed(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_paused", status="paused")
        _, pause = state.begin_playback("msg_paused")
        state.request_pause(None)
        client = TestClient(_make_app(state))

        response = client.post("/resume", json={"message_id": "msg_paused"})

        assert response.json()["paused"] == ["msg_paused"]
        assert not pause.is_set()

    def test_playing_message_is_not_resumed(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        response = client.post("/resume", json={"message_id": "msg_playing"})

        assert response.json()["paused"] == []
        with state.status_lock:
            assert state.statuses["msg_playing"].status == "playing"

    def test_unknown_message_returns_404(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.post("/resume", json={"message_id": "msg_nope"})

        assert response.status_code == 404


class TestReplayHistory:
    """A replay is the same utterance again, so it must not enter the history list."""

    def test_replay_message_is_excluded_from_recent(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello", "replay": True}).json()["message_id"]
        with state.status_lock:
            state.statuses[message_id].completed_at = time.time()

        assert client.get("/state").json()["recent"] == []

    def test_non_replay_message_still_appears_in_recent(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello"}).json()["message_id"]
        with state.status_lock:
            state.statuses[message_id].completed_at = time.time()

        recent = client.get("/state").json()["recent"]
        assert [m["message_id"] for m in recent] == [message_id]

    def test_replay_does_not_evict_originals_from_the_window(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        original = client.post("/say", json={"text": "Original"}).json()["message_id"]
        replay = client.post("/say", json={"text": "Original", "replay": True}).json()["message_id"]
        now = time.time()
        with state.status_lock:
            state.statuses[original].completed_at = now - 1
            state.statuses[replay].completed_at = now

        recent = client.get("/state").json()["recent"]
        assert [m["message_id"] for m in recent] == [original]

    def test_replay_defaults_to_false(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello"}).json()["message_id"]

        with state.status_lock:
            assert state.statuses[message_id].is_replay is False

    def test_replayed_message_is_still_reachable_by_status(self) -> None:
        # Excluded from the history list, but it really was spoken — its own
        # status endpoint must still report it.
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello", "replay": True}).json()["message_id"]

        assert client.get(f"/status/{message_id}").status_code == 200


class TestState:
    """Tests for GET /state."""

    def test_idle_state_has_no_current(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        response = client.get("/state")

        assert response.status_code == 200
        data = response.json()
        assert data["current"] is None
        assert data["recent"] == []
        assert data["queued"] == 0

    def test_playing_message_is_current(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")
        state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        data = client.get("/state").json()

        assert data["current"]["message_id"] == "msg_playing"
        assert data["current"]["status"] == "playing"

    def test_paused_message_is_current(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_paused", status="paused")
        state.begin_playback("msg_paused")
        state.request_pause(None)
        client = TestClient(_make_app(state))

        data = client.get("/state").json()

        assert data["current"]["message_id"] == "msg_paused"
        assert data["current"]["status"] == "paused"

    def test_recent_returns_finished_messages_newest_first(self) -> None:
        state = _make_state()
        now = time.time()
        for i, status in enumerate(["completed", "cancelled", "error"]):
            _queue_status(state, f"msg_{i}", status=status)
            with state.status_lock:
                # Recent timestamps (within the 1h TTL) ordered oldest→newest.
                state.statuses[f"msg_{i}"].completed_at = now - (2 - i)
        client = TestClient(_make_app(state))

        recent = client.get("/state").json()["recent"]

        assert [m["message_id"] for m in recent] == ["msg_2", "msg_1", "msg_0"]
        assert [m["status"] for m in recent] == ["error", "cancelled", "completed"]

    def test_current_takes_precedence_over_recent(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_done", status="completed")
        with state.status_lock:
            state.statuses["msg_done"].completed_at = 0.0
        _queue_status(state, "msg_playing", status="playing")
        state.begin_playback("msg_playing")
        client = TestClient(_make_app(state))

        data = client.get("/state").json()

        assert data["current"]["message_id"] == "msg_playing"
        assert data["recent"] == []


class TestSender:
    """Tests for the sender field on POST /say and its visibility in status/state."""

    def test_say_stores_sender(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello", "sender": "voice-anying"}).json()["message_id"]

        with state.status_lock:
            assert state.statuses[message_id].sender == "voice-anying"

    def test_status_exposes_sender(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello", "sender": "voice-anying"}).json()["message_id"]
        data = client.get(f"/status/{message_id}").json()

        assert data["sender"] == "voice-anying"

    def test_state_exposes_sender(self) -> None:
        state = _make_state()
        with state.status_lock:
            state.statuses["msg_done"] = MessageStatus(
                message_id="msg_done",
                status="completed",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=time.time(),
                sender="voice-anying",
            )
        client = TestClient(_make_app(state))

        data = client.get("/state").json()

        assert data["recent"][0]["sender"] == "voice-anying"

    def test_say_without_sender_stores_none(self) -> None:
        state = _make_state()
        client = TestClient(_make_app(state))

        message_id = client.post("/say", json={"text": "Hello"}).json()["message_id"]

        with state.status_lock:
            assert state.statuses[message_id].sender is None


class TestFailItem:
    """Tests for the worker's failure bookkeeping."""

    def test_does_not_overwrite_a_cancelled_message(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_cancelled", status="cancelled")

        # The worker's recovery handler reports anything that goes wrong after
        # the message settled — a cancelled message must stay cancelled.
        _fail_item(state, "msg_cancelled", "boom")

        with state.status_lock:
            assert state.statuses["msg_cancelled"].status == "cancelled"
            assert state.statuses["msg_cancelled"].error is None

    def test_still_fails_a_message_in_flight(self) -> None:
        state = _make_state()
        _queue_status(state, "msg_playing", status="playing")

        _fail_item(state, "msg_playing", "boom")

        with state.status_lock:
            assert state.statuses["msg_playing"].status == "error"
            assert state.statuses["msg_playing"].error == "boom"


class TestServerAudioWorker:
    """Tests for the server audio worker."""

    def test_processes_single_message(self) -> None:
        state = _make_state()
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]

        msg_id = "msg_test_001"
        with state.status_lock:
            state.statuses[msg_id] = MessageStatus(
                message_id=msg_id,
                status="queued",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        state.work_queue.put(WorkItem(message_id=msg_id, text="Hello", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses[msg_id].status == "completed"
            assert state.statuses[msg_id].audio_file is not None

    def test_streaming_mode_completes_message(self) -> None:
        state = _make_state(stream=True)
        c1 = MagicMock(audio=np.ones(100, dtype=np.float32))
        c2 = MagicMock(audio=np.ones(200, dtype=np.float32))
        model = _model_of(state)
        model.generate.return_value = [c1, c2]

        msg_id = "msg_stream_001"
        with state.status_lock:
            state.statuses[msg_id] = MessageStatus(
                message_id=msg_id,
                status="queued",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        state.work_queue.put(WorkItem(message_id=msg_id, text="Hello", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        model.generate.assert_called_once_with(
            text="Hello",
            voice="casual_female",
            stream=True,
            streaming_interval=state.streaming_interval,
        )
        with state.status_lock:
            assert state.statuses[msg_id].status == "completed"

    def test_processes_multiple_messages_sequentially(self) -> None:
        state = _make_state()
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]

        for i in range(3):
            msg_id = f"msg_test_{i:03d}"
            with state.status_lock:
                state.statuses[msg_id] = MessageStatus(
                    message_id=msg_id,
                    status="queued",
                    text=f"Message {i}",
                    audio_file=None,
                    error=None,
                    completed_at=None,
                )
            state.work_queue.put(WorkItem(message_id=msg_id, text=f"Message {i}", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=10)

        assert not t.is_alive()
        with state.status_lock:
            for i in range(3):
                msg_id = f"msg_test_{i:03d}"
                assert state.statuses[msg_id].status == "completed"

    def test_handles_generation_error(self) -> None:
        state = _make_state()
        model = _model_of(state)
        model.generate.side_effect = RuntimeError("Model crashed")

        msg_id = "msg_err_001"
        with state.status_lock:
            state.statuses[msg_id] = MessageStatus(
                message_id=msg_id,
                status="queued",
                text="Fail",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        state.work_queue.put(WorkItem(message_id=msg_id, text="Fail", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses[msg_id].status == "error"
            error = state.statuses[msg_id].error
            assert error is not None
            assert "Model crashed" in error

    def test_cancelled_queued_message_is_never_synthesized(self) -> None:
        state = _make_state()
        model = _model_of(state)
        model.generate.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]

        _queue_status(state, "msg_skip")
        _queue_status(state, "msg_keep")
        for msg_id in ("msg_skip", "msg_keep"):
            state.work_queue.put(WorkItem(message_id=msg_id, text=msg_id, voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)
        state.request_cancel("msg_skip", cancel_all=False)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses["msg_skip"].status == "cancelled"
            assert state.statuses["msg_keep"].status == "completed"
        # Only the surviving message reached the model.
        assert model.generate.call_count == 1

    def test_cancel_during_streaming_playback_reports_cancelled(self) -> None:
        state = _make_state(stream=True)
        model = _model_of(state)

        # No sleep or barrier is needed here, and adding one would only hide a
        # regression: the generator body runs on the worker thread, inside the
        # same next() call that play_stream is driving. So request_cancel has
        # already returned before play_stream sees the second chunk and checks
        # the event — the ordering is the generator protocol, not a race.
        def _generate(**_kwargs: Any) -> Any:
            yield MagicMock(audio=np.ones(100, dtype=np.float32))
            state.request_cancel(None, cancel_all=False)
            yield MagicMock(audio=np.ones(100, dtype=np.float32))

        model.generate.side_effect = _generate

        _queue_status(state, "msg_long")
        state.work_queue.put(WorkItem(message_id="msg_long", text="Long", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses["msg_long"].status == "cancelled"
            assert state.statuses["msg_long"].audio_file is None

    def test_next_message_plays_after_a_cancelled_one(self) -> None:
        state = _make_state(stream=True)
        model = _model_of(state)

        def _generate(*, text: str, **_kwargs: Any) -> Any:
            yield MagicMock(audio=np.ones(100, dtype=np.float32))
            if text == "first":
                state.request_cancel(None, cancel_all=False)
                yield MagicMock(audio=np.ones(100, dtype=np.float32))

        model.generate.side_effect = _generate

        for msg_id, text in (("msg_first", "first"), ("msg_second", "second")):
            _queue_status(state, msg_id)
            state.work_queue.put(WorkItem(message_id=msg_id, text=text, voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses["msg_first"].status == "cancelled"
            assert state.statuses["msg_second"].status == "completed"

    def test_shuts_down_on_none_sentinel(self) -> None:
        state = _make_state()
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        model = _model_of(state)
        model.generate.assert_not_called()

    def test_handles_playback_error(self) -> None:
        state = _make_state()
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]
        _ImmediateAudioPlayer.playback_error = RuntimeError("Audio device error")

        msg_id = "msg_play_err"
        with state.status_lock:
            state.statuses[msg_id] = MessageStatus(
                message_id=msg_id,
                status="queued",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        state.work_queue.put(WorkItem(message_id=msg_id, text="Hello", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses[msg_id].status == "error"
            error = state.statuses[msg_id].error
            assert error is not None
            assert "Audio device error" in error

    @patch("src.tts.player.sd")
    def test_recovers_after_output_stream_terminates(
        self,
        mock_sd: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("src.server.AudioPlayer", AudioPlayer)

        first_stream = MagicMock()
        second_stream = MagicMock()
        first_stream.write.side_effect = [None, RuntimeError("output stream terminated")]
        mock_sd.OutputStream.side_effect = [first_stream, second_stream]

        state = _make_state(save_wav=False, sample_rate=1000, lead_silence_ms=200)
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(4, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]

        first_msg = "msg_stream_lost"
        second_msg = "msg_stream_recovered"
        for message_id, text in [(first_msg, "first"), (second_msg, "second")]:
            with state.status_lock:
                state.statuses[message_id] = MessageStatus(
                    message_id=message_id,
                    status="queued",
                    text=text,
                    audio_file=None,
                    error=None,
                    completed_at=None,
                )
            state.work_queue.put(WorkItem(message_id=message_id, text=text, voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        assert mock_sd.OutputStream.call_count == 2

        first_silence = first_stream.write.call_args_list[0].args[0]
        second_silence = second_stream.write.call_args_list[0].args[0]
        assert first_silence.shape == (200, 1)
        assert second_silence.shape == (200, 1)
        assert float(np.max(np.abs(first_silence))) == 0.0
        assert float(np.max(np.abs(second_silence))) == 0.0

        with state.status_lock:
            assert state.statuses[first_msg].status == "error"
            assert state.statuses[first_msg].error is not None
            assert "output stream terminated" in state.statuses[first_msg].error
            assert state.statuses[second_msg].status == "completed"

    def test_loads_model_on_worker_thread_when_not_preloaded(self) -> None:
        """Regression: the model must be loaded on the same thread that calls
        generate, because MLX GPU streams are thread-local. Loading on the main
        thread and generating on the worker thread raised
        'no Stream(gpu, 0) in current thread'.
        """
        load_thread_id: dict[str, int] = {}
        generate_thread_id: dict[str, int] = {}
        load_calls: list[str] = []

        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)

        mock_model = MagicMock()

        def fake_generate(text: str, voice: str) -> list[Any]:
            generate_thread_id["id"] = threading.get_ident()
            return [mock_chunk]

        mock_model.generate.side_effect = fake_generate

        def fake_load(model_path: str) -> MagicMock:
            load_thread_id["id"] = threading.get_ident()
            load_calls.append(model_path)
            return mock_model

        registry = EngineRegistry(dict(DEFAULT_SPECS), loader=fake_load)
        state = _make_state(preload_model=False, registry=registry)

        msg_id = "msg_thread_001"
        with state.status_lock:
            state.statuses[msg_id] = MessageStatus(
                message_id=msg_id,
                status="queued",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        state.work_queue.put(WorkItem(message_id=msg_id, text="Hello", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        assert load_calls == ["test-model-path"]
        assert load_thread_id["id"] == t.ident
        assert generate_thread_id["id"] == t.ident
        assert load_thread_id["id"] == generate_thread_id["id"]
        with state.status_lock:
            assert state.statuses[msg_id].status == "completed"

    def test_reports_model_load_failure_via_ready_queue(self) -> None:
        """A model-load failure on the worker thread is propagated through
        ready_queue so the main thread (lifespan) can surface it on startup.
        """

        def failing_load(_model_path: str) -> MagicMock:
            msg = "model load boom"
            raise RuntimeError(msg)

        registry = EngineRegistry(dict(DEFAULT_SPECS), loader=failing_load)
        state = _make_state(preload_model=False, registry=registry)
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        result = state.ready_queue.get(timeout=1)
        assert isinstance(result, RuntimeError)
        assert "model load boom" in str(result)

    def test_signals_ready_when_model_preloaded(self) -> None:
        """When a model is pre-loaded, the worker still signals readiness with
        None so the lifespan startup unblocks.
        """
        state = _make_state()
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        assert state.ready_queue.get(timeout=1) is None


def _queue_item(state: ServerState, message_id: str, engine: str, voice: str) -> None:
    """Register a status and enqueue one work item for the worker."""
    with state.status_lock:
        state.statuses[message_id] = MessageStatus(
            message_id=message_id,
            status="queued",
            text="Hello",
            audio_file=None,
            error=None,
            completed_at=None,
            engine=engine,
        )
    state.work_queue.put(WorkItem(message_id=message_id, text="Hello", voice=voice, instruct=None, engine=engine))


def _run_worker(state: ServerState) -> threading.Thread:
    """Run the worker to completion after a sentinel has been queued."""
    state.work_queue.put(None)
    t = threading.Thread(target=server_audio_worker, args=(state,))
    t.start()
    t.join(timeout=5)
    return t


class TestLazyEngineLoading:
    """Tests for loading non-default engines on first use."""

    def test_startup_loads_only_the_default_engine(self) -> None:
        load_calls: list[str] = []

        def loader(model_path: str) -> MagicMock:
            load_calls.append(model_path)
            model = MagicMock()
            model.generate.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            return model

        state = _multi_engine_state(loader=loader, preload_default=False)
        _run_worker(state)

        assert load_calls == ["voxtral-path"]
        assert state.is_engine_loaded(VOXTRAL) is True
        assert state.is_engine_loaded(QWEN3) is False

    def test_first_item_for_another_engine_loads_it(self) -> None:
        load_calls: list[str] = []

        def loader(model_path: str) -> MagicMock:
            load_calls.append(model_path)
            model = MagicMock()
            model.generate_custom_voice.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            model.generate.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            return model

        state = _multi_engine_state(loader=loader, preload_default=False)
        _queue_item(state, "msg_lazy_001", QWEN3, "ryan")
        _run_worker(state)

        assert load_calls == ["voxtral-path", "qwen3-path"]
        assert state.is_engine_loaded(QWEN3) is True
        with state.status_lock:
            assert state.statuses["msg_lazy_001"].status == "completed"

    def test_second_item_for_the_same_engine_does_not_reload(self) -> None:
        load_calls: list[str] = []

        def loader(model_path: str) -> MagicMock:
            load_calls.append(model_path)
            model = MagicMock()
            model.generate_custom_voice.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            model.generate.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            return model

        state = _multi_engine_state(loader=loader, preload_default=False)
        _queue_item(state, "msg_lazy_001", QWEN3, "ryan")
        _queue_item(state, "msg_lazy_002", QWEN3, "serena")
        _run_worker(state)

        assert load_calls == ["voxtral-path", "qwen3-path"]

    def test_failed_lazy_load_keeps_the_worker_serving_the_default(self) -> None:
        """Regression: a bad non-default engine must not brick the worker.

        The startup load path signals ready_queue and kills the worker on
        failure; reusing it for lazy loads would let one unreachable engine
        take down an already-working default for every session.
        """

        def loader(model_path: str) -> MagicMock:
            if model_path == "qwen3-path":
                msg = "qwen3 model missing"
                raise RuntimeError(msg)
            model = MagicMock()
            model.generate.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            return model

        state = _multi_engine_state(loader=loader, preload_default=False)
        _queue_item(state, "msg_bad_001", QWEN3, "ryan")
        _queue_item(state, "msg_good_002", VOXTRAL, "casual_female")
        t = _run_worker(state)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses["msg_bad_001"].status == "error"
            assert "qwen3 model missing" in (state.statuses["msg_bad_001"].error or "")
            assert state.statuses["msg_good_002"].status == "completed"
        assert state.engine_error(QWEN3) is not None

    def test_failed_lazy_load_is_not_retried(self) -> None:
        load_calls: list[str] = []

        def loader(model_path: str) -> MagicMock:
            load_calls.append(model_path)
            if model_path == "qwen3-path":
                msg = "qwen3 model missing"
                raise RuntimeError(msg)
            return MagicMock()

        state = _multi_engine_state(loader=loader, preload_default=False)
        _queue_item(state, "msg_bad_001", QWEN3, "ryan")
        _queue_item(state, "msg_bad_002", QWEN3, "serena")
        _run_worker(state)

        assert load_calls == ["voxtral-path", "qwen3-path"]

    def test_status_is_loading_while_the_model_loads(self) -> None:
        release = threading.Event()
        observed: list[str] = []

        def loader(model_path: str) -> MagicMock:
            if model_path == "qwen3-path":
                release.wait(timeout=5)
            model = MagicMock()
            model.generate_custom_voice.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            model.generate.return_value = [MagicMock(audio=np.ones(100, dtype=np.float32))]
            return model

        state = _multi_engine_state(loader=loader, preload_default=False)
        _queue_item(state, "msg_slow_001", QWEN3, "ryan")
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()

        deadline = time.time() + 5
        while time.time() < deadline:
            with state.status_lock:
                current = state.statuses["msg_slow_001"].status
            if current == "loading":
                observed.append(current)
                break
            time.sleep(0.01)

        release.set()
        t.join(timeout=5)

        assert observed == ["loading"]
        with state.status_lock:
            assert state.statuses["msg_slow_001"].status == "completed"


class TestSaveWavDisabled:
    """Tests for save_wav=False behavior."""

    def test_completes_without_audio_file_when_save_wav_disabled(self) -> None:
        state = _make_state(save_wav=False)
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]

        msg_id = "msg_nosave_001"
        with state.status_lock:
            state.statuses[msg_id] = MessageStatus(
                message_id=msg_id,
                status="queued",
                text="Hello",
                audio_file=None,
                error=None,
                completed_at=None,
            )
        state.work_queue.put(WorkItem(message_id=msg_id, text="Hello", voice="casual_female", instruct=None, engine=VOXTRAL))
        state.work_queue.put(None)

        t = threading.Thread(target=server_audio_worker, args=(state,))
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        with state.status_lock:
            assert state.statuses[msg_id].status == "completed"
            assert state.statuses[msg_id].audio_file is None


class TestConcurrentSay:
    """Test concurrent /say requests are all accepted and processed sequentially."""

    def test_concurrent_requests_all_complete(self) -> None:
        state = _make_state()
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]

        app = _make_app(state)
        client = TestClient(app)

        num_messages = 5
        responses: list[dict[str, Any]] = [{}] * num_messages

        def send_say(index: int) -> None:
            resp = client.post("/say", json={"text": f"Message {index}"})
            responses[index] = resp.json()

        threads = [threading.Thread(target=send_say, args=(i,)) for i in range(num_messages)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All accepted with 'queued' status
        for i, r in enumerate(responses):
            assert "message_id" in r, f"Message {i} was not accepted"
            assert r["status"] == "queued"

        # Start worker to process the queue
        state.work_queue.put(None)
        worker = threading.Thread(target=server_audio_worker, args=(state,))
        worker.start()
        worker.join(timeout=15)

        assert not worker.is_alive()

        # All messages completed
        with state.status_lock:
            for r in responses:
                ms = state.statuses[r["message_id"]]
                assert ms.status == "completed"

        # Worker called generate once per message
        assert model.generate.call_count == num_messages

    def test_concurrent_requests_no_overlapping_playback(self) -> None:
        state = _make_state()
        mock_chunk = MagicMock()
        mock_chunk.audio = np.ones(100, dtype=np.float32)
        model = _model_of(state)
        model.generate.return_value = [mock_chunk]

        app = _make_app(state)
        client = TestClient(app)

        num_messages = 5
        for i in range(num_messages):
            client.post("/say", json={"text": f"Overlap test {i}"})

        state.work_queue.put(None)
        worker = threading.Thread(target=server_audio_worker, args=(state,))
        worker.start()
        worker.join(timeout=15)

        assert not worker.is_alive()
        assert _ImmediateAudioPlayer.max_active_count == 1


class TestTextPipelineIntegration:
    """Integration test: clean_text + simplify_punctuation compose correctly."""

    def test_cleaning_then_simplification(self):
        state = _make_state(simplify_punctuation=True)
        app = _make_app(state)
        client = TestClient(app)

        client.post("/say", json={"text": "  Hello,   world!  How  are  you?  "})

        item = state.work_queue.get_nowait()
        assert item is not None
        assert item.text == "Hello world. How are you."


class TestReplaySupport:
    """The two things the Replay button depends on, each locked by its own test.

    Both shipped in the same change and neither had coverage: the reviewer of
    bborbe/tts-mcp#24 flagged that a constant and a model field were introduced
    with no regression guard, which is exactly how a silent behaviour change
    reaches users.
    """

    def test_state_returns_at_most_the_history_limit(self) -> None:
        """More finished messages than the limit → exactly the limit is returned."""
        state = _make_state()
        client = TestClient(_make_app(state))

        for i in range(RECENT_HISTORY_LIMIT + 7):
            mid = client.post("/say", json={"text": f"message {i}"}).json()["message_id"]
            with state.status_lock:
                ms = state.statuses[mid]
                ms.status = "completed"
                ms.completed_at = time.time() + i  # distinct, ascending

        recent = client.get("/state").json()["recent"]
        assert len(recent) == RECENT_HISTORY_LIMIT

    def test_history_limit_is_larger_than_the_previous_ten(self) -> None:
        """Regression lock on the raise from 10 — a silent revert would break this."""
        assert RECENT_HISTORY_LIMIT >= 25

    def test_state_exposes_the_resolved_voice_for_replay(self) -> None:
        """say() → MessageStatus.voice → StatusResponse.voice, the full replay chain.

        Replay re-POSTs the recorded voice so the message sounds like the original.
        If any link drops it the replay still succeeds — in the default voice —
        which is the kind of wrongness no other assertion would catch.
        """
        state = _make_state()
        client = TestClient(_make_app(state))
        voice = "casual_female"  # _make_state's default_voice, a known-valid one

        mid = client.post("/say", json={"text": "Replay me", "voice": voice}).json()["message_id"]
        with state.status_lock:
            assert state.statuses[mid].voice == voice
            state.statuses[mid].status = "completed"
            state.statuses[mid].completed_at = time.time()

        recent = client.get("/state").json()["recent"]
        entry = next(m for m in recent if m["message_id"] == mid)
        assert entry["voice"] == voice

    def test_state_voice_survives_the_default_voice_fallback(self) -> None:
        """A /say with no voice still records the RESOLVED voice, not None.

        Otherwise replaying such a message re-resolves the default at replay time,
        which can differ from what was actually heard.
        """
        state = _make_state()
        client = TestClient(_make_app(state))

        mid = client.post("/say", json={"text": "No voice given"}).json()["message_id"]
        with state.status_lock:
            assert state.statuses[mid].voice is not None
