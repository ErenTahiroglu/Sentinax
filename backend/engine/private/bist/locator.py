"""
backend/engine/private/bist/locator.py
======================================
Verified Borsa İstanbul (BIST) bulletin resource locator & discovery adapter.

Separates discovery of official download resources from content parsing.
Derives download paths strictly from verified BISTDirectoryManifest entries.
Enforces SSRF domain whitelisting, HTTPS-only, and anti-traversal security rules.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import Optional

from backend.engine.private.bist.constants import (
    BIST_DATA_FILE_PATHS_URL,
    BIST_OFFICIAL_HOSTS,
    BIST_OFFICIAL_PORTAL_URL,
)
from backend.engine.private.bist.manifest import (
    BISTDirectoryManifest,
    BISTManifestDiscoveryError,
)


class BISTResourceResolutionError(ValueError):
    """Raised when an official bulletin resource cannot be securely or unambiguously resolved."""
    pass


@dataclass
class BISTResolvedResource:
    """
    Metadata representation of a verified official BIST data resource.
    """
    landing_page_url: str
    resolved_download_url: str
    official_filename: str
    requested_trade_date: date
    filename_trade_date: Optional[date] = None
    manifest_hash: Optional[str] = None
    is_stale_discovery: bool = False


class BISTBulletinLocator:
    """
    Verified locator adapter for Borsa İstanbul official daily bulletin files.
    Requires a verified BISTDirectoryManifest. Never constructs unverified guessed URLs.
    """

    def __init__(
        self,
        base_host: str = "https://www.borsaistanbul.com",
        landing_page_url: Optional[str] = None,
    ) -> None:
        self.base_host = base_host.rstrip("/")
        self.landing_page_url = landing_page_url or BIST_OFFICIAL_PORTAL_URL

    @staticmethod
    def parse_filename_trade_date(filename: str) -> Optional[date]:
        """
        Parses trade date from official filename patterns:
            - thbYYYYMMDDS.zip / thbYYYYMMDDS.csv (e.g. thb202410011.zip)
            - ehbYYYYMMDDS.zip / ehbYYYYMMDDS.csv
            - PAY_BULTEN_YYYYMMDD.csv / PAY_BULTEN_YYYYMMDD.zip
            - gunluk_bulten_YYYYMMDD.csv
        """
        if not filename:
            return None

        # 1. Match thb / ehb patterns: thb202410011.zip
        thb_match = re.search(r"[te]hb(\d{4})(\d{2})(\d{2})", filename, re.IGNORECASE)
        if thb_match:
            try:
                return date(int(thb_match.group(1)), int(thb_match.group(2)), int(thb_match.group(3)))
            except ValueError:
                pass

        # 2. Match general 8-digit date YYYYMMDD in filename
        match = re.search(r"(\d{4})(\d{2})(\d{2})", filename)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            try:
                return date(year, month, day)
            except ValueError:
                return None
        return None

    def resolve_bulletin_resource(
        self,
        trade_date: date,
        manifest: Optional[BISTDirectoryManifest] = None,
        is_stale_discovery: bool = False,
    ) -> BISTResolvedResource:
        """
        Resolves the official download resource for a given trade date from verified manifest evidence.
        Fails closed if no verified manifest is provided.
        """
        if manifest is None:
            raise BISTResourceResolutionError(
                "DISCOVERY_UNAVAILABLE: No verified directory manifest provided. Cannot guess download URL."
            )

        # Check for ambiguity in manifest
        if any("AMBIGUOUS" in d for d in manifest.diagnostics):
            raise BISTResourceResolutionError(
                "PAY_BULTEN_PATH_AMBIGUOUS: Conflicting bulletin entries in manifest."
            )

        entry = manifest.get_equity_bulletin_entry()
        if entry is None:
            raise BISTResourceResolutionError(
                "PAY_BULTEN_PATH_NOT_FOUND: No official bulletin entry found in directory manifest."
            )

        # 1. Format directory template (e.g. /data/thb/YYYY/AA/ -> /data/thb/2024/10/)
        dir_template = entry.directory_template
        dir_path = dir_template.replace("YYYY", trade_date.strftime("%Y"))
        dir_path = dir_path.replace("AA", trade_date.strftime("%m"))
        dir_path = dir_path.replace("GG", trade_date.strftime("%d"))
        dir_path = "/" + dir_path.strip("/") + "/" if dir_path.strip("/") else "/"

        # 2. Format filename template (e.g. thbYYYYAAGGS.zip -> thb202410011.zip)
        fn_template = entry.filename_template
        filename = fn_template.replace("YYYY", trade_date.strftime("%Y"))
        filename = filename.replace("AA", trade_date.strftime("%m"))
        filename = filename.replace("GG", trade_date.strftime("%d"))
        filename = filename.replace("S", entry.session_code)

        # 3. Construct and validate resolved URL
        # Handle if directory_template itself was an absolute URL
        if dir_template.startswith("http://") or dir_template.startswith("https://"):
            resolved_url = f"{dir_template.rstrip('/')}/{filename}"
        else:
            resolved_url = f"{self.base_host}{dir_path}{filename}"

        # 4. Enforce Security Invariants (SSRF, HTTPS, Path Traversal)
        parsed = urllib.parse.urlparse(resolved_url)
        if parsed.scheme.lower() != "https":
            raise BISTResourceResolutionError(
                f"UNSAFE_RESOLVED_URL: Insecure scheme '{parsed.scheme}'. HTTPS is strictly required."
            )

        netloc = parsed.netloc.lower()
        if netloc not in BIST_OFFICIAL_HOSTS:
            raise BISTResourceResolutionError(
                f"UNSAFE_RESOLVED_URL: Host '{netloc}' is not in official Borsa İstanbul domain whitelist: {BIST_OFFICIAL_HOSTS}"
            )

        if ".." in parsed.path:
            raise BISTResourceResolutionError(
                f"UNSAFE_RESOLVED_URL: Path traversal '..' detected in resolved URL: {resolved_url}"
            )

        filename_trade_date = self.parse_filename_trade_date(filename)

        return BISTResolvedResource(
            landing_page_url=self.landing_page_url,
            resolved_download_url=resolved_url,
            official_filename=filename,
            requested_trade_date=trade_date,
            filename_trade_date=filename_trade_date,
            manifest_hash=manifest.payload_hash,
            is_stale_discovery=is_stale_discovery,
        )
