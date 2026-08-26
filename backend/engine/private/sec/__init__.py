"""
backend/engine/private/sec/__init__.py
========================================
SEC EDGAR Filing & Raw XBRL CompanyFacts Backbone (Phase 8A Hardened).
"""

from backend.engine.private.sec.cik import format_cik_for_path, normalize_cik
from backend.engine.private.sec.client import (
    DEFAULT_SENTINAX_SAFETY_RPS,
    SEC_OFFICIAL_MAX_RPS,
    SECEdgarClient,
    SECRateLimiter,
    get_shared_sec_rate_limiter,
    reset_shared_sec_rate_limiter,
)
from backend.engine.private.sec.company_facts import (
    SECCompanyFactsParser,
    SECCompanyFactsProvider,
    parse_sec_decimal,
)
from backend.engine.private.sec.discovery import (
    SECTickerCandidate,
    SECTickerDiscoveryService,
)
from backend.engine.private.sec.lineage import (
    create_fact_filing_links,
    get_fact_acceptance_timestamp,
    get_fact_source_available_at,
    resolve_fact_filing_lineage,
)
from backend.engine.private.sec.models import (
    PeriodType,
    SECFactFilingLinkRecord,
    SECFilingRecord,
    SECRawFactRecord,
    SECResource,
    SECSubmissionMetadata,
    build_archive_url,
    build_company_facts_url,
    build_submissions_url,
)
from backend.engine.private.sec.submissions import (
    SECSubmissionsFetchResult,
    SECSubmissionsParser,
    SECSubmissionsProvider,
    parse_sec_boolean,
    parse_sec_date,
    parse_sec_datetime_hardened,
)

__all__ = [
    "normalize_cik",
    "format_cik_for_path",
    "PeriodType",
    "SECResource",
    "SECFilingRecord",
    "SECRawFactRecord",
    "SECFactFilingLinkRecord",
    "SECSubmissionMetadata",
    "SECSubmissionsFetchResult",
    "build_submissions_url",
    "build_company_facts_url",
    "build_archive_url",
    "SECRateLimiter",
    "SECEdgarClient",
    "get_shared_sec_rate_limiter",
    "reset_shared_sec_rate_limiter",
    "SEC_OFFICIAL_MAX_RPS",
    "DEFAULT_SENTINAX_SAFETY_RPS",
    "SECSubmissionsParser",
    "SECSubmissionsProvider",
    "parse_sec_datetime_hardened",
    "parse_sec_date",
    "parse_sec_boolean",
    "SECCompanyFactsParser",
    "SECCompanyFactsProvider",
    "parse_sec_decimal",
    "create_fact_filing_links",
    "resolve_fact_filing_lineage",
    "get_fact_acceptance_timestamp",
    "get_fact_source_available_at",
    "SECTickerCandidate",
    "SECTickerDiscoveryService",
]
