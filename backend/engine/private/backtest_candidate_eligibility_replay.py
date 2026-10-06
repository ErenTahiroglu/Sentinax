"""
backend/engine/private/backtest_candidate_eligibility_replay.py
===============================================================
Conditional historical candidate-universe eligibility replay (Phase 26D2), the second Phase 26D decision slice. It combines one COMPLETE Phase 26C2E input bundle (exactly
one PORTFOLIO_HISTORY slot plus one or more CANDIDATE_UNIVERSE slots) with EXPLICIT fixed `CrossAssetSleeve` objects and delegates, exactly once, to the CLOSED Phase 22B
`bind_candidate_universes_to_sleeves`, the only candidate-eligibility authority. This module copies none of its rules: SELECTED status, one shared evaluation date and PIT
context, the AssetClass correspondence and positive-weight membership are all enforced by that closed authority.

The sleeves are fixed replay POLICY PARAMETERS, not historically persisted settings, not a historically approved allocation and not the strategy actually used at that date.
The result is therefore a conditional (counterfactual) statement: had this sleeve configuration been evaluated at this replay point, would Phase 22B have admitted every
candidate from PIT-safe historical universe evidence? No strategy-configuration history exists here.

C2E canonicalizes candidate evidence by requirement key, which need not follow AssetClass order, while Phase 22B needs universes in the sleeves' AssetClass order. D2 therefore
maps the bundle's resolutions by exact AssetClass (exactly one per sleeve; a missing, duplicate or undeclared asset class is rejected, with no source preference and no
first-wins), then builds a transient tuple of the EXISTING resolution objects in sleeve order. That is representation alignment only: nothing is selected, cloned or reordered
inside the bundle. Present-but-unavailable states (NO_SOURCE_SNAPSHOT, NO_EFFECTIVE_SNAPSHOT, NO_SNAPSHOT_AS_OF, FRONTIER_CONFLICT) count as present for C2E and fail closed
through Phase 22B; unavailable never becomes an empty universe, a fallback or a current snapshot, and a SELECTED-empty universe is a different, valid declared state.
CURATED_CANDIDATES only says "in this curated set" (absence is never global ineligibility) and COMPLETE_MEMBERSHIP stays an upstream claim; neither is strengthened here.

Portfolio history is the replay anchor and is retained only through the bundle: no holding, transaction or value is read and no candidate is filtered by what is held. There is no
candidate discovery, ranking or weight change, no cross-asset composition, rebalance, Game Changer composition, optimizer, market data or execution. One private validation path
is shared by the builder and `__post_init__`, so a forged composition (reordered or cloned resolutions, foreign or mismatched binding) is rejected, while an independently
constructed canonical binding over the same sleeve and resolution objects is accepted. Non-pure by composition; no I/O, clock, randomness or hashing.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.allocation_candidate_universe import CandidateUniverseSleeveBinding, bind_candidate_universes_to_sleeves
from backend.engine.private.allocation_universe_composition import CrossAssetSleeve
from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputBundle,
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind,
)

_ERR_BUNDLE = "input_bundle must be an exact PrivateBacktestInputBundle instance"
_ERR_SLEEVES = "sleeves must be an exact tuple of exact CrossAssetSleeve instances"
_ERR_BINDING = "eligibility_binding must be an exact CandidateUniverseSleeveBinding instance"
_ERR_INCOMPLETE = "input_bundle must be COMPLETE with no missing requirement"
_ERR_SURFACE = "D2 manifest must declare only PORTFOLIO_HISTORY and CANDIDATE_UNIVERSE requirements"
_ERR_NO_UNIVERSE = "D2 requires at least one candidate-universe requirement"
_ERR_NO_SLEEVES = "D2 requires at least one explicit sleeve"
_ERR_DUPLICATE = "more than one candidate universe for the same sleeve asset class"
_ERR_ASSET_CLASSES = "candidate universes must match the sleeve asset classes exactly, one per sleeve"
_ERR_CONTEXT = "candidate universe binding must carry the very analysis context object"
_ERR_PIT = "candidate universe query must carry the very analysis PIT context object"
_ERR_DATE = "candidate universe query evaluation_date must equal the replay evaluation_date"
_ERR_SLEEVE_IDENTITY = "eligibility_binding must retain the very supplied sleeve objects"
_ERR_UNIVERSE_IDENTITY = "eligibility_binding must retain, in sleeve order, the very candidate resolution objects of the input bundle"

_D2_KINDS = frozenset({PrivateBacktestInputKind.PORTFOLIO_HISTORY, PrivateBacktestInputKind.CANDIDATE_UNIVERSE})


def _check_bundle(input_bundle: object) -> None:
    if type(input_bundle) is not PrivateBacktestInputBundle:
        raise TypeError(_ERR_BUNDLE)
    if input_bundle.status is not PrivateBacktestInputCompletenessStatus.COMPLETE or input_bundle.missing_requirements != ():
        raise ValueError(_ERR_INCOMPLETE)
    if not {requirement.kind for requirement in input_bundle.requirements} <= _D2_KINDS:
        raise ValueError(_ERR_SURFACE)
    if len(input_bundle.candidate_universes) < 1:
        raise ValueError(_ERR_NO_UNIVERSE)


def _check_sleeves(sleeves: object) -> None:
    if type(sleeves) is not tuple or not all(type(sleeve) is CrossAssetSleeve for sleeve in sleeves):
        raise TypeError(_ERR_SLEEVES)


def _aligned_resolutions(input_bundle: PrivateBacktestInputBundle, sleeves: tuple) -> tuple:
    """The bundle's existing resolution objects in sleeve AssetClass order (exactly one per sleeve), after re-proving context ownership."""
    if len(sleeves) < 1:
        raise ValueError(_ERR_NO_SLEEVES)
    context = input_bundle.analysis_context
    by_asset_class: dict = {}
    for binding in input_bundle.candidate_universes:
        query = binding.resolution.query
        if binding.analysis_context is not context:
            raise ValueError(_ERR_CONTEXT)
        if query.pit_context is not context.pit_context:
            raise ValueError(_ERR_PIT)
        if query.evaluation_date != context.replay_point.evaluation_date:
            raise ValueError(_ERR_DATE)
        if query.asset_class in by_asset_class:
            raise ValueError(_ERR_DUPLICATE)
        by_asset_class[query.asset_class] = binding.resolution
    if set(by_asset_class) != {sleeve.asset_class for sleeve in sleeves}:
        raise ValueError(_ERR_ASSET_CLASSES)
    return tuple(by_asset_class[sleeve.asset_class] for sleeve in sleeves)


