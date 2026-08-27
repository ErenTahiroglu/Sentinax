"""
backend/tests/test_bist_eod.py
==============================
Test suite for Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 Market Data Backbone (Phase 9A.6).

Discovery Authority & Citation:
    - Borsa İstanbul Pay Piyasası Dosya/Dizin Adresleri (DataFilePaths.zip)
    - Observed Manifest: VerilerDosyaIsimleri.xlsx
    - Official Equity Bulletin Mapping: "Bülten Verileri" -> /data/thb/YYYY/AA/ -> thbYYYYAAGGS.zip

Strict Invariants Verified:
    - Zero external network in tests (pytest-socket active).
    - Manifest-driven discovery: locator strictly derives URLs from verified DataFilePaths manifest.
    - No guessed fallback: locator without verified manifest fails closed (DISCOVERY_UNAVAILABLE).
    - Unsafe URL protection: non-HTTPS, non-BIST domains, and path traversal strictly rejected.
    - Two-header PAY_BULTEN / THB parsing: English header never parsed as data observation.
    - Zero float conversion: float input to parse_bist_decimal raises TypeError.
    - Missing/malformed close prices remain None (NEVER Decimal("0")!).
    - Raw symbol (e.g. KOZAA.E) preserved alongside normalized symbol (KOZAA).
    - ALTIN.S1 modeled as COMMODITY_CERTIFICATE (Darphane, 0.01g gold, 0.995 purity).
    - Deterministic duplicate conflict quarantine (order-independent).
    - Non-trading weekends vs empty weekday payloads vs 404 errors cleanly distinguished.
"""

import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Tuple
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
    BISTDataFilePathEntry,
    BISTDirectoryManifest,
    BISTDirectoryManifestCache,
    BISTDirectoryManifestParser,
    BISTEODObservation,
    BISTManifestDiscoveryError,
    BISTMarketSegment,
    BISTObservationStatus,
    BISTResolvedResource,
    BISTResourceResolutionError,
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
# Synthetic Test Fixtures Matching Real BIST Format
# ─────────────────────────────────────────────────────────────────────────────

OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV = """TARIH;ISLEM  KODU;BULTEN ADI;PAZAR GRUBU;PAZAR;YAPISAL BAZDA PIYASA ALT BOLUMU;ENSTRUMAN GRUBU;ENSTRUMAN TIPI;ENSTRUMAN SINIFI;ISLEM YONTEMI;PIYASA YAPICI;BIST 100 ENDEKS;BIST 30 ENDEKS;BRUT TAKAS;OZSERMAYE HALI;GECICI DURDURMA;ONCEKI KAPANIS FIYATI;ACILIS FIYATI;ACILIS SEANSI FIYATI;EN DUSUK FIYAT;EN YUKSEK FIYAT;KAPANIS FIYATI;KAPANIS SEANSI FIYATI;DEGISIM (%);BEKLEYEN EN IYI ALIS;BEKLEYEN EN IYI SATIS;A.O.F;TOPLAM ISLEM HACMI;TOPLAM ISLEM ADEDI;TOPLAM SOZLESME SAYISI
TRADE DATE;INSTRUMENT SERIES CODE;INSTRUMENT NAME;MARKET SUB SEGMENT;MARKET SEGMENT;MARKET;INSTRUMENT GROUP;INSTRUMENT TYPE;INSTRUMENT CLASS;TRADING METHOD;MARKET MAKER;BIST 100 INDEX;BIST 30 INDEX;GROSS SETTLEMENT;CORPORATE ACTION;SUSPENDED;PREVIOUS LAST PRICE;OPENING PRICE;OPENING SESSION PRICE;LOWEST PRICE;HIGHEST PRICE;CLOSING PRICE;CLOSING SESSION PRICE;CHANGE TO PREVIOUS CLOSING (%);REMAINING BID;REMAINING ASK;VWAP;TOTAL TRADED VALUE;TOTAL TRADED VOLUME;TOTAL NUMBER OF CONTRACTS
2024-10-01;KOZAA.E;KOZA MADENCILIK;;Z;MSPOT;EQT;MSPOTEQT;MSPOTEQTKOZAA;SI;0;1;0;0;;0;68.65;68.50;68.50;63.35;68.60;65.20;65.20;-5.025;65.15;65.20;65.76;332052579.40;5049497;31036
2024-10-01;THYAO.E;TURK HAVA YOLLARI;;Z;MSPOT;EQT;MSPOTEQT;MSPOTEQTTHYAO;SI;0;1;1;0;;0;285.00;285.25;285.25;274.25;287.00;277.75;277.75;-2.544;277.75;278.00;281.72;9764251444.00;34658869;113583
2024-10-01;ALTIN.S1;DARPHANE ALTIN SERTIFIKASI;;E;MSPOT;EMS;MSPOTEMS;MSPOTEMSALTIN;PY;1;0;0;0;;0;31.41;31.68;31.68;31.58;32.68;32.43;32.43;3.247;32.42;32.43;32.08;1660634479.17;51764007;44754
"""


