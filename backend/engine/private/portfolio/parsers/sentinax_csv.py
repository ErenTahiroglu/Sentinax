"""
backend/engine/private/portfolio/parsers/sentinax_csv.py
========================================================
Sentinax Canonical CSV v1 — Reference & Production Source Parser Adapter (Phase 13F).

This module implements the first real PortfolioImportSourceParser adapter for Sentinax.
It defines a strict, deterministic, line-oriented canonical CSV format designed for
manual import, test verification, and future broker-converter interchange.

Key Architectural Invariants:
1. Strict Line-Oriented Subset of CSV:
   - Each logical CSV record corresponds to exactly ONE physical line.
   - Multiline quoted fields containing physical newlines are strictly forbidden.
   - Line terminators: LF (\\n) or CRLF (\\r\\n). Bare CR (\\r) and mixed newline styles fail closed.
2. Exact Raw Byte Slices:
   - ExtractedImportRecord.raw_record is the exact source byte slice from original content
     excluding only the line terminator. Never re-encoded from decoded text.
3. Header & Field Grammar Binding:
   - First physical row is the header.
   - Every header key must satisfy Phase 13C grammar: ^[a-z][a-z0-9_]{0,63}$.
   - Header keys must be strictly unique (no duplicates).
   - Maximum columns: 128. Maximum data records: 250,000.
4. Preserved Textual Semantics & Whitespace:
   - CSV syntax is decoded (quotes/escapes processed), but field text is never stripped or coerced.
   - Blank cells become empty strings (""), not omitted fields.
   - All columns produce an ImportParsedField for every data row.
5. Absolute Separation from Financial Semantics:
   - Assigns no transaction types, currencies, instruments, or accounting numbers.
   - Operates strictly as a format-level textual field extractor.
"""

from __future__ import annotations

import csv
import re
from typing import List, Sequence, Set, Tuple

from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
)
from backend.engine.private.portfolio.import_pipeline import (
    ExtractedImportRecord,
)

_FIELD_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

MAX_CONTENT_BYTES: int = 52_428_800   # 50 MiB
MAX_RECORD_BYTES: int = 1_048_576     # 1 MiB per line excluding terminator
MAX_DATA_RECORDS: int = 250_000       # 250k data records maximum
MAX_COLUMNS: int = 128                # 128 columns maximum


class SentinaxCanonicalCsvError(ValueError):
    """Raised when Canonical CSV v1 content violates syntax, encoding, or structure constraints."""
    pass


def _scan_physical_lines(content: bytes) -> Tuple[List[bytes], List[str]]:
    """
    Scans raw content into exact byte slices and line terminators.
    Enforces no bare CR, no mixed newlines, and no blank lines.
    """
    lines: List[bytes] = []
    terminators: List[str] = []
    pos = 0
    n = len(content)

    while pos < n:
        next_r = content.find(b"\r", pos)
        next_n = content.find(b"\n", pos)

        if next_r == -1 and next_n == -1:
            # Final line without trailing line terminator
            line_bytes = content[pos:]
            if len(line_bytes) == 0:
                raise SentinaxCanonicalCsvError(f"Blank physical line at row {len(lines) + 1}")
            lines.append(line_bytes)
            terminators.append("NONE")
            break

        if next_r != -1 and (next_n == -1 or next_r < next_n):
            # \r encountered
            if next_r + 1 < n and content[next_r + 1] == 0x0A:  # \n follows \r -> CRLF
                line_bytes = content[pos:next_r]
                if len(line_bytes) == 0:
                    raise SentinaxCanonicalCsvError(f"Blank physical line at row {len(lines) + 1}")
                lines.append(line_bytes)
                terminators.append("CRLF")
                pos = next_r + 2
            else:
                raise SentinaxCanonicalCsvError(
                    f"Bare CR line terminator is not allowed at physical row {len(lines) + 1}"
                )
        else:
            # LF encountered
            line_bytes = content[pos:next_n]
            if len(line_bytes) == 0:
                raise SentinaxCanonicalCsvError(f"Blank physical line at row {len(lines) + 1}")
            lines.append(line_bytes)
            terminators.append("LF")
            pos = next_n + 1

    # Enforce uniform newline style throughout the file
    explicit_terminators = {t for t in terminators if t != "NONE"}
    if len(explicit_terminators) > 1:
        raise SentinaxCanonicalCsvError(
            f"Mixed newline styles (LF and CRLF) detected in CSV content: {sorted(explicit_terminators)}"
        )

    return lines, terminators


