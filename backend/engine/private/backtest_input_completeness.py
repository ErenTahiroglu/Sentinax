"""
backend/engine/private/backtest_input_completeness.py
=====================================================
Explicit replay input requirement manifest and historical input completeness bundle (Phase 26C2E). The caller declares, per replay decision, exactly which input
slots are required (`PrivateBacktestInputRequirement`: a closed kind plus a canonical key). The bundle then holds, relative to that manifest only, either exactly one
compatible historical authority object for each slot or an explicit entry in `missing_requirements`. Nothing is discovered, defaulted or inferred: the only universal
requirement is exactly one PORTFOLIO_HISTORY slot (a zero-transaction portfolio is valid, no portfolio is not).

Canonical keys derive from the evidence objects themselves, never hashed and never caller supplied:

    CANDIDATE_UNIVERSE   source_key|universe_key|asset_class.value   (from binding.resolution.query)
    MACRO                fact.canonical_key
    GAME_CHANGER         family.coverage_provenance_sha256            (the existing opaque family-evidence reference, not a global event-family id)
    RISK_EVIDENCE        evidence.kind.value
    PORTFOLIO_HISTORY    owner_id|portfolio_id                         (canonical str(UUID))
    MARKET_DATA          kind.value|query_key.to_string()
    USER_VIEWS           the constant "user_views"                     (at most one requirement)

Completeness is input-bundle completeness, NOT data quality, availability or investment safety: this module never reads an input's internal status. A
NO_SNAPSHOT_AS_OF universe, an UNAVAILABLE macro fact, MissingRiskEvidence, an INCOMPLETE_COVERAGE Game Changer family, any market-data resolution status and an
INCOMPLETE_COVERAGE user-view resolution are all represented historical states that fill their slot; Phase 26D owns their fail-closed handling. Every supplied
object must belong to the one exact analysis context (C2A bindings, portfolio coverage and user views by analysis-context object identity; market data by replay-point
object identity). Duplicate evidence keys and evidence that the manifest does not declare fail closed; an absent required slot is reported, not an error. Ordering is
canonical (kind.value, key) so caller order has no authority, and the exact source objects are retained by identity. Direct construction re-runs the same derivation,
so a forged status, missing tuple (including equal-valued cloned requirements) or noncanonical order is rejected.

Claim limit: completeness is relative to the caller's manifest; it does not prove the manifest lists every input a strategy ought to use, and an empty category means
that input is outside the declared replay contract, not that none exist. Non-pure by dependency composition; no I/O, clock, randomness, hashing or persistence, and no
decision logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext
from backend.engine.private.backtest_input_bindings import (
    PrivateBacktestCandidateUniverseInputBinding,
    PrivateBacktestGameChangerInputBinding,
    PrivateBacktestMacroInputBinding,
    PrivateBacktestRiskEvidenceInputBinding,
)
from backend.engine.private.backtest_market_data_resolution_snapshot import PrivateBacktestMarketDataResolutionSnapshot
from backend.engine.private.backtest_portfolio_history_coverage import PrivateBacktestPortfolioHistoryCoverage
from backend.engine.private.backtest_user_view_resolution import PrivateBacktestUserViewResolution

USER_VIEWS_KEY = "user_views"

_ERR_KIND = "kind must be an exact PrivateBacktestInputKind member"
_ERR_KEY_TYPE = "key must be an exact str instance"
_ERR_KEY_VALUE = "key must be non-empty with no leading or trailing whitespace"
_ERR_CONTEXT = "analysis_context must be an exact PrivateBacktestAnalysisContext instance"
_ERR_REQUIREMENTS = "requirements must be an exact tuple of exact PrivateBacktestInputRequirement instances"
_ERR_CATEGORY = "{name} must be an exact tuple of exact {cls} instances"
_ERR_PORTFOLIO_TYPE = "portfolio_history must be None or an exact PrivateBacktestPortfolioHistoryCoverage instance"
_ERR_USER_VIEWS_TYPE = "user_views must be None or an exact PrivateBacktestUserViewResolution instance"
_ERR_STATUS_TYPE = "status must be an exact PrivateBacktestInputCompletenessStatus member"
_ERR_MISSING_TYPE = "missing_requirements must be an exact tuple of exact PrivateBacktestInputRequirement instances"
_ERR_REQUIREMENT_ORDER = "requirements must be in canonical (kind.value, key) order"
_ERR_REQUIREMENT_DUPLICATE = "duplicate (kind, key) requirement"
_ERR_PORTFOLIO_COUNT = "exactly one PORTFOLIO_HISTORY requirement is mandatory"
_ERR_USER_VIEWS_REQUIREMENT = "at most one USER_VIEWS requirement is allowed and its key must be exactly 'user_views'"
_ERR_CONTEXT_OWNER = "{name} evidence must belong to the very analysis context object"
_ERR_REPLAY_POINT = "market-data evidence must belong to the very analysis replay point object"
_ERR_EVIDENCE_ORDER = "{name} must be in canonical key order"
_ERR_EVIDENCE_DUPLICATE = "duplicate {name} evidence key"
_ERR_EXTRA = "evidence is not declared by the requirement manifest"
_ERR_MISSING = "missing_requirements must be exactly the canonical requirement objects with no supplied evidence"
_ERR_STATUS = "status must be COMPLETE exactly when no requirement is missing"


class PrivateBacktestInputKind(Enum):
    CANDIDATE_UNIVERSE = "candidate_universe"
    MACRO = "macro"
    GAME_CHANGER = "game_changer"
    RISK_EVIDENCE = "risk_evidence"
    PORTFOLIO_HISTORY = "portfolio_history"
    MARKET_DATA = "market_data"
    USER_VIEWS = "user_views"


class PrivateBacktestInputCompletenessStatus(Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class PrivateBacktestInputRequirement:
    """One explicitly required replay input slot; the key's canonical meaning is category specific and never normalized."""
    kind: PrivateBacktestInputKind
    key: str

    def __post_init__(self) -> None:
        if type(self.kind) is not PrivateBacktestInputKind:
            raise TypeError(_ERR_KIND)
        if type(self.key) is not str:
            raise TypeError(_ERR_KEY_TYPE)
        if self.key == "" or self.key != self.key.strip():
            raise ValueError(_ERR_KEY_VALUE)


