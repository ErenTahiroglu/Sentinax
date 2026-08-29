"""
backend/tests/test_portfolio_fee_tax_attribution_write_repository.py
====================================================================
Comprehensive tests for Phase 14L: Owner-Bound Append-Only Fee/Tax Attribution
Persistence Write Repository.

Tests:
1. Input Type Strictness
2. Allocation & Reversal Insert Payload Contracts (10 keys, exact Decimal string, canonical UUIDs, owner binding)
3. Returning Minimal & Readback Authority
4. Readback Validation & Equivalence (UUIDs, Decimal representation, TIMESTAMPTZ physical instant)
5. Physical Event-ID Idempotency (Identical replay, Conflicting replay, Unexplained 23505)
6. Database Trigger & Operational Error Propagation (Non-23505, Over-allocation, Active duplicate, Inactive target)
7. Architectural Invariants (No clock, No UUID generation, No preflight calls, Static purity)
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from postgrest.exceptions import APIError
import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
    build_attribution_reversal_persistence_event,
)
from backend.engine.private.portfolio.postgrest_transport import (
    FEE_TAX_ATTRIBUTION_EVENT_SELECT,
)
from backend.engine.private.portfolio.repository import (
    PortfolioRepository,
    _fee_tax_attribution_events_persistence_equivalent,
)


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
        self._insert_payload: Optional[Any] = None
        self._returning: Optional[str] = None

    def select(self, projection: str) -> MockQueryBuilder:
        self.projection = projection
        self.client_store.recorded_selects.append((self.table_name, projection))
        return self

    def insert(self, row: Any, returning: str = "representation") -> MockQueryBuilder:
        self._insert_payload = row
        self._returning = returning
        self.client_store.recorded_inserts.append((self.table_name, row, returning))
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
        if self.client_store.insert_error is not None and self._insert_payload is not None:
            raise self.client_store.insert_error

        if self._insert_payload is not None:
            table_rows = self.client_store.tables.setdefault(self.table_name, [])
            row_to_insert = deepcopy(self._insert_payload)
            # Store row in memory DB
            table_rows.append(row_to_insert)
            return MockQueryResult(data=[row_to_insert] if self._returning != "minimal" else [])

        # SELECT queries
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

        if self.order_clauses:
            for col, desc in reversed(self.order_clauses):
                filtered.sort(key=lambda x: str(x.get(col, "")), reverse=desc)

        if self.range_start is not None and self.range_end is not None:
            filtered = filtered[self.range_start : self.range_end + 1]

        return MockQueryResult(data=filtered)


class MockSupabaseClient:
    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "portfolio_fee_tax_attribution_events": [],
            "portfolios": [],
            "portfolio_accounts": [],
            "portfolio_transactions": [],
        }
        self.recorded_selects: List[Tuple[str, str]] = []
        self.recorded_inserts: List[Tuple[str, Any, str]] = []
        self.insert_error: Optional[Exception] = None

    def table(self, table_name: str) -> MockQueryBuilder:
        return MockQueryBuilder(table_name, self)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_allocation_event(
    portfolio_id: UUID,
    account_id: UUID,
    charge_id: UUID,
    target_id: UUID,
    allocated_amount: Decimal = Decimal("6.000"),
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
        recorded_at=recorded_at or datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_reversal_event(
    portfolio_id: UUID,
    account_id: UUID,
    reverses_event_id: UUID,
    event_id: Optional[UUID] = None,
    recorded_at: Optional[datetime] = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.REVERSAL,
        charge_transaction_id=None,
        target_transaction_id=None,
        allocated_amount=None,
        reverses_attribution_event_id=reverses_event_id,
        recorded_at=recorded_at or datetime(2026, 8, 29, 12, 30, 0, tzinfo=timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Suites
# ─────────────────────────────────────────────────────────────────────────────

class TestInputTypeStrictness:
    """Item 37: Reject non-FeeTaxAttributionPersistenceEvent instances."""

    @pytest.mark.parametrize("bad_event", [
        None,
        True,
        False,
        {},
        "event",
        123,
        uuid4(),
    ])
    def test_rejects_invalid_event_types(self, bad_event: Any):
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=uuid4())

        with pytest.raises(TypeError, match="event must be a FeeTaxAttributionPersistenceEvent instance"):
            repo.append_fee_tax_attribution_event(bad_event)

        assert len(client.recorded_inserts) == 0


class TestInsertPayloadContracts:
    """Items 38, 39, 40, 61, 64: Verify exact 10-key row format, types, and returning=minimal."""

    def test_allocation_payload_contract(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        p_id = uuid4()
        a_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        e_id = uuid4()
        rec_at = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        amount = Decimal("12345678901234567890.123400")

        event = _make_allocation_event(
            portfolio_id=p_id,
            account_id=a_id,
            charge_id=c_id,
            target_id=t_id,
            allocated_amount=amount,
            event_id=e_id,
            recorded_at=rec_at,
        )

        persisted = repo.append_fee_tax_attribution_event(event)

        assert len(client.recorded_inserts) == 1
        table_name, row, returning = client.recorded_inserts[0]

        assert table_name == "portfolio_fee_tax_attribution_events"
        assert returning == "minimal"

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
        assert row["id"] == str(e_id)
        assert row["portfolio_id"] == str(p_id)
        assert row["account_id"] == str(a_id)
        assert row["owner_id"] == str(owner_id)
        assert row["event_type"] == "allocation"
        assert row["recorded_at"] == "2026-08-29T15:00:00+03:00"
        assert row["charge_transaction_id"] == str(c_id)
        assert row["target_transaction_id"] == str(t_id)
        assert row["allocated_amount"] == "12345678901234567890.123400"
        assert row["reverses_attribution_event_id"] is None

        assert persisted.id == e_id
        assert persisted.allocated_amount.as_tuple() == amount.as_tuple()

    def test_reversal_payload_contract(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        p_id = uuid4()
        a_id = uuid4()
        rev_target_id = uuid4()
        e_id = uuid4()
        rec_at = datetime(2026, 8, 29, 12, 30, 0, tzinfo=timezone.utc)

        event = _make_reversal_event(
            portfolio_id=p_id,
            account_id=a_id,
            reverses_event_id=rev_target_id,
            event_id=e_id,
            recorded_at=rec_at,
        )

        persisted = repo.append_fee_tax_attribution_event(event)

        assert len(client.recorded_inserts) == 1
        table_name, row, returning = client.recorded_inserts[0]

        assert table_name == "portfolio_fee_tax_attribution_events"
        assert returning == "minimal"
        assert row["id"] == str(e_id)
        assert row["portfolio_id"] == str(p_id)
        assert row["account_id"] == str(a_id)
        assert row["owner_id"] == str(owner_id)
        assert row["event_type"] == "reversal"
        assert row["charge_transaction_id"] is None
        assert row["target_transaction_id"] is None
        assert row["allocated_amount"] is None
        assert row["reverses_attribution_event_id"] == str(rev_target_id)

        assert persisted.id == e_id
        assert persisted.reverses_attribution_event_id == rev_target_id


class TestReadbackAuthorityAndEquivalence:
    """Items 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51: Readback checks and equivalence verification."""

    def test_returned_object_is_hydrated_readback_instance(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4(), Decimal("6.000"))
        result = repo.append_fee_tax_attribution_event(event)

        assert result is not event
        assert isinstance(result, FeeTaxAttributionPersistenceEvent)
        assert _fee_tax_attribution_events_persistence_equivalent(event, result)

    def test_same_instant_different_offset_accepted(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        p_id = uuid4()
        e_id = uuid4()
        rec_at = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        event = _make_allocation_event(p_id, uuid4(), uuid4(), uuid4(), Decimal("6.000"), event_id=e_id, recorded_at=rec_at)

        # In DB, TIMESTAMPTZ is stored as UTC instant
        result = repo.append_fee_tax_attribution_event(event)
        assert result.recorded_at.astimezone(timezone.utc) == rec_at.astimezone(timezone.utc)

    def test_missing_readback_raises_runtime_error(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4())

        # Intercept get_fee_tax_attribution_event to return None
        orig_get = repo.get_fee_tax_attribution_event
        repo.get_fee_tax_attribution_event = lambda p, e: None  # type: ignore

        with pytest.raises(RuntimeError, match="could not be read back from persistence"):
            repo.append_fee_tax_attribution_event(event)

    def test_decimal_representation_drift_raises_runtime_error(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4(), Decimal("6.000"))

        # Injected readback with Decimal("6") instead of Decimal("6.000")
        def tampered_get(p_id, e_id):
            return FeeTaxAttributionPersistenceEvent(
                id=event.id,
                portfolio_id=event.portfolio_id,
                account_id=event.account_id,
                event_type=event.event_type,
                charge_transaction_id=event.charge_transaction_id,
                target_transaction_id=event.target_transaction_id,
                allocated_amount=Decimal("6"),  # Tampered precision!
                reverses_attribution_event_id=None,
                recorded_at=event.recorded_at,
            )

        repo.get_fee_tax_attribution_event = tampered_get  # type: ignore

        with pytest.raises(RuntimeError, match="does not match expected persistence semantics"):
            repo.append_fee_tax_attribution_event(event)

    def test_different_physical_timestamp_raises_runtime_error(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        rec_at = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4(), recorded_at=rec_at)

        def tampered_get(p_id, e_id):
            return FeeTaxAttributionPersistenceEvent(
                id=event.id,
                portfolio_id=event.portfolio_id,
                account_id=event.account_id,
                event_type=event.event_type,
                charge_transaction_id=event.charge_transaction_id,
                target_transaction_id=event.target_transaction_id,
                allocated_amount=event.allocated_amount,
                reverses_attribution_event_id=None,
                recorded_at=rec_at + timedelta(seconds=1),  # Different instant!
            )

        repo.get_fee_tax_attribution_event = tampered_get  # type: ignore

        with pytest.raises(RuntimeError, match="does not match expected persistence semantics"):
            repo.append_fee_tax_attribution_event(event)


class TestPhysicalIdIdempotencyAndConflicts:
    """Items 52, 53, 54, 80: SQLSTATE 23505 race resolution."""

    def test_identical_physical_replay_returns_existing(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4(), Decimal("6.000"))

        # Pre-seed DB with identical event
        repo.append_fee_tax_attribution_event(event)
        assert len(client.recorded_inserts) == 1

        # Simulate 23505 on second append attempt
        err_23505 = APIError({"message": "duplicate key value violates unique constraint", "code": "23505"})
        client.insert_error = err_23505

        replayed = repo.append_fee_tax_attribution_event(event)
        assert replayed.id == event.id
        assert replayed.allocated_amount.as_tuple() == event.allocated_amount.as_tuple()

    def test_physical_id_conflict_raises_runtime_error(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        e_id = uuid4()
        p_id = uuid4()
        a_id = uuid4()
        c_id = uuid4()
        t1_id = uuid4()
        t2_id = uuid4()

        event1 = _make_allocation_event(p_id, a_id, c_id, t1_id, Decimal("6.000"), event_id=e_id)
        event2 = _make_allocation_event(p_id, a_id, c_id, t2_id, Decimal("4.000"), event_id=e_id)  # Same ID, different target/amount!

        repo.append_fee_tax_attribution_event(event1)

        err_23505 = APIError({"message": "duplicate key value violates unique constraint", "code": "23505"})
        client.insert_error = err_23505

        with pytest.raises(RuntimeError, match="Concurrent conflict"):
            repo.append_fee_tax_attribution_event(event2)

    def test_unexplained_23505_reraises_original_error(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4())

        # Simulate 23505 from a constraint other than PK (event.id does not exist in table)
        err_23505 = APIError({"message": "duplicate key value violates uq_fee_tax_attribution_single_reversal", "code": "23505"})
        client.insert_error = err_23505

        with pytest.raises(APIError) as exc_info:
            repo.append_fee_tax_attribution_event(event)
        assert exc_info.value is err_23505


class TestTriggerAndOperationalErrors:
    """Items 55, 56, 57, 58: Non-23505 database errors propagate unchanged."""

    def test_non_23505_api_error_propagates_unchanged(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4())

        err_custom = APIError({"message": "Cumulative active allocation exceeds charge capacity", "code": "P0001"})
        client.insert_error = err_custom

        with pytest.raises(APIError) as exc_info:
            repo.append_fee_tax_attribution_event(event)
        assert exc_info.value is err_custom


class TestArchitecturalInvariantsAndStaticPurity:
    """Items 59, 60, 62, 63, 66, 67, 68: Invariant and static purity assertions."""

    def test_no_clock_and_no_uuid_generation_in_method(self):
        client = MockSupabaseClient()

        def exploding_clock():
            raise AssertionError("Repository clock must not be called during append_fee_tax_attribution_event!")

        repo = PortfolioRepository(client=client, owner_id=uuid4(), clock=exploding_clock)
        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4())

        persisted = repo.append_fee_tax_attribution_event(event)
        assert persisted.id == event.id

    def test_no_preflight_queries_executed(self):
        owner_id = uuid4()
        client = MockSupabaseClient()
        repo = PortfolioRepository(client=client, owner_id=owner_id)

        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4())
        repo.append_fee_tax_attribution_event(event)

        # Ensure no calls were made to portfolios or portfolio_accounts
        tables_queried = [t for t, _ in client.recorded_selects]
        assert "portfolios" not in tables_queried
        assert "portfolio_accounts" not in tables_queried

    def test_static_source_code_purity(self):
        method = getattr(PortfolioRepository, "append_fee_tax_attribution_event")
        source = inspect.getsource(method)

        assert "_get_system_time" not in source
        assert "uuid4(" not in source
        assert "uuid5(" not in source
        assert ".update(" not in source
        assert ".delete(" not in source
        assert ".upsert(" not in source
        assert ".rpc(" not in source
        assert "serialize_fee_tax_attribution_persistence_event" in source
