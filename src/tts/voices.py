"""Voice selection policy: resolve a requested voice against an engine's allowlist."""

import logging

logger = logging.getLogger(__name__)


def resolve_voice(
    requested: str | None,
    engine: str,
    engine_default: str,
    allowed: tuple[str, ...],
) -> str:
    """Resolve the voice a request will actually be synthesised with.

    A request that names no voice takes its engine's own default. An empty
    ``allowed`` means that engine declares no allowlist, so every voice it
    offers stays reachable — an omission is not a denial. A named voice outside
    a non-empty allowlist is substituted with the allowlist's first entry.

    That substitution is a deliberate, scoped carve-out from this repo's
    "fail fast — never swallow errors" / "never silently fall back to a
    default" rule (CLAUDE.md). It is not silent: every substitution is logged
    at error level. Everything else on the /say path still fails fast — an
    unknown engine, an unavailable engine, and a voice belonging to no engine
    all keep raising 400.

    Args:
        requested: Voice named by the caller, or None when the request names none.
        engine: Engine the request targets, for the log line.
        engine_default: That engine's own default voice — never the global one,
            which is not a valid voice on every engine.
        allowed: Voices this engine permits; empty means unrestricted.

    Returns:
        The voice to synthesise with, guaranteed to be in ``allowed`` when that
        allowlist is non-empty.
    """
    voice = requested if requested else engine_default
    if not allowed or voice in allowed:
        return voice
    logger.error(
        "Voice '%s' is not allowed on engine '%s'; substituting '%s'. Allowed: %s",
        voice,
        engine,
        allowed[0],
        ", ".join(allowed),
    )
    return allowed[0]
