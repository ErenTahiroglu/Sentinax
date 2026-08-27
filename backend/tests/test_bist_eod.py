"""
backend/tests/test_bist_eod.py
==============================
Test suite for Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 market data backbone (Phase 9A).

Strict Invariants Verified:
    - Zero external network in tests (pytest-socket active).
    - Decimal exactness for all monetary and volume fields.
    - Missing optional fields remain None (missing != zero).
    - Missing required fields fail closed as SCHEMA_DRIFT.
    - Malformed numeric or negative prices/volumes fail closed.
    - Duplicate rows in same bulletin are deduplicated or flagged.
    - OHLC integrity contradictions (High < Low, High < Close, etc.) rejected.
    - Suffix .E stripped for equities, but .S1 strictly preserved for ALTIN.S1.
    - ALTIN.S1 modeled as COMMODITY_CERTIFICATE with 0.01g gold and 0.995 purity facts.
    - Raw snapshot first + PIT timestamp semantics (trade_date != retrieved_at).
    - Non-trading days distinguished from transport failures.
    - Provider contract conformance and serialization.
"""

import io
import json
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from backend.engine.private.bist import (
    ALTIN_S1_ASSET_CLASS,
    ALTIN_S1_CANONICAL_NAME,
    ALTIN_S1_CERTIFICATE_REPRESENTATION_GRAMS,
    ALTIN_S1_CURRENCY,
    ALTIN_S1_INSTRUMENT_TYPE,
    ALTIN_S1_ISSUER,
    ALTIN_S1_PURITY,
    ALTIN_S1_SYMBOL,
    ALTIN_S1_UNDERLYING,
    BISTBulletinParser,
    BISTBulletinSnapshot,
    BISTCapability,
    BISTEODObservation,
    BISTMarketSegment,
    BISTObservationStatus,
    BISTSchemaDriftError,
    clean_bist_symbol,
    parse_bist_date,
    parse_bist_decimal,
    parse_bist_int,
)
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.identity import (
    InstrumentRecord,
    InstrumentResolverService,
    ProviderAliasRecord,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.providers.bist_eod import BISTEODProvider


# ─────────────────────────────────────────────────────────────────────────────
# Sample Realistic BISTECH Bulletin Fixture (CSV & Semicolon Delimited)
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_BIST_BULLETIN_CSV = """BULTEN_TARIHI;HISSE_KODU;PAZAR;ONCEKI_KAPANIS_FIYATI;ACILIS_FIYATI;EN_YUKSEK_FIYAT;EN_DUSUK_FIYAT;KAPANIS_FIYATI;AGIRLIKLI_ORTALAMA_FIYAT;ISLEM_MIKTARI;ISLEM_HACMI;SOZLESME_SAYISI
2024-10-01;THYAO.E;YILDIZ PAZAR;280.50;281.00;285.75;279.50;284.25;283.40;15420100;4370076340.00;45120
2024-10-01;GARAN.E;YILDIZ PAZAR;115.20;115.50;118.00;114.80;117.30;116.85;25100000;2932935000.00;32400
2024-10-01;ALTIN.S1;EMTIA SERTIFIKALARI;31.20;31.25;31.80;31.10;31.65;31.52;5400120;170211782.40;18500
2024-10-01;UNKNOWN.E;ANA PAZAR;10.00;10.10;10.50;9.90;10.20;10.15;100000;1015000.00;500
"""

SAMPLE_TURKISH_FORMATTED_CSV = """BÜLTEN TARİHİ;HİSSE KODU;PAZAR;ÖNCEKİ KAPANIŞ FİYATI;AÇILIŞ FİYATI;EN YÜKSEK FİYAT;EN DÜŞÜK FİYAT;KAPANIŞ FİYATI;AĞIRLIKLI ORTALAMA FİYAT;İŞLEM MİKTARI;İŞLEM HACMİ;SÖZLEŞME SAYISI
01.10.2024;THYAO.E;YILDIZ PAZAR;280,50;281,00;285,75;279,50;284,25;283,40;15.420.100;4.370.076.340,00;45.120
01.10.2024;ALTIN.S1;EMTIA SERTIFIKALARI;31,20;31,25;31,80;31,10;31,65;31,52;5.400.120;170.211.782,40;18.500
"""


def _create_sample_zip_bytes(csv_content: str = SAMPLE_BIST_BULLETIN_CSV) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("gunluk_bulten_20241001.csv", csv_content.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def sample_resolver() -> InstrumentResolverService:
    resolver = InstrumentResolverService()

    # THYAO Master Instrument
    thyao_id = uuid4()
    thyao_inst = InstrumentRecord(
        id=thyao_id,
        canonical_name="Türk Hava Yolları A.O.",
        asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.BIST_STOCK,
        currency=Currency.TRY,
        mic="XIST",
        isin="TRATHYAO91M9",
        valid_from=date(1990, 1, 1),
    )
    resolver.register_instrument(thyao_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=thyao_id,
            provider="BIST",
            provider_symbol="THYAO",
            valid_from=date(1990, 1, 1),
        )
    )

    # ALTIN.S1 Master Instrument
    altin_id = uuid4()
    altin_inst = InstrumentRecord(
        id=altin_id,
        canonical_name=ALTIN_S1_CANONICAL_NAME,
        asset_class=ALTIN_S1_ASSET_CLASS,
        instrument_type=ALTIN_S1_INSTRUMENT_TYPE,
        currency=ALTIN_S1_CURRENCY,
        mic="XIST",
        isin="TRTDPHNE0013",
        valid_from=date(2022, 11, 21),
    )
    resolver.register_instrument(altin_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=altin_id,
            provider="BIST",
            provider_symbol=ALTIN_S1_SYMBOL,
            valid_from=date(2022, 11, 21),
        )
    )

    return resolver


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parser & Decimal Exactness Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTBulletinParser:

    def test_01_official_fixture_parses_successfully(self, sample_resolver):
        """Scenario 1: Official sample fixture parses clean observations."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=SAMPLE_BIST_BULLETIN_CSV,
            filename_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        assert len(observations) == 4
        symbols = [o.symbol for o in observations]
        assert symbols == ["THYAO", "GARAN", "ALTIN.S1", "UNKNOWN"]

        # Check THYAO observation
        thyao = observations[0]
        assert thyao.symbol == "THYAO"
        assert thyao.trade_date == date(2024, 10, 1)
        assert thyao.close == Decimal("284.25")
        assert thyao.open == Decimal("281.00")
        assert thyao.high == Decimal("285.75")
        assert thyao.low == Decimal("279.50")
        assert thyao.previous_close == Decimal("280.50")
        assert thyao.weighted_average == Decimal("283.40")
        assert thyao.volume == Decimal("15420100")
        assert thyao.turnover == Decimal("4370076340.00")
        assert thyao.trade_count == 45120
        assert thyao.status == BISTObservationStatus.VALID
        assert thyao.instrument_id is not None
        assert thyao.asset_class == AssetClass.EQUITY
        assert thyao.instrument_type == InstrumentType.BIST_STOCK

    def test_02_decimal_exactness_and_turkish_locale_formatting(self):
        """Scenario 2: Turkish dot-thousands/comma-decimals parsed to exact Decimal."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=SAMPLE_TURKISH_FORMATTED_CSV,
            filename_date=date(2024, 10, 1),
        )
        assert len(observations) == 2
        thyao = observations[0]
        assert isinstance(thyao.close, Decimal)
        assert thyao.close == Decimal("284.25")
        assert thyao.turnover == Decimal("4370076340.00")
        assert thyao.trade_count == 45120

    def test_03_missing_optional_fields_remain_none(self):
        """Scenario 3: Missing optional fields (e.g. open, turnover) remain None (not 0)."""
        csv_minimal = """BULTEN_TARIHI;HISSE_KODU;KAPANIS_FIYATI
2024-10-01;THYAO;284.25
"""
        observations = BISTBulletinParser.parse_bulletin_text(raw_text=csv_minimal)
        assert len(observations) == 1
        obs = observations[0]
        assert obs.close == Decimal("284.25")
        assert obs.open is None
        assert obs.high is None
        assert obs.low is None
        assert obs.volume is None
        assert obs.turnover is None
        assert obs.trade_count is None

    def test_04_missing_required_fields_raises_schema_drift(self):
        """Scenario 4: Missing required column (e.g. close) fails closed as BISTSchemaDriftError."""
        csv_no_close = """BULTEN_TARIHI;HISSE_KODU;OPEN_PRICE
2024-10-01;THYAO;280.00
"""
        with pytest.raises(BISTSchemaDriftError, match="missing required columns"):
            BISTBulletinParser.parse_bulletin_text(raw_text=csv_no_close)

    def test_05_malformed_numeric_returns_invalid_observation(self):
        """Scenario 5: Malformed numeric close price flags observation as INVALID_OBSERVATION."""
        csv_bad_price = """BULTEN_TARIHI;HISSE_KODU;KAPANIS_FIYATI
2024-10-01;THYAO;CORRUPTED_PRICE
"""
        observations = BISTBulletinParser.parse_bulletin_text(raw_text=csv_bad_price)
        assert len(observations) == 1
        obs = observations[0]
        assert obs.status == BISTObservationStatus.INVALID_OBSERVATION
        assert any("Error parsing close price" in d for d in obs.diagnostics)

    def test_06_duplicate_row_handling(self, sample_resolver):
        """Scenario 6: Identical duplicate row is idempotent; conflicting duplicate is marked invalid."""
        csv_dup = """BULTEN_TARIHI;HISSE_KODU;KAPANIS_FIYATI;ISLEM_MIKTARI
2024-10-01;THYAO;284.25;1000
2024-10-01;THYAO;284.25;1000
2024-10-01;GARAN;117.30;500
2024-10-01;GARAN;120.00;500
"""
        observations = BISTBulletinParser.parse_bulletin_text(raw_text=csv_dup, resolver=sample_resolver)
        # THYAO identical duplicate -> 1 observation
        thyao_obs = [o for o in observations if o.symbol == "THYAO"]
        assert len(thyao_obs) == 1
        assert thyao_obs[0].status == BISTObservationStatus.VALID

        # GARAN conflicting duplicate -> second is marked INVALID_OBSERVATION
        garan_obs = [o for o in observations if o.symbol == "GARAN"]
        assert len(garan_obs) == 2
        assert garan_obs[1].status == BISTObservationStatus.INVALID_OBSERVATION
        assert any("Conflicting duplicate" in d for d in garan_obs[1].diagnostics)

    def test_07_ohlc_contradiction_rejected(self):
        """Scenario 7: High < Low or High < Close fails closed as INVALID_OBSERVATION."""
        csv_ohlc_bad = """BULTEN_TARIHI;HISSE_KODU;ACILIS_FIYATI;EN_YUKSEK_FIYAT;EN_DUSUK_FIYAT;KAPANIS_FIYATI
2024-10-01;BAD1;280.00;270.00;290.00;284.00
2024-10-01;BAD2;280.00;285.00;275.00;290.00
2024-10-01;BAD3;280.00;285.00;282.00;275.00
"""
        observations = BISTBulletinParser.parse_bulletin_text(raw_text=csv_ohlc_bad)
        assert len(observations) == 3
        for obs in observations:
            assert obs.status == BISTObservationStatus.INVALID_OBSERVATION
            assert len(obs.diagnostics) > 0

    def test_08_unknown_symbol_unresolved_identity(self, sample_resolver):
        """Scenario 8: Unknown symbol in bulletin retains UNRESOLVED_IDENTITY and instrument_id=None."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=SAMPLE_BIST_BULLETIN_CSV,
            filename_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        unknown = [o for o in observations if o.symbol == "UNKNOWN"][0]
        assert unknown.status == BISTObservationStatus.UNRESOLVED_IDENTITY
        assert unknown.instrument_id is None
        assert unknown.confidence_level == DataConfidenceLevel.MEDIUM

    def test_09_s1_suffix_preserved_and_e_stripped(self):
        """Scenario 9: Suffix .E is stripped for equities, but .S1 is strictly preserved."""
        assert clean_bist_symbol("THYAO.E") == "THYAO"
        assert clean_bist_symbol("GARAN.E") == "GARAN"
        assert clean_bist_symbol("ALTIN.S1") == "ALTIN.S1"
        assert clean_bist_symbol("ALTIN.S1.E") == "ALTIN.S1"
        assert clean_bist_symbol("  akbnk.e  ") == "AKBNK"

    def test_10_altin_s1_resolves_to_commodity_certificate(self, sample_resolver):
        """Scenario 10: ALTIN.S1 resolves with COMMODITY_CERTIFICATE instrument type and master UUID."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=SAMPLE_BIST_BULLETIN_CSV,
            filename_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        altin = [o for o in observations if o.symbol == "ALTIN.S1"][0]
        assert altin.symbol == "ALTIN.S1"
        assert altin.asset_class == AssetClass.COMMODITY
        assert altin.instrument_type == InstrumentType.COMMODITY_CERTIFICATE
        assert altin.currency == Currency.TRY
        assert altin.instrument_id is not None
        assert altin.status == BISTObservationStatus.VALID

    def test_11_zip_archive_fixture_parsing(self, sample_resolver):
        """Scenario 11: ZIP archive containing CSV bulletin is cleanly extracted and parsed."""
        zip_bytes = _create_sample_zip_bytes()
        observations = BISTBulletinParser.parse_bulletin_bytes(
            raw_bytes=zip_bytes,
            filename="bulten_20241001.zip",
            filename_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        assert len(observations) == 4
        assert observations[0].symbol == "THYAO"
        assert observations[2].symbol == "ALTIN.S1"

    def test_12_negative_values_rejected(self):
        """Scenario 12: Negative prices or volumes are flagged as INVALID_OBSERVATION."""
        csv_neg = """BULTEN_TARIHI;HISSE_KODU;KAPANIS_FIYATI;ISLEM_MIKTARI
2024-10-01;NEG1;-284.25;1000
2024-10-01;NEG2;284.25;-500
"""
        observations = BISTBulletinParser.parse_bulletin_text(raw_text=csv_neg)
        assert len(observations) == 2
        assert observations[0].status == BISTObservationStatus.INVALID_OBSERVATION
        assert observations[1].status == BISTObservationStatus.INVALID_OBSERVATION


# ─────────────────────────────────────────────────────────────────────────────
# 2. ALTIN.S1 Economic Definition Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestALTINS1EconomicDefinition:

    def test_21_exact_symbol_altin_s1(self):
        """Scenario 21: Symbol is strictly ALTIN.S1 (distinct from ALTIN)."""
        assert ALTIN_S1_SYMBOL == "ALTIN.S1"
        assert clean_bist_symbol("ALTIN.S1") == "ALTIN.S1"

    def test_22_metadata_certificate_representation_001_gram(self):
        """Scenario 22: Certificate representation fact is exactly 0.01 gram."""
        assert ALTIN_S1_CERTIFICATE_REPRESENTATION_GRAMS == Decimal("0.01")

    def test_23_metadata_purity_0995(self):
        """Scenario 23: Gold certificate purity fact is exactly 0.995."""
        assert ALTIN_S1_PURITY == Decimal("0.995")
        assert ALTIN_S1_ISSUER == "T.C. Hazine ve Maliye Bakanlığı Darphane ve Damga Matbaası"
        assert ALTIN_S1_UNDERLYING == "gold"

    def test_24_market_price_comes_from_bulletin(self, sample_resolver):
        """Scenario 24: ALTIN.S1 price is the exchange-traded market price from the bulletin."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=SAMPLE_BIST_BULLETIN_CSV,
            resolver=sample_resolver,
        )
        altin = [o for o in observations if o.symbol == "ALTIN.S1"][0]
        assert altin.close == Decimal("31.65")
        assert altin.high == Decimal("31.80")
        assert altin.low == Decimal("31.10")

    def test_25_no_synthetic_gram_gold_fair_price(self):
        """Scenario 25: Phase 9A does not compute synthetic fair value formulas."""
        # Verify no synthetic formula attribute exists in observation
        obs = BISTEODObservation(
            symbol="ALTIN.S1",
            trade_date=date(2024, 10, 1),
            close=Decimal("31.65"),
        )
        assert not hasattr(obs, "fair_value")
        assert not hasattr(obs, "synthetic_price")

    def test_26_no_premium_discount_calculation(self):
        """Scenario 26: Phase 9A does not compute premium/discount metrics."""
        obs = BISTEODObservation(
            symbol="ALTIN.S1",
            trade_date=date(2024, 10, 1),
            close=Decimal("31.65"),
        )
        assert not hasattr(obs, "premium")
        assert not hasattr(obs, "discount")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provider Fetch, PIT & Error Handling Tests (Mocked Transport)
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTEODProvider:

    @pytest.mark.asyncio
    async def test_31_provider_classification_and_capabilities(self):
        """Scenario 31: Provider is classified as YELLOW with TIER_2_EXCHANGE quality."""
        provider = BISTEODProvider()
        assert provider.provider_name == "BIST_EOD"
        assert provider.source_quality == SourceTier.TIER_2_EXCHANGE
        assert provider.access_status == ProviderAccessStatus.YELLOW
        assert provider.official_source is True
        assert provider.developer_api is False
        assert provider.sla_guaranteed is False
        assert BISTCapability.CURRENT_DAILY_PUBLIC in provider.capabilities
        assert BISTCapability.HISTORICAL_DATASTORE_RESTRICTED in provider.capabilities

    @pytest.mark.asyncio
    async def test_32_fetch_daily_bulletin_success(self, sample_resolver):
        """Scenario 32: fetch_daily_bulletin returns raw snapshot and normalized observations."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = _create_sample_zip_bytes()
        mock_resp.headers = {"content-type": "application/zip"}
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, resolver=sample_resolver)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        assert snapshot.http_status == 200
        assert len(snapshot.payload_hash) == 64
        assert len(observations) == 4
        assert snapshot.trade_date == date(2024, 10, 1)
        assert isinstance(snapshot.retrieved_at, datetime)

    @pytest.mark.asyncio
    async def test_33_non_trading_weekend_distinguished_from_failure(self):
        """Scenario 33: Weekend dates return NON_TRADING_DAY diagnostic without making HTTP requests."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        provider = BISTEODProvider(http_client=mock_client)

        # 2024-10-05 is Saturday
        saturday = date(2024, 10, 5)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=saturday)

        assert snapshot.http_status == 200
        assert len(observations) == 0
        assert any("NON_TRADING_DAY: Weekend" in d for d in snapshot.diagnostics)
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_34_404_historical_restricted_to_datastore(self):
        """Scenario 34: 404 response on older date returns HISTORICAL_DATASTORE_RESTRICTED."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 404
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client)
        historical_date = date(2020, 1, 15)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=historical_date)

        assert snapshot.http_status == 404
        assert len(observations) == 0
        assert any("HISTORICAL_DATASTORE_RESTRICTED" in d for d in snapshot.diagnostics)

    @pytest.mark.asyncio
    async def test_35_fetch_context_single_symbol(self, sample_resolver):
        """Scenario 35: fetch(context) with provider_symbol returns matched instrument."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = _create_sample_zip_bytes()
        mock_resp.headers = {"content-type": "application/zip"}
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, resolver=sample_resolver)
        ctx = FetchContext(
            observation_type="BIST_EOD_PRICE",
            provider_symbol="ALTIN.S1",
            effective_date=date(2024, 10, 1),
        )
        response = await provider.fetch(ctx)

        assert response.status == DataStatus.COMPLETE
        assert response.provider_name == "BIST_EOD"
        assert response.effective_date == date(2024, 10, 1)
        assert response.provider_symbol == "ALTIN.S1"
        assert response.raw["close"] == "31.65"
        assert response.published_at is None  # BIST bulletin does not expose sub-daily publication timestamp

    @pytest.mark.asyncio
    async def test_36_timeout_raises_provider_timeout_error(self):
        """Scenario 36: Transport timeout raises ProviderTimeoutError after retries."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Connection timed out"))

        provider = BISTEODProvider(http_client=mock_client, timeout_seconds=1.0, max_retries=1)
        with pytest.raises(ProviderTimeoutError, match="Timeout connecting to BIST"):
            await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

    @pytest.mark.asyncio
    async def test_37_rate_limit_429_raises_provider_rate_limit_error(self):
        """Scenario 37: Repeated 429 raises ProviderRateLimitError."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 429
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, max_retries=1)
        with pytest.raises(ProviderRateLimitError, match="rate limit"):
            await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

    def test_38_validation_and_provenance(self):
        """Scenario 38: validate() catches OHLC issues; provenance() produces audit trail."""
        provider = BISTEODProvider()

        # Clean validation
        clean_obs = {"symbol": "THYAO", "trade_date": "2024-10-01", "open": "281.00", "high": "285.00", "low": "280.00", "close": "284.00"}
        assert provider.validate(clean_obs) == []

        # Contradiction validation
        bad_obs = {"symbol": "THYAO", "trade_date": "2024-10-01", "open": "281.00", "high": "270.00", "low": "280.00", "close": "284.00"}
        warnings = provider.validate(bad_obs)
        assert len(warnings) > 0
        assert any("OHLC integrity violation" in w for w in warnings)

        # Provenance check
        resp = ProviderResponse(
            provider_name="BIST_EOD",
            source_quality=SourceTier.TIER_2_EXCHANGE,
            retrieved_at=datetime(2024, 10, 1, 18, 30, tzinfo=timezone.utc),
            published_at=None,
            effective_date=date(2024, 10, 1),
            status=DataStatus.COMPLETE,
            raw=clean_obs,
            provider_symbol="THYAO",
        )
        prov = provider.provenance(resp)
        assert prov.provider_name == "BIST_EOD"
        assert prov.source_quality == SourceTier.TIER_2_EXCHANGE
        assert prov.metadata["official_source"] is True
        assert prov.metadata["access_status"] == "yellow"

    def test_39_serialization_to_generic_pit_storage_records(self, sample_resolver):
        """Scenario 39: Observations and snapshots cleanly serialize to Generic PIT storage models."""
        obs = BISTEODObservation(
            symbol="ALTIN.S1",
            trade_date=date(2024, 10, 1),
            close=Decimal("31.65"),
            open=Decimal("31.25"),
            high=Decimal("31.80"),
            low=Decimal("31.10"),
            volume=Decimal("5400120"),
            turnover=Decimal("170211782.40"),
            market_segment="EMTIA SERTIFIKALARI",
            instrument_id=uuid4(),
            asset_class=AssetClass.COMMODITY,
            instrument_type=InstrumentType.COMMODITY_CERTIFICATE,
        )

        norm_rec = obs.to_normalized_observation_record()
        assert norm_rec.observation_type == "BIST_EOD_PRICE"
        assert norm_rec.asset_class == AssetClass.COMMODITY
        assert norm_rec.instrument_type == InstrumentType.COMMODITY_CERTIFICATE
        assert norm_rec.effective_date == date(2024, 10, 1)
        assert norm_rec.currency == Currency.TRY

        snap = BISTBulletinSnapshot(
            trade_date=date(2024, 10, 1),
            retrieved_at=datetime(2024, 10, 1, 18, 30, tzinfo=timezone.utc),
            http_status=200,
            payload_hash="sample_sha256_hash",
            content_type="application/zip",
            observations=[obs],
        )
        raw_rec = snap.to_raw_snapshot_record()
        assert raw_rec.provider == "BIST_EOD"
        assert raw_rec.payload_hash == "sample_sha256_hash"
        assert raw_rec.http_status == 200

    def test_40_protocol_conformance(self):
        """Scenario 40: BISTEODProvider satisfies DataProviderContract protocol."""
        provider = BISTEODProvider()
        assert isinstance(provider, DataProviderContract)
