"""
backend/tests/test_portfolio_fee_tax_attribution_service.py
===========================================================
Comprehensive test suite for Owner-Bound Persisted Attribution Semantic Query Service (Phase 14K).

Tests:
1. Constructor & Dependency Validation (Repository, Clock)
2. Parameter Validation (Portfolio ID, as_of_recorded_at)
3. Current Clock Resolution (UTC normalization, single call, fail-closed on invalid)
4. As-Of Exact Representation Binding (+03:00 preservation, zero clock calls)
5. Repository Response Integrity (Missing portfolio, Wrong type, ID mismatch, Invalid collections)
6. Domain Scenarios (Empty history, FEE->BUY, TAX->DIVIDEND, Attribution reversal, PIT cutoffs)
7. Fail-Closed Error Propagation (Repository exceptions, Lower-layer binding/history errors)
8. Static Purity & Architectural Invariants (No owner args, single lookup, no calculation duplication)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import inspect
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionError,
    FeeTaxAttributionIntent,
    ObservedFeeTaxAttributionSet,
    ResolvedFeeTaxAttribution,
)
from backend.engine.private.portfolio.fee_tax_attribution_binding import (
    FeeTaxAttributionBindingError,
    PersistedFeeTaxAttributionSemanticView,
)
from backend.engine.private.portfolio.fee_tax_attribution_history import (
    FeeTaxAttributionHistoryError,
    PersistedFeeTaxAttributionHistoryView,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
    build_attribution_reversal_persistence_event,
)
from backend.engine.private.portfolio.fee_tax_attribution_service import (
    PortfolioFeeTaxAttributionQueryError,
    PortfolioFeeTaxAttributionQueryService,
    PortfolioFeeTaxAttributionRepositoryPort,
)
import backend.engine.private.portfolio.fee_tax_attribution_service as service_mod
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction


class NullOffsetTZ(tzinfo):
    """Custom tzinfo implementation returning None for utcoffset (non-None tzinfo but not aware)."""
    def utcoffset(self, dt: Optional[datetime]) -> Optional[timedelta]:
        return None

    def dst(self, dt: Optional[datetime]) -> Optional[timedelta]:
        return None

    def tzname(self, dt: Optional[datetime]) -> str:
        return "NULL"


# ─────────────────────────────────────────────────────────────────────────────
# Test Repository Double
# ─────────────────────────────────────────────────────────────────────────────

class StrictTestAttributionRepository:
    """
    Strict repository test double exposing ONLY get_portfolio, list_transactions,
    and list_fee_tax_attribution_events.
    """

    def __init__(
        self,
        portfolios: Optional[Dict[UUID, Portfolio]] = None,
        transactions: Optional[Dict[UUID, List[PortfolioTransaction]]] = None,
        attribution_events: Optional[Dict[UUID, List[FeeTaxAttributionPersistenceEvent]]] = None,
        get_portfolio_error: Optional[Exception] = None,
        list_transactions_error: Optional[Exception] = None,
        list_events_error: Optional[Exception] = None,
    ) -> None:
        self._portfolios: Dict[UUID, Portfolio] = portfolios or {}
        self._transactions: Dict[UUID, List[PortfolioTransaction]] = transactions or {}
        self._attribution_events: Dict[UUID, List[FeeTaxAttributionPersistenceEvent]] = attribution_events or {}
        self._get_portfolio_error: Optional[Exception] = get_portfolio_error
        self._list_transactions_error: Optional[Exception] = list_transactions_error
        self._list_events_error: Optional[Exception] = list_events_error

        self.get_portfolio_calls: List[Any] = []
        self.list_transactions_calls: List[Any] = []
        self.list_events_calls: List[Tuple[UUID, Optional[UUID], Optional[datetime]]] = []

    def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
        self.get_portfolio_calls.append(portfolio_id)
        if self._get_portfolio_error is not None:
            raise self._get_portfolio_error
        return self._portfolios.get(portfolio_id)

    def list_transactions(self, portfolio_id: UUID) -> List[PortfolioTransaction]:
        self.list_transactions_calls.append(portfolio_id)
        if self._list_transactions_error is not None:
            raise self._list_transactions_error
        return list(self._transactions.get(portfolio_id, []))

    def list_fee_tax_attribution_events(
        self,
        portfolio_id: UUID,
        account_id: Optional[UUID] = None,
        as_of_recorded_at: Optional[datetime] = None,
    ) -> List[FeeTaxAttributionPersistenceEvent]:
        self.list_events_calls.append((portfolio_id, account_id, as_of_recorded_at))
        if self._list_events_error is not None:
            raise self._list_events_error
        return list(self._attribution_events.get(portfolio_id, []))


# ─────────────────────────────────────────────────────────────────────────────
# Helper Factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_portfolio(
    portfolio_id: Optional[UUID] = None,
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
) -> Portfolio:
    return Portfolio(
        id=portfolio_id or uuid4(),
        owner_id=uuid4(),
        name="Test Portfolio",
        base_currency=Currency.USD,
        mode=mode,
        created_at=datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    tx_type: TransactionType,
    tx_id: Optional[UUID] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    trade_currency: Optional[Currency] = None,
    effective_date: date = date(2026, 8, 29),
    recorded_at: Optional[datetime] = None,
    reverses_transaction_id: Optional[UUID] = None,
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
    is_reversal = tx_type == TransactionType.REVERSAL
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
        trade_currency=(trade_currency or Currency.USD) if is_trade else None,
        cash_amount=cash_amount if is_cash_tx else None,
        cash_currency=(cash_currency or Currency.USD) if is_cash_tx else None,
        reverses_transaction_id=reverses_transaction_id if is_reversal else None,
    )



def _make_allocation_event(
    portfolio_id: UUID,
    account_id: UUID,
    charge_id: UUID,
    target_id: UUID,
    allocated_amount: Decimal = Decimal("5.000"),
    event_id: Optional[UUID] = None,
    recorded_at: Optional[datetime] = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        charge_transaction_id=charge_id,
        target_transaction_id=target_id,
        allocated_amount=allocated_amount,
        reverses_attribution_event_id=None,
        recorded_at=recorded_at or datetime(2026, 8, 29, 12, 30, 0, tzinfo=timezone.utc),
    )



# ─────────────────────────────────────────────────────────────────────────────
# Test Classes
# ─────────────────────────────────────────────────────────────────────────────

class TestConstructorAndDependencyValidation:
    """Items 36, 37: Constructor validation for repository and clock dependencies."""

    def test_rejects_none_repository(self):
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="repository must not be None"):
            PortfolioFeeTaxAttributionQueryService(repository=None)  # type: ignore

    def test_rejects_missing_get_portfolio(self):
        class MissingGetPortfolio:
            def list_transactions(self, p_id): return []
            def list_fee_tax_attribution_events(self, p_id, a_id=None, as_of=None): return []

        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="callable get_portfolio"):
            PortfolioFeeTaxAttributionQueryService(repository=MissingGetPortfolio())  # type: ignore

    def test_rejects_missing_list_transactions(self):
        class MissingListTransactions:
            def get_portfolio(self, p_id): return None
            def list_fee_tax_attribution_events(self, p_id, a_id=None, as_of=None): return []

        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="callable list_transactions"):
            PortfolioFeeTaxAttributionQueryService(repository=MissingListTransactions())  # type: ignore

    def test_rejects_missing_list_fee_tax_attribution_events(self):
        class MissingListEvents:
            def get_portfolio(self, p_id): return None
            def list_transactions(self, p_id): return []

        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="callable list_fee_tax_attribution_events"):
            PortfolioFeeTaxAttributionQueryService(repository=MissingListEvents())  # type: ignore

    def test_rejects_non_callable_method_attributes(self):
        class NonCallableRepo:
            get_portfolio = "not a method"
            list_transactions = 123
            list_fee_tax_attribution_events = None

        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="callable get_portfolio"):
            PortfolioFeeTaxAttributionQueryService(repository=NonCallableRepo())  # type: ignore

    @pytest.mark.parametrize("bad_clock", [True, False, 123, "clock", []])
    def test_rejects_invalid_clock_dependency(self, bad_clock: Any):
        repo = StrictTestAttributionRepository()
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="clock must be a callable"):
            PortfolioFeeTaxAttributionQueryService(repository=repo, clock=bad_clock)

    def test_valid_construction(self):
        repo = StrictTestAttributionRepository()
        service = PortfolioFeeTaxAttributionQueryService(repository=repo)
        assert service._repository is repo


class TestParameterValidation:
    """Items 8, 43: Portfolio ID and datetime parameter validations."""

    @pytest.mark.parametrize("bad_id", [None, True, False, "550e8400-e29b-41d4-a716-446655440000", 123, b"123"])
    def test_rejects_invalid_portfolio_id_in_current_query(self, bad_id: Any):
        service = PortfolioFeeTaxAttributionQueryService(StrictTestAttributionRepository())
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="portfolio_id must be a UUID"):
            service.get_current_attribution_view(bad_id)

    @pytest.mark.parametrize("bad_id", [None, True, False, "550e8400-e29b-41d4-a716-446655440000", 123, b"123"])
    def test_rejects_invalid_portfolio_id_in_as_of_query(self, bad_id: Any):
        service = PortfolioFeeTaxAttributionQueryService(StrictTestAttributionRepository())
        dt = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="portfolio_id must be a UUID"):
            service.get_attribution_view_as_of(bad_id, dt)

    @pytest.mark.parametrize("bad_dt", [
        None,
        True,
        False,
        "2026-08-29T12:00:00Z",
        1234567890,
        datetime(2026, 8, 29, 12, 0, 0),  # naive
        datetime(2026, 8, 29, 12, 0, 0, tzinfo=NullOffsetTZ()),  # non-null tzinfo but null utcoffset
    ])
    def test_rejects_invalid_as_of_recorded_at(self, bad_dt: Any):
        service = PortfolioFeeTaxAttributionQueryService(StrictTestAttributionRepository())
        with pytest.raises(PortfolioFeeTaxAttributionQueryError):
            service.get_attribution_view_as_of(uuid4(), bad_dt)


class TestCurrentClockResolution:
    """Items 38, 39, 40: Current clock invocation, UTC normalization, and single-call enforcement."""

    def test_current_query_invokes_clock_exactly_once(self):
        p = _make_portfolio()
        repo = StrictTestAttributionRepository(portfolios={p.id: p})

        call_count = 0
        fixed_dt = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))

        def counting_clock() -> datetime:
            nonlocal call_count
            call_count += 1
            return fixed_dt

        service = PortfolioFeeTaxAttributionQueryService(repo, clock=counting_clock)
        view = service.get_current_attribution_view(p.id)

        assert call_count == 1
        expected_utc = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        assert view.as_of_recorded_at == expected_utc
        assert view.ledger_view.as_of_recorded_at == expected_utc
        assert view.persisted_history.as_of_recorded_at == expected_utc

    @pytest.mark.parametrize("bad_clock_return", [
        None,
        True,
        False,
        "2026-08-29T12:00:00Z",
        datetime(2026, 8, 29, 12, 0, 0),  # naive
        datetime(2026, 8, 29, 12, 0, 0, tzinfo=NullOffsetTZ()),
    ])
    def test_rejects_invalid_clock_return_value(self, bad_clock_return: Any):
        p = _make_portfolio()
        repo = StrictTestAttributionRepository(portfolios={p.id: p})
        service = PortfolioFeeTaxAttributionQueryService(repo, clock=lambda: bad_clock_return)

        with pytest.raises(PortfolioFeeTaxAttributionQueryError):
            service.get_current_attribution_view(p.id)

        assert len(repo.get_portfolio_calls) == 0


class TestAsOfQuerySemantics:
    """Items 41, 42: As-of query does not call clock and preserves exact datetime representation."""

    def test_as_of_does_not_call_clock(self):
        p = _make_portfolio()
        repo = StrictTestAttributionRepository(portfolios={p.id: p})

        def exploding_clock():
            raise AssertionError("Clock must not be called in as-of query!")

        service = PortfolioFeeTaxAttributionQueryService(repo, clock=exploding_clock)
        dt = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        view = service.get_attribution_view_as_of(p.id, dt)

        assert view.as_of_recorded_at == dt
        assert view.as_of_recorded_at.tzinfo == dt.tzinfo
        assert view.ledger_view.as_of_recorded_at == dt
        assert view.persisted_history.as_of_recorded_at == dt


class TestRepositoryResponseIntegrity:
    """Items 44, 45, 46, 47, 48, 49, 50, 61: Repository response contracts and single-call enforcement."""

    def test_missing_portfolio_raises_and_stops(self):
        repo = StrictTestAttributionRepository()
        service = PortfolioFeeTaxAttributionQueryService(repo)

        missing_id = uuid4()
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="does not exist"):
            service.get_current_attribution_view(missing_id)

        assert repo.get_portfolio_calls == [missing_id]
        assert len(repo.list_transactions_calls) == 0
        assert len(repo.list_events_calls) == 0

    def test_wrong_portfolio_type_raises_and_stops(self):
        p_id = uuid4()
        class FakeRepo:
            def get_portfolio(self, p_id): return {"id": p_id}  # dict, not Portfolio
            def list_transactions(self, p_id): return []
            def list_fee_tax_attribution_events(self, p_id, a_id=None, as_of=None): return []

        service = PortfolioFeeTaxAttributionQueryService(FakeRepo())  # type: ignore
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="invalid portfolio object"):
            service.get_current_attribution_view(p_id)

    def test_wrong_portfolio_id_raises_and_stops(self):
        requested_id = uuid4()
        other_p = _make_portfolio(portfolio_id=uuid4())
        repo = StrictTestAttributionRepository(portfolios={requested_id: other_p})
        service = PortfolioFeeTaxAttributionQueryService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="for requested portfolio"):
            service.get_current_attribution_view(requested_id)

        assert len(repo.list_transactions_calls) == 0
        assert len(repo.list_events_calls) == 0

    @pytest.mark.parametrize("bad_txs", [
        None,
        "transactions",
        b"transactions",
        {"tx1": None},
        {1, 2, 3},
        (x for x in []),  # generator
        123,
    ])
    def test_invalid_transaction_collection_rejected(self, bad_txs: Any):
        p = _make_portfolio()
        class BadTxRepo:
            def get_portfolio(self, p_id): return p
            def list_transactions(self, p_id): return bad_txs
            def list_fee_tax_attribution_events(self, p_id, a_id=None, as_of=None): return []

        service = PortfolioFeeTaxAttributionQueryService(BadTxRepo())  # type: ignore
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="invalid transaction collection"):
            service.get_current_attribution_view(p.id)

    @pytest.mark.parametrize("bad_events", [
        None,
        "events",
        b"events",
        {"ev1": None},
        {1, 2, 3},
        (x for x in []),  # generator
        123,
    ])
    def test_invalid_attribution_collection_rejected(self, bad_events: Any):
        p = _make_portfolio()
        class BadEventRepo:
            def get_portfolio(self, portfolio_id): return p
            def list_transactions(self, portfolio_id): return []
            def list_fee_tax_attribution_events(self, portfolio_id, account_id=None, as_of_recorded_at=None): return bad_events

        service = PortfolioFeeTaxAttributionQueryService(BadEventRepo())  # type: ignore
        with pytest.raises(PortfolioFeeTaxAttributionQueryError, match="invalid attribution events collection"):
            service.get_current_attribution_view(p.id)

    def test_single_repository_queries_and_exact_arguments(self):
        p = _make_portfolio()
        repo = StrictTestAttributionRepository(portfolios={p.id: p})
        service = PortfolioFeeTaxAttributionQueryService(repo)

        cutoff = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        service.get_attribution_view_as_of(p.id, cutoff)

        assert repo.get_portfolio_calls == [p.id]
        assert repo.list_transactions_calls == [p.id]
        assert repo.list_events_calls == [(p.id, None, cutoff)]


class TestDomainScenarios:
    """Items 51, 52, 53, 54, 55, 56: End-to-end domain composition scenarios."""

    def test_empty_attribution_history(self):
        p = _make_portfolio()
        account_id = uuid4()
        fee_tx = _make_tx(p.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
            attribution_events={p.id: []},
        )
        service = PortfolioFeeTaxAttributionQueryService(repo)
        view = service.get_current_attribution_view(p.id)

        assert len(view.attribution_set.intents) == 0
        assert len(view.attribution_set.attributions) == 0
        assert len(view.observed_projection.events) == 1

    def test_end_to_end_fee_to_buy(self):
        p = _make_portfolio()
        account_id = uuid4()
        fee_tx = _make_tx(p.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc = _make_allocation_event(p.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"))

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
            attribution_events={p.id: [alloc]},
        )
        service = PortfolioFeeTaxAttributionQueryService(repo)
        view = service.get_current_attribution_view(p.id)

        assert len(view.attribution_set.attributions) == 1
        attr = view.attribution_set.attributions[0]
        assert attr.charge_transaction.id == fee_tx.id
        assert attr.target_transaction.id == buy_tx.id
        assert attr.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()

    def test_end_to_end_tax_withholding_to_dividend(self):
        p = _make_portfolio()
        account_id = uuid4()
        tax_tx = _make_tx(p.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("15.000"))
        div_tx = _make_tx(p.id, account_id, TransactionType.DIVIDEND, cash_amount=Decimal("100.000"))
        alloc = _make_allocation_event(p.id, account_id, tax_tx.id, div_tx.id, allocated_amount=Decimal("15.000"))

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [tax_tx, div_tx]},
            attribution_events={p.id: [alloc]},
        )
        service = PortfolioFeeTaxAttributionQueryService(repo)
        view = service.get_current_attribution_view(p.id)

        assert len(view.attribution_set.attributions) == 1
        attr = view.attribution_set.attributions[0]
        assert attr.charge_transaction.id == tax_tx.id
        assert attr.target_transaction.id == div_tx.id
        assert attr.allocated_amount.as_tuple() == Decimal("15.000").as_tuple()

    def test_attribution_reversal_excluded_from_semantic_set(self):
        p = _make_portfolio()
        account_id = uuid4()
        fee_tx = _make_tx(p.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_tx = _make_tx(p.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        t_alloc = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t_rev = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        alloc = _make_allocation_event(p.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=t_alloc)
        rev = FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=p.id,
            account_id=account_id,
            event_type=FeeTaxAttributionEventType.REVERSAL,
            charge_transaction_id=None,
            target_transaction_id=None,
            allocated_amount=None,
            reverses_attribution_event_id=alloc.id,
            recorded_at=t_rev,
        )

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
            attribution_events={p.id: [alloc, rev]},
        )
        service = PortfolioFeeTaxAttributionQueryService(repo)

        # Before reversal cutoff: active
        early_cutoff = datetime(2026, 8, 29, 10, 30, 0, tzinfo=timezone.utc)
        view_early = service.get_attribution_view_as_of(p.id, early_cutoff)
        assert len(view_early.attribution_set.attributions) == 1

        # After reversal cutoff: inactive / excluded
        late_cutoff = datetime(2026, 8, 29, 11, 30, 0, tzinfo=timezone.utc)
        view_late = service.get_attribution_view_as_of(p.id, late_cutoff)
        assert len(view_late.attribution_set.attributions) == 0

    def test_ledger_reversal_pit_fails_closed(self):
        p = _make_portfolio()
        account_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

        fee_tx = _make_tx(p.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t1)
        buy_tx = _make_tx(p.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t1)
        alloc = _make_allocation_event(p.id, account_id, fee_tx.id, buy_tx.id, allocated_amount=Decimal("6.000"), recorded_at=t2)
        # Ledger reversal of fee_tx at t3
        fee_rev = _make_tx(p.id, account_id, TransactionType.REVERSAL, reverses_transaction_id=fee_tx.id, recorded_at=t3)

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx, fee_rev]},
            attribution_events={p.id: [alloc]},
        )
        service = PortfolioFeeTaxAttributionQueryService(repo)

        # Before ledger reversal: valid
        view_early = service.get_attribution_view_as_of(p.id, datetime(2026, 8, 29, 11, 30, 0, tzinfo=timezone.utc))
        assert len(view_early.attribution_set.attributions) == 1


        # After ledger reversal: fee_tx is inactive at PIT, so persisted allocation fails closed via Phase 14J
        with pytest.raises(FeeTaxAttributionBindingError, match="not an active FEE or TAX_WITHHOLDING at PIT"):
            service.get_attribution_view_as_of(p.id, datetime(2026, 8, 29, 12, 30, 0, tzinfo=timezone.utc))



class TestErrorPropagation:
    """Items 58, 59, 60: Verification that operational and lower errors propagate unchanged."""

    def test_lower_binding_error_propagates_unchanged(self):
        p = _make_portfolio()
        account_id = uuid4()
        fee_tx = _make_tx(p.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        # Missing buy_tx in ledger transactions
        alloc = _make_allocation_event(p.id, account_id, fee_tx.id, uuid4(), allocated_amount=Decimal("6.000"))

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx]},
            attribution_events={p.id: [alloc]},
        )
        service = PortfolioFeeTaxAttributionQueryService(repo)

        with pytest.raises(FeeTaxAttributionBindingError):
            service.get_current_attribution_view(p.id)

    def test_lower_history_error_propagates_unchanged(self):
        p = _make_portfolio()
        account_id = uuid4()
        fee_tx = _make_tx(p.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, account_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        # Reversal event referencing non-existent allocation
        rev = FeeTaxAttributionPersistenceEvent(
            id=uuid4(),
            portfolio_id=p.id,
            account_id=account_id,
            event_type=FeeTaxAttributionEventType.REVERSAL,
            charge_transaction_id=None,
            target_transaction_id=None,
            allocated_amount=None,
            reverses_attribution_event_id=uuid4(),
            recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc),
        )

        repo = StrictTestAttributionRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
            attribution_events={p.id: [rev]},
        )

        service = PortfolioFeeTaxAttributionQueryService(repo)

        with pytest.raises(FeeTaxAttributionHistoryError):
            service.get_current_attribution_view(p.id)

    def test_repository_operational_error_propagates_unchanged(self):
        sentinel_exc = RuntimeError("Database connection pool exhausted")
        repo = StrictTestAttributionRepository(get_portfolio_error=sentinel_exc)
        service = PortfolioFeeTaxAttributionQueryService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.get_current_attribution_view(uuid4())
        assert exc_info.value is sentinel_exc


class TestStaticPurityAndInvariants:
    """Items 57, 62: Static purity and absence of forbidden patterns."""

    def test_public_methods_contain_no_owner_arguments(self):
        sig_curr = inspect.signature(PortfolioFeeTaxAttributionQueryService.get_current_attribution_view)
        sig_as_of = inspect.signature(PortfolioFeeTaxAttributionQueryService.get_attribution_view_as_of)

        forbidden_args = {"owner_id", "user_id", "auth_user_id", "credential", "service_role"}
        assert not (set(sig_curr.parameters.keys()) & forbidden_args)
        assert not (set(sig_as_of.parameters.keys()) & forbidden_args)

    def test_no_prohibited_patterns_in_production_source(self):
        source = inspect.getsource(service_mod)

        prohibited = [
            ".table(",
            ".rpc(",
            "Supabase",
            "PostgREST",
            "uuid4",
            "uuid5",
            "hashlib",
            "sha256",
            "float(",
            "round(",
            "quantize(",
        ]
        for p in prohibited:
            assert p not in source, f"Found prohibited pattern '{p}' in fee_tax_attribution_service.py"
