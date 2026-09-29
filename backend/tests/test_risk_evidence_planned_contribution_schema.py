"""
backend/tests/test_risk_evidence_planned_contribution_schema.py
===============================================================
Tests for the canonical PLANNED_CONTRIBUTION risk-evidence schema (Phase 15C.5).

PlannedContributionRiskFact <-> canonical envelope bytes. No hashing, no composition,
no missing branch, no source-construction adapter.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import risk_evidence_planned_contribution_schema as module_under_test
from backend.engine.private.domain import ContributionStatus, Currency, PortfolioMode
from backend.engine.private.risk_evidence_planned_contribution_schema import (
    PlannedContributionRiskFact,
    decode_planned_contribution_risk_fact,
    encode_planned_contribution_risk_fact,
)

_MSG = {
    "portfolio_id": r"^portfolio_id must be an exact UUID instance$",
    "contribution_id": r"^contribution_id must be an exact UUID instance$",
    "goal_id": r"^goal_id must be None or an exact UUID instance$",
    "cash_bucket_id": r"^cash_bucket_id must be None or an exact UUID instance$",
    "mode": r"^mode must be an exact PortfolioMode instance$",
    "expected_date": r"^expected_date must be an exact date instance$",
    "amount_type": r"^amount must be an exact Decimal instance$",
    "amount_value": r"^amount must be a finite positive Decimal$",
    "currency": r"^currency must be an exact Currency instance$",
    "status": r"^status must be an exact ContributionStatus instance$",
}
_FACT_TYPE_MSG = r"^fact must be an exact PlannedContributionRiskFact instance$"
_BYTES_MSG = r"^content must be exact bytes$"
_JSON_MSG = r"^planned contribution risk evidence content must be valid canonical UTF-8 JSON$"
_ENVELOPE_MSG = r"^planned contribution risk evidence envelope is invalid$"
_PAYLOAD_MSG = r"^planned contribution risk evidence payload is invalid$"
_CANON_MSG = r"^planned contribution risk evidence content is not canonical$"
_SIZE_AMOUNT_MSG = r"^amount canonical representation exceeds maximum supported size$"
_SIZE_CONTENT_MSG = r"^planned contribution risk evidence content exceeds maximum supported size$"
_EITHER = r"^planned contribution risk evidence (payload is invalid|content is not canonical)$"

_P = UUID("11111111-1111-4111-8111-111111111111")
_C = UUID("33333333-3333-4333-8333-333333333333")
_G = UUID("44444444-4444-4444-8444-444444444444")
_B = UUID("55555555-5555-4555-8555-555555555555")
_D = date(2026, 10, 1)


def _fact(**overrides) -> PlannedContributionRiskFact:
    kwargs = dict(
        portfolio_id=_P,
        mode=PortfolioMode.MY_PORTFOLIO,
        contribution_id=_C,
        goal_id=_G,
        cash_bucket_id=None,
        expected_date=_D,
        amount=Decimal("25000.50"),
        currency=Currency.TRY,
        status=ContributionStatus.PLANNED,
    )
    kwargs.update(overrides)
    return PlannedContributionRiskFact(**kwargs)


_LITERAL = (
    b'{"kind":"planned_contribution","payload":{"amount":"25000.5","cash_bucket_id":null,'
    b'"contribution_id":"33333333-3333-4333-8333-333333333333","currency":"TRY",'
    b'"expected_date":"2026-10-01","goal_id":"44444444-4444-4444-8444-444444444444",'
    b'"mode":"my_portfolio","portfolio_id":"11111111-1111-4111-8111-111111111111",'
    b'"status":"planned"},"schema_version":1}'
)


def _canonical_dict() -> dict:
    return json.loads(encode_planned_contribution_risk_fact(_fact()).decode("utf-8"))


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


class _FactSub(PlannedContributionRiskFact):
    pass


def _forbid(*_args, **_kwargs):
    raise AssertionError("expensive operation reached")


# --- fact -------------------------------------------------------------------

def test_fact_is_frozen_with_exactly_nine_fields() -> None:
    assert [f.name for f in dataclasses.fields(PlannedContributionRiskFact)] == [
        "portfolio_id", "mode", "contribution_id", "goal_id", "cash_bucket_id",
        "expected_date", "amount", "currency", "status",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _fact().amount = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize("attr", ["created_at", "owner_id", "kind", "schema_version", "context", "available_at",
                                  "knowledge_cutoff", "source_key", "content_sha256", "digest", "score",
                                  "capacity", "level", "suitability", "missing"])
def test_fact_has_no_extra_surface(attr: str) -> None:
    assert not hasattr(_fact(), attr)


@pytest.mark.parametrize("goal,bucket", [(None, None), (_G, None), (None, _B), (_G, _B)])
def test_all_link_combinations_valid_and_round_trip(goal, bucket) -> None:
    fact = _fact(goal_id=goal, cash_bucket_id=bucket)
    assert decode_planned_contribution_risk_fact(encode_planned_contribution_risk_fact(fact)) == fact


@pytest.mark.parametrize("status", list(ContributionStatus))
@pytest.mark.parametrize("mode", list(PortfolioMode))
def test_all_statuses_and_modes_round_trip(status, mode) -> None:
    fact = _fact(status=status, mode=mode)
    encoded = encode_planned_contribution_risk_fact(fact)
    decoded = decode_planned_contribution_risk_fact(encoded)
    assert decoded == fact
    assert encode_planned_contribution_risk_fact(decoded) == encoded


def test_status_members_are_exactly_the_four_lifecycle_values() -> None:
    assert [s.value for s in ContributionStatus] == ["planned", "confirmed", "cancelled", "received"]


@pytest.mark.parametrize("field", ["portfolio_id", "contribution_id"])
@pytest.mark.parametrize("value", [str(_P), _P.int, True, None, object(), _Hostile(), _UUIDSub(str(_P))])
def test_invalid_required_uuid(field: str, value) -> None:
    with pytest.raises(TypeError, match=_MSG[field]):
        _fact(**{field: value})


@pytest.mark.parametrize("field", ["goal_id", "cash_bucket_id"])
@pytest.mark.parametrize("value", [str(_G), _G.int, True, object(), _Hostile(), _UUIDSub(str(_G))])
def test_invalid_optional_uuid(field: str, value) -> None:
    with pytest.raises(TypeError, match=_MSG[field]):
        _fact(**{field: value})


@pytest.mark.parametrize("value", ["my_portfolio", None, 1, object()])
def test_invalid_mode(value) -> None:
    with pytest.raises(TypeError, match=_MSG["mode"]):
        _fact(mode=value)


@pytest.mark.parametrize("value", ["TRY", None, 1, object()])
def test_invalid_currency(value) -> None:
    with pytest.raises(TypeError, match=_MSG["currency"]):
        _fact(currency=value)


@pytest.mark.parametrize("value", ["planned", None, 1, object(), _Hostile()])
def test_invalid_status(value) -> None:
    with pytest.raises(TypeError, match=_MSG["status"]):
        _fact(status=value)


def test_past_dates_are_valid() -> None:
    assert _fact(expected_date=date(1999, 1, 1)).expected_date == date(1999, 1, 1)


@pytest.mark.parametrize("value", [datetime(2026, 10, 1), datetime(2026, 10, 1, 12), "2026-10-01", 20261001, 1.5,
                                   True, None, object(), _Hostile()])
def test_invalid_expected_date(value) -> None:
    with pytest.raises(TypeError, match=_MSG["expected_date"]):
        _fact(expected_date=value)


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("0.00"), Decimal("-0"), Decimal("-1"),
                                    Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_invalid_amount_values(amount: Decimal) -> None:
    with pytest.raises(ValueError, match=_MSG["amount_value"]):
        _fact(amount=amount)


@pytest.mark.parametrize("amount", [1, 1.0, True, "1", None, b"1", _DecimalSub("1"), _Hostile()])
def test_invalid_amount_types(amount) -> None:
    with pytest.raises(TypeError, match=_MSG["amount_type"]):
        _fact(amount=amount)


@pytest.mark.parametrize("amount", [Decimal("0.0000000001"), Decimal("1"), Decimal("1E+3"),
                                    Decimal("0.123456789012345678901234567890")])
def test_valid_amounts(amount: Decimal) -> None:
    assert _fact(amount=amount).amount == amount


# --- encoder ----------------------------------------------------------------

def test_encoder_exact_literal_bytes() -> None:
    out = encode_planned_contribution_risk_fact(_fact())
    assert type(out) is bytes
    assert out == _LITERAL
    assert not out.endswith(b"\n")


@pytest.mark.parametrize("text,expected", [
    ("1", "1"), ("1.0", "1"), ("1.00", "1"), ("1E+0", "1"),
    ("1000.00", "1000"), ("1E+3", "1000"), ("0.00100", "0.001"),
    ("25000.50", "25000.5"), ("10", "10"), ("100E-2", "1"),
])
def test_decimal_canonicalization_corpus(text: str, expected: str) -> None:
    out = encode_planned_contribution_risk_fact(_fact(amount=Decimal(text)))
    assert f'"amount":"{expected}"'.encode() in out


@pytest.mark.parametrize(
    "override",
    [
        {"portfolio_id": UUID(int=9)},
        {"mode": PortfolioMode.SANDBOX},
        {"contribution_id": UUID(int=9)},
        {"goal_id": None},
        {"goal_id": UUID(int=9)},
        {"cash_bucket_id": _B},
        {"expected_date": date(2026, 10, 2)},
        {"amount": Decimal("25000.51")},
        {"currency": Currency.USD},
        {"status": ContributionStatus.RECEIVED},
    ],
)
def test_each_field_changes_bytes(override) -> None:
    assert encode_planned_contribution_risk_fact(_fact(**override)) != encode_planned_contribution_risk_fact(_fact())


def test_expected_date_encodes_date_only() -> None:
    out = encode_planned_contribution_risk_fact(_fact(expected_date=date(2026, 1, 5)))
    assert b'"expected_date":"2026-01-05"' in out
    assert b"T" not in out.split(b'"expected_date"')[1].split(b",")[0]


@pytest.mark.parametrize("bad", [None, "x", {}, object(), _Hostile(), _HostileMetaInstance()])
def test_encoder_rejects_non_fact(bad) -> None:
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        encode_planned_contribution_risk_fact(bad)  # type: ignore[arg-type]


def test_encoder_rejects_fact_subclass() -> None:
    sub = _FactSub(portfolio_id=_P, mode=PortfolioMode.MY_PORTFOLIO, contribution_id=_C, goal_id=None,
                   cash_bucket_id=None, expected_date=_D, amount=Decimal("1"), currency=Currency.TRY,
                   status=ContributionStatus.PLANNED)
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        encode_planned_contribution_risk_fact(sub)


# --- decoder happy path / boundary -------------------------------------------

def test_round_trip_and_literal() -> None:
    fact = _fact()
    encoded = encode_planned_contribution_risk_fact(fact)
    decoded = decode_planned_contribution_risk_fact(encoded)
    assert type(decoded) is PlannedContributionRiskFact
    assert decoded == fact
    assert encode_planned_contribution_risk_fact(decoded) == encoded
    assert decode_planned_contribution_risk_fact(_LITERAL) == fact


@pytest.mark.parametrize("bad", [bytearray(_LITERAL), memoryview(_LITERAL), _LITERAL.decode(), None,
                                 _BytesSub(_LITERAL), 1, object(), _Hostile()])
def test_decoder_rejects_non_exact_bytes(bad) -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        decode_planned_contribution_risk_fact(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("content", [b"\xff\xfe", b"", b"{", b"nul", b"\xef\xbb\xbf" + _LITERAL,
                                     _LITERAL.replace(b'"25000.5"', b"NaN"),
                                     _LITERAL.replace(b'"25000.5"', b"Infinity"), b"[" * 4000])
def test_invalid_utf8_or_json(content: bytes) -> None:
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_planned_contribution_risk_fact(content)


@pytest.mark.parametrize("content", [b"[]", b"1", b"null", b'"planned_contribution"', b"true"])
def test_non_object_top_level(content: bytes) -> None:
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_planned_contribution_risk_fact(content)


@pytest.mark.parametrize("old,new", [
    (b'"kind":"planned_contribution"', b'"kind":"planned_contribution","kind":"cash_balance"'),
    (b'"amount":"25000.5"', b'"amount":"25000.5","amount":"9"'),
    (b'"goal_id":"44444444-4444-4444-8444-444444444444"',
     b'"goal_id":null,"goal_id":"44444444-4444-4444-8444-444444444444"'),
    (b'"schema_version":1', b'"schema_version":1,"schema_version":2'),
])
def test_duplicate_keys_rejected(old: bytes, new: bytes) -> None:
    assert old in _LITERAL
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_planned_contribution_risk_fact(_LITERAL.replace(old, new))


# --- envelope / payload validation ------------------------------------------

def test_extra_and_missing_top_level_keys() -> None:
    extra = _canonical_dict()
    extra["extra"] = 1
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_planned_contribution_risk_fact(_compact(extra))
    for key in ("kind", "schema_version", "payload"):
        d = _canonical_dict()
        del d[key]
        with pytest.raises(ValueError, match=_ENVELOPE_MSG):
            decode_planned_contribution_risk_fact(_compact(d))


@pytest.mark.parametrize("kind", ["PLANNED_CONTRIBUTION", "cash_balance", "investment_goal", "", None, 1, True, []])
def test_wrong_kind(kind) -> None:
    d = _canonical_dict()
    d["kind"] = kind
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_planned_contribution_risk_fact(_compact(d))


@pytest.mark.parametrize("version", [0, 2, -1, True, False, 1.0, "1", None, [], {}])
def test_wrong_schema_version(version) -> None:
    d = _canonical_dict()
    d["schema_version"] = version
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_planned_contribution_risk_fact(_compact(d))


@pytest.mark.parametrize("payload", [[], None, "x", 1, True])
def test_payload_must_be_object(payload) -> None:
    d = _canonical_dict()
    d["payload"] = payload
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_planned_contribution_risk_fact(_compact(d))


_PAYLOAD_KEYS = ["amount", "cash_bucket_id", "contribution_id", "currency", "expected_date", "goal_id",
                 "mode", "portfolio_id", "status"]


def test_extra_and_missing_payload_keys() -> None:
    for extra_key in ("owner_id", "created_at"):
        extra = _canonical_dict()
        extra["payload"][extra_key] = str(_P)
        with pytest.raises(ValueError, match=_PAYLOAD_MSG):
            decode_planned_contribution_risk_fact(_compact(extra))
    for key in _PAYLOAD_KEYS:
        d = _canonical_dict()
        del d["payload"][key]
        with pytest.raises(ValueError, match=_PAYLOAD_MSG):
            decode_planned_contribution_risk_fact(_compact(d))


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", 1), ("amount", 1.0), ("amount", True), ("amount", None), ("amount", []),
        ("amount", "0"), ("amount", "-1"), ("amount", "abc"), ("amount", ""), ("amount", "NaN"),
        ("amount", "Infinity"), ("amount", "1E+999999999"),
        ("currency", None), ("currency", 1), ("currency", "try"), ("currency", "CHF"),
        ("mode", 1), ("mode", None), ("mode", "MY_PORTFOLIO"), ("mode", "unknown"),
        ("status", None), ("status", 1), ("status", "PLANNED"), ("status", "Planned"), ("status", "paid"),
        ("portfolio_id", None), ("portfolio_id", 1), ("portfolio_id", "not-a-uuid"),
        ("contribution_id", None), ("contribution_id", True), ("contribution_id", ""),
        ("goal_id", 1), ("goal_id", True), ("goal_id", "not-a-uuid"), ("goal_id", ""),
        ("cash_bucket_id", 1), ("cash_bucket_id", []), ("cash_bucket_id", "x"),
        ("expected_date", None), ("expected_date", 20261001), ("expected_date", True),
        ("expected_date", "garbage"), ("expected_date", "2026-13-01"), ("expected_date", "2026-02-30"),
        ("expected_date", "2026-10-01T00:00:00"), ("expected_date", "2026-10-01T00:00:00+00:00"),
        ("expected_date", "0000-01-01"),
    ],
)
def test_payload_scalar_confusion(field: str, value) -> None:
    d = _canonical_dict()
    d["payload"][field] = value
    with pytest.raises(ValueError, match=_PAYLOAD_MSG):
        decode_planned_contribution_risk_fact(_compact(d))


# --- canonicality ------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["1.0", "1.00", "1E+0", "+1", "-0", "01", "1.", ".5", "25000.50",
                                      "2.50E+4", "0.0", "0E+0"])
def test_noncanonical_decimal_rejected(spelling: str) -> None:
    d = _canonical_dict()
    d["payload"]["amount"] = spelling
    with pytest.raises(ValueError, match=_EITHER):
        decode_planned_contribution_risk_fact(_compact(d))


def test_canonical_decimals_accepted() -> None:
    for text in ("1", "25000.5", "0.0001", "1000"):
        d = _canonical_dict()
        d["payload"]["amount"] = text
        assert decode_planned_contribution_risk_fact(_compact(d)).amount == Decimal(text)


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
    encoded = encode_planned_contribution_risk_fact(_fact(portfolio_id=_LP, goal_id=_LG))
    assert decode_planned_contribution_risk_fact(encoded).goal_id == _LG
    raw = mutate(encoded.decode("utf-8")).encode("utf-8")
    assert raw != encoded
    with pytest.raises(ValueError, match=_EITHER):
        decode_planned_contribution_risk_fact(raw)


@pytest.mark.parametrize("stamp", ["20261001", "2026-1-1", "2026-10-1", "2026-W40-4", "2026-274",
                                   " 2026-10-01", "2026-10-01 "])
def test_noncanonical_date_rejected(stamp: str) -> None:
    raw = _LITERAL.replace(b"2026-10-01", stamp.encode("utf-8"))
    with pytest.raises(ValueError, match=_EITHER):
        decode_planned_contribution_risk_fact(raw)


def test_whitespace_variants_rejected() -> None:
    spaced = json.dumps(_canonical_dict(), sort_keys=True).encode("utf-8")
    assert spaced != _LITERAL
    for raw in (spaced, _LITERAL + b"\n", b" " + _LITERAL, b"\n" + _LITERAL):
        with pytest.raises(ValueError, match=_CANON_MSG):
            decode_planned_contribution_risk_fact(raw)


def test_key_order_variant_rejected() -> None:
    d = _canonical_dict()
    reordered = {"payload": d["payload"], "schema_version": 1, "kind": "planned_contribution"}
    raw = json.dumps(reordered, sort_keys=False, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_planned_contribution_risk_fact(raw)
    payload_reordered = dict(reversed(list(d["payload"].items())))
    raw = json.dumps({"kind": d["kind"], "payload": payload_reordered, "schema_version": 1},
                     sort_keys=False, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_planned_contribution_risk_fact(raw)


def test_unicode_escape_variant_rejected() -> None:
    raw = _LITERAL.replace(b'"planned_contribution"', b'"planned\\u005fcontribution"')
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_planned_contribution_risk_fact(raw)


# --- resource ceiling ---------------------------------------------------------

@pytest.mark.parametrize("text", ["1E+999999999", "1E-999999999", "9" * 5000, "1E+4200", "1E-4200",
                                  "1" + "0" * 4200, "0." + "0" * 4200 + "1"])
def test_oversized_decimal_rejected_before_fixed_point_format(monkeypatch, text: str) -> None:
    fact = _fact(amount=Decimal(text))
    monkeypatch.setattr(module_under_test, "format", _forbid, raising=False)
    with pytest.raises(ValueError, match=_SIZE_AMOUNT_MSG):
        encode_planned_contribution_risk_fact(fact)


def test_oversize_error_does_not_leak_decimal() -> None:
    with pytest.raises(ValueError) as info:
        encode_planned_contribution_risk_fact(_fact(amount=Decimal("1E+999999999")))
    assert "999999999" not in str(info.value)


def test_large_but_safe_decimals_round_trip() -> None:
    for amount in (Decimal("9" * 3000), Decimal("1" + "0" * 3000), Decimal("0." + "0" * 2000 + "7"),
                   Decimal("1E+3000")):
        fact = _fact(amount=amount)
        encoded = encode_planned_contribution_risk_fact(fact)
        assert len(encoded) <= 4096
        decoded = decode_planned_contribution_risk_fact(encoded)
        assert decoded == fact
        assert encode_planned_contribution_risk_fact(decoded) == encoded


def test_content_ceiling_boundary() -> None:
    overhead = len(encode_planned_contribution_risk_fact(_fact(amount=Decimal("1")))) - 1
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        encode_planned_contribution_risk_fact(_fact(amount=Decimal("9" * (4096 - overhead + 5))))
    ok = encode_planned_contribution_risk_fact(_fact(amount=Decimal("9" * (4096 - overhead - 10))))
    assert len(ok) <= 4096
    assert module_under_test._MAX_CANONICAL_CONTENT_BYTES == 4096


def test_oversized_decoder_content_rejected_before_parsing(monkeypatch) -> None:
    monkeypatch.setattr(json, "loads", _forbid)
    for content in (b" " * 4097, b"\xff" * 5000, _LITERAL + b" " * 4096):
        assert type(content) is bytes
        with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
            decode_planned_contribution_risk_fact(content)


def test_content_at_exactly_ceiling_reaches_parser() -> None:
    raw = b"[" + b" " * 4094 + b"]"
    assert len(raw) == 4096
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_planned_contribution_risk_fact(raw)


def test_type_check_precedes_size_check() -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        decode_planned_contribution_risk_fact(bytearray(b" " * 5000))  # type: ignore[arg-type]


def test_existing_literal_unchanged_and_ceiling_is_not_a_fact_rule() -> None:
    assert encode_planned_contribution_risk_fact(_fact()) == _LITERAL
    fact = _fact(amount=Decimal("1E+999999999"))  # fact may hold it; only the codec bounds the wire format
    assert fact.amount == Decimal("1E+999999999")


# --- static errors / callback safety ----------------------------------------

def test_errors_do_not_leak_content() -> None:
    secret = "SECRET-ATTACKER-TEXT"
    d = _canonical_dict()
    d["payload"]["amount"] = secret
    with pytest.raises(ValueError) as info:
        decode_planned_contribution_risk_fact(_compact(d))
    assert secret not in str(info.value)
    with pytest.raises(ValueError) as info:
        decode_planned_contribution_risk_fact(b'{"a":"' + secret.encode() + b'"')
    assert secret not in str(info.value)


def test_hostile_objects_do_not_trigger_callbacks() -> None:
    for hostile in (_Hostile(), _HostileMetaInstance()):
        with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
            encode_planned_contribution_risk_fact(hostile)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=_BYTES_MSG):
            decode_planned_contribution_risk_fact(hostile)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=_MSG["currency"]):
            _fact(currency=hostile)
        with pytest.raises(TypeError, match=_MSG["amount_type"]):
            _fact(amount=hostile)
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
    assert "backend.engine.private.domain" in imports


def test_no_composition_or_adapter_surface() -> None:
    imports = _imports()
    for banned in ("backend.engine.private.risk_evidence", "backend.engine.private.risk_evidence_content_match",
                   "backend.engine.private.risk_evidence_kind_binding",
                   "backend.engine.private.risk_evidence_resolution"):
        assert banned not in imports
    for name in ("from_planned_contribution", "build_planned_contribution_risk_fact",
                 "resolve_planned_contribution_risk_evidence", "RiskAssessmentFact"):
        assert not hasattr(module_under_test, name)
