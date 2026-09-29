"""
backend/engine/private/risk_evidence_investment_goal_schema.py
==============================================================
Canonical INVESTMENT_GOAL risk-evidence envelope and byte codec for the Private Investment Decision Engine
(Phase 15C.7).

Architectural Invariants:
    - Pure domain value object + deterministic codec. Zero clock, network, persistence, UUID generation,
      entropy, filesystem, database, or cache access. Imports only the standard library and
      `backend.engine.private.domain`; independent of `portfolio.*`, persistence, the other schema modules,
      and the 15B/15C.2 evidence chain.
    - Performs NO hashing. Phase 15B remains the sole digest authority.
    - Fact grain: ONE declared investment goal (`InvestmentGoalRiskFact`), exactly nine fields: portfolio_id,
      mode, goal_id, target_amount (finite Decimal > 0), target_currency, target_date (None | exact `date`),
      priority, status (all four GoalStatus values), archived_at (None | exact timezone-aware datetime).
      Excluded: name (free text), created_at, owner_id, kind, schema version, context, availability, digest.
    - `mode` is required source context: `InvestmentGoal` itself carries no `PortfolioMode`, so a sandbox goal
      must not be byte-identical to a real one. This schema stores `mode` but does NOT prove it came from the
      authoritative Portfolio or that `portfolio_id` matches it; a later owner-bound source-construction boundary
      must establish that. No status/archived_at consistency rules are imposed beyond the source domain.
    - Envelope (kind = RiskEvidenceKind.INVESTMENT_GOAL, schema_version = 1, specific to this kind):
        {"kind": "investment_goal", "schema_version": 1, "payload": {nine exact keys}}
    - Canonical JSON: UTF-8, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, no
      trailing newline, exact `bytes`. Scalars: UUID -> lowercase hyphenated str; enums -> `.value`; date ->
      `YYYY-MM-DD` or null; Decimal -> fixed-point text, trailing fractional zeros stripped, no exponent, no
      float; archived_at -> UTC `isoformat()` or null.
    - Resource ceiling `_MAX_CANONICAL_CONTENT_BYTES` (4096) is wire/memory safety only, NOT financial precision
      or magnitude. The encoder bounds the fixed-point size from `Decimal.as_tuple()` BEFORE `format(..., "f")`;
      the decoder checks length BEFORE UTF-8 decoding and JSON parsing.
    - Decoder fails closed: exact bytes; valid UTF-8 JSON; duplicate keys rejected; exact envelope and payload key
      sets; exact kind and version; exact scalar types (target_amount is a JSON string); then re-encode-and-compare
      so exactly one byte representation exists per fact.
    - Missing evidence is NOT representable here: it stays `MissingRiskEvidence` in the binding chain.
    - Static error strings only. No scoring, capacity, required-return, suitability, or owner semantics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from backend.engine.private.domain import Currency, GoalPriority, GoalStatus, PortfolioMode

# Wire/serialization resource ceiling only (not financial precision or an economic maximum).
_MAX_CANONICAL_CONTENT_BYTES = 4096
_KIND_VALUE = "investment_goal"
_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = frozenset({"kind", "schema_version", "payload"})
_PAYLOAD_KEYS = frozenset(
    {
        "archived_at", "goal_id", "mode", "portfolio_id", "priority", "status",
        "target_amount", "target_currency", "target_date",
    }
)
_CANONICAL_AMOUNT_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?\Z")
_DATE_TEXT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

_ERR_AMOUNT_SIZE = "target amount canonical representation exceeds maximum supported size"
_ERR_CONTENT_SIZE = "investment goal risk evidence content exceeds maximum supported size"
_ERR_FACT_TYPE = "fact must be an exact InvestmentGoalRiskFact instance"
_ERR_BYTES = "content must be exact bytes"
_ERR_JSON = "investment goal risk evidence content must be valid canonical UTF-8 JSON"
_ERR_ENVELOPE = "investment goal risk evidence envelope is invalid"
_ERR_PAYLOAD = "investment goal risk evidence payload is invalid"
_ERR_NOT_CANONICAL = "investment goal risk evidence content is not canonical"


@dataclass(frozen=True)
class InvestmentGoalRiskFact:
    """
    One declared investment goal as canonical risk evidence (present, typed fact).
    """
    portfolio_id: UUID
    mode: PortfolioMode
    goal_id: UUID
    target_amount: Decimal
    target_currency: Currency
    target_date: date | None
    priority: GoalPriority
    status: GoalStatus
    archived_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.portfolio_id) is not UUID:
            raise TypeError("portfolio_id must be an exact UUID instance")
        if type(self.mode) is not PortfolioMode:
            raise TypeError("mode must be an exact PortfolioMode instance")
        if type(self.goal_id) is not UUID:
            raise TypeError("goal_id must be an exact UUID instance")
        if type(self.target_amount) is not Decimal:
            raise TypeError("target_amount must be an exact Decimal instance")
        if not self.target_amount.is_finite() or self.target_amount <= Decimal("0"):
            raise ValueError("target_amount must be a finite positive Decimal")
        if type(self.target_currency) is not Currency:
            raise TypeError("target_currency must be an exact Currency instance")
        if self.target_date is not None and type(self.target_date) is not date:
            raise TypeError("target_date must be None or an exact date instance")
        if type(self.priority) is not GoalPriority:
            raise TypeError("priority must be an exact GoalPriority instance")
        if type(self.status) is not GoalStatus:
            raise TypeError("status must be an exact GoalStatus instance")
        if self.archived_at is not None:
            _require_aware_datetime(self.archived_at)


def _require_aware_datetime(value: object) -> None:
    message = "archived_at must be None or an exact timezone-aware datetime"
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(message)
    try:
        if type(value.utcoffset()) is not timedelta:
            raise TypeError(message)
        value.astimezone(timezone.utc)
    except Exception:
        raise TypeError(message) from None


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


def encode_investment_goal_risk_fact(fact: InvestmentGoalRiskFact) -> bytes:
    """Encode a fact into its unique canonical envelope bytes."""
    if type(fact) is not InvestmentGoalRiskFact:
        raise TypeError(_ERR_FACT_TYPE)

    target_date = None if fact.target_date is None else fact.target_date.isoformat()
    archived_at = None
    if fact.archived_at is not None:
        archived_at = fact.archived_at.astimezone(timezone.utc).isoformat()

    envelope = {
        "kind": _KIND_VALUE,
        "schema_version": _SCHEMA_VERSION,
        "payload": {
            "portfolio_id": str(fact.portfolio_id),
            "mode": fact.mode.value,
            "goal_id": str(fact.goal_id),
            "target_amount": _canonical_decimal_text(fact.target_amount),
            "target_currency": fact.target_currency.value,
            "target_date": target_date,
            "priority": fact.priority.value,
            "status": fact.status.value,
            "archived_at": archived_at,
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


def decode_investment_goal_risk_fact(content: bytes) -> InvestmentGoalRiskFact:
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

    if encode_investment_goal_risk_fact(fact) != content:
        raise ValueError(_ERR_NOT_CANONICAL)
    return fact


def _build_fact(payload: dict[str, object]) -> InvestmentGoalRiskFact:
    portfolio_text = payload["portfolio_id"]
    mode_text = payload["mode"]
    goal_text = payload["goal_id"]
    amount_text = payload["target_amount"]
    currency_text = payload["target_currency"]
    priority_text = payload["priority"]
    status_text = payload["status"]
    date_text = payload["target_date"]
    archived_text = payload["archived_at"]

    for value in (portfolio_text, mode_text, goal_text, amount_text, currency_text,
                  priority_text, status_text):
        if type(value) is not str:
            raise TypeError("scalar must be a string")
    for value in (date_text, archived_text):
        if value is not None and type(value) is not str:
            raise TypeError("optional scalar must be null or a string")
    if _CANONICAL_AMOUNT_RE.match(amount_text) is None:
        raise ValueError("target_amount must be canonical decimal text")

    target_date = None
    if date_text is not None:
        if _DATE_TEXT_RE.match(date_text) is None:
            raise ValueError("target_date must be YYYY-MM-DD")
        target_date = date.fromisoformat(date_text)

    archived_at = None if archived_text is None else datetime.fromisoformat(archived_text)

    return InvestmentGoalRiskFact(
        portfolio_id=UUID(portfolio_text),
        mode=PortfolioMode(mode_text),
        goal_id=UUID(goal_text),
        target_amount=Decimal(amount_text),
        target_currency=Currency(currency_text),
        target_date=target_date,
        priority=GoalPriority(priority_text),
        status=GoalStatus(status_text),
        archived_at=archived_at,
    )
