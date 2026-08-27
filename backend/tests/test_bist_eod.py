"""
backend/tests/test_bist_eod.py
==============================
Test suite for Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 Market Data Backbone (Phase 9A.5).

Source Basis & Citation:
    - Borsa İstanbul Pay Piyasası Gün Sonu Kapanış Verileri (PAY_BULTEN_YYYYAAGG.csv)
    - Documented 2-row header schema (Turkish Row 1, English Row 2, Observations Row 3+)
    - Delimiter: ';' (semicolon), Decimal Symbol: '.' (dot)

Strict Invariants Verified:
    - Zero external network in tests (pytest-socket active).
    - Exact 2-header-row parsing (English header never parsed as an instrument).
    - Trade date sourced from verified bulletin context / filename (PAY_BULTEN_YYYYMMDD.csv).
    - Trade date mismatch fails closed.
    - Zero float conversion: float inputs to parse_bist_decimal raise TypeError.
    - Missing or malformed close prices remain None (NEVER Decimal("0")!).
    - Raw symbol (e.g. KOZAA.E) preserved alongside normalized symbol (KOZAA).
    - ALTIN.S1 modeled as COMMODITY_CERTIFICATE (Darphane, 0.01g gold, 0.995 purity).
    - Suffix .S1 strictly preserved; .E stripped for equities.
    - Deterministic duplicate conflict quarantine (order-independent).
    - Download discovery via BISTBulletinLocator preserving full resource metadata.
    - Non-trading weekends vs empty weekday payloads vs 404 errors cleanly distinguished.
"""

import io
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
    BISTBulletinLocator,
    BISTBulletinParser,
    BISTBulletinSnapshot,
    BISTCapability,
    BISTEODObservation,
    BISTMarketSegment,
    BISTObservationStatus,
    BISTResolvedResource,
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
# Official PAY_BULTEN Schema Fixture (Synthetic Values)
# ─────────────────────────────────────────────────────────────────────────────
# Documented 2-row header:
# Row 1: PAZAR KODU;PAY KODU;PAY ADI;ONCEKI KAPANIS FIYATI;ACILIS FIYATI;EN DUSUK FIYAT;EN YUKSEK FIYAT;KAPANIS FIYATI;DEGISIM(%);GUNLUK AGIRLIKLI ORTALAMA FIYAT;TOPLAM ISLEM HACMI;TOPLAM ISLEM ADEDI;TOPLAM SOZLESME SAYISI
# Row 2: MARKET SEGMENT;INSTRUMENT CODE;INSTRUMENT NAME;PREVIOUS CLOSING PRICE;OPENING PRICE;LOWEST PRICE;HIGHEST PRICE;CLOSING PRICE;CHANGE(%);WAP;TOTAL TRADE VALUE;TOTAL TRADE QUANTITY;TOTAL NUMBER OF TRADES

OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV = """PAZAR KODU;PAY KODU;PAY ADI;ONCEKI KAPANIS FIYATI;ACILIS FIYATI;EN DUSUK FIYAT;EN YUKSEK FIYAT;KAPANIS FIYATI;DEGISIM(%);GUNLUK AGIRLIKLI ORTALAMA FIYAT;TOPLAM ISLEM HACMI;TOPLAM ISLEM ADEDI;TOPLAM SOZLESME SAYISI
MARKET SEGMENT;INSTRUMENT CODE;INSTRUMENT NAME;PREVIOUS CLOSING PRICE;OPENING PRICE;LOWEST PRICE;HIGHEST PRICE;CLOSING PRICE;CHANGE(%);WAP;TOTAL TRADE VALUE;TOTAL TRADE QUANTITY;TOTAL NUMBER OF TRADES
Z;KOZAA.E;KOZA MADENCILIK;2.30;2.30;2.28;2.31;2.29;-0.43;2.29;4683455.01;2038336;897
Z;THYAO.E;TURK HAVA YOLLARI;280.50;281.00;279.50;285.75;284.25;1.34;283.40;4370076340.00;15420100;45120
E;ALTIN.S1;DARPHANE ALTIN SERTIFIKASI;31.20;31.25;31.10;31.80;31.65;1.44;31.52;170211782.40;5400120;18500
"""


