"""
backend/engine/private/sec/lineage.py
======================================
Fact-to-Filing Lineage & Point-In-Time Acceptance Timeline Resolution Service.

Core Principles:
    - Joins SECRawFactRecord with SECFilingRecord on `accession_number` to assign `filing_id`.
    - Facts with unresolved accessions are NEVER discarded; filing_id remains None (unresolved status).
    - `filed_date` (calendar date) is NOT the public knowledge boundary.
    - True Point-In-Time knowledge boundary is derived strictly from `filing.acceptance_datetime`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from backend.engine.private.sec.models import SECFilingRecord, SECRawFactRecord


def resolve_fact_filing_lineage(
    facts: List[SECRawFactRecord],
    filings: List[SECFilingRecord],
) -> List[SECRawFactRecord]:
    """
    Links raw fact records to their corresponding master filing records via accession number.
    Mutates/updates filing_id and instrument_id on matching facts.
    """
    filing_map: Dict[str, SECFilingRecord] = {
        f.accession_number.strip(): f for f in filings if f.accession_number
    }

    for fact in facts:
        accn = fact.accession_number.strip() if fact.accession_number else ""
        if accn in filing_map:
            filing = filing_map[accn]
            fact.filing_id = filing.id
            if fact.instrument_id is None and filing.instrument_id is not None:
                fact.instrument_id = filing.instrument_id

    return facts


def get_fact_knowledge_boundary(
    fact: SECRawFactRecord,
    filings_by_accession: Dict[str, SECFilingRecord],
) -> Optional[datetime]:
    """
    Derives the true point-in-time acceptance timestamp for a fact from its linked filing.
    Returns None if filing or acceptance_datetime is unresolved.
    """
    accn = fact.accession_number.strip() if fact.accession_number else ""
    filing = filings_by_accession.get(accn)
    if filing and filing.acceptance_datetime:
        return filing.acceptance_datetime
    return None
