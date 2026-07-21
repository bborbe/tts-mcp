"""Shared TTS engine: config, model/voice discovery, audio generation, playback, and saving."""

from src.tts.config import (
    DEFAULT_CONFIG_PATH,
    OUTPUT_DIR,
    config_env_var,
    discover_models,
    discover_voices,
    load_config,
    make_output_path,
    resolve_config_path,
)
from src.tts.device import (
    default_output_device_id,
    restart_process_on_device_change,
    start_output_device_change_watcher,
)
from src.tts.generate import (
    generate_chunks,
    generate_speech,
    iter_stream_chunks,
)
from src.tts.normalize import (
    boost_gain,
    normalize_chunks,
    normalize_stream,
)
from src.tts.player import (
    AudioPlayer,
    PlaybackJob,
    StreamingPlaybackJob,
    play_audio,
    play_chunks,
    play_stream,
    save_audio,
)
from src.tts.protocols import TTSModel
from src.tts.text import (
    clean_text,
    simplify_punctuation,
)
from src.tts.worker import (
    AudioSettings,
    audio_worker,
    audio_worker_from_model_id,
    streaming_chunk_iter,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "OUTPUT_DIR",
    "AudioPlayer",
    "AudioSettings",
    "PlaybackJob",
    "StreamingPlaybackJob",
    "TTSModel",
    "audio_worker",
    "audio_worker_from_model_id",
    "boost_gain",
    "clean_text",
    "config_env_var",
    "default_output_device_id",
    "discover_models",
    "discover_voices",
    "generate_chunks",
    "generate_speech",
    "iter_stream_chunks",
    "load_config",
    "make_output_path",
    "normalize_chunks",
    "normalize_stream",
    "play_audio",
    "play_chunks",
    "play_stream",
    "resolve_config_path",
    "restart_process_on_device_change",
    "save_audio",
    "simplify_punctuation",
    "start_output_device_change_watcher",
    "streaming_chunk_iter",
]
