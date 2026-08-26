#!/usr/bin/env python3
"""
scripts/smoke_sec_edgar.py
============================
Live smoke test for SEC EDGAR Submissions & CompanyFacts APIs.

Rules:
    - Reads SEC_USER_AGENT from environment.
    - Masks personal contact info in console output.
    - Executes 1 submissions request and 1 companyfacts request for Apple CIK (0000320193).
    - No DB writes.
    - Prints schema health, counts, and sample fact tags without dumping full payload.
"""

import asyncio
import os
import re
import sys

from backend.engine.private.sec import (
    SECEdgarClient,
    SECSubmissionsProvider,
    SECCompanyFactsProvider,
    normalize_cik,
)


def mask_user_agent(ua: str) -> str:
    # Mask email inside user agent string (e.g. Sentinax <ad***@domain.com>)
    return re.sub(r"<([^@]+)@([^>]+)>", r"<\1***@\2>", ua)


async def main() -> None:
    print("=" * 60)
    print("🔍 Sentinax SEC EDGAR Live Smoke Test (Phase 8A)")
    print("=" * 60)

    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua:
        print("⚠️ SEC_USER_AGENT environment variable is not set.")
        print("   Set it before running: export SEC_USER_AGENT='Sentinax <admin@example.com>'")
        sys.exit(1)

    print(f"Declared User-Agent: {mask_user_agent(ua)}")

    client = SECEdgarClient(user_agent=ua)
    sub_provider = SECSubmissionsProvider(client=client)
    facts_provider = SECCompanyFactsProvider(client=client)

    test_cik = "0000320193"  # Apple Inc.
    print(f"\nTarget CIK: {test_cik} (Apple Inc.)")

    # 1. Test Submissions
    print("\n1. Fetching Submissions metadata and recent filings...")
    try:
        res = await sub_provider.fetch_submissions(test_cik)
        meta = res.metadata
        filings = res.filings
        snap_sub = res.main_snapshot
        print(f"   ✅ Entity Name: {meta.entity_name}")
        print(f"   ✅ SIC: {meta.sic} ({meta.sic_description})")
        print(f"   ✅ Tickers: {meta.tickers}")
        print(f"   ✅ Exchanges: {meta.exchanges}")
        print(f"   ✅ Recent Filings Parsed: {len(filings)}")
        if filings:
            latest = filings[0]
            print(f"   Latest Filing: Form {latest.form} (Accession: {latest.accession_number})")
            print(f"   Filing Date: {latest.filing_date}, Report Date: {latest.report_date}")
            print(f"   Acceptance DateTime: {latest.acceptance_datetime} ({latest.acceptance_precision})")
            print(f"   Source URL: {latest.source_url}")
            print(f"   Snapshot ID: {snap_sub.id} (Hash: {snap_sub.payload_hash[:12]}...)")
    except Exception as e:
        print(f"   ❌ Submissions fetch failed: {e}")
        sys.exit(1)

    # 2. Test CompanyFacts
    print("\n2. Fetching CompanyFacts raw XBRL facts...")
    try:
        facts, snap_facts = await facts_provider.fetch_company_facts(test_cik)
        print(f"   ✅ Total Raw Facts Parsed: {len(facts)}")
        taxonomies = {f.taxonomy for f in facts}
        print(f"   ✅ Taxonomies Discovered: {sorted(taxonomies)}")

        sample_concepts = list({f.concept for f in facts[:50]})[:5]
        print(f"   ✅ Sample Standard Concepts: {sample_concepts}")

        # Show 1 instant and 1 duration sample
        inst_sample = next((f for f in facts if f.period_type.value == "instant"), None)
        dur_sample = next((f for f in facts if f.period_type.value == "duration"), None)

        if inst_sample:
            print(f"\n   Sample INSTANT Fact:")
            print(f"     Concept: {inst_sample.taxonomy}:{inst_sample.concept}")
            print(f"     End Date: {inst_sample.end_date}, Value: {inst_sample.value} {inst_sample.unit}")
            print(f"     Accession: {inst_sample.accession_number} (Form {inst_sample.form})")

        if dur_sample:
            print(f"\n   Sample DURATION Fact:")
            print(f"     Concept: {dur_sample.taxonomy}:{dur_sample.concept}")
            print(f"     Period: {dur_sample.start_date} to {dur_sample.end_date}")
            print(f"     Value: {dur_sample.value} {dur_sample.unit} (Form {dur_sample.form})")

        print(f"\n   Snapshot ID: {snap_facts.id} (Hash: {snap_facts.payload_hash[:12]}...)")
    except Exception as e:
        print(f"   ❌ CompanyFacts fetch failed: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("🎉 SEC EDGAR Smoke Test Passed Successfully!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
