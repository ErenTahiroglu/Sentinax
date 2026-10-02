"""
backend/engine/private/game_changer_extraction.py
=================================================
Safe structured-assessment extraction boundary for Game Changer events (Phase 23D1).

Phase 23B accepts an explicit caller-supplied assessment. This module defines the boundary through which a HUMAN or a MODEL
structured extraction may become a canonical Phase 23B assessment, and nothing more. Chain: 23A event evidence -> 23D1 extraction
-> 23B assessment -> 23C1 revision resolution -> 23C2 gate. There is no direct extraction-to-quarantine or extraction-to-portfolio
path: this module only materializes a closed Phase 23B assessment.

Authority separation: the extraction retains the exact Phase 23A `GameChangerEventPITBinding` by identity and may supply ONLY the
Phase 23B categorical axes (materiality, urgency, thesis_impact, impact_dimensions, materiality_basis) plus provenance. It has no field
for, and can never rewrite, the source key, event key, tier, scope, instrument ids, event type, effective / published / observed times,
content hash, revision lineage or PIT context; those stay Phase 23A authority reachable only through the binding. A model never infers
canonical security identity or source timestamps from prose.

Provenance (all explicit, nothing is hashed or verified here):

    extraction_mode          HUMAN or MODEL: audit provenance only; it changes no validation, value or gate semantics
    methodology_key          assessment rubric / version identity (same grammar as Phase 23B)
    extractor_key            concrete extractor implementation identity (strict lowercase identifier, no vendor hardcoded)
    extractor_revision       exact int >= 1, implementation revision (bool, float, Decimal and strings rejected), distinct from methodology
    source_content_sha256    must EQUAL the Phase 23A event content_sha256: the extraction declares the same source artifact (reference
                             consistency only, it does not prove authenticity or that raw bytes were verified)
    extraction_output_sha256 opaque reference to the upstream structured extraction artifact; no relation to the source hash is implied
                             and it becomes the Phase 23B assessment_provenance_sha256

System-availability rule: observed_at <= extracted_at <= knowledge_cutoff, by exact UTC instant, equality allowed, no tolerance, no
clock call (the caller supplies extracted_at). A structured extraction cannot exist before Sentinax observed the event and cannot be
injected into an earlier decision cutoff afterwards. Consequently a SOURCE_AS_OF event that was publicly knowable before the cutoff
but observed after it can never carry a Sentinax extraction at that cutoff (market could know it is not the same as Sentinax had
already produced its assessment). No retrospective model extraction is authorized here; a future replay methodology would need its own
explicit authority.

The categorical fields are validated by the closed Phase 23B contract itself (the Phase 23B builder is the single semantic authority;
OTHER exclusivity and every enum / tuple rule come from it), both on construction and on materialization.

Exclusions: there is no model runtime or SDK (no model runtime: the contract only, model execution lives outside this pure boundary), no
prompt storage, no confidence or probability (no confidence score: uncertainty is the explicit UNCERTAIN thesis impact), no raw text
retention (no raw text: headline, body, HTML, PDF, JSON or bytes belong to provider / ingestion infrastructure), no provider or network
access (no provider here: KAP / MKK access is Phase 23D2), no direct quarantine or new-capital decision, no portfolio, allocation,
rebalance, order or execution.

Architectural Invariants:
    - Pure domain module: standard library plus the closed Phase 23A binding and Phase 23B assessment surface. No clock, hashing,
      network, database, randomness or persistence; exact concrete types (subclasses and raw strings rejected).
    - `GameChangerStructuredAssessmentExtraction` re-validates its complete contract on direct construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from backend.engine.private.game_changer_assessment import (
    GameChangerImpactDimension,
    GameChangerMateriality,
    GameChangerMaterialityAssessment,
    GameChangerMaterialityBasis,
    GameChangerThesisImpact,
    GameChangerUrgency,
    build_game_changer_materiality_assessment,
)
from backend.engine.private.game_changer_event import GameChangerEventPITBinding

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_BINDING = "binding must be an exact GameChangerEventPITBinding instance"
_ERR_MODE = "extraction_mode must be an exact GameChangerExtractionMode instance"
_ERR_EXTRACTOR_KEY_TYPE = "extractor_key must be an exact str instance"
_ERR_EXTRACTOR_KEY_VALUE = "extractor_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_REVISION_TYPE = "extractor_revision must be an exact int instance"
_ERR_REVISION_VALUE = "extractor_revision must be at least 1"
_ERR_DATETIME = "extracted_at must be an exact timezone-aware datetime instance"
_ERR_WINDOW = "extracted_at must satisfy observed_at <= extracted_at <= knowledge_cutoff"
_ERR_SOURCE_HASH_TYPE = "source_content_sha256 must be an exact str instance"
_ERR_SOURCE_HASH_VALUE = "source_content_sha256 must be exactly 64 lowercase hexadecimal characters"
_ERR_SOURCE_HASH_MATCH = "source_content_sha256 must equal the bound event content_sha256"
_ERR_EXTRACTION_TYPE = "extraction must be an exact GameChangerStructuredAssessmentExtraction instance"


class GameChangerExtractionMode(Enum):
    """Audit provenance only: human- or model-produced structured extraction; changes no validation or semantics."""
    HUMAN = "human"
    MODEL = "model"


def _check_aware(value: object) -> None:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(_ERR_DATETIME)
    try:
        if value.utcoffset() is None:
            raise TypeError(_ERR_DATETIME)
        value.astimezone(timezone.utc)
    except TypeError:
        raise TypeError(_ERR_DATETIME) from None
    except Exception:
        raise TypeError(_ERR_DATETIME) from None


@dataclass(frozen=True)
class GameChangerStructuredAssessmentExtraction:
    """Explicit structured extraction of Phase 23B axes for one PIT-bound event; carries provenance references only."""
    binding: GameChangerEventPITBinding
    extraction_mode: GameChangerExtractionMode
    materiality: GameChangerMateriality
    urgency: GameChangerUrgency
    thesis_impact: GameChangerThesisImpact
    impact_dimensions: tuple[GameChangerImpactDimension, ...]
    materiality_basis: GameChangerMaterialityBasis
    methodology_key: str
    extractor_key: str
    extractor_revision: int
    extracted_at: datetime
    source_content_sha256: str
    extraction_output_sha256: str

    def __post_init__(self) -> None:
        if type(self.binding) is not GameChangerEventPITBinding:
            raise TypeError(_ERR_BINDING)
        if type(self.extraction_mode) is not GameChangerExtractionMode:
            raise TypeError(_ERR_MODE)
        if type(self.extractor_key) is not str:
            raise TypeError(_ERR_EXTRACTOR_KEY_TYPE)
        if _IDENTIFIER.fullmatch(self.extractor_key) is None:
            raise ValueError(_ERR_EXTRACTOR_KEY_VALUE)
        if type(self.extractor_revision) is not int:
            raise TypeError(_ERR_REVISION_TYPE)
        if self.extractor_revision < 1:
            raise ValueError(_ERR_REVISION_VALUE)
        _check_aware(self.extracted_at)
        if type(self.source_content_sha256) is not str:
            raise TypeError(_ERR_SOURCE_HASH_TYPE)
        if _SHA256.fullmatch(self.source_content_sha256) is None:
            raise ValueError(_ERR_SOURCE_HASH_VALUE)
        if self.source_content_sha256 != self.binding.event.content_sha256:
            raise ValueError(_ERR_SOURCE_HASH_MATCH)
        extracted = self.extracted_at.astimezone(timezone.utc)
        if not self.binding.event.observed_at.astimezone(timezone.utc) <= extracted <= self.binding.context.knowledge_cutoff_utc:
            raise ValueError(_ERR_WINDOW)
        build_game_changer_materiality_assessment(  # closed Phase 23B contract is the single semantic authority
            binding=self.binding,
            materiality=self.materiality,
            urgency=self.urgency,
            thesis_impact=self.thesis_impact,
            impact_dimensions=self.impact_dimensions,
            materiality_basis=self.materiality_basis,
            methodology_key=self.methodology_key,
            assessment_provenance_sha256=self.extraction_output_sha256,
        )


def build_game_changer_structured_assessment_extraction(
    *,
    binding: GameChangerEventPITBinding,
    extraction_mode: GameChangerExtractionMode,
    materiality: GameChangerMateriality,
    urgency: GameChangerUrgency,
    thesis_impact: GameChangerThesisImpact,
    impact_dimensions: tuple[GameChangerImpactDimension, ...],
    materiality_basis: GameChangerMaterialityBasis,
    methodology_key: str,
    extractor_key: str,
    extractor_revision: int,
    extracted_at: datetime,
    source_content_sha256: str,
    extraction_output_sha256: str,
) -> GameChangerStructuredAssessmentExtraction:
    """Validate an explicit structured extraction; every input is caller authority and nothing is defaulted."""
    return GameChangerStructuredAssessmentExtraction(
        binding=binding,
        extraction_mode=extraction_mode,
        materiality=materiality,
        urgency=urgency,
        thesis_impact=thesis_impact,
        impact_dimensions=impact_dimensions,
        materiality_basis=materiality_basis,
        methodology_key=methodology_key,
        extractor_key=extractor_key,
        extractor_revision=extractor_revision,
        extracted_at=extracted_at,
        source_content_sha256=source_content_sha256,
        extraction_output_sha256=extraction_output_sha256,
    )


def materialize_game_changer_materiality_assessment(
    *,
    extraction: GameChangerStructuredAssessmentExtraction,
) -> GameChangerMaterialityAssessment:
    """Materialize the closed Phase 23B assessment from a validated extraction; the binding is retained by identity."""
    if type(extraction) is not GameChangerStructuredAssessmentExtraction:
        raise TypeError(_ERR_EXTRACTION_TYPE)
    return build_game_changer_materiality_assessment(
        binding=extraction.binding,
        materiality=extraction.materiality,
        urgency=extraction.urgency,
        thesis_impact=extraction.thesis_impact,
        impact_dimensions=extraction.impact_dimensions,
        materiality_basis=extraction.materiality_basis,
        methodology_key=extraction.methodology_key,
        assessment_provenance_sha256=extraction.extraction_output_sha256,
    )
