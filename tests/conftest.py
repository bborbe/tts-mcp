"""Test-session setup shared by every test module.

CI runs the suite on Linux, where `mlx-audio` cannot be installed: MLX is
Apple-silicon only. `src.tts` imports `mlx_audio.tts.utils.load` at module load,
so without a placeholder every test module would fail at import before running a
single assertion. No test calls into MLX — each one injects its own loader — so a
placeholder whose `load` refuses to run is enough. On macOS the real package is
found and left untouched.
"""

import importlib.util
import sys
import types


def _install_mlx_audio_placeholder() -> None:
    """Register a placeholder mlx_audio package when the real one is unavailable."""
    if importlib.util.find_spec("mlx_audio") is not None:
        return

    def load(model_path: str) -> object:
        raise RuntimeError("mlx-audio is not installed on this platform; inject a loader instead of calling the default")

    package = types.ModuleType("mlx_audio")
    tts = types.ModuleType("mlx_audio.tts")
    utils = types.ModuleType("mlx_audio.tts.utils")
    utils.__dict__["load"] = load
    tts.__dict__["utils"] = utils
    package.__dict__["tts"] = tts
    sys.modules["mlx_audio"] = package
    sys.modules["mlx_audio.tts"] = tts
    sys.modules["mlx_audio.tts.utils"] = utils


_install_mlx_audio_placeholder()
