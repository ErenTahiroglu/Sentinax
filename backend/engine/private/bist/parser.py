"""
backend/engine/private/bist/parser.py
=====================================
Robust, exact parser for official Borsa İstanbul (BIST) PAY_BULTEN files.

Documented Source:
    - Borsa İstanbul Pay Piyasası Gün Sonu Kapanış Verileri (PAY_BULTEN_YYYYAAGG.csv)
    - Delimiter: ';' (semicolon)
    - Decimal Symbol: '.' (dot)
    - Header: 2 header rows (Row 1: Turkish, Row 2: English, Row 3+: Observations)

Hardening Invariants:
    - Two-row header support: English second header is never parsed as a market observation.
    - Zero float conversion: float input is strictly rejected (TypeError).
    - Missing fields remain None (missing != zero).
    - Malformed or missing close price NEVER becomes Decimal("0").
    - Raw symbol (e.g. KOZAA.E) preserved alongside normalized symbol (KOZAA).
    - ALTIN.S1 suffix (.S1) strictly preserved; .E stripped for equities.
    - Trade date comes from verified bulletin context / filename, not row columns.
    - If requested trade date and filename date disagree: fail closed.
    - Conflicting duplicate rows are deterministically quarantined with order-independence.
    - OHLC integrity validation (high >= max(open, close), low <= min(open, close), high >= low).
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
from backend.engine.private.bist.locator import BISTBulletinLocator
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
    Rejects float inputs to prevent precision loss.
    Official PAY_BULTEN uses '.' as the decimal point (e.g. '4683455.01').
    Returns None for empty, dash ('-'), 'N/A', or null values.
    """
    if val is None:
        return None
    
    if isinstance(val, float):
        raise TypeError("Float input prohibited in exact Decimal parser.")
    
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ValueError(f"Non-finite Decimal: {val}")
        return val
    
    if isinstance(val, int):
        return Decimal(val)

    val_str = str(val).strip()
    if not val_str or val_str in ("-", "--", "N/A", "n/a", "null", "NULL", "None", ""):
        return None

    # Strip currency symbol or extraneous whitespace
    val_str = val_str.replace("TL", "").replace("TRY", "").strip()

    # Locale-safe support if thousands separators or comma decimals are present
    if "." in val_str and "," in val_str:
        last_dot = val_str.rfind(".")
        last_comma = val_str.rfind(",")
        if last_comma > last_dot:
            # Turkish thousands dot, decimal comma: 1.234.567,89 -> 1234567.89
            val_str = val_str.replace(".", "").replace(",", ".")
        else:
            # US/UK thousands comma, decimal dot: 1,234,567.89 -> 1234567.89
            val_str = val_str.replace(",", "")
    elif "," in val_str:
        # Sole comma used as decimal separator
        val_str = val_str.replace(",", ".")

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
    if isinstance(val, float):
        raise TypeError("Float input prohibited in exact integer parser.")
    if isinstance(val, int):
        return val
    val_str = str(val).strip().replace(".", "").replace(",", "")
    if not val_str or val_str in ("-", "--", "N/A", "n/a"):
        return None
    try:
        return int(val_str)
    except ValueError as exc:
        raise ValueError(f"Cannot parse '{val}' as integer: {exc}") from exc


def parse_bist_date(val: Any) -> Optional[date]:
    """
    Parses date strings formatted as YYYY-MM-DD, DD/MM/YYYY, DD.MM.YYYY, or YYYYMMDD.
    """
    if val is None:
        return None
    if isinstance(val, date):
        return val
    
    val_str = str(val).strip()
    if not val_str or val_str in ("-", "N/A"):
        return None

    if "-" in val_str:
        parts = val_str.split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            elif len(parts[2]) == 4:
                return date(int(parts[2]), int(parts[1]), int(parts[0]))

    if "." in val_str:
        parts = val_str.split(".")
        if len(parts) == 3:
            if len(parts[2]) == 4:
                return date(int(parts[2]), int(parts[1]), int(parts[0]))
            elif len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))

    if "/" in val_str:
        parts = val_str.split("/")
        if len(parts) == 3:
            if len(parts[2]) == 4:
                return date(int(parts[2]), int(parts[1]), int(parts[0]))
            elif len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))

    if len(val_str) == 8 and val_str.isdigit():
        return date(int(val_str[:4]), int(val_str[4:6]), int(val_str[6:8]))

    raise ValueError(f"Unrecognized date format: '{val_str}'")


def clean_bist_symbol(raw_symbol: str) -> str:
    """
    Normalizes a BIST symbol to canonical ticker.
    - Strips whitespace.
    - If symbol ends with '.E' (BISTECH equity suffix), removes '.E' (e.g. 'KOZAA.E' -> 'KOZAA', 'THYAO.E' -> 'THYAO').
    - NEVER strips '.S1' (e.g. 'ALTIN.S1' remains 'ALTIN.S1').
    - If 'ALTIN.S1.E' -> 'ALTIN.S1'.
    """
    sym = (raw_symbol or "").strip().upper()
    if sym.endswith(".E") and not sym.endswith(".S1.E"):
        sym = sym[:-2]
    elif sym.endswith(".S1.E"):
        sym = sym[:-2]
    return sym


