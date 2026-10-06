# Private backtest market-data selected observation (Phase 26C2C2)

`backend/engine/private/backtest_market_data_selected_observation.py` reconstructs, from a Phase 26C2C1 resolution snapshot, a strictly typed **canonical typed representative** of
the SELECTED observation. It is **not original object** resurrection: the immutable C2C1 JSON stays the authority and the original mutable object's identity is gone.

## Why it was previously deferred, and why it is needed now

C2C2 stayed deferred until a real typed consumer existed: D1 (Game Changer replay) and D2 (candidate eligibility replay) consume no market observation. The upcoming D3 portfolio
replay needs **marked** current values (Phase 21 requires caller-supplied marked values), and the ledger gives quantities and cash but no replay-date price. Transaction prices, cost
basis, last purchase price, the latest price and any CURRENT_REPORTED fallback must not stand in for that, so PIT-safe prices selected by C2C1 must be readable as typed observations.

## Contract

`reconstruct_private_backtest_selected_observation(*, resolution_snapshot)` returns a frozen `PrivateBacktestMarketDataSelectedObservation` with exactly one field, the exact C2C1
snapshot. Nothing derived (kind, key, status, ids, price, currency, date) is stored a second time. `reconstruct()` returns a **fresh** typed object on every call; a mutable legacy
observation is never retained, so mutating one result cannot alter the wrapper, the C2C1 JSON or a later reconstruction. Direct construction performs the same full reconstruction,
so malformed selected JSON cannot be wrapped.

- **SELECTED only.** Any other canonical resolver status fails closed: never None, never an empty observation. C2E may count those states as present evidence, but a price consumer
  cannot reconstruct an observation that was not selected.
- **Strict five-kind payload shapes** (BIST_EOD, GLOBAL_EOD, TEFAS_FUND_PRICE, TEFAS_CURRENT_METRICS, PRECIOUS_METAL): the exact `to_dict()` key set, no missing and no extra key, so
  schema drift fails closed and forces an explicit checkpoint update.
- **Explicit parsers only:** canonical UUID text, ISO date, timezone-aware ISO datetime, Decimal from its exact finite string, exact int (not bool), exact enum, exact list. A float, an
  int or bool in place of a Decimal string, NaN or Infinity, a naive datetime or a malformed enum is rejected. There is no `**kwargs` pass-through.
