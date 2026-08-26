"""
backend/engine/private/sec/concept_resolver.py
================================================
Deterministic SEC Raw XBRL Fact to Canonical Economic Concept Candidate Resolver.

Core Invariants:
    - Pure deterministic rule-based resolution; NO LLM, NO fuzzy string matching.
    - Input: SECRawFactRecord instances (entity-level CIK).
    - Output: SECCanonicalFactCandidate instances.
    - Numerical values (Decimal) are NEVER modified, scaled, sign-flipped, or converted.
    - Expected PeriodType and UnitClass are strictly enforced.
    - Preserves all candidates across multiple accessions, forms, and amendments without winner selection (Phase 8B.2 scope).
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from backend.engine.private.sec.concepts import (
    CanonicalSECConceptDefinition,
    ConceptVariant,
    FormRole,
    MatchStrength,
    PeriodType,
    SECConceptMatchStatus,
    UnitClass,
    VerificationStatus,
    get_initial_canonical_concept_definitions,
)
from backend.engine.private.sec.models import (
    SECCanonicalFactCandidate,
    SECFilingRecord,
    SECRawFactRecord,
)

logger = logging.getLogger(__name__)

# Known standard currency codes and representations
STANDARD_CURRENCIES = {
    "USD", "EUR", "TRY", "GBP", "CAD", "JPY", "CHF", "AUD", "CNY", "SEK",
    "NOK", "DKK", "BRL", "INR", "KRW", "MXN", "SGD", "HKD", "NZD", "ZAR",
    "RUB", "PLN", "ILS", "THB", "IDR", "MYR", "PHP", "TWD", "CLP", "COP",
    "PEN", "ARS", "CZK", "HUF", "RON", "BGN", "HRK", "RSD", "KZT", "AED",
    "SAR", "QAR", "KWD", "BHD", "OMR", "EGP", "NGN", "KES", "GHS", "VND",
}


def classify_form_role(form: Optional[str]) -> Tuple[FormRole, bool]:
    """
    Classifies filing form into a deterministic FormRole without selecting filing winners.
    """
    if not form or not isinstance(form, str):
        return FormRole.OTHER, False

    f_clean = form.strip().upper()
    is_amendment = f_clean.endswith("/A")

    if f_clean in ("10-K", "10-K/A"):
        return FormRole.AMENDMENT_ANNUAL if is_amendment else FormRole.PRIMARY_ANNUAL, is_amendment
    if f_clean in ("10-Q", "10-Q/A"):
        return FormRole.AMENDMENT_QUARTERLY if is_amendment else FormRole.PRIMARY_QUARTERLY, is_amendment
    if f_clean in ("20-F", "20-F/A", "40-F", "40-F/A"):
        return FormRole.FPI_AMENDMENT_ANNUAL if is_amendment else FormRole.FPI_ANNUAL, is_amendment
    if f_clean == "6-K":
        return FormRole.FPI_INTERIM_OR_EVENT, is_amendment
    if f_clean == "8-K":
        return FormRole.EVENT_FILING, is_amendment

    return FormRole.OTHER, is_amendment


def validate_unit_compatibility(expected_unit_class: UnitClass, unit: Optional[str]) -> bool:
    """
    Validates whether raw unit string matches expected economic unit class.
    """
    if not unit or not isinstance(unit, str):
        return False

    u_clean = unit.strip()
    u_lower = u_clean.lower()

    if expected_unit_class == UnitClass.MONETARY:
        # Must be a currency code; reject shares, pure, ratios, per-share
        if u_clean.upper() in STANDARD_CURRENCIES:
            return True
        if u_lower in ("shares", "share", "pure", "ratio") or "/" in u_clean or "-per-" in u_lower:
            return False
        # If unknown 3-letter uppercase code, accept as candidate ISO currency
        if len(u_clean) == 3 and u_clean.isupper():
            return True
        return False

    if expected_unit_class == UnitClass.SHARES:
        # Must be shares
        if u_lower in ("shares", "share", "number"):
            return True
        return False

    if expected_unit_class == UnitClass.MONETARY_PER_SHARE:
        # Must have monetary numerator and shares denominator or pure/ratio
        if "/" in u_clean or "-per-" in u_lower or "pershare" in u_lower:
            return True
        if u_lower in ("pure", "ratio"):
            return True
        return False

    if expected_unit_class == UnitClass.PURE:
        if u_lower in ("pure", "ratio", "percentage", "rate", "decimal"):
            return True
        return False

    return False


class SECConceptResolver:
    """
    Deterministic resolver mapping raw SEC XBRL facts to canonical economic concept candidates.
    """

    def __init__(
        self,
        definitions: Optional[List[CanonicalSECConceptDefinition]] = None,
    ) -> None:
        self.definitions = definitions or get_initial_canonical_concept_definitions()
        self._lookup: Dict[Tuple[str, str], List[Tuple[CanonicalSECConceptDefinition, ConceptVariant]]] = {}
        self._build_index()

    def _build_index(self) -> None:
        for defn in self.definitions:
            if not defn.active:
                continue
            for variant in defn.variants:
                if not variant.active:
                    continue
                key = (variant.taxonomy.strip().lower(), variant.tag.strip().lower())
                if key not in self._lookup:
                    self._lookup[key] = []
                self._lookup[key].append((defn, variant))

    def resolve_raw_fact(
        self,
        fact: SECRawFactRecord,
        filings_by_accession: Optional[Dict[str, SECFilingRecord]] = None,
    ) -> Tuple[Optional[SECCanonicalFactCandidate], SECConceptMatchStatus, List[str]]:
        """
        Resolves a single SECRawFactRecord into an SECCanonicalFactCandidate.
        Enforces strict PeriodType, UnitClass, and taxonomy verification checks.
        """
        key = (fact.taxonomy.strip().lower(), fact.concept.strip().lower())
        matches = self._lookup.get(key)

        if not matches:
            return None, SECConceptMatchStatus.NO_MATCH, [f"Tag '{fact.taxonomy}:{fact.concept}' is not registered in canonical concept taxonomy."]

        if len(matches) > 1:
            return None, SECConceptMatchStatus.AMBIGUOUS, [f"Tag '{fact.taxonomy}:{fact.concept}' matches multiple canonical concepts ambiguously."]

        definition, variant = matches[0]

        # 1. Verification status check
        if variant.verification_status != VerificationStatus.VERIFIED_OFFICIAL or not variant.active:
            return None, SECConceptMatchStatus.UNVERIFIED_VARIANT, [f"Variant '{fact.taxonomy}:{fact.concept}' is unverified or inactive."]

        # 2. PeriodType validation
        if fact.period_type != definition.expected_period_type:
            return None, SECConceptMatchStatus.INVALID_PERIOD_TYPE, [
                f"Period type mismatch for '{definition.canonical_concept}': expected {definition.expected_period_type.value}, got {fact.period_type.value}."
            ]

        # 3. UnitClass validation
        if not validate_unit_compatibility(definition.expected_unit_class, fact.unit):
            return None, SECConceptMatchStatus.INVALID_UNIT, [
                f"Unit mismatch for '{definition.canonical_concept}': expected unit class {definition.expected_unit_class.value}, got '{fact.unit}'."
            ]

        # 4. Lineage status resolution
        filing_id = fact.filing_id
        lineage_status = "RESOLVED" if filing_id else "UNRESOLVED"

        if filings_by_accession is not None:
            if fact.accession_number:
                accn_clean = fact.accession_number.strip()
                filing = filings_by_accession.get(accn_clean)
                if filing:
                    filing_id = filing.id
                    lineage_status = "RESOLVED"
                else:
                    filing_id = None
                    lineage_status = "UNRESOLVED_FILING"
            else:
                filing_id = None
                lineage_status = "UNRESOLVED_ACCESSION"
        else:
            if not fact.accession_number:
                lineage_status = "UNRESOLVED_ACCESSION"

        # 5. Form role classification
        form_role, is_amendment = classify_form_role(fact.form)

        # 6. Confidence Level
        confidence = "HIGH" if variant.match_strength == MatchStrength.EXACT else "MEDIUM"

        candidate = SECCanonicalFactCandidate(
            raw_fact_id=fact.id,
            cik=fact.cik,
            canonical_concept=definition.canonical_concept,
            taxonomy=fact.taxonomy,
            source_concept=fact.concept,
            match_strength=variant.match_strength.value,
            variant_priority=variant.priority,
            value=fact.value,
            unit=fact.unit,
            period_type=fact.period_type,
            start_date=fact.start_date,
            end_date=fact.end_date,
            accession_number=fact.accession_number,
            form=fact.form,
            form_role=form_role.value,
            is_amendment=is_amendment,
            fiscal_year=fact.fiscal_year,
            fiscal_period=fact.fiscal_period,
            filed_date=fact.filed_date,
            frame=fact.frame,
            snapshot_id=fact.snapshot_id,
            filing_id=filing_id,
            lineage_status=lineage_status,
            unit_match_status="VALID",
            period_match_status="VALID",
            confidence_level=confidence,
            diagnostics=[],
        )

        return candidate, SECConceptMatchStatus.MATCHED, []

    def resolve_facts(
        self,
        facts: List[SECRawFactRecord],
        filings: Optional[List[SECFilingRecord]] = None,
    ) -> List[SECCanonicalFactCandidate]:
        """
        Resolves a list of SECRawFactRecord instances into SECCanonicalFactCandidate instances.
        Preserves all valid candidates without dropping comparative or amended filings.
        """
        filing_map: Optional[Dict[str, SECFilingRecord]] = None
        if filings is not None:
            filing_map = {f.accession_number.strip(): f for f in filings if f.accession_number}

        candidates: List[SECCanonicalFactCandidate] = []
        for fact in facts:
            cand, status, _ = self.resolve_raw_fact(fact, filings_by_accession=filing_map)
            if status == SECConceptMatchStatus.MATCHED and cand is not None:
                candidates.append(cand)

        return candidates
