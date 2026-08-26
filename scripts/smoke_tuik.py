#!/usr/bin/env python3
"""
scripts/smoke_tuik.py
======================
Manual Live Smoke Test for TÜİK SDMX Web Service.

Status:
    - Currently marked YELLOW (UNVERIFIED) pending official SDMX codelist catalog verification.
    - Halts gracefully if contract is unverified to prevent sending guessed queries.

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
    print("🔍 Testing TÜİK SDMX Web Service contract status...")
    provider = TUIKSDMXProvider(enforce_verified_contract=True)
    ctx = FetchContext(
        observation_type="MACRO_INFLATION",
        provider_symbol="TR_CPI_TUIK_YOY",
    )

    response = await provider.fetch(ctx)
    print(f"\nStatus:   {response.status.value}")
    print(f"Warnings: {response.warnings}")

    if not response.is_usable:
        print("\nℹ️ TÜİK SDMX dataflow codelists are currently UNVERIFIED.")
        print("   Guessed requests are halted safely by provider contract guards.")


if __name__ == "__main__":
    asyncio.run(main())
