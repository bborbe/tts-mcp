"""Tests for parsing the server's engine declarations from config.yaml."""

from pathlib import Path
from typing import Any

import pytest

from src.server import _build_server_state, _parse_server_config
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


class TestAllowedVoices:
    """allowed_voices is declared per engine and restricting is opt-in."""

    def test_absent_allowlist_leaves_the_engine_unrestricted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.server.load_config", lambda: _multi_engine_config(tmp_path))

        cfg = _parse_server_config()

        by_kind = {engine.kind: engine for engine in cfg.engines}
        assert by_kind[QWEN3].allowed_voices == ()
        assert by_kind[VOXTRAL].allowed_voices == ()

    def test_empty_allowlist_means_unrestricted_not_allow_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][QWEN3]["allowed_voices"] = []
        monkeypatch.setattr("src.server.load_config", lambda: config)

        cfg = _parse_server_config()

        assert cfg.engines[0].allowed_voices == ()

    def test_parses_the_per_engine_allowlist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][QWEN3]["allowed_voices"] = ["casual_female"]
        config["engines"][VOXTRAL]["allowed_voices"] = ["casual_male"]
        monkeypatch.setattr("src.server.load_config", lambda: config)

        cfg = _parse_server_config()

        by_kind = {engine.kind: engine for engine in cfg.engines}
        assert by_kind[QWEN3].allowed_voices == ("casual_female",)
        assert by_kind[VOXTRAL].allowed_voices == ("casual_male",)

    def test_non_list_allowlist_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][QWEN3]["allowed_voices"] = "casual_female"
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="engines.qwen3.allowed_voices"):
            _parse_server_config()

    def test_non_string_entry_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _multi_engine_config(tmp_path)
        config["engines"][QWEN3]["allowed_voices"] = ["casual_female", 7]
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="engines.qwen3.allowed_voices"):
            _parse_server_config()

    def test_top_level_allowlist_in_the_flat_form_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Silently ignoring it would leave the operator believing they were protected."""
        config = _base_config(tmp_path)
        config["allowed_voices"] = ["casual_female"]
        monkeypatch.setattr("src.server.load_config", lambda: config)

        with pytest.raises(ValueError, match="Top-level 'allowed_voices' is not supported"):
            _parse_server_config()


class TestStartupValidatesAllowedVoices:
    """An allowlist entry its engine cannot synthesise must fail at startup.

    At request time the substitution would pick a voice that does not exist and
    the caller would get a 400 from a config that looked valid — a silent no-op
    the operator would only discover from a failed utterance.
    """

    _DISCOVERED = {QWEN3: ["casual_female"], VOXTRAL: ["casual_female", "casual_male"]}

    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: list[str]) -> Any:
        config = _multi_engine_config(tmp_path)
        config["engines"][QWEN3]["allowed_voices"] = allowed
        monkeypatch.setattr("src.server.load_config", lambda: config)
        monkeypatch.setattr("src.server._discover_engine_voices", lambda _cfg: (dict(self._DISCOVERED), {}))
        return _parse_server_config()

    def test_unknown_allowed_voice_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = self._config(tmp_path, monkeypatch, ["casual_female", "nonexistent"])

        with pytest.raises(ValueError, match="allowed_voices nonexistent for engine 'qwen3'"):
            _build_server_state(cfg)

    def test_known_allowed_voices_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = self._config(tmp_path, monkeypatch, ["casual_female"])

        state = _build_server_state(cfg)

        assert state.allowed_voices_for(QWEN3) == ("casual_female",)
        assert state.allowed_voices_for(VOXTRAL) == ()
        assert state.engine_default_voice(VOXTRAL) == "casual_female"
