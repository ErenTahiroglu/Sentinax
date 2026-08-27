"""
backend/engine/private/sec/period_context.py
==============================================
SEC EDGAR Phase 8B.2A.5: Economic Period Context Classification & Candidate Grouping.

Core Invariants:
    - Pure deterministic economic period classification (INSTANT, COVER_DATE_INSTANT, ANNUAL_DURATION, QUARTER_DURATION, YTD_DURATION, IRREGULAR_DURATION).
    - No fabricated dates: missing start or end dates remain None. No date(1970, 1, 1) or epoch sentinels.
    - Preserves all candidate observations without winner selection or filing precedence.
    - Dates (start_date, end_date, filing report_date) drive period classification; fp/frame/form_date are auxiliary evidence.
    - DEI cover date shares outstanding (dei:EntityCommonStockSharesOutstanding) is strictly separated from us-gaap shares.
    - Filing vs Candidate consistency (CIK, Accession, Filing ID, Form) fails closed on mismatch (INVALID_CONTEXT).
    - Non-primary forms (8-K, 6-K, OTHER, 10-KT, 10-QT) cannot produce PRIMARY_REPORT_PERIOD.
    - Invalid/insufficient period candidates are preserved in SECPeriodGroupingResult.ungroupable and never merged into fake winner groups.
    - Inclusive duration days convention: duration_days = (end_date - start_date).days + 1.
    - Numerical values (Decimal) are NEVER altered (no YTD subtraction, no TTM, no annualization).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.models import (
    PeriodType,
    SECCanonicalFactCandidate,
    SECFilingRecord,
)

# ─────────────────────────────────────────────────────────────────────────────
# Duration Constants (Inclusive days: (end_date - start_date).days + 1)
# ─────────────────────────────────────────────────────────────────────────────

# Annual duration bounds (~11 months to ~12.5 months to support 52/53-week fiscal years)
ANNUAL_MIN_DAYS = 330
ANNUAL_MAX_DAYS = 385

# Standalone quarter bounds (~10 to ~16 weeks to support 52/53-week quarters)
QUARTER_MIN_DAYS = 70
QUARTER_MAX_DAYS = 115

# Interim 6-month YTD bounds (~5 to ~7 months)
YTD_6M_MIN_DAYS = 150
YTD_6M_MAX_DAYS = 210

# Interim 9-month YTD bounds (~8 to ~10 months)
YTD_9M_MIN_DAYS = 240
YTD_9M_MAX_DAYS = 300

# Supported periodic form roles eligible for PRIMARY_REPORT_PERIOD
SUPPORTED_PRIMARY_FORM_ROLES = {
    "primary_annual",
    "amendment_annual",
    "primary_quarterly",
    "amendment_quarterly",
    "fpi_annual",
    "fpi_amendment_annual",
}


class SECEconomicPeriodKind(Enum):
    """Classification of the economic time interval of a financial observation."""
    INSTANT = "instant"                           # Balance sheet point-in-time observation
    COVER_DATE_INSTANT = "cover_date_instant"     # Cover-page point-in-time observation (e.g. DEI shares outstanding)
    ANNUAL_DURATION = "annual_duration"           # Approx 1-year fiscal period (e.g. 10-K, 20-F, 40-F)
    QUARTER_DURATION = "quarter_duration"         # Standalone ~3-month fiscal quarter
    YTD_DURATION = "ytd_duration"                 # Year-to-date interim period (e.g. 6M Q2 YTD, 9M Q3 YTD)
    IRREGULAR_DURATION = "irregular_duration"     # Stub, transition, or irregular non-standard period
    UNKNOWN = "unknown"                           # Insufficient evidence to classify


class SECPeriodAlignmentStatus(Enum):
    """Semantic alignment of the candidate's period relative to the containing filing report date."""
    PRIMARY_REPORT_PERIOD = "primary_report_period"       # Current period ending on the filing's report_date
    COMPARATIVE_PRIOR_PERIOD = "comparative_prior_period" # Prior comparative period disclosed in the filing
    COVER_DATE_CONTEXT = "cover_date_context"             # Associated with filing cover date / subsequent event
    NON_PRIMARY_CONTEXT = "non_primary_context"           # 8-K, 6-K, OTHER form, or non-primary context
    UNRESOLVED_FILING = "unresolved_filing"               # Filing lineage missing, dates alone used
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"       # Dates missing or ambiguous
    INVALID_CONTEXT = "invalid_context"                   # Malformed period or filing metadata mismatch


