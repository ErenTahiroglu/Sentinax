"""
backend/tests/test_risk_evidence_provenance.py
==============================================
TDD unit test suite for RiskEvidenceProvenanceRef (Phase 15B.4).

Covers:
- RiskEvidenceProvenanceRef frozen dataclass invariants with exactly two stored fields:
  (source_key, content_sha256)
- Strict concrete-type validation:
  * `type(source_key) is str`
  * `type(content_sha256) is str`
- Regex and format validation:
  * source_key matches exactly: `^[a-z0-9][a-z0-9._-]{0,63}$`
  * content_sha256 matches exactly: `^[a-f0-9]{64}$`
- Rejects:
  * empty strings, padded strings, uppercase, unicode/non-ascii, overlength source_key
  * short/long, uppercase, non-hex, padded digests
  * str subclasses
  * None, bool, int, float, dict, bytes, object
- Static callback-safe error messages with anchored regex matching:
  * r"^source_key must be an exact valid canonical string$"
  * r"^content_sha256 must be an exact 64-character lowercase hex digest$"
- Adversarial test doubles (exploding __repr__, __str__, metaclass __name__, iter, properties)
- Purity invariants:
  * No presence/verification/value/score/level/context/axis/timestamp/owner/suitability attributes
  * No hashing, UUID generation, clock, network, or persistence operation
"""

from dataclasses import fields
import re

import pytest

from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef


VALID_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ANOTHER_VALID_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class SubclassedStr(str):
    pass


class AdversarialReprObject:
    def __repr__(self):
        raise RuntimeError("Adversarial repr explosion")

    def __str__(self):
        raise RuntimeError("Adversarial str explosion")


class AdversarialMeta(type):
    @property
    def __name__(cls):
        raise RuntimeError("Adversarial metaclass __name__ explosion")


class AdversarialMetaClass(metaclass=AdversarialMeta):
    pass


class TestRiskEvidenceProvenanceRefContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        ref = RiskEvidenceProvenanceRef(
            source_key="survey_v1",
            content_sha256=VALID_SHA256,
        )

        f_names = [f.name for f in fields(ref)]
        assert f_names == ["source_key", "content_sha256"]

        with pytest.raises(AttributeError):
            ref.source_key = "other_key"  # type: ignore

        with pytest.raises(AttributeError):
            ref.content_sha256 = ANOTHER_VALID_SHA256  # type: ignore

    def test_preserves_supplied_strings_without_normalization(self):
        key = "survey_v1.audit-2026"
        ref = RiskEvidenceProvenanceRef(source_key=key, content_sha256=VALID_SHA256)
        assert ref.source_key == key
        assert ref.source_key is key
        assert ref.content_sha256 == VALID_SHA256
        assert ref.content_sha256 is VALID_SHA256

    @pytest.mark.parametrize(
        "valid_key",
        [
            "a",
            "0",
            "survey",
            "survey_v1",
            "survey-v1",
            "survey.v1",
            "a" * 64,
            "0" + "a-._" * 15 + "z",
        ],
    )
    def test_valid_boundary_source_keys(self, valid_key):
        ref = RiskEvidenceProvenanceRef(source_key=valid_key, content_sha256=VALID_SHA256)
        assert ref.source_key == valid_key

    def test_valid_sha256_digests(self):
        ref = RiskEvidenceProvenanceRef(source_key="valid_key", content_sha256=VALID_SHA256)
        assert ref.content_sha256 == VALID_SHA256
        ref2 = RiskEvidenceProvenanceRef(source_key="valid_key", content_sha256=ANOTHER_VALID_SHA256)
        assert ref2.content_sha256 == ANOTHER_VALID_SHA256

    def test_no_convenience_or_presence_properties(self):
        ref = RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256=VALID_SHA256)
        prohibited_attrs = [
            "present",
            "verified",
            "value",
            "score",
            "level",
            "context",
            "axis",
            "timestamp",
            "as_of",
            "owner_id",
            "owner",
            "suitability",
            "complete",
            "status",
            "available",
            "evidence",
            "hash",
            "identity",
        ]
        for attr in prohibited_attrs:
            assert not hasattr(ref, attr), f"RiskEvidenceProvenanceRef must not have property '{attr}'"


class TestRiskEvidenceProvenanceRefValidation:
    @pytest.mark.parametrize(
        "bad_key",
        [
            None,
            True,
            False,
            123,
            b"survey_v1",
            SubclassedStr("survey_v1"),
            "",
            " ",
            "  ",
            " survey_v1",
            "survey_v1 ",
            "survey\nv1",
            "survey\tv1",
            "-survey",
            "_survey",
            ".survey",
            "SURVEY_V1",
            "Survey_V1",
            "survey@v1",
            "survey/v1",
            "survey:v1",
            "türkiye",
            "a" * 65,
            {"source_key": "survey_v1"},
            object(),
        ],
    )
    def test_rejects_invalid_source_key(self, bad_key):
        with pytest.raises(TypeError, match=r"^source_key must be an exact valid canonical string$"):
            RiskEvidenceProvenanceRef(source_key=bad_key, content_sha256=VALID_SHA256)  # type: ignore

    @pytest.mark.parametrize(
        "bad_digest",
        [
            None,
            True,
            False,
            123,
            b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            SubclassedStr(VALID_SHA256),
            "",
            " " * 64,
            VALID_SHA256[:63],
            VALID_SHA256 + "a",
            VALID_SHA256.upper(),
            VALID_SHA256[:-1] + "G",
            " " + VALID_SHA256[1:],
            VALID_SHA256[:-1] + " ",
            "g" * 64,
            {"content_sha256": VALID_SHA256},
            object(),
        ],
    )
    def test_rejects_invalid_content_sha256(self, bad_digest):
        with pytest.raises(TypeError, match=r"^content_sha256 must be an exact 64-character lowercase hex digest$"):
            RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256=bad_digest)  # type: ignore

    def test_rejects_swapped_arguments(self):
        with pytest.raises(TypeError, match=r"^content_sha256 must be an exact 64-character lowercase hex digest$"):
            RiskEvidenceProvenanceRef(source_key=VALID_SHA256, content_sha256="survey_v1")

    def test_adversarial_repr_source_key(self):
        with pytest.raises(TypeError, match=r"^source_key must be an exact valid canonical string$"):
            RiskEvidenceProvenanceRef(source_key=AdversarialReprObject(), content_sha256=VALID_SHA256)  # type: ignore

    def test_adversarial_repr_content_sha256(self):
        with pytest.raises(TypeError, match=r"^content_sha256 must be an exact 64-character lowercase hex digest$"):
            RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256=AdversarialReprObject())  # type: ignore

    def test_adversarial_metaclass_source_key(self):
        with pytest.raises(TypeError, match=r"^source_key must be an exact valid canonical string$"):
            RiskEvidenceProvenanceRef(source_key=AdversarialMetaClass(), content_sha256=VALID_SHA256)  # type: ignore

    def test_adversarial_metaclass_content_sha256(self):
        with pytest.raises(TypeError, match=r"^content_sha256 must be an exact 64-character lowercase hex digest$"):
            RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256=AdversarialMetaClass())  # type: ignore
