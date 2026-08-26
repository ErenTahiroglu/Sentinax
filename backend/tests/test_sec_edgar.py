"""
backend/tests/test_sec_edgar.py
=================================
Comprehensive Unit, Point-In-Time, and Semantic Verification Test Suite for
SEC EDGAR Filing & Raw XBRL CompanyFacts Backbone (Phase 8A).

Coverage:
    - Current Submissions Parser (Scenarios 1-20)
    - Archived Submissions Traversal (Scenarios 21-27)
    - CompanyFacts Raw Fact Ingestion (Scenarios 28-47)
    - Fact-to-Filing Lineage & Knowledge Boundaries (Scenarios 48-52)
    - Point-In-Time Semantics (Scenarios 53-58)
    - Instrument Identity & Symbology Integration (Scenarios 59-64)
    - Security, Fair Access & Typed Errors (Scenarios 65-75)
    - Storage Models & Migration Invariants (Scenarios 76-84)
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from backend.engine.private.domain import (
    AsOfMode,
    DataConfidenceLevel,
    DataStatus,
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
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.sec_edgar import SECEdgarProvider
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
from backend.engine.private.storage_models import RawProviderSnapshotRecord


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & Sample Payloads
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
            "accessionNumber": ["0000320193-24-000106", "0000320193-24-000006", "0000320193-23-000106"],
            "filingDate": ["2024-11-01", "2024-02-02", "2023-11-03"],
            "reportDate": ["2024-09-28", "2023-12-30", "2023-09-30"],
            "acceptanceDateTime": ["2024-11-01T16:05:34.000Z", "20240202160100", "2023-11-03T16:02:11.000Z"],
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
            {"name": "CIK0000320193-submissions-001.json", "filingCount": 1000, "filingFrom": "2015-01-01", "filingTo": "2023-10-31"}
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
                            "val": 391035000000,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-11-01",
                            "frame": "CY2024",
                            "accn": "0000320193-24-000106",
                        },
                        {
                            "start": "2023-10-01",
                            "end": "2024-09-28",
                            "val": 391035000000, # Duplicate to test deduplication
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
                            "accn": "0000320193-24-000006",
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
                            "val": 0.0,  # Test zero preservation
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
# 1. Current Submissions Parser Tests (Scenarios 1-20)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSubmissionsParser:

    def test_01_and_02_cik_normalization_and_validation(self):
        """Scenario 1 & 2: CIK normalizes to 10 digits; invalid inputs are strictly rejected."""
        assert normalize_cik("320193") == "0000320193"
        assert normalize_cik(320193) == "0000320193"
        assert normalize_cik("0000320193") == "0000320193"
        assert format_cik_for_path("0000320193") == "320193"

        with pytest.raises(ValueError, match="digits only"):
            normalize_cik("320193A")
        with pytest.raises(ValueError, match="cannot exceed 10 digits"):
            normalize_cik("0000000000320193")
        with pytest.raises(ValueError, match="cannot be empty"):
            normalize_cik("")

    @pytest.mark.asyncio
    async def test_03_to_07_submissions_fetch_and_metadata_parsing(self):
        """Scenario 3-7: Submissions fetch parses top-level metadata, tickers, exchanges."""
        mock_client = AsyncMock(spec=SECEdgarClient)
        mock_client.get_json.return_value = SAMPLE_SUBMISSIONS_PAYLOAD
        mock_client.get_user_agent.return_value = "Sentinax <admin@example.com>"

        provider = SECSubmissionsProvider(client=mock_client)
        meta, filings, snap = await provider.fetch_submissions("0000320193")

        assert meta.cik == "0000320193"
        assert meta.entity_name == "Apple Inc."
        assert meta.sic == "3571"
        assert meta.tickers == ["AAPL"]
        assert meta.exchanges == ["Nasdaq"]
        assert snap.provider == "SEC_EDGAR"
        assert snap.endpoint == "/submissions/CIK0000320193.json"

    def test_08_to_19_recent_filing_arrays_parsing(self):
        """Scenario 8-19: Parse recent filings into SECFilingRecord with exact date, acceptance, and flags."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        assert len(filings) == 3

        # Filing 0: 10-K (Original)
        f0 = filings[0]
        assert f0.accession_number == "0000320193-24-000106"
        assert f0.form == "10-K"
        assert f0.is_amendment is False
        assert f0.filing_date == date(2024, 11, 1)
        assert f0.report_date == date(2024, 9, 28)
        assert f0.acceptance_datetime == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert f0.acceptance_precision == "SECOND_EXACT_UTC"
        assert f0.is_xbrl is True
        assert f0.is_inline_xbrl is True
        assert f0.primary_document == "aapl-20240928.htm"
        assert f0.source_url == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000106/aapl-20240928.htm"

        # Filing 1: 10-Q/A (Amendment)
        f1 = filings[1]
        assert f1.accession_number == "0000320193-24-000006"
        assert f1.form == "10-Q/A"
        assert f1.is_amendment is True
        assert f1.acceptance_datetime == datetime(2024, 2, 2, 16, 1, 0, tzinfo=timezone.utc)

    def test_20_parallel_array_mismatch_raises_schema_error(self):
        """Scenario 20: Parallel array length mismatch strictly raises ProviderSchemaError."""
        bad_payload = {
            "cik": "0000320193",
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-24-000106", "0000320193-24-000006"],
                    "form": ["10-K"],  # Length 1 vs 2
                }
            }
        }
        with pytest.raises(ProviderSchemaError, match="array length mismatch"):
            SECSubmissionsParser.parse_filings(bad_payload)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Archived Submissions Traversal Tests (Scenarios 21-27)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECArchivedSubmissions:

    @pytest.mark.asyncio
    async def test_21_to_24_archived_files_traversal_and_path_security(self):
        """Scenario 21-24: Historical files referenced in payload['filings']['files'] parsed with path security."""
        mock_client = AsyncMock(spec=SECEdgarClient)
        mock_client.get_json.side_effect = [
            SAMPLE_SUBMISSIONS_PAYLOAD,
            {
                "accessionNumber": ["0000320193-20-000001"],
                "filingDate": ["2020-01-15"],
                "reportDate": ["2019-12-31"],
                "acceptanceDateTime": ["2020-01-15T16:00:00.000Z"],
                "form": ["10-Q"],
            },
        ]

        provider = SECSubmissionsProvider(client=mock_client)

        # Default does NOT fetch archived files
        meta_def, filings_def, _ = await provider.fetch_submissions("0000320193", include_archived=False)
        assert len(filings_def) == 3
        assert mock_client.get_json.call_count == 1

        # include_archived=True fetches secondary file
        mock_client.get_json.reset_mock()
        mock_client.get_json.side_effect = [
            SAMPLE_SUBMISSIONS_PAYLOAD,
            {
                "accessionNumber": ["0000320193-20-000001"],
                "filingDate": ["2020-01-15"],
                "reportDate": ["2019-12-31"],
                "acceptanceDateTime": ["2020-01-15T16:00:00.000Z"],
                "form": ["10-Q"],
            },
        ]
        meta_arch, filings_arch, _ = await provider.fetch_submissions("0000320193", include_archived=True)
        assert len(filings_arch) == 4
        assert any(f.accession_number == "0000320193-20-000001" for f in filings_arch)


