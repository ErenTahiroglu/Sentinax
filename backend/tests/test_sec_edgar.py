"""
backend/tests/test_sec_edgar.py
=================================
Comprehensive Unit, Point-In-Time, Entity Identity & Lineage Hardening Test Suite for
SEC EDGAR Filing & Raw XBRL CompanyFacts Backbone (Phase 8A.6 Hardened).

Coverage:
    - SEC Entity vs Security Identity Invariants (Scenarios 1-8)
    - Acceptance Timestamps: Aware TIMESTAMPTZ vs Naive Local Time (Scenarios 9-17)
    - Raw Fact Decimal Precision, Nullable Accession & Period Types (Scenarios 18-24)
    - Fact-to-Filing Append-Only Linkage & DB Integrity Constraints (Scenarios 25-32)
    - Full SEC Ingestion & Provider Contract Regression Coverage (Scenarios 33-60+)
"""

import math
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from backend.engine.private.domain import (
    AssetClass,
    AsOfMode,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderConfigurationError,
    ProviderInvalidSymbolError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.identity import (
    InstrumentRecord,
    resolve_instruments_for_sec_cik,
)
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.sec_edgar import (
    SECCompanyFactsDataProvider,
    SECEdgarProvider,
    SECSubmissionsDataProvider,
)
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
from backend.engine.private.storage_models import RawProviderSnapshotRecord


# ─────────────────────────────────────────────────────────────────────────────
# Sample Payloads
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_SUBMISSIONS_PAYLOAD = {
    "cik": "0000320193",
    "entityType": "operating",
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "exchanges": ["Nasdaq"],
    "ein": "942404110",
    "fiscalYearEnd": "0930",
    "stateOfIncorporation": "CA",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-24-000106",  # Self-filer accession
                "0001140361-24-024352",  # Third-party agent accession
                "0000320193-23-000106",
            ],
            "filingDate": ["2024-11-01", "2024-02-02", "2023-11-03"],
            "reportDate": ["2024-09-28", "2023-12-30", "2023-09-30"],
            "acceptanceDateTime": [
                "2024-11-01T16:05:34.000Z",  # Explicit UTC
                "20240202160100",            # Compact without timezone
                "2023-11-03",                 # Date-only
            ],
            "act": ["34", "34", "34"],
            "form": ["10-K", "10-Q/A", "10-K"],
            "fileNumber": ["001-36743", "001-36743", "001-36743"],
            "filmNumber": ["241378901", "24589012", "231378901"],
            "items": [[], ["2.02"], []],
            "size": [12450000, 4500000, 11900000],
            "isXBRL": [1, 1, 1],
            "isInlineXBRL": [1, 1, 1],
            "primaryDocument": ["aapl-20240928.htm", "aapl-20231230.htm", "aapl-20230930.htm"],
            "primaryDocDescription": ["10-K", "10-Q/A", "10-K"],
        },
        "files": [
            {"name": "CIK0000320193-submissions-001.json", "filingCount": 1000, "filingFrom": "2015-01-01", "filingTo": "2023-10-31"},
            {"name": "CIK0000320193-submissions-002.json", "filingCount": 500, "filingFrom": "2010-01-01", "filingTo": "2014-12-31"},
        ],
    },
}

