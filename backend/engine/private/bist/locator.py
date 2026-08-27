"""
backend/engine/private/bist/locator.py
======================================
Official Borsa İstanbul (BIST) bulletin resource locator & discovery adapter.

Separates discovery of official download resources from content parsing.
Preserves discovery metadata: landing_page_url, resolved_download_url,
official_filename, requested_trade_date, and filename_trade_date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from backend.engine.private.bist.constants import (
    BIST_BULLETIN_DIRECT_BASE_URL,
    BIST_OFFICIAL_PORTAL_URL,
    BIST_PAY_BULTEN_PREFIX,
)


@dataclass
class BISTResolvedResource:
    """
    Metadata representation of a resolved official BIST data resource.
    """
    landing_page_url: str
    resolved_download_url: str
    official_filename: str
    requested_trade_date: date
    filename_trade_date: Optional[date] = None


class BISTBulletinLocator:
    """
    Locator adapter for Borsa İstanbul official daily bulletin files.
    """

    def __init__(
        self,
        base_download_url: Optional[str] = None,
        landing_page_url: Optional[str] = None,
    ) -> None:
        self.base_download_url = (base_download_url or BIST_BULLETIN_DIRECT_BASE_URL).rstrip("/")
        self.landing_page_url = landing_page_url or BIST_OFFICIAL_PORTAL_URL

    @staticmethod
    def parse_filename_trade_date(filename: str) -> Optional[date]:
        """
        Parses trade date from official filename patterns:
            - PAY_BULTEN_YYYYMMDD.csv
            - PAY_BULTEN_YYYYMMDD.zip
            - gunluk_bulten_YYYYMMDD.csv
            - bulten_YYYYMMDD.zip
        """
        if not filename:
            return None
        
        # Match 8-digit date YYYYMMDD in filename
        match = re.search(r"(\d{4})(\d{2})(\d{2})", filename)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            try:
                return date(year, month, day)
            except ValueError:
                return None
        return None

    def resolve_bulletin_resource(self, trade_date: date) -> BISTResolvedResource:
        """
        Resolves the official download resource for a given trade date.
        """
        date_str = trade_date.strftime("%Y%m%d")
        official_filename = f"{BIST_PAY_BULTEN_PREFIX}{date_str}.csv"
        resolved_download_url = f"{self.base_download_url}/{official_filename}"

        return BISTResolvedResource(
            landing_page_url=self.landing_page_url,
            resolved_download_url=resolved_download_url,
            official_filename=official_filename,
            requested_trade_date=trade_date,
            filename_trade_date=trade_date,
        )
