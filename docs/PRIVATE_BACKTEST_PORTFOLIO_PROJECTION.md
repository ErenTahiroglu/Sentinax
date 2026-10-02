# Private Backtest Portfolio Projection (Phase 26C2B1)

Module: `backend/engine/private/backtest_portfolio_projection.py` (deliberately not in the PURE manifest: `portfolio.models` and `portfolio.projection` are outside
the PURE graph; this permits no I/O).

## What it does

`PrivateBacktestPortfolioProjectionBinding(analysis_context, portfolio, transactions)` stores `analysis_context`, `transactions` and `projection`. The projection is
derived internally by the closed `build_ledger_projection_view(portfolio, transactions, as_of_recorded_at=replay_point.portfolio_recorded_cutoff)`: replay
cutoff -> recorded_at cutoff. The full supplied tuple is passed (never prefiltered); the closed builder owns filtering, reversal logic, ordering and corruption
checks, and its `PortfolioProjectionError` propagates unchanged. The stored projection keeps the caller's cutoff representation (for example `+03:00`).

## No injected projection

The constructor and builder accept no `projection` (or `portfolio_id`, `mode`, `cutoff`, `owner_id`) argument: a caller cannot inject a `LedgerProjectionView`, so a
forged projection can never become historical authority. Portfolio id and mode come solely from the validated `Portfolio` through the closed builder.

## Time

economic time != knowledge time. Knowledge is `recorded_at <= cutoff`. `effective_date` and the replay `evaluation_date` never gate knowledge: a transaction with a
future effective date recorded before the cutoff is known; an old effective date recorded after the cutoff is unknown until recorded. Future reversals do not
retroactively affect an earlier replay (future reversals stay unknown): a BUY recorded at T0 stays active at a T1 cutoff while its reversal was recorded at T2, and is reversed at or after T2.

## Mutability

`Portfolio` is mutable: it is re-validated (`validate()`) before use, used only during construction and never retained, so later mutation of the caller's object cannot
alter the stored projection. `PortfolioTransaction` events are frozen and the exact supplied tuple is retained by identity (no copy, sort or dedup).

## Claim limit

C2B1 does NOT prove that the caller-supplied transaction tuple is complete relative to persistent storage. A subset and the full tuple can each yield a valid
canonical projection if internally consistent. Coverage/provenance of the history would be C2B2, if required.

No repository, accounting, positions, cash, PnL, market data, clock, hash, randomness, loop or decision. Remote CI does not run the Phase 26 tests yet.
