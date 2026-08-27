"""
backend/engine/private/bist/manifest.py
=======================================
Official Borsa İstanbul (BIST) DataFilePaths.zip Directory Manifest Models & Parser.

Documented Source & Inspection:
    - URL: https://www.borsaistanbul.com/files/DataFilePaths.zip
    - Member: VerilerDosyaIsimleri.xlsx
    - Official Equity Bulletin Mapping:
        Turkish: "Bülten Verileri" -> Directory: "/data/thb/YYYY/AA/" -> Filename: "thbYYYYAAGGS.zip"
        English: "Bulletin Data"   -> Directory: "/data/ehb/YYYY/AA/" -> Filename: "ehbYYYYAAGGS.zip"
"""

from __future__ import annotations

import hashlib
import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from backend.engine.private.bist.constants import BIST_DATA_FILE_PATHS_URL

logger = logging.getLogger(__name__)


class BISTManifestDiscoveryError(ValueError):
    """Raised when official directory manifest cannot be retrieved or parsed."""
    pass


@dataclass
class BISTDataFilePathEntry:
    """
    Individual data file directory/path mapping entry from DataFilePaths.zip.
    """
    description: str
    directory_template: str
    filename_template: str
    session_code: str = "1"

    def to_dict(self) -> Dict[str, str]:
        return {
            "description": self.description,
            "directory_template": self.directory_template,
            "filename_template": self.filename_template,
            "session_code": self.session_code,
        }


@dataclass
class BISTDirectoryManifest:
    """
    Immutable representation of the official Borsa İstanbul DataFilePaths manifest.
    """
    source_url: str
    retrieved_at: datetime
    payload_hash: str
    entries: Dict[str, BISTDataFilePathEntry] = field(default_factory=dict)
    raw_bytes: Optional[bytes] = None
    parser_version: str = "1.2.0"
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def get_equity_bulletin_entry(self) -> Optional[BISTDataFilePathEntry]:
        """
        Extracts the official equity market bulletin entry.
        Prioritizes 'Bülten Verileri' (TR) or 'Bulletin Data' (EN).
        """
        # Exact Turkish title
        if "Bülten Verileri" in self.entries:
            return self.entries["Bülten Verileri"]
        # Exact English title
        if "Bulletin Data" in self.entries:
            return self.entries["Bulletin Data"]
        # Case-insensitive / normalized lookup
        for desc, entry in self.entries.items():
            norm = desc.strip().lower()
            if norm in ("bülten verileri", "bulten verileri", "bulletin data"):
                return entry
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at.isoformat(),
            "payload_hash": self.payload_hash,
            "entry_count": len(self.entries),
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
            "parser_version": self.parser_version,
            "diagnostics": self.diagnostics,
        }


