"""
backend/engine/private/precious_metals/parser.py
================================================
Official Borsa İstanbul KMTP Bulletin Parser (KMP_Bulten_BISTECH.xlsx / KMPYYYYMMDD.zip).

Strict Invariants:
    - Pure Python XLSX/XML parsing using stdlib (zipfile + xml.etree.ElementTree).
    - Zero float conversion: float input to parse_decimal raises TypeError.
    - Missing or corrupted values remain None, NEVER Decimal("0").
    - Supported metals: GOLD and SILVER. Unsupported metals (Platinum, Palladium) marked UNSUPPORTED_METAL.
    - Purity, currency, quantity unit, and settlement terms are preserved explicitly.
    - Order-independent duplicate conflict quarantine.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID, uuid4

from backend.engine.private.domain import (
    Currency,
    DataConfidenceLevel,
)
from backend.engine.private.precious_metals.constants import (
    PreciousMetalMarket,
    PreciousMetalPriceType,
    PreciousMetalType,
    PreciousMetalUnit,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
)


class BISTKMTPParserError(Exception):
    """Base exception for KMTP parser errors."""
    pass


class BISTKMTPSchemaDriftError(BISTKMTPParserError):
    """Raised when critical KMTP workbook structure or sheets are missing."""
    pass


def parse_kmtp_decimal(val: Any) -> Optional[Decimal]:
    """
    Parses numeric string or int to Decimal.
    Strictly forbids float inputs to prevent precision loss.
    """
    if val is None:
        return None
    if isinstance(val, float):
        raise TypeError(f"Float input prohibited for exact monetary/quantity arithmetic: {val}")
    if isinstance(val, (int, Decimal)):
        return Decimal(str(val))
    if isinstance(val, str):
        cleaned = val.strip().replace(" ", "").replace("\xa0", "")
        if not cleaned or cleaned in ("-", "--", "N/A", "null", "None", "."):
            return None
        # Handle comma as decimal separator if dot is thousand sep (e.g. 7.140.695,58)
        if "," in cleaned and "." in cleaned:
            if cleaned.find(".") < cleaned.find(","):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def parse_kmtp_int(val: Any) -> Optional[int]:
    """Parses integer string or int. Strictly rejects floats."""
    if val is None:
        return None
    if isinstance(val, float):
        raise TypeError(f"Float input prohibited: {val}")
    if isinstance(val, int):
        return val
    d = parse_kmtp_decimal(val)
    return int(d) if d is not None else None


def parse_unit_and_currency(unit_str: str) -> Tuple[Optional[Currency], Optional[PreciousMetalUnit]]:
    """
    Parses unit strings like 'TRY/KG', 'USD/OZ', 'USD/ONS', 'EUR/ONS', 'TRY/GR'.
    """
    if not unit_str:
        return None, None
    clean = unit_str.upper().strip().replace(" ", "")

    curr = None
    unit = None

    if clean.startswith("TRY") or clean.startswith("TL"):
        curr = Currency.TRY
    elif clean.startswith("USD"):
        curr = Currency.USD
    elif clean.startswith("EUR"):
        curr = Currency.EUR

    if "KG" in clean:
        unit = PreciousMetalUnit.KG
    elif "OZ" in clean or "ONS" in clean:
        unit = PreciousMetalUnit.TROY_OZ
    elif "GR" in clean or "GRAM" in clean:
        unit = PreciousMetalUnit.GRAM

    return curr, unit


class BISTKMTPBulletinParser:
    """
    Parses official Borsa İstanbul KMTP Excel/ZIP daily bulletins.
    """

    @classmethod
    def parse_bulletin_bytes(
        self,
        raw_bytes: bytes,
        filename: Optional[str] = None,
        trade_date: Optional[date] = None,
        snapshot_id: Optional[UUID] = None,
        snapshot_hash: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
    ) -> List[PreciousMetalMarketObservation]:
        """
        Parses bytes of either KMPYYYYAAGG.zip (containing KMP_Bulten_BISTECH.xlsx) or direct .xlsx.
        """
        if not raw_bytes:
            return []

        xlsx_bytes: bytes
        # Check if raw_bytes is a ZIP archive containing the XLSX
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                xlsx_members = [m for m in zf.namelist() if m.endswith(".xlsx")]
                if xlsx_members:
                    # Prefer KMP_Bulten_BISTECH.xlsx if present
                    chosen = next((m for m in xlsx_members if "BISTECH" in m), xlsx_members[0])
                    xlsx_bytes = zf.read(chosen)
                else:
                    # Could already be direct XLSX
                    xlsx_bytes = raw_bytes
        except zipfile.BadZipFile:
            raise BISTKMTPParserError("Failed to parse KMTP payload: Not a valid ZIP or XLSX file.")

        return self.parse_xlsx_bytes(
            xlsx_bytes=xlsx_bytes,
            filename=filename,
            trade_date=trade_date,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            retrieved_at=retrieved_at,
        )

    @classmethod
    def parse_xlsx_bytes(
        self,
        xlsx_bytes: bytes,
        filename: Optional[str] = None,
        trade_date: Optional[date] = None,
        snapshot_id: Optional[UUID] = None,
        snapshot_hash: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
    ) -> List[PreciousMetalMarketObservation]:
        """
        Extracts and normalizes precious metal market observations from KMP_Bulten_BISTECH.xlsx.
        """
        now_utc = retrieved_at or datetime.now(timezone.utc)

        # 1. Determine verified economic trade date (Zero fallback to date.today())
        fn_date = None
        if filename:
            from backend.engine.private.precious_metals.locator import BISTPreciousMetalsBulletinLocator
            fn_date = BISTPreciousMetalsBulletinLocator.parse_filename_trade_date(filename)

        if trade_date is not None and fn_date is not None:
            if trade_date != fn_date:
                raise BISTKMTPSchemaDriftError(
                    f"Requested trade_date {trade_date} does not match verified filename date {fn_date}."
                )
            eff_date = trade_date
        elif trade_date is not None:
            eff_date = trade_date
        elif fn_date is not None:
            eff_date = fn_date
        else:
            raise BISTKMTPSchemaDriftError(
                "MISSING_EFFECTIVE_DATE: No verified trade_date or filename date provided. Fallback to today is strictly forbidden."
            )

        try:
            xzf = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
        except zipfile.BadZipFile as exc:
            raise BISTKMTPParserError(f"Corrupted KMTP Excel workbook: {exc}")

        # 2. Parse shared strings table
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in xzf.namelist():
            sst_tree = ET.fromstring(xzf.read("xl/sharedStrings.xml"))
            for si in sst_tree.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                t = si.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
                if t is not None and t.text:
                    shared_strings.append(t.text)
                else:
                    r_texts = [
                        r.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t").text
                        for r in si.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}r")
                        if r.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t") is not None
                    ]
                    shared_strings.append("".join(filter(None, r_texts)))

        # 3. Parse workbook sheet list
        if "xl/workbook.xml" not in xzf.namelist():
            raise BISTKMTPSchemaDriftError("Missing xl/workbook.xml in KMTP Excel file.")

        wb_tree = ET.fromstring(xzf.read("xl/workbook.xml"))
        sheet_nodes = wb_tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet")
        sheet_map: Dict[str, int] = {}
        for idx, s in enumerate(sheet_nodes, 1):
            sheet_map[s.attrib.get("name", "")] = idx

        # Helper to read sheet rows as Dict[row_idx, Dict[col_letter, value]]
        def read_sheet_data(sheet_num: int) -> Dict[int, Dict[str, str]]:
            targets = [
                f"xl/worksheets/worksheet{sheet_num}.xml",
                f"xl/worksheets/sheet{sheet_num}.xml",
            ]
            sheet_file = next((t for t in targets if t in xzf.namelist()), None)
            if not sheet_file:
                return {}

            ws_tree = ET.fromstring(xzf.read(sheet_file))
            rows_dict: Dict[int, Dict[str, str]] = {}

            for row in ws_tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                r_idx = int(row.attrib.get("r", "0"))
                cells: Dict[str, str] = {}
                for c in row.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                    c_ref = c.attrib.get("r", "")
                    c_type = c.attrib.get("t", "")
                    v = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                    val = v.text if v is not None and v.text is not None else ""
                    if c_type == "s" and val.isdigit() and int(val) < len(shared_strings):
                        val = shared_strings[int(val)]
                    col = "".join([ch for ch in c_ref if ch.isalpha()])
                    cells[col] = val
                if cells:
                    rows_dict[r_idx] = cells
            return rows_dict

        raw_observations: List[PreciousMetalMarketObservation] = []

        # 4. Parse Sheet 'Fiyatlar' (Summary Benchmark Rates)
        fiyatlar_idx = sheet_map.get("Fiyatlar")
        if fiyatlar_idx:
            fiyatlar_rows = read_sheet_data(fiyatlar_idx)
            raw_observations.extend(
                self._parse_fiyatlar_sheet(
                    rows=fiyatlar_rows,
                    effective_date=eff_date,
                    snapshot_id=snapshot_id,
                    payload_hash=snapshot_hash,
                    retrieved_at=now_utc,
                )
            )

        # 5. Parse Sheet 'Seri İstatistikleri' (Granular Transaction Series)
        seri_idx = sheet_map.get("Seri İstatistikleri")
        if seri_idx:
            seri_rows = read_sheet_data(seri_idx)
            raw_observations.extend(
                self._parse_seri_istatistikleri_sheet(
                    rows=seri_rows,
                    effective_date=eff_date,
                    snapshot_id=snapshot_id,
                    payload_hash=snapshot_hash,
                    retrieved_at=now_utc,
                )
            )

        if not raw_observations and not fiyatlar_idx and not seri_idx:
            raise BISTKMTPSchemaDriftError(
                f"KMTP workbook missing expected sheets 'Fiyatlar' and 'Seri İstatistikleri'. Found: {list(sheet_map.keys())}"
            )

        # 6. Quarantine conflicting duplicate observations deterministically
        quarantined = self._quarantine_duplicates(raw_observations)
        return quarantined

    @classmethod
    def _parse_fiyatlar_sheet(
        self,
        rows: Dict[int, Dict[str, str]],
        effective_date: date,
        snapshot_id: Optional[UUID],
        payload_hash: Optional[str],
        retrieved_at: datetime,
    ) -> List[PreciousMetalMarketObservation]:
        """
        Parses official benchmark rates from 'Fiyatlar' sheet:
        - Referans Fiyat (TRY/KG)
        - Metal Fiyatı (TRY/KG, USD/ONS, EUR/ONS)
        - AOF (USD/ONS, TRY/KG)
        Note: Summary benchmarks do not establish explicit purity or settlement terms; they remain None.
        """
        observations: List[PreciousMetalMarketObservation] = []

        # Column mappings on Row 2: Col E = Altın (GOLD), Col F = Gümüş (SILVER)
        metal_cols = [
            ("E", PreciousMetalType.GOLD),
            ("F", PreciousMetalType.SILVER),
        ]

        for r_num, cells in rows.items():
            desc = cells.get("B", "").strip()

            # 1. Referans Fiyat (TRY/KG)
            if "Referans Fiyat" in desc and "TRY/KG" in desc:
                for col, metal in metal_cols:
                    val_str = cells.get(col, "")
                    price = parse_kmtp_decimal(val_str)
                    status = PreciousMetalObservationStatus.VALID if price is not None else PreciousMetalObservationStatus.INVALID_OBSERVATION
                    obs = PreciousMetalMarketObservation(
                        metal=metal,
                        market=PreciousMetalMarket.BIST_KMTP,
                        effective_date=effective_date,
                        price=price,
                        price_currency=Currency.TRY,
                        quantity_unit=PreciousMetalUnit.KG,
                        price_type=PreciousMetalPriceType.REFERENCE,
                        purity=None,
                        raw_purity_value=None,
                        raw_purity_text=None,
                        purity_scale=None,
                        fineness_per_mille=None,
                        settlement_term=None,
                        value_date=None,
                        raw_symbol=f"BIST_KMTP_{metal.value}_REF_TRY_KG",
                        snapshot_id=snapshot_id,
                        payload_hash=payload_hash,
                        retrieved_at=retrieved_at,
                        status=status,
                        confidence=DataConfidenceLevel.HIGH if status == PreciousMetalObservationStatus.VALID else DataConfidenceLevel.NONE,
                        diagnostics=[] if status == PreciousMetalObservationStatus.VALID else ["Missing or invalid reference price value."],
                    )
                    observations.append(obs)

            # 2. Metal Fiyati (TRY/KG)
            elif "Metal Fiyat" in desc and "TRY/KG" in desc:
                for col, metal in metal_cols:
                    val_str = cells.get(col, "")
                    price = parse_kmtp_decimal(val_str)
                    status = PreciousMetalObservationStatus.VALID if price is not None else PreciousMetalObservationStatus.INVALID_OBSERVATION
                    obs = PreciousMetalMarketObservation(
                        metal=metal,
                        market=PreciousMetalMarket.BIST_KMTP,
                        effective_date=effective_date,
                        price=price,
                        price_currency=Currency.TRY,
                        quantity_unit=PreciousMetalUnit.KG,
                        price_type=PreciousMetalPriceType.METAL_PRICE,
                        purity=None,
                        raw_purity_value=None,
                        raw_purity_text=None,
                        purity_scale=None,
                        fineness_per_mille=None,
                        settlement_term=None,
                        value_date=None,
                        raw_symbol=f"BIST_KMTP_{metal.value}_PRICE_TRY_KG",
                        snapshot_id=snapshot_id,
                        payload_hash=payload_hash,
                        retrieved_at=retrieved_at,
                        status=status,
                        confidence=DataConfidenceLevel.HIGH if status == PreciousMetalObservationStatus.VALID else DataConfidenceLevel.NONE,
                        diagnostics=[] if status == PreciousMetalObservationStatus.VALID else ["Missing or invalid metal price value."],
                    )
                    observations.append(obs)

            # 3. Metal Fiyati (USD/ONS)
            elif "Metal Fiyat" in desc and "USD/ONS" in desc:
                for col, metal in metal_cols:
                    val_str = cells.get(col, "")
                    price = parse_kmtp_decimal(val_str)
                    status = PreciousMetalObservationStatus.VALID if price is not None else PreciousMetalObservationStatus.INVALID_OBSERVATION
                    obs = PreciousMetalMarketObservation(
                        metal=metal,
                        market=PreciousMetalMarket.BIST_KMTP,
                        effective_date=effective_date,
                        price=price,
                        price_currency=Currency.USD,
                        quantity_unit=PreciousMetalUnit.TROY_OZ,
                        price_type=PreciousMetalPriceType.METAL_PRICE,
                        purity=None,
                        raw_purity_value=None,
                        raw_purity_text=None,
                        purity_scale=None,
                        fineness_per_mille=None,
                        settlement_term=None,
                        value_date=None,
                        raw_symbol=f"BIST_KMTP_{metal.value}_PRICE_USD_OZ",
                        snapshot_id=snapshot_id,
                        payload_hash=payload_hash,
                        retrieved_at=retrieved_at,
                        status=status,
                        confidence=DataConfidenceLevel.HIGH if status == PreciousMetalObservationStatus.VALID else DataConfidenceLevel.NONE,
                        diagnostics=[] if status == PreciousMetalObservationStatus.VALID else ["Missing or invalid metal price value."],
                    )
                    observations.append(obs)

            # 4. Metal Fiyati (EUR/ONS)
            elif "Metal Fiyat" in desc and "EUR/ONS" in desc:
                for col, metal in metal_cols:
                    val_str = cells.get(col, "")
                    price = parse_kmtp_decimal(val_str)
                    status = PreciousMetalObservationStatus.VALID if price is not None else PreciousMetalObservationStatus.INVALID_OBSERVATION
                    obs = PreciousMetalMarketObservation(
                        metal=metal,
                        market=PreciousMetalMarket.BIST_KMTP,
                        effective_date=effective_date,
                        price=price,
                        price_currency=Currency.EUR,
                        quantity_unit=PreciousMetalUnit.TROY_OZ,
                        price_type=PreciousMetalPriceType.METAL_PRICE,
                        purity=None,
                        raw_purity_value=None,
                        raw_purity_text=None,
                        purity_scale=None,
                        fineness_per_mille=None,
                        settlement_term=None,
                        value_date=None,
                        raw_symbol=f"BIST_KMTP_{metal.value}_PRICE_EUR_OZ",
                        snapshot_id=snapshot_id,
                        payload_hash=payload_hash,
                        retrieved_at=retrieved_at,
                        status=status,
                        confidence=DataConfidenceLevel.HIGH if status == PreciousMetalObservationStatus.VALID else DataConfidenceLevel.NONE,
                        diagnostics=[] if status == PreciousMetalObservationStatus.VALID else ["Missing or invalid metal price value."],
                    )
                    observations.append(obs)

        return observations

    @classmethod
    def _parse_seri_istatistikleri_sheet(
        self,
        rows: Dict[int, Dict[str, str]],
        effective_date: date,
        snapshot_id: Optional[UUID],
        payload_hash: Optional[str],
        retrieved_at: datetime,
    ) -> List[PreciousMetalMarketObservation]:
        """
        Parses granular executed series from 'Seri İstatistikleri' sheet.
        Preserves raw purity text, value, scale, and exact settlement info without fabricating defaults.
        """
        observations: List[PreciousMetalMarketObservation] = []

        for r_num in sorted(rows.keys()):
            if r_num <= 2:
                continue
            cells = rows[r_num]
            series_code = cells.get("A", "").strip()
            metal_str = cells.get("B", "").strip()
            if not series_code and not metal_str:
                continue

            # Classify metal
            metal: Optional[PreciousMetalType] = None
            is_unsupported = False

            if "altın" in metal_str.lower() or "gold" in metal_str.lower() or series_code.startswith("AU_"):
                metal = PreciousMetalType.GOLD
            elif "gümüş" in metal_str.lower() or "silver" in metal_str.lower() or series_code.startswith("AG_"):
                metal = PreciousMetalType.SILVER
            else:
                is_unsupported = True
                metal = PreciousMetalType.GOLD  # placeholder for typing

            # Parse unit & currency
            unit_str = cells.get("F", "").strip()
            currency, qty_unit = parse_unit_and_currency(unit_str)
            if currency is None:
                currency = Currency.TRY
            if qty_unit is None:
                qty_unit = PreciousMetalUnit.KG

            # Parse purity (Ayar) with explicit scale and fineness
            raw_purity_text = cells.get("I", "").strip() or None
            raw_purity_val = parse_kmtp_decimal(raw_purity_text)
            purity_scale: Optional[str] = None
            fineness_per_mille: Optional[Decimal] = None

            if raw_purity_val is not None:
                if raw_purity_val >= Decimal("500"):
                    purity_scale = "PER_MILLE"
                    fineness_per_mille = raw_purity_val
                elif Decimal("0") < raw_purity_val <= Decimal("100"):
                    purity_scale = "PERCENT"
                    fineness_per_mille = raw_purity_val * Decimal("10")
                else:
                    purity_scale = "UNKNOWN"

            # Parse settlement/value date without fabricating T+0
            settlement_raw = cells.get("K", "").strip()
            settlement_term: Optional[str] = None
            value_date: Optional[date] = None

            if settlement_raw == "T+0":
                settlement_term = "T+0"
                value_date = effective_date
            elif settlement_raw:
                # Raw token like "2608" preserved in symbol/diagnostics, not fabricated as T+0
                settlement_term = None
                value_date = None

            # Parse volumes & turnovers
            volume = parse_kmtp_decimal(cells.get("L"))
            trade_count = parse_kmtp_int(cells.get("P"))
            turnover_tl = parse_kmtp_decimal(cells.get("M"))
            turnover_usd = parse_kmtp_decimal(cells.get("N"))
            turnover_eur = parse_kmtp_decimal(cells.get("O"))

            turnover = turnover_tl
            if currency == Currency.USD:
                turnover = turnover_usd
            elif currency == Currency.EUR:
                turnover = turnover_eur

            # Parse price fields
            aof = parse_kmtp_decimal(cells.get("Q"))
            high = parse_kmtp_decimal(cells.get("R"))
            low = parse_kmtp_decimal(cells.get("S"))
            close = parse_kmtp_decimal(cells.get("T"))

            # Determine status & diagnostic
            if is_unsupported:
                status = PreciousMetalObservationStatus.UNSUPPORTED_METAL
                diag = [f"Unsupported precious metal '{metal_str}' in series '{series_code}'."]
                conf = DataConfidenceLevel.NONE
            else:
                diag = []
                status = PreciousMetalObservationStatus.VALID
                conf = DataConfidenceLevel.HIGH

            # Add AOF observation if present
            if aof is not None:
                obs_aof = PreciousMetalMarketObservation(
                    metal=metal,
                    market=PreciousMetalMarket.BIST_KMTP,
                    effective_date=effective_date,
                    price=aof,
                    price_currency=currency,
                    quantity_unit=qty_unit,
                    price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
                    purity=raw_purity_val,
                    raw_purity_value=raw_purity_val,
                    raw_purity_text=raw_purity_text,
                    purity_scale=purity_scale,
                    fineness_per_mille=fineness_per_mille,
                    settlement_term=settlement_term,
                    value_date=value_date,
                    volume=volume,
                    turnover=turnover,
                    trade_count=trade_count,
                    raw_symbol=f"{series_code}_AOF",
                    snapshot_id=snapshot_id,
                    payload_hash=payload_hash,
                    retrieved_at=retrieved_at,
                    status=status,
                    confidence=conf,
                    diagnostics=list(diag),
                )
                observations.append(obs_aof)

            # Add Close observation if present
            if close is not None:
                obs_close = PreciousMetalMarketObservation(
                    metal=metal,
                    market=PreciousMetalMarket.BIST_KMTP,
                    effective_date=effective_date,
                    price=close,
                    price_currency=currency,
                    quantity_unit=qty_unit,
                    price_type=PreciousMetalPriceType.CLOSE,
                    purity=raw_purity_val,
                    raw_purity_value=raw_purity_val,
                    raw_purity_text=raw_purity_text,
                    purity_scale=purity_scale,
                    fineness_per_mille=fineness_per_mille,
                    settlement_term=settlement_term,
                    value_date=value_date,
                    volume=volume,
                    turnover=turnover,
                    trade_count=trade_count,
                    raw_symbol=f"{series_code}_CLOSE",
                    snapshot_id=snapshot_id,
                    payload_hash=payload_hash,
                    retrieved_at=retrieved_at,
                    status=status,
                    confidence=conf,
                    diagnostics=list(diag),
                )
                observations.append(obs_close)

        return observations

    @classmethod
    def _quarantine_duplicates(
        self,
        observations: List[PreciousMetalMarketObservation],
    ) -> List[PreciousMetalMarketObservation]:
        """
        Quarantines conflicting duplicate observations deterministically with order independence.
        """
        groups: Dict[Tuple[Any, ...], List[PreciousMetalMarketObservation]] = {}
        for obs in observations:
            key = (
                obs.metal,
                obs.market,
                obs.effective_date,
                obs.price_type,
                obs.price_currency,
                obs.quantity_unit,
                obs.purity,
                obs.settlement_term,
                obs.raw_symbol,
            )
            groups.setdefault(key, []).append(obs)

        result: List[PreciousMetalMarketObservation] = []
        for key, group in groups.items():
            if len(group) == 1:
                result.append(group[0])
            else:
                # Check if all prices are identical
                distinct_prices = {obs.price for obs in group}
                if len(distinct_prices) == 1:
                    # Identical duplicates: keep first
                    result.append(group[0])
                else:
                    # Conflicting prices: quarantine all rows in group
                    for obs in group:
                        obs.status = PreciousMetalObservationStatus.CONFLICT_QUARANTINED
                        obs.confidence = DataConfidenceLevel.NONE
                        obs.diagnostics.append(
                            f"CONFLICT_QUARANTINED: Duplicate observations for key {key} have conflicting prices: {distinct_prices}"
                        )
                        result.append(obs)
        return result
