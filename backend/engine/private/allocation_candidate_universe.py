"""
backend/engine/private/allocation_candidate_universe.py
=======================================================
Point-in-time candidate-universe authority and Phase 22A sleeve binding (Phase 22B).

Phase 22A turns explicit sleeves into a Phase 21 target / current-state pair. This module answers the upstream question: for the
decision being evaluated, which explicit candidate set was admissible under the knowledge available at that time, and what
source / effective-time provenance supports it? It consumes already-resolved canonical UUIDs inside source-neutral, immutable
membership snapshots; it does not discover securities, rank candidates, choose weights, infer an asset class or a taxonomy,
fetch provider data or hash payloads. `content_sha256` is an opaque upstream content-provenance reference: nothing is hashed,
authenticated or signature-verified here.

Three distinct times (never interchangeable):

    knowledge time        when a universe fact was public (`published_at`) and / or observed by Sentinax (`observed_at`)
    effective time        when the membership state applies economically: half-open [effective_from, effective_to)
    evaluation date       the date whose candidate universe is requested

    SOURCE_AS_OF   availability = published_at, else observed_at (deterministic fallback); usable iff availability <= cutoff
    SYSTEM_AS_OF   usable iff observed_at <= cutoff and (published_at is None or published_at <= cutoff)

All instants are compared as exact UTC instants (equality is allowed, no tolerance, no clock call).

Resolution pipeline (`resolve_candidate_universe`; one private function shared by the resolver and the constructors):

    1. source / universe_key / asset_class match         none -> NO_SOURCE_SNAPSHOT
    2. effective interval contains the evaluation date    none -> NO_EFFECTIVE_SNAPSHOT   (no backcast of today's list)
    3. PIT availability filter, BEFORE any conflict test  none -> NO_SNAPSHOT_AS_OF       (no future evidence, no fallback)
    4. latest applicable effective_from regime
    5. knowledge frontier inside the regime (latest availability instant)
    6. frontier members with identical economic fields are logical duplicates (earliest observed_at is the representative);
       any disagreement on hash, coverage, effective interval, members or published_at -> FRONTIER_CONFLICT, no tie-break

Future isolation: a later correction, conflicting list or corrupted snapshot that is not yet available at the cutoff cannot
change the selection or create a conflict. A correction available by the cutoff with a strictly later frontier supersedes the
older state: no resurrection and no union of snapshots (a member dropped by a newer snapshot stays dropped; for
CURATED_CANDIDATES that means only "not in this curated set", never a global ineligibility claim). `SELECTED` with an empty
member tuple is a valid declared state and differs from every unavailable status; missing is never turned into empty.

Coverage: COMPLETE_MEMBERSHIP is only the upstream claim that the snapshot lists every member of that declared universe_key and
regime (not independently proven here). CURATED_CANDIDATES is a subset / watchlist. source_key (provider) and universe_key (the
declared universe definition) are separate identities. The survivorship defense: a list of today's constituents is not a historical
universe unless upstream evidence supplies the historical effective membership; nothing here infers membership from current
existence, index membership, fund category or instrument status.

Binding (`bind_candidate_universes_to_sleeves`): one SELECTED resolution per sleeve in the same AssetClass order, all sharing the
same evaluation date and the same AnalysisPITContext object, and every sleeve instrument must be a member of its selected
snapshot. The binding never changes weights, candidates or ordering, never drops or replaces a candidate and does not require all
members to be held. UUID ordering is representation only.

Architectural Invariants:
    - Pure domain module: standard library, `AnalysisPITContext`, `AsOfMode`, `AssetClass` and the Phase 22A `CrossAssetSleeve`
      type. No network, filesystem, database, clock, randomness, float arithmetic or persistence; it does not call the
      composition builder; no identity, taxonomy (no taxonomy of any kind), provider, market-data or crypto lookup.
    - Resolution and binding objects retain their inputs by identity and recompute their canonical content on construction,
      rejecting forged status, selected snapshot, collection, universe or sleeve combinations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_universe_composition import CrossAssetSleeve
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, AssetClass

_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_UNIVERSE_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_SOURCE_KEY = "source_key must be a canonical lowercase identifier of at most 64 characters"
_ERR_UNIVERSE_KEY = "universe_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_SHA = "content_sha256 must be exactly 64 lowercase hexadecimal characters"
_ERR_KEY_TYPE = "source_key and universe_key must be exact str instances"
_ERR_ASSET_CLASS = "asset_class must be an exact AssetClass instance"
_ERR_COVERAGE = "coverage must be an exact CandidateUniverseCoverage instance"
_ERR_DATE = "effective_from, effective_to and evaluation_date must be exact date instances"
_ERR_EFFECTIVE_TO = "effective_to must be None or strictly greater than effective_from"
_ERR_DATETIME = "published_at and observed_at must be timezone-aware datetime instances"
_ERR_PUBLISHED = "published_at must not be later than observed_at"
_ERR_IDS_TYPE = "instrument_ids must be a tuple of exact UUID instances"
_ERR_IDS_VALUE = "instrument_ids must be unique and in canonical ascending UUID string order"
_ERR_QUERY_TYPE = "query must be an exact CandidateUniverseQuery instance"
_ERR_PIT_TYPE = "pit_context must be an exact AnalysisPITContext instance"
_ERR_SNAPSHOTS_TYPE = "snapshots must be a tuple of exact CandidateUniverseSnapshot instances"
_ERR_SNAPSHOT_DUPLICATE = "snapshots must not contain physically duplicate entries"
_ERR_STATUS_TYPE = "status must be an exact CandidateUniverseResolutionStatus instance"
_ERR_SELECTED_TYPE = "selected_snapshot must be None or an exact CandidateUniverseSnapshot instance"
_ERR_MODE = "unsupported point-in-time mode"
_ERR_MATCH = "resolution must match the canonical point-in-time resolution exactly"
_ERR_SLEEVES_TYPE = "sleeves must be a non-empty tuple of exact CrossAssetSleeve instances"
_ERR_UNIVERSES_TYPE = "universes must be a tuple of exact CandidateUniverseResolution instances"
_ERR_SLEEVE_SEQUENCE = "sleeves must have unique asset classes in canonical ascending AssetClass value sequence"
_ERR_BINDING_SHAPE = "exactly one universe per sleeve, in the same asset class sequence, is required"
_ERR_BINDING_UNAVAILABLE = "every bound universe must be a SELECTED resolution"
_ERR_BINDING_FRONTIER = "bound universes must share one evaluation date and the same AnalysisPITContext object"
_ERR_BINDING_MEMBERSHIP = "every sleeve instrument must be a member of its selected universe snapshot"


class CandidateUniverseCoverage(Enum):
    COMPLETE_MEMBERSHIP = "complete_membership"
    CURATED_CANDIDATES = "curated_candidates"


class CandidateUniverseResolutionStatus(Enum):
    SELECTED = "selected"
    NO_SOURCE_SNAPSHOT = "no_source_snapshot"
    NO_EFFECTIVE_SNAPSHOT = "no_effective_snapshot"
    NO_SNAPSHOT_AS_OF = "no_snapshot_as_of"
    FRONTIER_CONFLICT = "frontier_conflict"


def _check_key(value: object, pattern: re.Pattern[str], message: str) -> None:
    if type(value) is not str:
        raise TypeError(_ERR_KEY_TYPE)
    if pattern.fullmatch(value) is None:
        raise ValueError(message)


def _check_aware(value: object) -> None:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(_ERR_DATETIME)
    try:
        if value.utcoffset() is None:
            raise TypeError(_ERR_DATETIME)
        value.astimezone(timezone.utc)
    except TypeError:
        raise TypeError(_ERR_DATETIME) from None
    except Exception:
        raise TypeError(_ERR_DATETIME) from None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CandidateUniverseSnapshot:
    """Immutable source-neutral membership snapshot with explicit effective and knowledge-time provenance."""
    source_key: str
    universe_key: str
    content_sha256: str
    asset_class: AssetClass
    coverage: CandidateUniverseCoverage
    effective_from: date
    effective_to: date | None
    published_at: datetime | None
    observed_at: datetime
    instrument_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _check_key(self.source_key, _SOURCE_KEY, _ERR_SOURCE_KEY)
        _check_key(self.universe_key, _UNIVERSE_KEY, _ERR_UNIVERSE_KEY)
        if type(self.content_sha256) is not str:
            raise TypeError(_ERR_SHA)
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError(_ERR_SHA)
        if type(self.asset_class) is not AssetClass:
            raise TypeError(_ERR_ASSET_CLASS)
        if type(self.coverage) is not CandidateUniverseCoverage:
            raise TypeError(_ERR_COVERAGE)
        if type(self.effective_from) is not date or (self.effective_to is not None and type(self.effective_to) is not date):
            raise TypeError(_ERR_DATE)
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(_ERR_EFFECTIVE_TO)
        _check_aware(self.observed_at)
        if self.published_at is not None:
            _check_aware(self.published_at)
            if _utc(self.published_at) > _utc(self.observed_at):
                raise ValueError(_ERR_PUBLISHED)
        if type(self.instrument_ids) is not tuple or any(type(i) is not UUID for i in self.instrument_ids):
            raise TypeError(_ERR_IDS_TYPE)
        keys = [str(i) for i in self.instrument_ids]
        if len(set(keys)) != len(keys) or keys != sorted(keys):
            raise ValueError(_ERR_IDS_VALUE)


@dataclass(frozen=True)
class CandidateUniverseQuery:
    """Explicit request: declared universe definition, asset class, evaluation date and PIT context; no defaults."""
    source_key: str
    universe_key: str
    asset_class: AssetClass
    evaluation_date: date
    pit_context: AnalysisPITContext

    def __post_init__(self) -> None:
        _check_key(self.source_key, _SOURCE_KEY, _ERR_SOURCE_KEY)
        _check_key(self.universe_key, _UNIVERSE_KEY, _ERR_UNIVERSE_KEY)
        if type(self.asset_class) is not AssetClass:
            raise TypeError(_ERR_ASSET_CLASS)
        if type(self.evaluation_date) is not date:
            raise TypeError(_ERR_DATE)
        if type(self.pit_context) is not AnalysisPITContext:
            raise TypeError(_ERR_PIT_TYPE)


def _validate_snapshots(snapshots: object) -> None:
    if type(snapshots) is not tuple or any(type(s) is not CandidateUniverseSnapshot for s in snapshots):
        raise TypeError(_ERR_SNAPSHOTS_TYPE)
    if len(set(snapshots)) != len(snapshots):
        raise ValueError(_ERR_SNAPSHOT_DUPLICATE)


def _availability(snapshot: CandidateUniverseSnapshot, mode: AsOfMode) -> datetime:
    """Knowledge instant used for admissibility (SOURCE_AS_OF) and for the knowledge frontier."""
    if mode is AsOfMode.SOURCE_AS_OF:
        return _utc(snapshot.published_at if snapshot.published_at is not None else snapshot.observed_at)
    if mode is AsOfMode.SYSTEM_AS_OF:
        return _utc(snapshot.observed_at)
    raise ValueError(_ERR_MODE)


def _admissible(snapshot: CandidateUniverseSnapshot, mode: AsOfMode, cutoff: datetime) -> bool:
    if mode is AsOfMode.SOURCE_AS_OF:
        return _availability(snapshot, mode) <= cutoff
    if mode is AsOfMode.SYSTEM_AS_OF:
        published_ok = snapshot.published_at is None or _utc(snapshot.published_at) <= cutoff
        return _utc(snapshot.observed_at) <= cutoff and published_ok
    raise ValueError(_ERR_MODE)


def _economic_identity(snapshot: CandidateUniverseSnapshot) -> tuple[object, ...]:
    published = None if snapshot.published_at is None else _utc(snapshot.published_at)
    return (snapshot.source_key, snapshot.universe_key, snapshot.content_sha256, snapshot.asset_class, snapshot.coverage,
            snapshot.effective_from, snapshot.effective_to, snapshot.instrument_ids, published)


def _resolve(
    query: CandidateUniverseQuery,
    snapshots: tuple[CandidateUniverseSnapshot, ...],
) -> tuple[CandidateUniverseResolutionStatus, CandidateUniverseSnapshot | None]:
    """Single canonical resolution shared by the resolver and the constructor verification; independent of tuple order."""
    status = CandidateUniverseResolutionStatus
    mode = query.pit_context.mode
    cutoff = query.pit_context.knowledge_cutoff_utc
    matching = [s for s in snapshots
                if s.source_key == query.source_key and s.universe_key == query.universe_key and s.asset_class is query.asset_class]
    if not matching:
        return (status.NO_SOURCE_SNAPSHOT, None)
    effective = [s for s in matching
                 if s.effective_from <= query.evaluation_date and (s.effective_to is None or query.evaluation_date < s.effective_to)]
    if not effective:
        return (status.NO_EFFECTIVE_SNAPSHOT, None)
    known = [s for s in effective if _admissible(s, mode, cutoff)]
    if not known:
        return (status.NO_SNAPSHOT_AS_OF, None)
    regime_start = max(s.effective_from for s in known)
    regime = [s for s in known if s.effective_from == regime_start]
    latest = max(_availability(s, mode) for s in regime)
    frontier = [s for s in regime if _availability(s, mode) == latest]
    if len({_economic_identity(s) for s in frontier}) != 1:
        return (status.FRONTIER_CONFLICT, None)
    representative = min(frontier, key=lambda s: _utc(s.observed_at))
    return (status.SELECTED, representative)


@dataclass(frozen=True)
class CandidateUniverseResolution:
    """Canonical PIT resolution; retains the query and snapshots by identity and rejects any forged outcome."""
    query: CandidateUniverseQuery
    snapshots: tuple[CandidateUniverseSnapshot, ...]
    status: CandidateUniverseResolutionStatus
    selected_snapshot: CandidateUniverseSnapshot | None

    def __post_init__(self) -> None:
        if type(self.query) is not CandidateUniverseQuery:
            raise TypeError(_ERR_QUERY_TYPE)
        _validate_snapshots(self.snapshots)
        if type(self.status) is not CandidateUniverseResolutionStatus:
            raise TypeError(_ERR_STATUS_TYPE)
        if self.selected_snapshot is not None and type(self.selected_snapshot) is not CandidateUniverseSnapshot:
            raise TypeError(_ERR_SELECTED_TYPE)
        status, selected = _resolve(self.query, self.snapshots)
        if self.status is not status or self.selected_snapshot != selected:
            raise ValueError(_ERR_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...] | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.instrument_ids

    @property
    def coverage(self) -> CandidateUniverseCoverage | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.coverage

    @property
    def source_key(self) -> str | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.source_key

    @property
    def universe_key(self) -> str | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.universe_key

    @property
    def effective_from(self) -> date | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.effective_from

    @property
    def effective_to(self) -> date | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.effective_to

    @property
    def published_at(self) -> datetime | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.published_at

    @property
    def observed_at(self) -> datetime | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.observed_at

    @property
    def content_sha256(self) -> str | None:
        return None if self.selected_snapshot is None else self.selected_snapshot.content_sha256


def resolve_candidate_universe(
    *,
    query: CandidateUniverseQuery,
    snapshots: tuple[CandidateUniverseSnapshot, ...],
) -> CandidateUniverseResolution:
    """The canonical PIT-admissible candidate set for a declared source / universe definition; no discovery, no fallback."""
    if type(query) is not CandidateUniverseQuery:
        raise TypeError(_ERR_QUERY_TYPE)
    _validate_snapshots(snapshots)
    status, selected = _resolve(query, snapshots)
    return CandidateUniverseResolution(query=query, snapshots=snapshots, status=status, selected_snapshot=selected)


def _validate_binding(sleeves: object, universes: object) -> None:
    if type(sleeves) is not tuple or not sleeves or any(type(s) is not CrossAssetSleeve for s in sleeves):
        raise TypeError(_ERR_SLEEVES_TYPE)
    if type(universes) is not tuple or any(type(u) is not CandidateUniverseResolution for u in universes):
        raise TypeError(_ERR_UNIVERSES_TYPE)
    keys = [s.asset_class.value for s in sleeves]
    if any(left >= right for left, right in zip(keys, keys[1:])):
        raise ValueError(_ERR_SLEEVE_SEQUENCE)
    if len(universes) != len(sleeves) or any(u.query.asset_class is not s.asset_class for s, u in zip(sleeves, universes)):
        raise ValueError(_ERR_BINDING_SHAPE)
    if any(u.status is not CandidateUniverseResolutionStatus.SELECTED for u in universes):
        raise ValueError(_ERR_BINDING_UNAVAILABLE)
    first = universes[0].query
    if any(u.query.evaluation_date != first.evaluation_date or u.query.pit_context is not first.pit_context for u in universes):
        raise ValueError(_ERR_BINDING_FRONTIER)
    for sleeve, universe in zip(sleeves, universes):
        members = set(universe.instrument_ids or ())
        if not set(sleeve.instrument_ids) <= members:
            raise ValueError(_ERR_BINDING_MEMBERSHIP)


@dataclass(frozen=True)
class CandidateUniverseSleeveBinding:
    """Proof that every Phase 22A sleeve candidate has membership provenance in a SELECTED PIT universe."""
    sleeves: tuple[CrossAssetSleeve, ...]
    universes: tuple[CandidateUniverseResolution, ...]

    def __post_init__(self) -> None:
        _validate_binding(self.sleeves, self.universes)


def bind_candidate_universes_to_sleeves(
    *,
    sleeves: tuple[CrossAssetSleeve, ...],
    universes: tuple[CandidateUniverseResolution, ...],
) -> CandidateUniverseSleeveBinding:
    """Validate candidate provenance only; never alters weights, candidates or ordering."""
    _validate_binding(sleeves, universes)
    return CandidateUniverseSleeveBinding(sleeves=sleeves, universes=universes)