class BISTDirectoryManifestParser:
    """
    Parser for official Borsa İstanbul DataFilePaths.zip archive.
    Extracts mappings from VerilerDosyaIsimleri.xlsx using pure-Python XML parsing.
    """
    version: str = "1.2.0"

    @classmethod
    def parse_manifest_bytes(
        cls,
        raw_bytes: bytes,
        source_url: str = BIST_DATA_FILE_PATHS_URL,
        retrieved_at: Optional[datetime] = None,
    ) -> BISTDirectoryManifest:
        if not raw_bytes:
            raise BISTManifestDiscoveryError("Empty DataFilePaths payload.")

        now_utc = retrieved_at or datetime.now(timezone.utc)
        payload_hash = hashlib.sha256(raw_bytes).hexdigest()
        diagnostics: List[str] = []

        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
        except zipfile.BadZipFile as exc:
            raise BISTManifestDiscoveryError(f"Corrupted DataFilePaths.zip: {exc}") from exc

        # Locate .xlsx member (skipping macOS __MACOSX metadata)
        xlsx_members = [
            m for m in zf.namelist()
            if m.lower().endswith(".xlsx") and not m.startswith("__MACOSX")
        ]

        if not xlsx_members:
            # Check if plain CSV exists as fallback
            csv_members = [
                m for m in zf.namelist()
                if m.lower().endswith(".csv") and not m.startswith("__MACOSX")
            ]
            if csv_members:
                entries = cls._parse_csv_member(zf.read(csv_members[0]))
                return BISTDirectoryManifest(
                    source_url=source_url,
                    retrieved_at=now_utc,
                    payload_hash=payload_hash,
                    entries=entries,
                    raw_bytes=raw_bytes,
                    parser_version=cls.version,
                    diagnostics=diagnostics,
                )
            raise BISTManifestDiscoveryError("No VerilerDosyaIsimleri.xlsx or .csv found in DataFilePaths.zip.")

        target_member = xlsx_members[0]
        try:
            xlsx_bytes = zf.read(target_member)
            xzf = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
        except Exception as exc:
            raise BISTManifestDiscoveryError(f"Error reading internal Excel file '{target_member}': {exc}") from exc

        # 1. Parse Shared Strings (xl/sharedStrings.xml)
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in xzf.namelist():
            try:
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
            except Exception as exc:
                diagnostics.append(f"Error parsing sharedStrings.xml: {exc}")

        # 2. Parse Sheets (sheet1.xml is TR, sheet2.xml is EN)
        entries: Dict[str, BISTDataFilePathEntry] = {}
        sheet_files = [f for f in xzf.namelist() if f.startswith("xl/worksheets/sheet") and f.endswith(".xml")]

        for sheet_file in sheet_files:
            try:
                ws_tree = ET.fromstring(xzf.read(sheet_file))
                for row in ws_tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                    cells: Dict[str, str] = {}
                    for c in row.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                        c_ref = c.attrib.get("r", "")
                        c_type = c.attrib.get("t")
                        v = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                        val = v.text if v is not None else ""
                        if c_type == "s" and val.isdigit() and int(val) < len(shared_strings):
                            val = shared_strings[int(val)]
                        col_letter = "".join([ch for ch in c_ref if ch.isalpha()])
                        cells[col_letter] = val.strip()

                    desc = cells.get("B", "")
                    directory = cells.get("C", "")
                    filename = cells.get("D", "")

                    if desc and directory and filename and desc not in ("Açıklama", "Name"):
                        entries[desc] = BISTDataFilePathEntry(
                            description=desc,
                            directory_template=directory,
                            filename_template=filename,
                            session_code="1",
                        )
            except Exception as exc:
                diagnostics.append(f"Error parsing sheet '{sheet_file}': {exc}")

        # Check for ambiguity in bulletin entries
        bulletin_candidates = [
            (k, v) for k, v in entries.items()
            if "bülten verileri" in k.lower() or "bulletin data" in k.lower()
        ]
        unique_templates = {
            (v.directory_template, v.filename_template) for _, v in bulletin_candidates
        }
        # In official manifest, TR is /data/thb/YYYY/AA/thbYYYYAAGGS.zip and EN is /data/ehb/YYYY/AA/ehbYYYYAAGGS.zip
        # If multiple differing Turkish entries exist, fail closed
        tr_bulletins = [v for k, v in bulletin_candidates if "bülten verileri" in k.lower()]
        if len(tr_bulletins) > 1:
            tr_templates = {(v.directory_template, v.filename_template) for v in tr_bulletins}
            if len(tr_templates) > 1:
                diagnostics.append("AMBIGUOUS_BULLETIN_MAPPING: Multiple conflicting Turkish bulletin entries detected.")

        return BISTDirectoryManifest(
            source_url=source_url,
            retrieved_at=now_utc,
            payload_hash=payload_hash,
            entries=entries,
            raw_bytes=raw_bytes,
            parser_version=cls.version,
            diagnostics=diagnostics,
        )

    @classmethod
    def _parse_csv_member(cls, raw_csv: bytes) -> Dict[str, BISTDataFilePathEntry]:
        """Fallback helper if DataFilePaths is distributed as plain CSV."""
        text = raw_csv.decode("utf-8-sig", errors="replace")
        entries: Dict[str, BISTDataFilePathEntry] = {}
        for line in text.splitlines():
            parts = [p.strip() for p in line.split(";") if p.strip()]
            if len(parts) >= 3:
                desc, directory, filename = parts[0], parts[1], parts[2]
                if desc not in ("Açıklama", "Name"):
                    entries[desc] = BISTDataFilePathEntry(
                        description=desc,
                        directory_template=directory,
                        filename_template=filename,
                        session_code="1",
                    )
        return entries


class BISTDirectoryManifestCache:
    """
    Process-local cache for BIST Directory Manifest with TTL and graceful stale fallback.
    """

    def __init__(
        self,
        ttl_seconds: float = 86400.0,      # 24 Hours
        max_stale_seconds: float = 604800.0, # 7 Days
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_stale_seconds = max_stale_seconds
        self._cached_manifest: Optional[BISTDirectoryManifest] = None
        self._cached_at: Optional[datetime] = None

    def get_manifest(self) -> Tuple[Optional[BISTDirectoryManifest], bool]:
        """
        Returns (manifest, is_stale).
        If manifest is fresh: (manifest, False).
        If manifest is past TTL but within max_stale: (manifest, True).
        If manifest is missing or older than max_stale: (None, False).
        """
        if self._cached_manifest is None or self._cached_at is None:
            return None, False

        age = (datetime.now(timezone.utc) - self._cached_at).total_seconds()
        if age <= self.ttl_seconds:
            return self._cached_manifest, False
        elif age <= self.max_stale_seconds:
            return self._cached_manifest, True
        else:
            return None, False

    def set_manifest(self, manifest: BISTDirectoryManifest) -> None:
        self._cached_manifest = manifest
        self._cached_at = datetime.now(timezone.utc)

    def clear(self) -> None:
        self._cached_manifest = None
        self._cached_at = None
