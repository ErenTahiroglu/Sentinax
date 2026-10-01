"""
backend/engine/private/allocation_hrp.py
========================================
Hierarchical Risk Parity (HRP) benchmark over the Phase 18A sample covariance matrix (Phase 18D).

One canonical, non-configurable methodology (no linkage / distance / split options):

    covariance -> correlation -> correlation distance -> distance-of-distance -> single-linkage clustering
    -> quasi-diagonal leaf order -> recursive bisection -> cluster inverse-VARIANCE allocation -> exact weights.

Methodology:
    - Correlation: rho_ij = Sigma_ij / sqrt(Sigma_ii * Sigma_jj) (one calculation per pair, so rho_ij == rho_ji);
      rho_ii = 1 exactly. Every off-diagonal value must lie in [-1, 1]; it is never clipped, capped or made absolute.
    - Correlation distance: d_ij = sqrt((1 - rho_ij) / 2), d_ii = 0.
    - Distance-of-distance (the clustering input): D_ij = sqrt(sum_k (d_ki - d_kj)^2). No normalization, no Ward
      transform.
    - Single linkage, bottom-up. Leaves are 0 .. N-1 (canonical instrument order); merged clusters get ids N, N+1, ...
      in merge order. distance(A, B) = min D_ij over i in A, j in B (exact recurrence min(d(A,X), d(B,X))). At every
      step the pair is chosen by the exact key (linkage distance, smaller cluster id, larger cluster id), never by dict,
      set or hash order. The smaller id is the LEFT child, the larger the RIGHT child; orientation is never changed.
    - Quasi-diagonal order: depth-first left subtree then right subtree of the final merge. No post-sort.
    - Recursive bisection of that ORDER (not of the dendrogram branch sizes): split = len // 2, left = group[:split],
      right = group[split:] (the left half is the smaller one for odd lengths). Complete levels are processed
      left to right.
    - Cluster allocation uses inverse VARIANCE: raw_i = 1 / Sigma_ii (NOT the Phase 18B inverse VOLATILITY
      1 / sqrt(Sigma_ii)), q_i = raw_i / sum(raw). Cluster variance V = q' Sigma_C q uses the full covariance inside the
      cluster. Split factor alpha = V_right / (V_left + V_right); left weights *= alpha, right weights *= 1 - alpha.

Unavailability (never repaired, never fabricated):
    - ZERO_VARIANCE: any Sigma_ii == 0 (correlation is undefined). No epsilon, dropped asset or fallback.
    - INVALID_CORRELATION_GEOMETRY: a covariance-derived correlation lies outside [-1, 1].
    - DEGENERATE_CLUSTER_VARIANCE: a child cluster used in a split has variance <= 0 (e.g. a perfectly anti-correlated
      equal-variance pair), so no strictly positive allocation exists. No 0/100 split and no epsilon variance.
    Singular (rank-deficient) covariance is NOT a failure by itself: no determinant, matrix inverse, positive
    definiteness or T > N requirement exists. A genuine Decimal analytical overflow / exact-closure resource failure is
    a distinct static ValueError, never one of the availability reasons.

Exact stored-weight closure:
    - Preliminary weights come from the 50-digit analytics context and are closed to an exact Decimal sum of 1 by the
      reviewed Phase 18B context-free coefficient closure (`_close_to_one`, `_sums_to_exactly_one`). This is
      intentional package-internal reuse; the algorithm is not duplicated and Phase 18B is unchanged.

Architectural Invariants:
    - Pure domain module: standard library plus `allocation_matrix` and `allocation_benchmarks` only. No network,
      filesystem, database, ambient clock, randomness, float arithmetic, numpy/pandas/scipy, clustering library,
      persistence, provider or resolver calls. No matrix inversion, no expected return, no ERC reuse.
    - Each canonical build creates ONE fresh 50-digit ROUND_HALF_EVEN context with maximum exponent range and passes it
      explicitly; the ambient context is never read or mutated and no module-global mutable Context exists.
    - `HierarchicalRiskParityResult` retains its source by identity; weights[i] corresponds to source.instrument_ids[i]
      (the internal quasi-diagonal order is mapped back to canonical order). It recomputes the complete canonical
      result through the same private helper as the builder, rejecting forged values.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_benchmarks import _close_to_one, _sums_to_exactly_one
from backend.engine.private.allocation_matrix import AllocationCovarianceMatrix

_ERR_COVARIANCE_TYPE = "covariance must be an exact AllocationCovarianceMatrix instance"
_ERR_SOURCE_TYPE = "source must be an exact AllocationCovarianceMatrix instance"
_ERR_WEIGHTS_TYPE = "weights must be None or a tuple of exact Decimal instances"
_ERR_REASON_TYPE = "unavailable_reason must be None or an exact HierarchicalRiskParityUnavailableReason instance"
_ERR_PAIRING = "weights and unavailable_reason must be exactly one available weights tuple or one unavailable reason"
_ERR_SHAPE = "weights must match the covariance dimension exactly"
_ERR_WEIGHT_RANGE = "weights must be finite Decimals with 0 < weight <= 1"
_ERR_WEIGHT_SUM = "weights must sum to exactly 1"
_ERR_MATCH = "hierarchical risk parity result must match the canonical build exactly"
_ERR_RANGE = "hierarchical risk parity exceeds supported Decimal analytics range"


class HierarchicalRiskParityUnavailableReason(Enum):
    ZERO_VARIANCE = "zero_variance"
    INVALID_CORRELATION_GEOMETRY = "invalid_correlation_geometry"
    DEGENERATE_CLUSTER_VARIANCE = "degenerate_cluster_variance"


_Reason = HierarchicalRiskParityUnavailableReason
_Matrix = list[list[Decimal]]
_Merge = tuple[int, int, Decimal, int]  # (left cluster id, right cluster id, linkage distance, cluster size)


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _correlation(rows: tuple[tuple[Decimal, ...], ...], ctx: decimal.Context) -> _Matrix | None:
    """rho_ij = Sigma_ij / sqrt(Sigma_ii Sigma_jj), one calculation per pair, diagonal exactly 1; None if outside [-1, 1]."""
    size = len(rows)
    matrix: _Matrix = [[Decimal(1) if i == j else Decimal(0) for j in range(size)] for i in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            scale = ctx.sqrt(ctx.multiply(rows[i][i], rows[j][j]))
            rho = ctx.divide(rows[i][j], scale)
            if rho < Decimal(-1) or rho > Decimal(1):
                return None
            matrix[i][j] = rho
            matrix[j][i] = rho
    return matrix


def _correlation_distance(correlation: _Matrix, ctx: decimal.Context) -> _Matrix:
    """d_ij = sqrt((1 - rho_ij) / 2); d_ii = 0."""
    size = len(correlation)
    matrix: _Matrix = [[Decimal(0)] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            value = ctx.sqrt(ctx.divide(ctx.subtract(Decimal(1), correlation[i][j]), Decimal(2)))
            matrix[i][j] = value
            matrix[j][i] = value
    return matrix


def _distance_of_distance(distance: _Matrix, ctx: decimal.Context) -> _Matrix:
    """D_ij = sqrt(sum_k (d_ki - d_kj)^2): Euclidean distance between correlation-distance profiles."""
    size = len(distance)
    matrix: _Matrix = [[Decimal(0)] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            total = Decimal(0)
            for k in range(size):
                difference = ctx.subtract(distance[k][i], distance[k][j])
                total = ctx.add(total, ctx.multiply(difference, difference))
            value = ctx.sqrt(total)
            matrix[i][j] = value
            matrix[j][i] = value
    return matrix


def _single_linkage(distances: _Matrix) -> list[_Merge]:
    """Deterministic bottom-up single linkage; ties resolved by (distance, smaller id, larger id)."""
    size = len(distances)
    pair: dict[tuple[int, int], Decimal] = {}
    for i in range(size):
        for j in range(i + 1, size):
            pair[(i, j)] = distances[i][j]
    sizes = {i: 1 for i in range(size)}
    active = list(range(size))  # always ascending: merged ids are larger than every existing id
    merges: list[_Merge] = []
    next_id = size
    while len(active) > 1:
        best: tuple[Decimal, int, int] | None = None
        for position, first in enumerate(active):
            for second in active[position + 1:]:
                candidate = (pair[(first, second)], first, second)
                if best is None or candidate < best:
                    best = candidate
        assert best is not None
        distance, left, right = best
        for other in active:
            if other != left and other != right:
                from_left = pair[(min(other, left), max(other, left))]
                from_right = pair[(min(other, right), max(other, right))]
                pair[(other, next_id)] = from_left if from_left <= from_right else from_right
        sizes[next_id] = sizes[left] + sizes[right]
        merges.append((left, right, distance, sizes[next_id]))
        active = [other for other in active if other != left and other != right] + [next_id]
        next_id += 1
    return merges


def _quasi_diagonal_order(merges: list[_Merge], size: int) -> tuple[int, ...]:
    """Leaf order of the final cluster: left subtree then right subtree, no post-sort."""
    if size == 1:
        return (0,)
    children = {size + index: (merge[0], merge[1]) for index, merge in enumerate(merges)}
    order: list[int] = []
    stack = [size + len(merges) - 1]
    while stack:
        node = stack.pop()
        if node < size:
            order.append(node)
        else:
            left, right = children[node]
            stack.append(right)
            stack.append(left)
    return tuple(order)


def _bisection_split(group: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    split = len(group) // 2
    return (group[:split], group[split:])


def _inverse_variance_weights(
    rows: tuple[tuple[Decimal, ...], ...],
    members: tuple[int, ...],
    ctx: decimal.Context,
) -> list[Decimal]:
    """Cluster weights q_i = (1 / Sigma_ii) / sum(1 / Sigma_jj): inverse VARIANCE, not inverse volatility."""
    raw = [ctx.divide(Decimal(1), rows[i][i]) for i in members]
    total = Decimal(0)
    for value in raw:
        total = ctx.add(total, value)
    return [ctx.divide(value, total) for value in raw]


def _cluster_variance(
    rows: tuple[tuple[Decimal, ...], ...],
    members: tuple[int, ...],
    ctx: decimal.Context,
) -> Decimal:
    """q' Sigma_C q with the FULL covariance inside the cluster."""
    weights = _inverse_variance_weights(rows, members, ctx)
    variance = Decimal(0)
    for a, i in enumerate(members):
        for b, j in enumerate(members):
            variance = ctx.add(variance, ctx.multiply(ctx.multiply(weights[a], rows[i][j]), weights[b]))
    return variance


