"""Pluggable TTS engines: per-model voice discovery and generation dispatch.

Two model families are supported and selected by the required ``engine:`` key in
config.yaml:

``voxtral``
    Mistral Voxtral 4B TTS. Voices are per-voice embedding files shipped in the
    model directory; generation takes the voice name directly.

``qwen3``
    Qwen3-TTS CustomVoice. Voices ("speakers") are declared in the model's
    ``config.json`` under ``talker_config.spk_id``; generation goes through
    ``generate_custom_voice`` and additionally requires a language, with optional
    free-text emotion/style instructions.

Both families stream, so the worker and playback layers are engine-agnostic.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast

from src.tts.protocols import CustomVoiceModel, GenerationResult, TTSModel

VOXTRAL = "voxtral"
QWEN3 = "qwen3"
ENGINE_KINDS = (VOXTRAL, QWEN3)


class TTSEngine(Protocol):
    """Model-family-specific voice discovery and generation dispatch."""

    @property
    def kind(self) -> str:
        """Engine identifier as written in config.yaml."""
        ...

    def discover_voices(self, model_dir: Path) -> list[str]:
        """List the voices this model offers, read from the model directory."""
        ...

    def validate_model(self, model: TTSModel, model_id: str) -> None:
        """Raise RuntimeError if the loaded model cannot be driven by this engine."""
        ...

    def generate(
        self,
        model: TTSModel,
        text: str,
        voice: str,
        instruct: str | None,
        stream: bool,
        streaming_interval: float,
    ) -> Iterator[GenerationResult]:
        """Generate speech, yielding one result per audio chunk."""
        ...


class VoxtralEngine:
    """Voxtral 4B TTS: voice embeddings on disk, plain ``generate``."""

    @property
    def kind(self) -> str:
        """Engine identifier as written in config.yaml."""
        return VOXTRAL

    def discover_voices(self, model_dir: Path) -> list[str]:
        """List voices from the model's ``voice_embedding`` directory.

        Args:
            model_dir: Path to the model directory.

        Returns:
            Sorted list of available voice names.

        Raises:
            FileNotFoundError: If the directory is missing or holds no voices.
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

    def validate_model(self, model: TTSModel, model_id: str) -> None:
        """Check the loaded model exposes Voxtral-style generation.

        Args:
            model: Loaded model.
            model_id: Model path or identifier, for the error message.

        Raises:
            RuntimeError: If the model has no usable generate method.
        """
        if not hasattr(model, "generate") or model.generate is None:
            msg = f"Model {model_id} does not support generation (engine: {VOXTRAL})"
            raise RuntimeError(msg)

    def generate(
        self,
        model: TTSModel,
        text: str,
        voice: str,
        instruct: str | None,
        stream: bool,
        streaming_interval: float,
    ) -> Iterator[GenerationResult]:
        """Generate speech with the named voice.

        Args:
            model: Loaded Voxtral model.
            text: Text to synthesize.
            voice: Voice name.
            instruct: Must be None — Voxtral has no emotion/style control.
            stream: Whether to yield intermediate chunks during generation.
            streaming_interval: Approximate seconds of audio per streamed chunk.

        Returns:
            Iterator over generation results.

        Raises:
            ValueError: If instruct is supplied.
        """
        if instruct is not None:
            msg = "'instruct' is not supported by the voxtral engine"
            raise ValueError(msg)

        if stream:
            return model.generate(text=text, voice=voice, stream=True, streaming_interval=streaming_interval)
        return model.generate(text=text, voice=voice)


