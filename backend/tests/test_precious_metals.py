"""
backend/tests/test_precious_metals.py
=====================================
Test suite for BIST KMTP & TCMB EVDS Precious Metals (Gold / Silver) Market Backbone (Phase 9B.1).

Strict Invariants Verified:
    - Zero network in tests (pytest-socket active).
    - Manifest-driven discovery: locator strictly derives URLs from verified DataFilePaths manifest.
    - No guessed fallback: locator without verified manifest fails closed (DISCOVERY_UNAVAILABLE).
    - Unsafe URL protection: non-HTTPS, non-BIST domains, and path traversal strictly rejected.
    - Full dimensioned models: metal, currency, quantity_unit, purity, price_type, settlement_term.
    - Zero float conversion: float input to parse_kmtp_decimal raises TypeError.
    - Missing/corrupted prices remain None (NEVER Decimal("0")!).
    - Unsupported metals (Platinum, Palladium) classified as UNSUPPORTED_METAL; not fabricated as Gold/Silver.
    - Conflicting duplicate rows deterministically quarantined with order independence.
    - EVDS series definitions: exact codes, BIST originating source, pure Decimal parsing.
    - Strict cross-source comparability: CONSISTENT, DIVERGENT, NOT_COMPARABLE.
"""

import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from backend.engine.private.bist.locator import (
    BISTResolvedResource,
    BISTResourceResolutionError,
)
from backend.engine.private.bist.manifest import (
    BISTDirectoryManifest,
    BISTDirectoryManifestCache,
    BISTDirectoryManifestParser,
)
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.precious_metals import (
    BISTKMTPBulletinParser,
    BISTKMTPParserError,
    BISTKMTPSchemaDriftError,
    BISTPreciousMetalsBulletinLocator,
    ComparabilityResult,
    ComparabilityStatus,
    PreciousMetalCrossSourceComparator,
    PreciousMetalMarket,
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
    PreciousMetalPriceType,
    PreciousMetalSeriesDefinition,
    PreciousMetalSeriesRegistry,
    PreciousMetalSnapshot,
    PreciousMetalType,
    PreciousMetalUnit,
    parse_kmtp_decimal,
    parse_kmtp_int,
    parse_unit_and_currency,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.providers.bist_kmtp import BISTKMTPProvider
from backend.engine.private.providers.tcmb_evds import TCMBEVDSProvider


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic Test Fixture Builders for KMTP XLSX & DataFilePaths ZIP
# ─────────────────────────────────────────────────────────────────────────────

def build_mock_data_file_paths_zip(entries: Dict[str, Tuple[str, str]]) -> bytes:
    """Creates in-memory DataFilePaths.zip with VerilerDosyaIsimleri.xlsx."""
    xlsx_buf = io.BytesIO()
    with zipfile.ZipFile(xlsx_buf, "w", zipfile.ZIP_DEFLATED) as xzf:
        xzf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""")
        xzf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        xzf.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>""")
        xzf.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets>
    <sheet name="TR - www.borsaistanbul.com" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
  </sheets>
