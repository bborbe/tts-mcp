# mistral-text-to-spech

![Made with AI](https://img.shields.io/badge/Made%20with-AI-333333?labelColor=f00) ![Verified by Humans](https://img.shields.io/badge/Verified%20by-Humans-333333?labelColor=brightgreen)

Local text-to-speech powered by Mistral's Voxtral model via MLX, with real-time streaming playback on Apple Silicon. Offers both an interactive CLI and a FastAPI server with an MCP bridge for AI agent integration.

### Features

| Feature | Description |
|---------|-------------|
| Streaming Playback | Plays each utterance as it generates (chunk-by-chunk) for low latency; a buffered mode with cross-message lookahead and loudness normalization is also available |
| Interactive CLI | Terminal interface with model/voice selection, ESC to quit, and backspace support |
| REST API Server | FastAPI server with queued sequential playback and message status tracking |
| MCP Server | Ready-to-use MCP bridge for Claude Code and Claude Desktop integration |
| Multi-Voice | 20 voices across 9 languages (English, German, French, Spanish, Italian, Dutch, Portuguese, Hindi, Arabic) |
| Multi-Model | Supports 4-bit, 6-bit, and bf16 quantization variants of Voxtral 4B |

Under the hood, the project uses [mlx-audio](https://github.com/Blaizzy/mlx-audio) for model loading and inference on Apple Silicon, [sounddevice](https://python-sounddevice.readthedocs.io/) for real-time audio output, and [FastAPI](https://fastapi.tiangolo.com/) for the HTTP server. The MCP server is a lightweight TypeScript relay using the [Model Context Protocol SDK](https://modelcontextprotocol.io/).

## Design Principles

All configuration is explicit — no hardcoded defaults, no silent fallbacks. If a required value is missing from `config.yaml`, the application fails immediately with a clear error message. Audio files are saved to `data/output/` as WAV files with timestamps. A background worker thread owns audio generation and playback: in streaming mode (`stream: true`) it plays each utterance chunk-by-chunk as it is generated for low latency; in buffered mode (`stream: false`) it generates and loudness-normalizes the full utterance before playback, generating the next queued request while the current one plays to eliminate gaps between messages.

## Architecture

There are two independent entry paths into the system. The interactive CLI (`src/main.py`) resolves the model and voice, then starts a worker that loads the model and runs generation on the same thread. The FastAPI server (`src/server.py`) loads the model once at startup and serializes all requests through a work queue and a background audio worker (streaming or buffered per the `stream` setting). AI agents reach the server through the MCP relay (`mcp/tts-mcp.ts`), a thin stdio-to-HTTP bridge; any plain HTTP client can call the REST API directly. In both paths, inference runs on the Apple Silicon GPU via MLX (Metal), with the Voxtral model weights held in unified memory.

```text
┌───────────────────────────┐  ┌───────────────────────────┐  ┌───────────────────────────┐
│         AI Agent          │  │        HTTP Client        │  │       Terminal User       │
│   Claude Code / Desktop   │  │      curl / scripts       │  │         make chat         │
└─────────────┬─────────────┘  └─────────────┬─────────────┘  └─────────────┬─────────────┘
              │ MCP (stdio)                  │ HTTP                         │
              ▼                              │                              ▼
┌───────────────────────────┐                │                ┌───────────────────────────┐
│   MCP Server (Node.js)    │                │                │     CLI · src/main.py     │
│      mcp/tts-mcp.ts       │                │                │     interactive chat      │
│  tools: say, get_voices,  │                │                │  model & voice selection  │
│        get_status         │                │                │  queue text to worker     │
└─────────────┬─────────────┘                │                └─────────────┬─────────────┘
              │ HTTP                         │                              │ model path + text
              ▼                              ▼                              │
┌─────────────┬──────────────────────────────┬───────────────┐              │
│               FastAPI Server · src/server.py               │              │
│                                                            │              │
│  REST API   POST /say · GET /voices · GET /status/{id}     │              │
│             GET /health                                    │              │
│      │                                                     │              │
│      ▼                                                     │              │
│  work queue ───▶ audio worker thread (streaming / lookahead)│              │
└─────────────────────────────────────────────────┬──────────┘              │
                                                  │                         │
                                                  ▼                         ▼
┌──────────────────────┐            ┌─────────────┬─────────────────────────┬──────────────┐
│  Apple Silicon GPU   │            │            Shared TTS Engine · src/tts.py            │
│    Metal via MLX     │            │                                                      │
│                      │◀── text ───│   worker-thread generation                           │
│ Voxtral 4B TTS model │── audio ──▶│   CLI worker loads model before generate             │
│ 4-bit / 6-bit / bf16 │            │             │                                        │
│    unified memory    │            │             ▼                                        │
│                      │            │   loudness normalization (pyloudnorm · BS.1770-4)    │
└──────────────────────┘            │             │                                        │
                                    │             ├────────────────────────┐               │
                                    │             ▼                        ▼               │
                                    │   playback (sounddevice)        WAV writer           │
                                    └─────────────┬────────────────────────┬───────────────┘
                                                  │                        │
                                                  ▼                        ▼
                                            ┌──────────┐         ┌───────────────────┐
                                            │ Speakers │         │ data/output/*.wav │
                                            └──────────┘         └───────────────────┘
```

## Prerequisites

- **Apple Silicon Mac** — MLX requires Apple Silicon (M1/M2/M3/M4)
- **Python 3.12+**
- **uv** — Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **make** — preinstalled with the Xcode Command Line Tools
- **Node.js 18+** — For the MCP server (optional)

## Project Structure

```
.
├── src/                    # Application source code
│   ├── main.py             # CLI frontend
│   ├── server.py           # FastAPI TTS server
│   └── tts.py              # Shared TTS engine (config, generation, playback)
├── tests/                  # Unit tests
│   ├── test_main.py
│   ├── test_server.py
│   ├── test_tts.py
│   └── architecture/       # Architecture import rule tests
├── scripts/                # Utility scripts
│   ├── download-model.sh   # Interactive model downloader
│   └── test-concurrent-say.py  # Concurrent /say load test
├── mcp/                    # MCP server (TypeScript)
│   └── tts-mcp.ts          # MCP relay to FastAPI server
├── data/
│   └── output/             # Generated WAV files
├── config.yaml             # Local configuration (gitignored)
├── Makefile                # Build/dev targets (+ Makefile.variables, Makefile.precommit)
└── pyproject.toml          # Project metadata and dependencies
```

## Setup

```bash
make sync
```

Installs all dependencies via `uv sync --all-extras`.

### Download a Model

```bash
make download
```

Presents three Voxtral 4B variants to choose from:

| Model | Size | Speed |
|-------|------|-------|
| `Voxtral-4B-TTS-2603-mlx-4bit` | ~2.5 GB | Fastest (RTF <1.0x) |
| `Voxtral-4B-TTS-2603-mlx-6bit` | ~3.5 GB | Balanced (RTF ~1.1x) |
| `Voxtral-4B-TTS-2603-mlx-bf16` | ~8.0 GB | Highest quality (RTF ~6.3x) |

After downloading, update `config.yaml` with the model path.

### Getting Started

1. Run `make sync` — installs all dependencies
2. Run `make download` — downloads a Voxtral model
3. Create `config.yaml` with the model path and server settings (see Configuration below)
4. Run `make run` — starts the TTS server
5. Send requests via the API, CLI, or MCP bridge

## Configuration

Configuration lives in a `config.yaml` file, resolved in this precedence order:

1. `$TTS_MCP_CONFIG` — explicit path override
2. `$XDG_CONFIG_HOME/tts-mcp/config.yaml` — defaults to `~/.config/tts-mcp/config.yaml`
3. `./config.yaml` — project root (fallback)

Keeping machine-local config at `~/.config/tts-mcp/config.yaml` keeps it out of the repo working tree. Both the Python server/CLI and the TypeScript MCP relay use the same order. Note: `model:`, `models_dir:`, and data paths inside the file are resolved relative to the **process working directory**, not the config file's location. Example: 

```yaml
model: /path/to/Voxtral-4B-TTS-2603-mlx-6bit
models_dir: /path/to/models
sample_rate: 24000
default_voice: casual_female
save_wav: true
simplify_punctuation: false
stream: true
streaming_interval: 1.0
streaming_warmup_seconds: 2.0
normalize_audio: true
target_lufs: -20.0
true_peak_ceiling_db: -1.0
min_duration_seconds: 0.5
host: 0.0.0.0
port: 12000
```

| Key | Description |
|-----|-------------|
| `model` | Path to the downloaded MLX model directory |
| `models_dir` | Base directory containing model subdirectories (for CLI model selection) |
| `sample_rate` | Audio sample rate in Hz (24000 for Voxtral) |
| `default_voice` | Default voice for server requests without a voice override |
| `save_wav` | Save generated audio to WAV files in `data/output/` (`true` or `false`) |
| `simplify_punctuation` | Strip commas, replace other marks with periods for cleaner speech |
| `stream` | Stream playback within each utterance for low latency (`true` or `false`) — see below |
| `streaming_interval` | Approximate seconds of audio per streamed chunk when `stream` is enabled (e.g. `1.0`) |
| `streaming_warmup_seconds` | Warm-up window (seconds) buffered to measure the streaming loudness gain when `stream` and `normalize_audio` are both on (e.g. `2.0`) |
| `normalize_audio` | Enable boost-only LUFS loudness normalization (`true` or `false`); applied per-utterance when buffered, via a warm-up window when streaming |
| `target_lufs` | Target integrated loudness in LUFS when `normalize_audio` is enabled (e.g. `-20.0` for podcast mid) |
| `true_peak_ceiling_db` | Maximum allowed true peak in dBFS after gain is applied (e.g. `-1.0`); measured via 4x oversampling |
| `min_duration_seconds` | Utterances shorter than this are passed through unchanged (LUFS gating needs ~0.4s) |
| `host` | Server listen address |
| `port` | Server listen port |

### Streaming playback

With `stream: true` (the default), each utterance is played **as it is generated**: the model emits audio in chunks of roughly `streaming_interval` seconds, and each chunk is written to the output device as soon as it is decoded. Playback therefore starts after the first chunk instead of after the whole utterance has been generated, cutting the time-to-first-sound from the full generation time to a fraction of a second.

With `stream: false`, the server/CLI use the buffered path instead: the full utterance is generated (and loudness-normalized) before playback begins, while the next queued message is generated in the background so there is no gap between consecutive messages.

Trade-off: loudness normalization needs the whole signal and is therefore **not applied in streaming mode**. Choose `stream: true` for responsiveness (interactive/agent use) and `stream: false` when consistent normalized loudness across voices matters more than latency.

### Loudness normalization

Different Voxtral voices produce audio at significantly different average levels. Enable `normalize_audio` to apply
utterance-level loudness normalization following ITU-R BS.1770-4 (the EBU R128 standard used in broadcast):

- The integrated loudness of each utterance is measured in LUFS using [`pyloudnorm`](https://github.com/csteinmetz1/pyloudnorm).
- If the measurement is below `target_lufs`, a single scalar gain is applied to lift the utterance toward the target.
- If the measurement is at or above the target, the audio is passed through unchanged — normalization is **boost-only
  and never attenuates**, so already-loud voices are preserved exactly.
- Before applying gain, the 4x-oversampled true peak is measured via `scipy.signal.resample_poly`. The gain is capped
  so the resulting true peak stays at or below `true_peak_ceiling_db`, preventing inter-sample clipping.
- Utterances shorter than `min_duration_seconds` and fully silent utterances are passed through unchanged.
- The same normalized audio is used for both speaker playback and the saved WAV file, so there is no drift between
  what you hear and what is written to disk.

## Usage

| Command | Description |
|---------|-------------|
| `make chat` | Start the interactive CLI |
| `make run` | Start the FastAPI TTS server (foreground) |

### CLI

```bash
make chat
```

Prompts for model and voice selection, then enters an interactive loop. Type text and press Enter twice to submit. Press ESC twice to quit.

For one-shot usage:

```bash
uv run -m src.main "Hello world" --voice casual_female
```

### Server

```bash
make run
```

Starts a FastAPI server with queued playback. The server loads the model once at startup and processes requests sequentially through a background worker.

## API

FastAPI auto-generates interactive docs at `/docs` (Swagger) and `/redoc` (ReDoc) when the server is running.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check |
| GET | `/voices` | List available voices and default voice |
| POST | `/say` | Queue text for synthesis and playback (returns message ID) |
| GET | `/status/{message_id}` | Check status of a queued/playing/completed message |

### POST /say

```json
{
  "text": "Hello, this is a test.",
  "voice": "casual_female"
}
```

Returns `202 Accepted` with a message ID and queue position. Audio plays through the server's speakers.

### Message Lifecycle

`queued` -> `playing` -> `completed` (with audio file path) or `error` (with error details). Completed statuses expire after 1 hour.

## MCP Server

The MCP server (`mcp/tts-mcp.ts`) is a transparent relay between MCP clients and the FastAPI server. It exposes three tools:

| Tool | Description |
|------|-------------|
| `say` | Queue text for speech synthesis with a specified voice |
| `get_voices` | List all available voices |
| `get_status` | Check status of a speech request by message ID |

### Setup

```bash
cd mcp && npm install
```

### Usage with Claude Code / Claude Desktop

Add to your MCP configuration:

```json
{
  "mcpServers": {
    "tts": {
      "command": "npx",
      "args": ["tsx", "tts-mcp.ts"],
      "cwd": "/path/to/mistral-text-to-spech/mcp"
    }
  }
}
```

The MCP server reads `config.yaml` from the project root to determine the server URL.

## Development

Build tooling follows the standard `bborbe/python-skeleton` layout: a `Makefile` that includes `Makefile.variables` and `Makefile.precommit`. The toolchain is intentionally lean — ruff (format + lint), mypy + pyright (type checking), and pytest (including `tests/architecture/` import-rule tests via pytestarch).

| Command | Description |
|---------|-------------|
| `make format` | Auto-format and auto-fix with ruff |
| `make lint` | Check with ruff (read-only) |
| `make typecheck` | Type-check with mypy + pyright |
| `make check` | Run lint + typecheck |
| `make test` | Run the test suite (incl. architecture tests) |
| `make precommit` | Full gate: `sync format test check` — what CI runs |

### CI

`.github/workflows/ci.yml` runs `make precommit` on push and PR to `main`/`master`. It uses a **macOS Apple-Silicon runner** because `mlx-audio` (MLX/Metal) and `sounddevice` (PortAudio) are macOS/arm64-only and are imported at module load.

## AI-Assisted Development

This project includes a [CLAUDE.md](CLAUDE.md) file with development rules for AI coding assistants.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a full list of changes.

## Resources

- [mlx-audio](https://github.com/Blaizzy/mlx-audio) — MLX-based audio models for Apple Silicon
- [Voxtral](https://mistral.ai/news/voxtral) — Mistral's text-to-speech model
- [Model Context Protocol](https://modelcontextprotocol.io/) — Open protocol for AI tool integration
- [FastAPI](https://fastapi.tiangolo.com/) — Modern Python web framework

## License

This project is a derivative of [florianbuetow/tts-mcp](https://github.com/florianbuetow/tts-mcp), maintained independently by [bborbe/tts-mcp](https://github.com/bborbe/tts-mcp). Licensed under the MIT License (© 2025 Florian Butow, © 2026 Benjamin Borbe). See [LICENSE](LICENSE) for details.
