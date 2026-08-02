"""Tests for parsing the server's engine declarations from config.yaml."""

from pathlib import Path
from typing import Any

import pytest

from src.server import _parse_server_config
from src.tts import QWEN3, VOXTRAL


def _base_config(model_dir: Path) -> dict[str, Any]:
    """A complete server config in the legacy flat form."""
    return {
        "engine": VOXTRAL,
        "model": str(model_dir),
        "default_voice": "casual_female",
        "sample_rate": 24000,
        "save_wav": True,
        "normalize_audio": True,
        "target_lufs": -10.0,
        "true_peak_ceiling_db": -1.0,
        "min_duration_seconds": 0.5,
        "lead_silence_ms": 200,
        "stream": False,
        "streaming_interval": 1.0,
        "streaming_warmup_seconds": 2.0,
    }


def _multi_engine_config(model_dir: Path) -> dict[str, Any]:
    """The same config in the mapping form, declaring both engines."""
    config = _base_config(model_dir)
    for key in ("engine", "model"):
        del config[key]
    config["default_engine"] = QWEN3
    config["engines"] = {
        QWEN3: {"model": str(model_dir), "language": "English", "default_voice": "casual_female"},
        VOXTRAL: {"model": str(model_dir)},
    }
    return config


class TestLegacyFlatForm:
    """The pre-existing single-engine config must keep working untouched."""

    def test_parses_to_one_engine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.server.load_config", lambda: _base_config(tmp_path))

        cfg = _parse_server_config()

        assert len(cfg.engines) == 1
        assert cfg.engines[0].kind == VOXTRAL
        assert cfg.engines[0].model_path == str(tmp_path)
        assert cfg.engines[0].language is None
        assert cfg.default_engine == VOXTRAL

    def test_default_voice_becomes_the_engine_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.server.load_config", lambda: _base_config(tmp_path))

        cfg = _parse_server_config()

        assert cfg.engines[0].default_voice == "casual_female"

    def test_missing_model_directory_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _base_config(tmp_path)
        config["model"] = str(tmp_path / "nope")
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(FileNotFoundError, match="Model directory does not exist"):
            _parse_server_config()


class TestMappingForm:
    """The engines: mapping declares one or more engines."""

    def test_parses_every_declared_engine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.server.load_config", lambda: _multi_engine_config(tmp_path))

        cfg = _parse_server_config()

        by_kind = {engine.kind: engine for engine in cfg.engines}
        assert set(by_kind) == {QWEN3, VOXTRAL}
        assert by_kind[QWEN3].language == "English"
        assert by_kind[VOXTRAL].language is None
        assert cfg.default_engine == QWEN3

    def test_per_engine_default_voice_overrides_the_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][VOXTRAL]["default_voice"] = "de_male"
        monkeypatch.setattr("src.server.load_config", lambda: config)

        cfg = _parse_server_config()

        by_kind = {engine.kind: engine for engine in cfg.engines}
        assert by_kind[VOXTRAL].default_voice == "de_male"
        assert by_kind[QWEN3].default_voice == "casual_female"

    def test_single_engine_needs_no_default_engine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        del config["default_engine"]
        del config["engines"][VOXTRAL]
        monkeypatch.setattr("src.server.load_config", lambda: config)

        cfg = _parse_server_config()

        assert cfg.default_engine == QWEN3

    def test_several_engines_require_default_engine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        del config["default_engine"]
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="'default_engine' in config.yaml is required"):
            _parse_server_config()

    def test_undeclared_default_engine_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["default_engine"] = "piper"
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="default_engine 'piper' is not declared"):
            _parse_server_config()

    def test_empty_engines_mapping_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"] = {}
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="'engines' in config.yaml must be a non-empty mapping"):
            _parse_server_config()

    def test_engine_without_model_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        del config["engines"][VOXTRAL]["model"]
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="engines.voxtral.model"):
            _parse_server_config()


class TestFormsAreMutuallyExclusive:
    """Silent precedence between two config forms is a bug factory."""

    def test_both_forms_together_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engine"] = VOXTRAL
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="declares both 'engines:' and the flat key"):
            _parse_server_config()

    def test_error_names_the_offending_flat_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["model"] = str(tmp_path)
        config["language"] = "English"
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="model, language"):
            _parse_server_config()


class TestSampleRateGuard:
    """One player and one meter are built per worker, so rates must agree."""

    def test_matching_per_engine_sample_rate_is_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][VOXTRAL]["sample_rate"] = 24000
        monkeypatch.setattr("src.server.load_config", lambda: config)

        cfg = _parse_server_config()

        assert cfg.sample_rate == 24000

    def test_differing_per_engine_sample_rate_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][VOXTRAL]["sample_rate"] = 48000
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="Per-engine sample rates are not supported"):
            _parse_server_config()