</workbook>""")

        strings = ["Açıklama", "Web Sitesi Dizin Adresi", "Dosya Adı"]
        for desc, (d_path, f_name) in entries.items():
            strings.extend([desc, d_path, f_name])

        sst_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">\n'
        for s in strings:
            sst_xml += f"  <si><t>{s}</t></si>\n"
        sst_xml += "</sst>"
        xzf.writestr("xl/sharedStrings.xml", sst_xml)

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


def build_mock_kmtp_xlsx(
    fiyatlar_rows: Optional[Dict[int, Dict[str, str]]] = None,
    seri_rows: Optional[Dict[int, Dict[str, str]]] = None,
) -> bytes:
    """
    Creates an in-memory XLSX workbook matching KMP_Bulten_BISTECH.xlsx structure.
    """
    if fiyatlar_rows is None:
        fiyatlar_rows = {
            2: {"B": "Referans Fiyat ve Metal Fiyatlari", "E": "Altın", "F": "Gümüş", "G": "Platin", "H": "Paladyum"},
            3: {"B": "Referans Fiyat (TRY/KG)", "E": "7135618.04", "F": "105882.96", "G": "2938199.49", "H": "2413521.01"},
            4: {"B": "Metal Fiyati (TRY/KG)", "E": "7140695.59", "F": "106000", "G": "2875056.82", "H": "2067037.85"},
            5: {"B": "Metal Fiyati (USD/ONS)", "E": "4615.96", "F": "68.20", "G": "1859.26", "H": "1336.15"},
            6: {"B": "Metal Fiyati (EUR/ONS)", "E": "3962.63", "F": "58.79", "G": "1593.33", "H": "1145.53"},
        }

    if seri_rows is None:
        seri_rows = {
            2: {
                "A": "Seriler", "B": "Kıymetli Maden", "C": "Tip", "D": "Market Segment", "E": "Bar Tipi",
                "F": "Fiyat Birimi", "G": "Ağırlık", "H": "Ağırlık Birimi", "I": "Ayar", "J": "Kasa",
                "K": "Takas Tarihi", "L": "Adet", "M": "TL İşlem Hacmi", "N": "USD İşlem Hacmi",
                "O": "EURO İşlem Hacmi", "P": "İşlem Sayısı", "Q": "AOF", "R": "En Yüksek Fiyat",
                "S": "En Düşük Fiyat", "T": "Kapanış Fiyatı"
            },
            3: {
                "A": "AU_TL_S_995.0_BIM_1K_2608", "B": "Altın", "C": "Standart", "D": "KMP ALTIN - STANDART (TRY)",
                "E": "Külçe", "F": "TRY/KG", "G": "1", "H": "KG", "I": "995", "J": "Merkez",
                "K": "2608", "L": "427", "M": "3033831636", "N": "63189423.32", "O": "54186015.08",
                "P": "69", "Q": "7140696", "R": "7156999", "S": "7117100", "T": "7121200"
            },
            4: {
                "A": "AG_TL_S_99.90_GIM_25K_2608", "B": "Gümüş", "C": "Standart", "D": "KMP GUMUS - STANDART (TRY)",
                "E": "Granül Torbası", "F": "TRY/KG", "G": "25", "H": "KG", "I": "99.9", "J": "Merkez",
                "K": "2608", "L": "1", "M": "2647350", "N": "55139.68", "O": "47283.22",
                "P": "1", "Q": "106000", "R": "106000", "S": "106000", "T": "106000"
            },
            5: {
                "A": "PT_US_S_999.5_BIM_1K_2608", "B": "Platin", "C": "Standart", "D": "KMP PLATIN",
                "E": "Külçe", "F": "USD/OZ", "G": "1", "H": "KG", "I": "999.5", "J": "Merkez",
                "K": "2608", "L": "1", "M": "50000", "N": "1859.26", "O": "1593.33",
                "P": "1", "Q": "1859.26", "R": "1859.26", "S": "1859.26", "T": "1859.26"
            }
        }

    # Collect shared strings
    shared_strings: List[str] = []
    str_to_idx: Dict[str, int] = {}

    def get_str_idx(s: str) -> int:
        if s not in str_to_idx:
            str_to_idx[s] = len(shared_strings)
            shared_strings.append(s)
        return str_to_idx[s]

    xlsx_buf = io.BytesIO()
    with zipfile.ZipFile(xlsx_buf, "w", zipfile.ZIP_DEFLATED) as xzf:
        # [Content_Types].xml
        xzf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/worksheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/worksheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
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
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/worksheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/worksheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>""")

        # xl/workbook.xml
        xzf.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets>
    <sheet name="Fiyatlar" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
    <sheet name="Seri İstatistikleri" sheetId="2" r:id="rId2" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
  </sheets>
</workbook>""")

        # Build sheet 1: Fiyatlar
        ws1_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n<sheetData>\n'
        for r_num, cells in fiyatlar_rows.items():
            ws1_xml += f'  <row r="{r_num}">\n'
            for col, val in cells.items():
                s_idx = get_str_idx(val)
                ws1_xml += f'    <c r="{col}{r_num}" t="s"><v>{s_idx}</v></c>\n'
            ws1_xml += "  </row>\n"
        ws1_xml += "</sheetData>\n</worksheet>"
        xzf.writestr("xl/worksheets/worksheet1.xml", ws1_xml)

        # Build sheet 2: Seri İstatistikleri
        ws2_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n<sheetData>\n'
        for r_num, cells in seri_rows.items():
            ws2_xml += f'  <row r="{r_num}">\n'
            for col, val in cells.items():
                s_idx = get_str_idx(val)
                ws2_xml += f'    <c r="{col}{r_num}" t="s"><v>{s_idx}</v></c>\n'
            ws2_xml += "  </row>\n"
        ws2_xml += "</sheetData>\n</worksheet>"
        xzf.writestr("xl/worksheets/worksheet2.xml", ws2_xml)

        # Write xl/sharedStrings.xml
        sst_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">\n'
        for s in shared_strings:
            sst_xml += f"  <si><t>{s}</t></si>\n"
        sst_xml += "</sst>"
        xzf.writestr("xl/sharedStrings.xml", sst_xml)

    return xlsx_buf.getvalue()


def build_mock_kmtp_bulletin_zip(xlsx_bytes: bytes, filename: str = "KMP_Bulten_BISTECH.xlsx") -> bytes:
    """Creates a ZIP archive wrapping the KMTP XLSX."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, xlsx_bytes)
        zf.writestr("KMP_Bulten.pdf", b"%PDF-1.4 mock pdf content")
    return buf.getvalue()


