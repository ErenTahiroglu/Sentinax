"""
backend/tests/learning_support.py
=================================
Shared deterministic builders for the Phase 28B-0 learning evidence contract tests. Test-only: explicit instants, no ambient clock, no network.
Not a test module (pytest does not collect it).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

T0 = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
UUID_1 = UUID("00000000-0000-4000-8000-000000000001")
UUID_2 = UUID("00000000-0000-4000-8000-000000000002")
UUID_3 = UUID("00000000-0000-4000-8000-000000000003")


def plus(seconds: int = 0) -> datetime:
    return T0 + timedelta(seconds=seconds)


def provenance_kwargs(**over):
    base = dict(economic_date=date(2026, 10, 9), retrieved_at=plus(60), capture_attempted_at=plus(55))
    base.update(over)
    return base
