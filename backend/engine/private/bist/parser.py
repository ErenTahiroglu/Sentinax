"""
backend/engine/private/bist/parser.py
=====================================
Robust, locale-safe parser for Borsa İstanbul (BIST) Equity EOD bulletins.

Invariants:
    - Locale-safe Decimal parsing (supports Turkish dot-thousands/comma-decimals and standard formats).
    - Zero float conversion.
    - Missing fields remain None (missing != zero).
    - OHLC integrity validation (high >= max(open, close), low <= min(open, close), high >= low).
    - Negative prices and volumes are strictly rejected.
    - Symbol normalization removes .E equity suffix, but NEVER strips .S1 commodity certificate suffix.
    - Schema drift fails closed if required columns (trade_date, symbol, close) are missing.
    - Unresolved symbols in Instrument Master are cleanly quarantined without fabrication.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from backend.engine.private.bist.constants import (
    ALTIN_S1_ASSET_CLASS,
    ALTIN_S1_CURRENCY,
    ALTIN_S1_INSTRUMENT_TYPE,
    ALTIN_S1_SYMBOL,
    BIST_HEADER_MAPPINGS,
    REQUIRED_BULLETIN_COLUMNS,
)
from backend.engine.private.bist.models import (
    BISTEODObservation,
    BISTObservationStatus,
)
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    InstrumentType,
    SourceTier,
)
from backend.engine.private.identity import InstrumentResolverService

logger = logging.getLogger(__name__)


class BISTSchemaDriftError(ValueError):
    """Raised when an official BIST bulletin lacks required columns or structure."""
    pass


def parse_bist_decimal(val: Any) -> Optional[Decimal]:
    """
    Parses a string or numeric value into a pure Decimal.
    Supports both Turkish formatting ('1.234,56') and standard decimal formatting ('1234.56').
    Returns None for empty, dash ('-'), 'N/A', or null values.
    """
    if val is None:
        return None
    
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ValueError(f"Non-finite Decimal: {val}")
        return val
    
    if isinstance(val, (int, float)):
        val_str = str(val)
    else:
        val_str = str(val).strip()

    if not val_str or val_str in ("-", "--", "N/A", "n/a", "null", "NULL", "None", ""):
        return None

    # Strip currency symbol or whitespace
    val_str = val_str.replace("TL", "").replace("TRY", "").strip()

    # Detect Turkish format vs Standard format
    if "." in val_str and "," in val_str:
        # Check last occurrence to determine decimal separator
        last_dot = val_str.rfind(".")
        last_comma = val_str.rfind(",")
        if last_comma > last_dot:
            # Turkish format: 1.234.567,89 -> remove dots, replace comma with dot
            val_str = val_str.replace(".", "").replace(",", ".")
        else:
            # US/UK format with commas: 1,234,567.89 -> remove commas
            val_str = val_str.replace(",", "")
    elif "," in val_str:
        # Sole comma is the decimal separator: 123,45 -> 123.45
        val_str = val_str.replace(",", ".")
    elif "." in val_str:
        # Standard decimal or thousands dot. In monetary/price contexts, assume standard decimal.
        pass

    try:
        dec = Decimal(val_str)
        if not dec.is_finite():
            raise ValueError(f"Non-finite Decimal: {dec}")
        return dec
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Cannot parse '{val}' as exact Decimal: {exc}") from exc


def parse_bist_int(val: Any) -> Optional[int]:
    """Parses an integer field such as trade count."""
    if val is None:
        return None
    val_str = str(val).strip().replace(".", "").replace(",", "")
    if not val_str or val_str in ("-", "--", "N/A", "n/a"):
        return None
    try:
        return int(val_str)
    except ValueError as exc:
        raise ValueError(f"Cannot parse '{val}' as integer: {exc}") from exc


def parse_bist_date(val: Any) -> Optional[date]:
    """
    Parses date strings formatted as DD/MM/YYYY, DD.MM.YYYY, YYYY-MM-DD, or YYYYMMDD.
    """
    if val is None:
        return None
    if isinstance(val, date):
        return val
    
    val_str = str(val).strip()
    if not val_str or val_str in ("-", "N/A"):
        return None

    # Try ISO format YYYY-MM-DD
    if "-" in val_str:
        parts = val_str.split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:  # YYYY-MM-DD
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            elif len(parts[2]) == 4:  # DD-MM-YYYY
                return date(int(parts[2]), int(parts[1]), int(parts[0]))

    # Try dot format DD.MM.YYYY
    if "." in val_str:
        parts = val_str.split(".")
        if len(parts) == 3:
            if len(parts[2]) == 4:  # DD.MM.YYYY
                return date(int(parts[2]), int(parts[1]), int(parts[0]))
            elif len(parts[0]) == 4:  # YYYY.MM.DD
                return date(int(parts[0]), int(parts[1]), int(parts[2]))

    # Try slash format DD/MM/YYYY
    if "/" in val_str:
        parts = val_str.split("/")
        if len(parts) == 3:
            if len(parts[2]) == 4:  # DD/MM/YYYY
                return date(int(parts[2]), int(parts[1]), int(parts[0]))
            elif len(parts[0]) == 4:  # YYYY/MM/DD
                return date(int(parts[0]), int(parts[1]), int(parts[2]))

    # Try 8-digit compact YYYYMMDD
    if len(val_str) == 8 and val_str.isdigit():
        return date(int(val_str[:4]), int(val_str[4:6]), int(val_str[6:8]))

    raise ValueError(f"Unrecognized date format: '{val_str}'")


def clean_bist_symbol(raw_symbol: str) -> str:
    """
    Normalizes a BIST symbol.
    - Strips whitespace.
    - If symbol ends with '.E' (BISTECH equity share suffix), removes '.E' (e.g. 'THYAO.E' -> 'THYAO').
    - NEVER strips '.S1' (e.g. 'ALTIN.S1' remains 'ALTIN.S1').
    - If 'ALTIN.S1.E' -> 'ALTIN.S1'.
    """
    sym = (raw_symbol or "").strip().upper()
    if sym.endswith(".E"):
        sym = sym[:-2]
    return sym


class BISTBulletinParser:
    """
    Parser for official Borsa İstanbul (BIST) daily bulletin files (CSV, TXT, ZIP).
    """
    version: str = "1.0.0"

    @classmethod
    def parse_bulletin_bytes(
        cls,
        raw_bytes: bytes,
        filename: Optional[str] = None,
        filename_date: Optional[date] = None,
        snapshot_id: Optional[UUID] = None,
        snapshot_hash: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
        resolver: Optional[InstrumentResolverService] = None,
    ) -> List[BISTEODObservation]:
        """
        Parses raw bytes from a BIST bulletin download (handles ZIP archives or plain text).
        """
        if not raw_bytes:
            return []

        # Check for ZIP archive magic bytes 'PK\x03\x04'
        if raw_bytes[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                # Find CSV or TXT file inside archive
                names = zf.namelist()
                bulletin_names = [n for n in names if n.lower().endswith((".csv", ".txt", ".prn", ".dat"))]
                if not bulletin_names:
                    bulletin_names = names  # fallback to first file
                
                if not bulletin_names:
                    return []
                
                inner_bytes = zf.read(bulletin_names[0])
                text = inner_bytes.decode("utf-8-sig", errors="replace")
        else:
            text = raw_bytes.decode("utf-8-sig", errors="replace")

        return cls.parse_bulletin_text(
            raw_text=text,
            filename_date=filename_date,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            retrieved_at=retrieved_at,
            resolver=resolver,
        )

    @classmethod
    def parse_bulletin_text(
        cls,
        raw_text: str,
        filename_date: Optional[date] = None,
        snapshot_id: Optional[UUID] = None,
        snapshot_hash: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
        resolver: Optional[InstrumentResolverService] = None,
    ) -> List[BISTEODObservation]:
        """
        Parses raw text of a BIST CSV/TXT bulletin.
        """
        if not raw_text or not raw_text.strip():
            return []

        lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
        if not lines:
            return []

        # Detect delimiter from header line
        header_line = lines[0]
        delimiters = [";", ",", "\t", "|"]
        delimiter = max(delimiters, key=lambda d: header_line.count(d))

        reader = csv.reader(lines, delimiter=delimiter)
        raw_headers = next(reader, None)
        if not raw_headers:
            return []

        # Normalize headers using BIST_HEADER_MAPPINGS
        canonical_headers: List[Optional[str]] = []
        for h in raw_headers:
            clean_h = h.strip().upper()
            mapped = BIST_HEADER_MAPPINGS.get(clean_h)
            canonical_headers.append(mapped)

        # Check required columns for schema drift
        present_canonical = {h for h in canonical_headers if h is not None}
        missing_required = [req for req in REQUIRED_BULLETIN_COLUMNS if req not in present_canonical]
        if missing_required:
            raise BISTSchemaDriftError(
                f"BIST bulletin missing required columns: {missing_required}. Present columns: {raw_headers}"
            )

        observations: List[BISTEODObservation] = []
        seen_symbols: Dict[Tuple[str, date], BISTEODObservation] = {}

        for row_idx, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue

            row_dict: Dict[str, str] = {}
            for col_idx, cell in enumerate(row):
                if col_idx < len(canonical_headers):
                    col_name = canonical_headers[col_idx]
                    if col_name:
                        row_dict[col_name] = cell.strip()

            obs = cls._parse_single_row(
                row_dict=row_dict,
                row_idx=row_idx,
                filename_date=filename_date,
                snapshot_id=snapshot_id,
                snapshot_hash=snapshot_hash,
                retrieved_at=retrieved_at,
                resolver=resolver,
            )
            if obs is None:
                continue

            # Duplicate resolution within same bulletin
            dedup_key = (obs.symbol, obs.trade_date)
            if dedup_key in seen_symbols:
                existing = seen_symbols[dedup_key]
                if (
                    existing.close == obs.close
                    and existing.open == obs.open
                    and existing.high == obs.high
                    and existing.low == obs.low
                    and existing.volume == obs.volume
                ):
                    # Identical duplicate -> idempotent, skip
                    continue
                else:
                    # Conflicting duplicate row for same symbol and date -> mark invalid
                    obs.status = BISTObservationStatus.INVALID_OBSERVATION
                    obs.diagnostics.append(f"Conflicting duplicate row in bulletin for {obs.symbol} on {obs.trade_date}")
            
            seen_symbols[dedup_key] = obs
            observations.append(obs)

        return observations

    @classmethod
    def _parse_single_row(
        cls,
        row_dict: Dict[str, str],
        row_idx: int,
        filename_date: Optional[date],
        snapshot_id: Optional[UUID],
        snapshot_hash: Optional[str],
        retrieved_at: Optional[datetime],
        resolver: Optional[InstrumentResolverService],
    ) -> Optional[BISTEODObservation]:
        raw_symbol = row_dict.get("symbol", "")
        raw_date = row_dict.get("trade_date", "")
        raw_close = row_dict.get("close", "")

        if not raw_symbol or not raw_close:
            return None

        symbol = clean_bist_symbol(raw_symbol)
        diagnostics: List[str] = []
        obs_status = BISTObservationStatus.VALID

        # 1. Parse Trade Date
        try:
            trade_date = parse_bist_date(raw_date)
            if trade_date is None:
                if filename_date is not None:
                    trade_date = filename_date
                else:
                    return None
        except Exception as exc:
            if filename_date is not None:
                trade_date = filename_date
                diagnostics.append(f"Row {row_idx}: Could not parse trade_date '{raw_date}', fell back to filename_date ({exc})")
            else:
                return None

        if filename_date is not None and trade_date != filename_date:
            obs_status = BISTObservationStatus.INVALID_OBSERVATION
            diagnostics.append(f"Row {row_idx}: Trade date {trade_date} does not match bulletin filename date {filename_date}")

        # 2. Parse Numeric Prices and Volumes
        try:
            close_val = parse_bist_decimal(raw_close)
            if close_val is None or close_val < 0:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Invalid close price '{raw_close}'")
                close_val = Decimal("0")
        except Exception as exc:
            obs_status = BISTObservationStatus.INVALID_OBSERVATION
            diagnostics.append(f"Row {row_idx}: Error parsing close price '{raw_close}': {exc}")
            close_val = Decimal("0")

        open_val: Optional[Decimal] = None
        high_val: Optional[Decimal] = None
        low_val: Optional[Decimal] = None
        prev_close_val: Optional[Decimal] = None
        wap_val: Optional[Decimal] = None
        vol_val: Optional[Decimal] = None
        turnover_val: Optional[Decimal] = None
        trade_count_val: Optional[int] = None

        if "open" in row_dict:
            try:
                open_val = parse_bist_decimal(row_dict["open"])
                if open_val is not None and open_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative open price {open_val}")
            except Exception as exc:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Error parsing open price: {exc}")

        if "high" in row_dict:
            try:
                high_val = parse_bist_decimal(row_dict["high"])
                if high_val is not None and high_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative high price {high_val}")
            except Exception as exc:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Error parsing high price: {exc}")

        if "low" in row_dict:
            try:
                low_val = parse_bist_decimal(row_dict["low"])
                if low_val is not None and low_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative low price {low_val}")
            except Exception as exc:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Error parsing low price: {exc}")

        if "previous_close" in row_dict:
            try:
                prev_close_val = parse_bist_decimal(row_dict["previous_close"])
                if prev_close_val is not None and prev_close_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative previous close {prev_close_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing previous close: {exc}")

        if "weighted_average" in row_dict:
            try:
                wap_val = parse_bist_decimal(row_dict["weighted_average"])
                if wap_val is not None and wap_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative weighted average price {wap_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing weighted average: {exc}")

        if "volume" in row_dict:
            try:
                vol_val = parse_bist_decimal(row_dict["volume"])
                if vol_val is not None and vol_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative volume {vol_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing volume: {exc}")

        if "turnover" in row_dict:
            try:
                turnover_val = parse_bist_decimal(row_dict["turnover"])
                if turnover_val is not None and turnover_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative turnover {turnover_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing turnover: {exc}")

        if "trade_count" in row_dict:
            try:
                trade_count_val = parse_bist_int(row_dict["trade_count"])
                if trade_count_val is not None and trade_count_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative trade count {trade_count_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing trade count: {exc}")

        # 3. OHLC Integrity Checks
        if high_val is not None and low_val is not None:
            if high_val < low_val:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: High ({high_val}) < Low ({low_val})")

        if high_val is not None:
            if open_val is not None and high_val < open_val:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: High ({high_val}) < Open ({open_val})")
            if close_val is not None and high_val < close_val:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: High ({high_val}) < Close ({close_val})")

        if low_val is not None:
            if open_val is not None and low_val > open_val:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Low ({low_val}) > Open ({open_val})")
            if close_val is not None and low_val > close_val:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Low ({low_val}) > Close ({close_val})")

        # 4. Market Segment
        market_segment = row_dict.get("market_segment")

        # 5. Identity Resolution & Classification
        inst_id: Optional[UUID] = None
        if symbol == ALTIN_S1_SYMBOL:
            asset_class = ALTIN_S1_ASSET_CLASS
            inst_type = ALTIN_S1_INSTRUMENT_TYPE
            currency = ALTIN_S1_CURRENCY
        else:
            asset_class = AssetClass.EQUITY
            inst_type = InstrumentType.BIST_STOCK
            currency = Currency.TRY

        if resolver:
            inst_id = resolver.resolve_provider_symbol_to_instrument_id(
                provider="BIST",
                provider_symbol=symbol,
                as_of_date=trade_date,
            )
            if inst_id is None:
                if obs_status == BISTObservationStatus.VALID:
                    obs_status = BISTObservationStatus.UNRESOLVED_IDENTITY
                diagnostics.append(f"Symbol '{symbol}' not found in Instrument Master")
        else:
            if obs_status == BISTObservationStatus.VALID:
                obs_status = BISTObservationStatus.UNRESOLVED_IDENTITY

        confidence = DataConfidenceLevel.HIGH if obs_status == BISTObservationStatus.VALID else (
            DataConfidenceLevel.MEDIUM if obs_status == BISTObservationStatus.UNRESOLVED_IDENTITY else DataConfidenceLevel.LOW
        )

        return BISTEODObservation(
            symbol=symbol,
            trade_date=trade_date,
            open=open_val,
            high=high_val,
            low=low_val,
            close=close_val,
            previous_close=prev_close_val,
            weighted_average=wap_val,
            volume=vol_val,
            turnover=turnover_val,
            trade_count=trade_count_val,
            currency=currency,
            market_segment=market_segment,
            instrument_id=inst_id,
            asset_class=asset_class,
            instrument_type=inst_type,
            status=obs_status,
            source_provider="BIST_EOD",
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            retrieved_at=retrieved_at or datetime.now(timezone.utc),
            confidence_level=confidence,
            source_tier=SourceTier.TIER_2_EXCHANGE,
            diagnostics=diagnostics,
        )
