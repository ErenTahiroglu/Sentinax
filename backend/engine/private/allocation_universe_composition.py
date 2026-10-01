"""
backend/engine/private/allocation_universe_composition.py
=========================================================
Explicit cross-asset sleeve composition and cross-universe reconciliation (Phase 22A).

Phase 21 requires the target, current state, policy and friction objects to share one canonical instrument universe. A wider
portfolio pipeline needs candidates that are not yet held, holdings that are intentionally absent from the new target and
several asset-class sleeves. This module is the deterministic same-universe bridge into Phase 21. It is composition and
reconciliation only: it does not discover the investment universe, choose investments, rank anything or decide that a holding
should be liquidated.

Authority chain (all of it explicit caller input):

    explicit strategic AssetClass sleeve weights          CrossAssetSleeve.target_weight
    + explicit within-sleeve candidate weights            CrossAssetSleeve.instrument_weights (each > 0, sum exactly 1)
    + explicit zero-current-value confirmations           CrossUniverseAuthority
    + explicit exit authorizations                        CrossUniverseAuthority
    -> canonical union -> RebalanceTargetAllocation + RebalanceCurrentState (Phase 21A types)

    final_weight_i = sleeve.target_weight * sleeve.instrument_weight_i       exact product, no normalization or repair

Reconciliation rules (exact set equality, no inference, no stale entries):

    target_only  = target candidates - current instruments    must EQUAL confirmed_zero_current_value_instrument_ids
    current_only = current instruments - target candidates    must EQUAL authorized_exit_instrument_ids

A confirmation only authorizes this composition to represent the current marked value as exact Decimal(0); it says nothing
about position history. An exit authorization only grants permission for target weight zero; it is not a sell instruction, a
tax-aware liquidation, a lot choice or an order. Retaining a holding requires listing it in a sleeve with a positive weight:
nothing is kept or sold by default. A zero within-sleeve weight is rejected so that "candidate with zero weight" and "holding
explicitly authorized for exit" cannot be blurred.

The AssetClass of a sleeve is explicit caller authority. This module does not validate that an instrument really belongs to it:
no taxonomy, no instrument type, no provider category, no symbol or name heuristic and no UUID-to-type lookup (no crypto
inference either). Eligibility and universe provenance are a later layer.

Canonical orderings (representation only):

    sleeves                       strictly ascending AssetClass.value, validated and never reordered
    instrument ids / authorities  strictly ascending UUID string, validated and never silently sorted
    reconciled universe           canonical ascending UUID-string union of an already-authorized set
    UUID ordering has no investment meaning: it is representation only, never a preference, ranking or attractiveness.

Architectural Invariants:
    - Pure domain module: standard library, `domain.AssetClass` and the Phase 21A exact helpers / types. No network, filesystem,
      database, clock, randomness, float arithmetic, numpy/scipy/pandas, persistence or Phase 21B dependency.
    - All sums and products are context-free exact coefficient arithmetic (`Decimal.as_tuple()` integers); no ambient Decimal
      context, no division, no quantization, no tolerance. The Phase 21A representation ceiling applies unchanged.
    - `CrossAssetCompositionPlan` retains the source state, the sleeves and the authority and recomputes the single canonical
      target / state pair, rejecting any forged or alternative representation. No score, rank, optimizer, tax, cost-basis or
      execution semantics exist here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.allocation_rebalance import (
    _ERR_STATE_TYPE,
    RebalanceCurrentState,
    RebalanceTargetAllocation,
    _exact_add,
    _exact_mul,
    _validate_ids,
)
from backend.engine.private.domain import AssetClass

_ERR_ASSET_CLASS = "asset_class must be an exact AssetClass instance"
_ERR_WEIGHT_TYPE = "weights must be exact Decimal instances"
_ERR_SLEEVE_WEIGHT = "sleeve target_weight must be a finite unsigned Decimal greater than zero"
_ERR_INSTRUMENT_WEIGHTS_TYPE = "instrument_weights must be a tuple of exact Decimal instances"
_ERR_INSTRUMENT_WEIGHTS_SHAPE = "instrument_weights must match instrument_ids exactly"
_ERR_INSTRUMENT_WEIGHTS_RANGE = "instrument_weights must be finite unsigned Decimals with 0 < weight <= 1"
_ERR_INSTRUMENT_WEIGHTS_SUM = "instrument_weights must sum to exactly 1"
_ERR_AUTHORITY_TYPE = "authority must be an exact CrossUniverseAuthority instance"
_ERR_AUTHORITY_IDS = "authority instrument ids must be a tuple of exact UUID instances"
_ERR_AUTHORITY_DISJOINT = "zero-current confirmations and exit authorizations must be disjoint"
_ERR_SLEEVE_TYPE = "sleeves must be a non-empty tuple of exact CrossAssetSleeve instances"
_ERR_SLEEVE_SEQUENCE = "sleeves must have unique asset classes in canonical ascending AssetClass value sequence"
_ERR_SLEEVE_OVERLAP = "an instrument may appear in exactly one sleeve"
_ERR_SLEEVE_SUM = "sleeve target weights must sum to exactly 1"
_ERR_ZERO_CONFIRMATION = "zero-current confirmations must equal exactly the target-only instruments"
_ERR_EXIT_AUTHORIZATION = "exit authorizations must equal exactly the current-only instruments"
_ERR_PLAN_TYPE = "plan fields must be exact types"
_ERR_MATCH = "composition plan must match the canonical derived target and state exactly"
_ERR_INCONSISTENT = "derived composition weights must be unsigned and sum to exactly 1"

_ZERO = Decimal(0)
_ONE = Decimal(1)


def _check_exact_decimals(values: object, type_error: str) -> None:
    if type(values) is not tuple or any(type(v) is not Decimal for v in values):
        raise TypeError(type_error)


def _validate_authority_ids(ids: object) -> None:
    if type(ids) is not tuple or any(type(i) is not UUID for i in ids):
        raise TypeError(_ERR_AUTHORITY_IDS)
    if ids:
        _validate_ids(ids)


@dataclass(frozen=True)
class CrossAssetSleeve:
    """One explicit strategic AssetClass sleeve: its portfolio weight and explicit conditional candidate weights."""
    asset_class: AssetClass
    target_weight: Decimal
    instrument_ids: tuple[UUID, ...]
    instrument_weights: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if type(self.asset_class) is not AssetClass:
            raise TypeError(_ERR_ASSET_CLASS)
        if type(self.target_weight) is not Decimal:
            raise TypeError(_ERR_WEIGHT_TYPE)
        if not self.target_weight.is_finite() or self.target_weight.is_signed() or self.target_weight == 0:
            raise ValueError(_ERR_SLEEVE_WEIGHT)
        _validate_ids(self.instrument_ids)
        _check_exact_decimals(self.instrument_weights, _ERR_INSTRUMENT_WEIGHTS_TYPE)
        if len(self.instrument_weights) != len(self.instrument_ids):
            raise ValueError(_ERR_INSTRUMENT_WEIGHTS_SHAPE)
        if any(not w.is_finite() or w.is_signed() or w == 0 or w > _ONE for w in self.instrument_weights):
            raise ValueError(_ERR_INSTRUMENT_WEIGHTS_RANGE)
        if _exact_add(self.instrument_weights) != _ONE:
            raise ValueError(_ERR_INSTRUMENT_WEIGHTS_SUM)


@dataclass(frozen=True)
class CrossUniverseAuthority:
    """Explicit zero-current-value confirmations (target-only) and exit authorizations (current-only); no defaults."""
    confirmed_zero_current_value_instrument_ids: tuple[UUID, ...]
    authorized_exit_instrument_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _validate_authority_ids(self.confirmed_zero_current_value_instrument_ids)
        _validate_authority_ids(self.authorized_exit_instrument_ids)
        if set(self.confirmed_zero_current_value_instrument_ids) & set(self.authorized_exit_instrument_ids):
            raise ValueError(_ERR_AUTHORITY_DISJOINT)


def _validate_sleeves(sleeves: object) -> None:
    if type(sleeves) is not tuple or not sleeves or any(type(s) is not CrossAssetSleeve for s in sleeves):
        raise TypeError(_ERR_SLEEVE_TYPE)
    keys = [s.asset_class.value for s in sleeves]
    if any(left >= right for left, right in zip(keys, keys[1:])):
        raise ValueError(_ERR_SLEEVE_SEQUENCE)
    seen = [instrument for s in sleeves for instrument in s.instrument_ids]
    if len(set(seen)) != len(seen):
        raise ValueError(_ERR_SLEEVE_OVERLAP)
    if _exact_add([s.target_weight for s in sleeves]) != _ONE:
        raise ValueError(_ERR_SLEEVE_SUM)


def _by_uuid_string(ids: set[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted(ids, key=str))


def _derive(
    current_state: RebalanceCurrentState,
    sleeves: tuple[CrossAssetSleeve, ...],
    authority: CrossUniverseAuthority,
) -> tuple[RebalanceTargetAllocation, RebalanceCurrentState]:
    """Single canonical derivation shared by the builder and the constructor verification."""
    _validate_sleeves(sleeves)
    candidate_weights = {
        instrument: _exact_mul(sleeve.target_weight, weight)
        for sleeve in sleeves
        for instrument, weight in zip(sleeve.instrument_ids, sleeve.instrument_weights)
    }
    target_ids = set(candidate_weights)
    current_ids = set(current_state.instrument_ids)
    if authority.confirmed_zero_current_value_instrument_ids != _by_uuid_string(target_ids - current_ids):
        raise ValueError(_ERR_ZERO_CONFIRMATION)
    if authority.authorized_exit_instrument_ids != _by_uuid_string(current_ids - target_ids):
        raise ValueError(_ERR_EXIT_AUTHORIZATION)
    reconciled = _by_uuid_string(target_ids | current_ids)
    weights = tuple(candidate_weights.get(instrument, _ZERO) for instrument in reconciled)
    if any(w.is_signed() or w > _ONE for w in weights) or _exact_add(weights) != _ONE:
        raise ValueError(_ERR_INCONSISTENT)
    held = dict(zip(current_state.instrument_ids, current_state.current_values))
    values = tuple(held.get(instrument, _ZERO) for instrument in reconciled)
    target = RebalanceTargetAllocation(instrument_ids=reconciled, weights=weights)
    state = RebalanceCurrentState(
        instrument_ids=reconciled,
        current_values=values,
        investable_cash=current_state.investable_cash,
        currency=current_state.currency,
    )
    return (target, state)


def _same_decimals(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> bool:
    return len(left) == len(right) and all(a.as_tuple() == b.as_tuple() for a, b in zip(left, right))


@dataclass(frozen=True)
class CrossAssetCompositionPlan:
    """Canonical same-universe Phase 21 target / state pair derived from explicit sleeves and authority."""
    current_state: RebalanceCurrentState
    sleeves: tuple[CrossAssetSleeve, ...]
    authority: CrossUniverseAuthority
    rebalance_target: RebalanceTargetAllocation
    rebalance_state: RebalanceCurrentState

    def __post_init__(self) -> None:
        if type(self.current_state) is not RebalanceCurrentState:
            raise TypeError(_ERR_STATE_TYPE)
        if type(self.authority) is not CrossUniverseAuthority:
            raise TypeError(_ERR_AUTHORITY_TYPE)
        if type(self.rebalance_target) is not RebalanceTargetAllocation or type(self.rebalance_state) is not RebalanceCurrentState:
            raise TypeError(_ERR_PLAN_TYPE)
        target, state = _derive(self.current_state, self.sleeves, self.authority)
        if (
            self.rebalance_target.instrument_ids != target.instrument_ids
            or not _same_decimals(self.rebalance_target.weights, target.weights)
            or self.rebalance_state.instrument_ids != state.instrument_ids
            or not _same_decimals(self.rebalance_state.current_values, state.current_values)
            or self.rebalance_state.investable_cash.as_tuple() != state.investable_cash.as_tuple()
            or self.rebalance_state.currency is not state.currency
        ):
            raise ValueError(_ERR_MATCH)

    @property
    def target_candidate_instrument_ids(self) -> tuple[UUID, ...]:
        return _by_uuid_string({instrument for sleeve in self.sleeves for instrument in sleeve.instrument_ids})

    @property
    def current_only_exit_instrument_ids(self) -> tuple[UUID, ...]:
        return self.authority.authorized_exit_instrument_ids

    @property
    def target_only_zero_confirmed_instrument_ids(self) -> tuple[UUID, ...]:
        return self.authority.confirmed_zero_current_value_instrument_ids

    @property
    def reconciled_instrument_ids(self) -> tuple[UUID, ...]:
        return self.rebalance_target.instrument_ids

    @property
    def asset_class_target_weights(self) -> tuple[tuple[AssetClass, Decimal], ...]:
        return tuple((sleeve.asset_class, sleeve.target_weight) for sleeve in self.sleeves)


def build_cross_asset_composition_plan(
    *,
    current_state: RebalanceCurrentState,
    sleeves: tuple[CrossAssetSleeve, ...],
    authority: CrossUniverseAuthority,
) -> CrossAssetCompositionPlan:
    """The canonical composition plan; every input is explicit and nothing is inferred."""
    if type(current_state) is not RebalanceCurrentState:
        raise TypeError(_ERR_STATE_TYPE)
    if type(authority) is not CrossUniverseAuthority:
        raise TypeError(_ERR_AUTHORITY_TYPE)
    _validate_sleeves(sleeves)
    target, state = _derive(current_state, sleeves, authority)
    return CrossAssetCompositionPlan(
        current_state=current_state, sleeves=sleeves, authority=authority, rebalance_target=target, rebalance_state=state,
    )