class Qwen3Engine:
    """Qwen3-TTS CustomVoice: speakers from config.json, ``generate_custom_voice``."""

    def __init__(self, language: str) -> None:
        """Initialize the engine.

        Args:
            language: Language name passed to the model (e.g. "English", "German").
                Required — Qwen3 has no safe default.

        Raises:
            ValueError: If language is empty.
        """
        if not language:
            msg = "'language' is required in config.yaml for the qwen3 engine"
            raise ValueError(msg)
        self._language = language

    @property
    def kind(self) -> str:
        """Engine identifier as written in config.yaml."""
        return QWEN3

    @property
    def language(self) -> str:
        """Language passed to the model on every generation."""
        return self._language

    def discover_voices(self, model_dir: Path) -> list[str]:
        """List speakers from ``talker_config.spk_id`` in the model's config.json.

        Reading the speaker list from disk (rather than from a loaded model) keeps
        voice discovery available before the MLX model is loaded on the worker
        thread, matching how the Voxtral engine behaves.

        Args:
            model_dir: Path to the model directory.

        Returns:
            Sorted list of available speaker names.

        Raises:
            FileNotFoundError: If config.json is missing.
            ValueError: If the config declares no speakers.
        """
        config_path = model_dir / "config.json"
        if not config_path.exists():
            msg = f"No config.json found in {model_dir}"
            raise FileNotFoundError(msg)

        with config_path.open() as f:
            loaded: object = json.load(f)

        if not isinstance(loaded, dict):
            msg = f"Invalid {config_path}: expected a mapping, got {type(loaded).__name__}"
            raise ValueError(msg)
        raw = cast(dict[str, object], loaded)

        talker_config = raw.get("talker_config")
        if not isinstance(talker_config, dict):
            msg = f"No 'talker_config' mapping in {config_path}"
            raise ValueError(msg)
        talker = cast(dict[str, object], talker_config)

        spk_id = talker.get("spk_id")
        if not isinstance(spk_id, dict) or not spk_id:
            msg = f"No speakers declared under 'talker_config.spk_id' in {config_path}"
            raise ValueError(msg)
        speakers = cast(dict[str, object], spk_id)

        return sorted(str(name) for name in speakers)

    def validate_model(self, model: TTSModel, model_id: str) -> None:
        """Check the loaded model exposes CustomVoice generation.

        Args:
            model: Loaded model.
            model_id: Model path or identifier, for the error message.

        Raises:
            RuntimeError: If the model has no usable generate_custom_voice method.
        """
        method = getattr(model, "generate_custom_voice", None)
        if method is None:
            msg = f"Model {model_id} does not support generate_custom_voice (engine: {QWEN3}). Use a Qwen3-TTS CustomVoice model."
            raise RuntimeError(msg)

    def generate(
        self,
        model: TTSModel,
        text: str,
        voice: str,
        instruct: str | None,
        stream: bool,
        streaming_interval: float,
    ) -> Iterator[GenerationResult]:
        """Generate speech with the named speaker, language, and optional style.

        Args:
            model: Loaded Qwen3-TTS CustomVoice model.
            text: Text to synthesize.
            voice: Speaker name.
            instruct: Optional free-text emotion/style instruction.
            stream: Whether to yield intermediate chunks during generation.
            streaming_interval: Approximate seconds of audio per streamed chunk.

        Returns:
            Iterator over generation results.
        """
        custom_voice_model = cast(CustomVoiceModel, model)
        if stream:
            return custom_voice_model.generate_custom_voice(
                text=text,
                speaker=voice,
                language=self._language,
                instruct=instruct,
                stream=True,
                streaming_interval=streaming_interval,
            )
        return custom_voice_model.generate_custom_voice(
            text=text,
            speaker=voice,
            language=self._language,
            instruct=instruct,
        )


def build_engine(kind: str, language: str | None) -> TTSEngine:
    """Build the engine named by the config's ``engine:`` key.

    Args:
        kind: Engine identifier — one of ENGINE_KINDS.
        language: Language for the qwen3 engine; must be None for voxtral.

    Returns:
        The engine implementation for that model family.

    Raises:
        ValueError: If kind is unknown, or language is missing or superfluous.
    """
    if kind == VOXTRAL:
        if language is not None:
            msg = "'language' is not supported by the voxtral engine"
            raise ValueError(msg)
        return VoxtralEngine()
    if kind == QWEN3:
        if language is None:
            msg = "'language' is required in config.yaml for the qwen3 engine"
            raise ValueError(msg)
        return Qwen3Engine(language)
    msg = f"Unknown engine '{kind}' in config.yaml. Supported: {', '.join(ENGINE_KINDS)}"
    raise ValueError(msg)
