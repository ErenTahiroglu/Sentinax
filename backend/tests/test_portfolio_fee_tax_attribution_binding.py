"""
backend/tests/test_portfolio_fee_tax_attribution_binding.py
===========================================================
Tests for Phase 14J: Persisted Attribution to Authoritative Ledger Semantic Rebinding.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.fee_tax import (
    ObservedFeeTaxProjection,
    build_observed_fee_tax_projection,
)
from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionError,
    FeeTaxAttributionIntent,
    ObservedFeeTaxAttributionSet,
    ResolvedFeeTaxAttribution,
    build_observed_fee_tax_attribution_set,
)
from backend.engine.private.portfolio.fee_tax_attribution_binding import (
    FeeTaxAttributionBindingError,
    PersistedFeeTaxAttributionSemanticView,
    bind_persisted_fee_tax_attribution_history,
)
from backend.engine.private.portfolio.fee_tax_attribution_history import (
    PersistedFeeTaxAttributionHistoryView,
    build_persisted_fee_tax_attribution_history_view,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
)
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    build_ledger_projection_view,
)


def make_portfolio(
    portfolio_id: UUID | None = None,
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
) -> Portfolio:

    return Portfolio(
        id=portfolio_id or uuid4(),
        owner_id=uuid4(),
        mode=mode,
        name="Test Portfolio",
        base_currency=Currency.USD,
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    tx_type: TransactionType,
    tx_id: UUID | None = None,
    cash_amount: Decimal | None = None,
    cash_currency: Currency | None = None,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    effective_date: date = date(2026, 8, 29),
    recorded_at: datetime | None = None,
    reverses_transaction_id: UUID | None = None,
) -> PortfolioTransaction:
    is_trade = tx_type in (TransactionType.BUY, TransactionType.SELL)
    is_cash_tx = tx_type in (
        TransactionType.FEE,
        TransactionType.TAX_WITHHOLDING,
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.CASH_DEPOSIT,
        TransactionType.CASH_WITHDRAWAL,
    )
    return PortfolioTransaction(
        id=tx_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        transaction_type=tx_type,
        effective_date=effective_date,
        recorded_at=recorded_at or datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc),
        instrument_id="AAPL" if tx_type in (TransactionType.BUY, TransactionType.SELL, TransactionType.DIVIDEND) else None,
        quantity=quantity if is_trade else None,
        unit_price=unit_price if is_trade else None,
        trade_currency=Currency.USD if is_trade else None,
        cash_amount=cash_amount if is_cash_tx else None,
        cash_currency=(cash_currency or Currency.USD) if is_cash_tx else None,
        reverses_transaction_id=reverses_transaction_id,
    )




def make_allocation_event(
    portfolio_id: UUID,
    account_id: UUID,
    charge_tx_id: UUID,
    target_tx_id: UUID,
    allocated_amount: Decimal = Decimal("6.000"),
    event_id: UUID | None = None,
    recorded_at: datetime | None = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        recorded_at=recorded_at or datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc),
        charge_transaction_id=charge_tx_id,
        target_transaction_id=target_tx_id,
        allocated_amount=allocated_amount,
        reverses_attribution_event_id=None,
    )


def make_reversal_event(
    portfolio_id: UUID,
    account_id: UUID,
    reverses_event_id: UUID,
    event_id: UUID | None = None,
    recorded_at: datetime | None = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.REVERSAL,
        recorded_at=recorded_at or datetime(2026, 8, 29, 13, 0, 0, tzinfo=timezone.utc),
        charge_transaction_id=None,
        target_transaction_id=None,
        allocated_amount=None,
        reverses_attribution_event_id=reverses_event_id,
    )


class TestPersistedFeeTaxAttributionSemanticBinding:
    """Unit and domain tests for bind_persisted_fee_tax_attribution_history."""

    def test_empty_attribution_history(self):
        """Item 55: Ledger has FEE but persisted history has zero active attributions."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert semantic_view.portfolio_id == portfolio.id
        assert semantic_view.mode == portfolio.mode
        assert semantic_view.as_of_recorded_at is None
        assert semantic_view.ledger_view is ledger_view
        assert semantic_view.persisted_history is history_view
        assert len(semantic_view.observed_projection.events) == 1
        assert semantic_view.observed_projection.events[0] is fee_tx
        assert semantic_view.attribution_set.intents == ()
        assert semantic_view.attribution_set.attributions == ()

    def test_fee_to_buy_binding(self):
        """Item 56: Active FEE -> BUY allocation binds to exact authoritative ledger objects."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("10.000"), unit_price=Decimal("100.000"), cash_amount=Decimal("1000.000"))

        alloc_amount = Decimal("6.000")
        alloc_event = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=alloc_amount)

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc_event])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

        assert len(semantic_view.attribution_set.attributions) == 1
        attr = semantic_view.attribution_set.attributions[0]
        assert attr.charge_transaction is fee_tx
        assert attr.target_transaction is buy_tx
        assert attr.allocated_amount.as_tuple() == alloc_amount.as_tuple()
        assert attr.allocated_amount == alloc_amount

    def test_tax_withholding_to_dividend_binding(self):
        """Item 57: TAX_WITHHOLDING -> DIVIDEND binds correctly."""
        portfolio = make_portfolio()
        account_id = uuid4()
        tax_tx = make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("15.000"))
        div_tx = make_tx(portfolio.id, account_id, TransactionType.DIVIDEND, cash_amount=Decimal("100.000"))

        alloc_amount = Decimal("15.000")
        alloc_event = make_allocation_event(portfolio.id, account_id, tax_tx.id, div_tx.id, allocated_amount=alloc_amount)

        ledger_view = build_ledger_projection_view(portfolio, [tax_tx, div_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc_event])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert len(semantic_view.attribution_set.attributions) == 1
        attr = semantic_view.attribution_set.attributions[0]
        assert attr.charge_transaction is tax_tx
        assert attr.target_transaction is div_tx
        assert attr.allocated_amount.as_tuple() == alloc_amount.as_tuple()
        assert semantic_view.attribution_set.is_fully_allocated(tax_tx.id) is True

    def test_multi_target_binding_in_persisted_order(self):
        """Item 58: Multi-target FEE 10 -> BUY A (6) + SELL B (4) binds in persisted active order."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))
        sell_tx = make_tx(portfolio.id, account_id, TransactionType.SELL, quantity=Decimal("2"), unit_price=Decimal("10"), cash_amount=Decimal("20"))

        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)

        alloc1 = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=t1)
        alloc2 = make_allocation_event(portfolio.id, account_id, fee_tx.id, sell_tx.id, allocated_amount=Decimal("4.000"), recorded_at=t2)

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx, sell_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc1, alloc2])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert len(semantic_view.attribution_set.attributions) == 2
        assert semantic_view.attribution_set.attributions[0].target_transaction is buy_tx
        assert semantic_view.attribution_set.attributions[1].target_transaction is sell_tx
        assert semantic_view.attribution_set.is_fully_allocated(fee_tx.id) is True

    def test_partial_allocation_preserves_unallocated_remainder(self):
        """Item 59: Partial allocation leaves correct unallocated remainder in Phase 14D helper."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))

        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert semantic_view.attribution_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("4.000")
        assert semantic_view.attribution_set.is_fully_allocated(fee_tx.id) is False

    def test_reversed_attribution_excluded_from_semantic_set(self):
        """Item 60: Reversed attribution event is not bound to semantic set."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))

        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev = make_reversal_event(portfolio.id, account_id, reverses_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc, rev])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert semantic_view.attribution_set.attributions == ()
        assert semantic_view.attribution_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("10.000")

    def test_inactive_charge_rejected(self):
        """Item 61: Active persisted allocation referencing a reversed charge fails closed."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        fee_rev = make_tx(portfolio.id, account_id, TransactionType.REVERSAL, reverses_transaction_id=fee_tx.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=datetime(2026, 8, 29, 10, 30, 0, tzinfo=timezone.utc))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, fee_rev, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])

        with pytest.raises(FeeTaxAttributionBindingError, match="is not an active FEE or TAX_WITHHOLDING"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_inactive_target_rejected(self):
        """Item 62: Active persisted allocation referencing a reversed target fails closed."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        buy_rev = make_tx(portfolio.id, account_id, TransactionType.REVERSAL, reverses_transaction_id=buy_tx.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=datetime(2026, 8, 29, 10, 30, 0, tzinfo=timezone.utc))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx, buy_rev])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])

        with pytest.raises(FeeTaxAttributionBindingError, match="is not an active transaction at PIT"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_wrong_persisted_account_id_rejected(self):
        """Item 63: Persisted allocation carrying account B when charge/target are account A fails closed."""
        portfolio = make_portfolio()
        account_a = uuid4()
        account_b = uuid4()
        fee_tx = make_tx(portfolio.id, account_a, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_a, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))

        # event has account_b!
        alloc = make_allocation_event(portfolio.id, account_b, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])

        with pytest.raises(FeeTaxAttributionBindingError, match="does not match authoritative charge account_id"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_wrong_persisted_portfolio_id_rejected(self):
        """Item 64: Persisted history from different portfolio is rejected."""
        portfolio1 = make_portfolio()
        portfolio2 = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio1.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))

        ledger_view = build_ledger_projection_view(portfolio1, [fee_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio2.id, [])

        with pytest.raises(FeeTaxAttributionBindingError, match="portfolio_id mismatch"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_duplicate_active_pair_rejected(self):
        """Item 65: Corrupted history with two active allocations for same charge->target pair fails closed."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))

        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        alloc1 = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("3.000"), recorded_at=t1)
        alloc2 = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("3.000"), recorded_at=t2)

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc1, alloc2])

        with pytest.raises(FeeTaxAttributionError, match="Duplicate attribution intent detected"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_cumulative_over_allocation_rejected(self):
        """Item 66: Corrupted history with total allocations exceeding charge amount fails closed."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))
        sell_tx = make_tx(portfolio.id, account_id, TransactionType.SELL, quantity=Decimal("2"), unit_price=Decimal("10"), cash_amount=Decimal("20"))

        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        alloc1 = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=t1)
        alloc2 = make_allocation_event(portfolio.id, account_id, fee_tx.id, sell_tx.id, allocated_amount=Decimal("5.000"), recorded_at=t2)

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx, sell_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc1, alloc2])

        with pytest.raises(FeeTaxAttributionError, match="Over-allocation detected for charge"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_decimal_representation_preserved(self):
        """Item 67: Decimal representation is preserved identically across binding."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000000000000000000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("1"), unit_price=Decimal("10"), cash_amount=Decimal("10"))

        raw_amount = Decimal("6.123456789012345678")
        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=raw_amount)

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        attr = semantic_view.attribution_set.attributions[0]
        assert attr.allocated_amount.as_tuple() == raw_amount.as_tuple()

    def test_authoritative_object_identity_preserved(self):
        """Item 68: charge_transaction and target_transaction match authoritative objects by 'is'."""
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))

        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        attr = semantic_view.attribution_set.attributions[0]
        assert attr.charge_transaction is fee_tx
        assert attr.target_transaction is buy_tx

    def test_pit_exact_metadata_mismatch_rejected(self):
        """Item 70: Same physical instant but different timezone offset representations fail binding."""
        portfolio = make_portfolio()
        dt_utc = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        dt_plus3 = datetime.fromisoformat("2026-08-29T15:00:00+03:00")

        ledger_view = build_ledger_projection_view(portfolio, [], as_of_recorded_at=dt_utc)
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [], as_of_recorded_at=dt_plus3)

        with pytest.raises(FeeTaxAttributionBindingError, match="Exact as_of_recorded_at representation mismatch"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_pit_exact_metadata_match_accepted(self):
        """Item 71: Same representation passes metadata binding."""
        portfolio = make_portfolio()
        dt_plus3 = datetime.fromisoformat("2026-08-29T15:00:00+03:00")

        ledger_view = build_ledger_projection_view(portfolio, [], as_of_recorded_at=dt_plus3)
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [], as_of_recorded_at=dt_plus3)

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert semantic_view.as_of_recorded_at is dt_plus3

    def test_one_none_one_datetime_rejected(self):
        """Item 72: One None and one datetime fails closed."""
        portfolio = make_portfolio()
        dt_utc = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

        ledger_view = build_ledger_projection_view(portfolio, [], as_of_recorded_at=dt_utc)
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [], as_of_recorded_at=None)

        with pytest.raises(FeeTaxAttributionBindingError, match="Exact as_of_recorded_at representation mismatch"):
            bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

    def test_both_none_cutoff_accepted(self):
        """Item 73: Both None cutoff is valid."""
        portfolio = make_portfolio()
        ledger_view = build_ledger_projection_view(portfolio, [], as_of_recorded_at=None)
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [], as_of_recorded_at=None)

        semantic_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)
        assert semantic_view.as_of_recorded_at is None


class TestDirectConstructorHardening:
    """Item 74: Direct constructor tampering revalidation."""

    def test_wrong_portfolio_id_rejected(self):
        portfolio = make_portfolio()
        ledger_view = build_ledger_projection_view(portfolio, [])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [])
        valid_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

        with pytest.raises(FeeTaxAttributionBindingError, match="portfolio_id mismatch"):
            PersistedFeeTaxAttributionSemanticView(
                portfolio_id=uuid4(),  # Forged!
                mode=valid_view.mode,
                as_of_recorded_at=valid_view.as_of_recorded_at,
                ledger_view=valid_view.ledger_view,
                observed_projection=valid_view.observed_projection,
                persisted_history=valid_view.persisted_history,
                attribution_set=valid_view.attribution_set,
            )

    def test_wrong_mode_rejected(self):
        portfolio = make_portfolio(mode=PortfolioMode.MY_PORTFOLIO)

        ledger_view = build_ledger_projection_view(portfolio, [])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [])
        valid_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

        with pytest.raises(FeeTaxAttributionBindingError, match="mode mismatch"):
            PersistedFeeTaxAttributionSemanticView(
                portfolio_id=valid_view.portfolio_id,
                mode=PortfolioMode.SANDBOX,  # Forged!
                as_of_recorded_at=valid_view.as_of_recorded_at,
                ledger_view=valid_view.ledger_view,
                observed_projection=valid_view.observed_projection,
                persisted_history=valid_view.persisted_history,
                attribution_set=valid_view.attribution_set,
            )

    def test_tampered_attribution_set_rejected(self):
        portfolio = make_portfolio()
        account_id = uuid4()
        fee_tx = make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), cash_amount=Decimal("50"))

        alloc = make_allocation_event(portfolio.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"))

        ledger_view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
        history_view = build_persisted_fee_tax_attribution_history_view(portfolio.id, [alloc])
        valid_view = bind_persisted_fee_tax_attribution_history(ledger_view, history_view)

        # Forge empty attribution set when 1 was expected
        forged_attribution_set = build_observed_fee_tax_attribution_set(valid_view.observed_projection, ())

        with pytest.raises(FeeTaxAttributionBindingError, match="Tampered attribution_set attributions length"):
            PersistedFeeTaxAttributionSemanticView(
                portfolio_id=valid_view.portfolio_id,
                mode=valid_view.mode,
                as_of_recorded_at=valid_view.as_of_recorded_at,
                ledger_view=valid_view.ledger_view,
                observed_projection=valid_view.observed_projection,
                persisted_history=valid_view.persisted_history,
                attribution_set=forged_attribution_set,
            )


class TestStaticPurity:
    """Item 75: Verify production code contains zero prohibited patterns."""

    def test_no_prohibited_imports_or_calls(self):
        import backend.engine.private.portfolio.fee_tax_attribution_binding as mod
        source = inspect.getsource(mod)

        prohibited = [
            "datetime.now",
            "datetime.utcnow",
            "date.today",
            "uuid4",
            "uuid5",
            "hashlib",
            "sha256",
            ".rpc(",
            ".table(",
            "PortfolioRepository",
            "float(",
            "round(",
            "quantize(",
        ]
        for p in prohibited:
            assert p not in source, f"Found prohibited pattern '{p}' in fee_tax_attribution_binding.py"
