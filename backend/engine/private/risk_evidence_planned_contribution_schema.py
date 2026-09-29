"""
backend/engine/private/risk_evidence_planned_contribution_schema.py
===================================================================
Canonical PLANNED_CONTRIBUTION risk-evidence envelope and byte codec for the Private Investment Decision
Engine (Phase 15C.5).

Architectural Invariants:
    - Pure domain value object + deterministic codec. Zero clock, network, persistence, UUID generation,
      entropy, filesystem, database, or cache access. Imports only the standard library and
      `backend.engine.private.domain`; independent of `portfolio.*`, persistence, the CASH_BALANCE schema,
      and the 15B/15C.2/15C.4 evidence chain.
    - Performs NO hashing. Phase 15B remains the sole digest authority.
    - Fact grain: ONE declared expected future contribution (`PlannedContributionRiskFact`), exactly nine fields:
      portfolio_id, mode, contribution_id, goal_id (None | UUID), cash_bucket_id (None | UUID), expected_date
      (exact calendar `date`), amount (finite Decimal > 0), currency, status (all four ContributionStatus
      values). No created_at, owner_id, kind, schema version, context, availability, digest, score, or capacity.
    - `mode` is required source context: `PlannedContribution` itself carries only `portfolio_id`, so a sandbox
      contribution must not be byte-identical to a real one. This schema stores `mode` but does NOT prove it came
      from the authoritative Portfolio, that `portfolio_id` matches that Portfolio, or that goal/bucket links are
      valid; a later owner-bound source-construction boundary must establish those. Links are identity only.
    - A planned contribution is NOT current cash, guaranteed income, recurring income, or received cash. This
      module assigns no capacity meaning to any status.
    - Envelope (kind = RiskEvidenceKind.PLANNED_CONTRIBUTION, schema_version = 1, specific to this kind):
        {"kind": "planned_contribution", "schema_version": 1, "payload": {nine exact keys}}
    - Canonical JSON: UTF-8, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, no
      trailing newline, exact `bytes`. Scalars: UUID -> lowercase hyphenated str (or null for links); enums ->
      `.value`; date -> `YYYY-MM-DD`; Decimal -> fixed-point text, trailing fractional zeros stripped, no
      exponent, no float.
    - Resource ceiling `_MAX_CANONICAL_CONTENT_BYTES` (4096) is wire/memory safety only, NOT financial precision
      or magnitude. The encoder bounds the fixed-point size from `Decimal.as_tuple()` BEFORE `format(..., "f")`;
      the decoder checks length BEFORE UTF-8 decoding and JSON parsing.
    - Decoder fails closed: exact bytes; valid UTF-8 JSON; duplicate keys rejected; exact envelope and payload key
      sets; exact kind and version; exact scalar types (amount is a JSON string); then re-encode-and-compare so
      exactly one byte representation exists per fact.
    - Missing evidence is NOT representable here: it stays `MissingRiskEvidence` in the binding chain.
    - Static error strings only. No scoring, level, suitability, aggregation, required-risk, or owner semantics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from backend.engine.private.domain import ContributionStatus, Currency, PortfolioMode

# Wire/serialization resource ceiling only (not financial precision or an economic maximum).
_MAX_CANONICAL_CONTENT_BYTES = 4096
_KIND_VALUE = "planned_contribution"
_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = frozenset({"kind", "schema_version", "payload"})
_PAYLOAD_KEYS = frozenset(
    {
        "amount", "cash_bucket_id", "contribution_id", "currency", "expected_date",
        "goal_id", "mode", "portfolio_id", "status",
    }
)
_CANONICAL_AMOUNT_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?\Z")
_DATE_TEXT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

_ERR_AMOUNT_SIZE = "amount canonical representation exceeds maximum supported size"
_ERR_CONTENT_SIZE = "planned contribution risk evidence content exceeds maximum supported size"
_ERR_FACT_TYPE = "fact must be an exact PlannedContributionRiskFact instance"
_ERR_BYTES = "content must be exact bytes"
_ERR_JSON = "planned contribution risk evidence content must be valid canonical UTF-8 JSON"
_ERR_ENVELOPE = "planned contribution risk evidence envelope is invalid"
_ERR_PAYLOAD = "planned contribution risk evidence payload is invalid"
_ERR_NOT_CANONICAL = "planned contribution risk evidence content is not canonical"


@dataclass(frozen=True)
class PlannedContributionRiskFact:
    """
    One declared expected future contribution as canonical risk evidence (present, typed fact).
    """
    portfolio_id: UUID
    mode: PortfolioMode
    contribution_id: UUID
    goal_id: UUID | None
    cash_bucket_id: UUID | None
    expected_date: date
    amount: Decimal
    currency: Currency
    status: ContributionStatus

    def __post_init__(self) -> None:
        if type(self.portfolio_id) is not UUID:
            raise TypeError("portfolio_id must be an exact UUID instance")
        if type(self.mode) is not PortfolioMode:
            raise TypeError("mode must be an exact PortfolioMode instance")
        if type(self.contribution_id) is not UUID:
            raise TypeError("contribution_id must be an exact UUID instance")
        if self.goal_id is not None and type(self.goal_id) is not UUID:
            raise TypeError("goal_id must be None or an exact UUID instance")
        if self.cash_bucket_id is not None and type(self.cash_bucket_id) is not UUID:
            raise TypeError("cash_bucket_id must be None or an exact UUID instance")
        if type(self.expected_date) is not date:
            raise TypeError("expected_date must be an exact date instance")
        if type(self.amount) is not Decimal:
            raise TypeError("amount must be an exact Decimal instance")
        if not self.amount.is_finite() or self.amount <= Decimal("0"):
            raise ValueError("amount must be a finite positive Decimal")
        if type(self.currency) is not Currency:
            raise TypeError("currency must be an exact Currency instance")
        if type(self.status) is not ContributionStatus:
            raise TypeError("status must be an exact ContributionStatus instance")


def _fixed_point_length_upper_bound(value: Decimal) -> int:
    """
    Allocation-free upper bound on `len(format(value, "f"))` for a finite Decimal,
    computed from the coefficient digit count and exponent only.
    """
    _sign, digits, exponent = value.as_tuple()
    digit_count = len(digits)
    sign_chars = 1
    if exponent >= 0:
        if value.is_zero():
            return sign_chars + 1
        return sign_chars + digit_count + exponent
    if digit_count + exponent > 0:
        return sign_chars + digit_count + 1
    return sign_chars + 2 - exponent


def _canonical_decimal_text(value: Decimal) -> str:
    if _fixed_point_length_upper_bound(value) > _MAX_CANONICAL_CONTENT_BYTES:
        raise ValueError(_ERR_AMOUNT_SIZE)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return text


def _optional_uuid_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def encode_planned_contribution_risk_fact(fact: PlannedContributionRiskFact) -> bytes:
    """Encode a fact into its unique canonical envelope bytes."""
    if type(fact) is not PlannedContributionRiskFact:
        raise TypeError(_ERR_FACT_TYPE)

    envelope = {
        "kind": _KIND_VALUE,
        "schema_version": _SCHEMA_VERSION,
        "payload": {
            "portfolio_id": str(fact.portfolio_id),
            "mode": fact.mode.value,
            "contribution_id": str(fact.contribution_id),
            "goal_id": _optional_uuid_text(fact.goal_id),
            "cash_bucket_id": _optional_uuid_text(fact.cash_bucket_id),
            "expected_date": fact.expected_date.isoformat(),
            "amount": _canonical_decimal_text(fact.amount),
            "currency": fact.currency.value,
            "status": fact.status.value,
        },
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_CANONICAL_CONTENT_BYTES:
        raise ValueError(_ERR_CONTENT_SIZE)
    return encoded


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite constant")


def decode_planned_contribution_risk_fact(content: bytes) -> PlannedContributionRiskFact:
    """Decode canonical envelope bytes into a fact, failing closed on anything non-canonical."""
    if type(content) is not bytes:
        raise TypeError(_ERR_BYTES)
    if len(content) > _MAX_CANONICAL_CONTENT_BYTES:
        raise ValueError(_ERR_CONTENT_SIZE)

    try:
        text = content.decode("utf-8")
        envelope = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise ValueError(_ERR_JSON) from None

    if type(envelope) is not dict or set(envelope) != _ENVELOPE_KEYS:
        raise ValueError(_ERR_ENVELOPE)
    kind = envelope["kind"]
    version = envelope["schema_version"]
    payload = envelope["payload"]
    if type(kind) is not str or kind != _KIND_VALUE:
        raise ValueError(_ERR_ENVELOPE)
    if type(version) is not int or version != _SCHEMA_VERSION:
        raise ValueError(_ERR_ENVELOPE)
    if type(payload) is not dict:
        raise ValueError(_ERR_ENVELOPE)

    if set(payload) != _PAYLOAD_KEYS:
        raise ValueError(_ERR_PAYLOAD)

    try:
        fact = _build_fact(payload)
    except (TypeError, ValueError):
        raise ValueError(_ERR_PAYLOAD) from None

    if encode_planned_contribution_risk_fact(fact) != content:
        raise ValueError(_ERR_NOT_CANONICAL)
    return fact


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("link must be null or a string")
    return UUID(value)


def _build_fact(payload: dict[str, object]) -> PlannedContributionRiskFact:
    portfolio_text = payload["portfolio_id"]
    mode_text = payload["mode"]
    contribution_text = payload["contribution_id"]
    date_text = payload["expected_date"]
    amount_text = payload["amount"]
    currency_text = payload["currency"]
    status_text = payload["status"]

    for value in (portfolio_text, mode_text, contribution_text, date_text, amount_text,
                  currency_text, status_text):
        if type(value) is not str:
            raise TypeError("scalar must be a string")
    if _CANONICAL_AMOUNT_RE.match(amount_text) is None:
        raise ValueError("amount must be canonical decimal text")
    if _DATE_TEXT_RE.match(date_text) is None:
        raise ValueError("expected_date must be YYYY-MM-DD")

    return PlannedContributionRiskFact(
        portfolio_id=UUID(portfolio_text),
        mode=PortfolioMode(mode_text),
        contribution_id=UUID(contribution_text),
        goal_id=_optional_uuid(payload["goal_id"]),
        cash_bucket_id=_optional_uuid(payload["cash_bucket_id"]),
        expected_date=date.fromisoformat(date_text),
        amount=Decimal(amount_text),
        currency=Currency(currency_text),
        status=ContributionStatus(status_text),
    )
