# Private Backtest Market-Data Bridge (Phase 26A2)

Module: `backend/engine/private/backtest_market_data_bridge.py`.

- **A1** (`backtest_replay_point.py`) is the PURE temporal replay authority: the PIT context (knowledge cutoff and `AsOfMode`) plus an explicit `evaluation_date`.
- **A2** (this module) is a NON-PURE dependency adapter. It is outside the PURE manifest because the canonical `MarketDataResolutionMode` lives in
  `market_data/models.py`, which is outside the PURE dependency graph (G3 is not weakened and `market_data/models.py` is not added to the manifest). That is a
  dependency-graph classification, not permission for I/O: the module is deterministic and side-effect free.

## Mapping

By exact enum member identity, never by serialized value, name or case (the two enums differ in value casing):

- `AsOfMode.SOURCE_AS_OF` -> `MarketDataResolutionMode.SOURCE_AS_OF`
- `AsOfMode.SYSTEM_AS_OF` -> `MarketDataResolutionMode.SYSTEM_AS_OF`

`CURRENT_REPORTED` has no historical replay mapping and is never referenced by the bridge.

## No fallback

There is no fallback of any kind: no fallback between modes and no fallback to CURRENT_REPORTED.

An unavailable SOURCE_AS_OF stays unavailable (the resolver returns `UNAVAILABLE_SOURCE_AS_OF`); it is never retried as SYSTEM_AS_OF. A SYSTEM_AS_OF query
with no snapshot known at the cutoff stays `NO_SNAPSHOT_AS_OF`; it is never retried as CURRENT_REPORTED.

## Axes

`as_of` is the original knowledge-cutoff object of the replay point (not normalized; the resolver compares aware datetimes by instant). The economic
`evaluation_date` is a different axis: evaluation_date != market-data as_of. Market-data query keys already carry their own trade/effective dates.

## Boundaries

The bridge does not call the resolver, builds no query key and adds no wrapper: consumers pass `resolution_mode` and `as_of` to the existing resolver. No clock,
hash, randomness, I/O or loop. Remote CI does not run the Phase 26A1/A2 tests yet (a later Phase 26 boundary will).
