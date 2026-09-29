"""
backend/tests/test_risk_evidence_investment_goal_schema.py
==========================================================
Tests for the canonical INVESTMENT_GOAL risk-evidence schema (Phase 15C.7).

InvestmentGoalRiskFact <-> canonical envelope bytes. No hashing, no composition, no missing branch,
no source-construction adapter.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import risk_evidence_investment_goal_schema as module_under_test
from backend.engine.private.domain import Currency, GoalPriority, GoalStatus, PortfolioMode
from backend.engine.private.risk_evidence_investment_goal_schema import (
    InvestmentGoalRiskFact,
    decode_investment_goal_risk_fact,
    encode_investment_goal_risk_fact,
)

_MSG = {
    "portfolio_id": r"^portfolio_id must be an exact UUID instance$",
    "goal_id": r"^goal_id must be an exact UUID instance$",
    "mode": r"^mode must be an exact PortfolioMode instance$",
    "amount_type": r"^target_amount must be an exact Decimal instance$",
    "amount_value": r"^target_amount must be a finite positive Decimal$",
    "currency": r"^target_currency must be an exact Currency instance$",
    "target_date": r"^target_date must be None or an exact date instance$",
    "priority": r"^priority must be an exact GoalPriority instance$",
    "status": r"^status must be an exact GoalStatus instance$",
    "archived_at": r"^archived_at must be None or an exact timezone-aware datetime$",
}
_FACT_TYPE_MSG = r"^fact must be an exact InvestmentGoalRiskFact instance$"
_BYTES_MSG = r"^content must be exact bytes$"
_JSON_MSG = r"^investment goal risk evidence content must be valid canonical UTF-8 JSON$"
_ENVELOPE_MSG = r"^investment goal risk evidence envelope is invalid$"
_PAYLOAD_MSG = r"^investment goal risk evidence payload is invalid$"
_CANON_MSG = r"^investment goal risk evidence content is not canonical$"
_SIZE_AMOUNT_MSG = r"^target amount canonical representation exceeds maximum supported size$"
_SIZE_CONTENT_MSG = r"^investment goal risk evidence content exceeds maximum supported size$"
_EITHER = r"^investment goal risk evidence (payload is invalid|content is not canonical)$"

_P = UUID("11111111-1111-4111-8111-111111111111")
_G = UUID("33333333-3333-4333-8333-333333333333")
_T = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)


def _fact(**overrides) -> InvestmentGoalRiskFact:
    kwargs = dict(
        portfolio_id=_P,
        mode=PortfolioMode.MY_PORTFOLIO,
        goal_id=_G,
        target_amount=Decimal("100000"),
        target_currency=Currency.TRY,
        target_date=date(2027, 7, 1),
        priority=GoalPriority.MEDIUM,
        status=GoalStatus.ACTIVE,
        archived_at=None,
    )
    kwargs.update(overrides)
    return InvestmentGoalRiskFact(**kwargs)


_LITERAL = (
    b'{"kind":"investment_goal","payload":{"archived_at":null,'
    b'"goal_id":"33333333-3333-4333-8333-333333333333","mode":"my_portfolio",'
    b'"portfolio_id":"11111111-1111-4111-8111-111111111111","priority":"medium","status":"active",'
    b'"target_amount":"100000","target_currency":"TRY","target_date":"2027-07-01"},"schema_version":1}'
)


def _canonical_dict() -> dict:
    return json.loads(encode_investment_goal_risk_fact(_fact()).decode("utf-8"))


def _compact(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")

    def __str__(self) -> str:
        raise RuntimeError("hostile str")


class _HostileMeta(type):
    @property
    def __name__(cls):  # type: ignore[override]
        raise RuntimeError("hostile metaclass __name__")


class _HostileMetaInstance(metaclass=_HostileMeta):
    pass


class _DecimalSub(Decimal):
    pass


class _UUIDSub(UUID):
    pass


class _BytesSub(bytes):
    pass


class _FactSub(InvestmentGoalRiskFact):
    pass


def _forbid(*_args, **_kwargs):
    raise AssertionError("expensive operation reached")


# --- fact -------------------------------------------------------------------

def test_fact_is_frozen_with_exactly_nine_fields() -> None:
    assert [f.name for f in dataclasses.fields(InvestmentGoalRiskFact)] == [
        "portfolio_id", "mode", "goal_id", "target_amount", "target_currency",
        "target_date", "priority", "status", "archived_at",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _fact().status = GoalStatus.PAUSED  # type: ignore[misc]


@pytest.mark.parametrize("attr", ["name", "created_at", "owner_id", "kind", "schema_version", "context",
                                  "available_at", "source_key", "digest", "score", "capacity", "level",
                                  "suitability", "missing", "required_return", "required_risk"])
def test_fact_has_no_extra_surface(attr: str) -> None:
    assert not hasattr(_fact(), attr)


@pytest.mark.parametrize("priority", list(GoalPriority))
@pytest.mark.parametrize("status", list(GoalStatus))
@pytest.mark.parametrize("mode", list(PortfolioMode))
def test_all_priorities_statuses_modes_round_trip(priority, status, mode) -> None:
    fact = _fact(priority=priority, status=status, mode=mode)
    encoded = encode_investment_goal_risk_fact(fact)
    decoded = decode_investment_goal_risk_fact(encoded)
    assert decoded == fact
    assert encode_investment_goal_risk_fact(decoded) == encoded


def test_enum_members_are_the_documented_values() -> None:
    assert [p.value for p in GoalPriority] == ["low", "medium", "high", "critical"]
    assert [s.value for s in GoalStatus] == ["active", "paused", "completed", "cancelled"]


@pytest.mark.parametrize("target_date", [None, date(2027, 7, 1), date(1999, 1, 1)])
@pytest.mark.parametrize("archived_at", [None, _T, datetime(2026, 8, 28, 13, 0, 0, tzinfo=timezone(timedelta(hours=3)))])
def test_optional_fields_round_trip(target_date, archived_at) -> None:
    fact = _fact(target_date=target_date, archived_at=archived_at)
    assert decode_investment_goal_risk_fact(encode_investment_goal_risk_fact(fact)) == fact


def test_no_status_archived_consistency_rules() -> None:
    assert _fact(status=GoalStatus.CANCELLED, archived_at=None).archived_at is None
    assert _fact(status=GoalStatus.ACTIVE, archived_at=_T).archived_at == _T


@pytest.mark.parametrize("field", ["portfolio_id", "goal_id"])
@pytest.mark.parametrize("value", [str(_P), _P.int, True, None, object(), _Hostile(), _UUIDSub(str(_P))])
def test_invalid_uuid_fields(field: str, value) -> None:
    with pytest.raises(TypeError, match=_MSG[field]):
        _fact(**{field: value})


@pytest.mark.parametrize("field,key", [("mode", "mode"), ("target_currency", "currency"),
                                       ("priority", "priority"), ("status", "status")])
@pytest.mark.parametrize("value", ["x", None, 1, object(), _Hostile()])
def test_invalid_enum_fields(field: str, key: str, value) -> None:
    with pytest.raises(TypeError, match=_MSG[key]):
        _fact(**{field: value})


@pytest.mark.parametrize("value", [datetime(2027, 7, 1), datetime(2027, 7, 1, 12), "2027-07-01", 20270701,
                                   1.5, True, object(), _Hostile()])
def test_invalid_target_date(value) -> None:
    with pytest.raises(TypeError, match=_MSG["target_date"]):
        _fact(target_date=value)


@pytest.mark.parametrize("value", [datetime(2026, 8, 28, 10, 0, 0), "2026-08-28T10:00:00+00:00", 0, True,
                                   object(), _Hostile(), date(2026, 8, 28)])
def test_invalid_archived_at(value) -> None:
    with pytest.raises(TypeError, match=_MSG["archived_at"]):
        _fact(archived_at=value)


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("0.00"), Decimal("-0"), Decimal("-1"),
                                    Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_invalid_amount_values(amount: Decimal) -> None:
    with pytest.raises(ValueError, match=_MSG["amount_value"]):
        _fact(target_amount=amount)


@pytest.mark.parametrize("amount", [1, 1.0, True, "1", None, b"1", _DecimalSub("1"), _Hostile()])
def test_invalid_amount_types(amount) -> None:
    with pytest.raises(TypeError, match=_MSG["amount_type"]):
        _fact(target_amount=amount)


# --- encoder ----------------------------------------------------------------

def test_encoder_exact_literal_bytes() -> None:
    out = encode_investment_goal_risk_fact(_fact())
    assert type(out) is bytes
    assert out == _LITERAL
    assert not out.endswith(b"\n")


@pytest.mark.parametrize("text,expected", [
    ("1", "1"), ("1.0", "1"), ("1.00", "1"), ("1E+0", "1"),
    ("1000.00", "1000"), ("1E+3", "1000"), ("0.00100", "0.001"),
    ("100000.50", "100000.5"), ("10", "10"), ("100E-2", "1"),
])
def test_decimal_canonicalization_corpus(text: str, expected: str) -> None:
    out = encode_investment_goal_risk_fact(_fact(target_amount=Decimal(text)))
    assert f'"target_amount":"{expected}"'.encode() in out


def test_equivalent_offsets_encode_identically() -> None:
    utc = _fact(archived_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc))
    plus3 = _fact(archived_at=datetime(2026, 8, 28, 13, 0, 0, tzinfo=timezone(timedelta(hours=3))))
    assert encode_investment_goal_risk_fact(utc) == encode_investment_goal_risk_fact(plus3)
    assert b'"archived_at":"2026-08-28T10:00:00+00:00"' in encode_investment_goal_risk_fact(plus3)


def test_microseconds_preserved_and_nulls_explicit() -> None:
    out = encode_investment_goal_risk_fact(_fact(archived_at=_T.replace(microsecond=123456)))
    assert b"2026-08-28T10:00:00.123456+00:00" in out
    out = encode_investment_goal_risk_fact(_fact(target_date=None))
    assert b'"target_date":null' in out and b'"archived_at":null' in out


@pytest.mark.parametrize(
    "override",
    [
        {"portfolio_id": UUID(int=9)},
        {"mode": PortfolioMode.SANDBOX},
        {"goal_id": UUID(int=9)},
        {"target_amount": Decimal("100000.01")},
        {"target_currency": Currency.USD},
        {"target_date": date(2027, 7, 2)},
        {"target_date": None},
        {"priority": GoalPriority.CRITICAL},
        {"status": GoalStatus.COMPLETED},
        {"archived_at": _T},
    ],
)
def test_each_field_changes_bytes(override) -> None:
    assert encode_investment_goal_risk_fact(_fact(**override)) != encode_investment_goal_risk_fact(_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, object(), _Hostile(), _HostileMetaInstance()])
def test_encoder_rejects_non_fact(bad) -> None:
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        encode_investment_goal_risk_fact(bad)  # type: ignore[arg-type]


def test_encoder_rejects_fact_subclass() -> None:
    sub = _FactSub(portfolio_id=_P, mode=PortfolioMode.MY_PORTFOLIO, goal_id=_G, target_amount=Decimal("1"),
                   target_currency=Currency.TRY, target_date=None, priority=GoalPriority.LOW,
                   status=GoalStatus.ACTIVE, archived_at=None)
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        encode_investment_goal_risk_fact(sub)


# --- decoder happy path / boundary -------------------------------------------

def test_round_trip_and_literal() -> None:
    fact = _fact()
    encoded = encode_investment_goal_risk_fact(fact)
    decoded = decode_investment_goal_risk_fact(encoded)
    assert type(decoded) is InvestmentGoalRiskFact
    assert decoded == fact
    assert encode_investment_goal_risk_fact(decoded) == encoded
    assert decode_investment_goal_risk_fact(_LITERAL) == fact


@pytest.mark.parametrize("bad", [bytearray(_LITERAL), memoryview(_LITERAL), _LITERAL.decode(), None,
                                 _BytesSub(_LITERAL), 1, object(), _Hostile()])
def test_decoder_rejects_non_exact_bytes(bad) -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        decode_investment_goal_risk_fact(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("content", [b"\xff\xfe", b"", b"{", b"nul", b"\xef\xbb\xbf" + _LITERAL,
                                     _LITERAL.replace(b'"100000"', b"NaN"),
                                     _LITERAL.replace(b'"100000"', b"Infinity"), b"[" * 4000])
def test_invalid_utf8_or_json(content: bytes) -> None:
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_investment_goal_risk_fact(content)


@pytest.mark.parametrize("content", [b"[]", b"1", b"null", b'"investment_goal"', b"true"])
def test_non_object_top_level(content: bytes) -> None:
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_investment_goal_risk_fact(content)


@pytest.mark.parametrize("old,new", [
    (b'"kind":"investment_goal"', b'"kind":"investment_goal","kind":"cash_balance"'),
    (b'"target_amount":"100000"', b'"target_amount":"100000","target_amount":"9"'),
    (b'"archived_at":null', b'"archived_at":null,"archived_at":null'),
    (b'"schema_version":1', b'"schema_version":1,"schema_version":2'),
])
def test_duplicate_keys_rejected(old: bytes, new: bytes) -> None:
    assert old in _LITERAL
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_investment_goal_risk_fact(_LITERAL.replace(old, new))


# --- envelope / payload validation ------------------------------------------

def test_extra_and_missing_top_level_keys() -> None:
    extra = _canonical_dict()
    extra["extra"] = 1
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_investment_goal_risk_fact(_compact(extra))
    for key in ("kind", "schema_version", "payload"):
        d = _canonical_dict()
        del d[key]
        with pytest.raises(ValueError, match=_ENVELOPE_MSG):
            decode_investment_goal_risk_fact(_compact(d))


@pytest.mark.parametrize("kind", ["INVESTMENT_GOAL", "cash_balance", "planned_contribution", "", None, 1, True, []])
def test_wrong_kind(kind) -> None:
    d = _canonical_dict()
    d["kind"] = kind
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_investment_goal_risk_fact(_compact(d))


@pytest.mark.parametrize("version", [0, 2, -1, True, False, 1.0, "1", None, [], {}])
def test_wrong_schema_version(version) -> None:
    d = _canonical_dict()
    d["schema_version"] = version
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_investment_goal_risk_fact(_compact(d))


@pytest.mark.parametrize("payload", [[], None, "x", 1, True])
def test_payload_must_be_object(payload) -> None:
    d = _canonical_dict()
    d["payload"] = payload
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_investment_goal_risk_fact(_compact(d))


_PAYLOAD_KEYS = ["archived_at", "goal_id", "mode", "portfolio_id", "priority", "status",
                 "target_amount", "target_currency", "target_date"]


def test_extra_and_missing_payload_keys() -> None:
    for extra_key in ("owner_id", "created_at", "name"):
        extra = _canonical_dict()
        extra["payload"][extra_key] = "x"
        with pytest.raises(ValueError, match=_PAYLOAD_MSG):
            decode_investment_goal_risk_fact(_compact(extra))
    for key in _PAYLOAD_KEYS:
        d = _canonical_dict()
        del d["payload"][key]
        with pytest.raises(ValueError, match=_PAYLOAD_MSG):
            decode_investment_goal_risk_fact(_compact(d))


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_amount", 1), ("target_amount", 1.0), ("target_amount", True), ("target_amount", None),
        ("target_amount", []), ("target_amount", "0"), ("target_amount", "-1"), ("target_amount", "abc"),
        ("target_amount", ""), ("target_amount", "NaN"), ("target_amount", "Infinity"),
        ("target_amount", "1E+999999999"),
        ("target_currency", None), ("target_currency", 1), ("target_currency", "try"), ("target_currency", "CHF"),
        ("mode", 1), ("mode", None), ("mode", "MY_PORTFOLIO"), ("mode", "unknown"),
        ("priority", None), ("priority", 1), ("priority", "MEDIUM"), ("priority", "urgent"),
        ("status", None), ("status", 1), ("status", "ACTIVE"), ("status", "Active"), ("status", "done"),
        ("portfolio_id", None), ("portfolio_id", 1), ("portfolio_id", "not-a-uuid"),
        ("goal_id", None), ("goal_id", True), ("goal_id", ""),
        ("target_date", 20270701), ("target_date", True), ("target_date", "garbage"),
        ("target_date", "2027-13-01"), ("target_date", "2027-02-30"),
        ("target_date", "2027-07-01T00:00:00"), ("target_date", "0000-01-01"), ("target_date", []),
        ("archived_at", 0), ("archived_at", True), ("archived_at", "garbage"),
        ("archived_at", "2026-08-28T10:00:00"), ("archived_at", "2026-08-28"), ("archived_at", []),
    ],
)
def test_payload_scalar_confusion(field: str, value) -> None:
    d = _canonical_dict()
    d["payload"][field] = value
    with pytest.raises(ValueError, match=_PAYLOAD_MSG):
        decode_investment_goal_risk_fact(_compact(d))


# --- canonicality ------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["1.0", "1.00", "1E+0", "+1", "-0", "01", "1.", ".5", "100000.0",
                                      "1.0E+5", "0.0", "0E+0"])
def test_noncanonical_decimal_rejected(spelling: str) -> None:
    d = _canonical_dict()
    d["payload"]["target_amount"] = spelling
    with pytest.raises(ValueError, match=_EITHER):
        decode_investment_goal_risk_fact(_compact(d))


_LP = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_LG = UUID("abcdef01-2345-4678-89ab-cdef01234567")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.replace(str(_LP), str(_LP).upper()),
        lambda s: s.replace(str(_LG), str(_LG).upper()),
        lambda s: s.replace(str(_LG), _LG.hex),
        lambda s: s.replace(str(_LG), "{" + str(_LG) + "}"),
        lambda s: s.replace(str(_LG), "urn:uuid:" + str(_LG)),
    ],
)
def test_noncanonical_uuid_rejected(mutate) -> None:
    encoded = encode_investment_goal_risk_fact(_fact(portfolio_id=_LP, goal_id=_LG))
    assert decode_investment_goal_risk_fact(encoded).goal_id == _LG
    raw = mutate(encoded.decode("utf-8")).encode("utf-8")
    assert raw != encoded
    with pytest.raises(ValueError, match=_EITHER):
        decode_investment_goal_risk_fact(raw)


@pytest.mark.parametrize("stamp", ["20270701", "2027-7-1", "2027-07-1", "2027-W26-4", "2027-182",
                                   " 2027-07-01", "2027-07-01 "])
def test_noncanonical_date_rejected(stamp: str) -> None:
    raw = _LITERAL.replace(b"2027-07-01", stamp.encode("utf-8"))
    with pytest.raises(ValueError, match=_EITHER):
        decode_investment_goal_risk_fact(raw)


@pytest.mark.parametrize("stamp", ["2026-08-28T13:00:00+03:00", "2026-08-28T10:00:00Z", "2026-08-28 10:00:00+00:00",
                                   "2026-08-28T10:00:00.000000+00:00", "20260828T100000+0000"])
def test_noncanonical_archived_at_rejected(stamp: str) -> None:
    raw = encode_investment_goal_risk_fact(_fact(archived_at=_T)).replace(
        b"2026-08-28T10:00:00+00:00", stamp.encode("utf-8"))
    with pytest.raises(ValueError, match=_EITHER):
        decode_investment_goal_risk_fact(raw)


def test_whitespace_variants_rejected() -> None:
    spaced = json.dumps(_canonical_dict(), sort_keys=True).encode("utf-8")
    assert spaced != _LITERAL
    for raw in (spaced, _LITERAL + b"\n", b" " + _LITERAL, b"\n" + _LITERAL):
        with pytest.raises(ValueError, match=_CANON_MSG):
            decode_investment_goal_risk_fact(raw)


def test_key_order_variant_rejected() -> None:
    d = _canonical_dict()
    reordered = {"payload": d["payload"], "schema_version": 1, "kind": "investment_goal"}
    raw = json.dumps(reordered, sort_keys=False, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_investment_goal_risk_fact(raw)
    payload_reordered = dict(reversed(list(d["payload"].items())))
    raw = json.dumps({"kind": d["kind"], "payload": payload_reordered, "schema_version": 1},
                     sort_keys=False, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_investment_goal_risk_fact(raw)


def test_unicode_escape_variant_rejected() -> None:
    raw = _LITERAL.replace(b'"investment_goal"', b'"investment\\u005fgoal"')
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_investment_goal_risk_fact(raw)


# --- resource ceiling ---------------------------------------------------------

@pytest.mark.parametrize("text", ["1E+999999999", "1E-999999999", "9" * 5000, "1E+4200", "1E-4200",
                                  "1" + "0" * 4200, "0." + "0" * 4200 + "1"])
def test_oversized_decimal_rejected_before_fixed_point_format(monkeypatch, text: str) -> None:
    fact = _fact(target_amount=Decimal(text))
    monkeypatch.setattr(module_under_test, "format", _forbid, raising=False)
    with pytest.raises(ValueError, match=_SIZE_AMOUNT_MSG):
        encode_investment_goal_risk_fact(fact)


def test_oversize_error_does_not_leak_decimal() -> None:
    with pytest.raises(ValueError) as info:
        encode_investment_goal_risk_fact(_fact(target_amount=Decimal("1E+999999999")))
    assert "999999999" not in str(info.value)


def test_large_but_safe_decimals_round_trip() -> None:
    for amount in (Decimal("9" * 3000), Decimal("1" + "0" * 3000), Decimal("0." + "0" * 2000 + "7"),
                   Decimal("1E+3000")):
        fact = _fact(target_amount=amount)
        encoded = encode_investment_goal_risk_fact(fact)
        assert len(encoded) <= 4096
        decoded = decode_investment_goal_risk_fact(encoded)
        assert decoded == fact
        assert encode_investment_goal_risk_fact(decoded) == encoded


def test_content_ceiling_boundary() -> None:
    overhead = len(encode_investment_goal_risk_fact(_fact(target_amount=Decimal("1")))) - 1
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        encode_investment_goal_risk_fact(_fact(target_amount=Decimal("9" * (4096 - overhead + 5))))
    ok = encode_investment_goal_risk_fact(_fact(target_amount=Decimal("9" * (4096 - overhead - 10))))
    assert len(ok) <= 4096
    assert module_under_test._MAX_CANONICAL_CONTENT_BYTES == 4096


def test_oversized_decoder_content_rejected_before_parsing(monkeypatch) -> None:
    monkeypatch.setattr(json, "loads", _forbid)
    for content in (b" " * 4097, b"\xff" * 5000, _LITERAL + b" " * 4096):
        assert type(content) is bytes
        with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
            decode_investment_goal_risk_fact(content)


def test_content_at_exactly_ceiling_reaches_parser() -> None:
    raw = b"[" + b" " * 4094 + b"]"
    assert len(raw) == 4096
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_investment_goal_risk_fact(raw)


def test_type_check_precedes_size_check() -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        decode_investment_goal_risk_fact(bytearray(b" " * 5000))  # type: ignore[arg-type]


def test_ceiling_is_not_a_fact_rule() -> None:
    fact = _fact(target_amount=Decimal("1E+999999999"))
    assert fact.target_amount == Decimal("1E+999999999")


# --- static errors / callback safety ----------------------------------------

def test_errors_do_not_leak_content() -> None:
    secret = "SECRET-ATTACKER-TEXT"
    d = _canonical_dict()
    d["payload"]["target_amount"] = secret
    with pytest.raises(ValueError) as info:
        decode_investment_goal_risk_fact(_compact(d))
    assert secret not in str(info.value)
    with pytest.raises(ValueError) as info:
        decode_investment_goal_risk_fact(b'{"a":"' + secret.encode() + b'"')
    assert secret not in str(info.value)


def test_hostile_objects_do_not_trigger_callbacks() -> None:
    for hostile in (_Hostile(), _HostileMetaInstance()):
        with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
            encode_investment_goal_risk_fact(hostile)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=_BYTES_MSG):
            decode_investment_goal_risk_fact(hostile)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=_MSG["currency"]):
            _fact(target_currency=hostile)
        with pytest.raises(TypeError, match=_MSG["amount_type"]):
            _fact(target_amount=hostile)
        with pytest.raises(TypeError, match=_MSG["status"]):
            _fact(status=hostile)


# --- purity / scope ----------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_pure_and_independent() -> None:
    imports = _imports()
    for banned in ("hashlib", "hmac", "os", "pathlib", "random", "secrets"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.portfolio") for m in imports)
    assert not any("persistence" in m or "repository" in m for m in imports)
    assert "backend.engine.private.risk_evidence_cash_schema" not in imports
    assert "backend.engine.private.risk_evidence_planned_contribution_schema" not in imports
    assert "backend.engine.private.domain" in imports


def test_no_composition_or_adapter_surface() -> None:
    imports = _imports()
    for banned in ("backend.engine.private.risk_evidence", "backend.engine.private.risk_evidence_content_match",
                   "backend.engine.private.risk_evidence_kind_binding",
                   "backend.engine.private.risk_evidence_resolution"):
        assert banned not in imports
    for name in ("from_investment_goal", "build_investment_goal_risk_fact",
                 "resolve_investment_goal_risk_evidence", "RiskAssessmentFact"):
        assert not hasattr(module_under_test, name)