def _recursive_bisection(
    rows: tuple[tuple[Decimal, ...], ...],
    order: tuple[int, ...],
    ctx: decimal.Context,
) -> list[Decimal] | None:
    """Preliminary weights in canonical index order, or None if a child cluster variance is not positive."""
    weights = [Decimal(1) for _ in rows]
    groups: list[tuple[int, ...]] = [order]
    while groups:
        next_groups: list[tuple[int, ...]] = []
        for group in groups:  # complete level, left to right
            if len(group) < 2:
                continue
            left, right = _bisection_split(group)
            left_variance = _cluster_variance(rows, left, ctx)
            right_variance = _cluster_variance(rows, right, ctx)
            if left_variance <= 0 or right_variance <= 0:
                return None
            alpha = ctx.divide(right_variance, ctx.add(left_variance, right_variance))
            if not (alpha > 0 and alpha < 1):
                raise ValueError(_ERR_RANGE)
            complement = ctx.subtract(Decimal(1), alpha)
            for index in left:
                weights[index] = ctx.multiply(weights[index], alpha)
            for index in right:
                weights[index] = ctx.multiply(weights[index], complement)
            next_groups.extend(part for part in (left, right) if len(part) > 1)
        groups = next_groups
    return weights


def _structure(source: AllocationCovarianceMatrix, ctx: decimal.Context) -> tuple[tuple[int, ...], list[_Merge]] | _Reason:
    """Correlation -> distance -> distance-of-distance -> linkage -> quasi-diagonal order."""
    rows = source.covariance
    size = source.dimension
    if any(rows[i][i] == 0 for i in range(size)):
        return _Reason.ZERO_VARIANCE
    if size == 1:
        return ((0,), [])
    correlation = _correlation(rows, ctx)
    if correlation is None:
        return _Reason.INVALID_CORRELATION_GEOMETRY
    merges = _single_linkage(_distance_of_distance(_correlation_distance(correlation, ctx), ctx))
    return (_quasi_diagonal_order(merges, size), merges)


