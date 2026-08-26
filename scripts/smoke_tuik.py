#!/usr/bin/env python3
"""
scripts/smoke_tuik.py
======================
Manual Live Smoke Test for TÜİK SDMX Web Service (Active since June 2026).

Rules:
    - NEVER runs in CI or automated unit tests.
    - Zero database mutations (read-only health check).
    - Single small request to verify TÜİK SDMX API connectivity and response parsing.

Usage:
    python scripts/smoke_tuik.py
"""

import asyncio
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.tuik_sdmx import TUIKSDMXProvider


async def main() -> None:
    print("🔍 Testing TÜİK SDMX Web Service connection...")
    provider = TUIKSDMXProvider()
    ctx = FetchContext(
        observation_type="MACRO_INFLATION",
        provider_symbol="TR_CPI_TUIK_YOY",
    )

    try:
        response = await provider.fetch(ctx)
        print("\n--- TÜİK SDMX Response ---")
        print(f"Status:          {response.status.value}")
        print(f"Effective Date:  {response.effective_date}")
        print(f"Published At:    {response.published_at}")
        print(f"Usable:          {response.is_usable}")
        
        normalized = provider.normalize(response.raw)
        print(f"Normalized Data: {normalized}")
        
        if response.is_usable:
            print("\n✅ TÜİK SDMX Live Smoke Test: SUCCESS")
        else:
            print(f"\n⚠️ TÜİK SDMX returned unusable status: {response.warnings}")
    except Exception as e:
        print(f"\n❌ TÜİK SDMX Smoke Test FAILED with error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
