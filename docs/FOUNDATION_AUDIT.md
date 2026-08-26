# Sentinax — Foundation Audit Report

**Audit Date:** 2026-08-26  
**Version:** 6.0 (Hardened Foundation)  
**Scope:** Full repo audit & hardening — KEEP / REFACTOR / REMOVE / DEFER classification

---

## Executive Summary

Sentinax is a focused **investment decision-support platform** with two clear bounded contexts:

- **Public Buffett Engine**: Value investing screener for BIST stocks.
- **Private Personal Investment Decision Engine**: Institutional-grade personal portfolio analysis and decision engine.

All out-of-scope legacy systems (crypto, automated order execution, paper trading, unverified ML models, and Monte-Carlo optimizers) have been **permanently purged** from the repository.

---

## Audit Classification & Actions

### Backend — Engine

| Component | Status | Action | Rationale |
|-----------|--------|--------|-----------|
| `engine/graph.py` | REFACTOR | IslamicNode removed entirely; 2-branch data ingestion (Market, News) | Scope hardening. |
| `engine/agent_states.py` | REFACTOR | `islamic_report` and `check_islamic` removed | Clean GraphState. |
| `engine/circuit_breaker.py` | KEEP | Python 3.9 type-hint compatibility fixed | SRE risk firewall. |
| `engine/execution_engine.py` | **REMOVE** | ✅ Deleted | Sentinax does not send orders. |
| `deprecated/` | **REMOVE** | ✅ Permanently deleted | Git history preserves archive. Prevents accidental re-import. |
| `engine/buffett/` | KEEP | Untouched | Production value-investing screener. |
| `engine/private/` | **NEW** | ✅ Hardened | Pure UUID identity, interval overlap prevention, dual PIT query modes. |

### Backend — Identity & Storage Layer (Private Engine)

| Component | Status | Description |
|-----------|--------|-------------|
| `domain.py` | NEW | Missing data ≠ 0 invariant, PARTIAL aggregate analysis, `AsOfMode` (SOURCE_AS_OF vs SYSTEM_AS_OF). |
| `identity.py` | NEW | Master `InstrumentRecord` with pure UUID identity, `ProviderAliasRecord` with [valid_from, valid_to) interval semantics, `CorporateActionRecord` with action-specific field validation, `InstrumentResolverService`. |
| `storage_models.py` | NEW | `RawProviderSnapshotRecord` (deterministic SHA-256 hash), `NormalizedObservationRecord` (UUID instrument reference, strict PIT timestamps). |
| `provider_contract.py` | NEW | Runtime checkable `DataProviderContract` protocol. |
| `004_private_engine_pit_storage.sql` | NEW | Immutable raw snapshots, PIT normalized observations, anti-tamper triggers, dual-mode PIT RPC. |
| `005_instrument_identity.sql` | NEW | Master instruments (UUID), provider aliases with `btree_gist` exclusion constraint, corporate action semantic constraints. |

### Frontend

| Component | Status | Action | Rationale |
|-----------|--------|--------|-----------|
| `frontend/` (Buffett) | KEEP | Untouched | Active UI for Buffett Screener. |
| `frontend-vanilla-backup/` | **REMOVE** | ✅ Deleted | 100% duplicate directory. |

---

## Key Invariants Enforced

1. **UUID Instrument Identity:**
   - Tickers, fund codes, company names are NEVER primary keys.
   - Master instruments use immutable UUIDs (`internal_instrument_id`).
   - Ticker renames (e.g. `FB` -> `META`) preserve historical continuity without breaking time series.

2. **Provider Alias Interval Integrity:**
   - Boundary semantics: Half-open `[valid_from, valid_to)`.
   - Overlapping date intervals for the same `(provider, provider_symbol)` are strictly rejected by PostgreSQL `btree_gist` exclusion constraint and Python resolver service.
   - Non-overlapping historical ticker reuse is fully supported.

3. **Corporate Action Field Isolation:**
   - `SPLIT` requires `split_factor > 0` and forbids `cash_amount`.
   - `DIVIDEND` requires `cash_amount >= 0` and forbids `split_factor`.
   - `SYMBOL_CHANGE` requires `old_symbol` and `new_symbol` and forbids split/cash amounts.

4. **Point-In-Time (PIT) Storage & Dual Query Modes:**
   - `SOURCE_AS_OF`: Returns facts publicly available to the market at `as_of` (`published_at <= as_of`, fallback to `observed_at`).
   - `SYSTEM_AS_OF`: Returns facts ingested into Sentinax at `as_of` (`ingested_at <= as_of` AND `published_at <= as_of`).
   - Revisions & amendments published after `as_of` are strictly invisible to historical queries.
   - Anti-tamper triggers prohibit DELETE and destructive UPDATE on raw snapshots and observations.

5. **Missing Data Contract:**
   - Missing data is NEVER represented as 0.0 or fabricated.
   - Results with missing inputs produce `DataStatus.PARTIAL` or `DataStatus.UNAVAILABLE`.
