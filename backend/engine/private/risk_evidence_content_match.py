"""
backend/engine/private/risk_evidence_content_match.py
======================================================
Fail-closed risk-evidence content-digest match primitive for the Private Investment Decision Engine (Phase 15B.8).

Architectural Invariants:
    - Pure domain value object / content-integrity primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Zero UUID generation, filesystem, database, or cache access.
    - Strict concrete-type validation:
      * `type(pit_binding) is RiskEvidencePITBinding`
      * `type(content) is bytes` (rejects subclasses, bytearray, memoryview, str, None)
    - Empty `b""` is valid input: it is hashed normally and matched by its real digest.
      Empty bytes are NOT interpreted as missing, invalid, or insufficient evidence.
    - Computes SHA256 digest of content and compares with:
      `pit_binding.availability_ref.provenance_ref.content_sha256`
      using `hmac.compare_digest` for timing-safe comparison.
    - Digest mismatch fails closed with static ValueError.
    - Preserves `pit_binding` by identity (`is`); does NOT store raw content.
    - Stores only `pit_binding` and `content_length` (exact int).
    - Raw content must NOT appear in dataclass fields, repr, instance __dict__, or any cached attribute.
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Proves only: "the exact bytes supplied during construction matched the declared digest
      of an already PIT-admissible reference."
    - Does NOT claim: source authenticity, durable availability, completeness, sufficiency,
      risk value, score, level, or suitability.
    - Does not modify or wrap MissingRiskEvidence. Missing remains an explicit separate branch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac

from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding


@dataclass(frozen=True)
class RiskEvidenceContentMatch:
    """
    Content-integrity binding proving that caller-supplied bytes matched the declared digest
    of a PIT-admissible risk evidence reference.

    `content` is constructor-only validation material; it is intentionally not a dataclass
    field and is never retained.
    """
    pit_binding: RiskEvidencePITBinding
    content_length: int

    def __init__(self, pit_binding: object, content: object) -> None:
        # Validate pit_binding type first
        if type(pit_binding) is not RiskEvidencePITBinding:
            raise TypeError("pit_binding must be an exact RiskEvidencePITBinding instance")

        # Validate exact content type (empty bytes are valid)
        if type(content) is not bytes:
            raise TypeError("content must be exact bytes")

        # Compute actual digest
        actual_digest = hashlib.sha256(content).hexdigest()

        # Extract expected digest from pit_binding
        expected_digest = pit_binding.availability_ref.provenance_ref.content_sha256

        # Timing-safe comparison
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError("content digest mismatch")

        # Store fields using object.__setattr__ since dataclass is frozen
        object.__setattr__(self, "pit_binding", pit_binding)
        object.__setattr__(self, "content_length", len(content))

        # Note: content is NOT stored anywhere, ensuring it cannot appear in
        # fields, repr, __dict__, or any cached attribute
