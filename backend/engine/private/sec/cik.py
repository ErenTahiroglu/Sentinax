"""
backend/engine/private/sec/cik.py
===================================
Central Index Key (CIK) Validation & Canonical Zero-Padding Utilities.

Core Invariants:
    - CIK is an issuer-level numerical identifier assigned by the SEC.
    - Canonical storage format is a 10-digit zero-padded string (e.g. "0000320193").
    - Numeric string inputs up to 10 digits are accepted and normalized via zfill(10).
    - Inputs > 10 digits or containing non-numeric characters are strictly rejected.
    - Integer conversion is forbidden during storage to avoid silent loss of leading zeros.
"""

from typing import Union


def normalize_cik(cik_input: Union[str, int]) -> str:
    """
    Normalizes and validates a CIK string or integer into a canonical 10-digit zero-padded string.

    Args:
        cik_input: Raw CIK representation (e.g. "320193", "0000320193", 320193).

    Returns:
        Canonical 10-digit zero-padded CIK string (e.g. "0000320193").

    Raises:
        ValueError: If input is non-numeric, empty, or exceeds 10 digits.
    """
    if cik_input is None:
        raise ValueError("CIK cannot be None.")

    if isinstance(cik_input, int):
        if cik_input < 0:
            raise ValueError(f"CIK cannot be negative: {cik_input}")
        raw_str = str(cik_input)
    elif isinstance(cik_input, str):
        raw_str = cik_input.strip()
    else:
        raise ValueError(f"Unsupported CIK input type: {type(cik_input).__name__}")

    if not raw_str:
        raise ValueError("CIK cannot be empty.")

    if not raw_str.isdigit():
        raise ValueError(f"CIK must contain digits only, received: '{raw_str}'")

    if len(raw_str) > 10:
        raise ValueError(f"CIK cannot exceed 10 digits, received length {len(raw_str)}: '{raw_str}'")

    return raw_str.zfill(10)


def format_cik_for_path(cik: Union[str, int]) -> str:
    """
    Formats a CIK for SEC archive directory paths (unpadded numeric string e.g. '320193').
    """
    normalized = normalize_cik(cik)
    return str(int(normalized))