@pytest.fixture
def default_manifest() -> BISTDirectoryManifest:
    raw_zip = build_mock_data_file_paths_zip({
        "Kıymetli Madenler Piyasası Günlük Bülten": ("/data/kmpbltn/YYYY/AA/", "KMPYYYYAAGG.zip"),
        "Bülten Verileri": ("/data/thb/YYYY/AA/", "thbYYYYAAGGS.zip"),
    })
    return BISTDirectoryManifestParser.parse_manifest_bytes(raw_zip)


# ─────────────────────────────────────────────────────────────────────────────
# 1. BIST KMTP Manifest Discovery & Locator Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTKMTPDiscoveryAndLocator:

    def test_01_manifest_resolves_kmtp_bulletin_path(self, default_manifest):
        """Scenario 1: Locator derives verified KMTP URL from manifest."""
        locator = BISTPreciousMetalsBulletinLocator()
        res = locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=default_manifest)

        assert res.official_filename == "KMP20241001.zip"
        assert res.resolved_download_url == "https://www.borsaistanbul.com/data/kmpbltn/2024/10/KMP20241001.zip"
        assert res.requested_trade_date == date(2024, 10, 1)
        assert res.filename_trade_date == date(2024, 10, 1)

    def test_02_no_guessed_url_without_verified_manifest(self):
        """Scenario 2: Locator without verified manifest fails closed (DISCOVERY_UNAVAILABLE)."""
        locator = BISTPreciousMetalsBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="DISCOVERY_UNAVAILABLE"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=None)

    def test_03_unsafe_host_and_insecure_scheme_rejected(self):
        """Scenario 3: Non-HTTPS and non-whitelisted hosts rejected."""
        manifest_evil = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Kıymetli Madenler Piyasası Günlük Bülten": ("https://evil.example/data/", "KMP.zip")})
        )
        locator = BISTPreciousMetalsBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="UNSAFE_RESOLVED_URL"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_evil)

    def test_04_path_traversal_rejected(self):
        """Scenario 4: Path traversal in directory template rejected."""
        manifest_traversal = BISTDirectoryManifestParser.parse_manifest_bytes(
            build_mock_data_file_paths_zip({"Kıymetli Madenler Piyasası Günlük Bülten": ("/data/../../etc/", "KMP.zip")})
        )
        locator = BISTPreciousMetalsBulletinLocator()
        with pytest.raises(BISTResourceResolutionError, match="UNSAFE_RESOLVED_URL"):
            locator.resolve_bulletin_resource(trade_date=date(2024, 10, 1), manifest=manifest_traversal)


