# Private Backtest Replay Point (Phase 26A1)

Module: `backend/engine/private/backtest_replay_point.py` (pure; registered in the PURE manifests, G1-G5 clean). It defines one immutable
`PrivateBacktestReplayPoint(pit_context, evaluation_date)`: at this knowledge frontier, under this PIT perspective, evaluate this economic date. The knowledge cutoff, the PIT perspective and the evaluation date are separate inputs.

## Three dimensions

- **Knowledge cutoff** (`pit_context.knowledge_cutoff`, exposed by identity; `knowledge_cutoff_utc` delegates to the closed context): what may be known.
- **PIT perspective** (`pit_context.mode`, the canonical `AsOfMode`: SOURCE_AS_OF or SYSTEM_AS_OF; no second enum, no fallback between modes).
- **Economic evaluation date** (`evaluation_date`, an exact `date`, never a `datetime`): explicit, not derived from the cutoff and not ordered against it. A source
  may announce a future-effective state before its effective date, so an evaluation date after the cutoff date is valid; ordering semantics belong to the
  downstream data contracts.

Any future execution or outcome time is not part of the replay point and is never inferred from the other three.

## Reuse

The `AnalysisPITContext` is stored by identity (exact type, no subclass, no reconstruction) and is the single temporal authority for the existing consumers:
the candidate-universe resolver, Game Changer event PIT binding, and (through `portfolio_recorded_cutoff`) ledger projection. Candidate membership comes from
PIT snapshots, never from today's universe (survivorship/lookahead defense).

## Portfolio recorded time

`portfolio_recorded_cutoff` is the original cutoff object. Portfolio history is always bounded by `PortfolioTransaction.recorded_at <= knowledge_cutoff`: a
reversal recorded after the cutoff does not change an earlier replay. SOURCE_AS_OF does not create a source-as-of portfolio ledger; the ledger has no public
source perspective.

## Not in A1

`MarketDataResolutionMode` mapping is intentionally NOT in A1 because `market_data/models.py` is outside the PURE dependency graph (and G3 is not weakened).
The canonical bridge (SOURCE_AS_OF to SOURCE_AS_OF, SYSTEM_AS_OF to SYSTEM_AS_OF, never CURRENT_REPORTED) will be a non-pure adapter in Phase26A2.
Also out of scope: replay sequences, input bundles, decision replay, simulated execution, performance and walk-forward.