# --- canonical identity of supplied evidence (a pure derivation; no status or quality field is read) ----------------------

def _candidate_key(binding: PrivateBacktestCandidateUniverseInputBinding) -> str:
    query = binding.resolution.query
    return f"{query.source_key}|{query.universe_key}|{query.asset_class.value}"


def _macro_key(binding: PrivateBacktestMacroInputBinding) -> str:
    return binding.fact.canonical_key


def _game_changer_key(binding: PrivateBacktestGameChangerInputBinding) -> str:
    return binding.resolution.family.coverage_provenance_sha256


def _risk_key(binding: PrivateBacktestRiskEvidenceInputBinding) -> str:
    return binding.evidence.kind.value


def _portfolio_key(history: PrivateBacktestPortfolioHistoryCoverage) -> str:
    return f"{history.owner_id}|{history.portfolio_id}"


def _market_key(snapshot: PrivateBacktestMarketDataResolutionSnapshot) -> str:
    return f"{snapshot.kind.value}|{snapshot.query_key.to_string()}"


def _requirement_order(requirement: PrivateBacktestInputRequirement) -> tuple:
    return (requirement.kind.value, requirement.key)


# (bundle field name, input kind, exact class, key derivation), in the bundle's field order
_CATEGORIES = (
    ("candidate_universes", PrivateBacktestInputKind.CANDIDATE_UNIVERSE, PrivateBacktestCandidateUniverseInputBinding, _candidate_key),
    ("macro_inputs", PrivateBacktestInputKind.MACRO, PrivateBacktestMacroInputBinding, _macro_key),
    ("game_changers", PrivateBacktestInputKind.GAME_CHANGER, PrivateBacktestGameChangerInputBinding, _game_changer_key),
    ("risk_evidence", PrivateBacktestInputKind.RISK_EVIDENCE, PrivateBacktestRiskEvidenceInputBinding, _risk_key),
    ("market_data", PrivateBacktestInputKind.MARKET_DATA, PrivateBacktestMarketDataResolutionSnapshot, _market_key),
)


