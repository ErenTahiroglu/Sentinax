"""
backend/engine/private/sec/models.py
======================================
Canonical Domain Models, Enums, and URL Builders for SEC EDGAR Filing & XBRL Data Backbone.

Core Hardening Principles:
    - `accession_number` is preserved in official hyphenated format without requiring issuer CIK prefix match.
    - `acceptance_datetime` is strictly the SEC acceptance event timestamp; NOT guaranteed public availability.
    - `public_available_at` remains None unless explicitly observed/proven.
    - Numerical facts use `Decimal` for exact representation; zero is preserved as Decimal("0"); missing is None.
    - `PeriodType` enforces DURATION (start + end) vs INSTANT (end only); missing dates are rejected.
    - `SECFactFilingLinkRecord` provides append-only asynchronous fact-filing linkage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import UUID, uuid4

from backend.engine.private.sec.cik import format_cik_for_path, normalize_cik


class PeriodType(Enum):
    """Distinguishes duration facts from instant balance-sheet facts."""
    INSTANT = "instant"
    DURATION = "duration"


class SECResource(Enum):
    """Explicit resource routing for SEC EDGAR providers."""
    SUBMISSIONS = "submissions"
    COMPANY_FACTS = "company_facts"


@dataclass
class SECSubmissionMetadata:
    """
    Top-level issuer/filer metadata parsed from SEC Submissions endpoint (/submissions/CIK##########.json).
    """
    cik: str
    entity_name: str
    entity_type: Optional[str] = None
    sic: Optional[str] = None
    sic_description: Optional[str] = None
    tickers: List[str] = field(default_factory=list)
    exchanges: List[str] = field(default_factory=list)
    ein: Optional[str] = None
    description: Optional[str] = None
    website: Optional[str] = None
    investor_website: Optional[str] = None
    category: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    state_of_incorporation: Optional[str] = None
    state_of_incorporation_description: Optional[str] = None
    addresses: Dict[str, Any] = field(default_factory=dict)
    phone: Optional[str] = None
    flags: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SECFilingRecord:
    """
    Canonical immutable filing record ingested from official SEC EDGAR submissions.
    """
    cik: str
    accession_number: str
    form: str
    is_amendment: bool
    filing_date: Optional[date] = None
    report_date: Optional[date] = None
    acceptance_datetime: Optional[datetime] = None
    acceptance_raw: Optional[str] = None
    acceptance_precision: Optional[str] = None
    acceptance_timezone_semantics: Optional[str] = None
    public_available_at: Optional[datetime] = None
    public_availability_basis: Optional[str] = None
    act: Optional[str] = None
    file_number: Optional[str] = None
    film_number: Optional[str] = None
    items: List[str] = field(default_factory=list)
    size: Optional[int] = None
    is_xbrl: Optional[bool] = None
    is_inline_xbrl: Optional[bool] = None
    primary_document: Optional[str] = None
    primary_doc_description: Optional[str] = None
    source_url: Optional[str] = None
    instrument_id: Optional[UUID] = None
    snapshot_id: Optional[UUID] = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.cik = normalize_cik(self.cik)
        if not self.source_url:
            self.source_url = build_archive_url(self.cik, self.accession_number, self.primary_document)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "cik": self.cik,
            "accession_number": self.accession_number,
            "form": self.form,
            "is_amendment": self.is_amendment,
            "filing_date": self.filing_date.isoformat() if self.filing_date else None,
            "report_date": self.report_date.isoformat() if self.report_date else None,
            "acceptance_datetime": self.acceptance_datetime.isoformat() if self.acceptance_datetime else None,
            "acceptance_raw": self.acceptance_raw,
            "acceptance_precision": self.acceptance_precision,
            "acceptance_timezone_semantics": self.acceptance_timezone_semantics,
            "public_available_at": self.public_available_at.isoformat() if self.public_available_at else None,
            "public_availability_basis": self.public_availability_basis,
            "act": self.act,
            "file_number": self.file_number,
            "film_number": self.film_number,
            "items": self.items,
            "size": self.size,
            "is_xbrl": self.is_xbrl,
            "is_inline_xbrl": self.is_inline_xbrl,
            "primary_document": self.primary_document,
            "primary_doc_description": self.primary_doc_description,
            "source_url": self.source_url,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "raw_metadata": self.raw_metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class SECRawFactRecord:
    """
    Canonical immutable raw fact entry ingested from SEC CompanyFacts API (/api/xbrl/companyfacts/CIK##########.json).
    Preserves original standard taxonomy tags without premature metric interpretation.
    """
    cik: str
    taxonomy: str
    concept: str
    unit: str
    period_type: PeriodType
    accession_number: Optional[str] = None
    value: Optional[Decimal] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    label: Optional[str] = None
    description: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None
    form: Optional[str] = None
    filed_date: Optional[date] = None
    frame: Optional[str] = None
    instrument_id: Optional[UUID] = None
    filing_id: Optional[UUID] = None
    snapshot_id: Optional[UUID] = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_fact: Dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.cik = normalize_cik(self.cik)
        if isinstance(self.period_type, str):
            self.period_type = PeriodType(self.period_type)
        if self.accession_number:
            self.accession_number = self.accession_number.strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "filing_id": str(self.filing_id) if self.filing_id else None,
            "cik": self.cik,
            "accession_number": self.accession_number,
            "taxonomy": self.taxonomy,
            "concept": self.concept,
            "label": self.label,
            "description": self.description,
            "unit": self.unit,
            "value": str(self.value) if self.value is not None else None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "period_type": self.period_type.value,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "form": self.form,
            "filed_date": self.filed_date.isoformat() if self.filed_date else None,
            "frame": self.frame,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "raw_fact": self.raw_fact,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class SECFactFilingLinkRecord:
    """
    Append-only lineage record linking a raw XBRL fact to its corresponding SEC filing.
    """
    fact_id: UUID
    filing_id: UUID
    accession_number: str
    cik: str
    resolution_method: str = "ACCESSION_MATCH"
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.cik = normalize_cik(self.cik)
        self.accession_number = self.accession_number.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Official URL Builders
# ─────────────────────────────────────────────────────────────────────────────

def build_submissions_url(cik: Union[str, int]) -> str:
    """
    Builds the official SEC EDGAR Submissions API URL.
    Example: https://data.sec.gov/submissions/CIK0000320193.json
    """
    normalized_cik = normalize_cik(cik)
    return f"https://data.sec.gov/submissions/CIK{normalized_cik}.json"


def build_company_facts_url(cik: Union[str, int]) -> str:
    """
    Builds the official SEC EDGAR CompanyFacts API URL.
    Example: https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json
    """
    normalized_cik = normalize_cik(cik)
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{normalized_cik}.json"


def build_archive_url(
    cik: Union[str, int],
    accession_number: str,
    primary_document: Optional[str] = None,
) -> str:
    """
    Builds the official SEC EDGAR Archives URL for a filing or specific document.
    Note: Path CIK is the subject issuer's CIK unpadded; accession folder has hyphens removed.
    Accession prefix may be a third-party submitting agent's CIK.
    """
    path_cik = format_cik_for_path(cik)
    accn_no_hyphens = accession_number.replace("-", "").strip()
    base = f"https://www.sec.gov/Archives/edgar/data/{path_cik}/{accn_no_hyphens}"
    if primary_document:
        clean_doc = primary_document.strip().lstrip("/")
        return f"{base}/{clean_doc}"
    return base