def build_mock_data_file_paths_zip(entries: Dict[str, Tuple[str, str]]) -> bytes:
    """
    Creates an in-memory ZIP archive containing VerilerDosyaIsimleri.xlsx
    matching the exact structure observed from Borsa İstanbul.
    """
    xlsx_buf = io.BytesIO()
    with zipfile.ZipFile(xlsx_buf, "w", zipfile.ZIP_DEFLATED) as xzf:
        # [Content_Types].xml
        xzf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""")

        # _rels/.rels
        xzf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")

        # xl/_rels/workbook.xml.rels
        xzf.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>""")

        # xl/workbook.xml
        xzf.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets>
    <sheet name="TR - www.borsaistanbul.com" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
  </sheets>
</workbook>""")

        # Collect shared strings
        strings = ["Açıklama", "Web Sitesi Dizin Adresi", "Dosya Adı"]
        for desc, (d_path, f_name) in entries.items():
            strings.extend([desc, d_path, f_name])

        # xl/sharedStrings.xml
        sst_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">\n'
        for s in strings:
            sst_xml += f"  <si><t>{s}</t></si>\n"
        sst_xml += "</sst>"
        xzf.writestr("xl/sharedStrings.xml", sst_xml)

        # xl/worksheets/sheet1.xml
        ws_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n<sheetData>\n'
        ws_xml += '  <row r="3"><c r="B3" t="s"><v>0</v></c><c r="C3" t="s"><v>1</v></c><c r="D3" t="s"><v>2</v></c></row>\n'

        s_idx = 3
        row_idx = 4
        for desc, (d_path, f_name) in entries.items():
            ws_xml += f'  <row r="{row_idx}"><c r="B{row_idx}" t="s"><v>{s_idx}</v></c><c r="C{row_idx}" t="s"><v>{s_idx+1}</v></c><c r="D{row_idx}" t="s"><v>{s_idx+2}</v></c></row>\n'
            s_idx += 3
            row_idx += 1

        ws_xml += "</sheetData>\n</worksheet>"
        xzf.writestr("xl/worksheets/sheet1.xml", ws_xml)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("VerilerDosyaIsimleri.xlsx", xlsx_buf.getvalue())
    return zip_buf.getvalue()


def _create_sample_bulletin_zip_bytes(csv_content: str = OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thb202410011.csv", csv_content.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def default_manifest() -> BISTDirectoryManifest:
    raw_zip = build_mock_data_file_paths_zip({
        "Bülten Verileri": ("/data/thb/YYYY/AA/", "thbYYYYAAGGS.zip"),
        "Kıymetli Madenler Piyasası Günlük Bülten": ("/data/kmpbltn/YYYY/AA/", "KMPYYYYAAGG.zip"),
    })
    return BISTDirectoryManifestParser.parse_manifest_bytes(raw_zip)


@pytest.fixture
def sample_resolver() -> InstrumentResolverService:
    resolver = InstrumentResolverService()

    # KOZAA
    kozaa_id = uuid4()
    resolver.register_instrument(
        InstrumentRecord(
            id=kozaa_id,
            canonical_name="Koza Anadolu Metal Madencilik İşletmeleri A.Ş.",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            currency=Currency.TRY,
            mic="XIST",
            isin="TRAKOZAA91H9",
            valid_from=date(1990, 1, 1),
        )
    )
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=kozaa_id,
            provider="BIST",
            provider_symbol="KOZAA",
            valid_from=date(1990, 1, 1),
        )
    )

    # THYAO
    thyao_id = uuid4()
    resolver.register_instrument(
        InstrumentRecord(
            id=thyao_id,
            canonical_name="Türk Hava Yolları A.O.",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            currency=Currency.TRY,
            mic="XIST",
            isin="TRATHYAO91M9",
            valid_from=date(1990, 1, 1),
        )
    )
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=thyao_id,
            provider="BIST",
            provider_symbol="THYAO",
            valid_from=date(1990, 1, 1),
        )
    )

    # ALTIN.S1
    altin_id = uuid4()
    resolver.register_instrument(
        InstrumentRecord(
            id=altin_id,
            canonical_name=ALTIN_S1_CANONICAL_NAME,
            asset_class=ALTIN_S1_ASSET_CLASS,
            instrument_type=ALTIN_S1_INSTRUMENT_TYPE,
            currency=ALTIN_S1_CURRENCY,
            mic="XIST",
            isin="TRTDPHNE0013",
            valid_from=date(2022, 11, 21),
        )
    )
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
# 1. Manifest Discovery & Parser Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTDirectoryManifestDiscovery:

    def test_01_real_manifest_structure_parses_cleanly(self):
        """Scenario 1: DataFilePaths.zip fixture parses and extracts official bulletin mapping."""
        mock_zip = build_mock_data_file_paths_zip({
            "Bülten Verileri": ("/data/thb/YYYY/AA/", "thbYYYYAAGGS.zip"),
            "Marj Bilgileri": ("/data/thm/YYYY/AA/", "thmYYYYAAGGS.zip"),
        })
        manifest = BISTDirectoryManifestParser.parse_manifest_bytes(mock_zip)
        assert len(manifest.entries) == 2
        entry = manifest.get_equity_bulletin_entry()
        assert entry is not None
        assert entry.description == "Bülten Verileri"
        assert entry.directory_template == "/data/thb/YYYY/AA/"
        assert entry.filename_template == "thbYYYYAAGGS.zip"

    def test_02_no_guess_locator_fails_closed_without_manifest(self):
        """Scenario 2: Locator without verified manifest fails closed (never constructs guessed URL)."""
        locator = BISTBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="DISCOVERY_UNAVAILABLE"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=None)

    def test_03_verified_resolution_matches_observed_official_url(self, default_manifest):
        """Scenario 3: Locator derives exact URL from verified manifest."""
        locator = BISTBulletinLocator()
        resource = locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=default_manifest)

        assert resource.official_filename == "thb202410011.zip"
        assert resource.resolved_download_url == "https://www.borsaistanbul.com/data/thb/2024/10/thb202410011.zip"
        assert resource.requested_trade_date == date(2024, 10, 1)
        assert resource.filename_trade_date == date(2024, 10, 1)

    def test_04_dynamic_manifest_path_change_follows_new_manifest(self):
        """Scenario 4: If manifest directory changes from A to B, locator follows manifest B."""
        manifest_a = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Bülten Verileri": ("/data/pathA/YYYY/", "pathA_YYYYMMDDS.zip")})
        )
        manifest_b = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Bülten Verileri": ("/data/pathB/YYYY/AA/", "pathB_YYYYMMDDS.zip")})
        )

        locator = BISTBulletinLocator()
        res_a = locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_a)
        res_b = locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_b)

        assert "/pathA/" in res_a.resolved_download_url
        assert "/pathB/2024/10/" in res_b.resolved_download_url

    def test_05_missing_pay_bulten_entry_fails_closed(self):
        """Scenario 5: Manifest without equity bulletin entry raises PAY_BULTEN_PATH_NOT_FOUND."""
        manifest_no_bulletin = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Other File": ("/data/other/", "other.zip")})
        )
        locator = BISTBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="PAY_BULTEN_PATH_NOT_FOUND"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_no_bulletin)

    def test_06_ambiguous_manifest_entries_fail_closed(self):
        """Scenario 6: Conflicting multiple bulletin entries in manifest fail closed."""
        manifest_ambiguous = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({
                "Bülten Verileri": ("/data/thb1/YYYY/", "thb1.zip"),
                "Bülten Verileri (Alternatif)": ("/data/thb2/YYYY/", "thb2.zip"),
            })
        )
        locator = BISTBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="PAY_BULTEN_PATH_AMBIGUOUS"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_ambiguous)

    def test_07_unsafe_host_rejected(self):
        """Scenario 7: Manifest resolving to non-whitelisted host raises UNSAFE_RESOLVED_URL."""
        manifest_evil = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Bülten Verileri": ("https://evil.example/data/", "bulletin.zip")})
        )
        locator = BISTBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="UNSAFE_RESOLVED_URL"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_evil)

    def test_08_insecure_http_scheme_rejected(self):
        """Scenario 8: Insecure HTTP scheme raises UNSAFE_RESOLVED_URL."""
        manifest_http = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Bülten Verileri": ("http://www.borsaistanbul.com/data/", "bulletin.zip")})
        )
        locator = BISTBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="UNSAFE_RESOLVED_URL"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_http)

    def test_09_path_traversal_rejected(self):
        """Scenario 9: Path traversal in directory template raises UNSAFE_RESOLVED_URL."""
        manifest_traversal = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Bülten Verileri": ("/data/../../secret/", "bulletin.zip")})
        )
        locator = BISTBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="UNSAFE_RESOLVED_URL"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_traversal)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Two-Header PAY_BULTEN & THB Parser Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTPAYBultenParser:

    def test_10_two_header_rows_parsed_cleanly(self, sample_resolver):
        """Scenario 10: Two header rows parsed cleanly; English header is never an observation."""
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
        assert kozaa.close == Decimal("65.20")
        assert kozaa.open == Decimal("68.50")
        assert kozaa.low == Decimal("63.35")
        assert kozaa.high == Decimal("68.60")
        assert kozaa.previous_close == Decimal("68.65")
        assert kozaa.weighted_average == Decimal("65.76")
        assert kozaa.turnover == Decimal("332052579.40")
        assert kozaa.volume == Decimal("5049497")
        assert kozaa.trade_count == 31036
        assert kozaa.status == BISTObservationStatus.VALID

    def test_11_field_semantics_turnover_volume_trade_count(self, sample_resolver):
        """Scenario 11: TOPLAM ISLEM HACMI = turnover, TOPLAM ISLEM ADEDI = volume, TOPLAM SOZLESME SAYISI = trade_count."""
        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
            trade_date=date(2024, 10, 1),
            resolver=sample_resolver,
        )
        thyao = observations[1]
        assert thyao.turnover == Decimal("9764251444.00")
        assert thyao.volume == Decimal("34658869")
        assert thyao.trade_count == 113583

    def test_12_zero_float_conversion_rejected(self):
        """Scenario 12: Float input to parse_bist_decimal raises TypeError (zero float allowed)."""
        with pytest.raises(TypeError, match="Float input prohibited"):
            parse_bist_decimal(284.25)

        with pytest.raises(TypeError, match="Float input prohibited"):
            parse_bist_int(45120.0)

    def test_13_missing_required_column_raises_schema_drift(self):
        """Scenario 13: Missing required column (e.g. KAPANIS FIYATI) raises BISTSchemaDriftError."""
        csv_no_close = """PAZAR KODU;PAY KODU;ACILIS FIYATI
