"""CLI frontend for text-to-speech using Voxtral via mlx-audio."""

import argparse
import dataclasses
import datetime
import queue
import sys
import termios
import threading
import tty
from pathlib import Path
from typing import cast

import pyloudnorm as pyln

from src.tts import (
    OUTPUT_DIR,
    AudioSettings,
    audio_worker_from_model_id,
    clean_text,
    discover_models,
    discover_voices,
    load_config,
    make_output_path,
    simplify_punctuation,
)


@dataclasses.dataclass(frozen=True)
class NormalizationSettings:
    """Loudness normalization configuration loaded from config.yaml."""

    enabled: bool
    target_lufs: float
    true_peak_ceiling_db: float
    min_duration_seconds: float


@dataclasses.dataclass(frozen=True)
class CliConfig:
    """CLI-relevant settings parsed from config.yaml.

    Mirrors the server's _ServerConfig dataclass so both entry paths carry parsed
    settings as a typed object rather than a positional tuple.
    """

    sample_rate: int
    save_wav: bool
    simplify_punctuation: bool
    lead_silence_ms: int
    stream: bool
    streaming_interval: float
    streaming_warmup_seconds: float
    normalization: NormalizationSettings


def select_model(models: list[Path]) -> Path:
    """Display available models and let the user select one.

    Args:
        models: List of available model directory paths.

    Returns:
        Selected model directory path.
    """
    if len(models) == 1:
        print(f"\nUsing model: {models[0].name}")
        return models[0]

    print("\nAvailable models:")
    for i, model_path in enumerate(models, 1):
        print(f"  {i}. {model_path.name}")

    while True:
        choice = input(f"\nSelect model [1-{len(models)}]: ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                return models[idx]
        print(f"Invalid choice. Enter a number between 1 and {len(models)}.")


def select_voice(voices: list[str]) -> str:
    """Display available voices and let the user select one.

    Args:
        voices: List of available voice names.

    Returns:
        Selected voice name.
    """
    print("\nAvailable voices:")
    for i, voice in enumerate(voices, 1):
        print(f"  {i}. {voice}")

    while True:
        choice = input(f"\nSelect voice [1-{len(voices)}]: ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(voices):
                return voices[idx]
        print(f"Invalid choice. Enter a number between 1 and {len(voices)}.")


def read_input(prompt: str) -> str | None:
    """Read a line of input, character by character. Returns None on exit.

    Enter once inserts a newline; Enter twice submits the buffer. Pressing
    Enter twice with an empty buffer exits, as does pressing ESC twice.

    Args:
        prompt: The prompt to display.

    Returns:
        The entered text, or None if the user chose to exit.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf: list[str] = []
    last_was_esc = False
    last_was_enter = False

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)

            if ch == "\x1b":  # ESC
                if last_was_esc:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return None
                last_was_esc = True
                last_was_enter = False
                continue

            if ch in ("\r", "\n"):  # Enter
                if last_was_enter:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return "".join(buf) or None
                last_was_enter = True
                last_was_esc = False
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                continue

            if ch in ("\x7f", "\x08"):  # Backspace
                last_was_esc = False
                last_was_enter = False
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue

            if ch == "\x03":  # Ctrl+C
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return None

            if last_was_enter:
                buf.append("\n")
            last_was_esc = False
            last_was_enter = False
            buf.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def list_outputs(output_dir: Path) -> None:
    """List previously generated audio files in the output directory.

    Args:
        output_dir: Directory containing generated WAV files.
    """
    if not output_dir.exists():
        print("No output directory found. No audio has been generated yet.")
        return

    wav_files = sorted(output_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wav_files:
        print("No audio files found in output directory.")
        return

    print(f"\nGenerated audio files in {output_dir}:")
    for wav in wav_files:
        size_kb = wav.stat().st_size // 1024
        mtime = wav.stat().st_mtime
        ts = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {ts}  {wav.name}  ({size_kb} KB)")


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and return the CLI argument parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(description="Convert text to speech using Voxtral TTS via mlx-audio")
    parser.add_argument("text", nargs="?", help="Text to convert to speech (or enter interactively)")
    parser.add_argument("--model", help="MLX model path (overrides interactive selection)")
    parser.add_argument("--voice", help="Voice to use for synthesis (or select interactively)")
    parser.add_argument(
        "--list-outputs",
        action="store_true",
        help="List previously generated audio files and exit",
    )
    return parser


def resolve_model_dir(cli_model: str | None) -> str:
    """Resolve the model directory from CLI arg or interactive selection.

    Args:
        cli_model: Model path from CLI argument, or None.

    Returns:
        Resolved model directory path.

    Raises:
        ValueError: If no models_dir in config.yaml.
        FileNotFoundError: If the model directory does not exist.
    """
    if cli_model:
        if not Path(cli_model).exists():
            msg = f"Model directory does not exist: {cli_model}"
            raise FileNotFoundError(msg)
        return cli_model

    config = load_config()
    models_dir_str = config.get("models_dir")
    if not models_dir_str:
        msg = "No models_dir in config.yaml and no --model argument provided"
        raise ValueError(msg)

    models = discover_models(Path(models_dir_str))
    selected = select_model(models)
    return str(selected)


def prepare_text(text: str, simplify_punct: bool) -> str:
    """Clean text and optionally simplify punctuation.

    Args:
        text: Raw input text.
        simplify_punct: Whether punctuation simplification is enabled.

    Returns:
        Cleaned text, optionally with simplified punctuation.
    """
    prepared = clean_text(text)
    if prepared and simplify_punct:
        prepared = simplify_punctuation(prepared)
    return prepared


def shutdown_worker(work_queue: queue.Queue[str | None], worker: threading.Thread) -> None:
    """Wait for queued work to finish and stop the worker thread."""
    work_queue.join()
    work_queue.put(None)
    worker.join()


def _require(config: dict[str, object], key: str) -> object:
    """Fetch a required config key or raise ValueError with a clear message."""
    value = config.get(key)
    if value is None:
        msg = f"Missing required key '{key}' in config.yaml"
        raise ValueError(msg)
    return value


def load_cli_config() -> CliConfig:
    """Load CLI-relevant settings from config.yaml.

    Returns:
        A CliConfig with all parsed CLI settings.

    Raises:
        ValueError: If required keys are missing from config.yaml.
    """
    config = load_config()

    normalization = NormalizationSettings(
        enabled=bool(_require(config, "normalize_audio")),
        target_lufs=float(cast(float, _require(config, "target_lufs"))),
        true_peak_ceiling_db=float(cast(float, _require(config, "true_peak_ceiling_db"))),
        min_duration_seconds=float(cast(float, _require(config, "min_duration_seconds"))),
    )

    return CliConfig(
        sample_rate=int(cast(int, _require(config, "sample_rate"))),
        save_wav=bool(_require(config, "save_wav")),
        simplify_punctuation=bool(config.get("simplify_punctuation")),
        lead_silence_ms=int(cast(int, _require(config, "lead_silence_ms"))),
        stream=bool(_require(config, "stream")),
        streaming_interval=float(cast(float, _require(config, "streaming_interval"))),
        streaming_warmup_seconds=float(cast(float, _require(config, "streaming_warmup_seconds"))),
        normalization=normalization,
    )


def main() -> None:
    """Main entry point for text-to-speech."""
    parser = create_argument_parser()
    args = parser.parse_args()

    if args.list_outputs:
        list_outputs(OUTPUT_DIR)
        return

    cfg = load_cli_config()

    model_dir = resolve_model_dir(args.model)
    available_voices = discover_voices(Path(model_dir))

    voice: str = args.voice if args.voice else select_voice(available_voices)

    if voice not in available_voices:
        print(f"Error: voice '{voice}' not available. Choose from: {', '.join(available_voices)}", file=sys.stderr)
        sys.exit(1)

    print(f"\nLoading model: {model_dir}")

    output_path = make_output_path(OUTPUT_DIR) if cfg.save_wav else None
    work_queue: queue.Queue[str | None] = queue.Queue()
    ready_queue: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

    meter = pyln.Meter(float(cfg.sample_rate))

    settings = AudioSettings(
        sample_rate=cfg.sample_rate,
        lead_silence_ms=cfg.lead_silence_ms,
        normalize_audio=cfg.normalization.enabled,
        target_lufs=cfg.normalization.target_lufs,
        true_peak_ceiling_db=cfg.normalization.true_peak_ceiling_db,
        min_duration_seconds=cfg.normalization.min_duration_seconds,
        meter=meter,
        stream=cfg.stream,
        streaming_interval=cfg.streaming_interval,
        streaming_warmup_seconds=cfg.streaming_warmup_seconds,
    )

    worker = threading.Thread(
        target=audio_worker_from_model_id,
        args=(work_queue, model_dir, voice, output_path, settings, ready_queue),
        daemon=True,
    )
    worker.start()

    load_error = ready_queue.get()
    if load_error is not None:
        print(f"Error: {load_error}", file=sys.stderr)
        sys.exit(1)

    print(f"Voice: {voice}")
    print("Type text. Enter twice submits (single Enter = newline). Enter twice on empty input or ESC twice quits.\n")

    if args.text:
        text = prepare_text(args.text, cfg.simplify_punctuation)
        if not text:
            print("Error: text is empty after cleaning", file=sys.stderr)
            sys.exit(1)
        work_queue.put(text)
        shutdown_worker(work_queue, worker)
        return

    while True:
        result = read_input("Text: ")
        if result is None:
            break
        text = prepare_text(result, cfg.simplify_punctuation)
        if not text:
            continue
        work_queue.put(text)
        print()

    shutdown_worker(work_queue, worker)


if __name__ == "__main__":
    main()
