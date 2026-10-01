"""
backend/tests/test_allocation_candidate_universe.py
===================================================
Phase 22B: point-in-time candidate-universe authority. Source-neutral membership snapshots with explicit effective and
knowledge-time provenance; no discovery, no ranking, no taxonomy, no look-ahead, no survivorship backcast.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_candidate_universe as module_under_test
from backend.engine.private.allocation_candidate_universe import (
    CandidateUniverseCoverage,
    CandidateUniverseQuery,
    CandidateUniverseResolution,
    CandidateUniverseResolutionStatus,
    CandidateUniverseSleeveBinding,
    CandidateUniverseSnapshot,
    bind_candidate_universes_to_sleeves,
    resolve_candidate_universe,
)
from backend.engine.private.allocation_rebalance import RebalanceCurrentState
from backend.engine.private.allocation_universe_composition import (
    CrossAssetSleeve,
    CrossUniverseAuthority,
    build_cross_asset_composition_plan,
)
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, AssetClass, Currency
from backend.tests.invariants import static_guards as sg

D = Decimal
UA, UB, UC, UD, UE = (UUID(int=i) for i in (1, 2, 3, 4, 5))
EQ, FI = AssetClass.EQUITY, AssetClass.FIXED_INCOME
COMPLETE, CURATED = CandidateUniverseCoverage.COMPLETE_MEMBERSHIP, CandidateUniverseCoverage.CURATED_CANDIDATES
S = CandidateUniverseResolutionStatus
SRC, UNI = "msci-like", "world-large-cap"
SHA = "a" * 64
UTC = timezone.utc


def T(y, m, d, h=0, mi=0, s=0, us=0, tz=UTC) -> datetime:
    return datetime(y, m, d, h, mi, s, us, tzinfo=tz)


def snap(ids, effective_from=date(2026, 6, 1), effective_to=None, published=None, observed=T(2026, 5, 21), *, sha=SHA,
         coverage=COMPLETE, asset_class=EQ, source=SRC, universe=UNI) -> CandidateUniverseSnapshot:
    return CandidateUniverseSnapshot(
        source_key=source, universe_key=universe, content_sha256=sha, asset_class=asset_class, coverage=coverage,
        effective_from=effective_from, effective_to=effective_to, published_at=published, observed_at=observed,
        instrument_ids=tuple(ids),
    )


def query(evaluation_date, cutoff, mode=AsOfMode.SOURCE_AS_OF, asset_class=EQ, source=SRC, universe=UNI, context=None):
    return CandidateUniverseQuery(source_key=source, universe_key=universe, asset_class=asset_class, evaluation_date=evaluation_date,
                                  pit_context=context or AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff))


def resolve(snapshots, evaluation_date, cutoff, mode=AsOfMode.SOURCE_AS_OF, **kw) -> CandidateUniverseResolution:
    return resolve_candidate_universe(query=query(evaluation_date, cutoff, mode, **kw), snapshots=tuple(snapshots))


OLD = snap([UA, UB], date(2026, 6, 1), published=T(2026, 5, 20), observed=T(2026, 5, 21), sha="1" * 64)
NEW = snap([UA, UC], date(2026, 9, 1), published=T(2026, 8, 12, 9), observed=T(2026, 8, 12, 12), sha="2" * 64)


# --- enums / stored fields ---------------------------------------------------------------------------------------

def test_enums_and_stored_fields_are_exactly_the_specified_ones() -> None:
    assert [m.value for m in CandidateUniverseCoverage] == ["complete_membership", "curated_candidates"]
    assert [m.value for m in CandidateUniverseResolutionStatus] == ["selected", "no_source_snapshot", "no_effective_snapshot",
                                                                    "no_snapshot_as_of", "frontier_conflict"]
    assert [f.name for f in dataclasses.fields(CandidateUniverseSnapshot)] == [
        "source_key", "universe_key", "content_sha256", "asset_class", "coverage", "effective_from", "effective_to", "published_at",
        "observed_at", "instrument_ids"]
    assert [f.name for f in dataclasses.fields(CandidateUniverseQuery)] == ["source_key", "universe_key", "asset_class", "evaluation_date", "pit_context"]
    assert [f.name for f in dataclasses.fields(CandidateUniverseResolution)] == ["query", "snapshots", "status", "selected_snapshot"]
    assert [f.name for f in dataclasses.fields(CandidateUniverseSleeveBinding)] == ["sleeves", "universes"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        OLD.source_key = "x"  # type: ignore[misc]


# --- snapshot validation -----------------------------------------------------------------------------------------

def _raw(**changes):
    values = dict(source_key=SRC, universe_key=UNI, content_sha256=SHA, asset_class=EQ, coverage=COMPLETE, effective_from=date(2026, 6, 1),
                  effective_to=None, published_at=None, observed_at=T(2026, 5, 21), instrument_ids=(UA,))
    values.update(changes)
    return CandidateUniverseSnapshot(**values)


def test_snapshot_rejects_bad_types_and_values() -> None:
    type_errors = dict(source_key=[b"x", 1, None], universe_key=[1, None, b"x"], content_sha256=[1, None, b"a" * 64],
                       asset_class=["equity", None, 1], coverage=["complete_membership", None, 1],
                       effective_from=[datetime(2026, 6, 1, tzinfo=UTC), "2026-06-01", None],
                       effective_to=["2026-07-01", datetime(2026, 7, 1, tzinfo=UTC), 1],
                       published_at=["2026-05-20", date(2026, 5, 20), 1], observed_at=[None, "x", date(2026, 5, 21), datetime(2026, 5, 21)],
                       instrument_ids=[[UA], None, (UA, "x"), "x"])
    for field, bads in type_errors.items():
        for bad in bads:
            with pytest.raises(TypeError):
                _raw(**{field: bad})
    for bad in (datetime(2026, 5, 21), datetime(2026, 5, 21, tzinfo=None)):
        with pytest.raises(TypeError):
            _raw(published_at=bad)
    value_errors = dict(
        source_key=["", " msci", "MSCI", "msci ", "-x", "a" * 65, "https://x.y", "a b", "a/b", "msci\n"],
        universe_key=["", "Upper", " x", "x ", "a" * 129, "http://u", "x\n", "_x"],
        content_sha256=["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 63 + "\n"],
    )
    for field, bads in value_errors.items():
        for bad in bads:
            with pytest.raises(ValueError):
                _raw(**{field: bad})
    assert _raw(source_key="a" * 64, universe_key="b" * 128).source_key == "a" * 64


def test_snapshot_temporal_contract() -> None:
    with pytest.raises(ValueError):
        _raw(effective_to=date(2026, 6, 1))                          # strictly greater than effective_from
    with pytest.raises(ValueError):
        _raw(effective_to=date(2026, 5, 31))
    assert _raw(effective_to=date(2026, 6, 2)).effective_to == date(2026, 6, 2)
    with pytest.raises(ValueError):
        _raw(published_at=T(2026, 5, 21, 0, 0, 0, 1))                # published_at <= observed_at
    assert _raw(published_at=T(2026, 5, 21)).published_at == T(2026, 5, 21)
    plus9 = timezone(timedelta(hours=9))
    assert _raw(published_at=T(2026, 5, 21, 9, tz=plus9), observed_at=T(2026, 5, 21)).published_at is not None   # equal UTC instants
    with pytest.raises(ValueError):
        _raw(published_at=T(2026, 5, 21, 10, tz=plus9), observed_at=T(2026, 5, 21))      # 01:00Z > 00:00Z


def test_snapshot_instrument_ids_contract_and_empty_is_valid() -> None:
    assert _raw(instrument_ids=()).instrument_ids == ()
    with pytest.raises(ValueError):
        _raw(instrument_ids=(UB, UA))
    with pytest.raises(ValueError):
        _raw(instrument_ids=(UA, UA))


def test_hostile_tzinfo_fails_closed() -> None:
    class Broken(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("boom")

    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

    for tz in (Broken(), NoOffset()):
        with pytest.raises(TypeError):
            _raw(observed_at=datetime(2026, 5, 21, tzinfo=tz))


# --- query validation --------------------------------------------------------------------------------------------

def test_query_contract() -> None:
    ctx = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(2026, 8, 20))
    good = dict(source_key=SRC, universe_key=UNI, asset_class=EQ, evaluation_date=date(2026, 9, 1), pit_context=ctx)
    assert CandidateUniverseQuery(**good).pit_context is ctx

    class Sub(AnalysisPITContext):
        pass

    for field, bad in (("source_key", 1), ("universe_key", None), ("asset_class", "equity"), ("evaluation_date", datetime(2026, 9, 1, tzinfo=UTC)),
                       ("evaluation_date", "2026-09-01"), ("pit_context", object()), ("pit_context", None),
                       ("pit_context", Sub(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(2026, 8, 20)))):
        with pytest.raises(TypeError):
            CandidateUniverseQuery(**{**good, field: bad})
    for field, bad in (("source_key", "MSCI"), ("universe_key", "a b")):
        with pytest.raises(ValueError):
            CandidateUniverseQuery(**{**good, field: bad})
    with pytest.raises(TypeError):
        CandidateUniverseQuery(source_key=SRC, universe_key=UNI, asset_class=EQ, pit_context=ctx)  # type: ignore[call-arg]  # no default date


def test_resolver_is_keyword_only_and_strictly_typed() -> None:
    q = query(date(2026, 9, 1), T(2026, 8, 20))
    with pytest.raises(TypeError):
        resolve_candidate_universe(q, ())  # type: ignore[misc]
    with pytest.raises(TypeError):
        resolve_candidate_universe(query=q)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        resolve_candidate_universe(query=object(), snapshots=())  # type: ignore[arg-type]
    for bad in ([OLD], (OLD, "x"), None):
        with pytest.raises(TypeError):
            resolve_candidate_universe(query=q, snapshots=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        resolve_candidate_universe(query=q, snapshots=(OLD, OLD))       # physical duplicates are invalid input


# --- resolution statuses -----------------------------------------------------------------------------------------

def test_empty_input_and_foreign_definitions_do_not_influence_resolution() -> None:
    assert resolve([], date(2026, 9, 1), T(2026, 9, 2)).status is S.NO_SOURCE_SNAPSHOT
    foreign = [snap([UA], source="other-source"), snap([UA], universe="other-universe"), snap([UA], asset_class=FI)]
    result = resolve(foreign, date(2026, 9, 1), T(2026, 9, 2))
    assert result.status is S.NO_SOURCE_SNAPSHOT and result.selected_snapshot is None
    with_foreign = resolve([*foreign, OLD], date(2026, 9, 1), T(2026, 9, 2))
    assert with_foreign.status is S.SELECTED and with_foreign.selected_snapshot == OLD


def test_effective_interval_is_half_open() -> None:
    bounded = snap([UA], date(2026, 6, 1), effective_to=date(2026, 7, 1), observed=T(2026, 5, 21))
    cutoff = T(2026, 12, 1)
    assert resolve([bounded], date(2026, 5, 31), cutoff).status is S.NO_EFFECTIVE_SNAPSHOT
    assert resolve([bounded], date(2026, 6, 1), cutoff).status is S.SELECTED
    assert resolve([bounded], date(2026, 6, 30), cutoff).status is S.SELECTED
    assert resolve([bounded], date(2026, 7, 1), cutoff).status is S.NO_EFFECTIVE_SNAPSHOT
    assert resolve([OLD], date(2099, 1, 1), cutoff).status is S.SELECTED        # open-ended until superseded


def test_announcement_versus_effective_date() -> None:
    both = [OLD, NEW]
    a = resolve(both, date(2026, 8, 31), T(2026, 8, 20))               # announced, not yet effective
    assert a.status is S.SELECTED and a.selected_snapshot == OLD and a.instrument_ids == (UA, UB)
    b = resolve(both, date(2026, 9, 1), T(2026, 8, 1))                 # effective, but not yet announced at the cutoff
    assert b.status is S.SELECTED and b.selected_snapshot == OLD
    c = resolve(both, date(2026, 9, 1), T(2026, 8, 20))                # effective and announced
    assert c.status is S.SELECTED and c.selected_snapshot == NEW and c.instrument_ids == (UA, UC)
    assert resolve(both, date(2026, 9, 1), T(2026, 8, 20), AsOfMode.SYSTEM_AS_OF).selected_snapshot == NEW


def test_source_as_of_versus_system_as_of() -> None:
    cutoff = T(2026, 8, 12, 10)                                        # published 09:00Z <= cutoff < observed 12:00Z
    source = resolve([OLD, NEW], date(2026, 9, 1), cutoff, AsOfMode.SOURCE_AS_OF)
    system = resolve([OLD, NEW], date(2026, 9, 1), cutoff, AsOfMode.SYSTEM_AS_OF)
    assert source.selected_snapshot == NEW                              # the market could know it
    assert system.selected_snapshot == OLD                              # Sentinax had not observed it yet
    only_new = resolve([NEW], date(2026, 9, 1), cutoff, AsOfMode.SYSTEM_AS_OF)
    assert only_new.status is S.NO_SNAPSHOT_AS_OF and only_new.selected_snapshot is None
    assert resolve([NEW], date(2026, 9, 1), cutoff, AsOfMode.SOURCE_AS_OF).status is S.SELECTED


def test_system_as_of_also_requires_published_at_before_the_cutoff() -> None:
    odd = snap([UA], published=T(2026, 6, 1, 8), observed=T(2026, 6, 1, 9))
    assert resolve([odd], date(2026, 7, 1), T(2026, 6, 1, 9), AsOfMode.SYSTEM_AS_OF).status is S.SELECTED
    assert resolve([odd], date(2026, 7, 1), T(2026, 6, 1, 8, 59), AsOfMode.SYSTEM_AS_OF).status is S.NO_SNAPSHOT_AS_OF


def test_published_at_fallback_to_observed_at_has_no_tolerance() -> None:
    cutoff = T(2026, 8, 20, 12)
    exact = snap([UA], published=None, observed=cutoff)
    later = snap([UA], published=None, observed=cutoff + timedelta(microseconds=1))
    assert resolve([exact], date(2026, 9, 1), cutoff).status is S.SELECTED
    assert resolve([later], date(2026, 9, 1), cutoff).status is S.NO_SNAPSHOT_AS_OF
    published = snap([UA], published=cutoff + timedelta(microseconds=1), observed=cutoff + timedelta(microseconds=1))
    assert resolve([published], date(2026, 9, 1), cutoff).status is S.NO_SNAPSHOT_AS_OF


def test_utc_instant_comparison_across_offsets() -> None:
    plus9, minus5 = timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))
    cutoff = T(2026, 8, 20, 12, tz=minus5)                              # 17:00Z
    on_time = snap([UA], published=T(2026, 8, 21, 2, tz=plus9), observed=T(2026, 8, 21, 2, tz=plus9))   # 17:00Z exactly
    late = snap([UA], published=T(2026, 8, 21, 2, 0, 0, 1, tz=plus9), observed=T(2026, 8, 21, 2, 0, 0, 1, tz=plus9))
    assert resolve([on_time], date(2026, 9, 1), cutoff).status is S.SELECTED
    assert resolve([late], date(2026, 9, 1), cutoff).status is S.NO_SNAPSHOT_AS_OF


def test_current_list_cannot_be_backcast() -> None:
    today = snap([UA, UB], date(2026, 10, 1), published=T(2026, 9, 1), observed=T(2026, 9, 2))
    result = resolve([today], date(2025, 10, 1), T(2027, 1, 1))
    assert result.status is S.NO_EFFECTIVE_SNAPSHOT and result.selected_snapshot is None and result.instrument_ids is None


def test_no_snapshot_as_of_never_falls_back_to_future_evidence() -> None:
    result = resolve([OLD], date(2026, 7, 1), T(2026, 5, 1))
    assert result.status is S.NO_SNAPSHOT_AS_OF and result.selected_snapshot is None and result.instrument_ids is None


def test_future_conflict_cannot_poison_the_past() -> None:
    known = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64)
    future_conflict = snap([UA, UC], published=T(2026, 8, 1), observed=T(2026, 8, 2), sha="3" * 64)
    same_time_future = snap([UB, UC], published=T(2026, 8, 1), observed=T(2026, 8, 2), sha="4" * 64)
    result = resolve([known, future_conflict, same_time_future], date(2026, 7, 1), T(2026, 7, 1))
    assert result.status is S.SELECTED and result.selected_snapshot == known                # not FRONTIER_CONFLICT
    after = resolve([known, future_conflict, same_time_future], date(2026, 7, 1), T(2026, 9, 1))
    assert after.status is S.FRONTIER_CONFLICT and after.selected_snapshot is None


def test_true_frontier_conflict_has_no_tie_break() -> None:
    left = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64)
    right = snap([UA, UC], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="2" * 64)
    for combo in ((left, right), (right, left)):
        result = resolve(combo, date(2026, 7, 1), T(2026, 7, 1))
        assert result.status is S.FRONTIER_CONFLICT and result.selected_snapshot is None and result.instrument_ids is None
    same_hash = snap([UA, UC], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64)
    assert resolve((left, same_hash), date(2026, 7, 1), T(2026, 7, 1)).status is S.FRONTIER_CONFLICT       # same hash, other members
    other_coverage = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64, coverage=CURATED)
    assert resolve((left, other_coverage), date(2026, 7, 1), T(2026, 7, 1)).status is S.FRONTIER_CONFLICT
    other_interval = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64, effective_to=date(2027, 1, 1))
    assert resolve((left, other_interval), date(2026, 7, 1), T(2026, 7, 1)).status is S.FRONTIER_CONFLICT
    other_hash = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="2" * 64)
    assert resolve((left, other_hash), date(2026, 7, 1), T(2026, 7, 1)).status is S.FRONTIER_CONFLICT


def test_logical_duplicates_choose_the_earliest_observation_without_conflict() -> None:
    first = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 2))
    again = snap([UA, UB], published=T(2026, 6, 1), observed=T(2026, 6, 5))
    for combo in ((first, again), (again, first)):
        result = resolve(combo, date(2026, 7, 1), T(2026, 7, 1))
        assert result.status is S.SELECTED and result.selected_snapshot == first
    assert resolve((first, again), date(2026, 7, 1), T(2026, 6, 3), AsOfMode.SYSTEM_AS_OF).selected_snapshot == first  # again not yet observed


def test_correction_before_cutoff_supersedes_without_union_or_resurrection() -> None:
    original = snap([UA, UB, UC], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64)
    correction = snap([UA, UC], published=T(2026, 7, 1), observed=T(2026, 7, 2), sha="2" * 64)
    result = resolve([original, correction], date(2026, 8, 1), T(2026, 8, 1))
    assert result.selected_snapshot == correction and result.instrument_ids == (UA, UC)            # B is not resurrected
    before = resolve([original, correction], date(2026, 8, 1), T(2026, 6, 15))
    assert before.selected_snapshot == original and before.instrument_ids == (UA, UB, UC)
    assert resolve([original, correction], date(2026, 8, 1), T(2026, 7, 1, 0, 0, 0, 0)).selected_snapshot == correction  # known at the cutoff


def test_later_effective_regime_wins_only_from_its_effective_date() -> None:
    result = resolve([OLD, NEW], date(2026, 10, 1), T(2026, 12, 1))
    assert result.selected_snapshot == NEW
    older_knowledge_newer_regime = snap([UA, UD], date(2026, 9, 1), published=T(2026, 1, 1), observed=T(2026, 1, 2), sha="5" * 64)
    assert resolve([OLD, older_knowledge_newer_regime], date(2026, 9, 1), T(2026, 12, 1)).selected_snapshot == older_knowledge_newer_regime
    assert resolve([OLD, older_knowledge_newer_regime], date(2026, 8, 31), T(2026, 12, 1)).selected_snapshot == OLD


def test_empty_selected_universe_is_not_unavailable() -> None:
    empty = snap([], published=T(2026, 6, 1), observed=T(2026, 6, 2))
    result = resolve([empty], date(2026, 7, 1), T(2026, 7, 1))
    assert result.status is S.SELECTED and result.instrument_ids == () and result.selected_snapshot == empty
    unavailable = resolve([], date(2026, 7, 1), T(2026, 7, 1))
    assert unavailable.instrument_ids is None and unavailable.coverage is None and unavailable.effective_from is None


def test_selected_properties_come_directly_from_the_snapshot() -> None:
    result = resolve([NEW], date(2026, 9, 1), T(2026, 9, 2))
    assert (result.source_key, result.universe_key, result.content_sha256, result.coverage) == (SRC, UNI, "2" * 64, COMPLETE)
    assert (result.effective_from, result.effective_to, result.published_at, result.observed_at) == (date(2026, 9, 1), None, NEW.published_at, NEW.observed_at)


def test_curated_and_complete_coverage_are_carried_without_ineligibility_claims() -> None:
    curated = snap([UA, UB], coverage=CURATED, published=T(2026, 6, 1), observed=T(2026, 6, 2))
    result = resolve([curated], date(2026, 7, 1), T(2026, 7, 1))
    assert result.coverage is CURATED and UC not in result.instrument_ids
    public = {name for name in dir(CandidateUniverseResolution) + dir(CandidateUniverseSnapshot) + dir(CandidateUniverseSleeveBinding)
              if not name.startswith("_")}
    assert not [n for n in public if "inelig" in n or "excluded" in n or "reject" in n or "forbid" in n]
    assert resolve([snap([UA], coverage=COMPLETE, published=T(2026, 6, 1), observed=T(2026, 6, 2))], date(2026, 7, 1), T(2026, 7, 1)).coverage is COMPLETE


def test_input_permutation_invariance() -> None:
    a = snap([UA, UB, UC], published=T(2026, 6, 1), observed=T(2026, 6, 2), sha="1" * 64)
    b = snap([UA, UC], published=T(2026, 7, 1), observed=T(2026, 7, 2), sha="2" * 64)
    c = snap([UA, UB], published=T(2026, 7, 1), observed=T(2026, 7, 3), sha="3" * 64)       # conflicts with b once both are known
    d = snap([UA], published=T(2026, 7, 1), observed=T(2026, 7, 2), sha="4" * 64, asset_class=FI)
    for evaluation, cutoff in ((date(2026, 8, 1), T(2026, 6, 15)), (date(2026, 8, 1), T(2026, 7, 2, 12)), (date(2026, 8, 1), T(2026, 8, 1))):
        results = {(r.status, r.selected_snapshot) for r in (resolve(p, evaluation, cutoff) for p in itertools.permutations([a, b, c, d]))}
        assert len(results) == 1


# --- resolution forge resistance ---------------------------------------------------------------------------------

def test_resolution_constructor_rejects_forgeries() -> None:
    q_c = query(date(2026, 9, 1), T(2026, 8, 20))
    good = resolve_candidate_universe(query=q_c, snapshots=(OLD, NEW))
    assert good.query is q_c and good.snapshots == (OLD, NEW)
    assert CandidateUniverseResolution(query=q_c, snapshots=(OLD, NEW), status=S.SELECTED, selected_snapshot=NEW) == good
    forgeries = [
        dict(status=S.SELECTED, selected_snapshot=OLD),                                    # older snapshot when a newer one wins
        dict(status=S.SELECTED, selected_snapshot=None),
        dict(status=S.NO_SNAPSHOT_AS_OF, selected_snapshot=NEW),
        dict(status=S.NO_SNAPSHOT_AS_OF, selected_snapshot=None),
        dict(status=S.FRONTIER_CONFLICT, selected_snapshot=None),
        dict(status=S.NO_EFFECTIVE_SNAPSHOT, selected_snapshot=None),
        dict(status=S.SELECTED, selected_snapshot=snap([UA, UC], date(2026, 9, 1), published=T(2026, 8, 12, 9), observed=T(2026, 8, 12, 12), sha="9" * 64)),
    ]
    for kwargs in forgeries:
        with pytest.raises(ValueError):
            CandidateUniverseResolution(query=q_c, snapshots=(OLD, NEW), **kwargs)
    early = query(date(2026, 9, 1), T(2026, 8, 1))                                       # NEW is a future snapshot here
    with pytest.raises(ValueError):
        CandidateUniverseResolution(query=early, snapshots=(OLD, NEW), status=S.SELECTED, selected_snapshot=NEW)
    with pytest.raises(ValueError):                                                       # altered collection
        CandidateUniverseResolution(query=q_c, snapshots=(OLD,), status=S.SELECTED, selected_snapshot=NEW)
    with pytest.raises(TypeError):
        CandidateUniverseResolution(query=q_c, snapshots=[OLD, NEW], status=S.SELECTED, selected_snapshot=NEW)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CandidateUniverseResolution(query=object(), snapshots=(OLD, NEW), status=S.SELECTED, selected_snapshot=NEW)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CandidateUniverseResolution(query=q_c, snapshots=(OLD, NEW), status="selected", selected_snapshot=NEW)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CandidateUniverseResolution(query=q_c, snapshots=(OLD, NEW), status=S.SELECTED, selected_snapshot=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CandidateUniverseResolution(query=q_c, snapshots=(OLD, NEW), status=S.SELECTED, selected_snapshot="x")  # type: ignore[arg-type]


# --- sleeve binding ----------------------------------------------------------------------------------------------

def _sleeve(asset_class, weight, ids, weights) -> CrossAssetSleeve:
    return CrossAssetSleeve(asset_class=asset_class, target_weight=D(weight), instrument_ids=tuple(ids), instrument_weights=tuple(D(w) for w in weights))


CTX = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(2026, 12, 1))
EQ_SNAP = snap([UA, UB, UC], published=T(2026, 6, 1), observed=T(2026, 6, 2))
FI_SNAP = snap([UD, UE], asset_class=FI, universe="bond-universe", published=T(2026, 6, 1), observed=T(2026, 6, 2))


def _universe(snapshot, evaluation=date(2026, 9, 1), context=CTX):
    return resolve_candidate_universe(
        query=CandidateUniverseQuery(source_key=snapshot.source_key, universe_key=snapshot.universe_key, asset_class=snapshot.asset_class,
                                     evaluation_date=evaluation, pit_context=context),
        snapshots=(snapshot,))


def _sleeves():
    return (_sleeve(EQ, "0.60", [UA, UB], ["0.70", "0.30"]), _sleeve(FI, "0.40", [UD, UE], ["0.25", "0.75"]))


def test_binding_accepts_candidates_from_selected_universes_and_does_not_change_sleeves() -> None:
    sleeves = _sleeves()
    universes = (_universe(EQ_SNAP), _universe(FI_SNAP))
    binding = bind_candidate_universes_to_sleeves(sleeves=sleeves, universes=universes)
    assert binding.sleeves is sleeves and binding.universes is universes
    assert sleeves == _sleeves()                                                          # weights, candidates, ordering untouched
    assert set(EQ_SNAP.instrument_ids) > set(sleeves[0].instrument_ids)                   # the universe is larger; C is simply not weighted
    assert CandidateUniverseSleeveBinding(sleeves=sleeves, universes=universes) == binding


def test_binding_rejects_every_invalid_combination() -> None:
    sleeves = _sleeves()
    eq_u, fi_u = _universe(EQ_SNAP), _universe(FI_SNAP)

    def bad(s=sleeves, u=(eq_u, fi_u), error=ValueError):
        with pytest.raises(error):
            bind_candidate_universes_to_sleeves(sleeves=s, universes=u)

    bad(u=(eq_u,))                                                                        # missing universe
    bad(u=(eq_u, fi_u, fi_u))                                                             # extra / duplicate universe
    bad(u=(fi_u, eq_u))                                                                   # reordered universes
    bad(u=(eq_u, eq_u))                                                                   # wrong AssetClass
    bad(s=(_sleeve(EQ, "0.60", [UA, UD], ["0.70", "0.30"]), sleeves[1]))                  # candidate absent from the selected universe
    bad(u=(eq_u, _universe(snap([UD], asset_class=FI, universe="bond-universe", published=T(2026, 6, 1), observed=T(2026, 6, 2)))))  # E absent
    bad(u=(eq_u, resolve([], date(2026, 9, 1), T(2026, 12, 1), asset_class=FI, universe="bond-universe")))                          # unavailable
    bad(u=(eq_u, _universe(FI_SNAP, evaluation=date(2026, 9, 2))))                        # another evaluation date
    bad(u=(eq_u, _universe(FI_SNAP, context=AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(2026, 12, 1)))))   # equal but not the same context
    bad(u=(eq_u, _universe(FI_SNAP, context=AnalysisPITContext(mode=AsOfMode.SYSTEM_AS_OF, knowledge_cutoff=T(2026, 12, 1)))))
    bad(s=list(sleeves), error=TypeError)
    bad(s=(), error=TypeError)
    bad(s=(sleeves[0], object()), error=TypeError)
    bad(u=[eq_u, fi_u], error=TypeError)
    bad(u=(eq_u, object()), error=TypeError)
    bad(s=(sleeves[1], sleeves[0]), u=(fi_u, eq_u))                                       # noncanonical sleeve sequence
    with pytest.raises(TypeError):
        bind_candidate_universes_to_sleeves(sleeves, (eq_u, fi_u))  # type: ignore[misc]
    with pytest.raises(TypeError):
        bind_candidate_universes_to_sleeves(sleeves=sleeves)  # type: ignore[call-arg]


def test_empty_selected_universe_cannot_bind_a_non_empty_sleeve() -> None:
    empty = _universe(snap([], published=T(2026, 6, 1), observed=T(2026, 6, 2)))
    assert empty.status is S.SELECTED
    with pytest.raises(ValueError):
        bind_candidate_universes_to_sleeves(sleeves=(_sleeve(EQ, "1", [UA], ["1"]),), universes=(empty,))


def test_binding_constructor_recomputes_its_invariants() -> None:
    sleeves = _sleeves()
    unavailable = resolve([], date(2026, 9, 1), T(2026, 12, 1), asset_class=FI, universe="bond-universe")
    with pytest.raises(ValueError):
        CandidateUniverseSleeveBinding(sleeves=sleeves, universes=(_universe(EQ_SNAP), unavailable))
    with pytest.raises(ValueError):
        CandidateUniverseSleeveBinding(sleeves=sleeves, universes=(_universe(FI_SNAP), _universe(EQ_SNAP)))


def test_multi_sleeve_common_decision_frontier_is_required() -> None:
    other = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(2026, 7, 1))   # bond universe known at another time
    with pytest.raises(ValueError):
        bind_candidate_universes_to_sleeves(sleeves=_sleeves(), universes=(_universe(EQ_SNAP), _universe(FI_SNAP, context=other)))


def test_binding_integrates_with_phase_22a_without_changing_the_composition() -> None:
    sleeves = _sleeves()
    binding = bind_candidate_universes_to_sleeves(sleeves=sleeves, universes=(_universe(EQ_SNAP), _universe(FI_SNAP)))
    state = RebalanceCurrentState(instrument_ids=(UA, UD), current_values=(D("30"), D("20")), investable_cash=D("50"), currency=Currency.TRY)
    authority = CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids=(UB, UE), authorized_exit_instrument_ids=())
    plan = build_cross_asset_composition_plan(current_state=state, sleeves=binding.sleeves, authority=authority)
    assert plan.sleeves is sleeves
    assert plan.rebalance_target.weights == (D("0.42"), D("0.18"), D("0.10"), D("0.30"))
    assert plan.reconciled_instrument_ids == (UA, UB, UD, UE)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_candidate_universe.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_pit_context_domain_enums_and_the_22a_sleeve_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "uuid", "backend.engine.private.analysis_pit",
                     "backend.engine.private.domain", "backend.engine.private.allocation_universe_composition"}
    assert imported["backend.engine.private.analysis_pit"] == {"AnalysisPITContext"}
    assert imported["backend.engine.private.domain"] == {"AsOfMode", "AssetClass"}
    assert imported["backend.engine.private.allocation_universe_composition"] == {"CrossAssetSleeve"}
    for forbidden in ("identity", "fund_category", "market_data", "provider", "portfolio", "fee_tax", "allocation_rebalance", "allocation_cvar",
                      "allocation_user_view", "allocation_hrp", "allocation_risk", "allocation_matrix", "allocation_benchmarks",
                      "repository", "supabase", "numpy", "scipy", "pandas", "math", "random", "decimal", "fractions"):
        assert not any(forbidden in name for name in names), forbidden


def test_no_clock_network_float_or_composition_call() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "time", "sleep", "float", "build_cross_asset_composition_plan", "getcontext"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]


_FRAGMENTS = ("score", "rank", "recommend", "expected_return", "utility", "optimiz", "linprog", "solver", "cvar", "momentum", "technical",
              "macro", "tax", "capital_gain", "withholding", "cost_basis", "fifo", "lifo", "hifo", "broker", "quantity", "execution",
              "settle", "taxonomy", "category", "symbol", "persist", "supabase", "ineligible", "weight_choice")
_EXACT = {"alpha", "erc", "hrp", "isin", "mic", "lot", "order", "price"}


def test_no_ranking_taxonomy_tax_or_execution_surface() -> None:
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
    for name in ("resolve_candidate_universe", "bind_candidate_universes_to_sleeves"):
        function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert not function.args.args and function.args.kwonlyargs


def test_documents_the_authority_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("knowledge time", "effective time", "SOURCE_AS_OF", "SYSTEM_AS_OF", "source_key", "universe_key", "frontier",
                   "no resurrection", "survivorship", "not discover", "no taxonomy", "COMPLETE_MEMBERSHIP", "CURATED_CANDIDATES"):
        assert needle in doc, needle
