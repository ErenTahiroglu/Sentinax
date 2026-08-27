"""
backend/engine/private/precious_metals/locator.py
=================================================
Verified resource discovery and locator for Borsa İstanbul KMTP Daily Bulletins.

Strict Invariants:
    - Official discovery authority: derives URL strictly from verified BISTDirectoryManifest.
    - Zero guessing: fails closed if manifest is missing or ambiguous.
    - Security & SSRF Defense: HTTPS only, official host whitelist, anti-traversal validation.
"""

from __future__ import annotations

import re
from datetime import date
from typing import List, Optional
from urllib.parse import urlparse

from backend.engine.private.bist.locator import (
    BISTResolvedResource,
    BISTResourceResolutionError,
)
from backend.engine.private.bist.manifest import (
    BISTDataFilePathEntry,
    BISTDirectoryManifest,
)
from backend.engine.private.precious_metals.constants import (
    BIST_KMTP_DATA_URL,
    BIST_KMTP_MANIFEST_KEY_EN,
    BIST_KMTP_MANIFEST_KEY_TR,
    BIST_OFFICIAL_HOSTS,
)


class BISTPreciousMetalsBulletinLocator:
    """
    Locates official Borsa İstanbul Precious Metals Market (KMTP) daily bulletin resources.
    """
    def __init__(
        self,
        base_host: str = "https://www.borsaistanbul.com",
        landing_page_url: str = BIST_KMTP_DATA_URL,
    ) -> None:
        self.base_host = base_host.rstrip("/")
        self.landing_page_url = landing_page_url

    @staticmethod
    def parse_filename_trade_date(filename: str) -> Optional[date]:
        """
        Parses date from official KMTP filename pattern:
        - KMPYYYYMMDD.zip / KMPYYYYMMDD.xlsx (Turkish)
        - PMDYYYYMMDD.zip / PMDYYYYMMDD.xlsx (English)
        """
        if not filename:
            return None
        clean = filename.split("/")[-1].split("\\")[-1]
        m = re.search(r"(?:KMP|PMD)(\d{4})(\d{2})(\d{2})", clean, re.IGNORECASE)
        if m:
            try:
                yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return date(yyyy, mm, dd)
            except ValueError:
                return None
        return None

    def resolve_bulletin_resource(
        self,
        trade_date: date,
        manifest: Optional[BISTDirectoryManifest],
        is_stale_discovery: bool = False,
    ) -> BISTResolvedResource:
        """
        Resolves the verified download URL for a daily KMTP bulletin using official manifest.
        """
        if manifest is None:
            raise BISTResourceResolutionError(
                "DISCOVERY_UNAVAILABLE: Cannot locate KMTP bulletin without verified BISTDirectoryManifest. "
                "Hardcoded URL construction is strictly forbidden."
            )

        # Find matching manifest entries for KMTP daily bulletin
        matches: List[BISTDataFilePathEntry] = []
        for desc, entry in manifest.entries.items():
            if desc == BIST_KMTP_MANIFEST_KEY_TR or desc == BIST_KMTP_MANIFEST_KEY_EN:
                matches.append(entry)
            elif "kıymetli maden" in desc.lower() and "günlük bülten" in desc.lower():
                matches.append(entry)

        if not matches:
            raise BISTResourceResolutionError(
                f"KMTP_BULLETIN_PATH_NOT_FOUND: Manifest does not contain entry for '{BIST_KMTP_MANIFEST_KEY_TR}'."
            )

        if len(matches) > 1:
            # Check if all matches have identical directory and filename templates
            templates = {(m.directory_template, m.filename_template) for m in matches}
            if len(templates) > 1:
                raise BISTResourceResolutionError(
                    f"KMTP_BULLETIN_PATH_AMBIGUOUS: Multiple conflicting KMTP bulletin entries found in manifest: {templates}"
                )

        selected_entry = matches[0]
        dir_template = selected_entry.directory_template
        file_template = selected_entry.filename_template

        # Format date tokens
        yyyy = f"{trade_date.year:04d}"
        aa = f"{trade_date.month:02d}"
        gg = f"{trade_date.day:02d}"

        # Replace filename tokens
        filename = file_template.replace("YYYY", yyyy).replace("AA", aa).replace("MM", aa).replace("GG", gg).replace("DD", gg)

        # Build full candidate URL
        if dir_template.startswith("http://") or dir_template.startswith("https://"):
            resolved_url = f"{dir_template.rstrip('/')}/{filename}"
        else:
            sub_dir = dir_template.replace("YYYY", yyyy).replace("AA", aa).replace("MM", aa)
            if not sub_dir.startswith("/"):
                sub_dir = "/" + sub_dir
            if not sub_dir.endswith("/"):
                sub_dir = sub_dir + "/"
            resolved_url = f"{self.base_host}{sub_dir}{filename}"

        # Security & SSRF Validation
        parsed = urlparse(resolved_url)
        if parsed.scheme.lower() != "https":
            raise BISTResourceResolutionError(f"UNSAFE_RESOLVED_URL: Scheme must be HTTPS, got: {resolved_url}")

        if parsed.hostname not in BIST_OFFICIAL_HOSTS:
            raise BISTResourceResolutionError(
                f"UNSAFE_RESOLVED_URL: Host '{parsed.hostname}' is not in verified official BIST hosts: {BIST_OFFICIAL_HOSTS}"
            )

        if ".." in parsed.path:
            raise BISTResourceResolutionError(f"UNSAFE_RESOLVED_URL: Path traversal '..' detected in: {resolved_url}")

        filename_trade_date = self.parse_filename_trade_date(filename)

        return BISTResolvedResource(
            official_filename=filename,
            resolved_download_url=resolved_url,
            landing_page_url=self.landing_page_url,
            requested_trade_date=trade_date,
            filename_trade_date=filename_trade_date,
            manifest_hash=manifest.payload_hash,
            is_stale_discovery=is_stale_discovery,
        )
