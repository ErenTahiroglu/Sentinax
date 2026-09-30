#!/usr/bin/env python3
"""
scripts/smoke_evds.py
======================
Manual Live Smoke Test for the TCMB EVDS3 data service (https://evds3.tcmb.gov.tr/igmevdsms-dis/).

Rules:
    - NEVER runs in CI or automated unit tests.
    - Reads TCMB_EVDS_API_KEY from environment.
    - NEVER logs or prints raw API key.
    - Zero database mutations (read-only health check).
    - NEVER prints any part of the API key (no prefix, suffix, length or mask).
    - Tests verified series: USD/TRY, EUR/TRY, and TCMB AOFM (TP.APIFON4), plus raw transport probes for the
      monthly EVDS3 codes TP.ENFBEK.PKA12ENF and TP.BISPOLFAIZ.TUR over explicit historical windows. The probes are
      transport checks only; they do not verify or activate any registry series.

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


async def test_series(
    provider: TCMBEVDSProvider, symbol: str, label: str, window: tuple[str, str] | None = None,
) -> None:
    print(f"\n📡 Querying {label} ({symbol})...")
    ctx = FetchContext(
        observation_type="MACRO",
        provider_symbol=symbol,
        request_parameters={"startDate": window[0], "endDate": window[1]} if window else {},
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

    print("🔍 Testing TCMB EVDS3 API connection. EVDS credential configured.")

    provider = TCMBEVDSProvider(api_key=api_key)

    # 1. USD/TRY
    await test_series(provider, "TP.DK.USD.A.YTL", "USD/TRY Buying Rate", ("01-09-2026", "01-09-2026"))
    # 2. EUR/TRY
    await test_series(provider, "TP.DK.EUR.A.YTL", "EUR/TRY Buying Rate", ("01-09-2026", "01-09-2026"))
    # 3. TCMB AOFM (TP.APIFON4)
    await test_series(provider, "TP.APIFON4", "TCMB AOFM (Weighted Funding Cost)", ("01-09-2026", "30-09-2026"))
    # 4. Raw monthly transport probes (not registry-verified contracts)
    await test_series(provider, "TP.ENFBEK.PKA12ENF", "Raw monthly probe: PKA 12M expectation",
                      ("01-06-2026", "30-09-2026"))
    await test_series(provider, "TP.BISPOLFAIZ.TUR", "Raw monthly probe: BIS-distributed TUR policy rate",
                      ("01-05-2026", "31-07-2026"))


if __name__ == "__main__":
    asyncio.run(main())
