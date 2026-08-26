# Sentinax — Foundation Audit Report

**Audit Date:** 2026-08-26  
**Version:** 7.0 (Canonical Identity & Consistency Hardened)  
**Scope:** Canonical Identity, Immutability & Storage Consistency Hardening

---

## Executive Summary

Sentinax operates as an institutional-grade **investment decision-support platform** with two decoupled bounded contexts:

- **Public Buffett Engine**: Value investing screener for BIST stocks.
- **Private Personal Investment Decision Engine**: Multi-asset portfolio analysis and decision engine.

All ambiguous identifiers, redundant UUID fields, dangerous silent defaults, and partial-row immutability gaps have been eliminated.

---

## Audit Classification & Invariants

### 1. Single Canonical Instrument UUID
- Master instruments use a single canonical identifier: `instruments.id UUID PRIMARY KEY`.
- Redundant `internal_instrument_id` was eliminated from DB and Python models.
- All relationships (`provider_aliases.instrument_id`, `corporate_actions.instrument_id`, `normalized_observations.instrument_id`) reference `instruments.id`.
- Referential integrity: `normalized_observations.instrument_id` references `instruments(id)` with `ON DELETE RESTRICT`.

### 2. Provider Contract Identifier Semantics
- `ProviderResponse` and `ProviderProvenance` clearly separate:
  - `canonical_instrument_id: Optional[UUID]` (Sentinax canonical identity)
  - `provider_symbol: Optional[str]` (provider-native query identifier/ticker)
- Eliminates ambiguous `instrument_id: str` from provider contracts.

### 3. Full-Row Immutability & Anti-Tamper Protection
- `raw_provider_snapshots` and `normalized_observations` enforce full-row immutability via PostgreSQL triggers using strict allow-lists.
- DELETE is forbidden on both tables.
- UPDATE is restricted exclusively to system-driven `is_superseded` and `superseded_at` transitions. All substantive data, timestamps, and `supersedes_record_id` cannot be modified.

### 4. Corporate Action Strict Field Exclusivity
- `SPLIT`: Requires `split_factor > 0`; strictly forbids `cash_amount`, `currency`, `old_symbol`, `new_symbol`.
- `DIVIDEND`: Requires `cash_amount >= 0`, `currency NOT NULL`; strictly forbids `split_factor`, `old_symbol`, `new_symbol`.
- `SYMBOL_CHANGE` / `FUND_CODE_CHANGE`: Requires `old_symbol` & `new_symbol`; strictly forbids `split_factor`, `cash_amount`.
- `MERGER` / `DELISTING`: Strictly forbids `split_factor` and `cash_amount`.

### 5. Elimination of Dangerous Silent Defaults
- `instruments.currency` and `normalized_observations.currency` are explicitly required (`Currency`). No silent default to `TRY`.
- `instruments.mic` defaults to `None` (explicit for exchange-traded equities/ETFs, omitted for TEFAS funds/FX).

### 6. Provider Alias Case-Insensitive Normalization
- PostgreSQL generated stored columns: `normalized_provider` (`lower(trim(provider))`), `normalized_symbol` (`upper(trim(provider_symbol))`).
- `btree_gist` exclusion constraint prevents overlaps across variations like `Yahoo` vs `yahoo` or `META` vs `meta`.
- Python resolver mirrors normalization across all lookup paths.
