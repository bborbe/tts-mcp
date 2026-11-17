"""Test-session setup shared by every test module.

CI runs the suite on Linux, where `mlx-audio` cannot be installed: MLX is
Apple-silicon only. `src.tts` imports `mlx_audio.tts.utils.load` at module load,
so without a placeholder every test module would fail at import before running a
single assertion. No test calls into MLX — each one injects its own loader — so a
placeholder whose `load` refuses to run is enough. On macOS the real package is
found and left untouched.

`sounddevice` needs the same treatment for a different reason: importing it calls
PortAudio's `Pa_Initialize()`, which fails on a Linux runner with no audio server.
`src.tts.player` imports it at module load, so that failure aborts collection for
every test module too. No test plays audio — each injects its own player — so a
stand-in that answers any attribute is enough.
"""

import importlib.util
import sys
import types
from unittest.mock import MagicMock


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


def _install_sounddevice_placeholder() -> None:
    """Register a placeholder sounddevice when the real module cannot initialise.

    The import is the probe: sounddevice initialises PortAudio as a side effect of
    importing, so a missing library and an unreachable audio server both surface
    here rather than at the first call. Any failure means no test can run, and a
    stand-in is strictly better than a collection error.
    """
    try:
        import sounddevice  # noqa: F401, PLC0415 - the import itself is the probe
    except Exception:  # PortAudioError, ImportError, or any other initialisation failure
        sys.modules["sounddevice"] = MagicMock(name="sounddevice")


_install_mlx_audio_placeholder()
_install_sounddevice_placeholder()