def _validate_csv_line_quotes(line_text: str, physical_row: int) -> None:
    """
    Validates strict Canonical CSV v1 quote discipline on a single decoded physical line.

    Rules:
    - Quoted field begins with '"', ends with '"', escapes internal quotes with '""',
      and must be followed immediately by a delimiter (',') or end-of-line.
    - Unquoted field cannot contain any '"' character.
    - Fails closed on unclosed quotes, characters/whitespace after closing quotes, and quotes in unquoted fields.
    """
    i = 0
    n = len(line_text)

    while i < n:
        if line_text[i] == '"':
            # Quoted field
            i += 1
            closed = False
            while i < n:
                if line_text[i] == '"':
                    if i + 1 < n and line_text[i + 1] == '"':
                        # Escaped quote ("")
                        i += 2
                    else:
                        # Closing quote
                        closed = True
                        i += 1
                        if i == n:
                            break
                        if line_text[i] == ",":
                            i += 1
                            break
                        raise SentinaxCanonicalCsvError(
                            f"Malformed CSV quoting at physical row {physical_row}: unexpected character after closing quote"
                        )
                else:
                    i += 1
            if not closed:
                raise SentinaxCanonicalCsvError(
                    f"Malformed CSV quoting at physical row {physical_row}: unclosed quoted field"
                )
        else:
            # Unquoted field
            while i < n:
                if line_text[i] == ",":
                    i += 1
                    break
                if line_text[i] == '"':
                    raise SentinaxCanonicalCsvError(
                        f"Malformed CSV quoting at physical row {physical_row}: unquoted field contains quote"
                    )
                i += 1


