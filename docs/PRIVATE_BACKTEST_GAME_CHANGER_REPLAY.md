# Private backtest Game Changer decision replay (Phase 26D1)

`backend/engine/private/backtest_game_changer_replay.py` is the **first Phase 26D deterministic decision replay slice**. It does not build the whole Private Engine
decision pipeline: it replays the one mature closed policy that can consume a C2E historical authority directly, the Phase 23C2 Game Changer decision gate.

## Contract

`replay_private_backtest_game_changer_decision(*, input_bundle)` returns a frozen `PrivateBacktestGameChangerDecisionReplay` with exactly two fields:
`input_bundle` and `game_changer_gates`. Nothing the bundle already carries (context, cutoff, portfolio, resolutions) is duplicated, and there is no status, hash or decision id.

- **Requires a COMPLETE C2E bundle** with no missing requirement. An incomplete bundle fails closed; there is no partial replay and no default evidence.
- **Exact D1 manifest:** exactly one PORTFOLIO_HISTORY requirement plus one or more GAME_CHANGER requirements, nothing else. Candidate-universe, macro, risk-evidence,
  market-data and user-view requirements are rejected because D1 would ignore their evidence; they belong to later slices, not to "unimportant".
- **No false "no events" state:** zero GAME_CHANGER requirements is rejected. An empty gate tuple must never read as "no Game Changer events existed"; C2E already says an
  omitted category is outside the declared contract, not an empty search.
- **Portfolio history is the replay anchor only.** It is retained through the bundle; D1 reads no transaction, derives no holdings and never filters events by what is held
  (the gate also governs new capital).

## Gate derivation

For each Game Changer binding in the bundle's canonical order, exactly one call to the closed `build_game_changer_decision_gate(resolution=binding.resolution)`:
one historical family, one existing Phase 23C2 gate. Output order equals the bundle order and every `gate.resolution` is the binding's resolution by object identity. No
severity sorting, no latest-revision search, no second policy.

- **INCOMPLETE_COVERAGE is replayed, not rejected.** C2E already proved the historical state is present; the closed gate yields review REQUIRED and, for instrument scope,
  PAUSED_PENDING_EVIDENCE.
- **Resolved families** delegate entirely to Phase 23C2 (OPEN, QUARANTINED, review reasons); only the terminal assessment counts and earlier revisions do not leak.
- **Systemic safety:** a SYSTEMIC event stays NOT_APPLICABLE_SYSTEMIC with no affected ids; it is never widened to the portfolio, candidates or sleeves.
- **Quarantine is not a sale.** QUARANTINED keeps its Phase 23 meaning: pause new capital to the exact affected instruments plus review. There is no exit, target weight,
  no rebalance, no order, no execution and no portfolio mutation.
- **No aggregation:** no overall severity, maximum, score or confidence; multiple gates stay multiple explicit gates.

## Validation and determinism

One private validation path is shared by the builder and `__post_init__`: exact bundle type, COMPLETE, D1 manifest shape, at least one family, exact gate tuple, one gate per
binding, in order, over that binding's very resolution. A valid independently constructed canonical gate is accepted; a gate from another resolution (even an equal-valued
copy), a reordered, missing or extra gate is rejected. Gate policy is not duplicated: `GameChangerDecisionGate` already self-validates. The output is a pure function of the
immutable bundle: no clock, randomness, generated id, environment or global state.

## Not here

No market data is consumed: D1 imports no market-data module, calls no resolver and parses no selected observation, so C2C2 remains deferred (no typed market-data
consumer exists yet). No allocation benchmark, optimizer, user-view posterior, candidate sleeve binding, rebalance, macro, technical analyzer or scheduler. No simulated
execution, performance attribution or walk-forward. Later Phase 26D slices require an independent Red-Team design review.

D5B later consumes the exact D1 gates only after proving D1 and D5A share the same analysis context and exact portfolio projection binding (`docs/PRIVATE_BACKTEST_GAME_CHANGER_PLAN_ADMISSION.md`).
The later plan-admission layer does not change D1 gate semantics.