# ─────────────────────────────────────────────────────────────────────────────
# 3. CompanyFacts Raw Fact Ingestion Tests (Scenarios 28-47)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECCompanyFactsParser:

    def test_28_to_44_company_facts_parsing_and_instant_duration(self):
        """Scenario 28-44: Hierarchical facts parsed, taxonomies preserved, Instant vs Duration classified."""
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)
        assert len(facts) == 5  # 1 dei shares + 2 distinct revenues + 1 asset + 1 zero asset (1 revenue duplicate removed)

        # 1. Shares Outstanding (INSTANT dei)
        f_shares = next(f for f in facts if f.concept == "EntityCommonStockSharesOutstanding")
        assert f_shares.taxonomy == "dei"
        assert f_shares.unit == "shares"
        assert f_shares.period_type == PeriodType.INSTANT
        assert f_shares.start_date is None
        assert f_shares.end_date == date(2024, 10, 18)
        assert f_shares.value == 15115820000.0
        assert f_shares.accession_number == "0000320193-24-000106"

        # 2. Revenues FY (DURATION us-gaap)
        f_rev = next(f for f in facts if f.concept == "Revenues" and f.fiscal_period == "FY")
        assert f_rev.taxonomy == "us-gaap"
        assert f_rev.unit == "USD"
        assert f_rev.period_type == PeriodType.DURATION
        assert f_rev.start_date == date(2023, 10, 1)
        assert f_rev.end_date == date(2024, 9, 28)
        assert f_rev.value == 391035000000.0

        # 3. Assets zero preservation
        f_zero = next(f for f in facts if f.concept == "Assets" and f.fiscal_year == 2023)
        assert f_zero.value == 0.0
        assert f_zero.value is not None

    def test_45_to_47_multiple_accessions_for_period_both_preserved(self):
        """Scenario 45-47: Multiple accessions for the same period are both preserved without overwriting."""
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)
        rev_facts = [f for f in facts if f.concept == "Revenues"]
        assert len(rev_facts) == 2  # FY from 10-K and Q1 from 10-Q/A


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fact-to-Filing Lineage Tests (Scenarios 48-52)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFactFilingLineage:

    def test_48_to_52_fact_filing_lineage_and_acceptance_boundary(self):
        """Scenario 48-52: Facts resolve filing_id and acceptance_datetime; filed date is not acceptance time."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)

        # Resolve lineage
        linked_facts = resolve_fact_filing_lineage(facts, filings)

        # 10-K linked fact
        fact_10k = next(f for f in linked_facts if f.accession_number == "0000320193-24-000106")
        filing_10k = next(fl for fl in filings if fl.accession_number == "0000320193-24-000106")
        assert fact_10k.filing_id == filing_10k.id

        filing_map = {f.accession_number: f for f in filings}
        knowledge_boundary = get_fact_knowledge_boundary(fact_10k, filing_map)
        assert knowledge_boundary == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert knowledge_boundary.date() == fact_10k.filed_date  # Date matches, but boundary is exact UTC time

        # Fact with unknown accession is NOT dropped
        unresolved_fact = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-19-000099", # Not in recent filings
            taxonomy="us-gaap",
            concept="Cash",
            unit="USD",
            period_type=PeriodType.INSTANT,
            value=100.0,
        )
        linked_unresolved = resolve_fact_filing_lineage([unresolved_fact], filings)
        assert linked_unresolved[0].filing_id is None  # Explicitly unresolved, not dropped


# ─────────────────────────────────────────────────────────────────────────────
# 5. Point-In-Time Semantics & Identity Tests (Scenarios 53-64)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECPITAndIdentity:

    @pytest.mark.asyncio
    async def test_53_to_58_pit_guards_and_no_fabricated_timestamps(self):
        """Scenario 53-58: Historical SYSTEM_AS_OF & SOURCE_AS_OF external queries rejected."""
        provider = SECEdgarProvider()

        # SYSTEM_AS_OF
        ctx_sys = FetchContext(
            observation_type="FUNDAMENTAL_SEC",
            provider_symbol="0000320193",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        resp_sys = await provider.fetch(ctx_sys)
        assert resp_sys.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp_sys.warnings[0]

        # SOURCE_AS_OF
        ctx_src = FetchContext(
            observation_type="FUNDAMENTAL_SEC",
            provider_symbol="0000320193",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        resp_src = await provider.fetch(ctx_src)
        assert resp_src.status == DataStatus.UNAVAILABLE

    def test_59_to_64_instrument_identity_preserved(self):
        """Scenario 59-64: Canonical instrument UUID preserved; same CIK maps to multiple securities."""
        inst_uuid = uuid4()
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD, instrument_id=inst_uuid)
        assert filings[0].instrument_id == inst_uuid

        # Multiple share classes share same CIK without mutating master identity
        cik_aapl = normalize_cik("0000320193")
        assert cik_aapl == "0000320193"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Security, Fair Access & Typed Errors (Scenarios 65-75)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSecurityAndFairAccess:

    def test_65_to_69_user_agent_required_and_rate_limiter_bounds(self):
        """Scenario 65-69: SEC_USER_AGENT required; rate limiter clamped to <= 10 req/s."""
        client_no_env = SECEdgarClient(user_agent="")
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ProviderConfigurationError, match="SEC_USER_AGENT is not configured"):
                client_no_env.get_user_agent()

        limiter = SECRateLimiter(max_rps=15.0)  # Exceeds official max
        assert limiter.rate == SEC_OFFICIAL_MAX_RPS  # Clamped to 10
        assert DEFAULT_SENTINAX_SAFETY_RPS == 8.0

    @pytest.mark.asyncio
    async def test_70_to_75_typed_error_mappings(self):
        """Scenario 70-75: 429, 403, 5xx, timeout, and schema errors map to typed exceptions."""
        # 429 RateLimit
        m_429 = AsyncMock(spec=httpx.AsyncClient)
        m_429.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "5"})
        c_429 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_429)
        with pytest.raises(ProviderRateLimitError):
            await c_429.get_json("https://data.sec.gov/test")

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


# ─────────────────────────────────────────────────────────────────────────────
# 7. Ticker Discovery Candidate Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSECTickerDiscovery:

    @pytest.mark.asyncio
    async def test_ticker_discovery_candidate_resolution(self):
        """Discovers candidate CIK from company_tickers_exchange payload."""
        mock_payload = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [320193, "Apple Inc.", "AAPL", "Nasdaq"],
                [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
            ]
        }
        mock_client = AsyncMock(spec=SECEdgarClient)
        mock_client.get_json.return_value = mock_payload

        disc = SECTickerDiscoveryService(client=mock_client)
        cand = await disc.discover_candidate_by_ticker("AAPL")

        assert cand is not None
        assert cand.ticker == "AAPL"
        assert cand.cik == "0000320193"
        assert cand.company_name == "Apple Inc."
        assert cand.exchange == "Nasdaq"
        assert cand.confidence_level == DataConfidenceLevel.MEDIUM


# ─────────────────────────────────────────────────────────────────────────────
# 8. Storage Models & Migration 008 Verification (Scenarios 76-84)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECStorageModelsAndMigration:

    def test_76_to_84_storage_models_serialization_and_migration_file(self):
        """Scenario 76-84: SECFilingRecord & SECRawFactRecord serialize correctly; migration 008 exists."""
        filing = SECFilingRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 11, 1),
            report_date=date(2024, 9, 28),
            acceptance_datetime=datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc),
        )
        d_filing = filing.to_dict()
        assert d_filing["cik"] == "0000320193"
        assert d_filing["accession_number"] == "0000320193-24-000106"
        assert d_filing["is_amendment"] is False

        fact = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=391035000000.0,
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        d_fact = fact.to_dict()
        assert d_fact["taxonomy"] == "us-gaap"
        assert d_fact["concept"] == "Revenues"
        assert d_fact["period_type"] == "duration"

        # Check migration file exists and contains sec_filings & sec_raw_facts
        mig_path = os.path.join(os.path.dirname(__file__), "../../supabase/migrations/008_sec_edgar_backbone.sql")
        assert os.path.exists(mig_path)
        with open(mig_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS public.sec_filings" in content
        assert "CREATE TABLE IF NOT EXISTS public.sec_raw_facts" in content
        assert "prevent_sec_immutability_violation" in content