@dataclass
class SECPeriodizedFactCandidate:
    """
    Economic period-classified canonical fact candidate ready for candidate grouping.
    Preserves exact Decimal value, original taxonomy/concept provenance, and filing metadata.
    """
    candidate_id: UUID
    raw_fact_id: UUID
    cik: str
    canonical_concept: str
    economic_period_kind: SECEconomicPeriodKind
    period_alignment_status: SECPeriodAlignmentStatus
    economic_start_date: Optional[date]
    economic_end_date: Optional[date]
    duration_days: Optional[int]
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]
    filing_id: Optional[UUID]
    accession_number: Optional[str]
    form: Optional[str]
    form_role: str
    is_amendment: bool
    filing_report_date: Optional[date]
    is_comparative: bool
    classification_confidence: str      # "HIGH", "MEDIUM", "LOW"
    classification_basis: str
    diagnostics: List[str]
    value: Optional[Decimal]
    unit: str
    taxonomy: str
    source_concept: str
    match_strength: str
    variant_priority: int
    snapshot_id: Optional[UUID] = None
    filed_date: Optional[date] = None
    frame: Optional[str] = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "candidate_id": str(self.candidate_id),
            "raw_fact_id": str(self.raw_fact_id),
            "cik": self.cik,
            "canonical_concept": self.canonical_concept,
            "economic_period_kind": self.economic_period_kind.value,
            "period_alignment_status": self.period_alignment_status.value,
            "economic_start_date": self.economic_start_date.isoformat() if self.economic_start_date else None,
            "economic_end_date": self.economic_end_date.isoformat() if self.economic_end_date else None,
            "duration_days": self.duration_days,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "filing_id": str(self.filing_id) if self.filing_id else None,
            "accession_number": self.accession_number,
            "form": self.form,
            "form_role": self.form_role,
            "is_amendment": self.is_amendment,
            "filing_report_date": self.filing_report_date.isoformat() if self.filing_report_date else None,
            "is_comparative": self.is_comparative,
            "classification_confidence": self.classification_confidence,
            "classification_basis": self.classification_basis,
            "diagnostics": self.diagnostics,
            "value": str(self.value) if self.value is not None else None,
            "unit": self.unit,
            "taxonomy": self.taxonomy,
            "source_concept": self.source_concept,
            "match_strength": self.match_strength,
            "variant_priority": self.variant_priority,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "filed_date": self.filed_date.isoformat() if self.filed_date else None,
            "frame": self.frame,
            "created_at": self.created_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Period Classification & Grouping Service
# ─────────────────────────────────────────────────────────────────────────────

