"""
backend/engine/private/portfolio/__init__.py
=============================================
Private Portfolio Accounting & Immutable Ledger Package.

Pure domain models, in-memory ledger services, and strict persistence codecs for the Personal Investment Decision Engine.
"""

from backend.engine.private.portfolio.ledger import (
    AppendResult,
    AppendStatus,
    PortfolioLedger,
    PortfolioLedgerValidator,
)
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
    PositionLot,
)
from backend.engine.private.portfolio.persistence import (
    hydrate_cash_bucket,
    hydrate_investment_goal,
    hydrate_planned_contribution,
    hydrate_portfolio,
    hydrate_portfolio_account,
    hydrate_portfolio_transaction,
    serialize_cash_bucket,
    serialize_investment_goal,
    serialize_planned_contribution,
    serialize_portfolio,
    serialize_portfolio_account,
    serialize_portfolio_transaction,
)

__all__ = [
    "Portfolio",
    "PortfolioAccount",
    "PortfolioTransaction",
    "CashBucket",
    "InvestmentGoal",
    "PlannedContribution",
    "PositionLot",
    "PortfolioLedger",
    "PortfolioLedgerValidator",
    "AppendResult",
    "AppendStatus",
    "serialize_portfolio",
    "hydrate_portfolio",
    "serialize_portfolio_account",
    "hydrate_portfolio_account",
    "serialize_cash_bucket",
    "hydrate_cash_bucket",
    "serialize_investment_goal",
    "hydrate_investment_goal",
    "serialize_planned_contribution",
    "hydrate_planned_contribution",
    "serialize_portfolio_transaction",
    "hydrate_portfolio_transaction",
]
