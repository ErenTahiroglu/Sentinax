# Private Backtest Market-Data Resolution Snapshot (Phase 26C2C1)

Module: `backend/engine/private/backtest_market_data_resolution_snapshot.py` (NON-PURE, not in the PURE manifest).

## Purpose

A historical replay must not keep a mutable resolver result as authority. C2C1 captures one closed-resolver outcome as an immutable canonical JSON audit snapshot.
It does not claim that the overall historical input bundle is complete.

## Flow

1. The Phase 26A2 `PrivateBacktestMarketDataContext` supplies the historical `resolution_mode` (SOURCE_AS_OF or SYSTEM_AS_OF) and `as_of`.
2. The closed `PointInTimeMarketDataResolver` remains the only selection authority. A typed builder calls exactly one resolver method with that mode and `as_of`.
3. The legacy `MarketObservationResolutionResult` and the source snapshot models are mutable. The builder immediately converts the resolver's audit-grade `to_dict()` into
   canonical JSON text:
   `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`.
4. The source snapshots and the mutable result are used only during that synchronous call and are never retained. Later mutation of them cannot change the stored text.

## Supported surfaces (exactly five)

| Kind | Builder | Resolver method |
|---|---|---|
| `BIST_EOD` | `bind_private_backtest_bist_eod_resolution` | `resolve_bist_eod` |
| `GLOBAL_EOD` | `bind_private_backtest_global_eod_resolution` | `resolve_global_eod` |
| `TEFAS_FUND_PRICE` | `bind_private_backtest_tefas_fund_price_resolution` | `resolve_tefas_fund_price` |
| `TEFAS_CURRENT_METRICS` | `bind_private_backtest_tefas_current_metrics_resolution` | `resolve_tefas_current_metrics` |
| `PRECIOUS_METAL` | `bind_private_backtest_precious_metal_resolution` | `resolve_precious_metal` |

The kind enum is not another resolution mode. Builders require exact query-key classes and an exact `tuple` of exact snapshot classes. No builder accepts a caller-supplied result.

## Stored contract

Exactly four fields: `market_context`, `kind`, `query_key`, `resolution_payload_json`. Nothing else is stored: no result object, no source snapshot, no selected
observation object, no second status/mode/`as_of`, and no digest or id of the binding itself. `status`, `resolution_key`, `resolution_payload()` and
`selected_observation_payload()` are derived from the stored text; the dict accessors return a freshly parsed copy on every call.

## Invariants

- The audit payload must be recursively JSON-native (exact dict/list/str/int/bool/None); anything else is a RuntimeError. No float anywhere.
- CURRENT_REPORTED is impossible: the context only maps to SOURCE_AS_OF / SYSTEM_AS_OF and there is no fallback or retry in another perspective.
- The resolver result's mode must be the context mode and its `as_of` must be the same UTC instant as the context cutoff (equivalent offsets are valid, one microsecond off is not); otherwise RuntimeError.
- The resolver's `effective_date` must equal the query's economic date (null for TEFAS current metrics), and `canonical_instrument_id` must equal the query instrument id on the four instrument surfaces. These dates are never used to derive replay knowledge time.
- Every `MarketDataResolutionStatus` is preserved, including unavailable (for example UNAVAILABLE_SOURCE_AS_OF), missing and conflict outcomes. Only malformed or contradictory resolver output is an error.
- There is no hash and no new identity (no hash of the stored text is created): the resolver's own `resolution_key` stays the closed authority.
- No provider, network, filesystem, database, clock or randomness.

## Direct construction

Direct construction validates the evidence envelope only (exact types, JSON object with the closed result key set, mode and `as_of` match the context, canonical status).
It cannot prove that the caller ran the resolver. The typed builders are the authoritative normal path.

## No completeness claim

C2C1 is provenance capture, not a completeness judgment. There is no notion of required, missing-category, ready or decision-ready market data here; that belongs to C2E.

## Deferred

- C2B2B3 real PostgreSQL concurrent-writer verification is closed (verified by a real PostgreSQL integration test in CI).
- C2C2 typed selected-observation reconstruction is deferred unless later decision replay actually needs it.
- C2D user-view historical provenance, C2E completeness, Phase 26D onward are not part of this checkpoint.
