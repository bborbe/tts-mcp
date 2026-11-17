"""Idle-unload policy for resident TTS models.

A loaded model costs several GB of unified memory, and use is bursty: the
server can sit untouched for hours between utterances. This module decides
when the audio worker has been idle long enough to drop the models it holds.

The policy is a clock, not a thread. MLX GPU streams are thread-local, so the
models are only usable — and only releasable — on the audio worker thread that
loaded them (see src.tts.registry). The worker therefore owns the decision: it
asks whether the idle window has elapsed and performs the unload inline,
between work items, where no synthesis is in flight.
"""

import gc
import logging
import time
from collections.abc import Callable

import mlx.core as mx

from src.tts.registry import EngineRegistry

logger = logging.getLogger(__name__)

DEFAULT_IDLE_UNLOAD_SECONDS = 600.0
"""Idle seconds before resident models are dropped (10 minutes).

Long enough that a burst of utterances never pays a reload, short enough that
memory returns to the host between sessions.
"""


def idle_unload_seconds_from_config(config: dict[str, object]) -> float:
    """Read the optional ``idle_unload_seconds`` key from config.yaml.

    The key is optional so a config written before this feature keeps working:
    an absent key means DEFAULT_IDLE_UNLOAD_SECONDS. Zero disables the idle
    unload, which restores the old load-once behaviour. A key that is present
    must be a non-negative number — a value that cannot be honoured is a
    mistake, not a request for the default.

    Args:
        config: Raw config.yaml mapping.

    Returns:
        The idle window in seconds, or 0.0 when the feature is disabled.

    Raises:
        ValueError: If the key is present but is not a non-negative number.
    """
    raw = config.get("idle_unload_seconds")
    if raw is None:
        return DEFAULT_IDLE_UNLOAD_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        msg = f"'idle_unload_seconds' in config.yaml must be a number of seconds (got {raw!r})"
        raise ValueError(msg)
    seconds = float(raw)
    if seconds < 0:
        msg = f"'idle_unload_seconds' in config.yaml must not be negative (got {raw!r})"
        raise ValueError(msg)
    return seconds


class IdleUnloader:
    """Tracks worker activity and drops resident models once the idle window elapses.

    MUST be used from the audio worker thread only: it releases the registry's
    cached models, which MLX pins to the thread that loaded them.
    """

    def __init__(
        self,
        idle_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        clear_cache: Callable[[], None] = mx.clear_cache,
    ) -> None:
        """Start the idle clock.

        Args:
            idle_seconds: Idle window before unloading. Zero or negative
                disables unloading entirely.
            clock: Monotonic time source, injectable so tests can advance time
                without sleeping.
            clear_cache: MLX buffer-cache release, injectable so unit tests do
                not need a Metal context.
        """
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._clear_cache = clear_cache
        self._last_activity = clock()

    @property
    def idle_seconds(self) -> float:
        """The configured idle window in seconds."""
        return self._idle_seconds

    @property
    def enabled(self) -> bool:
        """Whether idle unloading is armed at all."""
        return self._idle_seconds > 0

    def touch(self) -> None:
        """Record activity now, restarting the idle window."""
        self._last_activity = self._clock()

    def seconds_until_unload(self) -> float | None:
        """Seconds left before the models should be dropped.

        Returns:
            The remaining window, 0.0 once it has elapsed, or None when
            unloading is disabled (the caller then waits without a timeout).
        """
        if not self.enabled:
            return None
        elapsed = self._clock() - self._last_activity
        return max(0.0, self._idle_seconds - elapsed)

    def unload(self, registry: EngineRegistry) -> list[str]:
        """Drop every resident model and release MLX's cached buffers.

        Args:
            registry: The worker's own engine registry.

        Returns:
            The engine kinds that were resident and have now been dropped.
        """
        dropped = registry.unload_all()
        if dropped:
            # MLX modules hold reference cycles, so dropping the registry's
            # references alone does not free them; collect before releasing the
            # buffer cache, or the memory only comes back on a later GC.
            gc.collect()
            self._clear_cache()
            logger.info("Unloaded idle model(s) for engine(s): %s", ", ".join(dropped))
        self.touch()
        return dropped