- **No default factories:** every constructor field is supplied from stored evidence, so no id, timestamp, confidence or provider is created on behalf of historical data.
- **Exact round trip:** the representative's `to_dict()` must equal the stored selected payload (deep equality, no normalization, no dropped diagnostics).
- **Top-level consistency** with the C2C1 result and query key: selected observation id; instrument identity (observation, query key and canonical instrument id; none is invented for
  the precious-metal reference); snapshot id and hash lineage (`snapshot_hash` for BIST, `payload_hash` otherwise); retrieval instant (same UTC instant, no tolerance; an equivalent
  offset only when the stored payload round-trips unchanged; compared only where the observation carries `retrieved_at`); effective date (None for current metrics); query semantics
  (the closed `PreciousMetalSemanticKey.matches`; the Global provider by the closed resolver's own query rule); provider; and confidence, exactly.

## SELECTED eligibility (R1)

Note that payload type validity is insufficient: `_enum(...)` accepts any legitimate enum member, so a directly constructed C2C1 envelope (C2C1 validates only the envelope) could otherwise carry
top-level SELECTED over an `INVALID_OBSERVATION` or a missing price. Top-level SELECTED requires **surface-specific observation eligibility**, which C2C2 validates against the closed
resolver's final observation-eligibility invariants without running the resolver (no selection, no source snapshots, no conflict logic, no provider lookup, no fallback):

| Kind | Required |
|---|---|
| BIST_EOD | status VALID; `close` present and finite (no positivity rule: the closed resolver has none) |
| GLOBAL_EOD | status VALID; `close` present and finite (`adj_close` is not required and is not a valuation authority) |
| TEFAS_FUND_PRICE | status VALID; unit price present, finite and > 0; currency present; instrument type in the five closed TEFAS types |
| TEFAS_CURRENT_METRICS | status VALID; portfolio size present, finite, >= 0; portfolio-size currency TRY; allowed TEFAS type; outstanding units None or finite >= 0; investor count None or int >= 0; `retrieved_at` present and the same instant as the top-level snapshot retrieval (the existing None `effective_date` / `published_at` rules remain; the diagnostic reported price is not required) |
| PRECIOUS_METAL | status VALID; price present and finite (no positivity rule); `query_key.matches` stays the closed semantic authority |

The local TEFAS allowed-type set is restated here so this module never imports the resolver; a test asserts it equals the resolver's `TEFAS_RESOLVER_ALLOWED_INSTRUMENT_TYPES` exactly.
This prevents a directly constructed C2C1 envelope from turning an invalid observation into a typed SELECTED price.

**Claim limit:** C2C2 still does not recompute the resolver resolution key and does not prove the resolver was actually called. Its claim is only that the immutable C2C1 SELECTED envelope
holds a strictly typed representative whose stored semantics are internally compatible with a closed resolver SELECTED outcome. D3A may rely on C2C2 only after this R1 closure.

## Limits and safety

- **BIST `raw_provider_symbol`:** `to_dict()` emits `raw_provider_symbol or symbol`, so an original None cannot be told from a value equal to the symbol. The representative takes the
  serialized value: representational canonicalization, not economic mutation; the round trip stays mandatory.
- **Confidence has two layers.** Observation-level confidence (the observation's own `confidence_level` / `confidence`) and resolver-level confidence (the top-level result) are distinct.
  The stale-discovery degradation belongs to the closed resolver: for BIST and precious metal, when `is_stale_discovery` is true the resolver appends DEGRADED_DISCOVERY and turns HIGH
  into MEDIUM, otherwise the layers agree; Global, TEFAS price and TEFAS metrics never degrade (`is_stale_discovery` is always false). C2C2 preserves and validates that stored
  relationship (a canonical stale-discovery SELECTED result reconstructs successfully; a tampered or impossible relationship is rejected) rather than recomputing a policy, and it never
  promotes or degrades either value. The reconstructed observation keeps its own confidence and the C2C1 JSON keeps the resolver-level one.
- **Global provider canonicalization** follows the already-closed resolver's `strip().upper()` rule only: the observation provider must equal the stored top-level provider exactly, and
  that must equal `query_key.provider.strip().upper()` (a query of `tiingo` legitimately resolves to `TIINGO`). C2C2 invents no new normalization: no aliases, no fuzzy matching, no
  substitution, no other canonicalization.
- **TEFAS metrics:** `to_dict()` fixes `published_at` and `effective_date` to None, so those must be None in the payload. `reported_current_unit_price` is reconstructed faithfully but
  stays **diagnostic**; TEFAS_FUND_PRICE is the dedicated fund-price surface.
- **no valuation-price policy:** this module does not decide which field is a valuation price (no close, adj_close, unit_price or price mapping) and does no arithmetic, conversion or
  currency handling. That belongs to a later D3 valuation adapter.
- A precious-metal observation is a dimensioned market reference and **not automatically a portfolio instrument**; no instrument id is created.
- **no resolver replay:** no resolver call, no raw source snapshot, no latest or conflict re-run. **no fallback:** no CURRENT_REPORTED, no SOURCE_AS_OF to SYSTEM_AS_OF, no previous day,
  no alternate provider, no synthetic observation. No clock, randomness, UUID or hash generation, no I/O; non-pure by dependency composition and not in the PURE manifest.

## Next

C2C2 alone does not create portfolio valuation. D3A (historical marked portfolio current state: ledger quantities and cash, these typed observations, explicit valuation-price and
currency policy, fail-closed missing prices) starts only after an independent Red Team of C2C2.