def _canonical_hrp(
    source: AllocationCovarianceMatrix,
) -> tuple[tuple[Decimal, ...], None] | tuple[None, HierarchicalRiskParityUnavailableReason]:
    """Single canonical HRP build shared by the builder and the constructor verification."""
    ctx = _analytics_context()
    try:
        structure = _structure(source, ctx)
        if isinstance(structure, _Reason):
            return (None, structure)
        order, _ = structure
        preliminary = _recursive_bisection(source.covariance, order, ctx)
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    if preliminary is None:
        return (None, _Reason.DEGENERATE_CLUSTER_VARIANCE)
    try:
        return (_close_to_one(preliminary), None)
    except ValueError:
        raise ValueError(_ERR_RANGE) from None


@dataclass(frozen=True)
class HierarchicalRiskParityResult:
    """Long-only HRP weights (aligned to source.instrument_ids) or an explicit unavailability reason."""
    source: AllocationCovarianceMatrix
    weights: tuple[Decimal, ...] | None
    unavailable_reason: HierarchicalRiskParityUnavailableReason | None

    def __post_init__(self) -> None:
        if type(self.source) is not AllocationCovarianceMatrix:
            raise TypeError(_ERR_SOURCE_TYPE)
        if self.weights is not None and (
            type(self.weights) is not tuple or any(type(w) is not Decimal for w in self.weights)
        ):
            raise TypeError(_ERR_WEIGHTS_TYPE)
        if self.unavailable_reason is not None and type(self.unavailable_reason) is not _Reason:
            raise TypeError(_ERR_REASON_TYPE)
        if (self.weights is None) == (self.unavailable_reason is None):
            raise ValueError(_ERR_PAIRING)
        if self.weights is not None:
            if len(self.weights) != self.source.dimension:
                raise ValueError(_ERR_SHAPE)
            if any(not w.is_finite() or not (w > Decimal(0) and w <= Decimal(1)) for w in self.weights):
                raise ValueError(_ERR_WEIGHT_RANGE)
            if not _sums_to_exactly_one(self.weights):
                raise ValueError(_ERR_WEIGHT_SUM)
        if (self.weights, self.unavailable_reason) != _canonical_hrp(self.source):
            raise ValueError(_ERR_MATCH)

    @property
    def instrument_ids(self) -> tuple[UUID, ...]:
        return self.source.instrument_ids

    @property
    def dimension(self) -> int:
        return self.source.dimension

    @property
    def is_available(self) -> bool:
        return self.weights is not None

    @property
    def quasi_diagonal_order(self) -> tuple[int, ...] | None:
        """Canonical instrument indices in quasi-diagonal leaf order (derived audit view; None if unavailable)."""
        if self.weights is None:
            return None
        try:
            structure = _structure(self.source, _analytics_context())
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None
        return None if isinstance(structure, _Reason) else structure[0]

    @property
    def ordered_instrument_ids(self) -> tuple[UUID, ...] | None:
        order = self.quasi_diagonal_order
        if order is None:
            return None
        ids = self.source.instrument_ids
        return tuple(ids[index] for index in order)


def build_hierarchical_risk_parity_benchmark(
    *,
    covariance: AllocationCovarianceMatrix,
) -> HierarchicalRiskParityResult:
    """The single canonical Hierarchical Risk Parity benchmark from a Phase 18A covariance matrix."""
    if type(covariance) is not AllocationCovarianceMatrix:
        raise TypeError(_ERR_COVARIANCE_TYPE)
    weights, reason = _canonical_hrp(covariance)
    return HierarchicalRiskParityResult(source=covariance, weights=weights, unavailable_reason=reason)
