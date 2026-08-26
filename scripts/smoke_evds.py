#!/usr/bin/env python3
"""
scripts/smoke_evds.py
======================
Manual Live Smoke Test for TCMB EVDS API.

Rules:
    - NEVER runs in CI or automated unit tests.
    - Reads TCMB_EVDS_API_KEY from environment.
    - NEVER logs or prints raw API key.
    - Zero database mutations (read-only health check).
    - Tests verified series: USD/TRY, EUR/TRY, and TCMB AOFM (TP.APIFON4).

Usage:
    export TCMB_EVDS_API_KEY="your_actual_key"
    python scripts/smoke_evds.py
"""

import asyncio
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.tcmb_evds import TCMBEVDSProvider


async def test_series(provider: TCMBEVDSProvider, symbol: str, label: str) -> None:
    print(f"\n📡 Querying {label} ({symbol})...")
    ctx = FetchContext(
        observation_type="MACRO",
        provider_symbol=symbol,
    )
    try:
        response = await provider.fetch(ctx)
        print(f"  Status:         {response.status.value}")
        print(f"  Effective Date: {response.effective_date}")
        print(f"  Usable:         {response.is_usable}")
        if response.is_usable and response.raw:
            normalized = provider.normalize(response.raw)
            print(f"  Normalized:     {normalized.get('value')}")
            print(f"  ✅ {label}: SUCCESS")
        else:
            print(f"  ⚠️ {label}: UNSUCCESSFUL ({response.warnings})")
    except Exception as e:
        print(f"  ❌ {label} FAILED: {e}")


async def main() -> None:
    api_key = os.getenv("TCMB_EVDS_API_KEY")
    if not api_key:
        print("❌ TCMB_EVDS_API_KEY environment variable is missing.")
        print("   Set it with: export TCMB_EVDS_API_KEY=\"your_key\"")
        sys.exit(1)

    masked_key = api_key[:3] + "..." + api_key[-3:] if len(api_key) > 6 else "***"
    print(f"🔍 Testing TCMB EVDS API connection using key: {masked_key}")

    provider = TCMBEVDSProvider(api_key=api_key)

    # 1. USD/TRY
    await test_series(provider, "TP.DK.USD.A.YTL", "USD/TRY Buying Rate")
    # 2. EUR/TRY
    await test_series(provider, "TP.DK.EUR.A.YTL", "EUR/TRY Buying Rate")
    # 3. TCMB AOFM (TP.APIFON4)
    await test_series(provider, "TP.APIFON4", "TCMB AOFM (Weighted Funding Cost)")


if __name__ == "__main__":
    asyncio.run(main())