def _create_sample_zip_bytes(csv_content: str = OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PAY_BULTEN_20241001.csv", csv_content.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def sample_resolver() -> InstrumentResolverService:
    resolver = InstrumentResolverService()

    # KOZAA Master Instrument
    kozaa_id = uuid4()
    kozaa_inst = InstrumentRecord(
        id=kozaa_id,
        canonical_name="Koza Anadolu Metal Madencilik İşletmeleri A.Ş.",
        asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.BIST_STOCK,
        currency=Currency.TRY,
        mic="XIST",
        isin="TRAKOZAA91H9",
        valid_from=date(1990, 1, 1),
    )
    resolver.register_instrument(kozaa_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=kozaa_id,
            provider="BIST",
            provider_symbol="KOZAA",
            valid_from=date(1990, 1, 1),
        )
    )

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
# 1. Two-Header PAY_BULTEN Parser Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTPAYBultenParser:

    def test_01_two_header_rows_parsed_cleanly(self, sample_resolver):
        """Scenario 1: Two header rows parsed cleanly; English header is never an observation."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
            trade_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        assert len(observations) == 3
        symbols = [o.symbol for o in observations]
        assert symbols == ["KOZAA", "THYAO", "ALTIN.S1"]

        kozaa = observations[0]
        assert kozaa.symbol == "KOZAA"
        assert kozaa.raw_provider_symbol == "KOZAA.E"
        assert kozaa.trade_date == date(2024, 10, 1)
        assert kozaa.close == Decimal("2.29")
        assert kozaa.open == Decimal("2.30")
        assert kozaa.low == Decimal("2.28")
        assert kozaa.high == Decimal("2.31")
        assert kozaa.previous_close == Decimal("2.30")
        assert kozaa.weighted_average == Decimal("2.29")
        assert kozaa.turnover == Decimal("4683455.01")
        assert kozaa.volume == Decimal("2038336")
        assert kozaa.trade_count == 897
        assert kozaa.market_segment == "Z"
        assert kozaa.instrument_name == "KOZA MADENCILIK"
        assert kozaa.status == BISTObservationStatus.VALID

    def test_02_field_semantics_turnover_volume_trade_count(self, sample_resolver):
        """Scenario 2: TOPLAM ISLEM HACMI = turnover, TOPLAM ISLEM ADEDI = volume, TOPLAM SOZLESME SAYISI = trade_count."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
            trade_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        thyao = observations[1]
        assert thyao.turnover == Decimal("4370076340.00")
        assert thyao.volume == Decimal("15420100")
        assert thyao.trade_count == 45120

    def test_03_zero_float_conversion_rejected(self):
        """Scenario 3: Float input to parse_bist_decimal raises TypeError (zero float allowed)."""
        with pytest.raises(TypeError, match="Float input prohibited"):
            parse_bist_decimal(284.25)

        with pytest.raises(TypeError, match="Float input prohibited"):
            parse_bist_int(45120.0)

    def test_04_missing_required_column_raises_schema_drift(self):
        """Scenario 4: Missing required column (e.g. KAPANIS FIYATI) raises BISTSchemaDriftError."""
        csv_no_close = """PAZAR KODU;PAY KODU;ACILIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;OPENING PRICE
Z;KOZAA.E;2.30
"""
        with pytest.raises(BISTSchemaDriftError, match="missing required columns"):
            BISTBulletinParser.parse_bulletin_text(raw_text=csv_no_close, trade_date=date(2024, 10, 1))

    def test_05_trade_date_from_filename_and_context(self):
        """Scenario 5: Trade date is correctly resolved from filename PAY_BULTEN_20241001.csv."""
        parsed_date = BISTBulletinLocator.parse_filename_trade_date("PAY_BULTEN_20241001.csv")
        assert parsed_date == date(2024, 10, 1)

        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
            filename_date=parsed_date,
        )
        assert all(o.trade_date == date(2024, 10, 1) for o in observations)

    def test_06_trade_date_and_filename_date_mismatch_fails_closed(self):
        """Scenario 6: If requested trade_date and filename date disagree, fail closed."""
        with pytest.raises(BISTSchemaDriftError, match="does not match verified filename date"):
            BISTBulletinParser.parse_bulletin_text(
                raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
                trade_date=date(2024, 10, 2),
                filename_date=date(2024, 10, 1),
            )

    def test_07_malformed_close_price_never_becomes_zero(self):
        """Scenario 7: Corrupt close price leaves close=None (NEVER Decimal('0')) and marks INVALID_OBSERVATION."""
        csv_corrupt_close = """PAZAR KODU;PAY KODU;KAPANIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;CLOSING PRICE
Z;KOZAA.E;CORRUPTED_PRICE
"""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=csv_corrupt_close,
            trade_date=date(2024, 10, 1),
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs.close is None, "Malformed close must remain None, NEVER Decimal('0')!"
        assert obs.status == BISTObservationStatus.INVALID_OBSERVATION
        assert any("Error parsing close price" in d for d in obs.diagnostics)

    def test_08_missing_close_price_remains_none(self):
        """Scenario 8: Empty close price cell remains None and marks INVALID_OBSERVATION."""
        csv_empty_close = """PAZAR KODU;PAY KODU;KAPANIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;CLOSING PRICE
Z;KOZAA.E;
"""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=csv_empty_close,
            trade_date=date(2024, 10, 1),
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs.close is None
        assert obs.status == BISTObservationStatus.INVALID_OBSERVATION
        assert any("Missing required closing price" in d for d in obs.diagnostics)

    def test_09_raw_symbol_preservation_and_e_stripping(self):
        """Scenario 9: raw_provider_symbol preserves exact source 'KOZAA.E', normalized symbol is 'KOZAA'."""
        csv_symbols = """PAZAR KODU;PAY KODU;KAPANIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;CLOSING PRICE
Z;KOZAA.E;2.29
Z;THYAO.E;284.25
E;ALTIN.S1;31.65
"""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=csv_symbols,
            trade_date=date(2024, 10, 1),
        )
        assert observations[0].raw_provider_symbol == "KOZAA.E"
        assert observations[0].symbol == "KOZAA"
        assert observations[1].raw_provider_symbol == "THYAO.E"
        assert observations[1].symbol == "THYAO"
        assert observations[2].raw_provider_symbol == "ALTIN.S1"
        assert observations[2].symbol == "ALTIN.S1"

    def test_10_altin_s1_economic_definition_regression(self, sample_resolver):
        """Scenario 10: ALTIN.S1 resolves with COMMODITY_CERTIFICATE, 0.01g gold, 0.995 purity facts."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
            trade_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        altin = [o for o in observations if o.symbol == "ALTIN.S1"][0]
        assert altin.symbol == "ALTIN.S1"
        assert altin.raw_provider_symbol == "ALTIN.S1"
        assert altin.asset_class == AssetClass.COMMODITY
        assert altin.instrument_type == InstrumentType.COMMODITY_CERTIFICATE
        assert altin.currency == Currency.TRY
        assert altin.close == Decimal("31.65")
        assert ALTIN_S1_CERTIFICATE_REPRESENTATION_GRAMS == Decimal("0.01")
        assert ALTIN_S1_PURITY == Decimal("0.995")
        assert ALTIN_S1_ISSUER == "T.C. Hazine ve Maliye Bakanlığı Darphane ve Damga Matbaası"
        assert ALTIN_S1_UNDERLYING == "gold"

    def test_11_duplicate_conflict_quarantine_and_order_independence(self):
        """Scenario 11: Conflicting duplicate rows are deterministically quarantined regardless of row order."""
        csv_forward = """PAZAR KODU;PAY KODU;KAPANIS FIYATI;TOPLAM ISLEM ADEDI
