# Development Rules for mistral-text-to-spech

This file provides guidance to AI agents and AI-assisted development tools when working with this project. This includes Claude Code, Cursor IDE, GitHub Copilot, Windsurf, and any other AI coding assistants.

## General Coding Principles
- **Fail fast — never swallow errors.** Always propagate errors and exit with code 1 immediately. No silent fallbacks, no `|| true`, no ignored return codes.
- **Never assume any default values anywhere.** Check for required values explicitly and exit 1 if something is missing. Default values mask underlying issues and make them hard to debug.
- **Never suppress checks with annotations.** Fix the underlying issue instead. No `# noqa`, `# type: ignore`, `# nosec`, `@pytest.mark.filterwarnings`, or any other mechanism that silences a checker.
- Always be explicit about values, paths, and configurations
- If a value is not provided, raise an error — never silently fall back to a default

## Git Commit Guidelines

**IMPORTANT:** When creating git commits in this repository:
- **NEVER include AI attribution in commit messages**
- **NEVER add "Generated with [AI tool name]" or similar phrases**
- **NEVER add "Co-Authored-By: [AI name]" or similar attribution**
- **NEVER run `git add -A` or `git add .` - always stage files explicitly**
- Keep commit messages professional and focused on the changes made
- Commit messages should describe what changed and why, without mentioning AI assistance
- **ALWAYS run `git push` after creating a commit to push changes to the remote repository**
- **NEVER use `git -C <path>`** — always run git commands from the project root directory

## Testing
- After **every change** to the code, the tests must be executed (`make test`)
- Always verify the program runs correctly with `make chat` after modifications

## Python Execution Rules
- Python code must be executed **only** via `uv run ...`
  - Example: `uv run src/main.py`
  - **Never** use: `python src/main.py` or `python3 src/main.py`
- The virtual environment must be created and updated **only** via `uv sync`
  - **Never** use: `pip install`, `python -m pip`, or `uv pip`
- All dependencies must be managed through `uv` and declared in `pyproject.toml`

## Makefile Conventions
Build tooling follows the standard `bborbe/python-skeleton` layout: a top-level `Makefile` that `include`s `Makefile.variables` and `Makefile.precommit`. All Python execution uses `uv run`, never `python` directly.

- Use `make sync` (alias `make install`) to sync dependencies (`uv sync --all-extras`)
- Use `make run` to start the FastAPI TTS server (foreground)
- Use `make chat` to run the interactive CLI
- Use `make download` to download a Voxtral model into `data/models/`
- Use `make format` to auto-format + auto-fix with ruff
- Use `make lint` to check with ruff (read-only)
- Use `make typecheck` to run mypy + pyright
- Use `make check` to run lint + typecheck
- Use `make test` to run pytest (includes `tests/architecture/`)
- Use `make precommit` to run the full gate (`sync format test check`) — this is what CI runs
- Use `make clean-local` to remove the venv and caches

Toolchain is intentionally lean (ruff + mypy + pyright + pytest, plus pytestarch architecture tests). No semgrep / bandit / deptry / codespell / pip-audit. CI (`.github/workflows/ci.yml`) runs `make precommit` on a macOS Apple-Silicon runner (mlx-audio + sounddevice are macOS/arm64-only).

## Project Structure
- All source code lives in `src/`
- Test scripts and utilities go in `scripts/`
- **Input data is organized**: `data/input/`
- **Output data is organized**: `data/output/`
- **Never create Python files in the project root directory**
  - Wrong: `./test.py`, `./helper.py`
  - Correct: `./src/helper.py`, `./scripts/test.py`

## Error Handling
- Fail fast — stop immediately on the first error, never continue past failures
- Never catch and silently ignore exceptions
- Raise exceptions with clear messages for missing or invalid data
- Exit with code 1 if any operation fails, 0 if all succeeded

## Optimization
- **Skip processing if output already exists** - Don't reprocess unnecessarily
- Check if output file exists before starting expensive operations
- Track skipped items separately in summary reports
- Allow users to force reprocessing by deleting output files