class BISTBulletinParser:
    """
    Parser for official Borsa İstanbul PAY_BULTEN daily bulletin files.
    """
    version: str = "1.1.0"

    @classmethod
    def parse_bulletin_bytes(
        cls,
        raw_bytes: bytes,
        filename: Optional[str] = None,
        trade_date: Optional[date] = None,
        snapshot_id: Optional[UUID] = None,
        snapshot_hash: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
        resolver: Optional[InstrumentResolverService] = None,
    ) -> List[BISTEODObservation]:
        """
        Parses raw bytes from a BIST bulletin download (handles CSV or ZIP archives).
        """
        if not raw_bytes:
            return []

        filename_date = BISTBulletinLocator.parse_filename_trade_date(filename or "")

        # Check for ZIP archive magic bytes 'PK\x03\x04'
        if raw_bytes[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = zf.namelist()
                bulletin_names = [n for n in names if n.lower().endswith((".csv", ".txt", ".prn", ".dat"))]
                if not bulletin_names:
                    bulletin_names = names
                
                if not bulletin_names:
                    return []
                
                inner_filename = bulletin_names[0]
                if not filename_date:
                    filename_date = BISTBulletinLocator.parse_filename_trade_date(inner_filename)

                inner_bytes = zf.read(inner_filename)
                text = inner_bytes.decode("utf-8-sig", errors="replace")
        else:
            text = raw_bytes.decode("utf-8-sig", errors="replace")

        return cls.parse_bulletin_text(
            raw_text=text,
            trade_date=trade_date,
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
        trade_date: Optional[date] = None,
        filename_date: Optional[date] = None,
        snapshot_id: Optional[UUID] = None,
        snapshot_hash: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
        resolver: Optional[InstrumentResolverService] = None,
    ) -> List[BISTEODObservation]:
        """
        Parses raw text of an official BIST PAY_BULTEN CSV.
        Supports 2 header rows (Turkish Row 1, English Row 2, Observations Row 3+).
        """
        if not raw_text or not raw_text.strip():
            return []

        # Validate trade_date vs filename_date consistency
        effective_trade_date: Optional[date] = trade_date or filename_date
        if trade_date is not None and filename_date is not None and trade_date != filename_date:
            raise BISTSchemaDriftError(
                f"Requested trade_date ({trade_date}) does not match verified filename date ({filename_date})."
            )

        lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
        if not lines:
            return []

        # Detect delimiter from header line
        header_line = lines[0]
        delimiters = [";", ",", "\t", "|"]
        delimiter = max(delimiters, key=lambda d: header_line.count(d))

        reader = list(csv.reader(lines, delimiter=delimiter))
        if not reader:
            return []

        # 1. Row 1 = Turkish Header
        raw_headers_row1 = [h.strip().upper() for h in reader[0]]
        canonical_headers: List[Optional[str]] = [
            BIST_HEADER_MAPPINGS.get(h) for h in raw_headers_row1
        ]

        # 2. Check if Row 2 is English Header
        start_row_idx = 1
        if len(reader) > 1:
            row2 = [c.strip().upper() for c in reader[1]]
            # If row 2 matches English header keywords or mapped headers, skip it
            mapped_row2 = [BIST_HEADER_MAPPINGS.get(c) for c in row2]
            non_none_matches = sum(1 for m in mapped_row2 if m is not None)
            if non_none_matches >= 2 or any(
                keyword in " ".join(row2) for keyword in ("MARKET SEGMENT", "INSTRUMENT CODE", "CLOSING PRICE", "TOTAL TRADE VALUE")
            ):
                start_row_idx = 2  # Skip English second header

        # 3. Check required columns
        present_canonical = {h for h in canonical_headers if h is not None}
        missing_required = [req for req in REQUIRED_BULLETIN_COLUMNS if req not in present_canonical]
        if missing_required:
            raise BISTSchemaDriftError(
                f"PAY_BULTEN missing required columns: {missing_required}. Present columns: {raw_headers_row1}"
            )

        # 4. Parse observation rows
        raw_observations: List[BISTEODObservation] = []
        symbol_observations_map: Dict[str, List[BISTEODObservation]] = {}

        for row_idx in range(start_row_idx, len(reader)):
            row = reader[row_idx]
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
                row_idx=row_idx + 1,
                effective_trade_date=effective_trade_date,
                snapshot_id=snapshot_id,
                snapshot_hash=snapshot_hash,
                retrieved_at=retrieved_at,
                resolver=resolver,
            )
            if obs is None:
                continue

            raw_observations.append(obs)
            symbol_observations_map.setdefault(obs.symbol, []).append(obs)

        # 5. Deterministic Duplicate Conflict Resolution (Order Independent)
        final_observations: List[BISTEODObservation] = []
        for symbol, obs_group in symbol_observations_map.items():
            if len(obs_group) == 1:
                final_observations.append(obs_group[0])
            else:
                # Check if all rows in the group have identical values
                first = obs_group[0]
                is_identical = all(
                    o.close == first.close
                    and o.open == first.open
                    and o.high == first.high
                    and o.low == first.low
                    and o.volume == first.volume
                    and o.turnover == first.turnover
                    for o in obs_group
                )
                if is_identical:
                    # Idempotent duplicate: keep exactly one valid instance
                    final_observations.append(first)
                else:
                    # Conflicting duplicate: quarantine ALL rows for this symbol deterministically
                    for conflicting_obs in obs_group:
                        conflicting_obs.status = BISTObservationStatus.CONFLICT_QUARANTINED
                        conflicting_obs.diagnostics.append(
                            f"Conflicting duplicate rows detected in bulletin for {symbol} on {conflicting_obs.trade_date}"
                        )
                        final_observations.append(conflicting_obs)

        return final_observations

    @classmethod
    def _parse_single_row(
        cls,
        row_dict: Dict[str, str],
        row_idx: int,
        effective_trade_date: Optional[date],
        snapshot_id: Optional[UUID],
        snapshot_hash: Optional[str],
        retrieved_at: Optional[datetime],
        resolver: Optional[InstrumentResolverService],
    ) -> Optional[BISTEODObservation]:
        raw_symbol_val = row_dict.get("symbol", "")
        raw_close_val = row_dict.get("close", "")

        if not raw_symbol_val:
            return None

        raw_provider_symbol = raw_symbol_val.strip()
        normalized_symbol = clean_bist_symbol(raw_provider_symbol)
        diagnostics: List[str] = []
        obs_status = BISTObservationStatus.VALID

        # 1. Resolve Trade Date (from row dict or verified bulletin context)
        row_date_str = row_dict.get("trade_date")
        if row_date_str:
            try:
                trade_date = parse_bist_date(row_date_str)
            except Exception as exc:
                trade_date = effective_trade_date
                diagnostics.append(f"Row {row_idx}: Could not parse trade_date '{row_date_str}': {exc}")
        else:
            trade_date = effective_trade_date

        if trade_date is None:
            raise ValueError(f"Row {row_idx}: Trade date must be supplied via bulletin context or filename.")

        # 2. Parse Close Price (CRITICAL: Never default to Decimal('0') on error)
        close_val: Optional[Decimal] = None
        if not raw_close_val:
            obs_status = BISTObservationStatus.INVALID_OBSERVATION
            diagnostics.append(f"Row {row_idx}: Missing required closing price.")
        else:
            try:
                close_val = parse_bist_decimal(raw_close_val)
                if close_val is None or close_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Invalid or negative close price '{raw_close_val}'.")
            except Exception as exc:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Error parsing close price '{raw_close_val}': {exc}")

        # 3. Parse Other Numeric Fields (open, high, low, previous_close, WAP, volume, turnover, trade_count)
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
                    diagnostics.append(f"Row {row_idx}: Negative weighted average {wap_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing weighted average: {exc}")

        if "volume" in row_dict:
            try:
                vol_val = parse_bist_decimal(row_dict["volume"])
                if vol_val is not None and vol_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative volume {vol_val}")
            except Exception as exc:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Error parsing volume: {exc}")

        if "turnover" in row_dict:
            try:
                turnover_val = parse_bist_decimal(row_dict["turnover"])
                if turnover_val is not None and turnover_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative turnover {turnover_val}")
            except Exception as exc:
                obs_status = BISTObservationStatus.INVALID_OBSERVATION
                diagnostics.append(f"Row {row_idx}: Error parsing turnover: {exc}")

        if "trade_count" in row_dict:
            try:
                trade_count_val = parse_bist_int(row_dict["trade_count"])
                if trade_count_val is not None and trade_count_val < 0:
                    obs_status = BISTObservationStatus.INVALID_OBSERVATION
                    diagnostics.append(f"Row {row_idx}: Negative trade count {trade_count_val}")
            except Exception as exc:
                diagnostics.append(f"Row {row_idx}: Error parsing trade count: {exc}")

        # 4. OHLC Integrity Checks
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

        # 5. Metadata and Identity Resolution
        market_segment = row_dict.get("market_segment")
        instrument_name = row_dict.get("instrument_name")

        inst_id: Optional[UUID] = None
        if normalized_symbol == ALTIN_S1_SYMBOL:
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
                provider_symbol=normalized_symbol,
                as_of_date=trade_date,
            )
            if inst_id is None:
                if obs_status == BISTObservationStatus.VALID:
                    obs_status = BISTObservationStatus.UNRESOLVED_IDENTITY
                diagnostics.append(f"Symbol '{normalized_symbol}' not found in Instrument Master")
        else:
            if obs_status == BISTObservationStatus.VALID:
                obs_status = BISTObservationStatus.UNRESOLVED_IDENTITY

        confidence = DataConfidenceLevel.HIGH if obs_status == BISTObservationStatus.VALID else (
            DataConfidenceLevel.MEDIUM if obs_status == BISTObservationStatus.UNRESOLVED_IDENTITY else DataConfidenceLevel.LOW
        )

        return BISTEODObservation(
            symbol=normalized_symbol,
            raw_provider_symbol=raw_provider_symbol,
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
            instrument_name=instrument_name,
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