class SECPeriodClassifier:
    """
    Deterministic classifier that derives SECEconomicPeriodKind and SECPeriodAlignmentStatus
    from canonical candidates and filing metadata without selecting winners.
    """

    @staticmethod
    def classify_candidate(
        candidate: SECCanonicalFactCandidate,
        filing: Optional[SECFilingRecord] = None,
    ) -> SECPeriodizedFactCandidate:
        """
        Classifies a single canonical fact candidate into a periodized fact candidate.
        """
        diagnostics: List[str] = list(candidate.diagnostics)

        # ─────────────────────────────────────────────────────────────────────
        # 0. Filing vs Candidate Consistency & Association Proof Validation
        # ─────────────────────────────────────────────────────────────────────
        filing_report_date: Optional[date] = None

        if filing is not None:
            # 1. CIK consistency check: prerequisite for any association
            cand_cik = normalize_cik(candidate.cik)
            filing_cik = normalize_cik(filing.cik)
            if cand_cik != filing_cik:
                msg = f"CIK mismatch: candidate CIK '{candidate.cik}' vs filing CIK '{filing.cik}'."
                return SECPeriodizedFactCandidate(
                    candidate_id=candidate.id,
                    raw_fact_id=candidate.raw_fact_id,
                    cik=candidate.cik,
                    canonical_concept=candidate.canonical_concept,
                    economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                    period_alignment_status=SECPeriodAlignmentStatus.INVALID_CONTEXT,
                    economic_start_date=candidate.start_date,
                    economic_end_date=candidate.end_date,
                    duration_days=None,
                    fiscal_year=candidate.fiscal_year,
                    fiscal_period=candidate.fiscal_period,
                    filing_id=candidate.filing_id,
                    accession_number=candidate.accession_number,
                    form=candidate.form,
                    form_role=candidate.form_role,
                    is_amendment=candidate.is_amendment,
                    filing_report_date=None,
                    is_comparative=False,
                    classification_confidence="LOW",
                    classification_basis=msg,
                    diagnostics=diagnostics + [msg],
                    value=candidate.value,
                    unit=candidate.unit,
                    taxonomy=candidate.taxonomy,
                    source_concept=candidate.source_concept,
                    match_strength=candidate.match_strength,
                    variant_priority=candidate.variant_priority,
                    snapshot_id=candidate.snapshot_id,
                    filed_date=candidate.filed_date,
                    frame=candidate.frame,
                )

            # 2. Check for explicit mismatches on accession and filing_id
            accession_match = False
            if candidate.accession_number is not None and filing.accession_number is not None:
                if candidate.accession_number.strip() != filing.accession_number.strip():
                    msg = f"Accession mismatch: candidate '{candidate.accession_number}' vs filing '{filing.accession_number}'."
                    return SECPeriodizedFactCandidate(
                        candidate_id=candidate.id,
                        raw_fact_id=candidate.raw_fact_id,
                        cik=candidate.cik,
                        canonical_concept=candidate.canonical_concept,
                        economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                        period_alignment_status=SECPeriodAlignmentStatus.INVALID_CONTEXT,
                        economic_start_date=candidate.start_date,
                        economic_end_date=candidate.end_date,
                        duration_days=None,
                        fiscal_year=candidate.fiscal_year,
                        fiscal_period=candidate.fiscal_period,
                        filing_id=candidate.filing_id,
                        accession_number=candidate.accession_number,
                        form=candidate.form,
                        form_role=candidate.form_role,
                        is_amendment=candidate.is_amendment,
                        filing_report_date=None,
                        is_comparative=False,
                        classification_confidence="LOW",
                        classification_basis=msg,
                        diagnostics=diagnostics + [msg],
                        value=candidate.value,
                        unit=candidate.unit,
                        taxonomy=candidate.taxonomy,
                        source_concept=candidate.source_concept,
                        match_strength=candidate.match_strength,
                        variant_priority=candidate.variant_priority,
                        snapshot_id=candidate.snapshot_id,
                        filed_date=candidate.filed_date,
                        frame=candidate.frame,
                    )
                accession_match = True

            id_match = False
            if candidate.filing_id is not None and filing.id is not None:
                if candidate.filing_id != filing.id:
                    msg = f"Filing ID mismatch: candidate filing_id '{candidate.filing_id}' vs filing id '{filing.id}'."
                    return SECPeriodizedFactCandidate(
                        candidate_id=candidate.id,
                        raw_fact_id=candidate.raw_fact_id,
                        cik=candidate.cik,
                        canonical_concept=candidate.canonical_concept,
                        economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                        period_alignment_status=SECPeriodAlignmentStatus.INVALID_CONTEXT,
                        economic_start_date=candidate.start_date,
                        economic_end_date=candidate.end_date,
                        duration_days=None,
                        fiscal_year=candidate.fiscal_year,
                        fiscal_period=candidate.fiscal_period,
                        filing_id=candidate.filing_id,
                        accession_number=candidate.accession_number,
                        form=candidate.form,
                        form_role=candidate.form_role,
                        is_amendment=candidate.is_amendment,
                        filing_report_date=None,
                        is_comparative=False,
                        classification_confidence="LOW",
                        classification_basis=msg,
                        diagnostics=diagnostics + [msg],
                        value=candidate.value,
                        unit=candidate.unit,
                        taxonomy=candidate.taxonomy,
                        source_concept=candidate.source_concept,
                        match_strength=candidate.match_strength,
                        variant_priority=candidate.variant_priority,
                        snapshot_id=candidate.snapshot_id,
                        filed_date=candidate.filed_date,
                        frame=candidate.frame,
                    )
                id_match = True

            # 3. Association proof verification: at least one of accession_match or id_match is required
            association_proven = accession_match or id_match

            if not association_proven:
                # Both accession and filing_id were absent on candidate (or unlinked)
                # CIK/form alone is NOT proof of specific filing identity!
                # Do NOT compare candidate.form vs filing.form!
                filing_report_date = None
                diagnostics.append("Supplied filing ignored because candidate lacks filing-level association proof.")
            else:
                # 4. Association is proven: now check Form consistency for metadata contradiction
                if candidate.form and filing.form:
                    if candidate.form.strip().upper() != filing.form.strip().upper():
                        msg = f"Form mismatch: candidate form '{candidate.form}' vs filing form '{filing.form}'."
                        return SECPeriodizedFactCandidate(
                            candidate_id=candidate.id,
                            raw_fact_id=candidate.raw_fact_id,
                            cik=candidate.cik,
                            canonical_concept=candidate.canonical_concept,
                            economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                            period_alignment_status=SECPeriodAlignmentStatus.INVALID_CONTEXT,
                            economic_start_date=candidate.start_date,
                            economic_end_date=candidate.end_date,
                            duration_days=None,
                            fiscal_year=candidate.fiscal_year,
                            fiscal_period=candidate.fiscal_period,
                            filing_id=candidate.filing_id,
                            accession_number=candidate.accession_number,
                            form=candidate.form,
                            form_role=candidate.form_role,
                            is_amendment=candidate.is_amendment,
                            filing_report_date=None,
                            is_comparative=False,
                            classification_confidence="LOW",
                            classification_basis=msg,
                            diagnostics=diagnostics + [msg],
                            value=candidate.value,
                            unit=candidate.unit,
                            taxonomy=candidate.taxonomy,
                            source_concept=candidate.source_concept,
                            match_strength=candidate.match_strength,
                            variant_priority=candidate.variant_priority,
                            snapshot_id=candidate.snapshot_id,
                            filed_date=candidate.filed_date,
                            frame=candidate.frame,
                        )

                filing_report_date = filing.report_date

        # ─────────────────────────────────────────────────────────────────────
        # 1. PeriodType.INSTANT Handling
        # ─────────────────────────────────────────────────────────────────────
        if candidate.period_type == PeriodType.INSTANT:
            if candidate.end_date is None:
                return SECPeriodizedFactCandidate(
                    candidate_id=candidate.id,
                    raw_fact_id=candidate.raw_fact_id,
                    cik=candidate.cik,
                    canonical_concept=candidate.canonical_concept,
                    economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                    period_alignment_status=SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE,
                    economic_start_date=None,
                    economic_end_date=None,
                    duration_days=None,
                    fiscal_year=candidate.fiscal_year,
                    fiscal_period=candidate.fiscal_period,
                    filing_id=candidate.filing_id,
                    accession_number=candidate.accession_number,
                    form=candidate.form,
                    form_role=candidate.form_role,
                    is_amendment=candidate.is_amendment,
                    filing_report_date=filing_report_date,
                    is_comparative=False,
                    classification_confidence="LOW",
                    classification_basis="Missing end_date on INSTANT fact.",
                    diagnostics=diagnostics + ["Missing end_date on INSTANT fact."],
                    value=candidate.value,
                    unit=candidate.unit,
                    taxonomy=candidate.taxonomy,
                    source_concept=candidate.source_concept,
                    match_strength=candidate.match_strength,
                    variant_priority=candidate.variant_priority,
                    snapshot_id=candidate.snapshot_id,
                    filed_date=candidate.filed_date,
                    frame=candidate.frame,
                )

            # Strict DEI cover-date shares rule
            is_dei_cover_shares = (
                candidate.canonical_concept == "SHARES_OUTSTANDING"
                and candidate.taxonomy == "dei"
                and candidate.source_concept == "EntityCommonStockSharesOutstanding"
            )

            if is_dei_cover_shares and filing_report_date and candidate.end_date > filing_report_date:
                kind = SECEconomicPeriodKind.COVER_DATE_INSTANT
                align = SECPeriodAlignmentStatus.COVER_DATE_CONTEXT
                conf = "HIGH"
                is_comp = False
                basis = "Cover page DEI shares outstanding dated after balance sheet report date."
            elif filing_report_date:
                kind = SECEconomicPeriodKind.INSTANT
                # Form role check: non-primary forms cannot be PRIMARY_REPORT_PERIOD
                if candidate.form_role in ("event_filing", "fpi_interim_or_event", "other") or candidate.form_role not in SUPPORTED_PRIMARY_FORM_ROLES:
                    align = SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
                    conf = "MEDIUM" if candidate.form_role == "fpi_interim_or_event" else "LOW"
                    is_comp = False
                    basis = f"Instant observation in non-primary periodic form ({candidate.form})."
                elif candidate.end_date == filing_report_date:
                    align = SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
                    conf = "HIGH"
                    is_comp = False
                    basis = "Instant observation matching filing report_date."
                elif candidate.end_date < filing_report_date:
                    align = SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD
                    conf = "HIGH"
                    is_comp = True
                    basis = "Instant balance sheet observation prior to current filing report_date."
                else:
                    align = SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
                    conf = "MEDIUM"
                    is_comp = False
                    basis = "Instant observation after filing report_date without DEI cover-page proof."
            else:
                kind = SECEconomicPeriodKind.INSTANT
                align = SECPeriodAlignmentStatus.UNRESOLVED_FILING
                conf = "LOW"
                is_comp = False
                basis = "Instant observation with unresolved filing report_date."

            return SECPeriodizedFactCandidate(
                candidate_id=candidate.id,
                raw_fact_id=candidate.raw_fact_id,
                cik=candidate.cik,
                canonical_concept=candidate.canonical_concept,
                economic_period_kind=kind,
                period_alignment_status=align,
                economic_start_date=None,
                economic_end_date=candidate.end_date,
                duration_days=None,
                fiscal_year=candidate.fiscal_year,
                fiscal_period=candidate.fiscal_period,
                filing_id=candidate.filing_id,
                accession_number=candidate.accession_number,
                form=candidate.form,
                form_role=candidate.form_role,
                is_amendment=candidate.is_amendment,
                filing_report_date=filing_report_date,
                is_comparative=is_comp,
                classification_confidence=conf,
                classification_basis=basis,
                diagnostics=diagnostics,
                value=candidate.value,
                unit=candidate.unit,
                taxonomy=candidate.taxonomy,
                source_concept=candidate.source_concept,
                match_strength=candidate.match_strength,
                variant_priority=candidate.variant_priority,
                snapshot_id=candidate.snapshot_id,
                filed_date=candidate.filed_date,
                frame=candidate.frame,
            )

        # ─────────────────────────────────────────────────────────────────────
        # 2. PeriodType.DURATION Handling
        # ─────────────────────────────────────────────────────────────────────
        if candidate.start_date is None or candidate.end_date is None:
            return SECPeriodizedFactCandidate(
                candidate_id=candidate.id,
                raw_fact_id=candidate.raw_fact_id,
                cik=candidate.cik,
                canonical_concept=candidate.canonical_concept,
                economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                period_alignment_status=SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE,
                economic_start_date=candidate.start_date,
                economic_end_date=candidate.end_date,
                duration_days=None,
                fiscal_year=candidate.fiscal_year,
                fiscal_period=candidate.fiscal_period,
                filing_id=candidate.filing_id,
                accession_number=candidate.accession_number,
                form=candidate.form,
                form_role=candidate.form_role,
                is_amendment=candidate.is_amendment,
                filing_report_date=filing_report_date,
                is_comparative=False,
                classification_confidence="LOW",
                classification_basis="Missing start_date or end_date on DURATION fact.",
                diagnostics=diagnostics + ["Missing start_date or end_date on DURATION fact."],
                value=candidate.value,
                unit=candidate.unit,
                taxonomy=candidate.taxonomy,
                source_concept=candidate.source_concept,
                match_strength=candidate.match_strength,
                variant_priority=candidate.variant_priority,
                snapshot_id=candidate.snapshot_id,
                filed_date=candidate.filed_date,
                frame=candidate.frame,
            )

        duration_days = (candidate.end_date - candidate.start_date).days + 1

        if candidate.start_date > candidate.end_date:
            return SECPeriodizedFactCandidate(
                candidate_id=candidate.id,
                raw_fact_id=candidate.raw_fact_id,
                cik=candidate.cik,
                canonical_concept=candidate.canonical_concept,
                economic_period_kind=SECEconomicPeriodKind.UNKNOWN,
                period_alignment_status=SECPeriodAlignmentStatus.INVALID_CONTEXT,
                economic_start_date=candidate.start_date,
                economic_end_date=candidate.end_date,
                duration_days=duration_days,
                fiscal_year=candidate.fiscal_year,
                fiscal_period=candidate.fiscal_period,
                filing_id=candidate.filing_id,
                accession_number=candidate.accession_number,
                form=candidate.form,
                form_role=candidate.form_role,
                is_amendment=candidate.is_amendment,
                filing_report_date=filing_report_date,
                is_comparative=False,
                classification_confidence="LOW",
                classification_basis=f"Malformed duration dates: start_date {candidate.start_date} is after end_date {candidate.end_date}.",
                diagnostics=diagnostics + ["start_date > end_date."],
                value=candidate.value,
                unit=candidate.unit,
                taxonomy=candidate.taxonomy,
                source_concept=candidate.source_concept,
                match_strength=candidate.match_strength,
                variant_priority=candidate.variant_priority,
                snapshot_id=candidate.snapshot_id,
                filed_date=candidate.filed_date,
                frame=candidate.frame,
            )

        # Classify economic period kind by duration span
        if ANNUAL_MIN_DAYS <= duration_days <= ANNUAL_MAX_DAYS:
            kind = SECEconomicPeriodKind.ANNUAL_DURATION
            kind_desc = f"Annual fiscal period ({duration_days} days)."
        elif QUARTER_MIN_DAYS <= duration_days <= QUARTER_MAX_DAYS:
            kind = SECEconomicPeriodKind.QUARTER_DURATION
            kind_desc = f"Standalone quarter period ({duration_days} days)."
        elif (YTD_6M_MIN_DAYS <= duration_days <= YTD_6M_MAX_DAYS) or (YTD_9M_MIN_DAYS <= duration_days <= YTD_9M_MAX_DAYS):
            kind = SECEconomicPeriodKind.YTD_DURATION
            kind_desc = f"Year-to-date interim period ({duration_days} days)."
        else:
            kind = SECEconomicPeriodKind.IRREGULAR_DURATION
            kind_desc = f"Irregular non-standard duration ({duration_days} days)."

        # Determine period alignment status
        if filing_report_date:
            if candidate.form_role in ("event_filing", "fpi_interim_or_event", "other") or candidate.form_role not in SUPPORTED_PRIMARY_FORM_ROLES:
                align = SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
                conf = "MEDIUM" if candidate.form_role == "fpi_interim_or_event" else "LOW"
                is_comp = False
                basis = f"{kind_desc} in non-primary periodic form ({candidate.form})."
            elif kind == SECEconomicPeriodKind.IRREGULAR_DURATION:
                align = SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
                conf = "LOW"
                is_comp = False
                basis = f"Irregular duration ({duration_days} days) in form ({candidate.form})."
            elif candidate.end_date == filing_report_date:
                align = SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
                conf = "HIGH"
                is_comp = False
                basis = f"Primary report period ending on filing report_date ({kind_desc})."
            elif candidate.end_date < filing_report_date:
                align = SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD
                conf = "HIGH"
                is_comp = True
                basis = f"Comparative prior period ending before filing report_date ({kind_desc})."
            else:
                align = SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
                conf = "MEDIUM"
                is_comp = False
                basis = f"Duration period ending after filing report_date ({kind_desc})."
        else:
            align = SECPeriodAlignmentStatus.UNRESOLVED_FILING
            conf = "LOW"
            is_comp = False
            basis = f"{kind_desc} with unresolved filing report_date."

        return SECPeriodizedFactCandidate(
            candidate_id=candidate.id,
            raw_fact_id=candidate.raw_fact_id,
            cik=candidate.cik,
            canonical_concept=candidate.canonical_concept,
            economic_period_kind=kind,
            period_alignment_status=align,
            economic_start_date=candidate.start_date,
            economic_end_date=candidate.end_date,
            duration_days=duration_days,
            fiscal_year=candidate.fiscal_year,
            fiscal_period=candidate.fiscal_period,
            filing_id=candidate.filing_id,
            accession_number=candidate.accession_number,
            form=candidate.form,
            form_role=candidate.form_role,
            is_amendment=candidate.is_amendment,
            filing_report_date=filing_report_date,
            is_comparative=is_comp,
            classification_confidence=conf,
            classification_basis=basis,
            diagnostics=diagnostics,
            value=candidate.value,
            unit=candidate.unit,
            taxonomy=candidate.taxonomy,
            source_concept=candidate.source_concept,
            match_strength=candidate.match_strength,
            variant_priority=candidate.variant_priority,
            snapshot_id=candidate.snapshot_id,
            filed_date=candidate.filed_date,
            frame=candidate.frame,
        )

    @classmethod
    def classify_candidates(
        cls,
        candidates: List[SECCanonicalFactCandidate],
        filings: Optional[List[SECFilingRecord]] = None,
    ) -> List[SECPeriodizedFactCandidate]:
        """
        Classifies a collection of canonical candidates into periodized candidates.
        """
        filing_map: Dict[str, SECFilingRecord] = {}
        if filings:
            for f in filings:
                if f.accession_number:
                    filing_map[f.accession_number.strip()] = f

        result: List[SECPeriodizedFactCandidate] = []
        for cand in candidates:
            f = filing_map.get(cand.accession_number.strip()) if cand.accession_number else None
            result.append(cls.classify_candidate(cand, filing=f))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Economic Candidate Grouping Helper & Grouping Result Model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SECPeriodGroupingResult:
    """
    Result of candidate grouping separating valid economic groups from ungroupable candidates.
    Preserves all candidates from original filings, amendments, and comparative disclosures.
    """
    groups: Dict[Tuple[str, str, str, str, Optional[str], str], List[SECPeriodizedFactCandidate]]
    ungroupable: List[SECPeriodizedFactCandidate]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_count": len(self.groups),
            "ungroupable_count": len(self.ungroupable),
            "groups": {
                f"{k[0]}|{k[1]}|{k[2]}|{k[3]}|{k[4]}|{k[5]}": [c.to_dict() for c in v]
                for k, v in self.groups.items()
            },
            "ungroupable": [c.to_dict() for c in self.ungroupable],
        }


