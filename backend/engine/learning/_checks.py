"""
backend/engine/learning/_checks.py
==================================
Strict, side-effect-free validators and canonical serialization shared by the learning evidence contracts. Exact types only (no subclasses, no bool-as-int, no raw strings
for enums). No clock, I/O or entropy.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional, Tuple, Type, TypeVar

_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_MAX_TOKEN = 128
_MAX_TEXT = 2000

E = TypeVar("E", bound=Enum)


def require_exact_type(name: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise TypeError(f"{name} must be exactly {expected.__name__}, got {type(value).__name__}: {value!r}")


def require_enum(name: str, value: object, enum_cls: Type[E]) -> E:
    if not isinstance(value, enum_cls) or type(value) is not enum_cls:
        raise TypeError(f"{name} must be a {enum_cls.__name__} member, got {type(value).__name__}: {value!r}")
    return value


def require_exact_bool(name: str, value: object) -> bool:
    require_exact_type(name, value, bool)
    return value  # type: ignore[return-value]


def require_aware_datetime(name: str, value: object) -> datetime:
    require_exact_type(name, value, datetime)
    assert isinstance(value, datetime)
    if value.tzinfo is None:
        raise TypeError(f"{name} must be timezone-aware, got naive: {value!r}")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise TypeError(f"{name} has an invalid tzinfo: {value!r}") from exc
    if type(offset) is not timedelta:
        raise TypeError(f"{name} must have a valid UTC offset: {value!r}")
    return value


def require_exact_date(name: str, value: object) -> date:
    require_exact_type(name, value, date)
    return value  # type: ignore[return-value]


def require_sha256(name: str, value: object) -> str:
    require_exact_type(name, value, str)
    assert isinstance(value, str)
    if not _SHA256.match(value):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def require_canonical_token(name: str, value: object) -> str:
    require_exact_type(name, value, str)
    assert isinstance(value, str)
    if len(value) > _MAX_TOKEN or not _TOKEN.match(value):
        raise ValueError(f"{name} must be a canonical lowercase token (letters/digits with single . _ - separators), got {value!r}")
    return value


def require_nonblank_text(name: str, value: object) -> str:
    require_exact_type(name, value, str)
    assert isinstance(value, str)
    if not value or value != value.strip() or len(value) > _MAX_TEXT or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be non-empty, trimmed, single-line text without control characters")
    return value


def require_semver(name: str, value: object) -> Tuple[int, int, int]:
    require_exact_type(name, value, str)
    assert isinstance(value, str)
    m = _SEMVER.match(value)
    if not m:
        raise ValueError(f"{name} must be MAJOR.MINOR.PATCH, got {value!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def require_positive_finite_decimal(name: str, value: object) -> Decimal:
    require_exact_type(name, value, Decimal)
    assert isinstance(value, Decimal)
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite Decimal > 0, got {value!r}")
    return value


def require_tuple_of(name: str, value: object, item_type: type, *, unique: bool = True) -> tuple:
    require_exact_type(name, value, tuple)
    assert isinstance(value, tuple)
    for item in value:
        if type(item) is not item_type:
            raise TypeError(f"{name} items must be exactly {item_type.__name__}, got {type(item).__name__}")
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{name} contains duplicate entries")
    return value


def optional(check, name: str, value: object) -> Optional[Any]:
    return None if value is None else check(name, value)


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return utc(value).isoformat()


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
