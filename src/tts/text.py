"""Text cleaning and punctuation simplification for TTS input."""

import re


def clean_text(text: str) -> str:
    """Clean text by stripping and collapsing whitespace.

    Args:
        text: Raw input text.

    Returns:
        Cleaned text. Empty string if input was only whitespace.
    """
    text = text.strip()
    text = re.sub(r"\t", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = text.strip()
    return text


def simplify_punctuation(text: str) -> str:
    """Simplify punctuation by removing commas and replacing other marks with periods.

    Handles ASCII punctuation, smart quotes, em/en dashes, and ellipsis.
    CJK and other script-specific punctuation is passed through unchanged.

    Args:
        text: Input text (should be pre-cleaned with clean_text).

    Returns:
        Text with simplified punctuation.
    """
    text = text.replace(",", "")
    text = text.replace("，", "")

    text = text.replace("...", ".")
    text = text.replace("--", ".")

    for ch in "!?;:()[]{}\"'`—–…“”‘’":
        text = text.replace(ch, ".")

    text = re.sub(r"\.\s*(?:\.\s*)+", ".", text)
    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\.(?=[^\s.\d])", ". ", text)
    text = re.sub(r"^[\s.]+", "", text)
    text = text.rstrip()

    return text
