"""
backend/tests/test_portfolio_fee_tax_attribution_repository.py
==============================================================
Tests for Phase 14H: Owner-Bound Fee/Tax Attribution Read Repository & PostgREST Transport.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceError,
    FeeTaxAttributionPersistenceEvent,
)
from backend.engine.private.portfolio.postgrest_transport import (
    ALL_SEVEN_FINANCIAL_NUMERIC_COLUMNS,
    FEE_TAX_ATTRIBUTION_EVENT_SELECT,
    FINANCIAL_NUMERIC_COLUMNS_BY_TABLE,
)
from backend.engine.private.portfolio.repository import PortfolioRepository


# ─────────────────────────────────────────────────────────────────────────────
# Mock Supabase / PostgREST Infrastructure
# ─────────────────────────────────────────────────────────────────────────────

class MockQueryResult:
    def __init__(self, data: Any, count: Optional[int] = None):
        self.data = data
        self.count = count


class MockQueryBuilder:
    def __init__(self, table_name: str, client_store: MockSupabaseClient):
        self.table_name = table_name
        self.client_store = client_store
        self.projection: Optional[str] = None
        self.eq_filters: Dict[str, Any] = {}
        self.lte_filters: Dict[str, Any] = {}
        self.order_clauses: List[Tuple[str, bool]] = []
        self.range_start: Optional[int] = None
        self.range_end: Optional[int] = None

    def select(self, projection: str) -> MockQueryBuilder:
        self.projection = projection
        self.client_store.recorded_selects.append((self.table_name, projection))
        return self

    def eq(self, column: str, value: Any) -> MockQueryBuilder:
        self.eq_filters[column] = value
        return self

    def lte(self, column: str, value: Any) -> MockQueryBuilder:
        self.lte_filters[column] = value
        return self

    def order(self, column: str, desc: bool = False, **kwargs: Any) -> MockQueryBuilder:
        self.order_clauses.append((column, desc))
        return self

    def range(self, start: int, end: int) -> MockQueryBuilder:
        self.range_start = start
        self.range_end = end
        return self

    def execute(self) -> MockQueryResult:
        table_rows = self.client_store.tables.get(self.table_name, [])
        filtered: List[Dict[str, Any]] = []

        for r in table_rows:
            match = True
            for col, val in self.eq_filters.items():
                if str(r.get(col)) != str(val):
                    match = False
                    break
            if not match:
                continue
            for col, val in self.lte_filters.items():
                if r.get(col) is None or str(r.get(col)) > str(val):
                    match = False
                    break
            if match:
                filtered.append(deepcopy(r))

        # Apply ordering if specified
        if self.order_clauses:
            for col, desc in reversed(self.order_clauses):
                filtered.sort(key=lambda x: str(x.get(col, "")), reverse=desc)

        # Apply range pagination if specified
        if self.range_start is not None and self.range_end is not None:
            filtered = filtered[self.range_start : self.range_end + 1]

        return MockQueryResult(data=filtered)


class MockSupabaseClient:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.recorded_selects: List[Tuple[str, str]] = []

    def table(self, table_name: str) -> MockQueryBuilder:
        return MockQueryBuilder(table_name, self)


def make_attribution_row(
    event_id: UUID,
    portfolio_id: UUID,
    account_id: UUID,
    owner_id: UUID,
    event_type: str = "allocation",
    recorded_at: str = "2026-08-29T12:00:00+00:00",
    charge_transaction_id: Optional[UUID] = None,
    target_transaction_id: Optional[UUID] = None,
    allocated_amount: Optional[str] = "50.000",
    reverses_attribution_event_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    return {
        "id": str(event_id),
        "portfolio_id": str(portfolio_id),
        "account_id": str(account_id),
        "owner_id": str(owner_id),
        "event_type": event_type,
        "recorded_at": recorded_at,
        "charge_transaction_id": str(charge_transaction_id) if charge_transaction_id else None,
        "target_transaction_id": str(target_transaction_id) if target_transaction_id else None,
        "allocated_amount": allocated_amount,
        "reverses_attribution_event_id": str(reverses_attribution_event_id) if reverses_attribution_event_id else None,
    }


class TestPortfolioFeeTaxAttributionRepository:
    """Repository unit & invariant tests for Phase 14H."""

    def test_get_fee_tax_attribution_event_success(self):
        """Item 53: Get attribution event queries correct table, select, and owner/portfolio/id filters."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        a_id = uuid4()
        e_id = uuid4()
        charge_id = uuid4()
        target_id = uuid4()

        row = make_attribution_row(
            event_id=e_id,
            portfolio_id=p_id,
            account_id=a_id,
            owner_id=owner_id,
            charge_transaction_id=charge_id,
            target_transaction_id=target_id,
            allocated_amount="12.500",
            recorded_at="2026-08-29T15:00:00+03:00",
        )
        client.tables["portfolio_fee_tax_attribution_events"] = [row]

        event = repo.get_fee_tax_attribution_event(p_id, e_id)
        assert event is not None
        assert event.id == e_id
        assert event.portfolio_id == p_id
        assert event.account_id == a_id
        assert event.event_type == FeeTaxAttributionEventType.ALLOCATION
        assert event.allocated_amount == Decimal("12.500")
        assert event.recorded_at == datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

        # Check recorded select
        assert len(client.recorded_selects) == 1
        table_name, projection = client.recorded_selects[0]
        assert table_name == "portfolio_fee_tax_attribution_events"
        assert projection == FEE_TAX_ATTRIBUTION_EVENT_SELECT

    def test_get_fee_tax_attribution_event_owner_tamper_rejected(self):
        """Item 54: If transport returns row with mismatched owner_id, hydration fails closed."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        other_owner = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        a_id = uuid4()
        e_id = uuid4()
        charge_id = uuid4()
        target_id = uuid4()

        # Insert row with wrong owner_id
        row = make_attribution_row(
            event_id=e_id,
            portfolio_id=p_id,
            account_id=a_id,
            owner_id=other_owner,
            charge_transaction_id=charge_id,
            target_transaction_id=target_id,
        )
        # Mock client bypassing owner check to test repository defense-in-depth
        client.tables["portfolio_fee_tax_attribution_events"] = [row]

        # Overriding eq filter behavior for this test to simulate rogue row returned
        class RogueQueryBuilder(MockQueryBuilder):
            def execute(self) -> MockQueryResult:
                return MockQueryResult(data=[row])

        client.table = lambda t: RogueQueryBuilder(t, client)  # type: ignore

        with pytest.raises(FeeTaxAttributionPersistenceError, match="does not match expected_owner_id"):
            repo.get_fee_tax_attribution_event(p_id, e_id)

    def test_get_fee_tax_attribution_event_not_found(self):
        """Item 55: Non-existent event returns None."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        e_id = uuid4()
        assert repo.get_fee_tax_attribution_event(p_id, e_id) is None

    def test_list_fee_tax_attribution_events_pagination(self):
        """Item 56: >1000 rows across pages are all fetched and returned."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        a_id = uuid4()

        total_rows = 1500
        rows = []
        for i in range(total_rows):
            rows.append(
                make_attribution_row(
                    event_id=UUID(f"00000000-0000-4000-8000-{i:012x}"),
                    portfolio_id=p_id,
                    account_id=a_id,
                    owner_id=owner_id,
                    charge_transaction_id=uuid4(),
                    target_transaction_id=uuid4(),
                    recorded_at=f"2026-08-29T12:00:{i % 60:02d}+00:00",
                )
            )
        client.tables["portfolio_fee_tax_attribution_events"] = rows

        events = repo.list_fee_tax_attribution_events(p_id)
        assert len(events) == total_rows

    def test_list_fee_tax_attribution_events_deterministic_order(self):
        """Item 57: Results are sorted deterministically by (recorded_at, id)."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        a_id = uuid4()

        id1 = UUID("11111111-1111-4111-8111-111111111111")
        id2 = UUID("22222222-2222-4222-8222-222222222222")
        id3 = UUID("33333333-3333-4333-8333-333333333333")

        # Invert arrival order
        rows = [
            make_attribution_row(
                event_id=id3,
                portfolio_id=p_id,
                account_id=a_id,
                owner_id=owner_id,
                recorded_at="2026-08-29T14:00:00+00:00",
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
            make_attribution_row(
                event_id=id2,
                portfolio_id=p_id,
                account_id=a_id,
                owner_id=owner_id,
                recorded_at="2026-08-29T12:00:00+00:00",
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
            make_attribution_row(
                event_id=id1,
                portfolio_id=p_id,
                account_id=a_id,
                owner_id=owner_id,
                recorded_at="2026-08-29T12:00:00+00:00",
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
        ]
        client.tables["portfolio_fee_tax_attribution_events"] = rows

        events = repo.list_fee_tax_attribution_events(p_id)
        assert len(events) == 3
        assert events[0].id == id1
        assert events[1].id == id2
        assert events[2].id == id3

    def test_list_fee_tax_attribution_events_account_filter(self):
        """Item 58: Account filter is applied when provided."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        a_id_1 = uuid4()
        a_id_2 = uuid4()

        rows = [
            make_attribution_row(
                event_id=uuid4(),
                portfolio_id=p_id,
                account_id=a_id_1,
                owner_id=owner_id,
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
            make_attribution_row(
                event_id=uuid4(),
                portfolio_id=p_id,
                account_id=a_id_2,
                owner_id=owner_id,
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
        ]
        client.tables["portfolio_fee_tax_attribution_events"] = rows

        events_a1 = repo.list_fee_tax_attribution_events(p_id, account_id=a_id_1)
        assert len(events_a1) == 1
        assert events_a1[0].account_id == a_id_1

        events_all = repo.list_fee_tax_attribution_events(p_id)
        assert len(events_all) == 2

    def test_list_fee_tax_attribution_events_pit_filter(self):
        """Item 59: as_of_recorded_at filters with exact physical instant converted to UTC."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        a_id = uuid4()

        rows = [
            make_attribution_row(
                event_id=uuid4(),
                portfolio_id=p_id,
                account_id=a_id,
                owner_id=owner_id,
                recorded_at="2026-08-29T10:00:00+00:00",
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
            make_attribution_row(
                event_id=uuid4(),
                portfolio_id=p_id,
                account_id=a_id,
                owner_id=owner_id,
                recorded_at="2026-08-29T12:00:00+00:00",
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
            make_attribution_row(
                event_id=uuid4(),
                portfolio_id=p_id,
                account_id=a_id,
                owner_id=owner_id,
                recorded_at="2026-08-29T14:00:00+00:00",
                charge_transaction_id=uuid4(),
                target_transaction_id=uuid4(),
            ),
        ]
        client.tables["portfolio_fee_tax_attribution_events"] = rows

        # Cutoff: 15:00+03:00 == 12:00+00:00
        cutoff = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(datetime.fromisoformat("2026-08-29T15:00:00+03:00").tzinfo.utcoffset(None)))  # +03:00
        events = repo.list_fee_tax_attribution_events(p_id, as_of_recorded_at=cutoff)
        assert len(events) == 2

        # Naive datetime rejection
        with pytest.raises(ValueError, match="must be timezone-aware"):
            repo.list_fee_tax_attribution_events(p_id, as_of_recorded_at=datetime(2026, 8, 29, 12, 0, 0))

    def test_list_fee_tax_attribution_events_empty_history(self):
        """Item 60: Empty table returns []."""
        client = MockSupabaseClient()
        owner_id = uuid4()
        repo = PortfolioRepository(client, owner_id)

        p_id = uuid4()
        assert repo.list_fee_tax_attribution_events(p_id) == []


class TestStaticAttributionRepositoryInvariants:
    """Static inspections for Phase 14H invariants."""

    def test_numeric_select_contains_text_cast(self):
        """Item 61: Attribution select contains allocated_amount::text."""
        assert "allocated_amount::text" in FEE_TAX_ATTRIBUTION_EVENT_SELECT
        assert "allocated_amount," not in FEE_TAX_ATTRIBUTION_EVENT_SELECT

    def test_numeric_registry_contains_attribution_table(self):
        """Item 7: Numeric registry contains portfolio_fee_tax_attribution_events."""
        assert "portfolio_fee_tax_attribution_events" in FINANCIAL_NUMERIC_COLUMNS_BY_TABLE
        assert "allocated_amount" in FINANCIAL_NUMERIC_COLUMNS_BY_TABLE["portfolio_fee_tax_attribution_events"]

    def test_no_wildcard_select_in_repository_attribution_methods(self):
        """Item 62: Repository attribution read methods do not use wildcard select."""
        source = inspect.getsource(PortfolioRepository.get_fee_tax_attribution_event)
        source += inspect.getsource(PortfolioRepository.list_fee_tax_attribution_events)
        assert 'select("*")' not in source
        assert "select('*')" not in source

    def test_owner_filter_explicit_in_all_attribution_read_methods(self):
        """Item 63: Explicit owner_id filter in every attribution method."""
        source_get = inspect.getsource(PortfolioRepository.get_fee_tax_attribution_event)
        source_list = inspect.getsource(PortfolioRepository.list_fee_tax_attribution_events)
        assert '.eq("owner_id", self._owner_id_str)' in source_get
        assert '.eq("owner_id", self._owner_id_str)' in source_list

    def test_no_writes_in_attribution_repository_code(self):
        """Item 64: Phase 14H adds zero attribution write methods."""
        repo_methods = dir(PortfolioRepository)
        attribution_methods = [m for m in repo_methods if "fee_tax_attribution" in m]
        assert set(attribution_methods) == {
            "get_fee_tax_attribution_event",
            "list_fee_tax_attribution_events",
        }

    def test_no_clock_use_in_attribution_read_methods(self):
        """Item 65: Attribution read methods contain no clock or now() calls."""
        source = inspect.getsource(PortfolioRepository.get_fee_tax_attribution_event)
        source += inspect.getsource(PortfolioRepository.list_fee_tax_attribution_events)
        assert "self._get_system_time" not in source
        assert "self._clock" not in source
        assert "datetime.now" not in source

    def test_no_ledger_lookups_in_attribution_read_methods(self):
        """Item 66: Attribution read methods do not query portfolio_transactions."""
        source = inspect.getsource(PortfolioRepository.get_fee_tax_attribution_event)
        source += inspect.getsource(PortfolioRepository.list_fee_tax_attribution_events)
        assert 'table("portfolio_transactions")' not in source
        assert "table('portfolio_transactions')" not in source
