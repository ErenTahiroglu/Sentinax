"""
backend/engine/private/portfolio/normalization.py
=================================================
Canonical Cross-Language External Identity Normalization Authority.

Exact Contract:
    1. External Source:
       - Strips ASCII U+0020 SPACE characters from boundaries ONLY (btrim(s, ' ')).
       - Case-normalizes ASCII letters a-z -> A-Z using explicit translation table.
       - Preserves every other character (including tabs, newlines, and non-ASCII Unicode characters) exactly.
    2. External Reference:
       - Strips ASCII U+0020 SPACE characters from boundaries ONLY (btrim(s, ' ')).
       - Case-sensitive (preserves case).
"""

from typing import Optional

ASCII_SOURCE_TRANSLATION = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)


def normalize_external_source(source: Optional[str]) -> Optional[str]:
    """
    Applies canonical normalization to external_source:
    - None -> None
    - Strips leading/trailing ASCII space ' ' only.
    - Converts ASCII lowercase a-z to uppercase A-Z.
    - Preserves non-ASCII characters and non-space whitespace (tabs, newlines).
    """
    if source is None:
        return None
    return source.strip(" ").translate(ASCII_SOURCE_TRANSLATION)


def normalize_external_reference(reference: Optional[str]) -> Optional[str]:
    """
    Applies canonical normalization to external_reference:
    - None -> None
    - Strips leading/trailing ASCII space ' ' only.
    - Preserves case and all other characters.
    """
    if reference is None:
        return None
    return reference.strip(" ")
