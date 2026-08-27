"""
backend/tests/test_portfolio_ledger.py
======================================
Comprehensive In-Memory Portfolio Ledger & Reversal Unit Tests.

Zero external network calls (pytest-socket enforced).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    CashPurpose,
    Currency,
    PortfolioMode,
    TransactionType,
)
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
)


def _make_buy(
    p_id: UUID,
    a_id: UUID,
    inst_id: UUID,
    eff_date: date,
    qty: str = "10",
    price: str = "100.00",
    curr: Currency = Currency.TRY,
    ext_source: str | None = None,
    ext_ref: str | None = None,
    exec_at: datetime | None = None,
    tx_id: UUID | None = None,
) -> PortfolioTransaction:
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    return PortfolioTransaction(
        id=tx_id or uuid4(),
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=eff_date,
        executed_at=exec_at,
        recorded_at=now,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        trade_currency=curr,
        external_source=ext_source,
        external_reference=ext_ref,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Append & List Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_append_valid_transactions_and_listing():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)

    tx1 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1))
    tx2 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 5))

    r1 = ledger.append(tx1)
    assert r1.status == AppendStatus.APPENDED
    assert r1.transaction_id == tx1.id

    r2 = ledger.append(tx2)
    assert r2.status == AppendStatus.APPENDED

    assert len(ledger) == 2
    txs = ledger.list_transactions()
    assert txs[0].effective_date == date(2026, 8, 1)
    assert txs[1].effective_date == date(2026, 8, 5)


def test_append_mismatched_portfolio_rejected():
    p_id = uuid4()
    other_p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)

    tx = _make_buy(other_p_id, a_id, inst_id, date(2026, 8, 1))
    res = ledger.append(tx)
    assert res.status == AppendStatus.INVALID
    assert "does not match ledger" in res.diagnostics[0]
    assert len(ledger) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. External Idempotency & Conflict Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_external_idempotency_safe_replay():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)

    tx1 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        ext_source="MIDAS", ext_ref="ORD-1001",
    )
    r1 = ledger.append(tx1)
    assert r1.status == AppendStatus.APPENDED

    # Replay identical event with different internal UUID and later recorded_at
    tx2 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        ext_source="MIDAS", ext_ref="ORD-1001",
    )
    r2 = ledger.append(tx2)
    assert r2.status == AppendStatus.IDEMPOTENT_DUPLICATE
    assert r2.transaction_id == tx1.id  # points to original
    assert len(ledger) == 1


def test_external_idempotency_conflict_detection():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)

    tx1 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        qty="100", price="50.00",
        ext_source="MIDAS", ext_ref="ORD-1001",
    )
    r1 = ledger.append(tx1)
    assert r1.status == AppendStatus.APPENDED

    # Same external ref but different quantity -> CONFLICT!
    tx2 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        qty="200", price="50.00",
        ext_source="MIDAS", ext_ref="ORD-1001",
    )
    r2 = ledger.append(tx2)
    assert r2.status == AppendStatus.CONFLICT
    assert len(ledger) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reversal & Correction Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_valid_reversal_workflow():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Original BUY
    buy = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1))
    r_buy = ledger.append(buy)
    assert r_buy.status == AppendStatus.APPENDED
    assert ledger.is_reversed(buy.id) is False

    # 2. Append REVERSAL
    rev = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=buy.id,
    )
    r_rev = ledger.append(rev)
    assert r_rev.status == AppendStatus.APPENDED
    assert ledger.is_reversed(buy.id) is True
    assert ledger.get_reversal_transaction_id(buy.id) == rev.id


def test_reversal_target_not_found():
    p_id = uuid4()
    a_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    rev = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=uuid4(),  # Non-existent
    )
    res = ledger.append(rev)
    assert res.status == AppendStatus.INVALID
    assert "not found in this ledger" in res.diagnostics[0]


def test_cross_portfolio_reversal_rejected():
    p_id_a = uuid4()
    p_id_b = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    ledger_b = PortfolioLedger(portfolio_id=p_id_b)

    # Transaction in A
    buy_a = _make_buy(p_id_a, a_id, inst_id, date(2026, 8, 1))

    # Hack to insert into ledger_b's dict for isolated cross-check
    ledger_b._tx_by_id[buy_a.id] = buy_a

    # Reversal in B targeting transaction from A
    rev_b = PortfolioTransaction(
        portfolio_id=p_id_b,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=buy_a.id,
    )
    res = ledger_b.append(rev_b)
    assert res.status == AppendStatus.INVALID
    assert "Cross-portfolio reversal rejected" in res.diagnostics[0]


def test_cross_account_reversal_rejected():
    p_id = uuid4()
    a_id_1 = uuid4()
    a_id_2 = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    ledger = PortfolioLedger(portfolio_id=p_id)

    # BUY in account 1
    buy_1 = _make_buy(p_id, a_id_1, inst_id, date(2026, 8, 1))
    ledger.append(buy_1)

    # REVERSAL in account 2 targeting BUY in account 1
    rev_2 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id_2,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=buy_1.id,
    )
    res = ledger.append(rev_2)
    assert res.status == AppendStatus.INVALID
    assert "Cross-account reversal rejected" in res.diagnostics[0]


def test_reversal_of_reversal_rejected():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    ledger = PortfolioLedger(portfolio_id=p_id)

    # 1. Original BUY
    buy = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1))
    ledger.append(buy)

    # 2. Valid Reversal 1
    rev1 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=buy.id,
    )
    r1 = ledger.append(rev1)
    assert r1.status == AppendStatus.APPENDED

    # 3. Attempt to reverse Reversal 1
    rev2 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 3),
        recorded_at=now,
        reverses_transaction_id=rev1.id,
    )
    r2 = ledger.append(rev2)
    assert r2.status == AppendStatus.INVALID
    assert "Reversal of a reversal" in r2.diagnostics[0]


def test_double_reversal_rejected():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    ledger = PortfolioLedger(portfolio_id=p_id)

    # 1. Original BUY
    buy = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1))
    ledger.append(buy)

    # 2. First Reversal
    rev1 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=buy.id,
    )
    r1 = ledger.append(rev1)
    assert r1.status == AppendStatus.APPENDED

    # 3. Second Reversal targeting same BUY -> Double reversal!
    rev2 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 3),
        recorded_at=now,
        reverses_transaction_id=buy.id,
    )
    r2 = ledger.append(rev2)
    assert r2.status == AppendStatus.INVALID
    assert "Double reversal rejected" in r2.diagnostics[0]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cross-Entity Validator Consistency Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_cross_entity_consistency_validator():
    p_id = uuid4()
    other_p_id = uuid4()
    a_id = uuid4()
    other_a_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    port = Portfolio(id=p_id, mode=PortfolioMode.MY_PORTFOLIO, name="Real", base_currency=Currency.TRY, created_at=now)
    acc = PortfolioAccount(id=a_id, portfolio_id=p_id, name="Acc", base_currency=Currency.TRY, created_at=now)
    bucket = CashBucket(id=uuid4(), portfolio_id=p_id, account_id=a_id, name="TL", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)

    tx = _make_buy(p_id, a_id, uuid4(), date(2026, 8, 1))

    # All match -> passes
    PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx, port, acc, bucket)

    # Mismatched account
    bad_acc = PortfolioAccount(id=other_a_id, portfolio_id=other_p_id, name="Bad", base_currency=Currency.TRY, created_at=now)
    with pytest.raises(ValueError, match="Account portfolio_id"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx, port, bad_acc)

    # Mismatched cash bucket
    bad_bucket = CashBucket(id=uuid4(), portfolio_id=other_p_id, name="Bad", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)
    with pytest.raises(ValueError, match="CashBucket portfolio_id"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx, port, acc, bad_bucket)


def test_goal_and_contribution_consistency():
    p_id = uuid4()
    other_p_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    port = Portfolio(id=p_id, mode=PortfolioMode.MY_PORTFOLIO, name="Real", base_currency=Currency.TRY, created_at=now)
    goal = InvestmentGoal(id=uuid4(), portfolio_id=p_id, name="Goal 1", target_amount=Decimal("10000"), target_currency=Currency.TRY, created_at=now)

    PortfolioLedgerValidator.validate_goal_consistency(goal, port)

    # Mismatched goal portfolio
    bad_goal = InvestmentGoal(id=uuid4(), portfolio_id=other_p_id, name="Bad", target_amount=Decimal("10000"), target_currency=Currency.TRY, created_at=now)
    with pytest.raises(ValueError, match="Goal portfolio_id"):
        PortfolioLedgerValidator.validate_goal_consistency(bad_goal, port)

    # Contribution consistency
    contrib = PlannedContribution(id=uuid4(), portfolio_id=p_id, goal_id=goal.id, expected_date=date(2026, 9, 1), amount=Decimal("1000"), currency=Currency.TRY, created_at=now)
    PortfolioLedgerValidator.validate_contribution_consistency(contrib, port, goal=goal)

    # Mismatched goal_id on same portfolio
    other_goal_same_port = InvestmentGoal(id=uuid4(), portfolio_id=p_id, name="Other Goal", target_amount=Decimal("20000"), target_currency=Currency.TRY, created_at=now)
    with pytest.raises(ValueError, match="Contribution goal_id"):
        PortfolioLedgerValidator.validate_contribution_consistency(contrib, port, goal=other_goal_same_port)



# ─────────────────────────────────────────────────────────────────────────────
# 5. Deterministic Audit Order Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_deterministic_audit_sorting():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(portfolio_id=p_id)

    t1 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 10))
    t2 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), exec_at=datetime(2026, 8, 1, 14, 0, 0, tzinfo=timezone.utc))
    t3 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), exec_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc))

    # Append out of order
    ledger.append(t1)
    ledger.append(t2)
    ledger.append(t3)

    sorted_txs = ledger.list_transactions()
    assert [t.id for t in sorted_txs] == [t3.id, t2.id, t1.id]
