"""Phase 28B-0: validators and temporal provenance. Missing or inferred publication time stays unknown; old economic dates never become retrieval dates."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.engine.learning._checks import (
    canonical_sha256,
    require_aware_datetime,
    require_canonical_token,
    require_sha256,
)
from backend.engine.learning.temporal_provenance import PublicationBasis, TemporalProvenance
from backend.tests.learning_support import HASH_A, plus, provenance_kwargs


class StrSub(str):
    pass


class DatetimeSub(datetime):
    pass


def test_naive_datetime_rejected() -> None:
    with pytest.raises(TypeError):
        require_aware_datetime("x", datetime(2026, 1, 1))


def test_datetime_subclass_rejected() -> None:
    with pytest.raises(TypeError):
        require_aware_datetime("x", DatetimeSub(2026, 1, 1, tzinfo=timezone.utc))


def test_date_is_not_a_datetime() -> None:
    with pytest.raises(TypeError):
        require_aware_datetime("x", date(2026, 1, 1))


@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "g" * 64, " " + "a" * 63, None, 5, StrSub("a" * 64)])
def test_malformed_sha256_rejected(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        require_sha256("h", bad)


@pytest.mark.parametrize("bad", ["", "TEFAS", "tefas price", "tefas..price", "-tefas", "tefas-", "1tefas", None, StrSub("tefas")])
def test_non_canonical_tokens_rejected(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        require_canonical_token("source_id", bad)


def test_canonical_token_accepts_dotted_lowercase() -> None:
    assert require_canonical_token("source_id", "tefas.fund_price-history") == "tefas.fund_price-history"


def test_valid_provenance_keeps_publication_unknown_by_default() -> None:
    p = TemporalProvenance(**provenance_kwargs())
    assert p.publication_basis is PublicationBasis.UNKNOWN
    assert p.publication_time is None and p.publication_evidence_sha256 is None


def test_old_economic_date_does_not_become_system_known_time() -> None:
    p = TemporalProvenance(**provenance_kwargs(economic_date=date(2021, 3, 4)))
    assert p.system_known_at_utc == plus(60)
    assert p.system_known_at_utc.date() != date(2021, 3, 4)


def test_turkish_local_midnight_economic_date_is_valid_when_utc_date_is_earlier() -> None:
    # 00:30 on 13 Oct in Turkey (UTC+3) is 21:30 UTC on 12 Oct: no UTC-calendar comparison with the economic date is made.
    p = TemporalProvenance(economic_date=date(2026, 10, 13), retrieved_at=datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc),
                           capture_attempted_at=datetime(2026, 10, 12, 21, 29, tzinfo=timezone.utc))
    assert p.economic_date == date(2026, 10, 13)


def test_future_effective_documentation_published_in_advance_is_valid() -> None:
    p = TemporalProvenance(**provenance_kwargs(economic_date=date(2027, 1, 1), publication_basis=PublicationBasis.SOURCE_DOCUMENT_STATED,
                                               publication_time=plus(10), publication_evidence_sha256=HASH_A))
    assert p.publication_time == plus(10) and p.economic_date == date(2027, 1, 1)


def test_historical_observation_retrieved_years_later_keeps_retrieval_as_only_knowledge_instant() -> None:
    p = TemporalProvenance(**provenance_kwargs(economic_date=date(2019, 5, 6)))
    assert p.system_known_at_utc == plus(60) and p.economic_date == date(2019, 5, 6)


def test_provenance_module_makes_no_economic_date_to_calendar_comparison() -> None:
    import inspect
    from backend.engine.learning import temporal_provenance
    assert ".date()" not in inspect.getsource(temporal_provenance.TemporalProvenance)


def test_capture_attempt_after_retrieval_rejected() -> None:
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(capture_attempted_at=plus(61)))


def test_publication_requires_stated_basis_and_evidence() -> None:
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(publication_time=plus(10)))
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(publication_basis=PublicationBasis.SOURCE_DOCUMENT_STATED))
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(publication_basis=PublicationBasis.SOURCE_DOCUMENT_STATED, publication_time=plus(10)))
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(publication_evidence_sha256=HASH_A))


def test_publication_after_retrieval_rejected() -> None:
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(publication_basis=PublicationBasis.SOURCE_DOCUMENT_STATED, publication_time=plus(120), publication_evidence_sha256=HASH_A))


def test_evidenced_publication_is_kept_separate_from_retrieval() -> None:
    p = TemporalProvenance(**provenance_kwargs(publication_basis=PublicationBasis.SOURCE_DOCUMENT_STATED, publication_time=plus(10), publication_evidence_sha256=HASH_A))
    assert p.publication_time == plus(10) and p.retrieved_at == plus(60)


def test_wrong_types_rejected() -> None:
    with pytest.raises(TypeError):
        TemporalProvenance(**provenance_kwargs(economic_date=datetime(2026, 10, 9, tzinfo=timezone.utc)))
    with pytest.raises(TypeError):
        TemporalProvenance(**provenance_kwargs(publication_basis="unknown"))
    with pytest.raises(TypeError):
        TemporalProvenance(**provenance_kwargs(data_revision=3))
    with pytest.raises(ValueError):
        TemporalProvenance(**provenance_kwargs(data_revision=" "))


def test_non_utc_offsets_compare_by_instant_and_serialize_in_utc() -> None:
    tr = timezone(timedelta(hours=3))
    p = TemporalProvenance(economic_date=date(2026, 10, 9), retrieved_at=plus(60).astimezone(tr), capture_attempted_at=plus(55).astimezone(tr))
    assert p.to_canonical_dict()["retrieved_at"] == "2026-10-12T09:01:00+00:00"


def test_provenance_is_frozen_and_deterministic() -> None:
    a, b = TemporalProvenance(**provenance_kwargs()), TemporalProvenance(**provenance_kwargs())
    assert a == b and a.canonical_sha256() == b.canonical_sha256()
    with pytest.raises(Exception):
        a.retrieved_at = plus(1)  # type: ignore[misc]


def test_canonical_hash_golden_value() -> None:
    assert canonical_sha256({"b": 1, "a": [1, None]}) == "28aaa2efb398aecfba7117c4101dc0b6f7a73ddf356ce57189d2fce19c134dad"  # compact sorted JSON, computed independently