MARKET SEGMENT;INSTRUMENT CODE;OPENING PRICE
Z;KOZAA.E;68.50
"""
        with pytest.raises(BISTSchemaDriftError, match="missing required columns"):
            BISTBulletinParser.parse_bulletin_text(raw_text=csv_no_close, trade_date=date(2024, 10, 1))

    def test_14_trade_date_from_filename_and_context(self):
        """Scenario 14: Trade date is correctly resolved from filename thb202410011.csv."""
        parsed_date = BISTBulletinLocator.parse_filename_trade_date("thb202410011.zip")
        assert parsed_date == date(2024, 10, 1)

        observations = BISTBulletinParser.parse_bulletin_text(
            raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
            filename_date=parsed_date,
        )
        assert all(o.trade_date == date(2024, 10, 1) for o in observations)

    def test_15_trade_date_and_filename_date_mismatch_fails_closed(self):
        """Scenario 15: If requested trade_date and filename date disagree, fail closed."""
        with pytest.raises(BISTSchemaDriftError, match="does not match verified filename date"):
            BISTBulletinParser.parse_bulletin_text(
                raw_text=OFFICIAL_SCHEMA_SYNTHETIC_VALUES_CSV,
                trade_date=date(2024, 10, 2),
                filename_date=date(2024, 10, 1),
            )

    def test_16_malformed_close_price_never_becomes_zero(self):
        """Scenario 16: Corrupt close price leaves close=None (NEVER Decimal('0')) and marks INVALID_OBSERVATION."""
        csv_corrupt_close = """PAZAR;ISLEM KODU;KAPANIS FIYATI
