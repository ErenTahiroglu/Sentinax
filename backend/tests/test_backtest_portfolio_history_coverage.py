"""
backend/tests/test_backtest_portfolio_history_coverage.py
=========================================================
Phase 26C2B2B2: trusted Python transport for the atomic portfolio-history coverage snapshot RPC and its exact reconciliation against the C2B1 canonical projection
(physical id + recorded_at epoch microseconds + recomputed economic fingerprint). Exactly one RPC, no table read, no retry. Deterministic client double; no network.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.engine.private import backtest_portfolio_history_coverage as module_under_test
from backend.engine.private.backtest_portfolio_history_coverage import (
    PrivateBacktestPortfolioHistoryCoverage,
    PrivateBacktestPortfolioHistoryCoverageMismatchError,
    PrivateBacktestPortfolioHistoryCoverageRepository,
)
from backend.engine.private.backtest_portfolio_projection import PrivateBacktestPortfolioProjectionBinding
from backend.engine.private.domain import TransactionType
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_portfolio_projection import T0, T1, T2, analysis, bind, buy
from backend.tests.test_portfolio_projection import _make_portfolio, _make_tx

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
OWNER = UUID(int=77)
RPC = "get_portfolio_transaction_history_coverage_snapshot"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FakeClient:
    def __init__(self, data=None) -> None:
        self.data = data
        self.rpc_calls: list[tuple[str, dict]] = []
        self.table_calls = 0
        self.error: Exception | None = None

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        outer = self

        class Call:
            def execute(self):
                if outer.error is not None:
                    raise outer.error
                return SimpleNamespace(data=outer.data)
        return Call()

    def table(self, name):
        self.table_calls += 1
        raise AssertionError("direct table access")


def micros(value: datetime) -> int:
    return (value.astimezone(UTC) - EPOCH) // timedelta(microseconds=1)


def row_for(binding: PrivateBacktestPortfolioProjectionBinding, owner=OWNER, **overrides) -> dict:
    known = binding.projection.known_transactions
    cutoff = binding.analysis_context.replay_point.portfolio_recorded_cutoff
    row = {
        "owner_id": str(owner), "portfolio_id": str(binding.projection.portfolio_id), "as_of_recorded_at": cutoff.astimezone(UTC).isoformat(),
        "observed_at": (cutoff.astimezone(UTC) + timedelta(seconds=5)).isoformat(), "transaction_count": len(known),
        "transaction_ids": [str(tx.id) for tx in known], "recorded_at_epoch_micros": [micros(tx.recorded_at) for tx in known],
        "economic_fingerprints": [tx.economic_fingerprint() for tx in known],
    }
    row.update(overrides)
    return row


def verify(binding, row=None, data="row", owner=OWNER):
    client = FakeClient([row_for(binding)] if data == "row" and row is None else ([row] if data == "row" else data))
    repository = PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=owner)
    return repository, client, repository.verify_projection_history(projection_binding=binding)


def history(portfolio=None, count=3):
    portfolio = portfolio or _make_portfolio()
    account = uuid4()
    txs = tuple(buy(portfolio, account, T0 + timedelta(minutes=i), quantity=None) for i in range(count))
    return portfolio, account, txs


def transport_error(binding, row=None, data="row"):
    client = FakeClient([row_for(binding)] if data == "row" and row is None else ([row] if data == "row" else data))
    repository = PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=OWNER)
    with pytest.raises(RuntimeError) as info:
        repository.verify_projection_history(projection_binding=binding)
    assert type(info.value) is RuntimeError, type(info.value)                                    # malformed transport, not a history mismatch
    assert len(client.rpc_calls) == 1


def mismatch_error(binding, row):
    client = FakeClient([row])
    repository = PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=OWNER)
    with pytest.raises(PrivateBacktestPortfolioHistoryCoverageMismatchError):
        repository.verify_projection_history(projection_binding=binding)
    assert len(client.rpc_calls) == 1


def sample_binding(cutoff=T1):
    portfolio, account, txs = history()
    return bind(analysis(cutoff=cutoff), portfolio, txs), portfolio, account, txs


# --- contracts -----------------------------------------------------------------------------------------------------

def test_mismatch_error_type_and_message_hygiene() -> None:
    assert issubclass(PrivateBacktestPortfolioHistoryCoverageMismatchError, RuntimeError)
    binding, *_ = sample_binding(T2)
    bad = row_for(binding)
    bad["economic_fingerprints"][0] = "f" * 64
    client = FakeClient([bad])
    with pytest.raises(PrivateBacktestPortfolioHistoryCoverageMismatchError) as info:
        PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=OWNER).verify_projection_history(projection_binding=binding)
    message = str(info.value)
    assert "f" * 64 not in message and str(binding.projection.known_transactions[0].id) not in message and message


def test_coverage_stored_shape_and_construction_trust_boundary() -> None:
    fields = dataclasses.fields(PrivateBacktestPortfolioHistoryCoverage)
    assert [f.name for f in fields] == ["projection_binding", "owner_id", "observed_at"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert PrivateBacktestPortfolioHistoryCoverage.__dataclass_params__.frozen is True
    binding, *_ = sample_binding()
    observed = datetime(2026, 8, 11, tzinfo=UTC)
    with pytest.raises(TypeError):
        PrivateBacktestPortfolioHistoryCoverage(projection_binding=binding, owner_id=OWNER, observed_at=observed)       # no private capability
    with pytest.raises(TypeError):
        PrivateBacktestPortfolioHistoryCoverage(projection_binding=binding, owner_id=OWNER, observed_at=observed, capability=object())
    assert not hasattr(module_under_test, "build_private_backtest_portfolio_history_coverage")
    capability = module_under_test._VERIFIED_COVERAGE_CAPABILITY
    good = PrivateBacktestPortfolioHistoryCoverage(projection_binding=binding, owner_id=OWNER, observed_at=observed, capability=capability)
    assert good.projection_binding is binding
    for kwargs in (dict(projection_binding=None), dict(projection_binding=binding.projection), dict(owner_id=str(OWNER)), dict(owner_id=None), dict(observed_at="x"),
                   dict(observed_at=datetime(2026, 8, 11))):
        values = dict(projection_binding=binding, owner_id=OWNER, observed_at=observed, capability=capability)
        values.update(kwargs)
        with pytest.raises(TypeError):
            PrivateBacktestPortfolioHistoryCoverage(**values)
    with pytest.raises(ValueError):
        PrivateBacktestPortfolioHistoryCoverage(projection_binding=binding, owner_id=OWNER, capability=capability,
                                                observed_at=binding.analysis_context.replay_point.knowledge_cutoff_utc - timedelta(microseconds=1))
    assert not any(hasattr(good, name) for name in ("transaction_ids", "projection", "fingerprints", "recorded_at_epoch_micros"))


def test_repository_signatures_and_exact_owner() -> None:
    parameters = list(inspect.signature(PrivateBacktestPortfolioHistoryCoverageRepository.__init__).parameters.values())[1:]
    assert [p.name for p in parameters] == ["client", "owner_id"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    verify_parameters = list(inspect.signature(PrivateBacktestPortfolioHistoryCoverageRepository.verify_projection_history).parameters.values())[1:]
    assert [p.name for p in verify_parameters] == ["projection_binding"] and verify_parameters[0].kind is inspect.Parameter.KEYWORD_ONLY

    class SubUUID(UUID):
        pass

    owner = UUID(int=5)
    repository = PrivateBacktestPortfolioHistoryCoverageRepository(client=FakeClient(), owner_id=owner)
    assert repository.owner_id is owner
    for bad in (str(owner), SubUUID(int=5), None, True, 5, object(), owner.hex):
        with pytest.raises(TypeError):
            PrivateBacktestPortfolioHistoryCoverageRepository(client=FakeClient(), owner_id=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PrivateBacktestPortfolioHistoryCoverageRepository(client=None, owner_id=owner)
    binding, *_ = sample_binding()
    for bad in (None, object(), binding.projection, binding.transactions, "binding"):
        client = FakeClient()
        with pytest.raises(TypeError):
            PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=owner).verify_projection_history(projection_binding=bad)  # type: ignore[arg-type]
        assert client.rpc_calls == []


# --- success paths -------------------------------------------------------------------------------------------------

def test_exact_rpc_name_and_parameters_one_call_no_table() -> None:
    binding, *_ = sample_binding()
    repository, client, coverage = verify(binding)
    cutoff = binding.analysis_context.replay_point.portfolio_recorded_cutoff
    assert client.rpc_calls == [(RPC, {"p_owner_id": str(OWNER), "p_portfolio_id": str(binding.projection.portfolio_id),
                                       "p_as_of_recorded_at": cutoff.isoformat(timespec="microseconds")})]
    assert client.table_calls == 0
    assert type(coverage) is PrivateBacktestPortfolioHistoryCoverage and coverage.projection_binding is binding and coverage.owner_id is repository.owner_id
    assert coverage.portfolio_id == binding.projection.portfolio_id and coverage.as_of_recorded_at is binding.projection.as_of_recorded_at
    assert coverage.transaction_count == len(binding.projection.known_transactions) == 3                 # all three history rows were recorded before the T1 cutoff


def test_non_utc_cutoff_and_equivalent_offset_echo() -> None:
    cutoff = datetime(2026, 8, 10, 15, 0, tzinfo=PLUS3)                                              # 12:00 UTC == T1
    portfolio, account, txs = history()
    binding = bind(analysis(cutoff=cutoff), portfolio, txs)
    for echo in (cutoff.astimezone(UTC).isoformat(), cutoff.isoformat(), "2026-08-10T12:00:00Z", "2026-08-10T12:00:00.000000+00:00"):
        _, client, coverage = verify(binding, row_for(binding, as_of_recorded_at=echo))
        assert coverage.as_of_recorded_at is cutoff and client.rpc_calls[0][1]["p_as_of_recorded_at"] == cutoff.isoformat(timespec="microseconds")
    with pytest.raises(RuntimeError):
        verify(binding, row_for(binding, as_of_recorded_at="2026-08-10T12:00:00.000001+00:00"))
    with pytest.raises(RuntimeError):
        verify(binding, row_for(binding, as_of_recorded_at="2026-08-10T11:59:59.999999+00:00"))


def test_observed_at_validation_and_representation() -> None:
    binding, *_ = sample_binding()
    cutoff = binding.analysis_context.replay_point.knowledge_cutoff_utc
    for observed in (cutoff, cutoff + timedelta(days=1)):
        _, _, coverage = verify(binding, row_for(binding, observed_at=observed.astimezone(PLUS3).isoformat()))
        assert coverage.observed_at.utcoffset() == timedelta(hours=3) and coverage.observed_at == observed                # stored as parsed, UTC only for comparison
    transport_error(binding, row_for(binding, observed_at=(cutoff - timedelta(microseconds=1)).isoformat()))


def test_empty_history() -> None:
    binding = bind(analysis(), _make_portfolio(), ())
    _, _, coverage = verify(binding)
    assert coverage.transaction_count == 0 and binding.projection.known_transactions == ()


def test_several_transactions_and_caller_order_do_not_matter() -> None:
    portfolio, account, txs = history(count=5)
    shuffled = (txs[3], txs[0], txs[4], txs[2], txs[1])
    binding = bind(analysis(cutoff=T2), portfolio, shuffled)
    assert binding.transactions is shuffled and binding.projection.known_transactions == txs
    _, _, coverage = verify(binding)
    assert coverage.transaction_count == 5


def test_future_transactions_are_excluded_from_the_comparison() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    a, b = buy(portfolio, account, T0), buy(portfolio, account, T2)
    binding = bind(analysis(cutoff=T1), portfolio, (a, b))
    assert binding.transactions == (a, b) and binding.projection.known_transactions == (a,)
    row = row_for(binding)
    assert row["transaction_ids"] == [str(a.id)]
    verify(binding, row)
    mismatch_error(binding, row_for(bind(analysis(cutoff=T2), portfolio, (a, b))) | {"as_of_recorded_at": row["as_of_recorded_at"]})        # a manifest that wrongly includes the future row


def test_future_reversal_is_excluded_at_an_earlier_cutoff_and_included_later() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    base = buy(portfolio, account, T0)
    reversal = _make_tx(portfolio.id, account, tx_type=TransactionType.REVERSAL, recorded_at=T2, reverses_tx_id=base.id)
    early = bind(analysis(cutoff=T1), portfolio, (base, reversal))
    assert row_for(early)["transaction_ids"] == [str(base.id)]
    verify(early)
    late = bind(analysis(cutoff=T2), portfolio, (base, reversal))
    assert set(row_for(late)["transaction_ids"]) == {str(base.id), str(reversal.id)}
    verify(late)


# --- history mismatches --------------------------------------------------------------------------------------------

def test_four_mismatch_classes_with_internally_valid_payloads() -> None:
    binding, *_ = sample_binding(T2)
    assert len(binding.projection.known_transactions) == 3
    wrong_fp = row_for(binding)
    wrong_fp["economic_fingerprints"][1] = "e" * 64
    mismatch_error(binding, wrong_fp)
    wrong_id = row_for(binding)
    wrong_id["transaction_ids"][2] = str(uuid4())
    mismatch_error(binding, wrong_id)
    wrong_time = row_for(binding)
    wrong_time["recorded_at_epoch_micros"][0] += 1
    mismatch_error(binding, wrong_time)
    shorter = row_for(binding)
    for key in ("transaction_ids", "recorded_at_epoch_micros", "economic_fingerprints"):
        shorter[key] = shorter[key][:-1]
    shorter["transaction_count"] = 2
    mismatch_error(binding, shorter)                                                                      # consistent but a different valid manifest
    longer = row_for(binding)
    longer["transaction_ids"].append(str(uuid4())); longer["recorded_at_epoch_micros"].append(micros(T2)); longer["economic_fingerprints"].append("a" * 64)
    longer["transaction_count"] = 4
    mismatch_error(binding, longer)                                                                       # a concurrent backdated writer: valid manifest, extra row
    reordered = row_for(binding)
    for key in ("transaction_ids", "recorded_at_epoch_micros", "economic_fingerprints"):
        reordered[key] = list(reversed(reordered[key]))
    mismatch_error(binding, reordered)                                                                    # same members in a different order


def test_empty_projection_against_a_non_empty_manifest_is_a_mismatch() -> None:
    binding = bind(analysis(), _make_portfolio(), ())
    row = row_for(binding, transaction_count=1, transaction_ids=[str(uuid4())], recorded_at_epoch_micros=[micros(T0)], economic_fingerprints=["a" * 64])
    mismatch_error(binding, row)


def test_expected_epoch_micros_are_exact_including_pre_1970_and_sub_second() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    odd = datetime(1969, 12, 31, 23, 59, 59, 123457, tzinfo=UTC)
    tx = buy(portfolio, account, odd)
    binding = bind(analysis(cutoff=T1), portfolio, (tx,))
    row = row_for(binding)
    assert row["recorded_at_epoch_micros"] == [-876543]
    verify(binding, row)
    plus3_tx = buy(portfolio, account, datetime(2026, 8, 5, 3, 0, 0, 999999, tzinfo=PLUS3))
    binding = bind(analysis(cutoff=T1), portfolio, (plus3_tx,))
    verify(binding)
    assert row_for(binding)["recorded_at_epoch_micros"] == [micros(plus3_tx.recorded_at)]


# --- malformed transport -------------------------------------------------------------------------------------------

def test_malformed_transport_matrix() -> None:
    binding, *_ = sample_binding(T2)
    good = row_for(binding)

    class DictSub(dict):
        pass

    class ListSub(list):
        pass

    for data in (None, {}, [], [good, good], "x", [None], [DictSub(good)], [[1]], (good,)):
        transport_error(binding, data=data)
    for key in good:
        transport_error(binding, {k: v for k, v in good.items() if k != key})
    transport_error(binding, {**good, "extra": 1})
    ids = good["transaction_ids"]
    cases = [
        {"owner_id": OWNER}, {"owner_id": str(OWNER).upper()}, {"owner_id": "{" + str(OWNER) + "}"}, {"owner_id": OWNER.hex}, {"owner_id": None}, {"owner_id": str(UUID(int=1))},
        {"portfolio_id": binding.projection.portfolio_id}, {"portfolio_id": str(uuid4())}, {"portfolio_id": 5},
        {"as_of_recorded_at": None}, {"as_of_recorded_at": 5}, {"as_of_recorded_at": "2026-08-10T12:00:00"}, {"as_of_recorded_at": "garbage"},
        {"as_of_recorded_at": datetime(2026, 8, 10, 12, tzinfo=UTC)}, {"observed_at": None}, {"observed_at": "2026-08-20T12:00:00"}, {"observed_at": "nope"},
        {"observed_at": 5},
        {"transaction_count": True}, {"transaction_count": 3.0}, {"transaction_count": "3"}, {"transaction_count": -1}, {"transaction_count": None},
        {"transaction_count": 2}, {"transaction_count": 4},
        {"transaction_ids": tuple(ids)}, {"transaction_ids": ListSub(ids)}, {"transaction_ids": None}, {"transaction_ids": "abc"}, {"transaction_ids": {"a": 1}},
        {"transaction_ids": ids[:-1]}, {"recorded_at_epoch_micros": good["recorded_at_epoch_micros"][:-1]}, {"economic_fingerprints": good["economic_fingerprints"][:-1]},
        {"transaction_ids": [UUID(i) for i in ids]}, {"transaction_ids": [i.upper() for i in ids]}, {"transaction_ids": [i.replace("-", "") for i in ids]},
        {"transaction_ids": ["{" + i + "}" for i in ids]}, {"transaction_ids": [ids[0], ids[0], ids[2]]}, {"transaction_ids": [ids[0], ids[1], 5]},
        {"recorded_at_epoch_micros": [True, 1, 2]}, {"recorded_at_epoch_micros": [1.0, 2, 3]}, {"recorded_at_epoch_micros": ["1", 2, 3]},
        {"recorded_at_epoch_micros": tuple(good["recorded_at_epoch_micros"])}, {"recorded_at_epoch_micros": None},
        {"economic_fingerprints": ["a" * 63, "b" * 64, "c" * 64]}, {"economic_fingerprints": ["A" * 64, "b" * 64, "c" * 64]}, {"economic_fingerprints": ["g" * 64, "b" * 64, "c" * 64]},
        {"economic_fingerprints": [5, "b" * 64, "c" * 64]}, {"economic_fingerprints": tuple(good["economic_fingerprints"])}, {"economic_fingerprints": None},
    ]
    for override in cases:
        transport_error(binding, {**good, **override})


def test_negative_epoch_micros_are_valid_transport() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    tx = buy(portfolio, account, datetime(1960, 1, 1, tzinfo=UTC))
    binding = bind(analysis(cutoff=T1), portfolio, (tx,))
    assert row_for(binding)["recorded_at_epoch_micros"][0] < 0
    verify(binding)


def test_execute_exception_propagates_without_retry() -> None:
    binding, *_ = sample_binding()
    client = FakeClient()
    client.error = RuntimeError("transport unavailable")
    with pytest.raises(RuntimeError) as info:
        PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=OWNER).verify_projection_history(projection_binding=binding)
    assert str(info.value) == "transport unavailable" and len(client.rpc_calls) == 1 and client.table_calls == 0


def test_wrong_owner_in_the_repository_versus_the_rpc_echo_is_transport_failure() -> None:
    binding, *_ = sample_binding()
    client = FakeClient([row_for(binding, owner=UUID(int=1))])
    with pytest.raises(RuntimeError) as info:
        PrivateBacktestPortfolioHistoryCoverageRepository(client=client, owner_id=OWNER).verify_projection_history(projection_binding=binding)
    assert type(info.value) is RuntimeError


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_portfolio_history_coverage.py"


def test_module_is_deliberately_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_imports_rpc_literal_and_forbidden_surfaces() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules == {"backend.engine.private.backtest_portfolio_projection"}
    imported_names = {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names}
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)} | imported_names
    assert not names & {"table", "from_", "range", "offset", "limit", "PAGE_SIZE", "list_transactions", "PortfolioRepository", "build_ledger_projection_view",
                        "now", "utcnow", "today", "time", "sleep", "uuid4", "random", "secrets", "urandom", "hashlib", "sha256", "timestamp", "float",
                        "sorted", "sort", "rebalance", "optimizer"}
    rpc_calls = [n for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "rpc"]
    assert len(rpc_calls) == 1 and isinstance(rpc_calls[0].args[0], ast.Constant) and rpc_calls[0].args[0].value == RPC
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await))]
    assert "economic_fingerprint" in names and "known_transactions" in names
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Attribute) and n.attr == "transactions" and isinstance(n.value, ast.Name) and n.value.id == "projection_binding"]


def test_documentation_states_the_boundaries() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_PORTFOLIO_HISTORY_COVERAGE.md").read_text(encoding="utf-8")
    for needle in ("known_transactions", "economic fingerprint", "observed_at", "no retry", "mismatch", "malformed", "not permanent", "trusted", "cryptographic", "C2B2B3"):
        assert needle in doc, needle
