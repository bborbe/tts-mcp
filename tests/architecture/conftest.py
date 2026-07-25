"""Architecture test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytestarch import EvaluableArchitecture, get_evaluable_architecture

# Resolve paths relative to this file:
#   tests/architecture/conftest.py -> tests/ -> project root
_TESTS_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _TESTS_DIR.parent
_PACKAGE_DIR = _PROJECT_ROOT / "src"


@pytest.fixture(scope="session")
def evaluable() -> EvaluableArchitecture:
    """Build the evaluable architecture graph over src/.

    src/ is both root and module path so module names resolve exactly as the
    code imports them ('src.tts', 'src.server'). Passing the project root as
    root instead would prefix every module with the checkout's directory name,
    which differs between clones and worktrees.
    """
    return get_evaluable_architecture(str(_PACKAGE_DIR), str(_PACKAGE_DIR))
