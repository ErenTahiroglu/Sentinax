"""
backend/engine/private/portfolio/postgrest_transport.py
======================================================
Exact Supabase / PostgREST Numeric Transport Contract for Portfolio Persistence.

Architecture & Invariants:
    1. PostgREST JSON Read Boundary:
       - Default PostgREST JSON responses parse PostgreSQL NUMERIC columns as JSON numbers.
       - Python JSON parsers (including PostgREST JSONAdapter) deserialize JSON numbers into
         Python `float` or `int`, losing exact Decimal precision (e.g. 1.2345678901234567e+19).
       - The canonical domain persistence codecs in `persistence.py` strictly REJECT `float`
         and `int` to protect financial correctness.
       - To preserve lossless decimal transport, all reads MUST use PostgREST vertical-select
         casts (`column::text`).
       - Wildcard selects (`*`) are strictly FORBIDDEN on tables containing financial NUMERIC columns.

    2. Seven Canonical Financial NUMERIC Columns:
       - public.investment_goals:
           * target_amount::text
       - public.planned_contributions:
           * amount::text
       - public.portfolio_transactions:
           * quantity::text
           * unit_price::text
           * cash_amount::text
           * from_amount::text
           * to_amount::text

    3. PostgREST JSON Write Boundary:
       - Outbound writes from Phase 12B.2A serializers (`serialize_*`) format Decimals as
         exact fixed-point string representations (e.g. "12345678901234567890.123456789").
       - PostgreSQL's JSON record conversion feeds JSON string contents to NUMERIC input
         conversion function, providing a lossless write path into PostgreSQL NUMERIC columns.
       - Outbound financial values MUST NEVER be converted to Python float/int.
"""

from typing import Final, FrozenSet, Mapping

# ─────────────────────────────────────────────────────────────────────────────
# 1. Explicit PostgREST Select Projection Contracts
# ─────────────────────────────────────────────────────────────────────────────

PORTFOLIO_SELECT: Final[str] = (
    "id,"
    "owner_id,"
    "mode,"
    "name,"
    "base_currency,"
    "created_at,"
    "archived_at,"
    "source_portfolio_id,"
    "source_snapshot_time"
)

PORTFOLIO_ACCOUNT_SELECT: Final[str] = (
    "id,"
    "portfolio_id,"
    "owner_id,"
    "name,"
    "base_currency,"
    "broker_label,"
    "created_at,"
    "archived_at"
)

CASH_BUCKET_SELECT: Final[str] = (
    "id,"
    "portfolio_id,"
    "owner_id,"
    "account_id,"
    "name,"
    "currency,"
    "purpose,"
    "included_in_investable_assets,"
    "created_at,"
    "archived_at"
)

INVESTMENT_GOAL_SELECT: Final[str] = (
    "id,"
    "portfolio_id,"
    "owner_id,"
    "name,"
    "target_amount::text,"
    "target_currency,"
    "target_date,"
    "priority,"
    "status,"
    "created_at,"
    "archived_at"
)

PLANNED_CONTRIBUTION_SELECT: Final[str] = (
    "id,"
    "portfolio_id,"
    "owner_id,"
    "goal_id,"
    "cash_bucket_id,"
    "expected_date,"
    "amount::text,"
    "currency,"
    "status,"
    "created_at"
)

PORTFOLIO_TRANSACTION_SELECT: Final[str] = (
    "id,"
    "portfolio_id,"
    "account_id,"
    "owner_id,"
    "transaction_type,"
    "effective_date,"
    "executed_at,"
    "recorded_at,"
    "instrument_id,"
    "quantity::text,"
    "unit_price::text,"
    "trade_currency,"
    "cash_amount::text,"
    "cash_currency,"
    "cash_bucket_id,"
    "from_currency,"
    "from_amount::text,"
    "to_currency,"
    "to_amount::text,"
    "external_source,"
    "external_reference,"
    "reverses_transaction_id,"
    "notes,"
    "economic_fingerprint"
)

FEE_TAX_ATTRIBUTION_EVENT_SELECT: Final[str] = (
    "id,"
    "portfolio_id,"
    "account_id,"
    "owner_id,"
    "event_type,"
    "recorded_at,"
    "charge_transaction_id,"
    "target_transaction_id,"
    "allocated_amount::text,"
    "reverses_attribution_event_id"
)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Canonical Financial NUMERIC Columns Whitelist
# ─────────────────────────────────────────────────────────────────────────────

FINANCIAL_NUMERIC_COLUMNS_BY_TABLE: Final[Mapping[str, FrozenSet[str]]] = {
    "portfolios": frozenset(),
    "portfolio_accounts": frozenset(),
    "cash_buckets": frozenset(),
    "investment_goals": frozenset({"target_amount"}),
    "planned_contributions": frozenset({"amount"}),
    "portfolio_transactions": frozenset({
        "quantity",
        "unit_price",
        "cash_amount",
        "from_amount",
        "to_amount",
    }),
    "portfolio_fee_tax_attribution_events": frozenset({
        "allocated_amount",
    }),
}

ALL_SEVEN_FINANCIAL_NUMERIC_COLUMNS: Final[FrozenSet[str]] = frozenset({
    "target_amount",
    "amount",
    "quantity",
    "unit_price",
    "cash_amount",
    "from_amount",
    "to_amount",
})

