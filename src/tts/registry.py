"""Lazy per-engine model cache.

The server declares one or more engines in config.yaml but only loads a model
when a request actually asks for that engine. If every caller uses the default
engine, exactly one model is ever resident — which matters because each model
costs several GB of unified memory.

Thread affinity is the reason this is a plain cache rather than anything
cleverer: MLX GPU streams are thread-local, so a model must be loaded on, and
generated from, the same thread. The registry is therefore owned by the single
audio worker thread and populated inline on it. See the module docstring of
src.tts.worker and the comments in the server's worker loop.

A failed load is cached as a failure and re-raised on every later call for that
engine. Retrying a load that takes ~20 seconds to fail on every request is worse
than surfacing the original error immediately; recovery is a server restart.
"""

import dataclasses
import logging
from collections.abc import Callable

from mlx_audio.tts.utils import load

from src.tts.engine import TTSEngine, build_engine
from src.tts.protocols import TTSModel

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class EngineSpec:
    """How to build and load one engine, as declared in config.yaml.

    Attributes:
        kind: Engine identifier — one of ENGINE_KINDS.
        model_path: Model directory or id passed to the loader.
        language: Language for the qwen3 engine; must be None for voxtral.
    """

    kind: str
    model_path: str
    language: str | None


@dataclasses.dataclass(frozen=True)
class LoadedEngine:
    """An engine paired with its loaded model.

    Attributes:
        engine: Engine that knows how to drive this model family.
        model: The loaded model, usable only on the thread that loaded it.
    """

    engine: TTSEngine
    model: TTSModel


class EngineRegistry:
    """Lazily builds and caches one (engine, model) pair per engine kind.

    MUST be used from a single thread: MLX GPU streams are thread-local, so
    every model this loads is only usable on the thread that called get().
    """

    def __init__(
        self,
        specs: dict[str, EngineSpec],
        loader: Callable[[str], TTSModel] = load,
    ) -> None:
        """Store the declared engines without building or loading anything.

        Args:
            specs: Declared engines keyed by engine kind.
            loader: Model loader, injectable so tests can avoid MLX.
        """
        self._specs = dict(specs)
        self._loader = loader
        self._loaded: dict[str, LoadedEngine] = {}
        self._failures: dict[str, Exception] = {}

    def get(self, kind: str) -> LoadedEngine:
        """Return the loaded engine for kind, loading it on first use.

        Args:
            kind: Engine identifier to resolve.

        Returns:
            The cached or freshly loaded engine and model.

        Raises:
            ValueError: If kind was not declared in the config.
            Exception: The original load failure, re-raised from cache on every
                later call for the same kind.
        """
        cached = self._loaded.get(kind)
        if cached is not None:
            return cached

        failure = self._failures.get(kind)
        if failure is not None:
            raise failure

        spec = self._specs.get(kind)
        if spec is None:
            declared = ", ".join(sorted(self._specs)) or "none"
            msg = f"Unknown engine '{kind}'. Declared: {declared}"
            raise ValueError(msg)

        try:
            engine = build_engine(spec.kind, spec.language)
            logger.info("Loading model for engine '%s': %s", kind, spec.model_path)
            model = self._loader(spec.model_path)
            engine.validate_model(model, spec.model_path)
        except Exception as exc:
            logger.error("Model load failed for engine '%s': %s", kind, exc)
            self._failures[kind] = exc
            raise

        loaded = LoadedEngine(engine=engine, model=model)
        self._loaded[kind] = loaded
        return loaded

    def preload(self, kind: str, loaded: LoadedEngine) -> None:
        """Seed the cache with an already-loaded engine.

        Used by tests and by any caller that loaded a model itself on this
        thread and wants the registry to serve it from then on.

        Args:
            kind: Engine identifier the pair belongs to.
            loaded: The engine and model to cache.
        """
        self._loaded[kind] = loaded

    def is_loaded(self, kind: str) -> bool:
        """Report whether this engine's model is resident.

        Args:
            kind: Engine identifier to check.

        Returns:
            True if the model has been loaded and cached.
        """
        return kind in self._loaded

    def failure(self, kind: str) -> Exception | None:
        """Return the cached load failure for this engine, if any.

        Args:
            kind: Engine identifier to check.

        Returns:
            The exception a previous load raised, or None.
        """
        return self._failures.get(kind)

    def kinds(self) -> list[str]:
        """List the declared engine kinds.

        Returns:
            Sorted engine identifiers from the config.
        """
        return sorted(self._specs)

    def spec(self, kind: str) -> EngineSpec | None:
        """Return the declared spec for this engine, if any.

        Args:
            kind: Engine identifier to look up.

        Returns:
            The spec, or None when the engine was not declared.
        """
        return self._specs.get(kind)
