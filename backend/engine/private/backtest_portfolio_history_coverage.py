"""
backend/engine/private/backtest_portfolio_history_coverage.py
=============================================================
Trusted Python transport and exact reconciliation for portfolio-history coverage (Phase 26C2B2B2). It connects three layers: C2B1 (the canonical projection of the
SUPPLIED history), C2B2B1 (the atomic database manifest RPC `get_portfolio_transaction_history_coverage_snapshot`) and this module (their exact reconciliation).

`PrivateBacktestPortfolioHistoryCoverageRepository.verify_projection_history` makes exactly ONE RPC call (no table read, no pagination, no retry; an execute error
propagates), strictly parses the response (any malformed or self-contradictory transport is a plain RuntimeError) and then requires the database manifest to equal
the projection's `known_transactions` (the closed `recorded_at <= cutoff` authority, in its existing canonical order; the full supplied transaction tuple is NOT the
comparison authority because it may hold rows recorded after the cutoff) on three dimensions: physical id, recorded_at as exact signed epoch microseconds (integer
arithmetic, no float) and the economic fingerprint recomputed now by `PortfolioTransaction.economic_fingerprint()` (never a cached or transport value). A valid but
different manifest, for example a concurrent trusted writer's backdated row, is `PrivateBacktestPortfolioHistoryCoverageMismatchError`; it is never hidden, retried or
rebuilt: the caller owns any retry policy. The projection is never rebuilt here.

The verified `PrivateBacktestPortfolioHistoryCoverage` stores only the exact projection binding, the explicit owner and the database `observed_at` (the clock authority,
kept as parsed and used as UTC only for comparison). It can be constructed only with a module-private capability held by this adapter's reconciled path: an
application trust boundary, not cryptographic provenance. A verified coverage is valid as observed at `observed_at`; it is not permanent finality and relies on the
trusted service-role write boundary. No clock, randomness, new hashing, owner inference, decision or execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from backend.engine.private.backtest_portfolio_projection import PrivateBacktestPortfolioProjectionBinding

_ROW_KEYS = frozenset({"owner_id", "portfolio_id", "as_of_recorded_at", "observed_at", "transaction_count", "transaction_ids", "recorded_at_epoch_micros",
                       "economic_fingerprints"})
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MICROSECOND = timedelta(microseconds=1)
_VERIFIED_COVERAGE_CAPABILITY = object()

_ERR_CLIENT = "client must not be None"
_ERR_OWNER = "owner_id must be an exact UUID instance"
_ERR_BINDING = "projection_binding must be an exact PrivateBacktestPortfolioProjectionBinding instance"
_ERR_CAPABILITY = "verified coverage can only be created by the reconciled RPC path"
_ERR_OBSERVED_TYPE = "observed_at must be an exact timezone-aware datetime"
_ERR_OBSERVED_VALUE = "observed_at must not precede the replay knowledge cutoff"
_ERR_RPC = "malformed coverage snapshot RPC response"
_ERR_MISMATCH = "the database history manifest does not equal the supplied canonical projection history"


class PrivateBacktestPortfolioHistoryCoverageMismatchError(RuntimeError):
    """The supplied C2B1 projection history differs from the database's atomic persisted-history manifest (no row data in the message)."""


@dataclass(frozen=True, init=False)
class PrivateBacktestPortfolioHistoryCoverage:
    """A projection binding whose known history exactly matched the database manifest observed at `observed_at`."""
    projection_binding: PrivateBacktestPortfolioProjectionBinding
    owner_id: UUID
    observed_at: datetime

    def __init__(self, *, projection_binding: PrivateBacktestPortfolioProjectionBinding, owner_id: UUID, observed_at: datetime, capability: object) -> None:
        if capability is not _VERIFIED_COVERAGE_CAPABILITY:
            raise TypeError(_ERR_CAPABILITY)
        if type(projection_binding) is not PrivateBacktestPortfolioProjectionBinding:
            raise TypeError(_ERR_BINDING)
        if type(owner_id) is not UUID:
            raise TypeError(_ERR_OWNER)
        if type(observed_at) is not datetime or observed_at.utcoffset() is None:
            raise TypeError(_ERR_OBSERVED_TYPE)
        if observed_at.astimezone(timezone.utc) < projection_binding.analysis_context.replay_point.knowledge_cutoff_utc:
            raise ValueError(_ERR_OBSERVED_VALUE)
        object.__setattr__(self, "projection_binding", projection_binding)
        object.__setattr__(self, "owner_id", owner_id)
        object.__setattr__(self, "observed_at", observed_at)

    @property
    def portfolio_id(self) -> UUID:
        return self.projection_binding.projection.portfolio_id

    @property
    def as_of_recorded_at(self) -> datetime:
        return self.projection_binding.projection.as_of_recorded_at

    @property
    def transaction_count(self) -> int:
        return len(self.projection_binding.projection.known_transactions)