MARKET;INSTRUMENT CODE;CLOSING PRICE
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

    def test_17_missing_close_price_remains_none(self):
        """Scenario 17: Empty close price cell remains None and marks INVALID_OBSERVATION."""
        csv_empty_close = """PAZAR;ISLEM KODU;KAPANIS FIYATI
MARKET;INSTRUMENT CODE;CLOSING PRICE
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

    def test_18_raw_symbol_preservation_and_e_stripping(self):
        """Scenario 18: raw_provider_symbol preserves exact source 'KOZAA.E', normalized symbol is 'KOZAA'."""
        csv_symbols = """PAZAR;ISLEM KODU;KAPANIS FIYATI
MARKET;INSTRUMENT CODE;CLOSING PRICE
Z;KOZAA.E;65.20
Z;THYAO.E;277.75
E;ALTIN.S1;32.43
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

    def test_19_altin_s1_economic_definition_regression(self, sample_resolver):
        """Scenario 19: ALTIN.S1 resolves with COMMODITY_CERTIFICATE, 0.01g gold, 0.995 purity facts."""
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
        assert altin.close == Decimal("32.43")
        assert ALTIN_S1_CERTIFICATE_REPRESENTATION_GRAMS == Decimal("0.01")
        assert ALTIN_S1_PURITY == Decimal("0.995")
        assert ALTIN_S1_ISSUER == "T.C. Hazine ve Maliye Bakanlığı Darphane ve Damga Matbaası"
        assert ALTIN_S1_UNDERLYING == "gold"

    def test_20_duplicate_conflict_quarantine_and_order_independence(self):
        """Scenario 20: Conflicting duplicate rows are deterministically quarantined regardless of row order."""
        csv_forward = """PAZAR;ISLEM KODU;KAPANIS FIYATI;TOPLAM ISLEM ADEDI
MARKET;INSTRUMENT CODE;CLOSING PRICE;TOTAL TRADED VOLUME
Z;KOZAA.E;65.20;1000
Z;KOZAA.E;66.00;1000
"""
        csv_reverse = """PAZAR;ISLEM KODU;KAPANIS FIYATI;TOPLAM ISLEM ADEDI
MARKET;INSTRUMENT CODE;CLOSING PRICE;TOTAL TRADED VOLUME
Z;KOZAA.E;66.00;1000
Z;KOZAA.E;65.20;1000
"""
        obs_fwd = BISTBulletinParser.parse_bulletin_text(raw_text=csv_forward, trade_date=date(2024, 10, 1))
        obs_rev = BISTBulletinParser.parse_bulletin_text(raw_text=csv_reverse, trade_date=date(2024, 10, 1))

        assert len(obs_fwd) == 2
        assert all(o.status == BISTObservationStatus.CONFLICT_QUARANTINED for o in obs_fwd)
        assert len(obs_rev) == 2
        assert all(o.status == BISTObservationStatus.CONFLICT_QUARANTINED for o in obs_rev)

    def test_21_zip_archive_parsing(self, sample_resolver):
        """Scenario 21: ZIP archive containing thb202410011.csv parses cleanly."""
        zip_bytes = _create_sample_bulletin_zip_bytes()
        observations = BISTBulletinParser.parse_bulletin_bytes(
            raw_bytes=zip_bytes,
            filename="thb202410011.zip",
            resolver=sample_resolver,
        )
        assert len(observations) == 3
        assert observations[0].symbol == "KOZAA"
        assert observations[0].trade_date == date(2024, 10, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provider Fetch, PIT, Statuses & Error Handling Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTEODProvider:

    @pytest.mark.asyncio
    async def test_31_fetch_daily_bulletin_with_manifest_discovery(self, sample_resolver):
        """Scenario 31: fetch_daily_bulletin dynamically fetches manifest and resolves official resource."""
        mock_manifest_zip = build_mock_data_file_paths_zip({
            "Bülten Verileri": ("/data/thb/YYYY/AA/", "thbYYYYAAGGS.zip")
        })
        mock_bulletin_zip = _create_sample_bulletin_zip_bytes()

        mock_client = MagicMock(spec=httpx.AsyncClient)

        def mock_get(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            if "DataFilePaths.zip" in url:
                resp.content = mock_manifest_zip
                resp.headers = {"content-type": "application/zip"}
            else:
                resp.content = mock_bulletin_zip
                resp.headers = {"content-type": "application/zip"}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        provider = BISTEODProvider(http_client=mock_client, resolver=sample_resolver)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        assert snapshot.http_status == 200
        assert snapshot.file_name == "thb202410011.zip"
        assert snapshot.resolved_download_url == "https://www.borsaistanbul.com/data/thb/2024/10/thb202410011.zip"
        assert len(observations) == 3

    @pytest.mark.asyncio
    async def test_32_manifest_fetch_failure_returns_discovery_fetch_failed(self):
        """Scenario 32: When DataFilePaths.zip fetch fails, snapshot records DISCOVERY_UNAVAILABLE."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = BISTEODProvider(http_client=mock_client, max_retries=1)
        snapshot, observations = await provider.fetch_daily_bulletin(trade_date=date(2024, 10, 1))

        assert snapshot.http_status in (500, 503)
        assert len(observations) == 0
        assert any("DISCOVERY" in d for d in snapshot.diagnostics)

    @pytest.mark.asyncio
    async def test_33_stale_manifest_cache_fallback(self):
        """Scenario 33: If live manifest refresh fails, stale cache is used with DEGRADED_DISCOVERY note."""
        mock_manifest_zip = build_mock_data_file_paths_zip({
            "Bülten Verileri": ("/data/thb/YYYY/AA/", "thbYYYYAAGGS.zip")
        })
        manifest = BISTDirectoryManifestParser.parse_manifest_bytes(mock_manifest_zip)

        cache = BISTDirectoryManifestCache(ttl_seconds=0.0, max_stale_seconds=3600.0)
        cache.set_manifest(manifest)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Network down"))

        provider = BISTEODProvider(http_client=mock_client, manifest_cache=cache)
        man, is_stale = await provider.fetch_directory_manifest()

        assert man is not None
        assert is_stale is True

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
        mock_manifest_zip = build_mock_data_file_paths_zip({
            "Bülten Verileri": ("/data/thb/YYYY/AA/", "thbYYYYAAGGS.zip")
        })
        mock_bulletin_zip = _create_sample_bulletin_zip_bytes()

        mock_client = MagicMock(spec=httpx.AsyncClient)

        def mock_get(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = mock_manifest_zip if "DataFilePaths.zip" in url else mock_bulletin_zip
            resp.headers = {"content-type": "application/zip"}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

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
        assert response.raw["close"] == "65.20"

    def test_36_protocol_conformance(self):
        """Scenario 36: BISTEODProvider satisfies DataProviderContract protocol."""
        provider = BISTEODProvider()
        assert isinstance(provider, DataProviderContract)

    def test_37_serialization_to_generic_pit_storage_records(self):
        """Scenario 37: Observations and snapshots cleanly serialize to Generic PIT storage models."""
        obs = BISTEODObservation(
            symbol="ALTIN.S1",
            raw_provider_symbol="ALTIN.S1",
            trade_date=date(2024, 10, 1),
            close=Decimal("32.43"),
            open=Decimal("31.68"),
            high=Decimal("32.68"),
            low=Decimal("31.58"),
            volume=Decimal("51764007"),
            turnover=Decimal("1660634479.17"),
            market_segment="E",
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
