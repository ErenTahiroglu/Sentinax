"""
backend/tests/test_risk_evidence_cash_schema.py
===============================================
Tests for the canonical CASH_BALANCE risk-evidence schema (Phase 15C.3).

CashBalanceRiskFact <-> canonical envelope bytes. No hashing, no binding composition,
no missing branch: zero balance is known cash, not missing evidence.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import risk_evidence_cash_schema as module_under_test
from backend.engine.private.domain import Currency, PortfolioMode
from backend.engine.private.risk_evidence_cash_schema import (
    CashBalanceRiskFact,
    decode_cash_balance_risk_fact,
    encode_cash_balance_risk_fact,
)

_FACT_MSGS = {
    "portfolio_id": r"^portfolio_id must be an exact UUID instance$",
    "account_id": r"^account_id must be an exact UUID instance$",
    "currency": r"^currency must be an exact Currency instance$",
    "mode": r"^mode must be an exact PortfolioMode instance$",
    "balance_type": r"^balance must be an exact Decimal instance$",
    "balance_value": r"^balance must be a finite non-negative Decimal$",
    "as_of": r"^as_of_recorded_at must be None or an exact timezone-aware datetime$",
}
_FACT_TYPE_MSG = r"^fact must be an exact CashBalanceRiskFact instance$"
_BYTES_MSG = r"^content must be exact bytes$"
_JSON_MSG = r"^cash balance risk evidence content must be valid canonical UTF-8 JSON$"
_ENVELOPE_MSG = r"^cash balance risk evidence envelope is invalid$"
_PAYLOAD_MSG = r"^cash balance risk evidence payload is invalid$"
_CANON_MSG = r"^cash balance risk evidence content is not canonical$"

_P = UUID("11111111-1111-4111-8111-111111111111")
_A = UUID("22222222-2222-4222-8222-222222222222")
_T = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)


def _fact(**overrides) -> CashBalanceRiskFact:
    kwargs = dict(
        portfolio_id=_P,
        account_id=_A,
        currency=Currency.TRY,
        balance=Decimal("1234.50"),
        mode=PortfolioMode.MY_PORTFOLIO,
        as_of_recorded_at=_T,
    )
    kwargs.update(overrides)
    return CashBalanceRiskFact(**kwargs)


_LITERAL = (
    b'{"kind":"cash_balance","payload":{"account_id":"22222222-2222-4222-8222-222222222222",'
    b'"as_of_recorded_at":"2026-08-28T10:00:00+00:00","balance":"1234.5","currency":"TRY",'
    b'"mode":"my_portfolio","portfolio_id":"11111111-1111-4111-8111-111111111111"},"schema_version":1}'
)


def _canonical_dict() -> dict:
    return json.loads(encode_cash_balance_risk_fact(_fact()).decode("utf-8"))


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


class _BytesSub(bytes):
    pass


class _FactSub(CashBalanceRiskFact):
    pass


# --- fact -------------------------------------------------------------------

def test_fact_is_frozen_with_exactly_six_fields() -> None:
    assert [f.name for f in dataclasses.fields(CashBalanceRiskFact)] == [
        "portfolio_id", "account_id", "currency", "balance", "mode", "as_of_recorded_at",
    ]
    fact = _fact()
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.balance = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize("attr", ["owner_id", "kind", "schema_version", "context", "source_key",
                                  "content_sha256", "score", "status", "suitability", "level",
                                  "missing", "positive_balances"])
def test_fact_has_no_extra_surface(attr: str) -> None:
    assert not hasattr(_fact(), attr)


@pytest.mark.parametrize("balance", [Decimal("0"), Decimal("0.00"), Decimal("1"), Decimal("1E+3"),
                                     Decimal("0.123456789012345678901234567890")])
def test_valid_balances(balance: Decimal) -> None:
    assert _fact(balance=balance).balance == balance


def test_zero_balance_is_known_cash() -> None:
    fact = _fact(balance=Decimal("0"))
    assert fact.balance == Decimal("0")
    assert decode_cash_balance_risk_fact(encode_cash_balance_risk_fact(fact)) == fact


@pytest.mark.parametrize("balance", [Decimal("-1"), Decimal("-0.0000001"), Decimal("NaN"),
                                     Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_invalid_balance_values(balance: Decimal) -> None:
    with pytest.raises(ValueError, match=_FACT_MSGS["balance_value"]):
        _fact(balance=balance)


@pytest.mark.parametrize("balance", [1, 1.0, True, "1", None, b"1", _DecimalSub("1"), _Hostile()])
def test_invalid_balance_types(balance) -> None:
    with pytest.raises(TypeError, match=_FACT_MSGS["balance_type"]):
        _fact(balance=balance)


@pytest.mark.parametrize("field", ["portfolio_id", "account_id"])
@pytest.mark.parametrize("value", [str(_P), _P.int, True, None, object(), _Hostile()])
def test_invalid_uuid_fields(field: str, value) -> None:
    with pytest.raises(TypeError, match=_FACT_MSGS[field]):
        _fact(**{field: value})


@pytest.mark.parametrize("value", ["TRY", None, 1, object(), _Hostile()])
def test_invalid_currency(value) -> None:
    with pytest.raises(TypeError, match=_FACT_MSGS["currency"]):
        _fact(currency=value)


@pytest.mark.parametrize("value", ["my_portfolio", None, 1, PortfolioMode.MY_PORTFOLIO.value, object()])
def test_invalid_mode(value) -> None:
    with pytest.raises(TypeError, match=_FACT_MSGS["mode"]):
        _fact(mode=value)


def test_as_of_none_and_aware_allowed() -> None:
    assert _fact(as_of_recorded_at=None).as_of_recorded_at is None
    assert _fact(as_of_recorded_at=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=3)))).as_of_recorded_at


@pytest.mark.parametrize("value", [datetime(2026, 8, 28, 10, 0, 0), "2026-08-28T10:00:00+00:00", 0, True, object(),
                                   _Hostile()])
def test_invalid_as_of(value) -> None:
    with pytest.raises(TypeError, match=_FACT_MSGS["as_of"]):
        _fact(as_of_recorded_at=value)


# --- encoder ----------------------------------------------------------------

def test_encoder_exact_literal_bytes() -> None:
    out = encode_cash_balance_risk_fact(_fact())
    assert type(out) is bytes
    assert out == _LITERAL
    assert not out.endswith(b"\n")


@pytest.mark.parametrize("text", ["1", "1.0", "1.00", "1E+0"])
def test_decimal_spellings_encode_identically(text: str) -> None:
    assert encode_cash_balance_risk_fact(_fact(balance=Decimal(text))) == \
        encode_cash_balance_risk_fact(_fact(balance=Decimal("1")))
    assert b'"balance":"1"' in encode_cash_balance_risk_fact(_fact(balance=Decimal(text)))


@pytest.mark.parametrize("text", ["1E+3", "1000.00", "1000"])
def test_decimal_thousand_spellings(text: str) -> None:
    assert b'"balance":"1000"' in encode_cash_balance_risk_fact(_fact(balance=Decimal(text)))


def test_zero_spellings_encode_as_zero() -> None:
    for text in ("0", "0.00", "0E+5", "-0", "-0.0"):
        assert b'"balance":"0"' in encode_cash_balance_risk_fact(_fact(balance=Decimal(text)))


def test_equivalent_offsets_encode_identically() -> None:
    utc = _fact(as_of_recorded_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc))
    plus3 = _fact(as_of_recorded_at=datetime(2026, 8, 28, 13, 0, 0, tzinfo=timezone(timedelta(hours=3))))
    assert encode_cash_balance_risk_fact(utc) == encode_cash_balance_risk_fact(plus3)


def test_microseconds_preserved() -> None:
    out = encode_cash_balance_risk_fact(_fact(as_of_recorded_at=_T.replace(microsecond=123456)))
    assert b"2026-08-28T10:00:00.123456+00:00" in out


def test_none_as_of_encodes_null() -> None:
    assert b'"as_of_recorded_at":null' in encode_cash_balance_risk_fact(_fact(as_of_recorded_at=None))


@pytest.mark.parametrize(
    "override",
    [
        {"portfolio_id": UUID("33333333-3333-4333-8333-333333333333")},
        {"account_id": UUID("33333333-3333-4333-8333-333333333333")},
        {"currency": Currency.USD},
        {"balance": Decimal("1234.51")},
        {"mode": PortfolioMode.SANDBOX},
        {"as_of_recorded_at": _T + timedelta(seconds=1)},
        {"as_of_recorded_at": None},
    ],
)
def test_each_field_changes_bytes(override) -> None:
    assert encode_cash_balance_risk_fact(_fact(**override)) != encode_cash_balance_risk_fact(_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, object(), _Hostile(), _HostileMetaInstance()])
def test_encoder_rejects_non_fact(bad) -> None:
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        encode_cash_balance_risk_fact(bad)  # type: ignore[arg-type]


def test_encoder_rejects_fact_subclass() -> None:
    sub = _FactSub(portfolio_id=_P, account_id=_A, currency=Currency.TRY, balance=Decimal("1"),
                   mode=PortfolioMode.MY_PORTFOLIO, as_of_recorded_at=None)
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        encode_cash_balance_risk_fact(sub)


# --- decoder happy path ------------------------------------------------------

def test_round_trip() -> None:
    fact = _fact()
    encoded = encode_cash_balance_risk_fact(fact)
    decoded = decode_cash_balance_risk_fact(encoded)
    assert type(decoded) is CashBalanceRiskFact
    assert decoded == fact
    assert encode_cash_balance_risk_fact(decoded) == encoded


def test_round_trip_none_as_of_and_sandbox() -> None:
    fact = _fact(as_of_recorded_at=None, mode=PortfolioMode.SANDBOX, currency=Currency.XAU)
    encoded = encode_cash_balance_risk_fact(fact)
    decoded = decode_cash_balance_risk_fact(encoded)
    assert decoded == fact
    assert encode_cash_balance_risk_fact(decoded) == encoded


def test_decode_literal() -> None:
    assert decode_cash_balance_risk_fact(_LITERAL) == _fact()


# --- decoder boundary rejections ---------------------------------------------

@pytest.mark.parametrize("bad", [bytearray(_LITERAL), memoryview(_LITERAL), _LITERAL.decode(), None,
                                 _BytesSub(_LITERAL), 1, object(), _Hostile()])
def test_decoder_rejects_non_exact_bytes(bad) -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        decode_cash_balance_risk_fact(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("content", [b"\xff\xfe", b"", b"{", b"nul", b"\xef\xbb\xbf" + _LITERAL,
                                     _LITERAL.replace(b'"1234.5"', b"NaN"), b"[" * 4000])
def test_invalid_utf8_or_json(content: bytes) -> None:
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_cash_balance_risk_fact(content)


@pytest.mark.parametrize("content", [b"[]", b"1", b"null", b'"cash_balance"', b"true"])
def test_non_object_top_level(content: bytes) -> None:
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_cash_balance_risk_fact(content)


def test_duplicate_top_level_key() -> None:
    raw = _LITERAL.replace(b'"kind":"cash_balance"', b'"kind":"cash_balance","kind":"investment_goal"')
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_cash_balance_risk_fact(raw)


def test_duplicate_payload_key() -> None:
    raw = _LITERAL.replace(b'"balance":"1234.5"', b'"balance":"1234.5","balance":"9"')
    with pytest.raises(ValueError, match=_JSON_MSG):
        decode_cash_balance_risk_fact(raw)


# --- envelope validation -----------------------------------------------------

def test_extra_and_missing_top_level_keys() -> None:
    extra = _canonical_dict()
    extra["extra"] = 1
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_cash_balance_risk_fact(_compact(extra))
    for key in ("kind", "schema_version", "payload"):
        d = _canonical_dict()
        del d[key]
        with pytest.raises(ValueError, match=_ENVELOPE_MSG):
            decode_cash_balance_risk_fact(_compact(d))


@pytest.mark.parametrize("kind", ["investment_goal", "planned_contribution", "CASH_BALANCE", "", None, 1, True, []])
def test_wrong_kind(kind) -> None:
    d = _canonical_dict()
    d["kind"] = kind
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_cash_balance_risk_fact(_compact(d))


@pytest.mark.parametrize("version", [0, 2, -1, True, False, 1.0, "1", None, [], {}])
def test_wrong_schema_version(version) -> None:
    d = _canonical_dict()
    d["schema_version"] = version
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_cash_balance_risk_fact(_compact(d))


@pytest.mark.parametrize("payload", [[], None, "x", 1, True])
def test_payload_must_be_object(payload) -> None:
    d = _canonical_dict()
    d["payload"] = payload
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_cash_balance_risk_fact(_compact(d))


# --- payload validation ------------------------------------------------------

def test_extra_and_missing_payload_keys() -> None:
    extra = _canonical_dict()
    extra["payload"]["owner_id"] = str(_P)
    with pytest.raises(ValueError, match=_PAYLOAD_MSG):
        decode_cash_balance_risk_fact(_compact(extra))
    for key in ("portfolio_id", "account_id", "currency", "balance", "mode", "as_of_recorded_at"):
        d = _canonical_dict()
        del d["payload"][key]
        with pytest.raises(ValueError, match=_PAYLOAD_MSG):
            decode_cash_balance_risk_fact(_compact(d))


@pytest.mark.parametrize(
    "field,value",
    [
        ("balance", 1), ("balance", 1.0), ("balance", True), ("balance", None), ("balance", []),
        ("balance", "-1"), ("balance", "abc"), ("balance", ""), ("balance", "NaN"), ("balance", "Infinity"),
        ("balance", "1E+999999999"),
        ("currency", None), ("currency", 1), ("currency", "try"), ("currency", "CHF"), ("currency", "Currency.TRY"),
        ("mode", 1), ("mode", None), ("mode", "MY_PORTFOLIO"), ("mode", "unknown"),
        ("portfolio_id", None), ("portfolio_id", 1), ("portfolio_id", "not-a-uuid"),
        ("account_id", True), ("account_id", ""),
        ("as_of_recorded_at", 0), ("as_of_recorded_at", True), ("as_of_recorded_at", "garbage"),
        ("as_of_recorded_at", "2026-08-28T10:00:00"),
    ],
)
def test_payload_scalar_confusion(field: str, value) -> None:
    d = _canonical_dict()
    d["payload"][field] = value
    with pytest.raises(ValueError, match=_PAYLOAD_MSG):
        decode_cash_balance_risk_fact(_compact(d))


# --- canonicality ------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["1234.50", "1234.500", "+1234.5", "01234.5", "1.2345E+3"])
def test_noncanonical_decimal_rejected(spelling: str) -> None:
    d = _canonical_dict()
    d["payload"]["balance"] = spelling
    with pytest.raises(ValueError, match=r"^cash balance risk evidence (payload is invalid|content is not canonical)$"):
        decode_cash_balance_risk_fact(_compact(d))


@pytest.mark.parametrize("spelling", ["1.0", "1.00", "1E+0", "+1", "-0", "0.0", "0E+0"])
def test_noncanonical_small_decimals_rejected(spelling: str) -> None:
    d = _canonical_dict()
    d["payload"]["balance"] = spelling
    with pytest.raises(ValueError, match=r"^cash balance risk evidence (payload is invalid|content is not canonical)$"):
        decode_cash_balance_risk_fact(_compact(d))


def test_canonical_small_decimals_accepted() -> None:
    for text in ("1", "0", "0.5", "1000", "10.25"):
        d = _canonical_dict()
        d["payload"]["balance"] = text
        assert decode_cash_balance_risk_fact(_compact(d)).balance == Decimal(text)


_LP = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_LA = UUID("abcdef01-2345-4678-89ab-cdef01234567")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.replace(str(_LP), str(_LP).upper()),
        lambda s: s.replace(str(_LA), str(_LA).upper()),
        lambda s: s.replace(str(_LA), _LA.hex),
        lambda s: s.replace(str(_LA), "{" + str(_LA) + "}"),
        lambda s: s.replace(str(_LA), "urn:uuid:" + str(_LA)),
    ],
)
def test_noncanonical_uuid_rejected(mutate) -> None:
    encoded = encode_cash_balance_risk_fact(_fact(portfolio_id=_LP, account_id=_LA))
    assert decode_cash_balance_risk_fact(encoded).portfolio_id == _LP
    raw = mutate(encoded.decode("utf-8")).encode("utf-8")
    assert raw != encoded
    with pytest.raises(ValueError, match=r"^cash balance risk evidence (payload is invalid|content is not canonical)$"):
        decode_cash_balance_risk_fact(raw)


@pytest.mark.parametrize(
    "stamp",
    ["2026-08-28T13:00:00+03:00", "2026-08-28T10:00:00Z", "2026-08-28 10:00:00+00:00",
     "2026-08-28T10:00:00.000000+00:00", "20260828T100000+0000"],
)
def test_noncanonical_datetime_rejected(stamp: str) -> None:
    raw = _LITERAL.replace(b"2026-08-28T10:00:00+00:00", stamp.encode("utf-8"))
    with pytest.raises(ValueError, match=r"^cash balance risk evidence (payload is invalid|content is not canonical)$"):
        decode_cash_balance_risk_fact(raw)


def test_whitespace_variant_rejected() -> None:
    spaced = json.dumps(_canonical_dict(), sort_keys=True).encode("utf-8")
    assert spaced != _LITERAL
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_cash_balance_risk_fact(spaced)
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_cash_balance_risk_fact(_LITERAL + b"\n")
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_cash_balance_risk_fact(b" " + _LITERAL)


def test_key_order_variant_rejected() -> None:
    d = _canonical_dict()
    reordered = {"payload": d["payload"], "schema_version": 1, "kind": "cash_balance"}
    raw = json.dumps(reordered, sort_keys=False, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_cash_balance_risk_fact(raw)


def test_ascii_escape_variant_rejected() -> None:
    raw = _LITERAL.replace(b'"cash_balance"', b'"cash\\u005fbalance"')
    with pytest.raises(ValueError, match=_CANON_MSG):
        decode_cash_balance_risk_fact(raw)


# --- static errors / callback safety ----------------------------------------

def test_errors_do_not_leak_content() -> None:
    secret = "SECRET-ATTACKER-TEXT"
    d = _canonical_dict()
    d["payload"]["balance"] = secret
    with pytest.raises(ValueError) as info:
        decode_cash_balance_risk_fact(_compact(d))
    assert secret not in str(info.value)
    with pytest.raises(ValueError) as info:
        decode_cash_balance_risk_fact(b'{"a":"' + secret.encode() + b'"')
    assert secret not in str(info.value)


def test_hostile_objects_do_not_trigger_callbacks() -> None:
    for hostile in (_Hostile(), _HostileMetaInstance()):
        with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
            encode_cash_balance_risk_fact(hostile)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=_BYTES_MSG):
            decode_cash_balance_risk_fact(hostile)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=_FACT_MSGS["currency"]):
            _fact(currency=hostile)
        with pytest.raises(TypeError, match=_FACT_MSGS["balance_type"]):
            _fact(balance=hostile)


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


def test_no_digest_or_portfolio_or_persistence_imports() -> None:
    imports = _imports()
    for banned in ("hashlib", "hmac", "os", "pathlib", "random", "secrets", "uuid.uuid4"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.portfolio") for m in imports)
    assert not any("persistence" in m or "repository" in m for m in imports)
    assert "backend.engine.private.domain" in imports


def test_no_composition_with_evidence_chain() -> None:
    imports = _imports()
    for banned in ("backend.engine.private.risk_evidence", "backend.engine.private.risk_evidence_content_match",
                   "backend.engine.private.risk_evidence_kind_binding",
                   "backend.engine.private.risk_evidence_resolution"):
        assert banned not in imports


# --- Phase 15C.3-R1: resource ceiling ---------------------------------------

_SIZE_BALANCE_MSG = r"^balance canonical representation exceeds maximum supported size$"
_SIZE_CONTENT_MSG = r"^cash balance risk evidence content exceeds maximum supported size$"


def _forbid(*_args, **_kwargs):
    raise AssertionError("expensive operation reached")


@pytest.mark.parametrize(
    "text",
    ["1E+999999999", "1E-999999999", "0E-999999999", "9" * 5000, "1E+4200", "1E-4200",
     "1" + "0" * 4200, "0." + "0" * 4200 + "1"],
)
def test_oversized_decimal_rejected_before_fixed_point_format(monkeypatch, text: str) -> None:
    fact = _fact(balance=Decimal(text))
    # If the encoder reached format(..., "f") the sentinel would raise AssertionError.
    monkeypatch.setattr(module_under_test, "format", _forbid, raising=False)
    with pytest.raises(ValueError, match=_SIZE_BALANCE_MSG):
        encode_cash_balance_risk_fact(fact)


def test_oversize_error_does_not_leak_decimal() -> None:
    with pytest.raises(ValueError) as info:
        encode_cash_balance_risk_fact(_fact(balance=Decimal("1E+999999999")))
    assert "999999999" not in str(info.value)


def test_compact_zero_with_positive_exponent_is_cheap_and_valid() -> None:
    out = encode_cash_balance_risk_fact(_fact(balance=Decimal("0E+999999999")))
    assert b'"balance":"0"' in out


def test_large_but_safe_decimals_round_trip() -> None:
    for balance in (Decimal("9" * 3000), Decimal("1" + "0" * 3000), Decimal("0." + "0" * 2000 + "7"),
                    Decimal("1E+3000"), Decimal("123456789012345678901234567890.123456789012345678901234567890")):
        fact = _fact(balance=balance)
        encoded = encode_cash_balance_risk_fact(fact)
        assert len(encoded) <= 4096
        decoded = decode_cash_balance_risk_fact(encoded)
        assert decoded == fact
        assert encode_cash_balance_risk_fact(decoded) == encoded


def test_ceiling_boundary_between_preformat_and_content_checks() -> None:
    # Passes the pre-format estimate but the full envelope exceeds the ceiling.
    borderline = _fact(balance=Decimal("9" * 3950))
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        encode_cash_balance_risk_fact(borderline)
    # Clearly below: succeeds.
    assert len(encode_cash_balance_risk_fact(_fact(balance=Decimal("9" * 3500)))) <= 4096


def test_existing_literal_bytes_unchanged() -> None:
    assert encode_cash_balance_risk_fact(_fact()) == _LITERAL
    assert decode_cash_balance_risk_fact(_LITERAL) == _fact()


def test_oversized_decoder_content_rejected_before_parsing(monkeypatch) -> None:
    content = b" " * 4097
    assert type(content) is bytes and len(content) == 4097
    monkeypatch.setattr(json, "loads", _forbid)
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        decode_cash_balance_risk_fact(content)
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        decode_cash_balance_risk_fact(_LITERAL + b" " * 4096)


def test_decoder_size_gate_precedes_utf8_validation(monkeypatch) -> None:
    monkeypatch.setattr(json, "loads", _forbid)
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        decode_cash_balance_risk_fact(b"\xff" * 5000)


def test_content_at_exactly_ceiling_reaches_parser() -> None:
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        decode_cash_balance_risk_fact(b"[" + b" " * 4094 + b"]")
    assert len(b"[" + b" " * 4094 + b"]") == 4096


def test_type_check_still_precedes_size_check() -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        decode_cash_balance_risk_fact(bytearray(b" " * 5000))  # type: ignore[arg-type]


def test_resource_ceiling_is_not_a_fact_rule() -> None:
    # The fact still represents any exact finite non-negative Decimal.
    fact = _fact(balance=Decimal("1E+999999999"))
    assert fact.balance == Decimal("1E+999999999")
    assert module_under_test._MAX_CANONICAL_CONTENT_BYTES == 4096
