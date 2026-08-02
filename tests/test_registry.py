"""Tests for the lazy per-engine model cache."""

from unittest.mock import MagicMock

import pytest

from src.tts import (
    QWEN3,
    VOXTRAL,
    EngineRegistry,
    EngineSpec,
    LoadedEngine,
    Qwen3Engine,
    VoxtralEngine,
)


def _specs() -> dict[str, EngineSpec]:
    return {
        VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="/models/voxtral", language=None),
        QWEN3: EngineSpec(kind=QWEN3, model_path="/models/qwen3", language="English"),
    }


class _CountingLoader:
    """Loader that records every model path it was asked for."""

    def __init__(self, model: object | None = None) -> None:
        self.calls: list[str] = []
        self._model = model if model is not None else MagicMock()

    def __call__(self, model_path: str) -> object:
        self.calls.append(model_path)
        return self._model


class TestLazyLoading:
    """Nothing is built or loaded until get() asks for it."""

    def test_nothing_loaded_before_first_get(self) -> None:
        loader = _CountingLoader()
        registry = EngineRegistry(_specs(), loader=loader)

        assert loader.calls == []
        assert registry.is_loaded(VOXTRAL) is False
        assert registry.is_loaded(QWEN3) is False

    def test_get_loads_only_the_requested_engine(self) -> None:
        loader = _CountingLoader()
        registry = EngineRegistry(_specs(), loader=loader)

        registry.get(VOXTRAL)

        assert loader.calls == ["/models/voxtral"]
        assert registry.is_loaded(VOXTRAL) is True
        assert registry.is_loaded(QWEN3) is False

    def test_repeated_get_loads_once(self) -> None:
        loader = _CountingLoader()
        registry = EngineRegistry(_specs(), loader=loader)

        first = registry.get(VOXTRAL)
        second = registry.get(VOXTRAL)

        assert first is second
        assert loader.calls == ["/models/voxtral"]

    def test_second_engine_does_not_evict_the_first(self) -> None:
        loader = _CountingLoader()
        registry = EngineRegistry(_specs(), loader=loader)

        voxtral = registry.get(VOXTRAL)
        registry.get(QWEN3)

        assert registry.is_loaded(VOXTRAL) is True
        assert registry.is_loaded(QWEN3) is True
        assert registry.get(VOXTRAL) is voxtral
        assert loader.calls == ["/models/voxtral", "/models/qwen3"]

    def test_builds_the_engine_matching_the_spec(self) -> None:
        registry = EngineRegistry(_specs(), loader=_CountingLoader())

        assert isinstance(registry.get(VOXTRAL).engine, VoxtralEngine)
        assert isinstance(registry.get(QWEN3).engine, Qwen3Engine)

    def test_validates_the_model_against_its_engine(self) -> None:
        model = MagicMock(spec=["generate"])
        registry = EngineRegistry(_specs(), loader=_CountingLoader(model))

        # qwen3 needs generate_custom_voice, which this model lacks.
        with pytest.raises(RuntimeError):
            registry.get(QWEN3)


class TestFailureCaching:
    """A failed load is remembered rather than retried."""

    def test_load_failure_propagates(self) -> None:
        def loader(_model_path: str) -> object:
            msg = "model directory missing"
            raise FileNotFoundError(msg)

        registry = EngineRegistry(_specs(), loader=loader)

        with pytest.raises(FileNotFoundError, match="model directory missing"):
            registry.get(VOXTRAL)

    def test_failure_is_reraised_without_reloading(self) -> None:
        calls: list[str] = []

        def loader(model_path: str) -> object:
            calls.append(model_path)
            msg = "boom"
            raise RuntimeError(msg)

        registry = EngineRegistry(_specs(), loader=loader)

        with pytest.raises(RuntimeError):
            registry.get(VOXTRAL)
        with pytest.raises(RuntimeError):
            registry.get(VOXTRAL)

        assert calls == ["/models/voxtral"]

    def test_failure_is_reported(self) -> None:
        def loader(_model_path: str) -> object:
            msg = "boom"
            raise RuntimeError(msg)

        registry = EngineRegistry(_specs(), loader=loader)

        assert registry.failure(VOXTRAL) is None
        with pytest.raises(RuntimeError):
            registry.get(VOXTRAL)
        assert isinstance(registry.failure(VOXTRAL), RuntimeError)

    def test_one_engine_failing_leaves_the_other_usable(self) -> None:
        def loader(model_path: str) -> object:
            if model_path == "/models/qwen3":
                msg = "boom"
                raise RuntimeError(msg)
            return MagicMock()

        registry = EngineRegistry(_specs(), loader=loader)

        with pytest.raises(RuntimeError):
            registry.get(QWEN3)

        assert registry.get(VOXTRAL) is not None
        assert registry.is_loaded(VOXTRAL) is True


class TestDeclaredEngines:
    """Lookups are limited to what the config declared."""

    def test_unknown_engine_raises(self) -> None:
        registry = EngineRegistry(_specs(), loader=_CountingLoader())

        with pytest.raises(ValueError, match="Unknown engine 'piper'"):
            registry.get("piper")

    def test_unknown_engine_lists_declared_engines(self) -> None:
        registry = EngineRegistry(_specs(), loader=_CountingLoader())

        with pytest.raises(ValueError, match="qwen3, voxtral"):
            registry.get("piper")

    def test_kinds_lists_declared_engines(self) -> None:
        registry = EngineRegistry(_specs(), loader=_CountingLoader())

        assert registry.kinds() == [QWEN3, VOXTRAL]

    def test_spec_returns_declaration(self) -> None:
        registry = EngineRegistry(_specs(), loader=_CountingLoader())

        spec = registry.spec(QWEN3)
        assert spec is not None
        assert spec.model_path == "/models/qwen3"
        assert spec.language == "English"
        assert registry.spec("piper") is None

    def test_bad_language_surfaces_from_build_engine(self) -> None:
        specs = {VOXTRAL: EngineSpec(kind=VOXTRAL, model_path="/m", language="English")}
        registry = EngineRegistry(specs, loader=_CountingLoader())

        with pytest.raises(ValueError, match="not supported by the voxtral engine"):
            registry.get(VOXTRAL)


class TestPreload:
    """A model loaded elsewhere on this thread can seed the cache."""

    def test_preloaded_engine_is_served_without_loading(self) -> None:
        loader = _CountingLoader()
        registry = EngineRegistry(_specs(), loader=loader)
        loaded = LoadedEngine(engine=VoxtralEngine(), model=MagicMock())

        registry.preload(VOXTRAL, loaded)

        assert registry.is_loaded(VOXTRAL) is True
        assert registry.get(VOXTRAL) is loaded
        assert loader.calls == []