MARKET SEGMENT;INSTRUMENT CODE;CLOSING PRICE;TOTAL TRADE QUANTITY
Z;KOZAA.E;2.29;1000
Z;KOZAA.E;2.35;1000
"""
        csv_reverse = """PAZAR KODU;PAY KODU;KAPANIS FIYATI;TOPLAM ISLEM ADEDI
MARKET SEGMENT;INSTRUMENT CODE;CLOSING PRICE;TOTAL TRADE QUANTITY
Z;KOZAA.E;2.35;1000
Z;KOZAA.E;2.29;1000
"""
        obs_fwd = BISTBulletinParser.parse_bulletin_text(raw_text=csv_forward, trade_date=date(2024, 10, 1))
        obs_rev = BISTBulletinParser.parse_bulletin_text(raw_text=csv_reverse, trade_date=date(2024, 10, 1))

        # Both rows in forward order are quarantined
        assert len(obs_fwd) == 2
        assert all(o.status == BISTObservationStatus.CONFLICT_QUARANTINED for o in obs_fwd)

        # Both rows in reverse order are quarantined identically
        assert len(obs_rev) == 2
        assert all(o.status == BISTObservationStatus.CONFLICT_QUARANTINED for o in obs_rev)

    def test_12_ohlc_integrity_violations(self):
        """Scenario 12: OHLC contradictions (High < Low, High < Close, Low > Close) rejected."""
        csv_bad_ohlc = """PAZAR KODU;PAY KODU;ACILIS FIYATI;EN DUSUK FIYAT;EN YUKSEK FIYAT;KAPANIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;OPENING PRICE;LOWEST PRICE;HIGHEST PRICE;CLOSING PRICE
