"""Tests for the pluggable TTS engines."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.tts import QWEN3, VOXTRAL, Qwen3Engine, VoxtralEngine, build_engine


def _write_qwen3_config(model_dir: Path, spk_id: dict[str, int] | None) -> None:
    talker_config: dict[str, object] = {}
    if spk_id is not None:
        talker_config["spk_id"] = spk_id
    (model_dir / "config.json").write_text(json.dumps({"talker_config": talker_config}))


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
