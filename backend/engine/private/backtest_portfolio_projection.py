"""
backend/engine/private/backtest_portfolio_projection.py
=======================================================
Canonical portfolio projection at the replay knowledge cutoff (Phase 26C2B1). The binding derives the reversal-aware `LedgerProjectionView` internally with the
CLOSED `build_ledger_projection_view`, passing the supplied Portfolio, the full supplied transaction tuple (never prefiltered) and exactly
`analysis_context.replay_point.portfolio_recorded_cutoff` as `as_of_recorded_at`. No caller can inject a projection: the constructor and builder accept no
`projection` argument, so a directly forged `LedgerProjectionView` can never become backtest authority.

Time: the portfolio history is bounded by `PortfolioTransaction.recorded_at <= knowledge cutoff` (economic time is not knowledge time: effective_date and the
replay evaluation_date never gate knowledge), so a transaction or reversal recorded after the cutoff cannot leak backward into an earlier replay. The stored
projection keeps the caller's cutoff representation.

Types and mutability: exact `Portfolio` (re-validated with `validate()` because it is mutable, then used only during construction and never retained; its id and
mode live on in the projection) and an exact tuple of exact frozen `PortfolioTransaction` events, retained by identity (not copied, sorted or deduplicated).
Corruption errors of the closed builder (`PortfolioProjectionError`) propagate unchanged.

Claim limit: this proves the canonical projection of the SUPPLIED portfolio and transaction tuple at the cutoff. It does NOT prove that the supplied tuple is
complete relative to persistent storage (a coverage and provenance claim, a later checkpoint). Non-pure by classification only: no repository, accounting,
market data, clock, hash, randomness, I/O, loop or decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.projection import LedgerProjectionView, build_ledger_projection_view

_ERR_CONTEXT = "analysis_context must be an exact PrivateBacktestAnalysisContext instance"
_ERR_PORTFOLIO = "portfolio must be an exact Portfolio instance"
_ERR_TUPLE = "transactions must be an exact tuple"
_ERR_MEMBER = "every transaction must be an exact PortfolioTransaction instance"


@dataclass(frozen=True, init=False)
class PrivateBacktestPortfolioProjectionBinding:
    """The replay context, the supplied transaction events and the projection derived from them at the replay recorded-time cutoff."""
    analysis_context: PrivateBacktestAnalysisContext
    transactions: tuple[PortfolioTransaction, ...]
    projection: LedgerProjectionView

    def __init__(self, *, analysis_context: PrivateBacktestAnalysisContext, portfolio: Portfolio, transactions: tuple[PortfolioTransaction, ...]) -> None:
        if type(analysis_context) is not PrivateBacktestAnalysisContext:
            raise TypeError(_ERR_CONTEXT)
        if type(portfolio) is not Portfolio:
            raise TypeError(_ERR_PORTFOLIO)
        if type(transactions) is not tuple:
            raise TypeError(_ERR_TUPLE)
        if not all(type(transaction) is PortfolioTransaction for transaction in transactions):
            raise TypeError(_ERR_MEMBER)
        portfolio.validate()
        projection = build_ledger_projection_view(portfolio, transactions, as_of_recorded_at=analysis_context.replay_point.portfolio_recorded_cutoff)
        object.__setattr__(self, "analysis_context", analysis_context)
        object.__setattr__(self, "transactions", transactions)
        object.__setattr__(self, "projection", projection)


def build_private_backtest_portfolio_projection(*, analysis_context: PrivateBacktestAnalysisContext, portfolio: Portfolio,
                                                transactions: tuple[PortfolioTransaction, ...]) -> PrivateBacktestPortfolioProjectionBinding:
    """Derive the canonical replay projection of the supplied portfolio history; there is no projection parameter."""
    return PrivateBacktestPortfolioProjectionBinding(analysis_context=analysis_context, portfolio=portfolio, transactions=transactions)