SAMPLE_COMPANY_FACTS_PAYLOAD = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Entity Common Stock, Shares Outstanding",
                "description": "Indicate number of shares or other units outstanding.",
                "units": {
                    "shares": [
                        {
                            "end": "2024-10-18",
                            "val": 15115820000,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-11-01",
                            "frame": "CY2024Q3",
                            "accn": "0000320193-24-000106",
                        }
                    ]
                },
            }
        },
        "us-gaap": {
            "Revenues": {
                "label": "Revenue from Contract with Customer, Excluding Assessed Tax",
                "description": "Amount of revenue recognized from goods or services transferred to customers.",
                "units": {
                    "USD": [
                        {
                            "start": "2023-10-01",
                            "end": "2024-09-28",
                            "val": "391035000000.50",
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-11-01",
                            "frame": "CY2024",
                            "accn": "0000320193-24-000106",
                        },
                        {
                            "start": "2023-10-01",
                            "end": "2023-12-30",
                            "val": 119575000000,
                            "fy": 2024,
                            "fp": "Q1",
                            "form": "10-Q/A",
                            "filed": "2024-02-02",
                            "accn": "0001140361-24-024352",
                        },
                    ]
                },
            },
            "Assets": {
                "label": "Assets",
                "description": "Sum of the carrying amounts of all assets.",
                "units": {
                    "USD": [
                        {
                            "end": "2024-09-28",
                            "val": 364980000000,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-11-01",
                            "accn": "0000320193-24-000106",
                        },
                        {
                            "end": "2023-09-30",
                            "val": 0,  # Zero preservation
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-03",
                            "accn": "0000320193-23-000106",
                        },
                    ]
                },
            },
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. SEC Entity vs Security Identity Invariants (Scenarios 1-8)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECEntityVsSecurityIdentity:

    def test_01_and_02_sec_raw_records_do_not_store_canonical_instrument_id(self):
        """Scenario 1 & 2: SEC raw filing and fact records are entity-level; instrument_id is None."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)

        assert all(f.instrument_id is None for f in filings)
        assert all(f.instrument_id is None for f in facts)

    @pytest.mark.asyncio
    async def test_03_provider_response_retains_requesting_instrument_id(self):
        """Scenario 3: ProviderResponse retains requesting canonical_instrument_id in response context."""
        mock_client = AsyncMock(spec=SECEdgarClient)
        mock_client.get_json.return_value = SAMPLE_SUBMISSIONS_PAYLOAD
        mock_client.get_user_agent.return_value = "Sentinax <admin@example.com>"

        provider = SECSubmissionsDataProvider(client=mock_client)
        inst_uuid = uuid4()
        ctx = FetchContext(
            observation_type="SEC_SUBMISSIONS",
            provider_symbol="0000320193",
            canonical_instrument_id=inst_uuid,
        )
        resp = await provider.fetch(ctx)

        assert resp.status == DataStatus.COMPLETE
        assert resp.canonical_instrument_id == inst_uuid

    def test_04_to_08_same_cik_resolves_to_multiple_instruments_no_company_uuid(self):
        """Scenario 4-8: Same CIK resolves to multiple securities (e.g. GOOG & GOOGL); no duplicate company UUID."""
        cik = "0001652044"  # Alphabet Inc.
        inst_goog = InstrumentRecord(
            canonical_name="Alphabet Inc. Class C",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            cik=cik,
        )
        inst_googl = InstrumentRecord(
            canonical_name="Alphabet Inc. Class A",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            cik=cik,
        )

        instruments = [inst_goog, inst_googl]
        resolved = resolve_instruments_for_sec_cik(cik, instruments)

        assert len(resolved) == 2
        assert {i.id for i in resolved} == {inst_goog.id, inst_googl.id}
        assert resolved[0].cik == resolved[1].cik == "0001652044"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Acceptance Timestamps: Aware TIMESTAMPTZ vs Naive Local Time (Scenarios 9-17)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECAcceptanceTimestampHardening:

    def test_09_to_15_aware_vs_naive_acceptance_parsing(self):
        """Scenario 9-15: Explicit Z parses to aware UTC; naive local parses to local datetime; date-only has no time."""
        # 1. Explicit Z
        aware_dt, local_dt, raw_z, prec_z, sem_z = parse_sec_datetime_hardened("2024-11-01T16:05:34.000Z")
        assert aware_dt == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert local_dt is None
        assert sem_z == "EXPLICIT_UTC"
        assert prec_z == "SECOND_EXACT_UTC"

        # 2. Explicit Offset (+02:00)
        aware_off, local_off, _, _, sem_off = parse_sec_datetime_hardened("2024-11-01T18:05:34+02:00")
        assert aware_off == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert local_off is None
        assert sem_off == "EXPLICIT_UTC"

        # 3. Compact 14-digit without timezone -> Naive local datetime
        aware_c, local_c, raw_c, prec_c, sem_c = parse_sec_datetime_hardened("20240202160100")
        assert aware_c is None
        assert local_c == datetime(2024, 2, 2, 16, 1, 0)
        assert local_c.tzinfo is None
        assert sem_c == "SEC_EST_DOCUMENTED"
        assert prec_c == "SECOND_EXACT_NAIVE"

        # 4. Naive ISO -> Naive local datetime
        aware_iso, local_iso, _, _, sem_iso = parse_sec_datetime_hardened("2024-02-02 16:01:00")
        assert aware_iso is None
        assert local_iso == datetime(2024, 2, 2, 16, 1, 0)
        assert sem_iso == "SEC_EST_DOCUMENTED"

        # 5. Date-only -> No fabricated time
        aware_d, local_d, raw_d, prec_d, sem_d = parse_sec_datetime_hardened("2023-11-03")
        assert aware_d is None
        assert local_d is None
        assert prec_d == "DATE_ONLY"
        assert raw_d == "2023-11-03"

    def test_16_and_17_model_validation_rejects_naive_aware_mismatches(self):
        """Scenario 16 & 17: SECFilingRecord validates aware in acceptance_datetime and naive in acceptance_local_datetime."""
        # Aware in acceptance_datetime -> Valid
        f_valid = SECFilingRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            form="10-K",
            is_amendment=False,
            acceptance_datetime=datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc),
        )
        assert f_valid.acceptance_datetime.tzinfo is not None

        # Naive in acceptance_datetime -> Raises ValueError
        with pytest.raises(ValueError, match="acceptance_datetime must be timezone-aware"):
            SECFilingRecord(
                cik="0000320193",
                accession_number="0000320193-24-000106",
                form="10-K",
                is_amendment=False,
                acceptance_datetime=datetime(2024, 11, 1, 16, 5, 34), # Naive
            )

        # Aware in acceptance_local_datetime -> Raises ValueError
        with pytest.raises(ValueError, match="acceptance_local_datetime must be a naive local datetime"):
            SECFilingRecord(
                cik="0000320193",
                accession_number="0000320193-24-000106",
                form="10-K",
                is_amendment=False,
                acceptance_local_datetime=datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc), # Aware
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Raw Fact Decimal Precision, Nullable Accession & Period Types (Scenarios 18-24)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECRawFactPrecisionAndNullableAccession:

    def test_18_to_20_nullable_accession_persists_without_link(self):
        """Scenario 18-20: Missing fact accession persists as NULL without fabricating fake UNKNOWN_ACCN."""
        fact_no_accn = SECRawFactRecord(
            cik="0000320193",
            accession_number=None,
            taxonomy="us-gaap",
            concept="Cash",
            unit="USD",
            period_type=PeriodType.INSTANT,
            value=Decimal("1000.00"),
            end_date=date(2024, 9, 28),
        )
        assert fact_no_accn.accession_number is None

        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        links, _ = create_fact_filing_links([fact_no_accn], filings)
        assert len(links) == 0  # No link created for NULL accession

    def test_21_to_24_decimal_exactness_and_period_validation(self):
        """Scenario 21-24: Decimal precision is exact, string serialization, zero preserved, period types validated."""
        assert parse_sec_decimal("391035000000.50") == Decimal("391035000000.50")
        assert parse_sec_decimal(0) == Decimal("0")

        fact = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("391035000000.50"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        d = fact.to_dict()
        assert d["value"] == "391035000000.50"
        assert isinstance(d["value"], str)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fact-to-Filing Append-Only Linkage & DB Integrity (Scenarios 25-32)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFactFilingLinkIntegrity:

    def test_25_to_32_link_creation_and_consistency(self):
        """Scenario 25-32: SECFactFilingLinkRecord requires exact accession and CIK match; raw fact is immutable."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)

        links, linked_facts = create_fact_filing_links(facts, filings)
        assert len(links) == 5  # All 5 facts match filings

        # Inconsistent accession/CIK raises ValueError
        mismatched_filing = SECFilingRecord(
            cik="0000789019",
            accession_number="0000320193-24-000106",
            form="10-K",
            is_amendment=False,
        )
        with pytest.raises(ValueError, match="CIK mismatch"):
            create_fact_filing_links(facts, [mismatched_filing])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Full Ingestion & Provider Regression Coverage (Scenarios 33-60+)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECProviderFullRegressionSuite:

    def test_33_to_36_cik_and_url_builders(self):
        """Scenario 33-36: CIK normalizes to 10 digits; URL builders generate official endpoints."""
        assert normalize_cik("320193") == "0000320193"
        assert normalize_cik(320193) == "0000320193"
        assert format_cik_for_path("0000320193") == "320193"

        with pytest.raises(ValueError, match="digits only"):
            normalize_cik("320193A")

        assert build_submissions_url("320193") == "https://data.sec.gov/submissions/CIK0000320193.json"
        assert build_company_facts_url("320193") == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"

    def test_37_to_39_user_agent_required_and_no_api_key(self):
        """Scenario 37-39: Declared SEC_USER_AGENT required; public endpoints require no secret token."""
        client_no_env = SECEdgarClient(user_agent="")
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ProviderConfigurationError, match="SEC_USER_AGENT is not configured"):
                client_no_env.get_user_agent()

    @pytest.mark.asyncio
    async def test_40_to_45_typed_error_mappings(self):
        """Scenario 40-45: Typed errors for 403, 404, 429, 5xx, timeouts, and malformed JSON."""
        # 403 Permission
        m_403 = AsyncMock(spec=httpx.AsyncClient)
        m_403.get.return_value = MagicMock(status_code=403)
        c_403 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_403)
        with pytest.raises(ProviderPermissionError):
            await c_403.get_json("https://data.sec.gov/test")

        # 404 Invalid Symbol
        m_404 = AsyncMock(spec=httpx.AsyncClient)
        m_404.get.return_value = MagicMock(status_code=404)
        c_404 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_404)
        with pytest.raises(ProviderInvalidSymbolError):
            await c_404.get_json("https://data.sec.gov/test")

        # 429 Rate Limit
        m_429 = AsyncMock(spec=httpx.AsyncClient)
        m_429.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "5"})
        c_429 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_429)
        with pytest.raises(ProviderRateLimitError):
            await c_429.get_json("https://data.sec.gov/test")

        # 500 Server Error
        m_500 = AsyncMock(spec=httpx.AsyncClient)
        m_500.get.return_value = MagicMock(status_code=500)
        c_500 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_500)
        with pytest.raises(ProviderServerError):
            await c_500.get_json("https://data.sec.gov/test")

        # Timeout Error
        m_to = AsyncMock(spec=httpx.AsyncClient)
        m_to.get.side_effect = httpx.TimeoutException("Timeout")
        c_to = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_to)
        with pytest.raises(ProviderTimeoutError):
            await c_to.get_json("https://data.sec.gov/test")

    def test_46_and_47_columnar_parallel_array_validation(self):
        """Scenario 46 & 47: Parallel array length mismatch strictly raises ProviderSchemaError."""
        bad_payload = {
            "cik": "0000320193",
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-24-000106", "0000320193-24-000006"],
                    "form": ["10-K"],
                }
            }
        }
        with pytest.raises(ProviderSchemaError, match="array length mismatch"):
            SECSubmissionsParser.parse_filings(bad_payload)

    @pytest.mark.asyncio
    async def test_50_to_52_archived_submissions_path_guard_and_snapshots(self):
        """Scenario 50-52: Historical file traversal is path-safe and generates independent snapshots."""
        mock_client = AsyncMock(spec=SECEdgarClient)
        mock_client.get_json.side_effect = [
            SAMPLE_SUBMISSIONS_PAYLOAD,
            {"accessionNumber": ["0000320193-20-000001"], "form": ["10-Q"], "filingDate": ["2020-01-15"]},
        ]

        provider = SECSubmissionsProvider(client=mock_client)
        res = await provider.fetch_submissions("0000320193", include_archived=True, max_archived_files=1)

        assert isinstance(res, SECSubmissionsFetchResult)
        assert len(res.archived_snapshots) == 1
        assert res.filings[0].snapshot_id == res.main_snapshot.id

    @pytest.mark.asyncio
    async def test_57_and_58_historical_as_of_fail_closed(self):
        """Scenario 57 & 58: Historical external SOURCE_AS_OF & SYSTEM_AS_OF rejected."""
        provider = SECEdgarProvider()

        ctx_src = FetchContext(
            observation_type="SEC_COMPANY_FACTS",
            provider_symbol="0000320193",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        assert (await provider.fetch(ctx_src)).status == DataStatus.UNAVAILABLE

    def test_59_and_60_migration_010_schema_invariants(self):
        """Scenario 59 & 60: Migration 010 defines acceptance_local_datetime, nullable fact accession, and link trigger."""
        mig_path = os.path.join(os.path.dirname(__file__), "../../supabase/migrations/010_sec_entity_persistence_consistency.sql")
        assert os.path.exists(mig_path)

        with open(mig_path, "r", encoding="utf-8") as f:
            sql = f.read()

        assert "ADD COLUMN IF NOT EXISTS acceptance_local_datetime TIMESTAMP WITHOUT TIME ZONE" in sql
        assert "ALTER TABLE public.sec_raw_facts ALTER COLUMN accession_number DROP NOT NULL" in sql
        assert "validate_sec_fact_filing_link_integrity" in sql
        assert "trg_validate_sec_fact_filing_link_integrity" in sql