def _validate(input_bundle: object, sleeves: object, eligibility_binding: object) -> None:
    """The single D2 validation path: exact types, bundle admission, asset-class alignment, then identity of the closed binding's contents."""
    if type(input_bundle) is not PrivateBacktestInputBundle:
        raise TypeError(_ERR_BUNDLE)
    _check_sleeves(sleeves)
    if type(eligibility_binding) is not CandidateUniverseSleeveBinding:
        raise TypeError(_ERR_BINDING)
    _check_bundle(input_bundle)
    expected = _aligned_resolutions(input_bundle, sleeves)
    if len(eligibility_binding.sleeves) != len(sleeves) or not all(a is b for a, b in zip(eligibility_binding.sleeves, sleeves)):
        raise ValueError(_ERR_SLEEVE_IDENTITY)
    if len(eligibility_binding.universes) != len(expected) or not all(a is b for a, b in zip(eligibility_binding.universes, expected)):
        raise ValueError(_ERR_UNIVERSE_IDENTITY)


@dataclass(frozen=True)
class PrivateBacktestCandidateEligibilityReplay:
    """The COMPLETE input bundle, the explicit fixed sleeves and the closed Phase 22B proof that every sleeve candidate is in its SELECTED historical universe."""
    input_bundle: PrivateBacktestInputBundle
    sleeves: tuple[CrossAssetSleeve, ...]
    eligibility_binding: CandidateUniverseSleeveBinding

    def __post_init__(self) -> None:
        _validate(self.input_bundle, self.sleeves, self.eligibility_binding)


def replay_private_backtest_candidate_eligibility(
    *,
    input_bundle: PrivateBacktestInputBundle,
    sleeves: tuple[CrossAssetSleeve, ...],
) -> PrivateBacktestCandidateEligibilityReplay:
    """Conditionally replay Phase 22B candidate eligibility for explicit sleeves over the bundle's historical candidate universes."""
    if type(input_bundle) is not PrivateBacktestInputBundle:
        raise TypeError(_ERR_BUNDLE)
    _check_sleeves(sleeves)
    _check_bundle(input_bundle)
    universes = _aligned_resolutions(input_bundle, sleeves)
    binding = bind_candidate_universes_to_sleeves(sleeves=sleeves, universes=universes)
    return PrivateBacktestCandidateEligibilityReplay(input_bundle=input_bundle, sleeves=sleeves, eligibility_binding=binding)
