"""
backend/tests/test_allocation_cvar_optimizer.py
===============================================
Phase 19B: long-only, fully invested minimum historical CVaR as a Rockafellar-Uryasev linear program solved by SciPy
HiGHS dual simplex (binary64) and then reconstructed and validated through the exact Decimal Phase 19A authority.

The solver output is a CANDIDATE only. Public weights / threshold / CVaR come from the exact Phase 19A rebuild.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import types
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest
import scipy

from backend.engine.private import allocation_cvar_optimizer as module_under_test
from backend.engine.private.allocation_benchmarks import (
    build_equal_weight_benchmark,
    build_inverse_volatility_benchmark,
)
from backend.engine.private.allocation_cvar import (
    CVarPortfolioWeights,
    build_cvar_scenario_series,
    calculate_historical_portfolio_cvar,
)
from backend.engine.private.allocation_cvar_optimizer import (
    MinimumCVarOptimizationResult,
    optimize_minimum_cvar_portfolio,
)
from backend.engine.private.allocation_hrp import build_hierarchical_risk_parity_benchmark
from backend.engine.private.allocation_matrix import (
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    AllocationReturnPanel,
    build_allocation_covariance_matrix,
    build_allocation_return_panel,
)
from backend.engine.private.allocation_risk_parity import build_equal_risk_contribution_benchmark
from backend.tests.invariants import static_guards as sg

D = Decimal
UUIDS = [UUID(int=i) for i in range(1, 9)]
ALPHA = D("0.5")
REPO_ROOT = Path(__file__).resolve().parents[2]
ERR_RANGE = "minimum CVaR solver input exceeds supported binary64 range"
ERR_SOLVER = "minimum CVaR solver did not return an optimal solution"
ERR_POST = "minimum CVaR solver candidate failed exact post-validation"


def _series(instrument_id: UUID, values: list[str]) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, i + 1), simple_return=D(v)) for i, v in enumerate(values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _panel(*columns: list[str]) -> AllocationReturnPanel:
    return build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(columns)))


def _exact_cvar(panel: AllocationReturnPanel, alpha: Decimal, weights: tuple[Decimal, ...]):
    scenarios = build_cvar_scenario_series(portfolio=CVarPortfolioWeights(source=panel, weights=weights))
    return calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=alpha)


# one asset: losses -0.10, 0, 0.10, 0.20
ONE = [["0.10", "0", "-0.10", "-0.20"]]
# asset A strictly dominates asset B in every scenario -> the unique minimum-CVaR portfolio is (1, 0)
DOM_A = ["0.05", "0.04", "0.03", "0.06"]
DOM_B = ["0.01", "-0.02", "0.00", "-0.01"]
# perfectly offsetting assets -> the unique minimum is the interior (0.5, 0.5) with CVaR 0
OFF_A = ["0.10", "-0.10", "0.10", "-0.10"]
OFF_B = ["-0.10", "0.10", "-0.10", "0.10"]
# identical assets -> every split has the same CVaR (non-unique optimum)
TWIN = ["0.05", "-0.02", "0.01", "0.03"]
WIDE = (
    ["0.01", "0.03", "-0.02", "0.04", "0.00", "-0.01", "0.02", "0.05"],
    ["0.02", "-0.01", "0.03", "0.01", "0.04", "0.00", "-0.02", "0.03"],
    ["-0.03", "0.02", "0.01", "-0.01", "0.02", "0.05", "0.00", "0.01"],
)


# --- dependency / version ----------------------------------------------------------------------------------------

def test_scipy_is_pinned_to_the_exact_version_the_canonical_rebuild_depends_on() -> None:
    assert scipy.__version__ == "1.18.0"
    from scipy.optimize import linprog  # noqa: F401  (import must succeed)

    requirements = (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert "scipy==1.18.0" in [line.strip() for line in requirements]
    assert not any(line.strip().lower().startswith("highspy") for line in requirements)
    root = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "-r backend/requirements.txt" in root and "scipy" not in root.lower()  # the root file stays a pure proxy


# --- LP formulation ----------------------------------------------------------------------------------------------

def _lp(columns, alpha=ALPHA):
    return module_under_test._build_linear_program(_panel(*columns), alpha)


def test_lp_variable_order_objective_constraints_and_bounds_on_a_hand_fixture() -> None:
    lp = _lp([["0.10", "-0.20"], ["0.00", "0.30"]])  # N = 2 assets, T = 2 scenarios, alpha = 0.5 -> 1 / (T (1 - alpha)) = 1
    assert lp.c.shape == (5,) and lp.c.tolist() == [0.0, 0.0, 1.0, 1.0, 1.0]  # [w0, w1, zeta, u0, u1]
    assert lp.a_ub.shape == (2, 5) and lp.b_ub.tolist() == [0.0, 0.0]
    assert lp.a_ub.tolist() == [
        [-0.10, -0.00, -1.0, -1.0, 0.0],   # u_0 >= -sum(r_i0 w_i) - zeta   ->  -r w - zeta - u_0 <= 0
        [0.20, -0.30, -1.0, 0.0, -1.0],
    ]
    assert lp.a_eq.shape == (1, 5) and lp.a_eq.tolist() == [[1.0, 1.0, 0.0, 0.0, 0.0]] and lp.b_eq.tolist() == [1.0]
    assert lp.bounds == [(0, 1), (0, 1), (None, None), (0, None), (0, None)]


def test_lp_dimension_and_objective_scale_follow_the_confidence_level() -> None:
    lp = _lp([DOM_A, DOM_B], D("0.75"))  # N = 2, T = 4: 1 / (4 * 0.25) = 1
    assert lp.c.shape[0] == 2 + 1 + 4 and lp.c[3:].tolist() == [1.0, 1.0, 1.0, 1.0]
    lp = _lp([DOM_A, DOM_B], D("0.5"))   # 1 / (4 * 0.5) = 0.5
    assert lp.c[3:].tolist() == [0.5, 0.5, 0.5, 0.5] and lp.c[:3].tolist() == [0.0, 0.0, 1.0]
    assert lp.a_ub.shape == (4, 7) and lp.a_eq.shape == (1, 7)


def test_lp_loss_constraint_signs_are_exact() -> None:
    lp = _lp([DOM_A, DOM_B])
    returns = np.array([[0.05, 0.01], [0.04, -0.02], [0.03, 0.00], [0.06, -0.01]])
    assert np.array_equal(lp.a_ub[:, :2], -returns)       # weight columns = -r_it
    assert lp.a_ub[:, 2].tolist() == [-1.0] * 4           # zeta column = -1
    assert np.array_equal(lp.a_ub[:, 3:], -np.eye(4))     # own u_t = -1, every other u = 0
    assert lp.b_ub.tolist() == [0.0] * 4
    assert lp.a_eq[0, :2].tolist() == [1.0, 1.0] and not lp.a_eq[0, 2:].any() and lp.b_eq.tolist() == [1.0]


def test_lp_bounds_are_explicit_including_the_free_zeta() -> None:
    lp = _lp([DOM_A, DOM_B])
    assert lp.bounds[:2] == [(0, 1), (0, 1)] and lp.bounds[2] == (None, None) and lp.bounds[3:] == [(0, None)] * 4


def test_lp_has_no_expected_return_regularizer_or_tie_break_coefficient() -> None:
    lp = _lp([DOM_A, DOM_B])
    assert lp.c[:2].tolist() == [0.0, 0.0]


# --- solver configuration ----------------------------------------------------------------------------------------

def _fake_result(weights: tuple[float, ...], panel: AllocationReturnPanel, alpha: Decimal = ALPHA, *, objective=None, **overrides):
    count = panel.observation_count
    exact = _exact_cvar(panel, alpha, tuple(D(str(w)) for w in weights)).conditional_value_at_risk if objective is None else objective
    values = dict(
        status=0, success=True, message="mock",
        x=np.array([*weights, 0.0, *([0.0] * count)], dtype=np.float64),
        fun=np.float64(str(exact)),
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _patch_solver(monkeypatch, result, calls=None):
    def fake_linprog(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return result

    monkeypatch.setattr(module_under_test, "linprog", fake_linprog)


def test_solver_configuration_is_exactly_highs_ds_with_the_documented_options(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    calls: list[dict] = []
    _patch_solver(monkeypatch, _fake_result((1.0, 0.0), panel), calls)
    optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)
    kwargs = calls[0]
    assert kwargs["method"] == "highs-ds"
    assert kwargs["options"] == {
        "presolve": True,
        "primal_feasibility_tolerance": 1e-9,
        "dual_feasibility_tolerance": 1e-9,
        "simplex_dual_edge_weight_strategy": "steepest-devex",
        "maxiter": 100000,
        "disp": False,
    }
    assert "time_limit" not in kwargs["options"] and "integrality" not in kwargs
    assert set(kwargs) == {"c", "A_ub", "b_ub", "A_eq", "b_eq", "bounds", "method", "options"}
    assert kwargs["bounds"][2] == (None, None) and len(kwargs["bounds"]) == 2 + 1 + 4
    assert kwargs["c"].shape == (7,) and kwargs["A_ub"].shape == (4, 7)


def test_public_builder_signature_is_keyword_only_with_no_configuration() -> None:
    panel = _panel(*ONE)
    with pytest.raises(TypeError):
        optimize_minimum_cvar_portfolio(panel, ALPHA)  # type: ignore[misc]
    with pytest.raises(TypeError):
        optimize_minimum_cvar_portfolio(return_panel=panel)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA, method="highs-ipm")  # type: ignore[call-arg]
    for bad in (None, object(), panel.series):
        with pytest.raises(TypeError):
            optimize_minimum_cvar_portfolio(return_panel=bad, confidence_level=ALPHA)  # type: ignore[arg-type]

    class _Sub(AllocationReturnPanel):
        pass

    sub = object.__new__(_Sub)
    object.__setattr__(sub, "series", panel.series)
    with pytest.raises(TypeError):
        optimize_minimum_cvar_portfolio(return_panel=sub, confidence_level=ALPHA)


def test_confidence_contract_reuses_phase_19a_semantics() -> None:
    panel = _panel(*ONE)

    class _DecSub(Decimal):
        pass

    for bad in (95, 0.5, "0.5", True, None, _DecSub("0.5")):
        with pytest.raises(TypeError):
            optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=bad)  # type: ignore[arg-type]
    for bad in ("0", "1", "-0.1", "1.5", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D(bad))


# --- binary64 boundary / support envelope ------------------------------------------------------------------------

def test_decimal_to_binary64_goes_through_the_decimal_string_without_float_or_ambient_arithmetic() -> None:
    value = module_under_test._decimal_to_solver64(D("0.1"))
    assert type(value) is np.float64 and value == np.float64("0.1")
    with decimal.localcontext(decimal.Context(prec=2, rounding=decimal.ROUND_DOWN)):
        assert module_under_test._decimal_to_solver64(D("0.123456789012345678")) == np.float64("0.123456789012345678")


def test_exact_zero_coefficients_and_normal_monthly_returns_are_accepted() -> None:
    lp = _lp([["0", "0.02", "-0.05", "0.1"], ["-0.0", "0", "0.003", "-0.99"]])
    assert np.isfinite(lp.a_ub).all()
    assert _lp([["1E-9", "-1E-9"]]).a_ub[0, 0] == -1e-9  # the boundary of the envelope itself is accepted


@pytest.mark.parametrize("bad", ["1E-10", "-1E-10", "9.99E-10", "1E-400"])
def test_nonzero_coefficient_below_the_envelope_is_rejected_not_rounded_up_or_zeroed(bad: str) -> None:
    with pytest.raises(ValueError, match=ERR_RANGE):
        _lp([[bad, "0.1"]])


@pytest.mark.parametrize("bad", ["1.0000001E+15", "1E+16", "1E+400"])
def test_coefficient_above_the_envelope_or_non_finite_binary64_is_rejected_not_clipped(bad: str) -> None:
    with pytest.raises(ValueError, match=ERR_RANGE):
        _lp([[bad, "0.1"]])


def test_extreme_confidence_that_explodes_the_objective_scale_is_rejected() -> None:
    panel = _panel(DOM_A, DOM_B)
    near_one = D("0.9999999999999999999999")  # 1 / (T (1 - alpha)) ~ 2.5E+21 > 1E+15
    with pytest.raises(ValueError, match=ERR_RANGE):
        module_under_test._build_linear_program(panel, near_one)
    exact = calculate_historical_portfolio_cvar(
        scenarios=build_cvar_scenario_series(portfolio=CVarPortfolioWeights(source=panel, weights=(D(1), D(0)))),
        confidence_level=near_one,
    )
    assert exact.conditional_value_at_risk is not None  # the exact authority domain is a superset of the optimizer domain


def test_envelope_failure_happens_before_the_solver_is_called(monkeypatch) -> None:
    calls: list[dict] = []
    _patch_solver(monkeypatch, types.SimpleNamespace(), calls)
    with pytest.raises(ValueError, match=ERR_RANGE):
        optimize_minimum_cvar_portfolio(return_panel=_panel(["1E-10", "0.1"]), confidence_level=ALPHA)
    assert calls == []


# --- mocked solver failure matrix --------------------------------------------------------------------------------

def _run(monkeypatch, result, panel=None, alpha=ALPHA):
    panel = panel or _panel(DOM_A, DOM_B)
    _patch_solver(monkeypatch, result)
    return optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=alpha)


@pytest.mark.parametrize("status", [1, 2, 3, 4])
def test_every_nonzero_scipy_status_fails_closed_without_a_result(monkeypatch, status: int) -> None:
    panel = _panel(DOM_A, DOM_B)
    result = _fake_result((1.0, 0.0), panel, status=status, success=False, message="HiGHS volatile text")
    with pytest.raises(ValueError, match=ERR_SOLVER) as excinfo:
        _run(monkeypatch, result, panel)
    assert "HiGHS" not in str(excinfo.value)  # volatile solver text is never the error authority


def test_success_flag_status_x_and_fun_contract(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    good = _fake_result((1.0, 0.0), panel)
    assert _run(monkeypatch, good, panel).weights == (D(1), D(0))
    for overrides in (
        dict(success=False),
        dict(status=1),
        dict(x=None),
        dict(x=np.array([1.0, 0.0, 0.0])),                                 # wrong length
        dict(x=np.array([1.0, np.nan, 0.0, 0, 0, 0, 0])),
        dict(x=np.array([1.0, 0.0, np.inf, 0, 0, 0, 0])),
        dict(x=np.array([1.0, 0.0, 0.0, 0, 0, 0, -np.inf])),
        dict(fun=np.nan), dict(fun=np.inf), dict(fun=None),
    ):
        with pytest.raises(ValueError, match=ERR_SOLVER):
            _run(monkeypatch, _fake_result((1.0, 0.0), panel, **overrides), panel)
    with pytest.raises(ValueError, match=ERR_SOLVER):
        _run(monkeypatch, types.SimpleNamespace(), panel)  # missing attributes entirely


def test_signed_zero_and_boundary_weights_are_canonicalized_to_exact_decimals(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    result = _run(monkeypatch, _fake_result((1.0, -0.0), panel, objective=_exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk), panel)
    assert result.weights == (D("1"), D("0"))
    assert result.weights[1].as_tuple() == D("0").as_tuple() and not result.weights[1].is_signed()
    tiny = _run(monkeypatch, _fake_result((1.0, -5e-10), panel, objective=_exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk), panel)
    assert tiny.weights[1] == 0 and not tiny.weights[1].is_signed()
    over = _run(monkeypatch, _fake_result((1.0 + 5e-10, 0.0), panel, objective=_exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk), panel)
    assert over.weights == (D(1), D(0))


@pytest.mark.parametrize("weights", [(1.0, -2e-9), (-2e-9, 1.0), (1.0 + 2e-9, 0.0), (0.0, 1.0 + 2e-9)])
def test_raw_weights_beyond_the_reconciliation_tolerance_are_rejected(monkeypatch, weights) -> None:
    panel = _panel(DOM_A, DOM_B)
    with pytest.raises(ValueError, match=ERR_POST):
        _run(monkeypatch, _fake_result(weights, panel, objective=D("0")), panel)


def test_sum_drift_within_1e8_is_closed_exactly_and_beyond_is_rejected(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    objective = _exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk
    closed = _run(monkeypatch, _fake_result((1.0 - 5e-9, 0.0), panel, objective=objective), panel)
    assert closed.weights == (D(1), D(0))  # the residual lands on the largest weight; zeros stay zero
    split = _run(monkeypatch, _fake_result((0.6 - 4e-9, 0.4), panel, objective=_exact_cvar(panel, ALPHA, (D("0.6"), D("0.4"))).conditional_value_at_risk), panel)
    assert split.weights[0] > split.weights[1] > 0
    for drift in (2e-8, -2e-8):
        with pytest.raises(ValueError, match=ERR_POST):
            _run(monkeypatch, _fake_result((1.0 - drift, 0.0), panel, objective=objective), panel)


def test_closure_keeps_zero_weights_and_applies_the_residual_to_the_largest_lowest_index() -> None:
    closed = module_under_test._close_weights([D("0.4999999995"), D("0.5000000000"), D("0")])
    assert closed == (D("0.4999999995"), D("0.5000000005"), D("0"))  # residual 5E-10 -> the largest weight; the zero stays zero
    assert module_under_test._sums_to_exactly_one(closed)
    tie = module_under_test._close_weights([D("0.4999999999"), D("0.4999999999"), D("0.0000000001")])
    assert tie == (D("0.5000000000"), D("0.4999999999"), D("0.0000000001"))  # tie -> lowest canonical index
    assert module_under_test._sums_to_exactly_one(tie)
    exact_one = module_under_test._close_weights([D("0.5"), D("0.5"), D("0")])
    assert exact_one == (D("0.5"), D("0.5"), D("0")) and [w.as_tuple() for w in exact_one] == [D("0.5").as_tuple(), D("0.5").as_tuple(), D("0").as_tuple()]
    with pytest.raises(ValueError, match=ERR_POST):
        module_under_test._close_weights([D("0.5"), D("0.4999999")])  # 1E-7 drift is beyond the 1E-8 limit


def test_solver_objective_mismatch_tolerance(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    exact = _exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk
    accepted = _run(monkeypatch, _fake_result((1.0, 0.0), panel, objective=exact + D("9E-9")), panel)
    assert accepted.conditional_value_at_risk == exact  # the public number is the exact one, never res.fun
    with pytest.raises(ValueError, match=ERR_POST):
        _run(monkeypatch, _fake_result((1.0, 0.0), panel, objective=exact + D("2E-8")), panel)
    with pytest.raises(ValueError, match=ERR_POST):
        _run(monkeypatch, _fake_result((1.0, 0.0), panel, objective=exact - D("2E-8")), panel)


def test_a_non_optimal_but_feasible_mock_candidate_cannot_pass_the_objective_check(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    worse = _exact_cvar(panel, ALPHA, (D("0.5"), D("0.5"))).conditional_value_at_risk
    best = _exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk
    assert worse > best
    with pytest.raises(ValueError, match=ERR_POST):  # candidate (0.5, 0.5) with the solver claiming the true optimum value
        _run(monkeypatch, _fake_result((0.5, 0.5), panel, objective=best), panel)


def test_phase_19a_range_errors_are_preserved_not_converted_to_success(monkeypatch) -> None:
    panel = _panel(DOM_A, DOM_B)
    _patch_solver(monkeypatch, _fake_result((1.0, 0.0), panel))

    def boom(**kwargs):
        raise ValueError("portfolio CVaR exceeds supported Decimal analytics range")

    monkeypatch.setattr(module_under_test, "calculate_historical_portfolio_cvar", boom)
    with pytest.raises(ValueError, match="portfolio CVaR exceeds supported Decimal analytics range"):
        optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)


# --- real HiGHS fixtures -----------------------------------------------------------------------------------------

def test_one_asset_uses_the_same_solver_path_and_matches_phase_19a_exactly() -> None:
    panel = _panel(*ONE)
    result = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D("0.75"))
    exact = _exact_cvar(panel, D("0.75"), (D(1),))
    assert result.weights == (D(1),)
    assert result.threshold == exact.threshold == D("0.10") and result.conditional_value_at_risk == exact.conditional_value_at_risk == D("0.20")


def test_unique_corner_optimum_keeps_exact_zero_weights() -> None:
    panel = _panel(DOM_A, DOM_B)
    result = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)
    assert result.weights == (D(1), D(0))
    assert result.weights[1].as_tuple() == D(0).as_tuple() and not result.weights[1].is_signed()
    flipped = optimize_minimum_cvar_portfolio(return_panel=_panel(DOM_B, DOM_A), confidence_level=ALPHA)
    assert flipped.weights == (D(0), D(1))
    # the dominated asset is a strictly worse portfolio under every confidence level
    for alpha in ("0.25", "0.75"):
        assert optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D(alpha)).weights == (D(1), D(0))


def test_interior_optimum_is_verified_independently_of_the_solver() -> None:
    panel = _panel(OFF_A, OFF_B)
    # hand calculation: w = (x, 1-x) gives returns +/-0.1 (2x-1) so the loss tail is 0.1 |2x-1| >= 0 with equality only at x = 1/2
    grid = {k: _exact_cvar(panel, ALPHA, (D(k) / 10, D(1) - D(k) / 10)).conditional_value_at_risk for k in range(11)}
    assert min(grid, key=lambda k: grid[k]) == 5 and grid[5] == 0 and all(grid[k] > 0 for k in grid if k != 5)
    result = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)
    assert all(abs(w - D("0.5")) <= D("1E-9") for w in result.weights)
    assert 0 <= result.conditional_value_at_risk <= D("1E-8") and result.weights[0] not in (0, 1)
    assert min(grid.values()) >= result.conditional_value_at_risk - D("1E-8")


def test_multiple_optimum_is_feasible_post_validated_repeatable_and_not_claimed_unique() -> None:
    panel = _panel(TWIN, TWIN)
    first = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)
    reference = _exact_cvar(panel, ALPHA, (D(1), D(0))).conditional_value_at_risk
    for split in ((D("0.3"), D("0.7")), (D("0.5"), D("0.5")), (D(0), D(1))):  # several distinct weight vectors, one exact CVaR
        assert _exact_cvar(panel, ALPHA, split).conditional_value_at_risk == reference
    assert first.conditional_value_at_risk == reference and module_under_test._sums_to_exactly_one(first.weights)
    assert all(0 <= w <= 1 for w in first.weights)
    again = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)
    assert again == first and [w.as_tuple() for w in again.weights] == [w.as_tuple() for w in first.weights]


def test_optimized_cvar_is_not_worse_than_the_phase_18_benchmarks() -> None:
    panel = _panel(*WIDE)
    covariance = build_allocation_covariance_matrix(return_panel=panel)
    candidates = {
        "equal": build_equal_weight_benchmark(covariance=covariance).weights,
        "inverse_volatility": build_inverse_volatility_benchmark(covariance=covariance).weights,
        "erc": build_equal_risk_contribution_benchmark(covariance=covariance).weights,
        "hrp": build_hierarchical_risk_parity_benchmark(covariance=covariance).weights,
    }
    for alpha in (D("0.5"), D("0.75")):
        optimized = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=alpha)
        for name, weights in candidates.items():
            assert weights is not None, name
            assert optimized.conditional_value_at_risk <= _exact_cvar(panel, alpha, weights).conditional_value_at_risk + D("1E-8"), name


def test_public_numbers_are_the_exact_phase_19a_authority() -> None:
    panel = _panel(*WIDE)
    result = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D("0.6"))
    exact = _exact_cvar(panel, D("0.6"), result.weights)
    assert result.conditional_value_at_risk == exact.conditional_value_at_risk and result.threshold == exact.threshold
    assert module_under_test._sums_to_exactly_one(result.weights) and all(type(w) is Decimal and 0 <= w <= 1 for w in result.weights)


# --- result contract ---------------------------------------------------------------------------------------------

def test_result_fields_lineage_and_derived_properties() -> None:
    panel = _panel(DOM_A, DOM_B)
    result = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)
    assert [f.name for f in dataclasses.fields(MinimumCVarOptimizationResult)] == [
        "source", "confidence_level", "weights", "threshold", "conditional_value_at_risk"]
    assert result.source is panel and result.confidence_level == ALPHA
    assert result.instrument_ids == panel.instrument_ids and result.periods == panel.periods
    assert result.scenario_count == 4 and result.dimension == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.weights = ()  # type: ignore[misc]
    stored = {type(getattr(result, f.name)).__name__ for f in dataclasses.fields(result)}
    assert not stored & {"ndarray", "float64", "OptimizeResult", "float"}


def test_forged_results_are_rejected_including_other_optimal_looking_alternatives() -> None:
    panel = _panel(DOM_A, DOM_B)
    good = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=ALPHA)

    def make(**changes):
        base = dict(source=panel, confidence_level=ALPHA, weights=good.weights, threshold=good.threshold,
                    conditional_value_at_risk=good.conditional_value_at_risk)
        base.update(changes)
        return MinimumCVarOptimizationResult(**base)

    assert make() == good
    with pytest.raises(ValueError):
        make(weights=(D("0.9"), D("0.1")))
    with pytest.raises(ValueError):
        make(weights=(D(0), D(1)))
    with pytest.raises(ValueError):
        make(threshold=good.threshold + D("0.01"))
    with pytest.raises(ValueError):
        make(conditional_value_at_risk=good.conditional_value_at_risk + D("0.001"))
    with pytest.raises(ValueError):
        make(confidence_level=D("0.6"))
    for bad in (list(good.weights), (0.5, 0.5), (1, 0)):
        with pytest.raises(TypeError):
            make(weights=bad)
    with pytest.raises(TypeError):
        make(threshold=0.1)
    with pytest.raises(TypeError):
        make(conditional_value_at_risk="0.1")
    with pytest.raises(TypeError):
        make(source=None)
    with pytest.raises(TypeError):
        make(confidence_level=0.5)


def test_repeated_real_solves_and_hostile_ambient_context_are_identical() -> None:
    panel = _panel(*WIDE)
    first = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D("0.6"))
    hostile_contexts = [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]
    for context in [None, None, *hostile_contexts]:
        if context is None:
            again = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D("0.6"))
        else:
            with decimal.localcontext(context) as ambient:
                ambient.clear_flags()
                snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
                again = optimize_minimum_cvar_portfolio(return_panel=panel, confidence_level=D("0.6"))
                assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
                assert not any(ambient.flags.values())
        assert again == first
        assert [w.as_tuple() for w in again.weights] == [w.as_tuple() for w in first.weights]
        assert again.threshold.as_tuple() == first.threshold.as_tuple()
        assert again.conditional_value_at_risk.as_tuple() == first.conditional_value_at_risk.as_tuple()


def test_input_series_order_does_not_change_the_canonical_result() -> None:
    forward = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(WIDE)))
    backward = build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in reversed(list(enumerate(WIDE)))))
    first = optimize_minimum_cvar_portfolio(return_panel=forward, confidence_level=D("0.6"))
    second = optimize_minimum_cvar_portfolio(return_panel=backward, confidence_level=D("0.6"))
    assert first == second and first.instrument_ids == tuple(UUIDS[:3])


# --- static guards / pure boundary / scope -----------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_cvar_optimizer.py"


def test_phase_19a_stays_pure_and_the_optimizer_is_deliberately_outside_the_pure_manifest() -> None:
    assert "backend/engine/private/allocation_cvar.py" in sg.PURE_MANIFEST
    assert _REL not in sg.PURE_MANIFEST


def test_targeted_static_guards_g1_to_g5_are_clean_on_the_optimizer_module() -> None:
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_limited_to_the_solver_boundary_and_the_exact_authorities() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {
        "__future__", "dataclasses", "decimal", "uuid", "numpy", "scipy.optimize",
        "backend.engine.private.allocation_cvar", "backend.engine.private.allocation_benchmarks",
    }
    for forbidden in ("allocation_risk_parity", "allocation_hrp", "fund_expected_shortfall", "deprecated", "pandas", "highspy",
                      "cvxpy", "pulp", "random", "statistics", "math"):
        assert not any(forbidden in name for name in plain), forbidden


def test_no_builtin_float_no_ambient_decimal_context_and_one_solver_boundary_helper() -> None:
    calls = [n for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert not [c for c in calls if c.func.id == "float"]
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "from_float"}
    assert "_decimal_to_solver64" in {n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)}
    assert not [n for n in _TREE.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", "") == "Context"]
    assert 'method="highs-ds"' in _SOURCE
    banned = {"time_limit", "integrality", "highs-ipm", "highs", "threads", "random_seed", "small_matrix_value", "large_matrix_value"}
    constants = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    keywords = {k.arg for n in ast.walk(_TREE) if isinstance(n, ast.Call) for k in n.keywords}
    assert not (constants | keywords) & banned  # no undocumented option, time limit, integrality or alternate solver path


def test_no_covariance_expected_return_or_decision_surface() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    identifiers = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | attributes
    identifiers |= {n.name for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    identifiers |= {n.arg for n in ast.walk(_TREE) if isinstance(n, ast.arg)}
    assert not identifiers & {"covariance", "sample_means", "AllocationCovarianceMatrix", "build_allocation_covariance_matrix"}
    for identifier in identifiers:
        lowered = identifier.lower()
        for fragment in ("expected_return", "target_return", "minimum_return", "sharpe", "utility", "turnover", "rebalance",
                         "black_litterman", "conviction", "bootstrap", "monte", "holdings", "fees"):
            assert fragment not in lowered, identifier
    assert {"MinimumCVarOptimizationResult", "optimize_minimum_cvar_portfolio"} <= {n for n in dir(module_under_test) if not n.startswith("_")}


def test_documents_the_numerical_boundary() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("CANDIDATE", "binary64", "highs-ds", "not the economic authority", "not claim", "unique", "1e-9", "1e-8"):
        assert needle in doc, needle
