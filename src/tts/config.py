"""Config file resolution, model/voice discovery, and output path helpers."""

import datetime
import os
from pathlib import Path
from typing import Any, cast

import yaml

OUTPUT_DIR = Path("data/output")

# Config file resolution. In precedence order:
#   1. $TTS_MCP_CONFIG (explicit override)
#   2. $XDG_CONFIG_HOME/tts-mcp/config.yaml (defaults to ~/.config/tts-mcp/config.yaml)
#   3. ./config.yaml in the current working directory (project-root fallback / back-compat)
# Data and model paths inside the config remain relative to the process working
# directory, not to the config file, so moving the config out of the repo does
# not change how `model:` / `models_dir:` resolve.
DEFAULT_CONFIG_PATH = Path("config.yaml")


def config_env_var() -> str:
    """Name of the environment variable that overrides the config file location."""
    return "TTS_MCP_CONFIG"


def xdg_config_path() -> Path:
    """Path to config.yaml under XDG config home (~/.config/tts-mcp/config.yaml by default)."""
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_home) if xdg_home else Path.home() / ".config"
    return base / "tts-mcp" / "config.yaml"


def resolve_config_path() -> Path:
    """Resolve the config file location following the documented precedence order."""
    override = os.environ.get(config_env_var())
    if override:
        return Path(override)
    xdg_path = xdg_config_path()
    if xdg_path.exists():
        return xdg_path
    return DEFAULT_CONFIG_PATH


def load_config() -> dict[str, Any]:
    """Load configuration from the resolved config path (see resolve_config_path).

    Returns:
        Configuration dictionary.

    Raises:
        FileNotFoundError: If the resolved config file does not exist.
        ValueError: If the config file is empty or invalid.
    """
    config_path = resolve_config_path()
    if not config_path.exists():
        msg = f"Configuration file not found: {config_path}"
        raise FileNotFoundError(msg)

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        msg = f"Invalid config.yaml: expected a mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    return cast(dict[str, Any], raw)


def discover_models(models_dir: Path) -> list[Path]:
    """Discover downloaded TTS models in the models directory.

    Args:
        models_dir: Base directory containing model subdirectories.

    Returns:
        Sorted list of model directory paths that contain model.safetensors.

    Raises:
        FileNotFoundError: If models_dir does not exist or has no models.
    """
    if not models_dir.exists():
        msg = f"Models directory does not exist: {models_dir}"
        raise FileNotFoundError(msg)

    models = sorted(p.parent for p in models_dir.glob("*/model.safetensors"))
    if not models:
        msg = f"No models found in {models_dir}. Run ./scripts/download-model.sh first."
        raise FileNotFoundError(msg)

    return models


def discover_voices(model_dir: Path) -> list[str]:
    """Discover available voices from the model's voice_embedding directory.

    Args:
        model_dir: Path to the model directory.

    Returns:
        Sorted list of available voice names.

    Raises:
        FileNotFoundError: If voice_embedding directory does not exist or has no voices.
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


def make_output_path(output_dir: Path) -> Path:
    """Generate a timestamped output path for a new audio file.

    Args:
        output_dir: Directory to save audio files.

    Returns:
        Path with a timestamp-based filename.
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"speech_{ts}.wav"
