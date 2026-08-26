# Sentinax — Foundation Audit Report

**Audit Date:** 2026-08-26  
**Version:** 5.0 (Foundation)  
**Scope:** Full repo audit — KEEP / REFACTOR / REMOVE / DEFER classification

---

## Executive Summary

Sentinax was refactored from an attempted autonomous portfolio management system
to a focused **investment decision-support platform** with two bounded contexts:

- **Public Buffett Engine**: Value investing screener for BIST stocks
- **Private Personal Investment Decision Engine**: Personal portfolio analysis (foundation phase)

Crypto support, order execution, paper trading, and ML predictions have been
permanently removed from the production path.

---

## Audit Classification Table

### Backend — Engine

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `engine/graph.py` | REFACTOR | Islamic node → no-op stub | LangGraph graph preserved |
| `engine/agent_states.py` | KEEP | No change | `check_islamic` field retained (harmless) |
| `engine/circuit_breaker.py` | KEEP | No change | Risk gate logic is correct |
| `engine/execution_engine.py` | **REMOVE** | ✅ Deleted | Paper trading / BUY-SELL orders. Scope violation. |
| `engine/optimization_engine.py` | **DEFER** | ✅ → `deprecated/` | Monte-Carlo MVO. Will be rewritten as HRP/CVaR. |
| `engine/buffett/` | KEEP | No change | Entire Buffett Engine preserved |
| `engine/private/` | **NEW** | ✅ Created | Private Engine bounded context |

### Backend — Analyzers

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `analyzers/bist_analyzer.py` | KEEP | No change | Core BIST+TEFAS analyzer |
| `analyzers/base_analyzer.py` | KEEP | No change | Base class |
| `analyzers/technical_analyzer.py` | KEEP | No change | Technical indicators |
| `analyzers/us_analyzer.py` | KEEP | No change | US stock analysis |
| `analyzers/ml_predictor.py` | **REMOVE** | ✅ → `deprecated/` | Not real ML. Crypto deps. |
| `analyzers/islamic_analyzer.py` | **REMOVE** | ✅ Deleted | Out of scope. User decision. |

### Backend — Data

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `data/market_detector.py` | REFACTOR | CRYPTO → UNKNOWN | Crypto route removed |
| `data/data_sources.py` | KEEP | No change | SSL, session management |
| `data/constants.py` | KEEP | No change | Ticker lists |
| `data/news_fetcher.py` | KEEP | No change | News feed |
| `data/tefas_scraper.py` | KEEP | No change | TEFAS data |
| `data/shadow_pnl_tracker.py` | **DEFER** | ✅ → `deprecated/` | Paper-trade semantics. PnL will be redesigned. |

### Backend — API / Routers

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `api/main.py` | REFACTOR | ✅ Cleaned | Removed scheduler, dead imports |
| `api/routers/buffett.py` | KEEP | No change | Active. `/buffett/*` |
| `api/routers/analysis.py` | **REMOVE** | ✅ Deleted | Comment-out. optimization + predict paths gone. |
| `api/routers/billing.py` | **REMOVE** | ✅ Deleted | Comment-out. No scope. |
| `api/routers/admin.py` | **REMOVE** | ✅ Deleted | Comment-out. No scope. |
| `api/routers/user.py` | **REMOVE** | ✅ Deleted | Comment-out. paper_trades references. |
| `api/routers/chat.py` | **REMOVE** | ✅ Deleted | Comment-out. Old chat system. |
| `api/routers/telemetry.py` | **REMOVE** | ✅ Deleted | Comment-out. |
| `api/models.py` | KEEP | No change | Pydantic request/response models |
| `api/dependencies.py` | KEEP | No change | JWT, rate-limit deps |
| `api/config.py` | KEEP | No change | Settings |

### Backend — Nodes

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `nodes/data_nodes.py` | REFACTOR | ✅ Crypto path removed | BIST/TEFAS/US only |
| `nodes/adversarial_agents.py` | KEEP | No change | Bull/Bear/Neutral/PM agents |
| `nodes/ai_agent.py` | KEEP | No change | LLM advisory functions |

