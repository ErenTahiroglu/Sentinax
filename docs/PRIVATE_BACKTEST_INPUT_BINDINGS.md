# Private Backtest Input Bindings (Phase 26C2A)

Module: `backend/engine/private/backtest_input_bindings.py` (deliberately not in the PURE manifest: it composes several closed domain subgraphs; this permits no
I/O). It binds already-resolved, self-validating historical inputs to one Phase 26C1 `PrivateBacktestAnalysisContext`.

## What C2A proves

The input belongs to exactly one replay frontier/context. Each binding stores the exact analysis context and the exact input by identity (no copy, no
rebuilding) and enforces:

- **candidate universe**: `resolution.query.pit_context` is the analysis PIT object (object identity; an equal-valued copy is rejected) and
  `query.evaluation_date == replay evaluation_date`.
- **macro**: `fact.mode` is the PIT mode (no SOURCE/SYSTEM conversion) and `fact.as_of` is exactly the replay knowledge-cutoff UTC instant (exact as_of: equal
  instants with another offset are valid; earlier or later, even by one microsecond, is rejected). The fact's effective date is not ordered against anything.
- **Game Changer**: the revision family's assessment PIT context is the analysis PIT object (the closed family validator makes it one object for the family).
- **risk evidence**: the risk context derived from the missing or digest-matched branch carries the exact analysis temporal context object (this binds the
  horizon/evaluation date and the PIT frontier at once).

## What it does not prove

Not completeness, decision readiness, source correctness, sufficiency or any portfolio action. Missing stays missing: candidate statuses (SELECTED,
NO_SNAPSHOT_AS_OF, NO_SOURCE_SNAPSHOT, NO_EFFECTIVE_SNAPSHOT, FRONTIER_CONFLICT), an UNAVAILABLE macro fact (value None, never zero), INCOMPLETE_COVERAGE (no
active assessment is invented) and `MissingRiskEvidence` (never zero, neutral or default) are all valid bound inputs. There is no cross-category relation and
no completeness enum (C2E owns requirement policy). No raw risk content is stored and nothing is re-hashed.

## Deferred on purpose

- portfolio (C2B): a bare `LedgerProjectionView` cannot prove canonical builder provenance.
- market data (C2C): `MarketObservationResolutionResult` is mutable and not self-revalidating.
- user views (C2D): the current user-view types carry no explicit historical availability authority.
- requirement/completeness bundle (C2E): now the explicit requirement manifest boundary in `docs/PRIVATE_BACKTEST_INPUT_COMPLETENESS.md`.

No fetching, resolver, repository, clock, hash, randomness, loop or decision. Remote CI does not run the Phase 26 tests yet.
