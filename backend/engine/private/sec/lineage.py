"""
backend/engine/private/sec/lineage.py
======================================
Fact-to-Filing Lineage & Point-In-Time Acceptance Timeline Resolution Service.

Core Hardening Principles:
    - `SECFactFilingLinkRecord` provides an append-only linkage table without mutating raw fact records.
    - CIK and accession number consistency is strictly verified between fact and filing before linking.
    - `acceptance_datetime` is strictly the SEC EDGAR acceptance event timestamp.
    - `public_available_at` represents verified public availability on sec.gov (None if unknown).
    - Unresolved facts persist with `filing_id = None` and are NEVER dropped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from backend.engine.private.sec.models import (
    SECFactFilingLinkRecord,
    SECFilingRecord,
    SECRawFactRecord,
)


def create_fact_filing_links(
    facts: List[SECRawFactRecord],
    filings: List[SECFilingRecord],
) -> Tuple[List[SECFactFilingLinkRecord], List[SECRawFactRecord]]:
    """
    Creates append-only SECFactFilingLinkRecord instances for matching accession numbers.
    Validates CIK and accession consistency.
    """
    filing_map: Dict[str, SECFilingRecord] = {
        f.accession_number.strip(): f for f in filings if f.accession_number
    }

    links: List[SECFactFilingLinkRecord] = []
    for fact in facts:
        if not fact.accession_number:
            continue

        accn = fact.accession_number.strip()
        filing = filing_map.get(accn)
        if filing:
            # Enforce CIK and accession consistency
            if fact.cik != filing.cik:
                raise ValueError(
                    f"CIK mismatch in fact-filing linkage: fact has '{fact.cik}', filing has '{filing.cik}' for accession '{accn}'."
                )
            if accn != filing.accession_number.strip():
                raise ValueError(
                    f"Accession mismatch in fact-filing linkage: fact has '{accn}', filing has '{filing.accession_number}'."
                )

            link = SECFactFilingLinkRecord(
                fact_id=fact.id,
                filing_id=filing.id,
                accession_number=accn,
                cik=fact.cik,
                resolution_method="ACCESSION_MATCH",
            )
            links.append(link)
            fact.filing_id = filing.id
            if fact.instrument_id is None and filing.instrument_id is not None:
                fact.instrument_id = filing.instrument_id

    return links, facts


def resolve_fact_filing_lineage(
    facts: List[SECRawFactRecord],
    filings: List[SECFilingRecord],
) -> List[SECRawFactRecord]:
    """
    Convenience wrapper that resolves fact.filing_id in-memory.
    """
    _, updated_facts = create_fact_filing_links(facts, filings)
    return updated_facts


def get_fact_acceptance_timestamp(
    fact: SECRawFactRecord,
    filings_by_accession: Dict[str, SECFilingRecord],
) -> Optional[datetime]:
    """
    Returns the SEC EDGAR acceptance timestamp for a fact from its linked filing.
    """
    if not fact.accession_number:
        return None
    accn = fact.accession_number.strip()
    filing = filings_by_accession.get(accn)
    if filing and filing.acceptance_datetime:
        return filing.acceptance_datetime
    return None


def get_fact_source_available_at(
    fact: SECRawFactRecord,
    filings_by_accession: Dict[str, SECFilingRecord],
) -> Optional[datetime]:
    """
    Returns the verified public availability timestamp for a fact from its linked filing.
    Returns None if unknown.
    """
    if not fact.accession_number:
        return None
    accn = fact.accession_number.strip()
    filing = filings_by_accession.get(accn)
    if filing and filing.public_available_at:
        return filing.public_available_at
    return None
