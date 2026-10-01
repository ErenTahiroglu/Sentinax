"""
backend/tests/test_allocation_user_views.py
===========================================
Phase 20A: base expected-return prior, absolute / relative user views, and the explicit confidence -> view-noise mapping.

Black-Litterman-LIKE Bayesian foundation only: no posterior, no tau, no market-equilibrium inference, no allocation.
The prior is an explicit caller input; its uncertainty U is NOT the historical return covariance.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import itertools
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_user_views as module_under_test
from backend.engine.private.allocation_matrix import (
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    AllocationReturnPanel,
    build_allocation_return_panel,
)
from backend.engine.private.allocation_user_views import (
    ExpectedReturnPrior,
    UserReturnView,
    UserReturnViewKind,
    UserReturnViewSet,
    build_user_return_view_set,
)
from backend.tests.invariants import static_guards as sg

D = Decimal
UUIDS = [UUID(int=i) for i in range(1, 9)]
KIND = UserReturnViewKind


def _series(instrument_id: UUID, values: list[str]) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, i + 1), simple_return=D(v)) for i, v in enumerate(values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _panel(count: int = 3) -> AllocationReturnPanel:
    columns = [["0.01", "0.02"], ["0.00", "0.01"], ["-0.01", "0.03"], ["0.02", "-0.02"]][:count]
    return build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(columns)))


def _rows(matrix) -> tuple[tuple[Decimal, ...], ...]:
    return tuple(tuple(D(v) for v in row) for row in matrix)


DIAG = [["4", "0", "0"], ["0", "9", "0"], ["0", "0", "1"]]
CORR = [["4", "1", "0.5"], ["1", "9", "-0.5"], ["0.5", "-0.5", "1"]]
M3 = tuple(D(v) for v in ("0.05", "0.03", "0.04"))


def _prior(matrix=DIAG, returns=M3, panel=None) -> ExpectedReturnPrior:
    return ExpectedReturnPrior(source=panel or _panel(len(matrix)), expected_returns=tuple(returns), uncertainty_covariance=_rows(matrix))


def _view(kind, loadings, target="0.03", confidence="0.75") -> UserReturnView:
    return UserReturnView(kind=kind, loadings=tuple(D(v) for v in loadings), target_return=D(target), confidence=D(confidence))


def _set(views, matrix=DIAG) -> UserReturnViewSet:
    return build_user_return_view_set(prior=_prior(matrix), views=tuple(views))


def _quadratic(matrix, loadings) -> Fraction:
    return sum(
        (Fraction(D(loadings[i])) * Fraction(D(matrix[i][j])) * Fraction(D(loadings[j])) for i in range(len(loadings)) for j in range(len(loadings))),
        Fraction(0),
    )


# --- prior: contract and lineage ---------------------------------------------------------------------------------

def test_prior_has_exactly_three_stored_fields_retains_the_panel_and_is_frozen() -> None:
    panel = _panel(3)
    prior = _prior(panel=panel)
    assert [f.name for f in dataclasses.fields(ExpectedReturnPrior)] == ["source", "expected_returns", "uncertainty_covariance"]
    assert prior.source is panel and prior.instrument_ids == panel.instrument_ids and prior.dimension == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        prior.expected_returns = ()  # type: ignore[misc]


def test_prior_requires_the_exact_panel_type() -> None:
    panel = _panel(3)

    class _Sub(AllocationReturnPanel):
        pass

    sub = object.__new__(_Sub)
    object.__setattr__(sub, "series", panel.series)
    for bad in (None, object(), panel.series, sub):
        with pytest.raises(TypeError):
            ExpectedReturnPrior(source=bad, expected_returns=M3, uncertainty_covariance=_rows(DIAG))  # type: ignore[arg-type]


def test_expected_returns_contract() -> None:
    panel = _panel(3)

    class _DecSub(Decimal):
        pass

    def make(returns):
        return ExpectedReturnPrior(source=panel, expected_returns=returns, uncertainty_covariance=_rows(DIAG))

    for bad in ([D("0.1")] * 3, (0.1, 0.1, 0.1), (1, 0, 0), (True, False, True), ("0.1",) * 3, (_DecSub("0.1"), D(0), D(0)), None):
        with pytest.raises(TypeError):
            make(bad)
    for bad in ("NaN", "sNaN", "Infinity", "-Infinity", "-1.0000001", "-2"):
        with pytest.raises(ValueError):
            make((D(bad), D(0), D(0)))
    for wrong in ((D(0),) * 2, (D(0),) * 4, ()):
        with pytest.raises(ValueError):
            make(wrong)
    assert make((D("-1"), D("0"), D("250"))).expected_returns[2] == 250  # support is r >= -1, no upper cap and no clamping


def test_prior_does_not_derive_anything_from_the_panel_returns() -> None:
    a = ExpectedReturnPrior(source=_panel(3), expected_returns=M3, uncertainty_covariance=_rows(DIAG))
    other = build_allocation_return_panel(series=tuple(_series(UUIDS[i], ["0.5", "-0.5"]) for i in range(3)))
    b = ExpectedReturnPrior(source=other, expected_returns=M3, uncertainty_covariance=_rows(DIAG))
    assert a.expected_returns == b.expected_returns == M3  # the panel supplies identity / order / dimension only


# --- prior: uncertainty matrix -----------------------------------------------------------------------------------

def test_uncertainty_matrix_shape_and_types() -> None:
    panel = _panel(2)
    good = _rows([["1", "0"], ["0", "4"]])

    class _DecSub(Decimal):
        pass

    def make(matrix):
        return ExpectedReturnPrior(source=panel, expected_returns=(D(0), D(0)), uncertainty_covariance=matrix)

    assert make(good).uncertainty_covariance == good
    for bad in ([list(r) for r in good], (list(good[0]), good[1]), [good[0], good[1]], None, 1):
        with pytest.raises(TypeError):
            make(bad)
    for bad in (((1, 0), (0, 4)), ((0.5, 0), (0, 4)), (("1", "0"), ("0", "4")), ((_DecSub(1), D(0)), (D(0), D(4)))):
        with pytest.raises(TypeError):
            make(bad)
    for bad in (good[:1], good + (good[0],), (good[0][:1], good[1]), (good[0] + (D(0),), good[1] + (D(0),))):
        with pytest.raises(ValueError):
            make(bad)
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            make(((D("1"), D(bad)), (D(bad), D("4"))))


def test_uncertainty_matrix_must_be_exactly_symmetric_and_is_never_symmetrized() -> None:
    panel = _panel(2)
    with pytest.raises(ValueError):
        ExpectedReturnPrior(source=panel, expected_returns=(D(0), D(0)), uncertainty_covariance=_rows([["4", "1"], ["1.0000000000000001", "4"]]))
    ok = ExpectedReturnPrior(source=panel, expected_returns=(D(0), D(0)), uncertainty_covariance=_rows([["4", "1"], ["1.00", "4"]]))
    assert ok.uncertainty_covariance[0][1] == ok.uncertainty_covariance[1][0]


@pytest.mark.parametrize("matrix", [
    [["1", "0"], ["0", "4"]],            # diagonal
    [["1", "0.5"], ["0.5", "1"]],        # correlated, positive definite
    [["2", "-1"], ["-1", "2"]],          # negatively correlated, positive definite
])
def test_positive_definite_fixtures_are_accepted(matrix) -> None:
    assert ExpectedReturnPrior(source=_panel(2), expected_returns=(D(0), D(0)), uncertainty_covariance=_rows(matrix)) is not None


@pytest.mark.parametrize("matrix", [
    [["1", "1"], ["1", "1"]],            # singular (positive semidefinite)
    [["1", "2"], ["2", "1"]],            # indefinite
    [["0", "0"], ["0", "1"]],            # zero diagonal
    [["-1", "0"], ["0", "1"]],           # negative diagonal
    [["1", "0"], ["0", "0"]],            # zero pivot
])
def test_non_positive_definite_matrices_are_rejected_without_repair(matrix) -> None:
    with pytest.raises(ValueError, match="strictly positive definite"):
        ExpectedReturnPrior(source=_panel(2), expected_returns=(D(0), D(0)), uncertainty_covariance=_rows(matrix))


def test_three_by_three_positive_definite_and_indefinite() -> None:
    assert _prior(CORR) is not None
    with pytest.raises(ValueError, match="strictly positive definite"):
        _prior([["1", "0.9", "0.9"], ["0.9", "1", "-0.9"], ["0.9", "-0.9", "1"]])  # pivot 3 negative


def test_positive_definiteness_is_a_prior_rule_only_a_singular_return_panel_is_unaffected() -> None:
    singular_panel = build_allocation_return_panel(series=tuple(_series(UUIDS[i], ["0", str(i + 1)]) for i in range(3)))
    assert _prior(panel=singular_panel) is not None  # rank-deficient historical returns do not matter to the prior


# --- view kind / absolute ----------------------------------------------------------------------------------------

def test_view_kind_enum_is_exactly_absolute_and_relative() -> None:
    assert issubclass(UserReturnViewKind, Enum)
    assert {m.name: m.value for m in UserReturnViewKind} == {"ABSOLUTE": "absolute", "RELATIVE": "relative"}


def test_view_has_exactly_four_stored_fields_and_is_frozen() -> None:
    view = _view(KIND.ABSOLUTE, ("0", "1", "0"))
    assert [f.name for f in dataclasses.fields(UserReturnView)] == ["kind", "loadings", "target_return", "confidence"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.confidence = D(1)  # type: ignore[misc]


def test_absolute_view_canonical_one_asset_loading() -> None:
    view = _view(KIND.ABSOLUTE, ("0", "1", "0"), "0.03", "0.75")
    assert view.loadings == (D(0), D(1), D(0)) and view.target_return == D("0.03") and view.confidence == D("0.75")
    assert _view(KIND.ABSOLUTE, ("1.00", "0", "0")).loadings[0] == 1  # numerically exactly 1
    for bad in (("0", "2", "0"), ("0", "0.5", "0"), ("0", "-1", "0"), ("1", "1", "0"), ("0", "1", "-1"), ("0", "0", "0")):
        with pytest.raises(ValueError):
            _view(KIND.ABSOLUTE, bad)


def test_absolute_target_support_is_simple_return_minus_one_with_no_upper_cap() -> None:
    assert _view(KIND.ABSOLUTE, ("0", "1", "0"), "-1").target_return == -1
    assert _view(KIND.ABSOLUTE, ("0", "1", "0"), "7.5").target_return == D("7.5")
    for bad in ("-1.0000001", "-2"):
        with pytest.raises(ValueError):
            _view(KIND.ABSOLUTE, ("0", "1", "0"), bad)


# --- relative / group --------------------------------------------------------------------------------------------

def test_relative_pair_and_weighted_group_views() -> None:
    pair = _view(KIND.RELATIVE, ("1", "-1", "0"), "0.02", "0.5")
    group = _view(KIND.RELATIVE, ("0.5", "0.5", "-1"), "0.01", "0.5")
    assert sum(pair.loadings, D(0)) == 0 and sum(group.loadings, D(0)) == 0
    assert sum(x for x in group.loadings if x > 0) == 1 and sum(x for x in group.loadings if x < 0) == -1
    assert _view(KIND.RELATIVE, ("0.25", "0.75", "-0.4", "-0.6")).loadings[1] == D("0.75")  # multi-leg on both sides


def test_relative_view_exact_leg_sums_are_context_free() -> None:
    third = D("0.3333333333333333333333333333333333333333333333333")
    last = D("0.3333333333333333333333333333333333333333333333334")
    with decimal.localcontext(decimal.Context(prec=2, rounding=decimal.ROUND_DOWN)):
        view = UserReturnView(kind=KIND.RELATIVE, loadings=(third, third, last, D("-1")), target_return=D("0.01"), confidence=D("0.5"))
        assert view.loadings[2] == last
        with pytest.raises(ValueError):
            UserReturnView(kind=KIND.RELATIVE, loadings=(third, third, third, D("-1")), target_return=D("0.01"), confidence=D("0.5"))


@pytest.mark.parametrize("loadings", [
    ("1", "0", "0"),            # a single nonzero loading is not relative
    ("0.5", "0.5", "0"),        # positive leg only
    ("-0.5", "-0.5", "0"),      # negative leg only (also fails the orientation rule)
    ("2", "-1", "0"),           # positive leg sums to 2
    ("1", "-2", "0"),           # negative leg sums to -2
    ("0.9", "-1", "0"),         # positive leg 0.9 != 1
    ("-1", "1", "0"),           # first nonzero loading negative: sign-flipped duplicate identity
    ("0", "-1", "1"),           # first nonzero loading negative
    ("1", "-0.5", "-0.4"),      # negative leg -0.9 != -1
    ("0", "0", "0"),            # all zero
])
def test_invalid_relative_loadings_are_rejected(loadings) -> None:
    with pytest.raises(ValueError):
        _view(KIND.RELATIVE, loadings)


def test_relative_orientation_canonicalization_rejects_the_sign_flipped_equivalent() -> None:
    assert _view(KIND.RELATIVE, ("1", "-1", "0"), "0.02").target_return == D("0.02")
    with pytest.raises(ValueError):
        _view(KIND.RELATIVE, ("-1", "1", "0"), "-0.02")


def test_relative_target_is_a_spread_with_no_lower_bound() -> None:
    assert _view(KIND.RELATIVE, ("1", "-1", "0"), "-2.5").target_return == D("-2.5")
    assert _view(KIND.RELATIVE, ("1", "-1", "0"), "0").target_return == 0
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            _view(KIND.RELATIVE, ("1", "-1", "0"), bad)


# --- view type / confidence contracts ----------------------------------------------------------------------------

def test_view_type_contracts() -> None:
    class _DecSub(Decimal):
        pass

    ok = dict(kind=KIND.ABSOLUTE, loadings=(D(0), D(1), D(0)), target_return=D("0.03"), confidence=D("0.75"))

    def make(**changes):
        return UserReturnView(**{**ok, **changes})

    for bad in ("absolute", None, 1, object()):
        with pytest.raises(TypeError):
            make(kind=bad)
    for bad in ([D(0), D(1), D(0)], (0, 1, 0), (0.0, 1.0, 0.0), ("0", "1", "0"), (_DecSub(0), D(1), D(0)), None):
        with pytest.raises(TypeError):
            make(loadings=bad)
    with pytest.raises(ValueError):
        make(loadings=())  # at least one entry
    for bad in ("NaN", "Infinity"):
        with pytest.raises(ValueError):
            make(loadings=(D(0), D(bad), D(0)))
    for bad in (0.03, 1, "0.03", None, True, _DecSub("0.03")):
        with pytest.raises(TypeError):
            make(target_return=bad)


@pytest.mark.parametrize("bad", [0, 1, 0.5, "0.5", True, False, None])
def test_confidence_must_be_an_exact_decimal(bad) -> None:
    with pytest.raises(TypeError):
        UserReturnView(kind=KIND.ABSOLUTE, loadings=(D(0), D(1), D(0)), target_return=D("0.03"), confidence=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["0", "0.0", "-0.1", "1.0000001", "2", "NaN", "Infinity", "-Infinity"])
def test_confidence_must_lie_in_zero_exclusive_one_inclusive(bad: str) -> None:
    with pytest.raises(ValueError):
        _view(KIND.ABSOLUTE, ("0", "1", "0"), confidence=bad)


def test_full_confidence_is_allowed_and_zero_confidence_is_never_approximated() -> None:
    assert _view(KIND.ABSOLUTE, ("0", "1", "0"), confidence="1").confidence == 1
    assert _view(KIND.ABSOLUTE, ("0", "1", "0"), confidence="0.0000000000000000000000000000000000000000000000001").confidence > 0
    with pytest.raises(ValueError):
        _view(KIND.ABSOLUTE, ("0", "1", "0"), confidence="0")


def test_class_signature_has_no_default_confidence() -> None:
    with pytest.raises(TypeError):
        UserReturnView(kind=KIND.ABSOLUTE, loadings=(D(0), D(1), D(0)), target_return=D("0.03"))  # type: ignore[call-arg]


# --- view set ----------------------------------------------------------------------------------------------------

ABS_1 = _view(KIND.ABSOLUTE, ("0", "1", "0"), "0.03", "0.75")
ABS_0 = _view(KIND.ABSOLUTE, ("1", "0", "0"), "0.04", "0.60")
REL_PAIR = _view(KIND.RELATIVE, ("1", "-1", "0"), "0.02", "0.5")
REL_GROUP = _view(KIND.RELATIVE, ("0.5", "0.5", "-1"), "0.01", "0.25")


def test_view_set_has_exactly_two_stored_fields_and_requires_exact_types() -> None:
    prior = _prior()
    viewset = build_user_return_view_set(prior=prior, views=(ABS_1,))
    assert [f.name for f in dataclasses.fields(UserReturnViewSet)] == ["source", "views"]
    assert viewset.source is prior
    with pytest.raises(dataclasses.FrozenInstanceError):
        viewset.views = ()  # type: ignore[misc]
    for bad in (None, prior.source, object()):
        with pytest.raises(TypeError):
            UserReturnViewSet(source=bad, views=(ABS_1,))  # type: ignore[arg-type]
    for bad in ([ABS_1], (ABS_1, "x"), None, ABS_1):
        with pytest.raises(TypeError):
            UserReturnViewSet(source=prior, views=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        UserReturnViewSet(source=prior, views=())


def test_every_view_must_match_the_prior_dimension_exactly() -> None:
    prior = _prior()
    for bad in (_view(KIND.ABSOLUTE, ("0", "1")), _view(KIND.ABSOLUTE, ("0", "1", "0", "0")), _view(KIND.RELATIVE, ("1", "-1"))):
        with pytest.raises(ValueError):
            UserReturnViewSet(source=prior, views=(bad,))
        with pytest.raises(ValueError):
            build_user_return_view_set(prior=prior, views=(bad,))


def test_duplicate_loadings_are_rejected_regardless_of_target_or_confidence() -> None:
    prior = _prior()
    twin = _view(KIND.ABSOLUTE, ("0", "1", "0"), "-0.5", "0.1")
    for views in ((ABS_1, twin), (twin, ABS_1)):
        with pytest.raises(ValueError, match="duplicate"):
            build_user_return_view_set(prior=prior, views=views)
    numerically_equal = _view(KIND.ABSOLUTE, ("0.0", "1.00", "0"), "0.03", "0.75")
    with pytest.raises(ValueError, match="duplicate"):
        build_user_return_view_set(prior=prior, views=(ABS_1, numerically_equal))


def test_constructor_requires_canonical_order_and_the_builder_canonicalizes_it() -> None:
    prior = _prior()
    canonical = (ABS_1, ABS_0, REL_GROUP, REL_PAIR)  # absolute first; lexicographic numeric loadings within a kind
    assert UserReturnViewSet(source=prior, views=canonical).views == canonical
    for shuffled in ((ABS_0, ABS_1, REL_GROUP, REL_PAIR), (REL_PAIR, REL_GROUP, ABS_0, ABS_1), (ABS_1, ABS_0, REL_PAIR, REL_GROUP)):
        with pytest.raises(ValueError, match="canonical"):
            UserReturnViewSet(source=prior, views=shuffled)


def test_input_order_independence_and_view_object_identity() -> None:
    prior = _prior()
    base = (ABS_1, ABS_0, REL_GROUP, REL_PAIR)
    reference = build_user_return_view_set(prior=prior, views=base)
    for permutation in itertools.permutations(base):
        built = build_user_return_view_set(prior=prior, views=permutation)
        assert built.views == reference.views == base
        assert all(a is b for a, b in zip(built.views, base))  # the original view instances, never copied
        assert built.view_matrix == reference.view_matrix and built.view_targets == reference.view_targets
        assert built.view_confidences == reference.view_confidences
        assert built.projected_prior_uncertainties == reference.projected_prior_uncertainties
        assert built.view_noise_covariance == reference.view_noise_covariance


def test_builder_is_keyword_only_with_exact_input_types() -> None:
    prior = _prior()
    with pytest.raises(TypeError):
        build_user_return_view_set(prior, (ABS_1,))  # type: ignore[misc]
    for bad in (None, prior.source, object()):
        with pytest.raises(TypeError):
            build_user_return_view_set(prior=bad, views=(ABS_1,))  # type: ignore[arg-type]
    for bad in ([ABS_1], None, (ABS_1, "x")):
        with pytest.raises(TypeError):
            build_user_return_view_set(prior=prior, views=bad)  # type: ignore[arg-type]


def test_absolute_views_sort_before_relative_and_loadings_sort_numerically() -> None:
    prior = _prior()
    built = build_user_return_view_set(prior=prior, views=(REL_PAIR, ABS_1, REL_GROUP, ABS_0))
    assert [v.kind for v in built.views] == [KIND.ABSOLUTE, KIND.ABSOLUTE, KIND.RELATIVE, KIND.RELATIVE]
    assert [v.loadings for v in built.views] == [ABS_1.loadings, ABS_0.loadings, REL_GROUP.loadings, REL_PAIR.loadings]


# --- derived matrices --------------------------------------------------------------------------------------------

def test_view_matrix_targets_and_confidences_are_derived_in_canonical_order() -> None:
    viewset = _set((REL_PAIR, ABS_1, ABS_0))
    assert viewset.view_matrix == (ABS_1.loadings, ABS_0.loadings, REL_PAIR.loadings)
    assert viewset.view_targets == (D("0.03"), D("0.04"), D("0.02"))
    assert viewset.view_confidences == (D("0.75"), D("0.60"), D("0.5"))
    assert all(type(row) is tuple for row in viewset.view_matrix)
    stored = {f.name for f in dataclasses.fields(UserReturnViewSet)}
    assert not stored & {"view_matrix", "view_targets", "view_confidences", "view_noise_covariance"}


def test_projected_prior_uncertainty_absolute_relative_and_group_on_a_diagonal_prior() -> None:
    viewset = _set((ABS_1, REL_PAIR, REL_GROUP))
    # absolute asset 1: U11 = 9 ; relative (1,-1,0): 4 + 9 = 13 ; group (0.5,0.5,-1): 0.25*4 + 0.25*9 + 1 = 4.25
    by_loadings = dict(zip((v.loadings for v in viewset.views), viewset.projected_prior_uncertainties))
    assert by_loadings[ABS_1.loadings] == 9 and by_loadings[REL_PAIR.loadings] == 13 and by_loadings[REL_GROUP.loadings] == D("4.25")


def test_projected_uncertainty_uses_the_full_matrix_against_an_independent_reference() -> None:
    viewset = _set((ABS_1, REL_PAIR, REL_GROUP), CORR)
    for view, got in zip(viewset.views, viewset.projected_prior_uncertainties):
        assert Fraction(got) == _quadratic(CORR, view.loadings)
    group = next(s for v, s in zip(viewset.views, viewset.projected_prior_uncertainties) if v is REL_GROUP)
    assert group == D("4.75")  # includes 2 * (p0 p1 U01 + p0 p2 U02 + p1 p2 U12) = 0.5


def test_off_diagonal_prior_covariance_changes_projection_and_view_noise() -> None:
    same_diagonal = [["4", "3", "0"], ["3", "9", "0"], ["0", "0", "1"]]
    plain, correlated = _set((REL_PAIR,), DIAG), _set((REL_PAIR,), same_diagonal)
    assert [DIAG[i][i] for i in range(3)] == [same_diagonal[i][i] for i in range(3)]
    assert plain.projected_prior_uncertainties == (D("13"),) and correlated.projected_prior_uncertainties == (D("7"),)  # 4 + 9 - 2*3
    assert plain.view_noise_covariance != correlated.view_noise_covariance
    assert plain.view_noise_covariance[0][0] == 13 and correlated.view_noise_covariance[0][0] == 7  # confidence 0.5 -> omega = s


# --- confidence -> view noise ------------------------------------------------------------------------------------

def test_confidence_to_noise_mapping_endpoints_and_examples() -> None:
    prior_matrix = [["4", "0", "0"], ["0", "9", "0"], ["0", "0", "1"]]  # asset 0 has projected uncertainty s = 4
    for confidence, expected in (("1", "0"), ("0.5", "4"), ("0.25", "12")):
        viewset = _set((_view(KIND.ABSOLUTE, ("1", "0", "0"), "0.03", confidence),), prior_matrix)
        assert viewset.view_noise_covariance == ((D(expected),),)
        assert viewset.view_noise_variances == (D(expected),)


def test_relative_example_has_projected_uncertainty_13_and_noise_13() -> None:
    viewset = _set((REL_PAIR,), DIAG)
    assert viewset.projected_prior_uncertainties == (D("13"),) and viewset.view_noise_covariance == ((D("13"),),)


def test_higher_confidence_means_lower_noise_and_noise_scales_with_the_projection() -> None:
    noise = [
        _set((_view(KIND.ABSOLUTE, ("0", "1", "0"), "0.03", c),)).view_noise_variances[0] for c in ("0.1", "0.3", "0.5", "0.9", "1")
    ]
    assert noise == sorted(noise, reverse=True) and noise[-1] == 0 and noise[0] > noise[1]
    exact = _set((_view(KIND.ABSOLUTE, ("0", "1", "0"), "0.03", "0.75"),)).view_noise_variances[0]
    assert abs(Fraction(exact) - Fraction(1, 3) * 9) <= Fraction(1, 10**45)  # ((1 - c) / c) * s with s = 9


def test_noise_covariance_is_diagonal_with_independent_view_errors() -> None:
    viewset = _set((ABS_1, ABS_0, REL_PAIR, REL_GROUP), CORR)
    omega = viewset.view_noise_covariance
    assert len(omega) == 4 and all(len(row) == 4 for row in omega) and all(type(row) is tuple for row in omega)
    assert all(omega[i][j] == 0 for i in range(4) for j in range(4) if i != j)
    assert [omega[i][i] for i in range(4)] == list(viewset.view_noise_variances)
    assert all(omega[i][i] >= 0 for i in range(4))


def test_multiple_views_each_use_their_own_projection_and_confidence() -> None:
    viewset = _set((ABS_1, REL_PAIR))
    for view, s, omega in zip(viewset.views, viewset.projected_prior_uncertainties, viewset.view_noise_variances):
        expected = Fraction(1) - Fraction(view.confidence)
        assert abs(Fraction(omega) - expected / Fraction(view.confidence) * Fraction(s)) <= Fraction(1, 10**45)


# --- Decimal isolation / range -----------------------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_changes_nothing_and_is_not_mutated() -> None:
    baseline = _set((ABS_1, ABS_0, REL_PAIR, REL_GROUP), CORR)
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            viewset = _set((ABS_1, ABS_0, REL_PAIR, REL_GROUP), CORR)  # prior validation + views + projection + noise
            derived = (viewset.projected_prior_uncertainties, viewset.view_noise_covariance)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert viewset == baseline
        assert [s.as_tuple() for s in derived[0]] == [s.as_tuple() for s in baseline.projected_prior_uncertainties]
        assert [[x.as_tuple() for x in row] for row in derived[1]] == [[x.as_tuple() for x in row] for row in baseline.view_noise_covariance]


def test_decimal_overflow_is_a_static_range_error(monkeypatch) -> None:
    viewset = _set((ABS_1, REL_PAIR))

    def _tiny_range_context() -> decimal.Context:
        return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=-5, Emax=0)

    monkeypatch.setattr(module_under_test, "_analytics_context", _tiny_range_context)
    with pytest.raises(ValueError, match="user-view analytics exceeds supported Decimal range"):
        _ = viewset.projected_prior_uncertainties
    with pytest.raises(ValueError, match="user-view analytics exceeds supported Decimal range"):
        _ = viewset.view_noise_covariance
    with pytest.raises(ValueError, match="user-view analytics exceeds supported Decimal range"):
        _prior([["100", "0"], ["0", "100"]], returns=(D(0), D(0)))


def test_repeated_derivations_are_identical() -> None:
    viewset = _set((ABS_1, REL_PAIR, REL_GROUP), CORR)
    first = (viewset.projected_prior_uncertainties, viewset.view_noise_covariance)
    for _ in range(3):
        assert (viewset.projected_prior_uncertainties, viewset.view_noise_covariance) == first


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_user_views.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_plus_the_two_allocation_authorities_only() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {
        "__future__", "dataclasses", "decimal", "enum", "uuid",
        "backend.engine.private.allocation_matrix", "backend.engine.private.allocation_benchmarks",
    }
    for forbidden in ("numpy", "scipy", "pandas", "sklearn", "statistics", "math", "random", "time", "datetime", "os",
                      "allocation_cvar", "allocation_risk_parity", "allocation_hrp", "deprecated"):
        assert not any(forbidden in name for name in plain), forbidden


def test_one_fresh_analytics_context_no_ambient_state_and_no_float() -> None:
    owners = [f.name for f in ast.walk(_TREE) if isinstance(f, ast.FunctionDef)
              for c in ast.walk(f) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "Context"]
    assert owners == ["_analytics_context"]
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "float", "fmean"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert "prec=50" in _SOURCE and "ROUND_HALF_EVEN" in _SOURCE
    assert "Emin=decimal.MIN_EMIN" in _SOURCE and "Emax=decimal.MAX_EMAX" in _SOURCE
    assert not [n for n in _TREE.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", "") == "Context"]


def test_no_return_inference_from_the_panel_or_historical_covariance() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not (attributes | names) & {"covariance", "sample_means", "AllocationCovarianceMatrix", "build_allocation_covariance_matrix",
                                       "simple_return", "points", "series"}


_FORBIDDEN_FRAGMENTS = (
    "risk_aversion", "equilibrium", "market_cap", "market_portfolio", "reverse_optim", "posterior", "inverse", "invert",
    "solve", "linalg", "gain", "bayesian_update", "optimiz", "benchmark", "weights", "cvar", "score", "rank", "recommend",
    "rebalance", "persist", "repository", "user_id", "owner_id", "supabase", "idzorek",
)


def test_no_tau_equilibrium_posterior_allocation_or_persistence_surface() -> None:
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
        assert lowered != "tau", identifier
        for fragment in _FORBIDDEN_FRAGMENTS:  # "tau" is an exact-name check above (the substring occurs in unrelated words)
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    public = {n for n in dir(module_under_test) if not n.startswith("_")}
    assert {"ExpectedReturnPrior", "UserReturnViewKind", "UserReturnView", "UserReturnViewSet", "build_user_return_view_set"} <= public


def test_documents_the_methodology_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("Black-Litterman-LIKE", "NOT the historical return covariance", "no tau", "Sentinax canonical confidence-to-view-uncertainty mapping",
                   "not the Idzorek", "independent", "no posterior"):
        assert needle in doc, needle