def build_economic_group_key(
    candidate: SECPeriodizedFactCandidate,
) -> Optional[Tuple[str, str, str, str, Optional[str], str]]:
    """
    Constructs the canonical 6-tuple economic grouping key for a periodized candidate.
    Grouping Key: (cik, canonical_concept, unit, economic_period_kind, start_date, end_date)

    Returns None if:
        - economic_period_kind is UNKNOWN or IRREGULAR_DURATION
        - period_alignment_status is INSUFFICIENT_EVIDENCE or INVALID_CONTEXT
        - economic_end_date is None
        - economic_period_kind is a duration kind and economic_start_date is None
    """
    if candidate.economic_period_kind in (SECEconomicPeriodKind.UNKNOWN, SECEconomicPeriodKind.IRREGULAR_DURATION):
        return None

    if candidate.period_alignment_status in (
        SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE,
        SECPeriodAlignmentStatus.INVALID_CONTEXT,
    ):
        return None

    if candidate.economic_end_date is None:
        return None

    if candidate.economic_period_kind in (
        SECEconomicPeriodKind.ANNUAL_DURATION,
        SECEconomicPeriodKind.QUARTER_DURATION,
        SECEconomicPeriodKind.YTD_DURATION,
    ) and candidate.economic_start_date is None:
        return None

    start_str = candidate.economic_start_date.isoformat() if candidate.economic_start_date else None
    end_str = candidate.economic_end_date.isoformat()

    return (
        candidate.cik,
        candidate.canonical_concept,
        candidate.unit,
        candidate.economic_period_kind.value,
        start_str,
        end_str,
    )


def group_periodized_candidates(
    candidates: List[SECPeriodizedFactCandidate],
) -> SECPeriodGroupingResult:
    """
    Groups periodized candidates by their canonical economic observation key.
    Preserves all candidates from original filings, amendments, and comparative disclosures
    without dropping or picking winners.

    Ungroupable candidates (e.g. UNKNOWN, INVALID_CONTEXT, INSUFFICIENT_EVIDENCE, IRREGULAR_DURATION)
    are preserved in the `ungroupable` list and never merged into a fake UNKNOWN group.
    """
    groups: Dict[Tuple[str, str, str, str, Optional[str], str], List[SECPeriodizedFactCandidate]] = {}
    ungroupable: List[SECPeriodizedFactCandidate] = []

    for cand in candidates:
        key = build_economic_group_key(cand)
        if key is None:
            ungroupable.append(cand)
        else:
            if key not in groups:
                groups[key] = []
            groups[key].append(cand)

    return SECPeriodGroupingResult(groups=groups, ungroupable=ungroupable)
