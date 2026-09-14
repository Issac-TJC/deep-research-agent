"""Deterministic parsing for the single free-form research input."""

import re

URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"'，。；：！？（）【】《》]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """Extract pasted URLs without asking users to route them into a separate field."""
    trailing = ".,;:!?)]}，。；：！？）】》"
    return [match.group(0).rstrip(trailing) for match in URL_PATTERN.finditer(text)]
