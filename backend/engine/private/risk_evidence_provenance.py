"""
backend/engine/private/risk_evidence_provenance.py
==================================================
Risk-evidence provenance reference foundation for the Private Investment Decision Engine (Phase 15B.4).

Architectural Invariants:
    - Pure domain value object / provenance reference primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Zero UUID generation, hashing, raw-byte storage, filesystem, or database access.
    - Strict concrete-type validation:
      * `type(source_key) is str`
      * `type(content_sha256) is str`
    - Exact regex validation:
      * source_key matches `^[a-z0-9][a-z0-9._-]{0,63}$`
      * content_sha256 matches `^[a-f0-9]{64}$`
    - Preserves caller-supplied strings without normalization.
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Does NOT claim that evidence exists, is present, complete, authentic, sufficient, verified,
      PIT-eligible, or owner-authorized.
    - Carries no context, axis, value, score, level, status, timestamps, owner_id, or suitability result.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

_SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class RiskEvidenceProvenanceRef:
    """
    Opaque source/content provenance reference for risk evidence.
    """
    source_key: str
    content_sha256: str

    def __post_init__(self) -> None:
        if type(self.source_key) is not str or not _SOURCE_KEY_RE.match(self.source_key):
            raise TypeError("source_key must be an exact valid canonical string")

        if type(self.content_sha256) is not str or not _SHA256_RE.match(self.content_sha256):
            raise TypeError("content_sha256 must be an exact 64-character lowercase hex digest")
