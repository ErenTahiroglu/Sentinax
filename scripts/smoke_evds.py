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
    - Single small request to verify API connectivity and response parsing.

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


async def main() -> None:
    api_key = os.getenv("TCMB_EVDS_API_KEY")
    if not api_key:
        print("❌ TCMB_EVDS_API_KEY environment variable is missing.")
        print("   Set it with: export TCMB_EVDS_API_KEY=\"your_key\"")
        sys.exit(1)

    masked_key = api_key[:3] + "..." + api_key[-3:] if len(api_key) > 6 else "***"
    print(f"🔍 Testing TCMB EVDS API connection using key: {masked_key}")

    provider = TCMBEVDSProvider(api_key=api_key)
    ctx = FetchContext(
        observation_type="MACRO_FX",
        provider_symbol="TP.DK.USD.A.YTL",
    )

    try:
        response = await provider.fetch(ctx)
        print("\n--- TCMB EVDS Response ---")
        print(f"Status:          {response.status.value}")
        print(f"Effective Date:  {response.effective_date}")
        print(f"Retrieved At:    {response.retrieved_at.isoformat()}")
        print(f"Usable:          {response.is_usable}")
        
        normalized = provider.normalize(response.raw)
        print(f"Normalized Data: {normalized}")
        
        if response.is_usable and normalized.get("value") is not None:
            print("\n✅ TCMB EVDS Live Smoke Test: SUCCESS")
        else:
            print(f"\n⚠️ TCMB EVDS returned unusable status: {response.warnings}")
    except Exception as e:
        print(f"\n❌ TCMB EVDS Smoke Test FAILED with error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
