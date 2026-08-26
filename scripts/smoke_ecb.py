#!/usr/bin/env python3
"""
scripts/smoke_ecb.py
=====================
Manual Live Smoke Test for European Central Bank (ECB) Data Portal SDMX 2.1 API.

Rules:
    - NEVER runs in CI or automated unit tests.
    - Zero database mutations (read-only health check).
    - Queries:
        1. EUR/USD Reference Rate (EXR/D.USD.EUR.SP00.A)
        2. Deposit Facility Rate (FM/D.U2.EUR.4F.KR.DFR.LEV)

Usage:
    python scripts/smoke_ecb.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.ecb_sdmx import ECBDataPortalProvider


async def test_ecb_series(provider: ECBDataPortalProvider, symbol: str, label: str) -> None:
    print(f"\n📡 Querying ECB {label} ({symbol})...")
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
            print(f"  Quote / Role:   {response.source_metadata.get('quote_direction') or response.source_metadata.get('source_role')}")
            print(f"  ✅ {label}: SUCCESS")
        else:
            print(f"  ⚠️ {label}: UNSUCCESSFUL ({response.warnings})")
    except Exception as e:
        print(f"  ❌ {label} FAILED: {e}")


async def main() -> None:
    print("🔍 Testing European Central Bank Data Portal SDMX 2.1 connection...")
    provider = ECBDataPortalProvider()
    await test_ecb_series(provider, "EA_EURUSD_REFERENCE_RATE", "EUR/USD Daily Reference Rate")
    await test_ecb_series(provider, "EA_ECB_DEPOSIT_FACILITY_RATE", "ECB Deposit Facility Rate")


if __name__ == "__main__":
    asyncio.run(main())