Z;BAD1.E;2.30;2.35;2.25;2.30
Z;BAD2.E;2.30;2.20;2.25;2.30
"""
        observations = BISTBulletinParser.parse_bulletin_text(raw_text=csv_bad_ohlc, trade_date=date(2024, 10, 1))
        assert len(observations) == 2
        assert observations[0].status == BISTObservationStatus.INVALID_OBSERVATION
        assert observations[1].status == BISTObservationStatus.INVALID_OBSERVATION

    def test_13_zip_archive_with_pay_bulten_filename(self, sample_resolver):
        """Scenario 13: ZIP archive containing PAY_BULTEN_20241001.csv parses and extracts filename date."""
        zip_bytes = _create_sample_zip_bytes()
        observations = BISTBulletinParser.parse_bulletin_bytes(
            raw_bytes=zip_bytes,
            filename="PAY_BULTEN_20241001.zip",
            resolver=sample_resolver,
        )
        assert len(observations) == 3
        assert observations[0].symbol == "KOZAA"
        assert observations[0].trade_date == date(2024, 10, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Locator & Resource Discovery Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTBulletinLocator:

    def test_21_locator_resolves_resource_metadata(self):
        """Scenario 21: BISTBulletinLocator resolves official resource without guessing."""
        locator = BISTBulletinLocator()
        res = locator.resolve_bulletin_resource(date(2024, 10, 1))

        assert isinstance(res, BISTResolvedResource)
        assert res.official_filename == "PAY_BULTEN_20241001.csv"
        assert res.resolved_download_url.endswith("/PAY_BULTEN_20241001.csv")
        assert res.requested_trade_date == date(2024, 10, 1)
        assert res.filename_trade_date == date(2024, 10, 1)
        assert res.landing_page_url == "https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provider Fetch, PIT, Statuses & Error Handling Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTEODProvider:

    @pytest.mark.asyncio
    async def test_31_fetch_daily_bulletin_preserves_discovery_metadata(self, sample_resolver):
        """Scenario 31: fetch_daily_bulletin attaches locator metadata to snapshot."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = _create_sample_zip_bytes()
        mock_resp.headers = {"content-type": "application/zip"}
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, resolver=sample_resolver)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        assert snapshot.http_status == 200
        assert snapshot.file_name == "PAY_BULTEN_20241001.csv"
        assert snapshot.requested_trade_date == date(2024, 10, 1)
        assert snapshot.filename_trade_date == date(2024, 10, 1)
        assert snapshot.landing_page_url is not None
        assert snapshot.resolved_download_url is not None
        assert len(observations) == 3

    @pytest.mark.asyncio
    async def test_32_404_resource_not_found_semantics(self):
        """Scenario 32: 404 response sets RESOURCE_NOT_FOUND without assuming false DataStore proof."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 404
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        assert snapshot.http_status == 404
        assert len(observations) == 0
        assert any("RESOURCE_NOT_FOUND" in d for d in snapshot.diagnostics)

    @pytest.mark.asyncio
    async def test_33_empty_weekday_payload_unresolved_session(self):
        """Scenario 33: Empty weekday payload returns EMPTY_SOURCE_PAYLOAD / UNRESOLVED_SESSION."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = b""
        mock_client.get = AsyncMock(return_value=mock_resp)

        # 2024-10-01 is Tuesday
        provider = BISTEODProvider(http_client=mock_client)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        assert snapshot.http_status == 200
        assert len(observations) == 0
        assert any("EMPTY_SOURCE_PAYLOAD" in d for d in snapshot.diagnostics)

    @pytest.mark.asyncio
    async def test_34_weekend_non_trading_day_no_network(self):
        """Scenario 34: Weekend session returns NON_TRADING_DAY with zero HTTP requests."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        provider = BISTEODProvider(http_client=mock_client)

        # 2024-10-05 is Saturday
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 5))
        assert snapshot.http_status == 200
        assert len(observations) == 0
        assert any("NON_TRADING_DAY: Weekend" in d for d in snapshot.diagnostics)
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_35_fetch_single_symbol_preserves_raw_provider_symbol(self, sample_resolver):
        """Scenario 35: fetch(context) preserves raw_provider_symbol and returns exact observation."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = _create_sample_zip_bytes()
        mock_resp.headers = {"content-type": "application/zip"}
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, resolver=sample_resolver)
        ctx = FetchContext(
            observation_type="BIST_EOD_PRICE",
            provider_symbol="KOZAA.E",
            effective_date=date(2024, 10, 1),
        )
        response = await provider.fetch(ctx)

        assert response.status == DataStatus.COMPLETE
        assert response.provider_symbol == "KOZAA.E"
        assert response.raw["symbol"] == "KOZAA"
        assert response.raw["raw_provider_symbol"] == "KOZAA.E"
        assert response.raw["close"] == "2.29"

    @pytest.mark.asyncio
    async def test_36_multi_instrument_bulletin_status_degraded_when_invalid(self):
        """Scenario 36: Bulletin with invalid rows returns aggregate DataStatus.DEGRADED."""
        csv_with_bad = """PAZAR KODU;PAY KODU;KAPANIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;CLOSING PRICE
Z;GOOD.E;10.00
Z;BAD.E;CORRUPT
"""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = csv_with_bad.encode("utf-8")
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client)
        ctx = FetchContext(observation_type="BIST_EOD_PRICE", effective_date=date(2024, 10, 1))
        response = await provider.fetch(ctx)

        assert response.status == DataStatus.DEGRADED
        assert len(response.raw) == 2

    def test_37_validation_and_provenance(self):
        """Scenario 37: validate() catches OHLC issues; provenance() produces audit trail."""
        provider = BISTEODProvider()

        # Clean validation
        clean_obs = {"symbol": "KOZAA", "trade_date": "2024-10-01", "open": "2.30", "high": "2.31", "low": "2.28", "close": "2.29"}
        assert provider.validate(clean_obs) == []

        # Contradiction validation
        bad_obs = {"symbol": "KOZAA", "trade_date": "2024-10-01", "open": "2.30", "high": "2.20", "low": "2.28", "close": "2.29"}
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
            provider_symbol="KOZAA.E",
        )
        prov = provider.provenance(resp)
        assert prov.provider_name == "BIST_EOD"
        assert prov.source_quality == SourceTier.TIER_2_EXCHANGE
        assert prov.metadata["official_source"] is True
        assert prov.metadata["access_status"] == "yellow"

    def test_38_serialization_to_generic_pit_storage_records(self):
        """Scenario 38: Observations and snapshots cleanly serialize to Generic PIT storage models."""
        obs = BISTEODObservation(
            symbol="ALTIN.S1",
            raw_provider_symbol="ALTIN.S1",
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

    @pytest.mark.asyncio
    async def test_39_rate_limit_and_timeout_errors(self):
        """Scenario 39: 429 raises ProviderRateLimitError, timeout raises ProviderTimeoutError."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 429
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, max_retries=1)
        with pytest.raises(ProviderRateLimitError, match="rate limit"):
            await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Connection timed out"))
        provider_to = BISTEODProvider(http_client=mock_client, timeout_seconds=0.1, max_retries=1)
        with pytest.raises(ProviderTimeoutError, match="Timeout connecting to BIST"):
            await provider_to.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

    def test_40_protocol_conformance(self):
        """Scenario 40: BISTEODProvider satisfies DataProviderContract protocol."""
        provider = BISTEODProvider()
        assert isinstance(provider, DataProviderContract)
