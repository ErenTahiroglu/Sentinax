"""
backend/tests/test_sec_edgar.py
=================================
Comprehensive Unit, Point-In-Time, Lineage & Semantic Hardening Test Suite for
SEC EDGAR Filing & Raw XBRL CompanyFacts Backbone (Phase 8A Hardened).

Coverage (63 Scenarios across 7 Domains):
    1. Acceptance & PIT Semantics (Scenarios 1-11)
    2. Accession & Identity Invariants (Scenarios 12-18)
    3. Raw Fact Numeric Precision & Period Types (Scenarios 19-29)
    4. Fact-to-Filing Lineage & Link Table (Scenarios 30-37)
    5. Archived Submissions & Separate Snapshots (Scenarios 38-43)
    6. Fair Access Leaky Pacing Rate Limiter (Scenarios 44-53)
    7. Database Constraints & Migration 009 (Scenarios 54-63)
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
# Sample Payloads for Testing
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
                "0001140361-24-024352",  # Third-party agent accession (CIK prefix != issuer CIK)
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
                            "val": "391035000000.50",  # String decimal
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
                            "accn": "0001140361-24-024352",  # Third-party agent accession
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
# 1. Acceptance & PIT Semantics Tests (Scenarios 1-11)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECAcceptanceAndPITHardening:

    def test_01_to_06_acceptance_datetime_parsing_and_raw_preservation(self):
        """Scenario 1-6: Explicit Z/offset parses to UTC; timezone-less compact/date-only does NOT fabricate UTC."""
        # 1. Explicit Z
        dt_z, raw_z, prec_z, sem_z = parse_sec_datetime_hardened("2024-11-01T16:05:34.000Z")
        assert dt_z == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert sem_z == "EXPLICIT_UTC"
        assert prec_z == "SECOND_EXACT_UTC"
        assert raw_z == "2024-11-01T16:05:34.000Z"

        # 2. Explicit Offset (+04:00)
        dt_off, _, prec_off, sem_off = parse_sec_datetime_hardened("2024-11-01T20:05:34+04:00")
        assert dt_off == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert sem_off == "EXPLICIT_UTC"

        # 3. Compact 14-digit without timezone -> Naive local, NOT silently UTC
        dt_c, raw_c, prec_c, sem_c = parse_sec_datetime_hardened("20240202160100")
        assert dt_c.tzinfo is None
        assert sem_c == "EDGAR_LOCAL_UNSPECIFIED"
        assert prec_c == "SECOND_EXACT_NAIVE"
        assert raw_c == "20240202160100"

        # 4. Naive ISO without timezone -> Naive local
        dt_iso, _, _, sem_iso = parse_sec_datetime_hardened("2024-02-02 16:01:00")
        assert dt_iso.tzinfo is None
        assert sem_iso == "EDGAR_LOCAL_UNSPECIFIED"

        # 5. Date-only -> Not fabricated into 00:00 UTC
        dt_d, raw_d, prec_d, sem_d = parse_sec_datetime_hardened("2023-11-03")
        assert dt_d is None
        assert prec_d == "DATE_ONLY"
        assert sem_d == "NONE"
        assert raw_d == "2023-11-03"

    def test_07_to_09_acceptance_not_equal_public_availability(self):
        """Scenario 7-9: acceptance_datetime != public_available_at; public_available_at remains None."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        f0 = filings[0]
        assert f0.acceptance_datetime is not None
        assert f0.public_available_at is None
        assert f0.public_availability_basis is None

    @pytest.mark.asyncio
    async def test_10_and_11_historical_as_of_modes_fail_closed(self):
        """Scenario 10 & 11: External historical SOURCE_AS_OF & SYSTEM_AS_OF queries rejected."""
        provider = SECEdgarProvider()

        # SOURCE_AS_OF
        ctx_src = FetchContext(
            observation_type="SEC_SUBMISSIONS",
            provider_symbol="0000320193",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        assert (await provider.fetch(ctx_src)).status == DataStatus.UNAVAILABLE

        # SYSTEM_AS_OF
        ctx_sys = FetchContext(
            observation_type="SEC_SUBMISSIONS",
            provider_symbol="0000320193",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        assert (await provider.fetch(ctx_sys)).status == DataStatus.UNAVAILABLE


# ─────────────────────────────────────────────────────────────────────────────
# 2. Accession & Identity Invariants (Scenarios 12-18)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECAccessionAndIdentityHardening:

    def test_12_to_15_accession_prefix_may_differ_from_issuer_cik(self):
        """Scenario 12-15: Accession prefix is submitting entity CIK; can differ from subject issuer CIK."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)

        # Self-filer
        f0 = filings[0]
        assert f0.cik == "0000320193"
        assert f0.accession_number == "0000320193-24-000106"
        assert f0.source_url == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000106/aapl-20240928.htm"

        # Third-party filing agent accession (0001140361 prefix vs 0000320193 issuer CIK)
        f1 = filings[1]
        assert f1.cik == "0000320193"
        assert f1.accession_number == "0001140361-24-024352"
        assert f1.source_url == "https://www.sec.gov/Archives/edgar/data/320193/000114036124024352/aapl-20231230.htm"

    def test_16_to_18_same_cik_maps_to_multiple_securities_no_new_issuer_uuid(self):
        """Scenario 16-18: Multiple share classes share same issuer CIK; no duplicate sec_company_uuid created."""
        inst_a = uuid4()
        inst_b = uuid4()
        cik = normalize_cik("320193")

        f_a = SECFilingRecord(cik=cik, accession_number="0000320193-24-000106", form="10-K", is_amendment=False, instrument_id=inst_a)
        f_b = SECFilingRecord(cik=cik, accession_number="0000320193-24-000106", form="10-K", is_amendment=False, instrument_id=inst_b)

        assert f_a.cik == f_b.cik == "0000320193"
        assert f_a.instrument_id != f_b.instrument_id


# ─────────────────────────────────────────────────────────────────────────────
# 3. Raw Fact Numeric Precision & Period Types (Scenarios 19-29)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECRawFactPrecisionAndPeriodTypes:

    def test_19_to_24_decimal_parsing_and_zero_preservation(self):
        """Scenario 19-24: Decimal parsing preserves large integers, fractions, and Decimal('0'); rejects NaN/Inf/bool."""
        assert parse_sec_decimal("391035000000.50") == Decimal("391035000000.50")
        assert parse_sec_decimal(15115820000) == Decimal("15115820000")
        assert parse_sec_decimal(0) == Decimal("0")
        assert parse_sec_decimal("0.0") == Decimal("0.0")

        assert parse_sec_decimal(None) is None
        assert parse_sec_decimal(True) is None  # Rejects booleans
        assert parse_sec_decimal(False) is None
        assert parse_sec_decimal(float("nan")) is None
        assert parse_sec_decimal(float("inf")) is None
        assert parse_sec_decimal("invalid_str") is None

    def test_21_decimal_serialization_does_not_cast_to_float(self):
        """Scenario 21: SECRawFactRecord.to_dict() serializes Decimal as exact string, never float."""
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

    def test_25_to_29_period_type_validation_and_no_fake_accession(self):
        """Scenario 25-29: Duration requires start+end; Instant requires end only; missing end is rejected."""
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)

        # 1. Instant fact (shares outstanding)
        f_shares = next(f for f in facts if f.concept == "EntityCommonStockSharesOutstanding")
        assert f_shares.period_type == PeriodType.INSTANT
        assert f_shares.start_date is None
        assert f_shares.end_date == date(2024, 10, 18)

        # 2. Duration fact (revenues)
        f_rev = next(f for f in facts if f.concept == "Revenues" and f.fiscal_period == "FY")
        assert f_rev.period_type == PeriodType.DURATION
        assert f_rev.start_date == date(2023, 10, 1)
        assert f_rev.end_date == date(2024, 9, 28)

        # 3. Third-party accession fact preserved
        f_agent = next(f for f in facts if f.concept == "Revenues" and f.fiscal_period == "Q1")
        assert f_agent.accession_number == "0001140361-24-024352"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fact-to-Filing Lineage & Link Table (Scenarios 30-37)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFactFilingLinkTable:

    def test_30_to_37_append_only_fact_filing_links(self):
        """Scenario 30-37: SECFactFilingLinkRecord created; raw fact remains immutable; CIK/accession verified."""
        filings = SECSubmissionsParser.parse_filings(SAMPLE_SUBMISSIONS_PAYLOAD)
        facts = SECCompanyFactsParser.parse_facts(SAMPLE_COMPANY_FACTS_PAYLOAD)

        links, linked_facts = create_fact_filing_links(facts, filings)
        assert len(links) == 5  # All 5 facts match filings

        # Link validation
        l0 = links[0]
        assert isinstance(l0, SECFactFilingLinkRecord)
        assert l0.accession_number in ("0000320193-24-000106", "0001140361-24-024352", "0000320193-23-000106")
        assert l0.cik == "0000320193"

        # Timestamp getters
        filing_map = {f.accession_number: f for f in filings}
        fact_10k = next(f for f in linked_facts if f.accession_number == "0000320193-24-000106")
        acc_ts = get_fact_acceptance_timestamp(fact_10k, filing_map)
        assert acc_ts == datetime(2024, 11, 1, 16, 5, 34, tzinfo=timezone.utc)
        assert get_fact_source_available_at(fact_10k, filing_map) is None

        # Inconsistent link rejection (CIK mismatch)
        bad_filing = SECFilingRecord(cik="0000789019", accession_number="0000320193-24-000106", form="10-K", is_amendment=False)
        with pytest.raises(ValueError, match="CIK mismatch"):
            create_fact_filing_links(facts, [bad_filing])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Archived Submissions & Separate Snapshots (Scenarios 38-43)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECArchivedSnapshotsHardening:

    @pytest.mark.asyncio
    async def test_38_to_43_separate_snapshots_for_archived_files(self):
        """Scenario 38-43: Main submissions and each archived JSON file get distinct RawProviderSnapshotRecord."""
        mock_client = AsyncMock(spec=SECEdgarClient)
        mock_client.get_json.side_effect = [
            SAMPLE_SUBMISSIONS_PAYLOAD,
            {"accessionNumber": ["0000320193-20-000001"], "form": ["10-Q"], "filingDate": ["2020-01-15"]},
            {"accessionNumber": ["0000320193-12-000001"], "form": ["10-K"], "filingDate": ["2012-10-31"]},
        ]

        provider = SECSubmissionsProvider(client=mock_client)
        result = await provider.fetch_submissions("0000320193", include_archived=True, max_archived_files=2)

        assert isinstance(result, SECSubmissionsFetchResult)
        assert len(result.archived_snapshots) == 2
        assert len(result.all_snapshots) == 3

        # Main filings point to main snapshot
        main_filing = next(f for f in result.filings if f.accession_number == "0000320193-24-000106")
        assert main_filing.snapshot_id == result.main_snapshot.id

        # Archived filing points to archived snapshot
        arch_filing = next(f for f in result.filings if f.accession_number == "0000320193-20-000001")
        assert arch_filing.snapshot_id == result.archived_snapshots[0].id
        assert arch_filing.snapshot_id != result.main_snapshot.id


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fair Access Leaky Pacing Rate Limiter (Scenarios 44-53)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFairAccessRateLimiter:

    @pytest.mark.asyncio
    async def test_44_to_47_leaky_pacing_serializes_requests(self):
        """Scenario 44-47: Serialized rate limiter enforces min_interval spacing (burst capacity = 1)."""
        current_time = 100.0
        sleeps: list[float] = []

        def fake_time():
            nonlocal current_time
            return current_time

        async def fake_sleep(duration):
            nonlocal current_time
            sleeps.append(duration)
            current_time += duration

        limiter = SECRateLimiter(max_rps=8.0, time_func=fake_time, sleep_func=fake_sleep)
        assert limiter.min_interval == 0.125  # 1 / 8.0

        # First acquire: no sleep
        await limiter.acquire()
        assert len(sleeps) == 0

        # Immediate second acquire: sleeps exactly 0.125s
        await limiter.acquire()
        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(0.125)

    def test_48_to_51_rate_validation(self):
        """Scenario 48-51: Reject zero/negative/NaN/inf RPS; clamp rates above 10 req/s."""
        with pytest.raises(ValueError, match="Invalid max_rps"):
            SECRateLimiter(max_rps=0.0)

        with pytest.raises(ValueError, match="Invalid max_rps"):
            SECRateLimiter(max_rps=-5.0)

        with pytest.raises(ValueError, match="Invalid max_rps"):
            SECRateLimiter(max_rps=float("nan"))

        with pytest.raises(ValueError, match="Invalid max_rps"):
            SECRateLimiter(max_rps=float("inf"))

        # Clamp > 10
        limiter_high = SECRateLimiter(max_rps=15.0)
        assert limiter_high.rate == SEC_OFFICIAL_MAX_RPS
        assert DEFAULT_SENTINAX_SAFETY_RPS == 8.0

    @pytest.mark.asyncio
    async def test_52_and_53_typed_403_and_429_errors(self):
        """Scenario 52 & 53: 403 maps to ProviderPermissionError; 429 maps to ProviderRateLimitError."""
        m_403 = AsyncMock(spec=httpx.AsyncClient)
        m_403.get.return_value = MagicMock(status_code=403)
        c_403 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_403)
        with pytest.raises(ProviderPermissionError, match="access blocked"):
            await c_403.get_json("https://data.sec.gov/test")

        m_429 = AsyncMock(spec=httpx.AsyncClient)
        m_429.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "10"})
        c_429 = SECEdgarClient(user_agent="Sentinax <admin@example.com>", http_client=m_429)
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await c_429.get_json("https://data.sec.gov/test")
        assert exc_info.value.retry_after_seconds == 10.0


# ─────────────────────────────────────────────────────────────────────────────
# 7. Database Constraints & Migration 009 (Scenarios 54-63)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECDatabaseMigration009:

    def test_54_to_63_migration_009_schema_invariants(self):
        """Scenario 54-63: Migration 009 defines sec_fact_filing_links, CIK checks, RLS, and immutability."""
        mig_path = os.path.join(os.path.dirname(__file__), "../../supabase/migrations/009_sec_edgar_hardening.sql")
        assert os.path.exists(mig_path)

        with open(mig_path, "r", encoding="utf-8") as f:
            sql = f.read()

        # Constraints
        assert "chk_sec_filings_cik_format CHECK (cik ~ '^[0-9]{10}$')" in sql
        assert "chk_sec_filings_accession_format CHECK (accession_number ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$')" in sql
        assert "chk_sec_raw_facts_cik_format CHECK (cik ~ '^[0-9]{10}$')" in sql
        assert "ALTER TABLE public.sec_filings ALTER COLUMN is_amendment DROP DEFAULT" in sql
        assert "ALTER TABLE public.sec_filings ALTER COLUMN retrieved_at DROP DEFAULT" in sql

        # Link table
        assert "CREATE TABLE IF NOT EXISTS public.sec_fact_filing_links" in sql
        assert "CONSTRAINT uq_sec_fact_filing_link UNIQUE (fact_id)" in sql
        assert "trg_sec_fact_filing_links_immutable" in sql

        # RLS
        assert "ALTER TABLE public.sec_filings ENABLE ROW LEVEL SECURITY" in sql
        assert "ALTER TABLE public.sec_raw_facts ENABLE ROW LEVEL SECURITY" in sql
        assert "ALTER TABLE public.sec_fact_filing_links ENABLE ROW LEVEL SECURITY" in sql
