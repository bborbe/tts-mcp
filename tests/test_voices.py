"""Tests for the voice allowlist resolution policy."""

import logging

import pytest

from src.tts import resolve_voice


class TestNoAllowlist:
    """An engine that declares no allowlist keeps every voice it offers reachable."""

    def test_requested_voice_passes_through(self) -> None:
        assert resolve_voice("serena", "qwen3", "ryan", ()) == "serena"

    def test_missing_voice_takes_the_engine_default(self) -> None:
        assert resolve_voice(None, "qwen3", "ryan", ()) == "ryan"


class TestAllowlist:
    """A non-empty allowlist substitutes every voice outside it."""

    def test_allowed_voice_is_unchanged(self) -> None:
        assert resolve_voice("ryan", "qwen3", "ryan", ("ryan",)) == "ryan"

    def test_disallowed_voice_falls_back_to_the_first_allowed(self) -> None:
        assert resolve_voice("serena", "qwen3", "ryan", ("ryan", "dylan")) == "ryan"

    def test_missing_voice_takes_the_engine_default(self) -> None:
        assert resolve_voice(None, "qwen3", "ryan", ("ryan",)) == "ryan"

    def test_substitution_is_logged_at_error_level(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR):
            resolve_voice("serena", "qwen3", "ryan", ("ryan",))

        assert "not allowed" in caplog.text
        assert "serena" in caplog.text

    def test_allowed_voice_is_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR):
            resolve_voice("ryan", "qwen3", "ryan", ("ryan",))

        assert caplog.text == ""


class TestPerEngineIsolation:
    """Voice names are engine-specific, so the allowlist must be too."""

    def test_a_qwen3_voice_does_not_leak_onto_voxtral(self) -> None:
        # 'ryan' exists only on qwen3; requesting it on voxtral must not survive.
        assert resolve_voice("ryan", "voxtral", "casual_male", ("casual_male",)) == "casual_male"

    def test_a_voxtral_voice_does_not_leak_onto_qwen3(self) -> None:
        assert resolve_voice("casual_male", "qwen3", "ryan", ("ryan",)) == "ryan"

    def test_each_engine_keeps_its_own_default(self) -> None:
        assert resolve_voice(None, "qwen3", "ryan", ("ryan",)) == "ryan"
        assert resolve_voice(None, "voxtral", "casual_male", ("casual_male",)) == "casual_male"
