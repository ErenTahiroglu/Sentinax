"""
backend/tests/test_scheduler_calendar_evidence.py
=================================================
Phase 24B3A: the external calendar EVIDENCE boundary. Exchange session windows (not an open/closed bool) and planned source-release entries
for ONE explicit local date, with complete-vs-unavailable coverage, source provenance and SYSTEM-AS-OF PIT binding. Evidence only: no provider,
no weekend inference, no applicability decision, no occurrence filtering, no dispatch.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

from backend.engine.private import scheduler_calendar_evidence as module_under_test
from backend.engine.private.scheduler_calendar_evidence import (
    PrivateSchedulerCalendarCoverage,
    PrivateSchedulerCalendarDateEvidence,
    PrivateSchedulerCalendarKind,
    PrivateSchedulerCalendarPITBinding,
    PrivateSchedulerExchangeSessionWindow,
    PrivateSchedulerReleaseTimePrecision,
    PrivateSchedulerSourceReleasePlanEntry,
    bind_private_scheduler_calendar_pit,
    build_private_scheduler_calendar_date_evidence,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
CK, CV, PR = PrivateSchedulerCalendarKind, PrivateSchedulerCalendarCoverage, PrivateSchedulerReleaseTimePrecision
SHA = "a" * 64
DAY = date(2026, 10, 2)


def U(h, mi=0, s=0, us=0, day=2) -> datetime:
    return datetime(2026, 10, day, h, mi, s, us, tzinfo=UTC)


def window(open_h=9, close_h=16) -> PrivateSchedulerExchangeSessionWindow:
    return PrivateSchedulerExchangeSessionWindow(opens_at=U(open_h), closes_at=U(close_h))


def entry(key="rel-1", precision=PR.DATE_ONLY, planned=None) -> PrivateSchedulerSourceReleasePlanEntry:
    return PrivateSchedulerSourceReleasePlanEntry(release_key=key, time_precision=precision, planned_for=planned)


def kwargs(**changes):
    base = dict(calendar_key="exchange.test.core", calendar_revision=1, calendar_kind=CK.EXCHANGE_SESSION, timezone_key="UTC", local_date=DAY,
                coverage=CV.COMPLETE_FOR_DATE, exchange_sessions=(window(),), source_releases=(), source_key="source.test.calendar",
                published_at=U(1), observed_at=U(2), source_content_sha256=SHA)
    base.update(changes)
    return base


def release_kwargs(**changes):
    return kwargs(**{**dict(calendar_key="release.test.plan", calendar_kind=CK.SOURCE_RELEASE_PLAN, exchange_sessions=(),
                            source_releases=(entry(),)), **changes})


def exchange(**changes) -> PrivateSchedulerCalendarDateEvidence:
    return build_private_scheduler_calendar_date_evidence(**kwargs(**changes))


def releases(**changes) -> PrivateSchedulerCalendarDateEvidence:
    return build_private_scheduler_calendar_date_evidence(**release_kwargs(**changes))


# --- enums / stored shapes ---------------------------------------------------------------------------------------

def test_enums_and_stored_shapes() -> None:
    assert [(m.name, m.value) for m in CK] == [("EXCHANGE_SESSION", "exchange_session"), ("SOURCE_RELEASE_PLAN", "source_release_plan")]
    assert [(m.name, m.value) for m in CV] == [("COMPLETE_FOR_DATE", "complete_for_date"), ("UNAVAILABLE", "unavailable")]
    assert [(m.name, m.value) for m in PR] == [("DATE_ONLY", "date_only"), ("EXACT_TIME", "exact_time")]
    expected = {
        PrivateSchedulerExchangeSessionWindow: ["opens_at", "closes_at"],
        PrivateSchedulerSourceReleasePlanEntry: ["release_key", "time_precision", "planned_for"],
        PrivateSchedulerCalendarDateEvidence: ["calendar_key", "calendar_revision", "calendar_kind", "timezone_key", "local_date", "coverage",
                                               "exchange_sessions", "source_releases", "source_key", "published_at", "observed_at",
                                               "source_content_sha256"],
        PrivateSchedulerCalendarPITBinding: ["evidence", "knowledge_cutoff"],
    }
    for cls, names in expected.items():
        fields = dataclasses.fields(cls)
        assert [f.name for f in fields] == names
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert [len(dataclasses.fields(c)) for c in expected] == [2, 3, 12, 2]
    with pytest.raises(dataclasses.FrozenInstanceError):
        window().opens_at = U(1)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry().release_key = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        exchange().calendar_key = "x"  # type: ignore[misc]
    for function, count in ((build_private_scheduler_calendar_date_evidence, 12), (bind_private_scheduler_calendar_pit, 2)):
        parameters = list(inspect.signature(function).parameters.values())
        assert len(parameters) == count and all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    with pytest.raises(TypeError):
        build_private_scheduler_calendar_date_evidence(**kwargs(), is_open=True)  # type: ignore[call-arg]


def test_no_actual_availability_or_open_flag_surface() -> None:
    names = {f.name for cls in (PrivateSchedulerExchangeSessionWindow, PrivateSchedulerSourceReleasePlanEntry, PrivateSchedulerCalendarDateEvidence,
                                PrivateSchedulerCalendarPITBinding) for f in dataclasses.fields(cls)}
    assert not names & {"available_at", "actual_release_at", "ingested_data_at", "release_succeeded", "is_open", "is_half_day", "is_holiday", "open"}
    public = {n for cls in (PrivateSchedulerCalendarDateEvidence, PrivateSchedulerCalendarPITBinding) for n in dir(cls) if not n.startswith("_")}
    assert not [n for n in public if any(w in n for w in ("available", "succe", "holiday", "weekend", "half_day", "is_open"))]


# --- session window ----------------------------------------------------------------------------------------------

def test_session_window_is_canonical_utc_and_strictly_ordered() -> None:
    assert (window().opens_at, window().closes_at) == (U(9), U(16))
    plus3 = timezone(timedelta(hours=3))

    class SubDatetime(datetime):
        pass

    for opens, closes in ((U(9), U(9)), (U(16), U(9)), (U(9), U(9) - timedelta(microseconds=1))):
        with pytest.raises(ValueError):
            PrivateSchedulerExchangeSessionWindow(opens_at=opens, closes_at=closes)
    for bad in (datetime(2026, 10, 2, 9), U(9).astimezone(plus3), datetime(2026, 10, 2, 9, tzinfo=timezone(timedelta(0), "x"))):
        with pytest.raises(ValueError):                                                       # non-canonical representation: never converted
            PrivateSchedulerExchangeSessionWindow(opens_at=bad, closes_at=U(16))
        with pytest.raises(ValueError):
            PrivateSchedulerExchangeSessionWindow(opens_at=U(9), closes_at=bad)
    for bad in (SubDatetime(2026, 10, 2, 9, tzinfo=UTC), "2026-10-02T09:00:00Z", None, 1, DAY):
        with pytest.raises(TypeError):
            PrivateSchedulerExchangeSessionWindow(opens_at=bad, closes_at=U(16))
        with pytest.raises(TypeError):
            PrivateSchedulerExchangeSessionWindow(opens_at=U(9), closes_at=bad)
    assert PrivateSchedulerExchangeSessionWindow(opens_at=U(9, 0, 0, 1), closes_at=U(9, 0, 0, 2)).closes_at == U(9, 0, 0, 2)


# --- release entry -----------------------------------------------------------------------------------------------

def test_release_entry_contract() -> None:
    class SubStr(str):
        pass

    assert entry("AbC-1/x y").release_key == "AbC-1/x y"                                      # case and inner spaces preserved
    assert entry("x" * 128).release_key == "x" * 128
    for bad in ("", " ", " lead", "trail ", "x" * 129, "a\nb", "a\tb", "nul\x00", " x"):
        with pytest.raises(ValueError):
            entry(bad)
    for bad in (None, 1, b"x", SubStr("x")):
        with pytest.raises(TypeError):
            entry(bad)
    for bad in ("date_only", None, 1, CK.EXCHANGE_SESSION):
        with pytest.raises(TypeError):
            entry(precision=bad)


def test_date_only_release_never_carries_a_planned_instant() -> None:
    assert entry(precision=PR.DATE_ONLY, planned=None).planned_for is None
    with pytest.raises(ValueError):
        entry(precision=PR.DATE_ONLY, planned=U(9))                                           # no fabricated time


def test_exact_time_release_requires_a_canonical_utc_instant() -> None:
    assert entry(precision=PR.EXACT_TIME, planned=U(7)).planned_for == U(7)
    with pytest.raises(TypeError):
        entry(precision=PR.EXACT_TIME, planned=None)
    plus3 = timezone(timedelta(hours=3))
    for bad in (datetime(2026, 10, 2, 7), U(7).astimezone(plus3)):
        with pytest.raises(ValueError):
            entry(precision=PR.EXACT_TIME, planned=bad)
    for bad in ("2026-10-02T07:00:00Z", DAY, 1):
        with pytest.raises(TypeError):
            entry(precision=PR.EXACT_TIME, planned=bad)


# --- exchange evidence -------------------------------------------------------------------------------------------

def test_exchange_evidence_preserves_exact_session_windows_including_a_half_day() -> None:
    normal = exchange(exchange_sessions=(window(9, 16),))
    shortened = exchange(exchange_sessions=(window(9, 13),))                                   # generic early-close shape
    assert normal.exchange_sessions[0].closes_at == U(16) and shortened.exchange_sessions[0].closes_at == U(13)
    assert normal != shortened and shortened.coverage is CV.COMPLETE_FOR_DATE
    assert not hasattr(shortened, "is_half_day")                                              # the session itself is authority


def test_complete_closure_and_unavailable_are_distinct() -> None:
    closed = exchange(coverage=CV.COMPLETE_FOR_DATE, exchange_sessions=())
    unknown = exchange(coverage=CV.UNAVAILABLE, exchange_sessions=())
    assert closed.exchange_sessions == () == unknown.exchange_sessions
    assert closed != unknown and closed.coverage is CV.COMPLETE_FOR_DATE and unknown.coverage is CV.UNAVAILABLE
    with pytest.raises(ValueError):
        exchange(coverage=CV.UNAVAILABLE, exchange_sessions=(window(),))                      # unavailable carries no windows


def test_multiple_ordered_non_overlapping_sessions() -> None:
    morning, afternoon = window(9, 12), window(13, 16)
    assert exchange(exchange_sessions=(morning, afternoon)).exchange_sessions == (morning, afternoon)
    assert exchange(exchange_sessions=(window(9, 12), window(12, 16))).exchange_sessions[1].opens_at == U(12)       # adjacent allowed
    for bad in ((afternoon, morning), (window(9, 13), window(12, 16)), (window(9, 12), window(9, 12)), (window(9, 16), window(10, 12))):
        with pytest.raises(ValueError):                                                       # reversed / overlap / duplicate / nested: never sorted
            exchange(exchange_sessions=bad)


def test_session_boundaries_must_map_to_the_evidence_local_date() -> None:
    istanbul = dict(timezone_key="Europe/Istanbul", local_date=DAY)
    ok = PrivateSchedulerExchangeSessionWindow(opens_at=U(6), closes_at=U(15))                # 09:00-18:00 local (UTC+3)
    assert exchange(**istanbul, exchange_sessions=(ok,)).exchange_sessions == (ok,)
    previous_day_open = PrivateSchedulerExchangeSessionWindow(opens_at=U(20, day=1), closes_at=U(15))        # opens on 1 Oct local
    next_day_close = PrivateSchedulerExchangeSessionWindow(opens_at=U(6), closes_at=U(21))                   # closes 00:00 on 3 Oct local
    for bad in (previous_day_open, next_day_close):
        with pytest.raises(ValueError):
            exchange(**istanbul, exchange_sessions=(bad,))
    with pytest.raises(ValueError):                                                           # no overnight modelling
        exchange(exchange_sessions=(PrivateSchedulerExchangeSessionWindow(opens_at=U(22), closes_at=U(2, day=3)),))
    with pytest.raises(ValueError):
        exchange(local_date=date(2026, 10, 3))                                                # the UTC 09:00-16:00 session is on 2 Oct


# --- source-release evidence -------------------------------------------------------------------------------------

def test_release_plan_shapes_and_coverage() -> None:
    plan = releases(source_releases=(entry("a"), entry("b", PR.EXACT_TIME, U(7))))
    assert [e.release_key for e in plan.source_releases] == ["a", "b"]
    none_planned = releases(coverage=CV.COMPLETE_FOR_DATE, source_releases=())
    unavailable = releases(coverage=CV.UNAVAILABLE, source_releases=())
    assert none_planned.source_releases == () == unavailable.source_releases
    assert none_planned != unavailable and none_planned.coverage is CV.COMPLETE_FOR_DATE and unavailable.coverage is CV.UNAVAILABLE
    with pytest.raises(ValueError):
        releases(coverage=CV.UNAVAILABLE, source_releases=(entry(),))


def test_release_keys_are_unique_never_deduplicated() -> None:
    with pytest.raises(ValueError):
        releases(source_releases=(entry("dup"), entry("dup", PR.EXACT_TIME, U(7))))
    assert releases(source_releases=(entry("b"), entry("a"))).source_releases[0].release_key == "b"          # caller order kept, no priority


def test_exact_time_release_must_map_to_the_evidence_local_date() -> None:
    istanbul = dict(timezone_key="Europe/Istanbul")
    assert releases(**istanbul, source_releases=(entry("a", PR.EXACT_TIME, U(7)),)).source_releases[0].planned_for == U(7)
    for planned in (U(20, day=1), U(21)):                                                    # 23:00 on 1 Oct local; 00:00 on 3 Oct local
        with pytest.raises(ValueError):
            releases(**istanbul, source_releases=(entry("a", PR.EXACT_TIME, planned),))
    assert releases(**istanbul, source_releases=(entry("d", PR.DATE_ONLY, None),)).source_releases[0].planned_for is None


def test_shape_cross_contamination_is_rejected() -> None:
    with pytest.raises(ValueError):
        exchange(source_releases=(entry(),))
    with pytest.raises(ValueError):
        releases(exchange_sessions=(window(),))
    with pytest.raises(ValueError):
        exchange(coverage=CV.UNAVAILABLE, exchange_sessions=(window(),))
    with pytest.raises(ValueError):
        releases(coverage=CV.UNAVAILABLE, source_releases=(entry(),))
    for bad in ([window()], None, (entry(),), (object(),), "x"):
        with pytest.raises(TypeError):
            exchange(exchange_sessions=bad)
    for bad in ([entry()], None, (window(),), (object(),), "x"):
        with pytest.raises(TypeError):
            releases(source_releases=bad)


def test_no_weekend_inference() -> None:
    saturday = date(2026, 10, 3)
    assert saturday.weekday() == 5
    open_saturday = exchange(local_date=saturday, exchange_sessions=(PrivateSchedulerExchangeSessionWindow(opens_at=U(9, day=3), closes_at=U(16, day=3)),))
    assert len(open_saturday.exchange_sessions) == 1                                          # the snapshot is authority, not the weekday
    unknown_saturday = exchange(local_date=saturday, coverage=CV.UNAVAILABLE, exchange_sessions=())
    assert unknown_saturday.coverage is CV.UNAVAILABLE                                        # unknown, never "closed"
    sunday_open_release = releases(local_date=date(2026, 10, 4), source_releases=(entry("sun", PR.EXACT_TIME, U(7, day=4)),))
    assert sunday_open_release.source_releases[0].planned_for == U(7, day=4)


# --- identity / provenance ---------------------------------------------------------------------------------------

def test_calendar_and_source_identity() -> None:
    class SubStr(str):
        pass

    for field in ("calendar_key", "source_key"):
        for ok in ("x", "a" * 128, "exchange.bist.equity", "0-a_b.c"):
            assert getattr(exchange(**{field: ok}), field) == ok
        for bad in ("", " x", "x ", "X", "a" * 129, "a b", "x\n", "-x", "https://x.y"):
            with pytest.raises(ValueError):
                exchange(**{field: bad})
        for bad in (None, 1, b"x", SubStr("x")):
            with pytest.raises(TypeError):
                exchange(**{field: bad})
    for ok in (1, 2, 10 ** 9):
        assert exchange(calendar_revision=ok).calendar_revision == ok
    for bad in (0, -1):
        with pytest.raises(ValueError):
            exchange(calendar_revision=bad)
    for bad in (True, False, 1.0, "1", Decimal(1), None):
        with pytest.raises(TypeError):
            exchange(calendar_revision=bad)
    for bad in ("exchange_session", None, 1, CV.UNAVAILABLE):
        with pytest.raises(TypeError):
            exchange(calendar_kind=bad)
    for bad in ("complete_for_date", None, 1, CK.EXCHANGE_SESSION):
        with pytest.raises(TypeError):
            exchange(coverage=bad)


def test_timezone_and_local_date_are_exact() -> None:
    class SubStr(str):
        pass

    class SubDate(date):
        pass

    for ok in ("UTC", "Europe/Istanbul", "America/New_York"):
        assert exchange(timezone_key=ok, exchange_sessions=()).timezone_key == ok
    for bad in ("", " UTC", "UTC ", "Not/AZone", "+03:00", "../x", "Europe", "x" * 129, "UTC\n"):
        with pytest.raises(ValueError):
            exchange(timezone_key=bad)
    for bad in (None, 1, b"UTC", SubStr("UTC")):
        with pytest.raises(TypeError):
            exchange(timezone_key=bad)
    for bad in (datetime(2026, 10, 2), datetime(2026, 10, 2, tzinfo=UTC), SubDate(2026, 10, 2), "2026-10-02", None):
        with pytest.raises(TypeError):
            exchange(local_date=bad)


def test_hash_and_observation_times() -> None:
    class SubStr(str):
        pass

    class Broken(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("boom")

    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

    class SubDatetime(datetime):
        pass

    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 64 + "\n"):
        with pytest.raises(ValueError):
            exchange(source_content_sha256=bad)
    for bad in (None, 1, b"a" * 64, SubStr("a" * 64)):
        with pytest.raises(TypeError):
            exchange(source_content_sha256=bad)
    plus9 = timezone(timedelta(hours=9))
    assert exchange(published_at=None).published_at is None
    assert exchange(published_at=datetime(2026, 10, 2, 11, tzinfo=plus9), observed_at=U(2)).published_at.utcoffset() == timedelta(hours=9)  # representation kept
    with pytest.raises(ValueError):
        exchange(published_at=U(2, 0, 0, 1), observed_at=U(2))
    assert exchange(published_at=U(2), observed_at=U(2)).published_at == U(2)
    for field in ("observed_at", "published_at"):
        for bad in (datetime(2026, 10, 2, 2), datetime(2026, 10, 2, 2, tzinfo=Broken()), datetime(2026, 10, 2, 2, tzinfo=NoOffset()),
                    SubDatetime(2026, 10, 2, 2, tzinfo=UTC), "2026-10-02T02:00:00Z", DAY, 1):
            with pytest.raises(TypeError):
                exchange(**{field: bad})
    with pytest.raises(TypeError):
        exchange(observed_at=None)


def test_direct_construction_validates_the_whole_contract() -> None:
    good = exchange()
    fields = {f.name: getattr(good, f.name) for f in dataclasses.fields(good)}
    assert PrivateSchedulerCalendarDateEvidence(**fields) == good
    for change in (dict(calendar_revision=0), dict(coverage=CV.UNAVAILABLE), dict(source_releases=(entry(),)), dict(local_date=date(2026, 10, 3)),
                   dict(timezone_key="Not/AZone"), dict(source_content_sha256="A" * 64), dict(exchange_sessions=(window(12, 13), window(9, 10)))):
        with pytest.raises((ValueError, TypeError)):
            PrivateSchedulerCalendarDateEvidence(**{**fields, **change})
    assert exchange() == exchange()                                                           # deterministic


# --- PIT binding -------------------------------------------------------------------------------------------------

def test_pit_binding_is_system_as_of_with_exact_microsecond_boundaries() -> None:
    evidence = exchange(published_at=U(1), observed_at=U(2))
    assert bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=U(2)).evidence is evidence          # equality allowed
    with pytest.raises(ValueError):
        bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=U(2) - timedelta(microseconds=1))
    published_late = exchange(published_at=U(2, 0, 0, 1), observed_at=U(2, 0, 0, 1))
    with pytest.raises(ValueError):
        bind_private_scheduler_calendar_pit(evidence=published_late, knowledge_cutoff=U(2))
    assert bind_private_scheduler_calendar_pit(evidence=published_late, knowledge_cutoff=U(2, 0, 0, 1)).knowledge_cutoff == U(2, 0, 0, 1)
    assert bind_private_scheduler_calendar_pit(evidence=exchange(published_at=None), knowledge_cutoff=U(2)).evidence.published_at is None


def test_pit_binding_compares_utc_instants_across_offsets() -> None:
    plus9, minus5 = timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))
    evidence = exchange(published_at=datetime(2026, 10, 2, 10, tzinfo=plus9), observed_at=datetime(2026, 10, 1, 21, tzinfo=minus5))   # 01:00Z, 02:00Z
    exact = datetime(2026, 10, 2, 11, tzinfo=plus9)                                                                                  # 02:00Z
    assert bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=exact).evidence is evidence
    with pytest.raises(ValueError):
        bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=exact - timedelta(microseconds=1))


def test_source_availability_before_observation_does_not_authorize_the_scheduler() -> None:
    public_long_ago = exchange(published_at=U(1, day=1), observed_at=U(2))                    # public earlier, observed by Sentinax later
    with pytest.raises(ValueError):
        bind_private_scheduler_calendar_pit(evidence=public_long_ago, knowledge_cutoff=U(1))  # SYSTEM-AS-OF: Sentinax had not observed it yet
    assert not hasattr(PrivateSchedulerCalendarPITBinding, "mode")


def test_pit_binding_types_and_direct_construction() -> None:
    class SubEvidence(PrivateSchedulerCalendarDateEvidence):
        pass

    class SubDatetime(datetime):
        pass

    evidence = exchange()
    fields = {f.name: getattr(evidence, f.name) for f in dataclasses.fields(evidence)}
    for bad in (object(), None, fields, SubEvidence(**fields)):
        with pytest.raises(TypeError):
            bind_private_scheduler_calendar_pit(evidence=bad, knowledge_cutoff=U(3))  # type: ignore[arg-type]
    for bad in (datetime(2026, 10, 3), SubDatetime(2026, 10, 3, tzinfo=UTC), "2026-10-03", None, 1):
        with pytest.raises(TypeError):
            bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PrivateSchedulerCalendarPITBinding(evidence=evidence, knowledge_cutoff=U(1))
    with pytest.raises(TypeError):
        bind_private_scheduler_calendar_pit(evidence, U(3))  # type: ignore[misc]


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_calendar_evidence.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_the_standard_library_subset_only() -> None:
    names: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "zoneinfo"}
    assert not any(name.startswith("backend") for name in names)                              # no 24A / 24B1 / 24B2 / provider import


def test_no_clock_provider_decision_runtime_or_legacy_surface() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "sleep", "timestamp", "weekday", "isoweekday", "isocalendar",
                        "float", "random", "combine", "time"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert ints <= {1, 128}                                                                  # no hardcoded session hour, holiday or clock slot
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]


_FRAGMENTS = ("scheduler_interval", "legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio",
              "fastapi", "supabase", "redis", "database", "repository", "provider", "scrap", "browser", "weekend", "weekday", "holiday",
              "half_day", "dispatch", "execut", "pending", "running", "success", "succe", "failed", "retry", "backoff", "dlq", "queue", "worker",
              "available_at", "actual_release", "ingested", "recurrence", "occurrence", "decision", "applicab", "bist", "nyse", "tuik", "tcmb")
_EXACT = {"lease", "lock", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today", "run", "skip"}


def test_no_weekend_holiday_availability_decision_or_runtime_identifiers() -> None:
    identifiers: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for identifier in identifiers:
        lowered = identifier.lower()
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    members = {m.name for enum in (CK, CV, PR) for m in enum}
    assert not members & {"HOLIDAY", "MARKET_OPEN", "OPEN", "CLOSED", "RUN", "SKIP", "UNKNOWN"}


def test_documents_the_evidence_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("evidence only", "24B3B", "exchange session windows", "complete", "unavailable", "planned release", "not actual",
                   "SYSTEM-AS-OF", "observed_at", "no weekend inference", "no provider", "no dispatch", "DATE_ONLY", "EXACT_TIME"):
        assert needle in doc, needle
