"""
backend/tests/test_portfolio_fee_tax_attribution_persistence.py
==============================================================
Comprehensive Unit, Invariant, Anti-Tamper, Codec Round-Trip, and Red-Team Tests
for Phase 14E (Fee/Tax Attribution Persistence Event Contract & Exact Codec).
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone, tzinfo, timedelta
from decimal import Decimal
import inspect
from typing import Any, Dict, List, Mapping, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import Portfolio, PortfolioAccount, PortfolioTransaction
from backend.engine.private.portfolio.fee_tax_attribution import (
    ResolvedFeeTaxAttribution,
)
import backend.engine.private.portfolio.fee_tax_attribution_persistence as persistence_module
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceError,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
    build_attribution_reversal_persistence_event,
    hydrate_fee_tax_attribution_persistence_event,
    serialize_fee_tax_attribution_persistence_event,
)


# ==============================================================================
# Helper Factories
# ==============================================================================

def _make_portfolio(mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO) -> Portfolio:
    return Portfolio(
        id=uuid4(),
        owner_id=uuid4(),
        name="Test Portfolio",
        mode=mode,
        base_currency=Currency.USD,
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    tx_type: TransactionType,
    *,
    tx_id: Optional[UUID] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    recorded_at: Optional[datetime] = None,
    reverses_transaction_id: Optional[UUID] = None,
) -> PortfolioTransaction:
    now = recorded_at or datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    if tx_type in (TransactionType.FEE, TransactionType.TAX_WITHHOLDING):
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=now,
            effective_date=now.date(),
            cash_amount=cash_amount or Decimal("10.00"),
            cash_currency=cash_currency or Currency.USD,
        )
    elif tx_type == TransactionType.BUY:
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            recorded_at=now,
            effective_date=now.date(),
            instrument_id=uuid4(),
            quantity=Decimal("10"),
            unit_price=Decimal("100.00"),
            trade_currency=Currency.USD,
        )
    elif tx_type == TransactionType.DIVIDEND:
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.DIVIDEND,
            recorded_at=now,
            effective_date=now.date(),
            instrument_id=uuid4(),
            cash_amount=cash_amount or Decimal("50.00"),
            cash_currency=cash_currency or Currency.USD,
        )
    elif tx_type == TransactionType.REVERSAL:
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.REVERSAL,
            recorded_at=now,
            effective_date=now.date(),
            reverses_transaction_id=reverses_transaction_id or uuid4(),
        )
    else:
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=now,
            effective_date=now.date(),
            cash_amount=cash_amount or Decimal("100.00"),
            cash_currency=cash_currency or Currency.USD,
        )


def _make_resolved_attribution(
    charge_type: TransactionType = TransactionType.FEE,
    target_type: TransactionType = TransactionType.BUY,
    allocated_amount: Decimal = Decimal("6.000"),
) -> ResolvedFeeTaxAttribution:
    portfolio = _make_portfolio()
    account_id = uuid4()
    charge = _make_tx(portfolio.id, account_id, charge_type, cash_amount=Decimal("10.000"))
    target = _make_tx(portfolio.id, account_id, target_type)
    return ResolvedFeeTaxAttribution(
        charge_transaction=charge,
        target_transaction=target,
        allocated_amount=allocated_amount,
    )


# ==============================================================================
# 1. Event Model Construction & Family Validation Tests
# ==============================================================================

def test_allocation_event_valid_construction() -> None:
    event_id = uuid4()
    p_id = uuid4()
    a_id = uuid4()
    c_id = uuid4()
    t_id = uuid4()
    recorded_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    amount = Decimal("6.000")

    event = FeeTaxAttributionPersistenceEvent(
        id=event_id,
        portfolio_id=p_id,
        account_id=a_id,
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        recorded_at=recorded_at,
        charge_transaction_id=c_id,
        target_transaction_id=t_id,
        allocated_amount=amount,
        reverses_attribution_event_id=None,
    )
    assert event.id is event_id
    assert event.portfolio_id is p_id
    assert event.account_id is a_id
    assert event.event_type == FeeTaxAttributionEventType.ALLOCATION
    assert event.recorded_at == recorded_at
    assert event.charge_transaction_id is c_id
    assert event.target_transaction_id is t_id
    assert event.allocated_amount is amount
    assert event.reverses_attribution_event_id is None


def test_reversal_event_valid_construction() -> None:
    event_id = uuid4()
    p_id = uuid4()
    a_id = uuid4()
    rev_target_id = uuid4()
    recorded_at = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    event = FeeTaxAttributionPersistenceEvent(
        id=event_id,
        portfolio_id=p_id,
        account_id=a_id,
        event_type=FeeTaxAttributionEventType.REVERSAL,
        recorded_at=recorded_at,
        charge_transaction_id=None,
        target_transaction_id=None,
        allocated_amount=None,
        reverses_attribution_event_id=rev_target_id,
    )
    assert event.id is event_id
    assert event.portfolio_id is p_id
    assert event.account_id is a_id
    assert event.event_type == FeeTaxAttributionEventType.REVERSAL
    assert event.recorded_at == recorded_at
    assert event.charge_transaction_id is None
    assert event.target_transaction_id is None
    assert event.allocated_amount is None
    assert event.reverses_attribution_event_id is rev_target_id


def test_event_is_frozen_immutable() -> None:
    event = FeeTaxAttributionPersistenceEvent(
        id=uuid4(),
        portfolio_id=uuid4(),
        account_id=uuid4(),
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        charge_transaction_id=uuid4(),
        target_transaction_id=uuid4(),
        allocated_amount=Decimal("5.00"),
        reverses_attribution_event_id=None,
    )
    with pytest.raises((FrozenInstanceError, AttributeError)):
        event.allocated_amount = Decimal("10.00")  # type: ignore[misc]


@pytest.mark.parametrize(
    "invalid_id",
    [None, True, False, "c8a1e8e2-6bf2-411a-8c76-2f08960824b2", 12345, 3.14],
)
def test_event_rejects_invalid_id(invalid_id: Any) -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="UUID"):
        FeeTaxAttributionPersistenceEvent(
            id=invalid_id,
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("5.00"),
        )


@pytest.mark.parametrize(
    "invalid_dt",
    [
        None,
        True,
        False,
        "2026-06-01T12:00:00Z",
        datetime(2026, 6, 1, 12, 0, 0),  # naive
        datetime(2026, 6, 1, 12, 0, 0).date(),  # date-only
    ],
)
def test_event_rejects_invalid_recorded_at(invalid_dt: Any) -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="recorded_at"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=invalid_dt,
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("5.00"),
        )


def test_event_rejects_fold_1() -> None:
    fold_1_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc, fold=1)
    with pytest.raises(FeeTaxAttributionPersistenceError, match="fold=0"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=fold_1_dt,
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("5.00"),
        )

    with pytest.raises(FeeTaxAttributionPersistenceError, match="fold=0"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=fold_1_dt,
            reverses_attribution_event_id=uuid4(),
        )


def test_allocation_builder_rejects_fold_1() -> None:
    attribution = _make_resolved_attribution()
    fold_1_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc, fold=1)
    with pytest.raises(FeeTaxAttributionPersistenceError, match="fold=0"):
        build_allocation_persistence_event(
            event_id=uuid4(),
            recorded_at=fold_1_dt,
            attribution=attribution,
        )


def test_reversal_builder_rejects_fold_1() -> None:
    fold_1_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc, fold=1)
    with pytest.raises(FeeTaxAttributionPersistenceError, match="fold=0"):
        build_attribution_reversal_persistence_event(
            event_id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            recorded_at=fold_1_dt,
            reverses_attribution_event_id=uuid4(),
        )


def test_event_rejects_raw_string_event_type() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="FeeTaxAttributionEventType"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type="allocation",  # type: ignore[arg-type]
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("5.00"),
        )


# ==============================================================================
# 2. Allocation Family Tamper & Invariant Tests (Items 50-53)
# ==============================================================================

def test_allocation_rejects_missing_charge_transaction_id() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="ALLOCATION event requires non-None charge_transaction_id"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=None,
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("5.00"),
        )


def test_allocation_rejects_missing_target_transaction_id() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="ALLOCATION event requires non-None target_transaction_id"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            target_transaction_id=None,
            allocated_amount=Decimal("5.00"),
        )


def test_allocation_rejects_missing_allocated_amount() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="ALLOCATION event requires non-None allocated_amount"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=None,
        )


def test_allocation_rejects_non_none_reversal_id() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="ALLOCATION event must have reverses_attribution_event_id=None"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("5.00"),
            reverses_attribution_event_id=uuid4(),
        )


def test_allocation_rejects_self_link() -> None:
    same_id = uuid4()
    with pytest.raises(FeeTaxAttributionPersistenceError, match="Self-attribution rejected"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=same_id,
            target_transaction_id=same_id,
            allocated_amount=Decimal("5.00"),
        )


@pytest.mark.parametrize(
    "bad_amt",
    [
        True,
        False,
        "5.00",
        5,
        5.0,
        Decimal("0"),
        Decimal("-1.00"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_allocation_rejects_invalid_amount_types_and_values(bad_amt: Any) -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="allocated_amount"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.ALLOCATION,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=bad_amt,
        )


# ==============================================================================
# 3. Reversal Family Tamper & Invariant Tests (Items 54-58)
# ==============================================================================

def test_reversal_rejects_populated_charge_id() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="REVERSAL event must have charge_transaction_id=None"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            charge_transaction_id=uuid4(),
            reverses_attribution_event_id=uuid4(),
        )


def test_reversal_rejects_populated_target_id() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="REVERSAL event must have target_transaction_id=None"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            target_transaction_id=uuid4(),
            reverses_attribution_event_id=uuid4(),
        )


def test_reversal_rejects_populated_allocated_amount() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="REVERSAL event must have allocated_amount=None"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            allocated_amount=Decimal("5.00"),
            reverses_attribution_event_id=uuid4(),
        )


def test_reversal_rejects_missing_reverses_attribution_event_id() -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="REVERSAL event requires non-None reverses_attribution_event_id"):
        FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            reverses_attribution_event_id=None,
        )


def test_reversal_rejects_self_reversal() -> None:
    same_id = uuid4()
    with pytest.raises(FeeTaxAttributionPersistenceError, match="Self-reversal rejected"):
        FeeTaxAttributionPersistenceEvent(
            id=same_id,
            portfolio_id=uuid4(),
            account_id=uuid4(),
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            reverses_attribution_event_id=same_id,
        )


# ==============================================================================
# 4. Pure Builder Functions Tests (Items 21-26, 59-61)
# ==============================================================================

def test_build_allocation_persistence_event_from_phase_14d_fee_to_buy() -> None:
    attribution = _make_resolved_attribution(
        charge_type=TransactionType.FEE,
        target_type=TransactionType.BUY,
        allocated_amount=Decimal("6.000"),
    )
    event_id = uuid4()
    recorded_at = datetime(2026, 6, 1, 14, 30, 0, tzinfo=timezone.utc)

    event = build_allocation_persistence_event(
        event_id=event_id,
        recorded_at=recorded_at,
        attribution=attribution,
    )

    assert event.id is event_id
    assert event.portfolio_id == attribution.charge_transaction.portfolio_id
    assert event.account_id == attribution.charge_transaction.account_id
    assert event.event_type == FeeTaxAttributionEventType.ALLOCATION
    assert event.recorded_at == recorded_at
    assert event.charge_transaction_id == attribution.charge_transaction.id
    assert event.target_transaction_id == attribution.target_transaction.id
    assert event.allocated_amount == Decimal("6.000")
    assert event.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()
    assert event.reverses_attribution_event_id is None


def test_build_allocation_persistence_event_from_tax_to_dividend() -> None:
    attribution = _make_resolved_attribution(
        charge_type=TransactionType.TAX_WITHHOLDING,
        target_type=TransactionType.DIVIDEND,
        allocated_amount=Decimal("7.500"),
    )
    event_id = uuid4()
    recorded_at = datetime(2026, 6, 1, 14, 30, 0, tzinfo=timezone.utc)

    event = build_allocation_persistence_event(
        event_id=event_id,
        recorded_at=recorded_at,
        attribution=attribution,
    )

    assert event.event_type == FeeTaxAttributionEventType.ALLOCATION
    assert event.charge_transaction_id == attribution.charge_transaction.id
    assert event.target_transaction_id == attribution.target_transaction.id
    assert event.allocated_amount == Decimal("7.500")


def test_build_allocation_builder_rejects_invalid_inputs() -> None:
    attribution = _make_resolved_attribution()
    valid_id = uuid4()
    valid_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(FeeTaxAttributionPersistenceError, match="event_id"):
        build_allocation_persistence_event(
            event_id="bad-uuid",  # type: ignore[arg-type]
            recorded_at=valid_dt,
            attribution=attribution,
        )

    with pytest.raises(FeeTaxAttributionPersistenceError, match="recorded_at"):
        build_allocation_persistence_event(
            event_id=valid_id,
            recorded_at=datetime(2026, 6, 1, 12, 0, 0),  # naive
            attribution=attribution,
        )

    with pytest.raises(FeeTaxAttributionPersistenceError, match="attribution"):
        build_allocation_persistence_event(
            event_id=valid_id,
            recorded_at=valid_dt,
            attribution={"duck": "type"},  # type: ignore[arg-type]
        )


def test_build_attribution_reversal_persistence_event() -> None:
    event_id = uuid4()
    p_id = uuid4()
    a_id = uuid4()
    target_event_id = uuid4()
    recorded_at = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    event = build_attribution_reversal_persistence_event(
        event_id=event_id,
        portfolio_id=p_id,
        account_id=a_id,
        recorded_at=recorded_at,
        reverses_attribution_event_id=target_event_id,
    )

    assert event.id is event_id
    assert event.portfolio_id is p_id
    assert event.account_id is a_id
    assert event.event_type == FeeTaxAttributionEventType.REVERSAL
    assert event.recorded_at == recorded_at
    assert event.charge_transaction_id is None
    assert event.target_transaction_id is None
    assert event.allocated_amount is None
    assert event.reverses_attribution_event_id is target_event_id


# ==============================================================================
# 5. Serializer Tests (Items 27-33)
# ==============================================================================

def test_serialize_allocation_event() -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution(allocated_amount=Decimal("6.000"))
    event_id = uuid4()
    recorded_at = datetime(2026, 6, 1, 14, 30, 0, tzinfo=timezone(timedelta(hours=3)))

    event = build_allocation_persistence_event(
        event_id=event_id,
        recorded_at=recorded_at,
        attribution=attribution,
    )

    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)

    expected_keys = {
        "id",
        "portfolio_id",
        "account_id",
        "owner_id",
        "event_type",
        "recorded_at",
        "charge_transaction_id",
        "target_transaction_id",
        "allocated_amount",
        "reverses_attribution_event_id",
    }
    assert set(row.keys()) == expected_keys
    assert row["id"] == str(event_id)
    assert row["portfolio_id"] == str(event.portfolio_id)
    assert row["account_id"] == str(event.account_id)
    assert row["owner_id"] == str(owner_id)
    assert row["event_type"] == "allocation"
    assert row["recorded_at"] == "2026-06-01T14:30:00+03:00"
    assert row["charge_transaction_id"] == str(event.charge_transaction_id)
    assert row["target_transaction_id"] == str(event.target_transaction_id)
    assert row["allocated_amount"] == "6.000"
    assert row["reverses_attribution_event_id"] is None


def test_serialize_reversal_event() -> None:
    owner_id = uuid4()
    event_id = uuid4()
    p_id = uuid4()
    a_id = uuid4()
    target_event_id = uuid4()
    recorded_at = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    event = build_attribution_reversal_persistence_event(
        event_id=event_id,
        portfolio_id=p_id,
        account_id=a_id,
        recorded_at=recorded_at,
        reverses_attribution_event_id=target_event_id,
    )

    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)

    assert row["event_type"] == "reversal"
    assert row["charge_transaction_id"] is None
    assert row["target_transaction_id"] is None
    assert row["allocated_amount"] is None
    assert row["reverses_attribution_event_id"] == str(target_event_id)


def test_serializer_rejects_invalid_inputs() -> None:
    event = FeeTaxAttributionPersistenceEvent(
        id=uuid4(),
        portfolio_id=uuid4(),
        account_id=uuid4(),
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        charge_transaction_id=uuid4(),
        target_transaction_id=uuid4(),
        allocated_amount=Decimal("5.00"),
    )

    with pytest.raises(FeeTaxAttributionPersistenceError, match="event"):
        serialize_fee_tax_attribution_persistence_event("not-an-event", uuid4())  # type: ignore[arg-type]

    with pytest.raises(FeeTaxAttributionPersistenceError, match="owner_id"):
        serialize_fee_tax_attribution_persistence_event(event, "not-a-uuid")  # type: ignore[arg-type]


# ==============================================================================
# 6. Hydrator Tests & Codec Round-Trip (Items 34-49)
# ==============================================================================

def test_allocation_round_trip_preserves_exact_representation() -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution(allocated_amount=Decimal("6.000"))
    event_id = uuid4()
    recorded_at = datetime(2026, 6, 1, 14, 30, 0, tzinfo=timezone(timedelta(hours=3)))

    original_event = build_allocation_persistence_event(
        event_id=event_id,
        recorded_at=recorded_at,
        attribution=attribution,
    )

    serialized = serialize_fee_tax_attribution_persistence_event(original_event, owner_id)
    hydrated = hydrate_fee_tax_attribution_persistence_event(serialized, expected_owner_id=owner_id)

    assert hydrated.id == original_event.id
    assert hydrated.portfolio_id == original_event.portfolio_id
    assert hydrated.account_id == original_event.account_id
    assert hydrated.event_type == original_event.event_type
    assert hydrated.recorded_at == original_event.recorded_at
    assert hydrated.charge_transaction_id == original_event.charge_transaction_id
    assert hydrated.target_transaction_id == original_event.target_transaction_id
    assert hydrated.reverses_attribution_event_id is None

    # Exact Decimal representation equality check
    assert hydrated.allocated_amount == original_event.allocated_amount
    assert hydrated.allocated_amount.as_tuple() == original_event.allocated_amount.as_tuple()
    assert hydrated.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()

    # Re-serialization exact equality check
    re_serialized = serialize_fee_tax_attribution_persistence_event(hydrated, owner_id)
    assert re_serialized == serialized


def test_reversal_round_trip() -> None:
    owner_id = uuid4()
    event_id = uuid4()
    p_id = uuid4()
    a_id = uuid4()
    target_event_id = uuid4()
    recorded_at = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    original_event = build_attribution_reversal_persistence_event(
        event_id=event_id,
        portfolio_id=p_id,
        account_id=a_id,
        recorded_at=recorded_at,
        reverses_attribution_event_id=target_event_id,
    )

    serialized = serialize_fee_tax_attribution_persistence_event(original_event, owner_id)
    hydrated = hydrate_fee_tax_attribution_persistence_event(serialized, expected_owner_id=owner_id)

    assert hydrated.id == original_event.id
    assert hydrated.event_type == FeeTaxAttributionEventType.REVERSAL
    assert hydrated.reverses_attribution_event_id == target_event_id
    assert hydrated.charge_transaction_id is None
    assert hydrated.target_transaction_id is None
    assert hydrated.allocated_amount is None


def test_hydrator_rejects_owner_mismatch() -> None:
    owner_a = uuid4()
    owner_b = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    serialized = serialize_fee_tax_attribution_persistence_event(event, owner_a)

    with pytest.raises(FeeTaxAttributionPersistenceError, match="does not match expected_owner_id"):
        hydrate_fee_tax_attribution_persistence_event(serialized, expected_owner_id=owner_b)


@pytest.mark.parametrize(
    "bad_row",
    [None, "string", b"bytes", [1, 2, 3], (1, 2), 123, True],
)
def test_hydrator_rejects_non_mapping_row(bad_row: Any) -> None:
    with pytest.raises(FeeTaxAttributionPersistenceError, match="Mapping"):
        hydrate_fee_tax_attribution_persistence_event(bad_row, expected_owner_id=uuid4())


def test_hydrator_rejects_missing_keys() -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    valid_row = serialize_fee_tax_attribution_persistence_event(event, owner_id)

    for key in valid_row.keys():
        corrupted = dict(valid_row)
        del corrupted[key]
        with pytest.raises(FeeTaxAttributionPersistenceError, match="missing required keys"):
            hydrate_fee_tax_attribution_persistence_event(corrupted, expected_owner_id=owner_id)


def test_hydrator_rejects_unexpected_extra_keys() -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    corrupted = dict(serialize_fee_tax_attribution_persistence_event(event, owner_id))
    corrupted["extra_field"] = "malicious_payload"

    with pytest.raises(FeeTaxAttributionPersistenceError, match="unexpected extra keys"):
        hydrate_fee_tax_attribution_persistence_event(corrupted, expected_owner_id=owner_id)


# ==============================================================================
# 7. Malformed Wire Input Matrix Tests (Items 63-65)
# ==============================================================================

@pytest.mark.parametrize(
    "malformed_uuid",
    [
        "C8A1E8E2-6BF2-411A-8C76-2F08960824B2",  # uppercase
        "{c8a1e8e2-6bf2-411a-8c76-2f08960824b2}",  # braces
        "urn:uuid:c8a1e8e2-6bf2-411a-8c76-2f08960824b2",  # urn
        "c8a1e8e26bf2411a8c762f08960824b2",  # compact 32-char
        " c8a1e8e2-6bf2-411a-8c76-2f08960824b2",  # leading space
        "c8a1e8e2-6bf2-411a-8c76-2f08960824b2 ",  # trailing space
        uuid4(),  # UUID object instead of string
        12345678,
        None,
    ],
)
def test_hydrator_rejects_malformed_uuid_in_row(malformed_uuid: Any) -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)
    row["id"] = malformed_uuid

    with pytest.raises(FeeTaxAttributionPersistenceError, match="(UUID|missing)"):
        hydrate_fee_tax_attribution_persistence_event(row, expected_owner_id=owner_id)


@pytest.mark.parametrize(
    "malformed_decimal",
    [
        "01.00",  # leading zero on integer part
        "+1",  # explicit plus
        "1.",  # trailing dot
        ".5",  # leading dot
        "1e3",  # scientific notation
        "1E3",
        "NaN",  # nan
        "Infinity",  # infinity
        "-1",  # negative (fails positive check in post_init)
        "0",  # zero (fails strictly positive check)
        "0.00",  # zero
        " 1.00",  # leading whitespace
        "1.00 ",  # trailing whitespace
        1.0,  # float
        10,  # int
        Decimal("6.000"),  # Decimal instance instead of str
        True,  # bool
    ],
)
def test_hydrator_rejects_malformed_decimal_in_row(malformed_decimal: Any) -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)
    row["allocated_amount"] = malformed_decimal

    with pytest.raises(FeeTaxAttributionPersistenceError):
        hydrate_fee_tax_attribution_persistence_event(row, expected_owner_id=owner_id)


@pytest.mark.parametrize(
    "canonical_dt_str",
    [
        "2026-08-29T12:00:00+03:00",
        "2026-08-29T09:00:00+00:00",
        "2026-08-29T12:00:00.123456+03:00",
        "2026-08-29T05:00:00-04:00",
    ],
)
def test_canonical_datetime_representations_round_trip(canonical_dt_str: str) -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime.fromisoformat(canonical_dt_str),
        attribution=attribution,
    )
    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)
    assert row["recorded_at"] == canonical_dt_str

    hydrated = hydrate_fee_tax_attribution_persistence_event(row, expected_owner_id=owner_id)
    assert hydrated.recorded_at.isoformat() == canonical_dt_str

    re_serialized = serialize_fee_tax_attribution_persistence_event(hydrated, owner_id)
    assert re_serialized["recorded_at"] == canonical_dt_str


@pytest.mark.parametrize(
    "malformed_dt",
    [
        "2026-08-29 12:00:00+03:00",  # space separator
        "2026-08-29T12:00+03:00",  # reduced time precision
        "2026-08-29T09:00:00Z",  # Z representation
        "20260829T120000+03:00",  # basic ISO
        "2026-06-01T12:00:00",  # naive ISO
        "2026-06-01",  # date-only
        "",  # empty
        "   ",  # whitespace
        "garbage",  # garbage
        datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),  # datetime object
        12345678,
    ],
)
def test_hydrator_rejects_malformed_datetime_in_row(malformed_dt: Any) -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)
    row["recorded_at"] = malformed_dt

    with pytest.raises(FeeTaxAttributionPersistenceError, match=r"(?i)(datetime|iso-8601)"):
        hydrate_fee_tax_attribution_persistence_event(row, expected_owner_id=owner_id)


@pytest.mark.parametrize(
    "malformed_event_type",
    [
        "ALLOCATION",  # uppercase
        "REVERSAL",  # uppercase
        " allocation",  # leading space
        "reversal ",  # trailing space
        "unknown",  # unknown
        "TRADE",
        123,
        True,
    ],
)
def test_hydrator_rejects_malformed_event_type_in_row(malformed_event_type: Any) -> None:
    owner_id = uuid4()
    attribution = _make_resolved_attribution()
    event = build_allocation_persistence_event(
        event_id=uuid4(),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        attribution=attribution,
    )
    row = serialize_fee_tax_attribution_persistence_event(event, owner_id)
    row["event_type"] = malformed_event_type

    with pytest.raises(FeeTaxAttributionPersistenceError, match="event_type"):
        hydrate_fee_tax_attribution_persistence_event(row, expected_owner_id=owner_id)


# ==============================================================================
# 8. Full Red-Team Scenario (Section 82)
# ==============================================================================

def test_final_red_team_scenario_section_82() -> None:
    """
    Section 82:
    1. Construct: FEE 10.000 USD -> BUY allocation 6.000
    2. Create persistence ALLOCATION event.
    3. Verify serialized row:
       event_type = 'allocation', allocated_amount = '6.000',
       charge_transaction_id = canonical FEE UUID string,
       target_transaction_id = canonical BUY UUID string,
       reverses_attribution_event_id = None
    4. Hydrate it back:
       Decimal.as_tuple() exact, recorded_at representation exact, UUID identities exact
    5. Construct reversal:
       REVERSAL -> references original attribution event ID
       charge_transaction_id = None, target_transaction_id = None, allocated_amount = None
    6. Try to encode independent economics into reversal -> Must reject.
    7. Try malformed persisted values (+6, 6e0, 06.000, UUID uppercase, naive recorded_at, unknown key) -> All reject.
    """
    owner_id = uuid4()
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    attribution = ResolvedFeeTaxAttribution(
        charge_transaction=fee_tx,
        target_transaction=buy_tx,
        allocated_amount=Decimal("6.000"),
    )
    alloc_event_id = uuid4()
    t1 = datetime(2026, 6, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    # Step 2: Create persistence ALLOCATION event
    alloc_event = build_allocation_persistence_event(
        event_id=alloc_event_id,
        recorded_at=t1,
        attribution=attribution,
    )

    # Step 3: Serialize
    row_alloc = serialize_fee_tax_attribution_persistence_event(alloc_event, owner_id)
    assert row_alloc["event_type"] == "allocation"
    assert row_alloc["allocated_amount"] == "6.000"
    assert row_alloc["charge_transaction_id"] == str(fee_tx.id)
    assert row_alloc["target_transaction_id"] == str(buy_tx.id)
    assert row_alloc["reverses_attribution_event_id"] is None

    # Step 4: Hydrate back
    hydrated_alloc = hydrate_fee_tax_attribution_persistence_event(row_alloc, expected_owner_id=owner_id)
    assert hydrated_alloc.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()
    assert hydrated_alloc.recorded_at == t1
    assert hydrated_alloc.id == alloc_event_id
    assert hydrated_alloc.charge_transaction_id == fee_tx.id
    assert hydrated_alloc.target_transaction_id == buy_tx.id

    # Step 5: Construct reversal referencing alloc_event_id
    rev_event_id = uuid4()
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
    rev_event = build_attribution_reversal_persistence_event(
        event_id=rev_event_id,
        portfolio_id=portfolio.id,
        account_id=account_id,
        recorded_at=t2,
        reverses_attribution_event_id=alloc_event_id,
    )
    assert rev_event.charge_transaction_id is None
    assert rev_event.target_transaction_id is None
    assert rev_event.allocated_amount is None
    assert rev_event.reverses_attribution_event_id == alloc_event_id

    # Step 6: Try to encode independent economics into reversal -> reject
    with pytest.raises(FeeTaxAttributionPersistenceError):
        FeeTaxAttributionPersistenceEvent(
            id=rev_event_id,
            portfolio_id=portfolio.id,
            account_id=account_id,
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=t2,
            charge_transaction_id=fee_tx.id,
            reverses_attribution_event_id=alloc_event_id,
        )

    with pytest.raises(FeeTaxAttributionPersistenceError):
        FeeTaxAttributionPersistenceEvent(
            id=rev_event_id,
            portfolio_id=portfolio.id,
            account_id=account_id,
            event_type=FeeTaxAttributionEventType.REVERSAL,
            recorded_at=t2,
            allocated_amount=Decimal("6.000"),
            reverses_attribution_event_id=alloc_event_id,
        )

    # Step 7: Malformed values rejected
    for bad_val in ["+6", "6e0", "06.000"]:
        bad_row = dict(row_alloc)
        bad_row["allocated_amount"] = bad_val
        with pytest.raises(FeeTaxAttributionPersistenceError):
            hydrate_fee_tax_attribution_persistence_event(bad_row, expected_owner_id=owner_id)

    bad_row_uuid = dict(row_alloc)
    bad_row_uuid["id"] = str(alloc_event_id).upper()
    with pytest.raises(FeeTaxAttributionPersistenceError):
        hydrate_fee_tax_attribution_persistence_event(bad_row_uuid, expected_owner_id=owner_id)

    bad_row_dt = dict(row_alloc)
    bad_row_dt["recorded_at"] = "2026-06-01T15:00:00"  # naive
    with pytest.raises(FeeTaxAttributionPersistenceError):
        hydrate_fee_tax_attribution_persistence_event(bad_row_dt, expected_owner_id=owner_id)

    bad_row_extra = dict(row_alloc)
    bad_row_extra["unknown_key"] = "test"
    with pytest.raises(FeeTaxAttributionPersistenceError):
        hydrate_fee_tax_attribution_persistence_event(bad_row_extra, expected_owner_id=owner_id)


# ==============================================================================
# 9. Static Purity & AST Isolation Tests
# ==============================================================================

def test_static_purity_ast_checks() -> None:
    source_path = inspect.getfile(persistence_module)
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()
        tree = ast.parse(content, filename=source_path)

    prohibited_names = {
        "now",
        "utcnow",
        "today",
        "uuid4",
        "uuid5",
        "hashlib",
        "sha256",
        "rpc",
        "table",
        "PortfolioRepository",
        "Supabase",
        "PostgREST",
        "float",
        "round",
        "quantize",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in prohibited_names, f"Prohibited identifier '{node.id}' found in {source_path}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in prohibited_names, f"Prohibited attribute access '{node.attr}' found in {source_path}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in prohibited_names, f"Prohibited call '{node.func.id}()' found in {source_path}"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in prohibited_names, f"Prohibited method call '.{node.func.attr}()' found in {source_path}"
