"""Tests for the idle-unload policy and its wiring into the audio worker."""

import gc
import queue
import weakref
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from src.server import WorkItem, _next_item
from src.tts import (
    DEFAULT_IDLE_UNLOAD_SECONDS,
    VOXTRAL,
    EngineRegistry,
    EngineSpec,
    IdleUnloader,
    LoadedEngine,
    VoxtralEngine,
    idle_unload_seconds_from_config,
)
from tests.test_server import _make_state


class _Clock:
    """Manually advanced monotonic clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CacheRecorder:
    """Records how often MLX's cache release was requested."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class _Model:
    """Weakref-able stand-in for a loaded model."""


def _registry() -> EngineRegistry:
    """A registry with one engine already cached, as after a first utterance."""
    specs = {VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="/models/voxtral", language=None, temperature=None, seed=None)}
    registry = EngineRegistry(specs, loader=lambda _path: object())
    registry.preload(VOXTRAL, LoadedEngine(engine=VoxtralEngine(), model=object()))
    return registry


def _item() -> WorkItem:
    return WorkItem(message_id="m1", text="hello", voice="casual_female", instruct=None, engine=VOXTRAL)


class TestConfigKey:
    """``idle_unload_seconds`` is optional, and a bad value fails loudly."""

    def test_absent_key_uses_the_default(self) -> None:
        assert idle_unload_seconds_from_config({}) == DEFAULT_IDLE_UNLOAD_SECONDS

    def test_default_is_ten_minutes(self) -> None:
        assert DEFAULT_IDLE_UNLOAD_SECONDS == 600.0

    def test_zero_disables(self) -> None:
        assert idle_unload_seconds_from_config({"idle_unload_seconds": 0}) == 0.0

    def test_explicit_value_is_used(self) -> None:
        assert idle_unload_seconds_from_config({"idle_unload_seconds": 90}) == 90.0

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            idle_unload_seconds_from_config({"idle_unload_seconds": -1})

    def test_non_number_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            idle_unload_seconds_from_config({"idle_unload_seconds": "600"})

    def test_bool_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a number"):
            idle_unload_seconds_from_config({"idle_unload_seconds": True})


class TestIdleWindow:
    """The window is measured from the last recorded activity."""

    def test_disabled_never_reports_a_deadline(self) -> None:
        unloader = IdleUnloader(0, clock=_Clock(), clear_cache=_CacheRecorder())

        assert unloader.enabled is False
        assert unloader.seconds_until_unload() is None

    def test_window_counts_down(self) -> None:
        clock = _Clock()
        unloader = IdleUnloader(60, clock=clock, clear_cache=_CacheRecorder())

        clock.advance(20)
        assert unloader.seconds_until_unload() == 40.0

    def test_window_expires(self) -> None:
        clock = _Clock()
        unloader = IdleUnloader(60, clock=clock, clear_cache=_CacheRecorder())

        clock.advance(90)
        assert unloader.seconds_until_unload() == 0.0

    def test_touch_restarts_the_window(self) -> None:
        clock = _Clock()
        unloader = IdleUnloader(60, clock=clock, clear_cache=_CacheRecorder())

        clock.advance(50)
        unloader.touch()
        clock.advance(50)

        assert unloader.seconds_until_unload() == 10.0


class TestUnload:
    """Unloading drops resident models and releases MLX's cache."""

    def test_drops_the_resident_model(self) -> None:
        registry = _registry()
        unloader = IdleUnloader(60, clock=_Clock(), clear_cache=_CacheRecorder())

        dropped = unloader.unload(registry)

        assert dropped == [VOXTRAL]
        assert registry.is_loaded(VOXTRAL) is False

    def test_releases_the_mlx_cache(self) -> None:
        recorder = _CacheRecorder()
        unloader = IdleUnloader(60, clock=_Clock(), clear_cache=recorder)

        unloader.unload(_registry())

        assert recorder.calls == 1

    def test_nothing_resident_is_a_no_op(self) -> None:
        recorder = _CacheRecorder()
        registry = EngineRegistry({}, loader=lambda _path: object())
        unloader = IdleUnloader(60, clock=_Clock(), clear_cache=recorder)

        assert unloader.unload(registry) == []
        assert recorder.calls == 0

    def test_unload_restarts_the_window(self) -> None:
        clock = _Clock()
        unloader = IdleUnloader(60, clock=clock, clear_cache=_CacheRecorder())
        clock.advance(60)
        assert unloader.seconds_until_unload() == 0.0

        unloader.unload(_registry())

        assert unloader.seconds_until_unload() == 60.0

    def test_reload_after_unload_loads_again(self) -> None:
        calls: list[str] = []

        def loader(model_path: str) -> object:
            calls.append(model_path)
            return MagicMock()

        specs = {VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="/models/voxtral", language=None, temperature=None, seed=None)}
        registry = EngineRegistry(specs, loader=loader)
        unloader = IdleUnloader(60, clock=_Clock(), clear_cache=_CacheRecorder())

        registry.get(VOXTRAL)
        unloader.unload(registry)
        registry.get(VOXTRAL)

        assert calls == ["/models/voxtral", "/models/voxtral"]

    def test_cached_failure_survives_unload(self) -> None:
        def loader(_model_path: str) -> object:
            msg = "boom"
            raise RuntimeError(msg)

        specs = {VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="/models/voxtral", language=None, temperature=None, seed=None)}
        registry = EngineRegistry(specs, loader=loader)
        with pytest.raises(RuntimeError):
            registry.get(VOXTRAL)

        IdleUnloader(60, clock=_Clock(), clear_cache=_CacheRecorder()).unload(registry)

        assert isinstance(registry.failure(VOXTRAL), RuntimeError)

    def test_unload_actually_releases_the_model(self) -> None:
        model = _Model()
        ref = weakref.ref(model)
        specs = {VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="/models/voxtral", language=None, temperature=None, seed=None)}
        registry = EngineRegistry(specs, loader=lambda _path: None)
        registry.preload(VOXTRAL, LoadedEngine(engine=VoxtralEngine(), model=cast(Any, model)))
        unloader = IdleUnloader(60, clock=_Clock(), clear_cache=_CacheRecorder())

        unloader.unload(registry)
        del model
        gc.collect()

        # The registry was the last holder: dropping it must free the model,
        # otherwise the memory the feature exists to release never comes back.
        assert ref() is None


