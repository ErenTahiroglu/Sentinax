"""
backend/engine/private/risk_evidence_cash_schema.py
===================================================
Canonical CASH_BALANCE risk-evidence envelope and byte codec for the Private Investment Decision Engine
(Phase 15C.3).

Architectural Invariants:
    - Pure domain value object + deterministic codec. Zero clock, network, persistence, UUID generation,
      entropy, filesystem, database, or cache access. Imports only the standard library and
      `backend.engine.private.domain`; independent of `portfolio.*`, persistence, and the 15B/15C.2 chain.
    - Performs NO hashing. Phase 15B remains the sole digest authority. This module answers only
      "what do these bytes mean?", never "do these bytes match a provenance digest?".
    - Fact grain: ONE account/currency cash balance state (`CashBalanceRiskFact`), exactly six fields:
      portfolio_id, account_id, currency, balance (finite Decimal >= 0; zero is known cash, not missing),
      mode (MY_PORTFOLIO vs SANDBOX), as_of_recorded_at (None or exact timezone-aware datetime).
      No owner_id, kind, schema version, context, digest, score, or status.
    - Envelope (kind = RiskEvidenceKind.CASH_BALANCE, schema_version = 1):
        {"kind": "cash_balance", "schema_version": 1, "payload": {six exact keys}}
    - Canonical JSON: UTF-8, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
      no trailing newline. Returns exact `bytes`.
    - Canonical scalars: UUID -> lowercase hyphenated str; enums -> `.value`; Decimal -> fixed-point text with
      trailing fractional zeros stripped and no exponent ("-0" -> "0"); datetime -> UTC `isoformat()`.
    - Decoder fails closed: exact bytes only; valid UTF-8 JSON; duplicate keys rejected; exact envelope and
      payload key sets; exact kind and schema version; exact scalar types (balance is a JSON string, never a
      number); then re-encode-and-compare so exactly one byte representation exists per fact.
    - Missing evidence is NOT representable here: it stays `MissingRiskEvidence` in the binding chain.
    - Resource ceiling: canonical content is at most `_MAX_CANONICAL_CONTENT_BYTES` (4096) bytes. This is a
      serialization/wire-size bound only: it is NOT financial precision, NOT PostgreSQL NUMERIC semantics,
      NOT a maximum cash balance, and NOT a risk threshold; `CashBalanceRiskFact` still represents any exact
      finite non-negative Decimal. The encoder estimates the fixed-point size from `Decimal.as_tuple()` and
      rejects oversized values BEFORE `format(..., "f")`; the decoder checks length BEFORE UTF-8 decoding and
      JSON parsing. `schema_version` stays 1: every in-budget v1 fact keeps byte-identical canonical form.
    - Static error strings only (no raw content, repr, str, type names, or parser text).
    - No scoring, level, suitability, required-risk, or overall-risk semantics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from backend.engine.private.domain import Currency, PortfolioMode

# Wire/serialization resource ceiling only (not financial precision or an economic maximum).
_MAX_CANONICAL_CONTENT_BYTES = 4096
_KIND_VALUE = "cash_balance"
_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = frozenset({"kind", "schema_version", "payload"})
_PAYLOAD_KEYS = frozenset(
    {"portfolio_id", "account_id", "currency", "balance", "mode", "as_of_recorded_at"}
)
# Canonical non-negative decimal text; excludes signs, exponents, redundant zeros.
_CANONICAL_BALANCE_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?\Z")

_ERR_BALANCE_SIZE = "balance canonical representation exceeds maximum supported size"
_ERR_CONTENT_SIZE = "cash balance risk evidence content exceeds maximum supported size"
_ERR_FACT_TYPE = "fact must be an exact CashBalanceRiskFact instance"
_ERR_BYTES = "content must be exact bytes"
_ERR_JSON = "cash balance risk evidence content must be valid canonical UTF-8 JSON"
_ERR_ENVELOPE = "cash balance risk evidence envelope is invalid"
_ERR_PAYLOAD = "cash balance risk evidence payload is invalid"
_ERR_NOT_CANONICAL = "cash balance risk evidence content is not canonical"


@dataclass(frozen=True)
class CashBalanceRiskFact:
    """
    One account/currency cash balance state as canonical risk evidence (present, typed fact).
    """
    portfolio_id: UUID
    account_id: UUID
    currency: Currency
    balance: Decimal
    mode: PortfolioMode
    as_of_recorded_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.portfolio_id) is not UUID:
            raise TypeError("portfolio_id must be an exact UUID instance")
        if type(self.account_id) is not UUID:
            raise TypeError("account_id must be an exact UUID instance")
        if type(self.currency) is not Currency:
            raise TypeError("currency must be an exact Currency instance")
        if type(self.balance) is not Decimal:
            raise TypeError("balance must be an exact Decimal instance")
        if not self.balance.is_finite() or self.balance < Decimal("0"):
            raise ValueError("balance must be a finite non-negative Decimal")
        if type(self.mode) is not PortfolioMode:
            raise TypeError("mode must be an exact PortfolioMode instance")
        if self.as_of_recorded_at is not None:
            _require_aware_datetime(self.as_of_recorded_at)


def _require_aware_datetime(value: object) -> None:
    message = "as_of_recorded_at must be None or an exact timezone-aware datetime"
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
            return sign_chars + 1  # zero with a non-negative exponent formats as "0"
        return sign_chars + digit_count + exponent
    if digit_count + exponent > 0:
        return sign_chars + digit_count + 1  # integer digits + "." + fraction digits
    return sign_chars + 2 - exponent  # "0." + leading fractional zeros + digits


def _canonical_decimal_text(value: Decimal) -> str:
    if _fixed_point_length_upper_bound(value) > _MAX_CANONICAL_CONTENT_BYTES:
        raise ValueError(_ERR_BALANCE_SIZE)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return text


def encode_cash_balance_risk_fact(fact: CashBalanceRiskFact) -> bytes:
    """Encode a fact into its unique canonical envelope bytes."""
    if type(fact) is not CashBalanceRiskFact:
        raise TypeError(_ERR_FACT_TYPE)

    as_of = None
    if fact.as_of_recorded_at is not None:
        as_of = fact.as_of_recorded_at.astimezone(timezone.utc).isoformat()

    envelope = {
        "kind": _KIND_VALUE,
        "schema_version": _SCHEMA_VERSION,
        "payload": {
            "portfolio_id": str(fact.portfolio_id),
            "account_id": str(fact.account_id),
            "currency": fact.currency.value,
            "balance": _canonical_decimal_text(fact.balance),
            "mode": fact.mode.value,
            "as_of_recorded_at": as_of,
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


def decode_cash_balance_risk_fact(content: bytes) -> CashBalanceRiskFact:
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

    if encode_cash_balance_risk_fact(fact) != content:
        raise ValueError(_ERR_NOT_CANONICAL)
    return fact


def _build_fact(payload: dict[str, object]) -> CashBalanceRiskFact:
    portfolio_text = payload["portfolio_id"]
    account_text = payload["account_id"]
    currency_text = payload["currency"]
    balance_text = payload["balance"]
    mode_text = payload["mode"]
    as_of_text = payload["as_of_recorded_at"]

    for value in (portfolio_text, account_text, currency_text, balance_text, mode_text):
        if type(value) is not str:
            raise TypeError("scalar must be a string")
    if as_of_text is not None and type(as_of_text) is not str:
        raise TypeError("as_of_recorded_at must be null or a string")
    if _CANONICAL_BALANCE_RE.match(balance_text) is None:
        raise ValueError("balance must be canonical non-negative decimal text")

    as_of = None if as_of_text is None else datetime.fromisoformat(as_of_text)

    return CashBalanceRiskFact(
        portfolio_id=UUID(portfolio_text),
        account_id=UUID(account_text),
        currency=Currency(currency_text),
        balance=Decimal(balance_text),
        mode=PortfolioMode(mode_text),
        as_of_recorded_at=as_of,
    )
