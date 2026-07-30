"""Speech generation: full-buffer and streaming chunk generation from a TTS model.

Generation is dispatched through a TTSEngine (see src/tts/engine.py), which knows
how the configured model family is driven — Voxtral takes a voice name, Qwen3-TTS
takes a speaker plus a language and optional style instruction.
"""

from collections.abc import Iterator

import numpy as np

from src.tts.engine import TTSEngine
from src.tts.protocols import TTSModel


def iter_stream_chunks(
    engine: TTSEngine,
    model: TTSModel,
    text: str,
    voice: str,
    instruct: str | None,
    streaming_interval: float,
) -> Iterator[np.ndarray]:
    """Yield audio chunks incrementally as the model streams them.

    Unlike generate_chunks (which drains the whole utterance into a list before
    returning), this drives the model in streaming mode and yields each chunk as
    soon as it is decoded, so playback can start after the first chunk instead of
    after the entire utterance. Zero-length chunks (the streaming final marker
    when all frames were already emitted) are skipped.

    Args:
        engine: Engine that knows how to drive this model family.
        model: Loaded TTS model.
        text: The text to convert to speech.
        voice: The voice to use for synthesis.
        instruct: Optional emotion/style instruction (qwen3 engine only).
        streaming_interval: Approximate seconds of audio per streamed chunk.

    Yields:
        Audio chunks as float32 numpy arrays.
    """
    results = engine.generate(
        model,
        text,
        voice,
        instruct,
        stream=True,
        streaming_interval=streaming_interval,
    )
    for result in results:
        chunk = np.array(result.audio, dtype=np.float32)
        if chunk.size > 0:
            yield chunk


def generate_chunks(
    engine: TTSEngine,
    model: TTSModel,
    text: str,
    voice: str,
    instruct: str | None,
    streaming_interval: float,
) -> list[np.ndarray]:
    """Generate audio chunks from text without playing.

    Args:
        engine: Engine that knows how to drive this model family.
        model: Loaded TTS model.
        text: The text to convert to speech.
        voice: The voice to use for synthesis.
        instruct: Optional emotion/style instruction (qwen3 engine only).
        streaming_interval: Approximate seconds of audio per streamed chunk;
            unused in buffered mode but part of the engine call signature.

    Returns:
        List of audio chunks as numpy arrays.
    """
    results = engine.generate(
        model,
        text,
        voice,
        instruct,
        stream=False,
        streaming_interval=streaming_interval,
    )
    return [np.array(result.audio, dtype=np.float32) for result in results]