def _is_tuple_of(value: object, cls: type) -> bool:
    return type(value) is tuple and all(type(member) is cls for member in value)


def _check_types(analysis_context, requirements, categories, portfolio_history, user_views) -> None:
    if type(analysis_context) is not PrivateBacktestAnalysisContext:
        raise TypeError(_ERR_CONTEXT)
    if not _is_tuple_of(requirements, PrivateBacktestInputRequirement):
        raise TypeError(_ERR_REQUIREMENTS)
    for (name, _kind, cls, _derive), members in zip(_CATEGORIES, categories):
        if not _is_tuple_of(members, cls):
            raise TypeError(_ERR_CATEGORY.format(name=name, cls=cls.__name__))
    if portfolio_history is not None and type(portfolio_history) is not PrivateBacktestPortfolioHistoryCoverage:
        raise TypeError(_ERR_PORTFOLIO_TYPE)
    if user_views is not None and type(user_views) is not PrivateBacktestUserViewResolution:
        raise TypeError(_ERR_USER_VIEWS_TYPE)


def _check_requirements(requirements) -> None:
    for earlier, later in zip(requirements, requirements[1:]):
        if _requirement_order(earlier) == _requirement_order(later):
            raise ValueError(_ERR_REQUIREMENT_DUPLICATE)
        if _requirement_order(earlier) > _requirement_order(later):
            raise ValueError(_ERR_REQUIREMENT_ORDER)
    if sum(1 for r in requirements if r.kind is PrivateBacktestInputKind.PORTFOLIO_HISTORY) != 1:
        raise ValueError(_ERR_PORTFOLIO_COUNT)
    user_view_requirements = [r for r in requirements if r.kind is PrivateBacktestInputKind.USER_VIEWS]
    if len(user_view_requirements) > 1 or any(r.key != USER_VIEWS_KEY for r in user_view_requirements):
        raise ValueError(_ERR_USER_VIEWS_REQUIREMENT)


def _check_context_ownership(analysis_context, categories, portfolio_history, user_views) -> None:
    for (name, kind, _cls, _derive), members in zip(_CATEGORIES, categories):
        for member in members:
            if kind is PrivateBacktestInputKind.MARKET_DATA:
                if member.market_context.replay_point is not analysis_context.replay_point:
                    raise ValueError(_ERR_REPLAY_POINT)
            elif member.analysis_context is not analysis_context:
                raise ValueError(_ERR_CONTEXT_OWNER.format(name=name))
    if portfolio_history is not None and portfolio_history.projection_binding.analysis_context is not analysis_context:
        raise ValueError(_ERR_CONTEXT_OWNER.format(name="portfolio_history"))
    if user_views is not None and user_views.analysis_context is not analysis_context:
        raise ValueError(_ERR_CONTEXT_OWNER.format(name="user_views"))


def _check_evidence_order(categories) -> None:
    for (name, _kind, _cls, derive), members in zip(_CATEGORIES, categories):
        for earlier, later in zip(members, members[1:]):
            first, second = derive(earlier), derive(later)
            if first == second:
                raise ValueError(_ERR_EVIDENCE_DUPLICATE.format(name=name))
            if first > second:
                raise ValueError(_ERR_EVIDENCE_ORDER.format(name=name))


def _present_slots(categories, portfolio_history, user_views) -> set:
    """The (kind, key) identity of every supplied evidence object; categories are already proven duplicate-free."""
    slots = set()
    for (_name, kind, _cls, derive), members in zip(_CATEGORIES, categories):
        slots.update((kind, derive(member)) for member in members)
    if portfolio_history is not None:
        slots.add((PrivateBacktestInputKind.PORTFOLIO_HISTORY, _portfolio_key(portfolio_history)))
    if user_views is not None:
        slots.add((PrivateBacktestInputKind.USER_VIEWS, USER_VIEWS_KEY))
    return slots


def _status_for(missing: tuple) -> PrivateBacktestInputCompletenessStatus:
    return PrivateBacktestInputCompletenessStatus.COMPLETE if missing == () else PrivateBacktestInputCompletenessStatus.INCOMPLETE


