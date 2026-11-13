"""Minimal stub for MLX's ``mlx.core``, which is macOS/arm64-only.

Only the surface this project calls is declared, so the code type-checks on a
platform where MLX is not installed.
"""

class _Random:
    @staticmethod
    def seed(seed: int) -> None: ...

random: _Random
