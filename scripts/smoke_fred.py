#!/usr/bin/env python3
"""
scripts/smoke_fred.py
======================
Manual Live Smoke Test for St. Louis Fed FRED & ALFRED API (v1).

Rules:
    - NEVER runs in CI or automated unit tests.
    - Reads FRED_API_KEY from environment.
    - NEVER logs or prints raw API key.
    - Zero database mutations (read-only health check).
    - Tests:
        1. Current CPIAUCSL (Headline CPI)
        2. Current UNRATE (Unemployment Rate)
        3. ALFRED Vintage GDPC1 (Real GDP)

Usage:
    export FRED_API_KEY="your_actual_key"
    python scripts/smoke_fred.py
"""

import asyncio
from datetime import datetime, timezone
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.fred_alfred import FREDALFREDProvider


async def test_current_series(provider: FREDALFREDProvider, symbol: str, label: str) -> None:
    print(f"\n📡 Querying Current {label} ({symbol})...")
    ctx = FetchContext(
        observation_type="MACRO_US",
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
            print(f"  Realtime Start: {normalized.get('realtime_start')}")
            print(f"  ✅ {label}: SUCCESS")
        else:
            print(f"  ⚠️ {label}: UNSUCCESSFUL ({response.warnings})")
    except Exception as e:
        print(f"  ❌ {label} FAILED: {e}")


async def test_vintage_series(provider: FREDALFREDProvider, symbol: str, as_of: datetime, label: str) -> None:
    print(f"\n📡 Querying ALFRED Vintage {label} ({symbol}) as of {as_of.date()}...")
    ctx = FetchContext(
        observation_type="MACRO_US",
        provider_symbol=symbol,
        as_of_time=as_of,
        as_of_mode="SOURCE_AS_OF",
    )
    try:
        response = await provider.fetch(ctx)
        print(f"  Status:         {response.status.value}")
        print(f"  Effective Date: {response.effective_date}")
        print(f"  Usable:         {response.is_usable}")
        if response.is_usable and response.raw:
            normalized = provider.normalize(response.raw)
            print(f"  Vintage Value:  {normalized.get('value')}")
            print(f"  Realtime Start: {normalized.get('realtime_start')}")
            print(f"  ✅ ALFRED Vintage {label}: SUCCESS")
        else:
            print(f"  ⚠️ ALFRED Vintage {label}: UNSUCCESSFUL ({response.warnings})")
    except Exception as e:
        print(f"  ❌ ALFRED Vintage {label} FAILED: {e}")


async def main() -> None:
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        print("❌ FRED_API_KEY environment variable is missing.")
        print("   Set it with: export FRED_API_KEY=\"your_key\"")
        sys.exit(1)

    masked_key = api_key[:3] + "..." + api_key[-3:] if len(api_key) > 6 else "***"
    print(f"🔍 Testing FRED/ALFRED API v1 connection using key: {masked_key}")

    provider = FREDALFREDProvider(api_key=api_key)

    # 1. US Headline CPI
    await test_current_series(provider, "US_CPI_HEADLINE_INDEX", "US Headline CPI")
    # 2. US Unemployment Rate
    await test_current_series(provider, "US_UNEMPLOYMENT_RATE", "US Unemployment Rate")
    # 3. ALFRED Vintage Real GDP (as of 2023-05-01)
    await test_vintage_series(
        provider,
        "US_REAL_GDP",
        datetime(2023, 5, 1, tzinfo=timezone.utc),
        "US Real GDP (Historical Vintage)",
    )


if __name__ == "__main__":
    asyncio.run(main())
