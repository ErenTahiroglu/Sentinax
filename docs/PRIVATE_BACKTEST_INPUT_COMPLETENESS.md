# Private backtest input completeness (Phase 26C2E)

`backend/engine/private/backtest_input_completeness.py` is the explicit replay **requirement manifest** boundary. For one exact replay context it answers:
which input slots did the caller explicitly require, and is each represented by exactly one compatible historical authority object? It is **not data quality**,
availability or investment safety, and it makes no decision or recommendation.

## Requirement manifest

`PrivateBacktestInputRequirement(kind, key)`: a closed `PrivateBacktestInputKind` (candidate_universe, macro, game_changer, risk_evidence, portfolio_history,
market_data, user_views) and an exact, non-empty, un-normalized `str` key. The caller owns the manifest; nothing is discovered, defaulted or inferred. Requirements are
canonicalized by `(kind.value, key)`; a duplicate `(kind, key)` is invalid. Exactly one PORTFOLIO_HISTORY requirement is mandatory (a zero-transaction portfolio is
valid; no portfolio is not). USER_VIEWS has at most one requirement and its key is exactly `user_views`. No other category is required merely because it exists.

## Canonical keys (derived from the evidence, never hashed or caller supplied)

| Kind | Key |
|---|---|
| candidate_universe | `source_key\|universe_key\|asset_class.value` of `binding.resolution.query` |
| macro | `fact.canonical_key` |
| game_changer | `family.coverage_provenance_sha256` (the existing opaque family-evidence reference; not a global event-family id) |
| risk_evidence | `evidence.kind.value` |
| portfolio_history | `owner_id\|portfolio_id` (canonical `str(UUID)`) |
| market_data | `kind.value\|query_key.to_string()` |
| user_views | the constant `user_views` |

## Bundle semantics

`build_private_backtest_input_bundle(...)` (keyword-only, no defaults, exact tuples and exact types) stores the exact evidence objects by object identity in canonical order
plus `status` (COMPLETE / INCOMPLETE) and `missing_requirements` (the exact canonical requirement objects with no supplied evidence). Caller order has no authority.

- **One replay context.** C2A bindings, portfolio coverage and user views must carry the very `analysis_context` object; market data must carry the very replay-point
  object. An equal-valued clone fails.
- **Duplicate evidence** (same derived `(kind, key)`, including the same object twice) fails closed; nothing is selected or deduplicated.
- **Extra evidence is forbidden.** Evidence the manifest does not declare fails closed, so the downstream decision never receives an undeclared input. A wrong owner or
  portfolio is extra evidence.
- **Missing evidence** for a declared slot is reported in `missing_requirements` and makes the bundle INCOMPLETE; it is not a construction error.
- **Direct construction** re-runs the same derivation: forged status, forged or cloned-requirement missing tuples, noncanonical order, duplicates, extras and context
  mismatch are rejected.

## Input completeness is not data availability

The module never reads an input's internal status. All of these are present, represented historical states that fill their slot and are preserved for Phase 26D:
candidate NO_SNAPSHOT_AS_OF / FRONTIER_CONFLICT / other statuses, macro UNAVAILABLE, `MissingRiskEvidence`, Game Changer INCOMPLETE_COVERAGE, any market-data resolution
status (unavailable, missing, conflict) and user-view INCOMPLETE_COVERAGE. A source-only AST guard in the tests enforces that completeness is derived from identity
and context ownership only.

## Missing is not zero; omitted is not empty

- An empty requirement category means that input is **outside the declared replay contract**. It does not mean "we searched and found none"; in particular an empty
  Game Changer set makes no "no Game Changer events" claim and invents no provider discovery coverage.
- User views: omitting the USER_VIEWS requirement (overlay outside the contract) is distinct from requiring it and supplying a COMPLETE resolution with zero active
  revisions (the canonical known-zero-active-views state). A required but unsupplied resolution is missing.

## Claim limit

Completeness is relative to the caller's manifest. It does not prove the manifest itself lists every input a strategy ought to use. Phase 26D may consume only an
explicitly complete bundle whose manifest matches its own declared decision contract. No second coverage mechanism is added and no database is called.

## Not here

No decision, gate, view set, posterior, optimizer, rebalance, order or execution. C2E itself does not reconstruct observations: it carries the C2C1 snapshots only, and typed selected-observation reconstruction is the separate C2C2 boundary (`docs/PRIVATE_BACKTEST_MARKET_DATA_SELECTED_OBSERVATION.md`). No I/O, clock, randomness, hash or persistence. Non-pure by dependency composition; not in the PURE manifest. Phase 26D1 consumes COMPLETE C2E bundles for the historical Game Changer replay slice (see `docs/PRIVATE_BACKTEST_GAME_CHANGER_REPLAY.md`). D1 and D2 did not require C2C2 (as recorded at those checkpoints: C2C2 remains deferred because D1 consumes no typed market observation). Phase 26D2 consumes COMPLETE portfolio + candidate-universe bundles for conditional historical sleeve-eligibility replay (see `docs/PRIVATE_BACKTEST_CANDIDATE_ELIGIBILITY_REPLAY.md`); its sleeves are separate fixed replay policy parameters and are not claimed by C2E to have historical availability provenance. (C2C2 remains deferred because D2 consumes no typed market observation.) The upcoming D3 portfolio marked-value boundary does need typed selected observations, so C2C2 is now implemented. Later Phase 26D slices need an independent Red Team first.