def _derive_missing(analysis_context, requirements, categories, portfolio_history, user_views) -> tuple:
    """The whole canonical derivation; the builder and direct construction both run exactly this on canonically ordered tuples."""
    _check_types(analysis_context, requirements, categories, portfolio_history, user_views)
    _check_requirements(requirements)
    _check_context_ownership(analysis_context, categories, portfolio_history, user_views)
    _check_evidence_order(categories)
    declared = {(r.kind, r.key) for r in requirements}
    present = _present_slots(categories, portfolio_history, user_views)
    if not present <= declared:
        raise ValueError(_ERR_EXTRA)
    return tuple(r for r in requirements if (r.kind, r.key) not in present)


@dataclass(frozen=True)
class PrivateBacktestInputBundle:
    """The explicit requirement manifest and, relative to it only, the exact historical authorities supplied for the one replay context."""
    analysis_context: PrivateBacktestAnalysisContext
    requirements: tuple[PrivateBacktestInputRequirement, ...]
    candidate_universes: tuple[PrivateBacktestCandidateUniverseInputBinding, ...]
    macro_inputs: tuple[PrivateBacktestMacroInputBinding, ...]
    game_changers: tuple[PrivateBacktestGameChangerInputBinding, ...]
    risk_evidence: tuple[PrivateBacktestRiskEvidenceInputBinding, ...]
    portfolio_history: PrivateBacktestPortfolioHistoryCoverage | None
    market_data: tuple[PrivateBacktestMarketDataResolutionSnapshot, ...]
    user_views: PrivateBacktestUserViewResolution | None
    status: PrivateBacktestInputCompletenessStatus
    missing_requirements: tuple[PrivateBacktestInputRequirement, ...]

    def __post_init__(self) -> None:
        categories = (self.candidate_universes, self.macro_inputs, self.game_changers, self.risk_evidence, self.market_data)
        missing = _derive_missing(self.analysis_context, self.requirements, categories, self.portfolio_history, self.user_views)
        if type(self.status) is not PrivateBacktestInputCompletenessStatus:
            raise TypeError(_ERR_STATUS_TYPE)
        if not _is_tuple_of(self.missing_requirements, PrivateBacktestInputRequirement):
            raise TypeError(_ERR_MISSING_TYPE)
        if len(self.missing_requirements) != len(missing) or not all(a is b for a, b in zip(self.missing_requirements, missing)):
            raise ValueError(_ERR_MISSING)
        if self.status is not _status_for(missing):
            raise ValueError(_ERR_STATUS)


def build_private_backtest_input_bundle(
    *,
    analysis_context: PrivateBacktestAnalysisContext,
    requirements: tuple[PrivateBacktestInputRequirement, ...],
    candidate_universes: tuple[PrivateBacktestCandidateUniverseInputBinding, ...],
    macro_inputs: tuple[PrivateBacktestMacroInputBinding, ...],
    game_changers: tuple[PrivateBacktestGameChangerInputBinding, ...],
    risk_evidence: tuple[PrivateBacktestRiskEvidenceInputBinding, ...],
    portfolio_history: PrivateBacktestPortfolioHistoryCoverage | None,
    market_data: tuple[PrivateBacktestMarketDataResolutionSnapshot, ...],
    user_views: PrivateBacktestUserViewResolution | None,
) -> PrivateBacktestInputBundle:
    """Canonicalize every tuple (caller order has no authority), derive the missing requirements and status, and construct the validated bundle."""
    categories = (candidate_universes, macro_inputs, game_changers, risk_evidence, market_data)
    _check_types(analysis_context, requirements, categories, portfolio_history, user_views)
    ordered = tuple(sorted(requirements, key=_requirement_order))
    ordered_categories = tuple(tuple(sorted(members, key=derive)) for (_name, _kind, _cls, derive), members in zip(_CATEGORIES, categories))
    missing = _derive_missing(analysis_context, ordered, ordered_categories, portfolio_history, user_views)
    return PrivateBacktestInputBundle(
        analysis_context=analysis_context, requirements=ordered, candidate_universes=ordered_categories[0], macro_inputs=ordered_categories[1],
        game_changers=ordered_categories[2], risk_evidence=ordered_categories[3], portfolio_history=portfolio_history, market_data=ordered_categories[4],
        user_views=user_views, status=_status_for(missing), missing_requirements=missing)
