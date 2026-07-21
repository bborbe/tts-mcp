"""Typing protocols for TTS models, generation results, and audio output streams."""

from collections.abc import Iterator
from typing import Protocol

import numpy as np


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
