"""
backend/engine/private/scheduler_calendar_evidence.py
=====================================================
External calendar evidence boundary for the private scheduler (Phase 24B3A).

This module is evidence only. It defines the immutable boundary that represents, for ONE explicit local calendar date, what an externally
authoritative calendar says: either exact EXCHANGE SESSION windows or a SOURCE RELEASE PLAN, together with which calendar and revision, the
timezone, whether date coverage is complete, which source artifact supplied it, when Sentinax observed that artifact and whether it was
available at an explicit knowledge cutoff. It does not decide whether a scheduled occurrence should run: applicability of a civil recurrence
decision against this evidence is Phase 24B3B, dispatch and run authority is 24C, runtime and persistence is 24D. The 24B2 civil recurrence
is not an external calendar and this module imports nothing from 24A, 24B1 or 24B2.

Why exchange session windows and not an open / closed bool: official calendars contain full days, half days, early closes and complete
closures. The evidence therefore stores exact UTC session windows; a shortened close is simply a different `closes_at` and there is no
half-day flag, because the session itself is the authority. Windows are canonical UTC (`tzinfo is timezone.utc`, `opens_at < closes_at`,
no zero-length or reversed window), every boundary must fall on the evidence local date in the evidence timezone (same-local-date sessions
only; overnight sessions would need a new explicit contract), and the caller-supplied tuple must be chronological and non-overlapping
(adjacent windows are allowed; nothing is sorted).

Coverage (explicit caller assertion, never inferred):

    COMPLETE_FOR_DATE   the evidence completely describes the calendar for that date under the represented snapshot. An exchange tuple
                        of () therefore means an explicit full closure and a release tuple of () means an explicit "no planned
                        release on this date"
    UNAVAILABLE         complete coverage cannot be proven: the session / release tuple must be (); this is NEVER read as closed and
                        NEVER as "no release", so complete closure and unavailable evidence stay distinguishable

Source release plans: `DATE_ONLY` entries identify the release date and carry planned_for None (no midnight, no 09:00, no recurrence time
is invented); `EXACT_TIME` entries carry a canonical UTC planned instant that must map to the evidence local date. A planned release is
not actual source availability: planned_for is only the instant the calendar planned the release. It is not published_at, not observed_at and
not ingestion success, and there is no available_at, actual-release or success field here (actual data availability remains provider / PIT
evidence elsewhere). Release keys are exact (1..128 printable trimmed characters, case preserved) and unique inside one evidence object;
nothing is deduplicated, overwritten or prioritized.

Provenance: `calendar_key` and `source_key` are strict lowercase identifiers (no value hardcoded, source tier not inferred),
`calendar_revision` an exact int >= 1 (a Sentinax contract revision, not proof of provider completeness), `timezone_key` an exact
printable trimmed IANA key that must resolve through `ZoneInfo` (no offset table, alias normalization or UTC fallback), `local_date` an
exact `date`, and `source_content_sha256` an opaque 64-lowercase-hex reference to the calendar artifact (nothing is hashed, no authenticity
is claimed). `observed_at` is when Sentinax observed the CALENDAR ARTIFACT, not when any economic data was released; `published_at` is None
or an aware instant not later than `observed_at`.

PIT binding is SYSTEM-AS-OF only: `PrivateSchedulerCalendarPITBinding` requires, by exact UTC instant (equality allowed, no tolerance),
observed_at <= knowledge_cutoff and, when present, published_at <= knowledge_cutoff. A public calendar that existed earlier does not
authorize a replayed Sentinax run to pretend it had already ingested it (source could know is not Sentinax knew), so there is no
SOURCE_AS_OF mode and no mode enum here.

Exclusions: no provider adapter or network, no hardcoded exchange, holiday or release table, no weekend inference (Saturday and Sunday
imply nothing; only explicit COMPLETE evidence with no windows means closed, and UNAVAILABLE is unknown), no ambient clock, no dispatch, run
state, retry or persistence, and no use of the legacy `backend/infrastructure/scheduler.py`.

Architectural Invariants:
    - Pure domain module: standard library only (`zoneinfo` for key resolution). No Sentinax import, no async, no network, no database.
    - Every dataclass re-validates its complete contract on direct construction; exact concrete types, subclasses are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_UTC_TYPE = "session and planned instants must be exact datetime instances"
_ERR_UTC_VALUE = "session and planned instants must be canonical UTC (tzinfo is timezone.utc)"
_ERR_WINDOW_ORDER = "a session window must satisfy opens_at < closes_at"
_ERR_RELEASE_KEY_TYPE = "release_key must be an exact str instance"
_ERR_RELEASE_KEY_VALUE = "release_key must be a printable, already trimmed string of 1 to 128 characters"
_ERR_PRECISION = "time_precision must be an exact PrivateSchedulerReleaseTimePrecision instance"
_ERR_DATE_ONLY = "a DATE_ONLY release must carry planned_for None"
_ERR_CALENDAR_KEY_TYPE = "calendar_key and source_key must be exact str instances"
_ERR_CALENDAR_KEY_VALUE = "calendar_key and source_key must be canonical lowercase identifiers of at most 128 characters"
_ERR_REVISION_TYPE = "calendar_revision must be an exact int instance"
_ERR_REVISION_VALUE = "calendar_revision must be at least 1"
_ERR_KIND = "calendar_kind must be an exact PrivateSchedulerCalendarKind instance"
_ERR_COVERAGE = "coverage must be an exact PrivateSchedulerCalendarCoverage instance"
_ERR_TIMEZONE_TYPE = "timezone_key must be an exact str instance"
_ERR_TIMEZONE_VALUE = "timezone_key must be a printable, already trimmed IANA key of 1 to 128 characters"
_ERR_TIMEZONE_UNKNOWN = "timezone_key is not an available IANA timezone"
_ERR_DATE = "local_date must be an exact date instance"
_ERR_SESSIONS_TYPE = "exchange_sessions must be a tuple of exact PrivateSchedulerExchangeSessionWindow instances"
_ERR_RELEASES_TYPE = "source_releases must be a tuple of exact PrivateSchedulerSourceReleasePlanEntry instances"
_ERR_DATETIME = "published_at, observed_at and knowledge_cutoff must be exact timezone-aware datetime instances"
_ERR_PUBLISHED = "published_at must not be later than observed_at"
_ERR_HASH_TYPE = "source_content_sha256 must be an exact str instance"
_ERR_HASH_VALUE = "source_content_sha256 must be exactly 64 lowercase hexadecimal characters"
_ERR_EXCHANGE_SHAPE = "an EXCHANGE_SESSION evidence object must have no source releases"
_ERR_RELEASE_SHAPE = "a SOURCE_RELEASE_PLAN evidence object must have no exchange sessions"
_ERR_UNAVAILABLE_SHAPE = "UNAVAILABLE coverage must carry no sessions and no releases"
_ERR_SESSION_DATE = "every session boundary must fall on the evidence local date in the evidence timezone"
_ERR_SESSION_ORDER = "sessions must be chronological and non-overlapping in the supplied order"
_ERR_RELEASE_DUPLICATE = "release_key must be unique inside one evidence object"
_ERR_RELEASE_DATE = "an EXACT_TIME planned release must map to the evidence local date"
_ERR_EVIDENCE_TYPE = "evidence must be an exact PrivateSchedulerCalendarDateEvidence instance"
_ERR_NOT_OBSERVED = "calendar evidence was not observed (and published) by the knowledge cutoff"


class PrivateSchedulerCalendarKind(Enum):
    EXCHANGE_SESSION = "exchange_session"
    SOURCE_RELEASE_PLAN = "source_release_plan"


class PrivateSchedulerCalendarCoverage(Enum):
    """Explicit coverage assertion: COMPLETE_FOR_DATE (an empty tuple is an explicit empty answer) or UNAVAILABLE (unknown, never closed)."""
    COMPLETE_FOR_DATE = "complete_for_date"
    UNAVAILABLE = "unavailable"


class PrivateSchedulerReleaseTimePrecision(Enum):
    """Precision of the PLANNED calendar entry; says nothing about actual publication availability."""
    DATE_ONLY = "date_only"
    EXACT_TIME = "exact_time"


def _check_utc(value: object) -> None:
    if type(value) is not datetime:
        raise TypeError(_ERR_UTC_TYPE)
    if value.tzinfo is not timezone.utc:
        raise ValueError(_ERR_UTC_VALUE)


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


def _check_identifier(value: object) -> None:
    if type(value) is not str:
        raise TypeError(_ERR_CALENDAR_KEY_TYPE)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(_ERR_CALENDAR_KEY_VALUE)


@dataclass(frozen=True)
class PrivateSchedulerExchangeSessionWindow:
    """One exact canonical-UTC trading-session window; a shortened close is just a different closes_at."""
    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        _check_utc(self.opens_at)
        _check_utc(self.closes_at)
        if not self.opens_at < self.closes_at:
            raise ValueError(_ERR_WINDOW_ORDER)


@dataclass(frozen=True)
class PrivateSchedulerSourceReleasePlanEntry:
    """One PLANNED release entry (not actual availability): DATE_ONLY has no instant, EXACT_TIME a canonical UTC planned instant."""
    release_key: str
    time_precision: PrivateSchedulerReleaseTimePrecision
    planned_for: datetime | None

    def __post_init__(self) -> None:
        if type(self.release_key) is not str:
            raise TypeError(_ERR_RELEASE_KEY_TYPE)
        if not 1 <= len(self.release_key) <= 128 or self.release_key != self.release_key.strip() or not self.release_key.isprintable():
            raise ValueError(_ERR_RELEASE_KEY_VALUE)
        if type(self.time_precision) is not PrivateSchedulerReleaseTimePrecision:
            raise TypeError(_ERR_PRECISION)
        if self.time_precision is PrivateSchedulerReleaseTimePrecision.DATE_ONLY:
            if self.planned_for is not None:
                raise ValueError(_ERR_DATE_ONLY)
        else:
            _check_utc(self.planned_for)


@dataclass(frozen=True)
class PrivateSchedulerCalendarDateEvidence:
    """External calendar evidence for one local date: sessions or a release plan, coverage and source provenance."""
    calendar_key: str
    calendar_revision: int
    calendar_kind: PrivateSchedulerCalendarKind
    timezone_key: str
    local_date: date
    coverage: PrivateSchedulerCalendarCoverage
    exchange_sessions: tuple[PrivateSchedulerExchangeSessionWindow, ...]
    source_releases: tuple[PrivateSchedulerSourceReleasePlanEntry, ...]
    source_key: str
    published_at: datetime | None
    observed_at: datetime
    source_content_sha256: str

    def __post_init__(self) -> None:
        _check_identifier(self.calendar_key)
        if type(self.calendar_revision) is not int:
            raise TypeError(_ERR_REVISION_TYPE)
        if self.calendar_revision < 1:
            raise ValueError(_ERR_REVISION_VALUE)
        if type(self.calendar_kind) is not PrivateSchedulerCalendarKind:
            raise TypeError(_ERR_KIND)
        if type(self.timezone_key) is not str:
            raise TypeError(_ERR_TIMEZONE_TYPE)
        if not 1 <= len(self.timezone_key) <= 128 or self.timezone_key != self.timezone_key.strip() or not self.timezone_key.isprintable():
            raise ValueError(_ERR_TIMEZONE_VALUE)
        try:
            zone = ZoneInfo(self.timezone_key)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(_ERR_TIMEZONE_UNKNOWN) from None
        if type(self.local_date) is not date:
            raise TypeError(_ERR_DATE)
        if type(self.coverage) is not PrivateSchedulerCalendarCoverage:
            raise TypeError(_ERR_COVERAGE)
        if type(self.exchange_sessions) is not tuple:
            raise TypeError(_ERR_SESSIONS_TYPE)
        for window in self.exchange_sessions:
            if type(window) is not PrivateSchedulerExchangeSessionWindow:
                raise TypeError(_ERR_SESSIONS_TYPE)
        if type(self.source_releases) is not tuple:
            raise TypeError(_ERR_RELEASES_TYPE)
        for release in self.source_releases:
            if type(release) is not PrivateSchedulerSourceReleasePlanEntry:
                raise TypeError(_ERR_RELEASES_TYPE)
        _check_identifier(self.source_key)
        _check_aware(self.observed_at)
        if self.published_at is not None:
            _check_aware(self.published_at)
            if self.published_at.astimezone(timezone.utc) > self.observed_at.astimezone(timezone.utc):
                raise ValueError(_ERR_PUBLISHED)
        if type(self.source_content_sha256) is not str:
            raise TypeError(_ERR_HASH_TYPE)
        if _SHA256.fullmatch(self.source_content_sha256) is None:
            raise ValueError(_ERR_HASH_VALUE)
        if self.calendar_kind is PrivateSchedulerCalendarKind.EXCHANGE_SESSION:
            if self.source_releases:
                raise ValueError(_ERR_EXCHANGE_SHAPE)
        elif self.exchange_sessions:
            raise ValueError(_ERR_RELEASE_SHAPE)
        if self.coverage is PrivateSchedulerCalendarCoverage.UNAVAILABLE and (self.exchange_sessions or self.source_releases):
            raise ValueError(_ERR_UNAVAILABLE_SHAPE)
        previous_close = None
        for window in self.exchange_sessions:
            if window.opens_at.astimezone(zone).date() != self.local_date or window.closes_at.astimezone(zone).date() != self.local_date:
                raise ValueError(_ERR_SESSION_DATE)
            if previous_close is not None and window.opens_at < previous_close:
                raise ValueError(_ERR_SESSION_ORDER)
            previous_close = window.closes_at
        seen: set[str] = set()
        for release in self.source_releases:
            if release.release_key in seen:
                raise ValueError(_ERR_RELEASE_DUPLICATE)
            seen.add(release.release_key)
            if release.planned_for is not None and release.planned_for.astimezone(zone).date() != self.local_date:
                raise ValueError(_ERR_RELEASE_DATE)


def build_private_scheduler_calendar_date_evidence(
    *,
    calendar_key: str,
    calendar_revision: int,
    calendar_kind: PrivateSchedulerCalendarKind,
    timezone_key: str,
    local_date: date,
    coverage: PrivateSchedulerCalendarCoverage,
    exchange_sessions: tuple[PrivateSchedulerExchangeSessionWindow, ...],
    source_releases: tuple[PrivateSchedulerSourceReleasePlanEntry, ...],
    source_key: str,
    published_at: datetime | None,
    observed_at: datetime,
    source_content_sha256: str,
) -> PrivateSchedulerCalendarDateEvidence:
    """Validate explicit external calendar evidence for one local date; nothing is inferred, defaulted or fetched."""
    return PrivateSchedulerCalendarDateEvidence(
        calendar_key=calendar_key,
        calendar_revision=calendar_revision,
        calendar_kind=calendar_kind,
        timezone_key=timezone_key,
        local_date=local_date,
        coverage=coverage,
        exchange_sessions=exchange_sessions,
        source_releases=source_releases,
        source_key=source_key,
        published_at=published_at,
        observed_at=observed_at,
        source_content_sha256=source_content_sha256,
    )


@dataclass(frozen=True)
class PrivateSchedulerCalendarPITBinding:
    """Evidence proven observed (and published) by an explicit knowledge cutoff; retains the evidence by identity."""
    evidence: PrivateSchedulerCalendarDateEvidence
    knowledge_cutoff: datetime

    def __post_init__(self) -> None:
        if type(self.evidence) is not PrivateSchedulerCalendarDateEvidence:
            raise TypeError(_ERR_EVIDENCE_TYPE)
        _check_aware(self.knowledge_cutoff)
        cutoff = self.knowledge_cutoff.astimezone(timezone.utc)
        if self.evidence.observed_at.astimezone(timezone.utc) > cutoff:
            raise ValueError(_ERR_NOT_OBSERVED)
        if self.evidence.published_at is not None and self.evidence.published_at.astimezone(timezone.utc) > cutoff:
            raise ValueError(_ERR_NOT_OBSERVED)


def bind_private_scheduler_calendar_pit(
    *,
    evidence: PrivateSchedulerCalendarDateEvidence,
    knowledge_cutoff: datetime,
) -> PrivateSchedulerCalendarPITBinding:
    """Bind calendar evidence to an explicit knowledge cutoff (SYSTEM-AS-OF); fails closed when it was not yet observed."""
    return PrivateSchedulerCalendarPITBinding(evidence=evidence, knowledge_cutoff=knowledge_cutoff)
