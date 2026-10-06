# Phase 27 FIX C: permanent CI coverage of Phase 8 to 14 and source-ingestion correctness

## Finding (F-03)

The Phase 27 audit found that many authoritative Phase 8 to 14 test files existed and passed locally but were never executed by GitHub CI. At the FIX_B baseline (`7fed2768`) `backend/tests` held
177 `test_*.py` files (the Phase 27 brief said 178; the glob `backend/tests/test_*.py` counts 177), of which 102 were directly referenced by `.github/workflows/ci.yml` and 75 were not. 65 of the 75 are
closed source / portfolio / import / attribution authorities. Green CI therefore proved nothing about them, which is how the Phase 27 FIX_A regression (migration 025 versus the Phase 14 trigger locks) survived.

## What is now permanent (eight named steps, 65 files, tests counted at the FIX_C baseline, Python 3.12, `TZ=UTC`)

| CI step | Files | Tests |
|---|---|---|
| Phase 8-11 source and identity foundations (instrument identity, private storage, provider framework) | 3 | 60 |
| Phase 8 SEC fundamental-data correctness | 6 | 200 |
| Phase 9 BIST EOD ingestion correctness | 1 | 28 |
| Phase 10 global EOD provider correctness (Alpha Vantage, Marketstack, Tiingo) | 3 | 69 |
| Phase 11 TEFAS source-ingestion correctness (price, current metrics) | 2 | 62 |
| Phase 12 portfolio domain and persistence correctness | 11 | 437 |
| Phase 13 portfolio import pipeline correctness (including the canonical CSV path) | 23 | 1028 |
| Phase 14 fee-tax attribution correctness | 16 | 687 |

Step names are functional, not a rewrite of phase history (the foundations were not labelled Phases 1 to 7 in the repository). Counts are audit evidence that the intended files ran, not correctness proof.

## Separations kept

- Provider adapters (Phase 8 to 11 ingestion) are distinct from the point-in-time resolvers (Phase 26 market-data dependency regression, kept) and from Phase 16 fund analytics (kept).
- Ordinary tests are distinct from the real PostgreSQL gates, which stay separate on the disposable PostgreSQL 16 service: Phase 26 history-coverage concurrency and Phase 27 trigger privilege regression. The Phase 14 step is in
  addition to, never instead of, the latter.
- The FIX_B gate `test_backtest_market_data_temporal_admission.py` stays in Phase 26 backtest architecture correctness. All Phase 15 to 26 and static / no-crypto gates are unchanged.

## Socket policy

Portfolio, import and attribution groups run with `--disable-socket`. The source/provider groups are asyncio tests: pytest-asyncio opens an AF_UNIX `socketpair` for its event loop, which `--disable-socket` alone blocks
(`SocketBlockedError`). Those five steps therefore add `--allow-unix-socket`: every network (AF_INET/AF_INET6) socket stays blocked, so no live SEC, BIST, Alpha Vantage, Tiingo, Marketstack, TEFAS or Supabase access is
possible and no credential is needed. (The precious-metal and Phase 17 provider steps keep their existing different exception.)

## Result

All 65 required files are now referenced by CI (65 / 65); 167 of 177 test files are referenced. No production code, migration, dependency, baseline or existing test file was changed.

## Intentionally not (yet) permanent: 10 files

Not part of a closed Phase 8 to 14 production authority and not automatically suitable for every push; each needs a separate decision:

- chaos and resilience: `test_chaos_engineering.py`, `test_chaos_resilience.py`, `test_llm_chaos.py`
- performance: `test_performance.py`
- end-to-end / legacy public-app and SRE utilities: `test_e2e_integration.py`, `test_sre_firewall.py`, `test_logic_hardening.py`, `test_market_detector.py`, `test_technical_analyzer.py`
- macro provider foundation not in the Phase 8 to 14 list: `test_ecb_eurostat_treasury.py` (a candidate for a later checkpoint)

## Observation (not a CI failure)

`test_provider_framework.py::TestProviderOrchestratorSuite::test_01_primary_success_yields_complete` depends on the ambient clock and timezone (it failed locally with a UTC+3 local date after midnight, "future data lookahead",
and passes under `TZ=UTC`, which GitHub Actions uses). It is a latent test/clock sensitivity of the legacy foundation orchestrator, left untouched here.