### Backend — Services

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `services/analysis_service.py` | REFACTOR | ✅ optimize_portfolio removed | Placeholder only |
| `services/chat_orchestrator.py` | KEEP | No change | Old graph orchestrator (kept for now) |

### Backend — Infrastructure

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `infrastructure/` (all) | KEEP | No change | Redis, Supabase, LLM factory, auth |

### Backend — Tests

| File | Classification | Action | Notes |
|------|---------------|--------|-------|
| `tests/conftest.py` | KEEP | No change | Zero-trust mocking |
| `tests/test_market_detector.py` | REFACTOR | ✅ CRYPTO→UNKNOWN | Test expectation updated |
| `tests/test_pnl_isolation.py` | SKIP | ✅ `pytestmark skip` | PnL deprecated |
| `tests/test_financial_integrity.py` | SKIP | ✅ `pytestmark skip` | PnL deprecated |
| `tests/test_decimal_precision_v3.py` | SKIP | ✅ `pytestmark skip` | PnL deprecated |
| `tests/test_shadow_pnl_chaos.py` | SKIP | ✅ `pytestmark skip` | PnL deprecated |
| `tests/test_logic_hardening.py` | REFACTOR | ✅ PnL test individually skipped | CB + auth tests active |
| `tests/test_private_domain.py` | **NEW** | ✅ Created | Private engine domain tests |
| `tests/test_no_crypto_path.py` | **NEW** | ✅ Created | Structural regression guard |

### Frontend

| File/Dir | Classification | Action | Notes |
|----------|---------------|--------|-------|
| `frontend/buffett/` | KEEP | No change | Public Buffett UI |
| `frontend/` (main) | KEEP | No change | General UI (legacy, low priority) |
| `frontend-vanilla-backup/` | **REMOVE** | ✅ Deleted | Byte-for-byte duplicate of `frontend/` |

### New Modules Created

| File | Purpose |
|------|---------|
| `backend/deprecated/__init__.py` | Deprecated package marker |
| `backend/deprecated/README.md` | Deprecated archive documentation |
| `backend/engine/private/__init__.py` | Private Engine package |
| `backend/engine/private/domain.py` | Core domain enums and value types |
| `backend/engine/private/result.py` | DataResult / AnalysisResult contracts |
| `backend/engine/private/provider_contract.py` | DataProviderContract Protocol |
| `backend/tests/test_private_domain.py` | Private Engine domain tests |
| `backend/tests/test_no_crypto_path.py` | Structural crypto regression guard |
| `docs/FOUNDATION_AUDIT.md` | This file |

---

## Deferred (Explicit DEFER — Not This Phase)

| Topic | Reason |
|-------|--------|
| EVDS / TEFAS API integration | Data provider implementation — Phase 2 |
| CVaR / HRP / HERC optimizer | Optimizer rewrite — Phase 3 |
| Tax engine | Tax framework — Phase 4+ |
| Portfolio import UI | Frontend Private Engine — Phase 2 |
| ML (experimental) | Shadow mode only — future phase |
| Telegram alerts | Scheduler refactor — future phase |
| Billing / Admin routers | Not in MVP scope |
| `oracle_worker/` | Not yet inspected — standing decision |
| `scripts/` | Not yet inspected — standing decision |

---

## Core Principles Established

1. **Missing data ≠ zero.** `DataResult.unavailable()` always returns `None`.
2. **PARTIAL is a valid, first-class result.** Never crash on missing data.
3. **No fabrication.** If a value cannot be computed, it is `UNAVAILABLE`.
4. **No crypto in scope.** `detect_market()` returns `UNKNOWN` for crypto-like tickers.
5. **No execution.** System never sends orders or simulates paper trades.
6. **Point-in-time integrity.** `effective_date` ≠ `retrieved_at` in provider responses.
7. **Audit trail.** Every data point can be traced to its `ProviderProvenance`.