class _ScriptedQueue:
    """Queue stand-in: raise Empty a set number of times, then return shutdown."""

    def __init__(self, empties: int) -> None:
        self._remaining = empties
        self.timeouts: list[float | None] = []

    def get(self, timeout: float | None = None) -> None:
        self.timeouts.append(timeout)
        if self._remaining > 0:
            self._remaining -= 1
            raise queue.Empty
        return None


class TestWorkerWiring:
    """The worker waits through _next_item, which owns the unload decision."""

    def test_returns_a_queued_item_without_unloading(self) -> None:
        state = _make_state(idle_unload_seconds=600)
        state.work_queue.put(_item())
        unloader = IdleUnloader(600, clock=_Clock(), clear_cache=_CacheRecorder())

        item = _next_item(state, unloader)

        assert item is not None
        assert item.message_id == "m1"
        assert state.registry.is_loaded(VOXTRAL) is True

    def test_returns_none_on_shutdown(self) -> None:
        state = _make_state(idle_unload_seconds=600)
        state.work_queue.put(None)
        unloader = IdleUnloader(600, clock=_Clock(), clear_cache=_CacheRecorder())

        assert _next_item(state, unloader) is None

    def test_activity_restarts_the_window(self) -> None:
        clock = _Clock()
        state = _make_state(idle_unload_seconds=60)
        state.work_queue.put(_item())
        unloader = IdleUnloader(60, clock=clock, clear_cache=_CacheRecorder())
        clock.advance(50)

        _next_item(state, unloader)

        assert unloader.seconds_until_unload() == 60.0

    def test_idle_queue_unloads_and_reports_the_engine(self) -> None:
        state = _make_state(idle_unload_seconds=0.01)
        recorder = _CacheRecorder()
        unloader = IdleUnloader(0.01, clock=_Clock(), clear_cache=recorder)
        state.work_queue = cast(Any, _ScriptedQueue(3))

        assert _next_item(state, unloader) is None

        assert recorder.calls == 1
        assert state.registry.is_loaded(VOXTRAL) is False
        assert state.is_engine_loaded(VOXTRAL) is False

    def test_no_unload_while_playback_is_active(self) -> None:
        state = _make_state(idle_unload_seconds=0.01)
        recorder = _CacheRecorder()
        unloader = IdleUnloader(0.01, clock=_Clock(), clear_cache=recorder)
        # A message holding a cancel/pause event means audio is still in flight.
        state.begin_playback("playing-1")
        state.work_queue = cast(Any, _ScriptedQueue(3))

        assert _next_item(state, unloader) is None

        assert recorder.calls == 0
        assert state.registry.is_loaded(VOXTRAL) is True

    def test_disabled_idle_never_unloads(self) -> None:
        state = _make_state(idle_unload_seconds=0)
        recorder = _CacheRecorder()
        unloader = IdleUnloader(0, clock=_Clock(), clear_cache=recorder)
        scripted = _ScriptedQueue(0)
        state.work_queue = cast(Any, scripted)

        assert _next_item(state, unloader) is None

        # The disabled path must block like the old code did: no timeout at all.
        assert scripted.timeouts == [None]
        assert recorder.calls == 0
        assert state.registry.is_loaded(VOXTRAL) is True


class TestStateReporting:
    """Server state reflects an unloaded engine so /voices tells the truth."""

    def test_mark_unloaded_clears_the_loaded_flag(self) -> None:
        state = _make_state(idle_unload_seconds=600)
        assert state.is_engine_loaded(VOXTRAL) is True

        state.mark_engine_unloaded(VOXTRAL)

        assert state.is_engine_loaded(VOXTRAL) is False

    def test_has_active_playback_tracks_begin_and_end(self) -> None:
        state = _make_state(idle_unload_seconds=600)
        assert state.has_active_playback() is False

        state.begin_playback("m1")
        assert state.has_active_playback() is True

        state.end_playback("m1")
        assert state.has_active_playback() is False

    def test_idle_window_is_exposed(self) -> None:
        state = _make_state(idle_unload_seconds=42)

        assert state.idle_unload_seconds == 42
