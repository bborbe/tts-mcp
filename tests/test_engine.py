"""Tests for the pluggable TTS engines."""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pyloudnorm as pyln
import pytest

from src.tts import (
    QWEN3,
    VOXTRAL,
    AudioSettings,
    Qwen3Engine,
    VoxtralEngine,
    build_engine,
    iter_stream_chunks,
    streaming_chunk_iter,
)


def _write_qwen3_config(model_dir: Path, spk_id: dict[str, int] | None) -> None:
    talker_config: dict[str, object] = {}
    if spk_id is not None:
        talker_config["spk_id"] = spk_id
    (model_dir / "config.json").write_text(json.dumps({"talker_config": talker_config}))


def _write_qwen3_language_config(model_dir: Path, languages: list[str] | None) -> None:
    talker_config: dict[str, object] = {"spk_id": {"ryan": 3061}}
    if languages is not None:
        talker_config["codec_language_id"] = {name.lower(): 2050 + index for index, name in enumerate(languages)}
    (model_dir / "config.json").write_text(json.dumps({"talker_config": talker_config}))


def _stub_mlx_random(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Install a stub ``mlx.core`` so the RNG seeding can be observed without MLX.

    MLX is macOS/arm64-only, so importing it directly would fail on CI. The
    engine imports it lazily at call time, so a stub in ``sys.modules`` is what
    the import resolves to.

    Returns:
        The list the stubbed ``mx.random.seed`` appends each seed to.
    """
    seeded: list[int] = []
    core = types.SimpleNamespace(random=types.SimpleNamespace(seed=seeded.append))
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return seeded


class TestBuildEngine:
    """Tests for build_engine dispatch and config validation."""

    def test_builds_voxtral(self) -> None:
        engine = build_engine(VOXTRAL, None)
        assert isinstance(engine, VoxtralEngine)
        assert engine.kind == VOXTRAL

    def test_builds_qwen3(self) -> None:
        engine = build_engine(QWEN3, "English")
        assert isinstance(engine, Qwen3Engine)
        assert engine.kind == QWEN3
        assert engine.language == "English"

    def test_rejects_unknown_engine(self) -> None:
        with pytest.raises(ValueError, match="Unknown engine"):
            build_engine("kokoro", None)

    def test_rejects_qwen3_without_language(self) -> None:
        with pytest.raises(ValueError, match="language"):
            build_engine(QWEN3, None)

    def test_rejects_voxtral_with_language(self) -> None:
        with pytest.raises(ValueError, match="language"):
            build_engine(VOXTRAL, "English")

    def test_passes_sampling_to_qwen3(self) -> None:
        engine = build_engine(QWEN3, "English", temperature=0.6, seed=42)
        assert isinstance(engine, Qwen3Engine)
        assert engine.temperature == 0.6
        assert engine.seed == 42

    def test_sampling_defaults_to_none(self) -> None:
        engine = build_engine(QWEN3, "English")
        assert isinstance(engine, Qwen3Engine)
        assert engine.temperature is None
        assert engine.seed is None

    def test_rejects_sampling_on_voxtral(self) -> None:
        with pytest.raises(ValueError, match="temperature"):
            build_engine(VOXTRAL, None, temperature=0.6)
        with pytest.raises(ValueError, match="seed"):
            build_engine(VOXTRAL, None, seed=42)


class TestQwen3DiscoverVoices:
    """Tests for reading speakers out of the model's config.json."""

    def test_discovers_speakers_sorted(self, tmp_path: Path) -> None:
        _write_qwen3_config(tmp_path, {"ryan": 3061, "aiden": 2861, "vivian": 3065})
        assert Qwen3Engine("English").discover_voices(tmp_path) == ["aiden", "ryan", "vivian"]

    def test_raises_if_config_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="config.json"):
            Qwen3Engine("English").discover_voices(tmp_path)

    def test_raises_if_no_speakers(self, tmp_path: Path) -> None:
        _write_qwen3_config(tmp_path, {})
        with pytest.raises(ValueError, match="spk_id"):
            Qwen3Engine("English").discover_voices(tmp_path)

    def test_raises_if_no_talker_config(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(json.dumps({"other": 1}))
        with pytest.raises(ValueError, match="talker_config"):
            Qwen3Engine("English").discover_voices(tmp_path)


class TestQwen3ValidateLanguage:
    """Tests that a language the model cannot condition on fails loudly."""

    def test_accepts_declared_language(self, tmp_path: Path) -> None:
        _write_qwen3_language_config(tmp_path, ["English", "German"])
        Qwen3Engine("English").validate_language(tmp_path)

    def test_accepts_declared_language_case_insensitively(self, tmp_path: Path) -> None:
        _write_qwen3_language_config(tmp_path, ["English"])
        Qwen3Engine("english").validate_language(tmp_path)

    def test_rejects_unknown_language(self, tmp_path: Path) -> None:
        _write_qwen3_language_config(tmp_path, ["English"])
        with pytest.raises(ValueError, match="Klingon"):
            Qwen3Engine("Klingon").validate_language(tmp_path)

    def test_rejection_names_the_accepted_set(self, tmp_path: Path) -> None:
        _write_qwen3_language_config(tmp_path, ["English", "German"])
        with pytest.raises(ValueError, match="english, german"):
            Qwen3Engine("Klingon").validate_language(tmp_path)

    def test_accepts_auto_without_a_language_map(self, tmp_path: Path) -> None:
        _write_qwen3_language_config(tmp_path, None)
        Qwen3Engine("auto").validate_language(tmp_path)

    def test_rejects_missing_language_map(self, tmp_path: Path) -> None:
        _write_qwen3_language_config(tmp_path, None)
        with pytest.raises(ValueError, match="codec_language_id"):
            Qwen3Engine("English").validate_language(tmp_path)

    def test_voxtral_ignores_language(self, tmp_path: Path) -> None:
        VoxtralEngine().validate_language(tmp_path)


class TestQwen3Sampling:
    """Tests that temperature and seed reach the model call and the RNG."""

    def test_temperature_reaches_the_model_call(self) -> None:
        model = MagicMock()
        Qwen3Engine("English", temperature=0.6).generate(model, "hi", "ryan", None, stream=False, streaming_interval=1.0)
        model.generate_custom_voice.assert_called_once_with(
            text="hi",
            speaker="ryan",
            language="English",
            instruct=None,
            temperature=0.6,
        )

    def test_absent_temperature_leaves_the_call_unchanged(self) -> None:
        model = MagicMock()
        Qwen3Engine("English").generate(model, "hi", "ryan", None, stream=False, streaming_interval=1.0)
        assert "temperature" not in model.generate_custom_voice.call_args.kwargs

    def test_seed_seeds_the_process_rng(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seeded = _stub_mlx_random(monkeypatch)
        model = MagicMock()

        Qwen3Engine("English", seed=42).generate(model, "hi", "ryan", None, stream=False, streaming_interval=1.0)

        assert seeded == [42]

    def test_no_seed_leaves_the_rng_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seeded = _stub_mlx_random(monkeypatch)
        model = MagicMock()

        Qwen3Engine("English").generate(model, "hi", "ryan", None, stream=False, streaming_interval=1.0)

        assert seeded == []


class TestGenerateDispatch:
    """Tests that each engine drives its model family with the right call."""

    def test_voxtral_buffered_call(self) -> None:
        model = MagicMock()
        VoxtralEngine().generate(model, "hi", "casual_male", None, stream=False, streaming_interval=1.0)
        model.generate.assert_called_once_with(text="hi", voice="casual_male")

    def test_voxtral_streaming_call(self) -> None:
        model = MagicMock()
        VoxtralEngine().generate(model, "hi", "casual_male", None, stream=True, streaming_interval=2.0)
        model.generate.assert_called_once_with(text="hi", voice="casual_male", stream=True, streaming_interval=2.0)

    def test_voxtral_rejects_instruct(self) -> None:
        model = MagicMock()
        with pytest.raises(ValueError, match="instruct"):
            VoxtralEngine().generate(model, "hi", "casual_male", "Very happy.", stream=False, streaming_interval=1.0)

    def test_qwen3_buffered_call(self) -> None:
        model = MagicMock()
        Qwen3Engine("English").generate(model, "hi", "ryan", None, stream=False, streaming_interval=1.0)
        model.generate_custom_voice.assert_called_once_with(text="hi", speaker="ryan", language="English", instruct=None)

    def test_qwen3_passes_instruct_and_streaming(self) -> None:
        model = MagicMock()
        Qwen3Engine("German").generate(model, "hallo", "eric", "Very happy.", stream=True, streaming_interval=1.5)
        model.generate_custom_voice.assert_called_once_with(
            text="hallo",
            speaker="eric",
            language="German",
            instruct="Very happy.",
            stream=True,
            streaming_interval=1.5,
        )


class TestStreamingPathWiring:
    """Tests that the streaming helpers drive engine.generate with stream=True.

    Covers the iter_stream_chunks -> engine.generate wiring and the
    streaming_chunk_iter -> iter_stream_chunks hand-off, which the engine
    dispatch tests above exercise only at the engine boundary.
    """

    def test_iter_stream_chunks_requests_streaming_from_voxtral(self) -> None:
        model = MagicMock()
        model.generate.return_value = [
            MagicMock(audio=np.ones(100, dtype=np.float32)),
            MagicMock(audio=np.zeros(0, dtype=np.float32)),
            MagicMock(audio=np.ones(50, dtype=np.float32)),
        ]

        chunks = list(iter_stream_chunks(VoxtralEngine(), model, "hello", "casual_male", None, 1.5))

        model.generate.assert_called_once_with(text="hello", voice="casual_male", stream=True, streaming_interval=1.5)
        assert [len(c) for c in chunks] == [100, 50]

    def test_iter_stream_chunks_requests_streaming_from_qwen3_with_instruct(self) -> None:
        model = MagicMock()
        model.generate_custom_voice.return_value = [MagicMock(audio=np.ones(80, dtype=np.float32))]

        chunks = list(iter_stream_chunks(Qwen3Engine("English"), model, "hi", "ryan", "Very happy.", 2.0))

        model.generate_custom_voice.assert_called_once_with(
            text="hi",
            speaker="ryan",
            language="English",
            instruct="Very happy.",
            stream=True,
            streaming_interval=2.0,
        )
        assert [len(c) for c in chunks] == [80]

    def test_streaming_chunk_iter_passes_instruct_through_to_model(self) -> None:
        model = MagicMock()
        model.generate_custom_voice.return_value = [MagicMock(audio=np.ones(120, dtype=np.float32))]
        settings = AudioSettings(
            sample_rate=24000,
            lead_silence_ms=0,
            normalize_audio=False,
            target_lufs=-20.0,
            true_peak_ceiling_db=-1.0,
            min_duration_seconds=0.5,
            meter=pyln.Meter(24000.0),
            stream=True,
            streaming_interval=1.0,
            streaming_warmup_seconds=2.0,
        )

        chunks = list(streaming_chunk_iter(Qwen3Engine("German"), model, "hallo", "eric", "Calm.", settings))

        model.generate_custom_voice.assert_called_once_with(
            text="hallo",
            speaker="eric",
            language="German",
            instruct="Calm.",
            stream=True,
            streaming_interval=1.0,
        )
        assert [len(c) for c in chunks] == [120]


class TestValidateModel:
    """Tests that each engine rejects a model of the wrong family."""

    def test_qwen3_rejects_model_without_custom_voice(self) -> None:
        model = MagicMock(spec=["generate"])
        with pytest.raises(RuntimeError, match="generate_custom_voice"):
            Qwen3Engine("English").validate_model(model, "some-model")

    def test_qwen3_accepts_custom_voice_model(self) -> None:
        model = MagicMock(spec=["generate_custom_voice"])
        Qwen3Engine("English").validate_model(model, "some-model")

    def test_voxtral_rejects_model_without_generate(self) -> None:
        model = MagicMock(spec=["generate_custom_voice"])
        with pytest.raises(RuntimeError, match="does not support generation"):
            VoxtralEngine().validate_model(model, "some-model")
