"""
backend/engine/private/sec/__init__.py
========================================
SEC EDGAR Filing & Raw XBRL CompanyFacts Backbone (Phase 8A).
"""

from backend.engine.private.sec.cik import format_cik_for_path, normalize_cik
from backend.engine.private.sec.client import (
    DEFAULT_SENTINAX_SAFETY_RPS,
    SEC_OFFICIAL_MAX_RPS,
    SECEdgarClient,
    SECRateLimiter,
)
from backend.engine.private.sec.company_facts import (
    SECCompanyFactsParser,
    SECCompanyFactsProvider,
)
from backend.engine.private.sec.discovery import (
    SECTickerCandidate,
    SECTickerDiscoveryService,
)
from backend.engine.private.sec.lineage import (
    get_fact_knowledge_boundary,
    resolve_fact_filing_lineage,
)
from backend.engine.private.sec.models import (
    PeriodType,
    SECFilingRecord,
    SECRawFactRecord,
    SECSubmissionMetadata,
    build_archive_url,
    build_company_facts_url,
    build_submissions_url,
)
from backend.engine.private.sec.submissions import (
    SECSubmissionsParser,
    SECSubmissionsProvider,
    parse_sec_boolean,
    parse_sec_date,
    parse_sec_datetime,
)

__all__ = [
    "normalize_cik",
    "format_cik_for_path",
    "PeriodType",
    "SECFilingRecord",
    "SECRawFactRecord",
    "SECSubmissionMetadata",
    "build_submissions_url",
    "build_company_facts_url",
    "build_archive_url",
    "SECRateLimiter",
    "SECEdgarClient",
    "SEC_OFFICIAL_MAX_RPS",
    "DEFAULT_SENTINAX_SAFETY_RPS",
    "SECSubmissionsParser",
    "SECSubmissionsProvider",
    "parse_sec_datetime",
    "parse_sec_date",
    "parse_sec_boolean",
    "SECCompanyFactsParser",
    "SECCompanyFactsProvider",
    "resolve_fact_filing_lineage",
    "get_fact_knowledge_boundary",
    "SECTickerCandidate",
    "SECTickerDiscoveryService",
]
