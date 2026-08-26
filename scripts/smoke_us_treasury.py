#!/usr/bin/env python3
"""
scripts/smoke_us_treasury.py
=============================
Manual Live Smoke Test for U.S. Department of the Treasury Yield Curve Feed.

Rules:
    - NEVER runs in CI or automated unit tests.
    - Zero database mutations (read-only health check).
    - Queries:
        1. Current month 10-Year Par Yield Rate (US_TREASURY_PAR_10Y)
        2. Current month 2-Year Par Yield Rate (US_TREASURY_PAR_2Y)

Usage:
    python scripts/smoke_us_treasury.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.us_treasury import USTreasuryYieldCurveProvider


async def test_treasury_series(provider: USTreasuryYieldCurveProvider, symbol: str, label: str) -> None:
    print(f"\n📡 Querying U.S. Treasury {label} ({symbol})...")
    ctx = FetchContext(
        observation_type="MACRO_US_TREASURY",
        provider_symbol=symbol,
    )
    try:
        response = await provider.fetch(ctx)
        print(f"  Status:         {response.status.value}")
        print(f"  Effective Date: {response.effective_date}")
        print(f"  Usable:         {response.is_usable}")
        if response.is_usable and response.raw:
            normalized = provider.normalize(response.raw)
            print(f"  Target Value:   {normalized.get('maturities', {}).get(response.source_metadata.get('target_field'))}")
            print(f"  Full Curve Map: {normalized.get('maturities')}")
            print(f"  ✅ {label}: SUCCESS")
        else:
            print(f"  ⚠️ {label}: UNSUCCESSFUL ({response.warnings})")
    except Exception as e:
        print(f"  ❌ {label} FAILED: {e}")


async def main() -> None:
    print("🔍 Testing U.S. Department of the Treasury XML Interest Rate Data Feed connection...")
    provider = USTreasuryYieldCurveProvider()
    await test_treasury_series(provider, "US_TREASURY_PAR_10Y", "10-Year Daily Par Yield Rate")
    await test_treasury_series(provider, "US_TREASURY_PAR_2Y", "2-Year Daily Par Yield Rate")


if __name__ == "__main__":
    asyncio.run(main())