def _fail() -> RuntimeError:
    return RuntimeError(_ERR_RPC)


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise _fail()
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise _fail() from error
    if str(parsed) != value:
        raise _fail()
    return parsed


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise _fail()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _fail() from error
    if parsed.utcoffset() is None:
        raise _fail()
    return parsed


def _epoch_micros(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400 * 1_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _exact_list(value: object) -> list:
    if type(value) is not list:
        raise _fail()
    return value


class PrivateBacktestPortfolioHistoryCoverageRepository:
    """Trusted backend adapter: one coverage-snapshot RPC per verification, exact reconciliation, no retry."""

    def __init__(self, *, client: Any, owner_id: UUID) -> None:
        if client is None:
            raise ValueError(_ERR_CLIENT)
        if type(owner_id) is not UUID:
            raise TypeError(_ERR_OWNER)
        self._client = client
        self._owner_id = owner_id

    @property
    def owner_id(self) -> UUID:
        return self._owner_id

    def verify_projection_history(self, *, projection_binding: PrivateBacktestPortfolioProjectionBinding) -> PrivateBacktestPortfolioHistoryCoverage:
        """Verify that the database manifest through the replay cutoff equals the projection's known transactions exactly."""
        if type(projection_binding) is not PrivateBacktestPortfolioProjectionBinding:
            raise TypeError(_ERR_BINDING)
        projection = projection_binding.projection
        cutoff = projection_binding.analysis_context.replay_point.portfolio_recorded_cutoff
        params = {
            "p_owner_id": str(self._owner_id),
            "p_portfolio_id": str(projection.portfolio_id),
            "p_as_of_recorded_at": cutoff.isoformat(timespec="microseconds"),
        }
        data = self._client.rpc("get_portfolio_transaction_history_coverage_snapshot", params).execute().data
        if type(data) is not list or len(data) != 1 or type(data[0]) is not dict or set(data[0]) != _ROW_KEYS:
            raise _fail()
        row = data[0]

        if _uuid(row["owner_id"]) != self._owner_id or _uuid(row["portfolio_id"]) != projection.portfolio_id:
            raise _fail()
        as_of = _instant(row["as_of_recorded_at"])
        observed_at = _instant(row["observed_at"])
        if as_of.astimezone(timezone.utc) != cutoff.astimezone(timezone.utc):
            raise _fail()
        if observed_at.astimezone(timezone.utc) < as_of.astimezone(timezone.utc):
            raise _fail()
        count = row["transaction_count"]
        if type(count) is not int or count < 0:
            raise _fail()
        ids, micros, fingerprints = _exact_list(row["transaction_ids"]), _exact_list(row["recorded_at_epoch_micros"]), _exact_list(row["economic_fingerprints"])
        if not (len(ids) == len(micros) == len(fingerprints) == count):
            raise _fail()
        parsed_ids = [_uuid(item) for item in ids]
        if len(set(parsed_ids)) != len(parsed_ids):
            raise _fail()
        if any(type(item) is not int for item in micros):
            raise _fail()
        if any(type(item) is not str or _FINGERPRINT.fullmatch(item) is None for item in fingerprints):
            raise _fail()

        known = projection.known_transactions
        if (ids != [str(transaction.id) for transaction in known]
                or micros != [_epoch_micros(transaction.recorded_at) for transaction in known]
                or fingerprints != [transaction.economic_fingerprint() for transaction in known]):
            raise PrivateBacktestPortfolioHistoryCoverageMismatchError(_ERR_MISMATCH)
        return PrivateBacktestPortfolioHistoryCoverage(projection_binding=projection_binding, owner_id=self._owner_id, observed_at=observed_at,
                                                       capability=_VERIFIED_COVERAGE_CAPABILITY)
