# Point-in-Time Global EOD Market Data Observation Resolver

## 1. Overview & Architectural Role

The `PointInTimeMarketDataResolver.resolve_global_eod(...)` extension provides deterministic, audit-grade Point-in-Time (PIT) selection for Sentinax's global market data providers:
- **Alpha Vantage** (`ALPHA_VANTAGE`): Low-volume current/recent US/EU EOD prices.
- **Tiingo** (`TIINGO`): Free tier US equities/ETFs long history (60+ years) and corporate actions.
- **Marketstack** (`MARKETSTACK`): Free tier European equities/ETFs rolling 1-year history and corporate actions.

---

## 2. Core Resolution Invariants

1. **Provider-Explicit Authority:**
   The `GlobalEODQueryKey` requires an explicit provider name (`ALPHA_VANTAGE`, `TIINGO`, `MARKETSTACK`), a canonical `instrument_id` (UUID), and an economic `trade_date`. The resolver does not automatically rank or fallback across providers; multi-provider orchestration belongs strictly to downstream layers.
2. **Per-Instrument Snapshot Authority:**
   Global market data providers deliver per-instrument time-series snapshots. Snapshots for one instrument (e.g. `SPY`) never supersede or affect another instrument (e.g. `AAPL`).
3. **Target-Date Coverage Hierarchy:**
   A snapshot is authoritative for `query.trade_date` if:
   * **A) Explicit Request Bounds:** `start_date <= target_date <= end_date`
   * **B) Returned Trade Date Range:** `min_date <= target_date <= max_date`
   * **C) Explicit Observation Presence:** The snapshot contains a row matching `target_date`.
   Snapshots that do not cover the target date are disregarded and cannot supersede historical observations.
4. **Incremental Snapshots Do Not Erase History:**
   A recent incremental snapshot (e.g. covering `2026-08-19` to `2026-08-20`) does not cover `2024-06-10` and therefore never invalidates or supersedes an older 5-year history snapshot.
5. **No-Resurrection Under Covered Scopes:**
   If a newer snapshot explicitly covers `target_date` (e.g. full-year re-pull) but the target date observation is absent or invalid, the resolver fails closed with `NO_ELIGIBLE_OBSERVATION` rather than resurrecting older superseded data.
6. **Temporal Lineage Modes:**
   * `CURRENT_REPORTED`: Latest available authoritative snapshot as of current knowledge.
   * `SYSTEM_AS_OF`: Filters `snapshot.retrieved_at <= as_of` **before** conflict checks and observation evaluation. Future corruptions or conflicts cannot contaminate historical backtests. Requires timezone-aware timestamps.
   * `SOURCE_AS_OF`: Returns `UNAVAILABLE_SOURCE_AS_OF` because global EOD aggregators do not supply immutable historical first-publication timestamps.
7. **Observation Lineage & Deduplication:**
   * Lineage invariants require matching `provider`, `snapshot_id`, and `payload_hash`.
   * Multiple rows with differing fingerprints flag `OBSERVATION_CONFLICT`. Identical logical fingerprints deduplicate deterministically.
   * `resolution_key` is calculated via SHA-256 over economic and logical attributes, completely independent of memory object UUIDs and input list order.
8. **Raw vs Adjusted Preservation:**
   The resolver selects and returns the complete `GlobalEODObservation`. It does not perform price calculations or choose valuation vs return price bases. `adj_close`, `split_factor`, and `div_cash` are preserved untouched.

---

## 3. European Deep History Disposition

- `EU_DEEP_HISTORY_BOOTSTRAP >= 5Y` = **DEFERRED_TO_PHASE_13_IMPORTS**
- European daily valuation remains supported via Alpha Vantage Free, and rolling 1-year history is supported via Marketstack Free.
- If a downstream model requires European history deeper than 1 year and no imported dataset is present, the model must degrade or report data unavailability. No prices are ever fabricated.
