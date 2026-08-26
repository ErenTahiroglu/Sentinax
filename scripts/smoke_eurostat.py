#!/usr/bin/env python3
"""
scripts/smoke_eurostat.py
==========================
Manual Live Smoke Test for Eurostat SDMX 2.1 Dissemination API.

Rules:
    - NEVER runs in CI or automated unit tests.
    - Zero database mutations (read-only health check).
    - Queries:
        1. Euro Area HICP YoY (prc_hicp_manr/M.RCH_A.CP00.EA20)
        2. Euro Area Unemployment Rate (une_rt_m/M.SA.TOTAL.PC_ACT.T.EA20)

Usage:
    python scripts/smoke_eurostat.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.eurostat_sdmx import EurostatSDMXProvider


async def test_eurostat_series(provider: EurostatSDMXProvider, symbol: str, label: str) -> None:
    print(f"\n📡 Querying Eurostat {label} ({symbol})...")
    ctx = FetchContext(
        observation_type="MACRO_EA",
        provider_symbol=symbol,
    )
    try:
        response = await provider.fetch(ctx)
        print(f"  Status:         {response.status.value}")
        print(f"  Effective Date: {response.effective_date}")
        print(f"  Usable:         {response.is_usable}")
        if response.is_usable and response.raw:
            normalized = provider.normalize(response.raw)
            print(f"  Normalized Val: {normalized.get('value')}")
            print(f"  Geo / Dataset:  {response.source_metadata.get('geo')} / {response.source_metadata.get('dataset_code')}")
            print(f"  ✅ {label}: SUCCESS")
        else:
            print(f"  ⚠️ {label}: UNSUCCESSFUL ({response.warnings})")
    except Exception as e:
        print(f"  ❌ {label} FAILED: {e}")


async def main() -> None:
    print("🔍 Testing Eurostat SDMX 2.1 Dissemination API connection...")
    provider = EurostatSDMXProvider()
    await test_eurostat_series(provider, "EA_HICP_ALL_ITEMS_YOY", "Euro Area HICP Annual Inflation (%)")
    await test_eurostat_series(provider, "EA_UNEMPLOYMENT_RATE", "Euro Area Civilian Unemployment Rate (%)")


if __name__ == "__main__":
    asyncio.run(main())