class SentinaxCanonicalCsvParserV1:
    """
    Authoritative Sentinax Canonical CSV v1 parser adapter.
    """

    @property
    def source_key(self) -> str:
        """Fixed canonical source key."""
        return "sentinax_csv"

    @property
    def parser_revision(self) -> int:
        """Fixed parser contract revision."""
        return 1

    def extract_records(self, content: bytes) -> Tuple[ExtractedImportRecord, ...]:
        """
        Parses raw Canonical CSV v1 bytes into extracted records with exact source byte bindings.

        Args:
            content: Exact raw file bytes.

        Returns:
            Tuple of ExtractedImportRecord objects in physical file order.

        Raises:
            SentinaxCanonicalCsvError: On malformed CSV syntax, encoding, headers, or limit violations.
        """
        if type(content) is not bytes or isinstance(content, (bytearray, memoryview)):
            raise SentinaxCanonicalCsvError(
                f"content must be an immutable bytes instance, got {type(content).__name__}"
            )
        if len(content) == 0:
            raise SentinaxCanonicalCsvError("content must not be empty")

        if len(content) > MAX_CONTENT_BYTES:
            raise SentinaxCanonicalCsvError(
                f"content exceeds maximum size limit of {MAX_CONTENT_BYTES} bytes ({len(content)} bytes)"
            )

        if content.startswith(b"\xef\xbb\xbf"):
            raise SentinaxCanonicalCsvError("UTF-8 BOM is not allowed in Canonical CSV v1")

        if b"\x00" in content:
            raise SentinaxCanonicalCsvError("NUL bytes are not allowed in Canonical CSV v1")

        lines, _ = _scan_physical_lines(content)
        if len(lines) == 0:
            raise SentinaxCanonicalCsvError("CSV content must contain at least a header row")

        # 1. Header Validation
        header_raw_bytes = lines[0]
        if len(header_raw_bytes) > MAX_RECORD_BYTES:
            raise SentinaxCanonicalCsvError(
                f"Header row exceeds maximum size limit of {MAX_RECORD_BYTES} bytes ({len(header_raw_bytes)} bytes)"
            )

        try:
            header_text = header_raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            raise SentinaxCanonicalCsvError(f"Malformed UTF-8 in header row: {e}") from e

        _validate_csv_line_quotes(header_text, physical_row=1)

        try:
            header_reader = csv.reader([header_text], delimiter=",", quotechar='"', doublequote=True, strict=True)
            header_rows = list(header_reader)
        except csv.Error as e:
            raise SentinaxCanonicalCsvError(f"Malformed CSV syntax in header row: {e}") from e

        if len(header_rows) != 1 or len(header_rows[0]) == 0:
            raise SentinaxCanonicalCsvError("Header row must contain at least one column")

        headers = header_rows[0]
        if len(headers) > MAX_COLUMNS:
            raise SentinaxCanonicalCsvError(
                f"Header exceeds maximum column limit of {MAX_COLUMNS} ({len(headers)} columns)"
            )

        seen_keys: Set[str] = set()
        for col_idx, key in enumerate(headers):
            if not _FIELD_KEY_PATTERN.fullmatch(key):
                raise SentinaxCanonicalCsvError(
                    f"Invalid header column key at index {col_idx}: {key!r} (must match '^[a-z][a-z0-9_]{{0,63}}$')"
                )
            if key in seen_keys:
                raise SentinaxCanonicalCsvError(f"Duplicate header key detected: {key!r}")
            seen_keys.add(key)

        # 2. Data Rows Validation
        data_lines = lines[1:]
        if len(data_lines) > MAX_DATA_RECORDS:
            raise SentinaxCanonicalCsvError(
                f"Data row count exceeds maximum limit of {MAX_DATA_RECORDS} ({len(data_lines)} records)"
            )

        extracted_records: List[ExtractedImportRecord] = []
        for i, row_bytes in enumerate(data_lines):
            physical_row = i + 2
            if len(row_bytes) > MAX_RECORD_BYTES:
                raise SentinaxCanonicalCsvError(
                    f"Physical row {physical_row} exceeds maximum size limit of {MAX_RECORD_BYTES} bytes"
                )

            try:
                row_text = row_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as e:
                raise SentinaxCanonicalCsvError(
                    f"Malformed UTF-8 at physical row {physical_row}: {e}"
                ) from e

            _validate_csv_line_quotes(row_text, physical_row=physical_row)

            try:
                row_reader = csv.reader([row_text], delimiter=",", quotechar='"', doublequote=True, strict=True)
                parsed_rows = list(row_reader)
            except csv.Error as e:
                raise SentinaxCanonicalCsvError(
                    f"Malformed CSV at physical row {physical_row}: {e}"
                ) from e

            if len(parsed_rows) != 1:
                raise SentinaxCanonicalCsvError(
                    f"Malformed CSV at physical row {physical_row}: expected 1 logical row, got {len(parsed_rows)}"
                )

            cells = parsed_rows[0]
            if len(cells) != len(headers):
                raise SentinaxCanonicalCsvError(
                    f"Malformed CSV at physical row {physical_row}: expected {len(headers)} columns, got {len(cells)}"
                )

            parsed_fields = [
                ImportParsedField(field_key=headers[j], field_value=cells[j])
                for j in range(len(headers))
            ]
            sorted_fields = tuple(sorted(parsed_fields, key=lambda f: f.field_key))

            record = ExtractedImportRecord(
                raw_record=row_bytes,
                fields=sorted_fields,
            )
            extracted_records.append(record)

        return tuple(extracted_records)