# ─────────────────────────────────────────────────────────────────────────────
# 2. BIST KMTP Parser & Schema Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTKMTPParser:

    def test_05_actual_schema_gold_rows_parse_cleanly(self):
        """Scenario 5: Gold benchmark and transaction rows parse with exact dimensions."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        zip_bytes = build_mock_kmtp_bulletin_zip(xlsx_bytes)

        observations = BISTKMTPBulletinParser.parse_bulletin_bytes(
            raw_bytes=zip_bytes,
            filename="KMP20241001.zip",
            trade_date=date(2024, 10, 1),
        )

        gold_obs = [o for o in observations if o.metal == PreciousMetalType.GOLD and o.status == PreciousMetalObservationStatus.VALID]
        assert len(gold_obs) >= 4

        # Check Referans Fiyat (TRY/KG)
        gold_ref = next(o for o in gold_obs if o.price_type == PreciousMetalPriceType.REFERENCE)
        assert gold_ref.price == Decimal("7135618.04")
        assert gold_ref.price_currency == Currency.TRY
        assert gold_ref.quantity_unit == PreciousMetalUnit.KG

        # Check Metal Fiyatı (USD/ONS)
        gold_usd = next(o for o in gold_obs if o.price_type == PreciousMetalPriceType.METAL_PRICE and o.price_currency == Currency.USD)
        assert gold_usd.price == Decimal("4615.96")
        assert gold_usd.quantity_unit == PreciousMetalUnit.TROY_OZ

    def test_06_actual_schema_silver_rows_parse_cleanly(self):
        """Scenario 6: Silver benchmark and transaction rows parse with exact dimensions."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        zip_bytes = build_mock_kmtp_bulletin_zip(xlsx_bytes)

        observations = BISTKMTPBulletinParser.parse_bulletin_bytes(
            raw_bytes=zip_bytes,
            filename="KMP20241001.zip",
            trade_date=date(2024, 10, 1),
        )

        silver_obs = [o for o in observations if o.metal == PreciousMetalType.SILVER and o.status == PreciousMetalObservationStatus.VALID]
        assert len(silver_obs) >= 3

        silver_ref = next(o for o in silver_obs if o.price_type == PreciousMetalPriceType.REFERENCE)
        assert silver_ref.price == Decimal("105882.96")
        assert silver_ref.price_currency == Currency.TRY
        assert silver_ref.quantity_unit == PreciousMetalUnit.KG

        silver_usd = next(o for o in silver_obs if o.price_type == PreciousMetalPriceType.METAL_PRICE and o.price_currency == Currency.USD)
        assert silver_usd.price == Decimal("68.20")
        assert silver_usd.quantity_unit == PreciousMetalUnit.TROY_OZ

    def test_07_purity_preserved_from_series(self):
        """Scenario 7: Purity (Ayar) preserved with raw representation, scale, and canonical fineness."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        observations = BISTKMTPBulletinParser.parse_xlsx_bytes(xlsx_bytes, trade_date=date(2024, 10, 1))

        gold_series = [o for o in observations if o.raw_symbol and "AU_TL" in o.raw_symbol]
        assert len(gold_series) >= 1
        assert gold_series[0].raw_purity_value == Decimal("995")
        assert gold_series[0].raw_purity_text == "995"
        assert gold_series[0].purity_scale == "PER_MILLE"
        assert gold_series[0].fineness_per_mille == Decimal("995")

        silver_series = [o for o in observations if o.raw_symbol and "AG_TL" in o.raw_symbol]
        assert len(silver_series) >= 1
        assert silver_series[0].raw_purity_value == Decimal("99.9")
        assert silver_series[0].raw_purity_text == "99.9"
        assert silver_series[0].purity_scale == "PERCENT"
        assert silver_series[0].fineness_per_mille == Decimal("999.0")

    def test_08_zero_float_conversion_rejected(self):
        """Scenario 8: Float input to parse_kmtp_decimal strictly raises TypeError."""
        with pytest.raises(TypeError, match="Float input prohibited"):
            parse_kmtp_decimal(4615.96)
        with pytest.raises(TypeError, match="Float input prohibited"):
            parse_kmtp_int(12.0)

    def test_09_malformed_price_becomes_none(self):
        """Scenario 9: Missing or malformed price remains None (never 0.0) and marks INVALID_OBSERVATION."""
        corrupt_fiyatlar = {
            2: {"B": "Referans Fiyat ve Metal Fiyatlari", "E": "Altın", "F": "Gümüş"},
            3: {"B": "Referans Fiyat (TRY/KG)", "E": "-", "F": "CORRUPT"},
        }
        xlsx_bytes = build_mock_kmtp_xlsx(fiyatlar_rows=corrupt_fiyatlar, seri_rows={})
        observations = BISTKMTPBulletinParser.parse_xlsx_bytes(xlsx_bytes, trade_date=date(2024, 10, 1))

        assert len(observations) == 2
        for obs in observations:
            assert obs.price is None
            assert obs.status == PreciousMetalObservationStatus.INVALID_OBSERVATION

    def test_10_unsupported_metal_classified_safely(self):
        """Scenario 10: Unsupported metals (Platinum, Palladium) classified as UNSUPPORTED_METAL."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        observations = BISTKMTPBulletinParser.parse_xlsx_bytes(xlsx_bytes, trade_date=date(2024, 10, 1))

        pt_obs = [o for o in observations if o.raw_symbol and "PT_US" in o.raw_symbol]
        assert len(pt_obs) >= 1
        assert pt_obs[0].status == PreciousMetalObservationStatus.UNSUPPORTED_METAL
        assert any("Unsupported" in d for d in pt_obs[0].diagnostics)

    def test_11_duplicate_conflict_quarantine(self):
        """Scenario 11: Duplicate rows with conflicting prices are quarantined deterministically."""
        conflicting_seri = {
            2: {
                "A": "Seriler", "B": "Kıymetli Maden", "C": "Tip", "D": "Market Segment", "E": "Bar Tipi",
                "F": "Fiyat Birimi", "G": "Ağırlık", "H": "Ağırlık Birimi", "I": "Ayar", "J": "Kasa",
                "K": "Takas Tarihi", "L": "Adet", "M": "TL İşlem Hacmi", "N": "USD İşlem Hacmi",
                "O": "EURO İşlem Hacmi", "P": "İşlem Sayısı", "Q": "AOF", "R": "En Yüksek Fiyat",
                "S": "En Düşük Fiyat", "T": "Kapanış Fiyatı"
            },
            3: {
                "A": "AU_TL_S_995.0_BIM_1K_2608", "B": "Altın", "F": "TRY/KG", "I": "995",
                "K": "2608", "Q": "7140000", "T": "7120000"
            },
            4: {
                "A": "AU_TL_S_995.0_BIM_1K_2608", "B": "Altın", "F": "TRY/KG", "I": "995",
                "K": "2608", "Q": "7150000", "T": "7130000"
            }
        }
        xlsx_bytes = build_mock_kmtp_xlsx(fiyatlar_rows={}, seri_rows=conflicting_seri)
        observations = BISTKMTPBulletinParser.parse_xlsx_bytes(xlsx_bytes, trade_date=date(2024, 10, 1))

        assert len(observations) == 4  # 2 AOF + 2 Close
        assert all(o.status == PreciousMetalObservationStatus.CONFLICT_QUARANTINED for o in observations)

    def test_11b_historical_date_from_kmp_filename(self):
        """Scenario 11b: Date parsed from KMPYYYYMMDD.zip when trade_date is None (never system today)."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        observations = BISTKMTPBulletinParser.parse_xlsx_bytes(
            xlsx_bytes=xlsx_bytes,
            filename="KMP20241001.zip",
            trade_date=None,
        )
        assert len(observations) > 0
        for obs in observations:
            assert obs.effective_date == date(2024, 10, 1)

    def test_11c_date_mismatch_fails_closed(self):
        """Scenario 11c: Mismatch between requested trade_date and filename date fails closed."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        with pytest.raises(BISTKMTPSchemaDriftError, match="does not match verified filename date"):
            BISTKMTPBulletinParser.parse_xlsx_bytes(
                xlsx_bytes=xlsx_bytes,
                filename="KMP20241001.zip",
                trade_date=date(2024, 10, 2),
            )

    def test_11d_no_date_fails_closed(self):
        """Scenario 11d: Missing both trade_date and filename date fails closed (no fallback to today)."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        with pytest.raises(BISTKMTPSchemaDriftError, match="MISSING_EFFECTIVE_DATE"):
            BISTKMTPBulletinParser.parse_xlsx_bytes(
                xlsx_bytes=xlsx_bytes,
                filename="unrelated_file.xlsx",
                trade_date=None,
            )

    def test_11e_summary_benchmarks_have_none_purity_and_settlement(self):
        """Scenario 11e: Summary benchmarks from 'Fiyatlar' sheet have purity=None and settlement_term=None."""
        xlsx_bytes = build_mock_kmtp_xlsx()
        observations = BISTKMTPBulletinParser.parse_xlsx_bytes(xlsx_bytes, trade_date=date(2024, 10, 1))

        benchmarks = [o for o in observations if o.price_type in (PreciousMetalPriceType.REFERENCE, PreciousMetalPriceType.METAL_PRICE)]
        assert len(benchmarks) > 0
        for b in benchmarks:
            assert b.purity is None
            assert b.raw_purity_value is None
            assert b.purity_scale is None
            assert b.fineness_per_mille is None
            assert b.settlement_term is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provider Fetch, PIT & Error Handling Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTKMTPProvider:

    @pytest.mark.asyncio
    async def test_12_fetch_daily_bulletin_success(self):
        """Scenario 12: Successful discovery, download, and parsing of daily KMTP bulletin."""
        mock_manifest_zip = build_mock_data_file_paths_zip({
            "Kıymetli Madenler Piyasası Günlük Bülten": ("/data/kmpbltn/YYYY/AA/", "KMPYYYYAAGG.zip")
        })
        mock_kmtp_zip = build_mock_kmtp_bulletin_zip(build_mock_kmtp_xlsx())

        mock_client = MagicMock(spec=httpx.AsyncClient)

        def mock_get(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = mock_manifest_zip if "DataFilePaths.zip" in url else mock_kmtp_zip
            resp.headers = {"content-type": "application/zip"}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        provider = BISTKMTPProvider(http_client=mock_client)
        ctx = FetchContext(
            observation_type="PRECIOUS_METAL_MARKET_REFERENCE",
            effective_date=date(2024, 10, 1),
        )
        response = await provider.fetch(ctx)

        assert response.status == DataStatus.COMPLETE
        assert response.provider_name == "BIST_KMTP"
        assert response.effective_date == date(2024, 10, 1)
        assert len(response.raw) > 0

    @pytest.mark.asyncio
    async def test_13_weekend_non_trading_day_no_network(self):
        """Scenario 13: Requesting a weekend date immediately returns UNAVAILABLE with 0 network calls."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock()

        provider = BISTKMTPProvider(http_client=mock_client)
        # 2024-10-06 is Sunday
        ctx = FetchContext(
            observation_type="PRECIOUS_METAL_MARKET_REFERENCE",
            effective_date=date(2024, 10, 6),
        )
        response = await provider.fetch(ctx)

        assert response.status == DataStatus.UNAVAILABLE
        assert any("Weekend" in w for w in response.warnings)
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_14_fetch_single_metal_via_context(self):
        """Scenario 14: Requesting a specific registered provider_symbol filters observations."""
        mock_manifest_zip = build_mock_data_file_paths_zip({
            "Kıymetli Madenler Piyasası Günlük Bülten": ("/data/kmpbltn/YYYY/AA/", "KMPYYYYAAGG.zip")
        })
        mock_kmtp_zip = build_mock_kmtp_bulletin_zip(build_mock_kmtp_xlsx())

        mock_client = MagicMock(spec=httpx.AsyncClient)

        def mock_get(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = mock_manifest_zip if "DataFilePaths.zip" in url else mock_kmtp_zip
            resp.headers = {"content-type": "application/zip"}
            return resp

        mock_client.get = AsyncMock(side_effect=mock_get)

        provider = BISTKMTPProvider(http_client=mock_client)
        ctx = FetchContext(
            observation_type="PRECIOUS_METAL_MARKET_REFERENCE",
            provider_symbol="BIST_KMTP_GOLD_REF_TRY_KG",
            effective_date=date(2024, 10, 1),
        )
        response = await provider.fetch(ctx)

        assert response.status == DataStatus.COMPLETE
        assert response.raw["price"] == "7135618.04"
        assert response.raw["metal"] == "GOLD"
        assert response.raw["price_currency"] == "TRY"


# ─────────────────────────────────────────────────────────────────────────────
# 4. TCMB EVDS Series Registry & Metadata Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTCMBEVDSPreciousMetals:

    def test_15_unverified_evds_series_are_disabled_and_unverified(self):
        """Scenario 15: Unverified EVDS precious-metals series definitions are marked UNVERIFIED and is_active=False."""
        from backend.engine.private.precious_metals.models import SeriesVerificationStatus

        for code in ("TP.MK.G.ALTIN.USD", "TP.MK.G.ALTIN.TRY", "TP.MK.G.GUMUS.USD"):
            defn = PreciousMetalSeriesRegistry.get(code)
            assert defn is not None
            assert defn.verification_status == SeriesVerificationStatus.UNVERIFIED
            assert defn.is_active is False
            assert PreciousMetalSeriesRegistry.is_verified(code) is False
            assert PreciousMetalSeriesRegistry.get_verified(code) is None

    @pytest.mark.asyncio
    async def test_16_tcmb_evds_provider_refuses_unverified_precious_metals_no_network(self):
        """Scenario 16: TCMBEVDSProvider refuses unverified precious-metals series with 0 HTTP calls."""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock()

        provider = TCMBEVDSProvider(api_key="test_key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="PRECIOUS_METAL_MARKET_REFERENCE",
            provider_symbol="TP.MK.G.ALTIN.USD",
            effective_date=date(2024, 10, 1),
        )
        res = await provider.fetch(ctx)

        assert res.status == DataStatus.UNAVAILABLE
        assert any("unverified or disabled" in w for w in res.warnings)
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_17_verified_evds_precious_metal_exact_decimal_normalization(self):
        """Scenario 17: A verified EVDS precious-metal series parses price as exact Decimal (zero float)."""
        from backend.engine.private.precious_metals.models import SeriesVerificationStatus

        # Register a verified test-only definition
        test_defn = PreciousMetalSeriesDefinition(
            series_code="TP.MK.G.TEST_GOLD.USD",
            canonical_name="Test Verified Gold Series",
            metal=PreciousMetalType.GOLD,
            provider="TCMB_EVDS",
            originating_source="BIST",
            frequency="DAILY",
            value_unit="USD/ONS",
            currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            verification_status=SeriesVerificationStatus.VERIFIED,
            is_active=True,
        )
        PreciousMetalSeriesRegistry.register(test_defn)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_resp = httpx.Response(
            status_code=200,
            json={
                "items": [
                    {
                        "Tarih": "01-10-2024",
                        "UNIXTIME": "1727740800",
                        "TP_MK_G_TEST_GOLD_USD": "4615.960000",
                    }
                ]
            },
        )
        mock_client.get = AsyncMock(return_value=mock_resp)

        provider = TCMBEVDSProvider(api_key="test_key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="PRECIOUS_METAL_MARKET_REFERENCE",
            provider_symbol="TP.MK.G.TEST_GOLD.USD",
            effective_date=date(2024, 10, 1),
        )
        res = await provider.fetch(ctx)

        assert res.status == DataStatus.COMPLETE
        # Normalize directly for precious metal series returns exact Decimal
        norm = provider.normalize(mock_resp.json(), is_precious_metal=True)
        assert isinstance(norm["value"], Decimal)
        assert norm["value"] == Decimal("4615.960000")

        # Zero is valid Decimal("0")
        zero_payload = {"items": [{"Tarih": "01-10-2024", "VAL": "0"}]}
        norm_zero = provider.normalize(zero_payload, is_precious_metal=True)
        assert norm_zero["value"] == Decimal("0")

        # Malformed becomes None
        bad_payload = {"items": [{"Tarih": "01-10-2024", "VAL": "BAD"}]}
        norm_bad = provider.normalize(bad_payload, is_precious_metal=True)
        assert norm_bad["value"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cross-Source Strict Semantic Comparability Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPreciousMetalCrossSourceComparator:

    def test_18_same_dimensions_and_same_price_is_consistent(self):
        """Scenario 18: Same 8 semantic dimensions and same price => CONSISTENT."""
        t_date = date(2024, 10, 1)
        obs_a = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            raw_purity_value=Decimal("995.0"),
            purity_scale="PER_MILLE",
            fineness_per_mille=Decimal("995.0"),
            settlement_term="T+0",
            provider="BIST_KMTP",
        )
        obs_b = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.TCMB_EVDS,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            raw_purity_value=Decimal("995.0"),
            purity_scale="PER_MILLE",
            fineness_per_mille=Decimal("995.0"),
            settlement_term="T+0",
            provider="TCMB_EVDS",
        )

        res = PreciousMetalCrossSourceComparator.compare(obs_a, obs_b)
        assert res.status == ComparabilityStatus.CONSISTENT
        assert res.is_comparable is True
        assert res.difference == Decimal("0")

    def test_19_same_dimensions_and_different_price_is_divergent(self):
        """Scenario 19: Same dimensions with differing prices => DIVERGENT."""
        t_date = date(2024, 10, 1)
        obs_a = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            raw_purity_value=Decimal("995.0"),
            purity_scale="PER_MILLE",
            fineness_per_mille=Decimal("995.0"),
            settlement_term="T+0",
        )
        obs_b = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.TCMB_EVDS,
            effective_date=t_date,
            price=Decimal("4618.50"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            raw_purity_value=Decimal("995.0"),
            purity_scale="PER_MILLE",
            fineness_per_mille=Decimal("995.0"),
            settlement_term="T+0",
        )

        res = PreciousMetalCrossSourceComparator.compare(obs_a, obs_b)
        assert res.status == ComparabilityStatus.DIVERGENT
        assert res.is_comparable is True
        assert res.difference == Decimal("2.54")

    def test_20_different_currency_is_not_comparable(self):
        """Scenario 20: Differing currency (TRY vs USD) => NOT_COMPARABLE."""
        t_date = date(2024, 10, 1)
        obs_try = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("7140695.59"),
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        )
        obs_usd = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.TCMB_EVDS,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        )

        res = PreciousMetalCrossSourceComparator.compare(obs_try, obs_usd)
        assert res.status == ComparabilityStatus.NOT_COMPARABLE
        assert res.is_comparable is False
        assert any("CURRENCY_MISMATCH" in r for r in res.reasons)

    def test_21_different_quantity_unit_is_not_comparable(self):
        """Scenario 21: Differing quantity unit (KG vs TROY_OZ) => NOT_COMPARABLE."""
        t_date = date(2024, 10, 1)
        obs_kg = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        )
        obs_oz = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.TCMB_EVDS,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        )

        res = PreciousMetalCrossSourceComparator.compare(obs_kg, obs_oz)
        assert res.status == ComparabilityStatus.NOT_COMPARABLE
        assert res.is_comparable is False
        assert any("UNIT_MISMATCH" in r for r in res.reasons)

    def test_22_different_purity_or_unknown_purity_is_not_comparable(self):
        """Scenario 22: Differing or unknown purity => NOT_COMPARABLE."""
        t_date = date(2024, 10, 1)
        obs_995 = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            raw_purity_value=Decimal("995.0"),
            purity_scale="PER_MILLE",
            fineness_per_mille=Decimal("995.0"),
        )
        obs_none = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.TCMB_EVDS,
            effective_date=t_date,
            price=Decimal("4615.96"),
            price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            raw_purity_value=None,
            purity_scale=None,
            fineness_per_mille=None,
        )

        res = PreciousMetalCrossSourceComparator.compare(obs_995, obs_none)
        assert res.status == ComparabilityStatus.NOT_COMPARABLE
        assert any("PURITY" in r for r in res.reasons)

    def test_23_different_settlement_or_unknown_settlement_is_not_comparable(self):
        """Scenario 23: Differing settlement term (None vs T+0) => NOT_COMPARABLE."""
        t_date = date(2024, 10, 1)
        obs_none = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("7135618.04"),
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.REFERENCE,
            settlement_term=None,
        )
        obs_t0 = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=t_date,
            price=Decimal("7135618.04"),
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.REFERENCE,
            settlement_term="T+0",
        )

        res = PreciousMetalCrossSourceComparator.compare(obs_none, obs_t0)
        assert res.status == ComparabilityStatus.NOT_COMPARABLE
        assert any("SETTLEMENT_MISMATCH" in r for r in res.reasons)

    def test_24_serialization_to_generic_pit_records_preserves_lineage(self):
        """Scenario 24: Precious metals models preserve snapshot lineage without generating fake UUIDs."""
        # 1. Observation with explicit snapshot_id
        snap_id = uuid4()
        obs = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=date(2024, 10, 1),
            price=Decimal("7135618.04"),
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.REFERENCE,
            raw_purity_value=None,
            raw_symbol="BIST_KMTP_GOLD_REF_TRY_KG",
            snapshot_id=snap_id,
        )
        norm_rec = obs.to_normalized_observation_record()
        assert norm_rec.snapshot_id == snap_id
        assert norm_rec.observation_type == "PRECIOUS_METAL_MARKET_REFERENCE"
        assert norm_rec.asset_class == AssetClass.COMMODITY
        assert norm_rec.instrument_type == InstrumentType.GOLD
        assert norm_rec.effective_date == date(2024, 10, 1)
        assert norm_rec.observation_data["value"] == "7135618.04"
        assert norm_rec.currency == Currency.TRY

        # 2. Observation without snapshot_id preserves None (no fabricated UUID)
        obs_no_snap = PreciousMetalMarketObservation(
            metal=PreciousMetalType.GOLD,
            market=PreciousMetalMarket.BIST_KMTP,
            effective_date=date(2024, 10, 1),
            price=Decimal("7135618.04"),
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.REFERENCE,
            snapshot_id=None,
        )
        norm_rec_2 = obs_no_snap.to_normalized_observation_record()
        assert norm_rec_2.snapshot_id is None

        # 3. Snapshot record conversion
        snap = PreciousMetalSnapshot(
            trade_date=date(2024, 10, 1),
            retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
            http_status=200,
            payload_hash="sample_hash",
            content_type="application/zip",
            observations=[obs],
        )
        raw_rec = snap.to_raw_snapshot_record()
        assert raw_rec.provider == "BIST_KMTP"
        assert raw_rec.payload_hash == "sample_hash"
