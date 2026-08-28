"""
backend/tests/test_portfolio_ledger.py
======================================
Comprehensive In-Memory Portfolio Ledger, Reversal & Validator Tests (Phase 12A, 12A.5 & 12A.6).

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


def _make_portfolio(mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO, p_id: UUID | None = None) -> Portfolio:
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    return Portfolio(
        id=p_id or uuid4(),
        mode=mode,
        name="Test Portföy",
        base_currency=Currency.TRY,
        created_at=now,
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
    cash_bucket_id: UUID | None = None,
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
        cash_bucket_id=cash_bucket_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Append & List Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_append_valid_transactions_and_listing():
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

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
    port = _make_portfolio()
    other_p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

    tx = _make_buy(other_p_id, a_id, inst_id, date(2026, 8, 1))
    res = ledger.append(tx)
    assert res.status == AppendStatus.INVALID
    assert "does not match ledger" in res.diagnostics[0]
    assert len(ledger) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. External Idempotency & Conflict Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_external_idempotency_safe_replay():
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

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


def test_external_idempotency_decimal_lexical_variation():
    """Phase 12B.2A.5: Replaying external transaction with different Decimal lexical form (1.00 vs 1, 1000.00 vs 1E+3) is IDEMPOTENT_DUPLICATE."""
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

    # Initial append with standard decimal string
    tx1 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        qty="1.00", price="1000.00",
        ext_source="MIDAS", ext_ref="ORD-2001",
    )
    r1 = ledger.append(tx1)
    assert r1.status == AppendStatus.APPENDED

    # Replay with integer / exponent representation
    tx2 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        qty="1", price="1E+3",
        ext_source="MIDAS", ext_ref="ORD-2001",
    )
    r2 = ledger.append(tx2)
    assert r2.status == AppendStatus.IDEMPOTENT_DUPLICATE
    assert r2.transaction_id == tx1.id
    assert len(ledger) == 1

    # Conflict test: replay with genuine numeric difference
    tx3 = _make_buy(
        p_id, a_id, inst_id, date(2026, 8, 1),
        qty="1.01", price="1000.00",
        ext_source="MIDAS", ext_ref="ORD-2001",
    )
    r3 = ledger.append(tx3)
    assert r3.status == AppendStatus.CONFLICT
    assert len(ledger) == 1



def test_external_idempotency_conflict_detection():
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

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


def test_manual_events_no_economic_auto_dedupe():
    """Phase 12A.6: Two manual transactions with identical economics are BOTH appended."""
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

    tx1 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), qty="10", price="100.00")
    tx2 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), qty="10", price="100.00")

    r1 = ledger.append(tx1)
    assert r1.status == AppendStatus.APPENDED

    r2 = ledger.append(tx2)
    assert r2.status == AppendStatus.APPENDED
    assert len(ledger) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reversal & Correction Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_valid_reversal_workflow():
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)
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


def test_reversal_external_idempotency_and_double_reversal():
    """Phase 12A.6: Replay of external reversal is IDEMPOTENT_DUPLICATE; distinct 2nd reversal is INVALID."""
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Original BUY
    buy = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), ext_source="MIDAS", ext_ref="ORD-1")
    ledger.append(buy)

    # 2. Reversal with external reference
    rev1 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=buy.id,
        external_source="MIDAS",
        external_reference="REV-1",
    )
    r1 = ledger.append(rev1)
    assert r1.status == AppendStatus.APPENDED
    assert r1.transaction_id == rev1.id

    # 3. Replay exact same external reversal event -> IDEMPOTENT_DUPLICATE
    rev1_replay = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=datetime(2026, 8, 27, 15, 0, 0, tzinfo=timezone.utc),
        reverses_transaction_id=buy.id,
        external_source="MIDAS",
        external_reference="REV-1",
    )
    r1_replay = ledger.append(rev1_replay)
    assert r1_replay.status == AppendStatus.IDEMPOTENT_DUPLICATE
    assert r1_replay.transaction_id == rev1.id
    assert len(ledger) == 2

    # 4. A genuinely different second reversal (e.g. REV-2) targeting same BUY -> INVALID (Double reversal)
    rev2 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 3),
        recorded_at=now,
        reverses_transaction_id=buy.id,
        external_source="MIDAS",
        external_reference="REV-2",
    )
    r2 = ledger.append(rev2)
    assert r2.status == AppendStatus.INVALID
    assert "Double reversal rejected" in r2.diagnostics[0]
    assert len(ledger) == 2

    # 5. Same external key with changed economics (e.g. targeting different transaction) -> CONFLICT
    other_buy = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), ext_source="MIDAS", ext_ref="ORD-2")
    ledger.append(other_buy)

    rev_conflict = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 2),
        recorded_at=now,
        reverses_transaction_id=other_buy.id,  # Different target
        external_source="MIDAS",
        external_reference="REV-1",  # Same external ref as rev1
    )
    r_conflict = ledger.append(rev_conflict)
    assert r_conflict.status == AppendStatus.CONFLICT


def test_reversal_target_not_found():
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    ledger = PortfolioLedger(port)
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
    port_a = _make_portfolio()
    port_b = _make_portfolio()
    p_id_a = port_a.id
    p_id_b = port_b.id
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    ledger_b = PortfolioLedger(port_b)

    # Transaction in A
    buy_a = _make_buy(p_id_a, a_id, inst_id, date(2026, 8, 1))

    # Mock insert into ledger_b's dict for isolated cross-check
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
    port = _make_portfolio()
    p_id = port.id
    a_id_1 = uuid4()
    a_id_2 = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    ledger = PortfolioLedger(port)

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
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    ledger = PortfolioLedger(port)

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
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    ledger = PortfolioLedger(port)

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
# 4. Cross-Entity Validator Consistency Tests (Phase 12A.6)
# ─────────────────────────────────────────────────────────────────────────────

def test_cross_entity_consistency_validator_success_and_failures():
    p_id = uuid4()
    other_p_id = uuid4()
    a_id = uuid4()
    other_a_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    port = Portfolio(id=p_id, mode=PortfolioMode.MY_PORTFOLIO, name="Real", base_currency=Currency.TRY, created_at=now)
    acc = PortfolioAccount(id=a_id, portfolio_id=p_id, name="Acc", base_currency=Currency.TRY, created_at=now)
    bucket = CashBucket(id=uuid4(), portfolio_id=p_id, account_id=a_id, name="TL", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)

    # 1. Valid BUY referencing bucket with exact match and matching currency
    tx_with_bucket = _make_buy(p_id, a_id, uuid4(), date(2026, 8, 1), curr=Currency.TRY, cash_bucket_id=bucket.id)
    PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_with_bucket, port, acc, bucket)

    # 2. Valid BUY with cash_bucket_id=None and cash_bucket=None
    tx_no_bucket = _make_buy(p_id, a_id, uuid4(), date(2026, 8, 1), curr=Currency.TRY, cash_bucket_id=None)
    PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_no_bucket, port, acc, None)

    # 3. Transaction has cash_bucket_id but bucket object is omitted (None) -> rejected
    with pytest.raises(ValueError, match="no cash_bucket object was supplied"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_with_bucket, port, acc, None)

    # 4. Transaction references bucket A, but bucket B is supplied -> rejected
    other_bucket = CashBucket(id=uuid4(), portfolio_id=p_id, account_id=a_id, name="TL 2", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)
    with pytest.raises(ValueError, match="Supplied CashBucket.id"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_with_bucket, port, acc, other_bucket)

    # 5. Transaction has cash_bucket_id=None but unrelated bucket is supplied -> rejected
    with pytest.raises(ValueError, match="has no cash_bucket_id, but an unrelated cash_bucket was supplied"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_no_bucket, port, acc, bucket)

    # 6. Cross-portfolio bucket -> rejected
    bad_p_bucket = CashBucket(id=bucket.id, portfolio_id=other_p_id, account_id=a_id, name="Bad", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)
    with pytest.raises(ValueError, match="CashBucket portfolio_id"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_with_bucket, port, acc, bad_p_bucket)

    # 7. Wrong account-scoped bucket -> rejected
    bad_a_bucket = CashBucket(id=bucket.id, portfolio_id=p_id, account_id=other_a_id, name="Bad", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)
    with pytest.raises(ValueError, match="CashBucket account_id"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_with_bucket, port, acc, bad_a_bucket)

    # 8. Portfolio-wide bucket (account_id=None) -> accepted
    global_bucket = CashBucket(id=uuid4(), portfolio_id=p_id, account_id=None, name="Global TL", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)
    tx_global_bucket = _make_buy(p_id, a_id, uuid4(), date(2026, 8, 1), curr=Currency.TRY, cash_bucket_id=global_bucket.id)
    PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_global_bucket, port, acc, global_bucket)

    # 9. Mismatched account
    bad_acc = PortfolioAccount(id=other_a_id, portfolio_id=other_p_id, name="Bad", base_currency=Currency.TRY, created_at=now)
    with pytest.raises(ValueError, match="Account portfolio_id"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(tx_no_bucket, port, bad_acc)


def test_validator_cash_bucket_currency_consistency():
    """Phase 12A.6: CashBucket currency must match relevant trade_currency or cash_currency."""
    p_id = uuid4()
    a_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    port = Portfolio(id=p_id, mode=PortfolioMode.MY_PORTFOLIO, name="Real", base_currency=Currency.TRY, created_at=now)
    acc = PortfolioAccount(id=a_id, portfolio_id=p_id, name="Acc", base_currency=Currency.TRY, created_at=now)

    try_bucket = CashBucket(id=uuid4(), portfolio_id=p_id, account_id=a_id, name="TL", currency=Currency.TRY, purpose=CashPurpose.INVESTABLE, created_at=now)
    usd_bucket = CashBucket(id=uuid4(), portfolio_id=p_id, account_id=a_id, name="USD", currency=Currency.USD, purpose=CashPurpose.INVESTABLE, created_at=now)

    # 1. CASH_DEPOSIT in USD referencing TRY bucket -> rejected
    deposit_usd = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.CASH_DEPOSIT,
        effective_date=date(2026, 8, 27),
        recorded_at=now,
        cash_amount=Decimal("1000.00"),
        cash_currency=Currency.USD,
        cash_bucket_id=try_bucket.id,
    )
    with pytest.raises(ValueError, match="Referenced CashBucket currency"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(deposit_usd, port, acc, try_bucket)

    # Valid USD deposit referencing USD bucket -> passes
    deposit_usd_valid = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.CASH_DEPOSIT,
        effective_date=date(2026, 8, 27),
        recorded_at=now,
        cash_amount=Decimal("1000.00"),
        cash_currency=Currency.USD,
        cash_bucket_id=usd_bucket.id,
    )
    PortfolioLedgerValidator.validate_transaction_portfolio_consistency(deposit_usd_valid, port, acc, usd_bucket)

    # 2. BUY in USD referencing TRY funding bucket -> rejected
    buy_usd_bad_bucket = _make_buy(p_id, a_id, uuid4(), date(2026, 8, 27), curr=Currency.USD, cash_bucket_id=try_bucket.id)
    with pytest.raises(ValueError, match="Referenced funding CashBucket currency"):
        PortfolioLedgerValidator.validate_transaction_portfolio_consistency(buy_usd_bad_bucket, port, acc, try_bucket)

    # Valid BUY in USD referencing USD funding bucket -> passes
    buy_usd_valid = _make_buy(p_id, a_id, uuid4(), date(2026, 8, 27), curr=Currency.USD, cash_bucket_id=usd_bucket.id)
    PortfolioLedgerValidator.validate_transaction_portfolio_consistency(buy_usd_valid, port, acc, usd_bucket)


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
    port = _make_portfolio()
    p_id = port.id
    a_id = uuid4()
    inst_id = uuid4()
    ledger = PortfolioLedger(port)

    t1 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 10))
    t2 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), exec_at=datetime(2026, 8, 1, 14, 0, 0, tzinfo=timezone.utc))
    t3 = _make_buy(p_id, a_id, inst_id, date(2026, 8, 1), exec_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc))

    # Append out of order
    ledger.append(t1)
    ledger.append(t2)
    ledger.append(t3)

    sorted_txs = ledger.list_transactions()
    assert [t.id for t in sorted_txs] == [t3.id, t2.id, t1.id]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Root Mode Binding & Sandbox Cross-Contamination Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_ledger_root_mode_binding():
    real_port = _make_portfolio(mode=PortfolioMode.MY_PORTFOLIO)
    real_ledger = PortfolioLedger(real_port)
    assert real_ledger.portfolio_id == real_port.id
    assert real_ledger.mode == PortfolioMode.MY_PORTFOLIO
    assert real_ledger.portfolio == real_port

    sandbox_port = _make_portfolio(mode=PortfolioMode.SANDBOX)
    sandbox_ledger = PortfolioLedger(sandbox_port)
    assert sandbox_ledger.portfolio_id == sandbox_port.id
    assert sandbox_ledger.mode == PortfolioMode.SANDBOX

    with pytest.raises(TypeError, match="must be an instance of Portfolio"):
        PortfolioLedger("not_a_portfolio")  # type: ignore

    with pytest.raises(TypeError, match="must be an instance of Portfolio"):
        PortfolioLedger(real_port.id)  # type: ignore


def test_sandbox_cross_contamination_isolation():
    real_port = _make_portfolio(mode=PortfolioMode.MY_PORTFOLIO)
    real_ledger = PortfolioLedger(real_port)

    sandbox_port = _make_portfolio(mode=PortfolioMode.SANDBOX)
    sandbox_ledger = PortfolioLedger(sandbox_port)

    a_id = uuid4()
    inst_id = uuid4()

    real_tx = _make_buy(real_port.id, a_id, inst_id, date(2026, 8, 1))
    sandbox_tx = _make_buy(sandbox_port.id, a_id, inst_id, date(2026, 8, 1))

    # Append real tx to sandbox ledger -> INVALID
    res_sandbox = sandbox_ledger.append(real_tx)
    assert res_sandbox.status == AppendStatus.INVALID
    assert len(sandbox_ledger) == 0

    # Append sandbox tx to real ledger -> INVALID
    res_real = real_ledger.append(sandbox_tx)
    assert res_real.status == AppendStatus.INVALID
    assert len(real_ledger) == 0
