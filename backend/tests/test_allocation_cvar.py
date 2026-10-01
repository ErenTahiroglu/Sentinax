"""
backend/tests/test_allocation_cvar.py
=====================================
Phase 19A: exact historical portfolio scenarios and the Rockafellar-Uryasev CVaR objective for GIVEN long-only weights.

    F_alpha(zeta) = zeta + 1 / (T (1 - alpha)) * sum_t max(L_t - zeta, 0),     L_t = -sum_i w_i r_it

The empirical minimum over zeta is evaluated exactly on the finite candidate set of unique scenario losses. No optimizer,
no covariance, no expected return, no Phase 16J expected-shortfall reuse.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_cvar as module_under_test
from backend.engine.private.allocation_cvar import (
    CVarPortfolioWeights,
    CVarScenarioPoint,
    CVarScenarioSeries,
    HistoricalPortfolioCVar,
    build_cvar_scenario_series,
    calculate_historical_portfolio_cvar,
    evaluate_cvar_objective,
)
from backend.engine.private.allocation_matrix import (
    AllocationInstrumentReturnSeries,
    AllocationMonth,
    AllocationMonthlyReturnPoint,
    AllocationReturnPanel,
    build_allocation_return_panel,
)

D = Decimal
UUIDS = [UUID(int=i) for i in range(1, 9)]


def _series(instrument_id: UUID, values: list[str]) -> AllocationInstrumentReturnSeries:
    points = tuple(
        AllocationMonthlyReturnPoint(period=AllocationMonth(2026, i + 1), simple_return=D(v)) for i, v in enumerate(values)
    )
    return AllocationInstrumentReturnSeries(instrument_id=instrument_id, points=points)


def _panel(*columns: list[str]) -> AllocationReturnPanel:
    return build_allocation_return_panel(series=tuple(_series(UUIDS[i], v) for i, v in enumerate(columns)))


def _weights(panel: AllocationReturnPanel, *values: str) -> CVarPortfolioWeights:
    return CVarPortfolioWeights(source=panel, weights=tuple(D(v) for v in values))


def _scenarios(columns: list[list[str]], weights: list[str]) -> CVarScenarioSeries:
    panel = _panel(*columns)
    return build_cvar_scenario_series(portfolio=_weights(panel, *weights))


def _exact_sum(values) -> Fraction:
    return sum((Fraction(v) for v in values), Fraction(0))


ALPHA = D("0.75")
# one-asset fixture with losses [-0.10, 0, 0.10, 0.20]: returns are the negated losses
ONE_ASSET = [["0.10", "0", "-0.10", "-0.20"]]
# two assets, w = (0.25, 0.75): portfolio returns 0.025, 0.10, -0.0625, -0.225  -> losses -0.025, -0.10, 0.0625, 0.225
TWO_A = ["0.10", "-0.20", "0.05", "0.30"]
TWO_B = ["0.00", "0.20", "-0.10", "-0.40"]


# --- portfolio weights -------------------------------------------------------------------------------------------

def test_weights_have_exactly_two_stored_fields_and_are_frozen() -> None:
    panel = _panel(*ONE_ASSET)
    weights = _weights(panel, "1")
    assert [f.name for f in dataclasses.fields(CVarPortfolioWeights)] == ["source", "weights"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        weights.weights = ()  # type: ignore[misc]
    assert weights.source is panel


def test_weights_require_exact_panel_type() -> None:
    panel = _panel(*ONE_ASSET)

    class _Sub(AllocationReturnPanel):
        pass

    sub = object.__new__(_Sub)
    object.__setattr__(sub, "series", panel.series)
    for bad in (None, object(), panel.series, sub):
        with pytest.raises(TypeError):
            CVarPortfolioWeights(source=bad, weights=(D(1),))  # type: ignore[arg-type]


def test_weights_require_an_exact_tuple_of_exact_decimals() -> None:
    panel = _panel(TWO_A, TWO_B)

    class _DecSub(Decimal):
        pass

    for bad in ([D("0.5"), D("0.5")], (0.5, 0.5), (1, 0), (True, False), ("0.5", "0.5"), (_DecSub("0.5"), D("0.5")), None):
        with pytest.raises(TypeError):
            CVarPortfolioWeights(source=panel, weights=bad)  # type: ignore[arg-type]


def test_weights_range_and_exact_sum() -> None:
    panel = _panel(TWO_A, TWO_B)
    for bad in (("1.1", "-0.1"), ("-0.1", "1.1"), ("NaN", "1"), ("Infinity", "-Infinity"), ("1", "1"), ("0.5", "0.4999999999999999999999999999999999999999999999999")):
        with pytest.raises(ValueError):
            CVarPortfolioWeights(source=panel, weights=tuple(D(v) for v in bad))
    with pytest.raises(ValueError):
        CVarPortfolioWeights(source=panel, weights=(D("-0"), D("1")))  # signed zero is not canonical
    assert _weights(panel, "0.25", "0.75").weights == (D("0.25"), D("0.75"))


def test_weights_wrong_dimension_is_rejected() -> None:
    panel = _panel(TWO_A, TWO_B)
    for bad in ((D(1),), (D("0.5"), D("0.25"), D("0.25")), ()):
        with pytest.raises(ValueError):
            CVarPortfolioWeights(source=panel, weights=bad)


def test_exact_sum_uses_context_free_coefficients_not_ambient_rounding() -> None:
    panel = _panel(TWO_A, TWO_B)
    third = D("0.3333333333333333333333333333333333333333333333333")
    big = _panel(["0", "0.1"], ["0.1", "0"], ["0.2", "0.3"])
    CVarPortfolioWeights(source=big, weights=(third, third, D("0.3333333333333333333333333333333333333333333333334")))
    with decimal.localcontext(decimal.Context(prec=2)):
        _weights(panel, "0.25", "0.75")
        with pytest.raises(ValueError):
            CVarPortfolioWeights(source=big, weights=(third, third, third))


def test_zero_weights_are_valid_and_keep_the_full_dimension() -> None:
    panel = _panel(TWO_A, TWO_B)
    for values in (("1", "0"), ("0", "1")):
        weights = _weights(panel, *values)
        scenarios = build_cvar_scenario_series(portfolio=weights)
        assert weights.source is panel and scenarios.source is weights
        assert len(weights.weights) == panel.instrument_count == 2  # no instrument dropped, no renormalization
    assert [p.portfolio_return for p in build_cvar_scenario_series(portfolio=_weights(panel, "1", "0")).points] == [D(v) for v in TWO_A]
    assert [p.portfolio_return for p in build_cvar_scenario_series(portfolio=_weights(panel, "0", "1")).points] == [D(v) for v in TWO_B]


# --- scenarios ---------------------------------------------------------------------------------------------------

def test_scenario_point_has_exactly_three_stored_fields_and_is_frozen() -> None:
    assert [f.name for f in dataclasses.fields(CVarScenarioPoint)] == ["period", "portfolio_return", "loss"]
    point = CVarScenarioPoint(period=AllocationMonth(2026, 1), portfolio_return=D("0.1"), loss=D("-0.1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.loss = D(0)  # type: ignore[misc]


def test_scenario_point_validates_types_and_the_loss_sign_convention() -> None:
    month = AllocationMonth(2026, 1)

    class _DecSub(Decimal):
        pass

    with pytest.raises(TypeError):
        CVarScenarioPoint(period=(2026, 1), portfolio_return=D("0.1"), loss=D("-0.1"))  # type: ignore[arg-type]
    for bad in (0.1, 1, "0.1", None, _DecSub("0.1")):
        with pytest.raises(TypeError):
            CVarScenarioPoint(period=month, portfolio_return=bad, loss=D("-0.1"))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            CVarScenarioPoint(period=month, portfolio_return=D("0.1"), loss=bad)  # type: ignore[arg-type]
    for bad in ("NaN", "Infinity"):
        with pytest.raises(ValueError):
            CVarScenarioPoint(period=month, portfolio_return=D(bad), loss=D(bad))
    with pytest.raises(ValueError):
        CVarScenarioPoint(period=month, portfolio_return=D("0.1"), loss=D("0.1"))  # loss must be -return
    with pytest.raises(ValueError):
        CVarScenarioPoint(period=month, portfolio_return=D("0"), loss=D("-0"))  # negative zero is not canonical
    assert CVarScenarioPoint(period=month, portfolio_return=D("0"), loss=D("0")).loss == 0


def test_one_asset_scenarios_are_negated_returns_in_period_order() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])
    assert [p.portfolio_return for p in scenarios.points] == [D("0.10"), D("0"), D("-0.10"), D("-0.20")]
    assert [p.loss for p in scenarios.points] == [D("-0.10"), D("0"), D("0.10"), D("0.20")]
    assert [p.period for p in scenarios.points] == [AllocationMonth(2026, m) for m in (1, 2, 3, 4)]


def test_two_asset_portfolio_return_and_loss_by_hand() -> None:
    scenarios = _scenarios([TWO_A, TWO_B], ["0.25", "0.75"])
    assert [p.portfolio_return for p in scenarios.points] == [D("0.025"), D("0.10"), D("-0.0625"), D("-0.225")]
    assert [p.loss for p in scenarios.points] == [D("-0.025"), D("-0.10"), D("0.0625"), D("0.225")]


def test_profitable_scenarios_have_negative_loss_and_loss_is_not_floored() -> None:
    scenarios = _scenarios([["0.5", "0.2"]], ["1"])
    assert [p.loss for p in scenarios.points] == [D("-0.5"), D("-0.2")]
    assert max(p.loss for p in scenarios.points) < 0


def test_negative_zero_loss_is_canonicalized_to_plain_zero() -> None:
    for returns in (["0", "0.1"], ["-0", "0.1"]):
        scenarios = _scenarios([returns], ["1"])
        loss = scenarios.points[0].loss
        assert loss.as_tuple() == D("0").as_tuple() and not loss.is_signed()
        assert not scenarios.points[0].portfolio_return.is_signed()
    zero_weight = _scenarios([["-0.5", "0.1"], ["0.2", "0.3"]], ["0", "1"])
    assert zero_weight.points[0].loss == D("-0.2")


def test_scenario_series_retains_source_and_uses_the_panel_period_sequence() -> None:
    panel = _panel(TWO_A, TWO_B)
    portfolio = _weights(panel, "0.25", "0.75")
    scenarios = build_cvar_scenario_series(portfolio=portfolio)
    assert scenarios.source is portfolio
    assert [f.name for f in dataclasses.fields(CVarScenarioSeries)] == ["source", "points"]
    assert tuple(p.period for p in scenarios.points) == panel.periods and len(scenarios.points) == panel.observation_count


def test_scenario_builder_is_keyword_only_exact_type() -> None:
    panel = _panel(*ONE_ASSET)
    portfolio = _weights(panel, "1")
    with pytest.raises(TypeError):
        build_cvar_scenario_series(portfolio)  # type: ignore[misc]
    for bad in (None, panel, object()):
        with pytest.raises(TypeError):
            build_cvar_scenario_series(portfolio=bad)  # type: ignore[arg-type]


def test_scenario_series_forge_resistance() -> None:
    scenarios = _scenarios([TWO_A, TWO_B], ["0.25", "0.75"])
    good, source = scenarios.points, scenarios.source

    def forged(index: int, **changes):
        points = list(good)
        points[index] = dataclasses.replace(points[index], **changes)
        return tuple(points)

    with pytest.raises(ValueError):
        CVarScenarioSeries(source=source, points=forged(0, portfolio_return=D("0.03"), loss=D("-0.03")))
    with pytest.raises(ValueError):
        CVarScenarioSeries(source=source, points=forged(1, period=AllocationMonth(2030, 1)))
    with pytest.raises(ValueError):
        CVarScenarioSeries(source=source, points=good[::-1])
    with pytest.raises(ValueError):
        CVarScenarioSeries(source=source, points=good[:-1])
    with pytest.raises(ValueError):
        CVarScenarioSeries(source=source, points=good + (good[0],))
    with pytest.raises(TypeError):
        CVarScenarioSeries(source=source, points=list(good))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CVarScenarioSeries(source=source, points=(good[0], "x", good[2], good[3]))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CVarScenarioSeries(source=source.source, points=good)  # type: ignore[arg-type]


# --- confidence --------------------------------------------------------------------------------------------------

def test_confidence_must_be_an_exact_decimal_strictly_inside_zero_one() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])

    class _DecSub(Decimal):
        pass

    for bad in (95, 0.95, "0.95", True, None, _DecSub("0.95")):
        with pytest.raises(TypeError):
            calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=bad)  # type: ignore[arg-type]
    for bad in ("0", "1", "-0.1", "1.5", "95", "NaN", "sNaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D(bad))
    for good in ("0.0000000000000000001", "0.5", "0.9999999999999999999"):  # open interval: any interior value is valid
        result = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D(good))
        assert result.confidence_level == D(good)


def test_equivalent_decimal_spellings_give_the_same_cvar() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])
    first = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.75"))
    for spelling in ("0.750", "0.7500000", "7.5E-1", "0.75E0"):
        again = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D(spelling))
        assert again.conditional_value_at_risk == first.conditional_value_at_risk and again.threshold == first.threshold


def test_no_default_confidence_level() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])
    with pytest.raises(TypeError):
        calculate_historical_portfolio_cvar(scenarios=scenarios)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        evaluate_cvar_objective(scenarios=scenarios, threshold=D(0))  # type: ignore[call-arg]


# --- Rockafellar-Uryasev objective -------------------------------------------------------------------------------

def _objective(scenarios: CVarScenarioSeries, zeta: str, alpha: str = "0.75") -> Decimal:
    return evaluate_cvar_objective(scenarios=scenarios, confidence_level=D(alpha), threshold=D(zeta))


def test_objective_at_every_candidate_threshold_on_the_hand_fixture() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])  # losses -0.10, 0, 0.10, 0.20 ; T = 4 ; 1 / (T (1 - alpha)) = 1
    assert _objective(scenarios, "-0.10") == D("0.50")   # -0.10 + (0 + 0.10 + 0.20 + 0.30)
    assert _objective(scenarios, "0") == D("0.30")       #  0    + (0 + 0 + 0.10 + 0.20)
    assert _objective(scenarios, "0.10") == D("0.20")    #  0.10 + (0.10)
    assert _objective(scenarios, "0.20") == D("0.20")    #  0.20 + 0


def test_objective_positive_part_negative_zero_and_out_of_range_thresholds() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])
    assert _objective(scenarios, "0") == D("0.30")                # zeta = 0
    below = _objective(scenarios, "-5")                            # below every loss: every term is active
    assert below == D("-5") + (D("4.9") + D("5.0") + D("5.1") + D("5.2"))
    above = _objective(scenarios, "7")                             # above every loss: the positive part is 0, F = zeta
    assert above == D("7")
    assert _objective(scenarios, "-0.10", "0.75") == D("0.50")


def test_objective_accepts_negative_zeta_and_uses_the_confidence_scaling() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])
    # alpha = 0.5 -> 1 / (T (1 - alpha)) = 1 / 2 ; F(0.10) = 0.10 + 0.10 / 2
    assert _objective(scenarios, "0.10", "0.5") == D("0.15")
    assert _objective(scenarios, "-0.10", "0.5") == D("-0.10") + D("0.60") / 2


def test_objective_is_convex_on_the_fixture() -> None:
    # regression sanity fixture only; it is not a proof of general convexity
    scenarios = _scenarios([TWO_A, TWO_B], ["0.25", "0.75"])
    z1, z3 = Fraction(-1, 5), Fraction(3, 10)

    def value(zeta: Fraction) -> Fraction:
        return Fraction(_objective(scenarios, str(D(zeta.numerator) / D(zeta.denominator)), "0.5"))

    for lam in (Fraction(1, 5), Fraction(1, 2), Fraction(3, 5)):
        z2 = lam * z1 + (1 - lam) * z3  # strictly between z1 and z3
        assert z1 < z2 < z3
        assert value(z2) <= lam * value(z1) + (1 - lam) * value(z3)


def test_objective_validates_inputs() -> None:
    scenarios = _scenarios(ONE_ASSET, ["1"])

    class _DecSub(Decimal):
        pass

    for bad in (0.1, 1, "0.1", None, True, _DecSub("0.1")):
        with pytest.raises(TypeError):
            evaluate_cvar_objective(scenarios=scenarios, confidence_level=ALPHA, threshold=bad)  # type: ignore[arg-type]
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            evaluate_cvar_objective(scenarios=scenarios, confidence_level=ALPHA, threshold=D(bad))
    for bad in (None, scenarios.source, object()):
        with pytest.raises(TypeError):
            evaluate_cvar_objective(scenarios=bad, confidence_level=ALPHA, threshold=D(0))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_cvar_objective(scenarios, ALPHA, D(0))  # type: ignore[misc]


# --- empirical CVaR minimum --------------------------------------------------------------------------------------

def test_hand_fixture_minimum_tie_breaks_to_the_smallest_threshold() -> None:
    result = calculate_historical_portfolio_cvar(scenarios=_scenarios(ONE_ASSET, ["1"]), confidence_level=ALPHA)
    assert result.threshold == D("0.10")                      # 0.10 and 0.20 both attain F = 0.20: smallest wins
    assert result.conditional_value_at_risk == D("0.20")
    assert _objective(result.source, str(result.threshold)) == _objective(result.source, "0.20") == result.conditional_value_at_risk


def test_cvar_is_not_the_mean_of_losses_at_or_above_the_threshold() -> None:
    result = calculate_historical_portfolio_cvar(scenarios=_scenarios(ONE_ASSET, ["1"]), confidence_level=ALPHA)
    tail_mean = (D("0.10") + D("0.20")) / 2  # Phase 16J style: every loss >= the selected threshold
    assert result.conditional_value_at_risk != tail_mean and tail_mean == D("0.15")
    assert result.conditional_value_at_risk == D("0.20")  # the Rockafellar-Uryasev minimum, 25% tail = the single worst loss


def test_weighted_portfolio_minimum_by_hand() -> None:
    result = calculate_historical_portfolio_cvar(
        scenarios=_scenarios([TWO_A, TWO_B], ["0.25", "0.75"]), confidence_level=D("0.5"),
    )
    # losses -0.10, -0.025, 0.0625, 0.225 ; F(-0.10)=0.18125, F(-0.025)=0.14375, F(0.0625)=0.14375, F(0.225)=0.225
    assert result.conditional_value_at_risk == D("0.14375")
    assert result.threshold == D("-0.025")  # multiple minimizers: the smaller one
    assert _objective(result.source, "0.0625", "0.5") == D("0.14375")
    assert result.conditional_value_at_risk == (D("0.225") + D("0.0625")) / 2  # worst 50% mean agrees in this fixture


def test_corner_weights_give_each_asset_its_own_cvar() -> None:
    first = calculate_historical_portfolio_cvar(scenarios=_scenarios([TWO_A, TWO_B], ["1", "0"]), confidence_level=D("0.5"))
    second = calculate_historical_portfolio_cvar(scenarios=_scenarios([TWO_A, TWO_B], ["0", "1"]), confidence_level=D("0.5"))
    # asset A losses -0.30, -0.05, 0.20, -0.10 -> worst two 0.20, -0.05 ; asset B losses 0.40, 0.10, -0.20, 0.00
    assert first.conditional_value_at_risk == (D("0.20") + D("-0.05")) / 2
    assert second.conditional_value_at_risk == (D("0.40") + D("0.10")) / 2
    assert first.source.source.weights == (D("1"), D("0")) and second.source.source.weights == (D("0"), D("1"))


def test_all_profitable_scenarios_have_a_negative_cvar() -> None:
    result = calculate_historical_portfolio_cvar(scenarios=_scenarios([["0.1", "0.2", "0.3", "0.4"]], ["1"]), confidence_level=D("0.5"))
    # losses -0.4, -0.3, -0.2, -0.1 ; F(-0.4)=-0.1, F(-0.3)=F(-0.2)=-0.15, F(-0.1)=-0.1
    assert result.conditional_value_at_risk == D("-0.15") and result.conditional_value_at_risk < 0
    assert result.threshold == D("-0.3")


def test_discrete_ties_and_repeated_losses() -> None:
    scenarios = _scenarios([["-0.1", "-0.1", "-0.1", "0.1"]], ["1"])  # losses 0.1, 0.1, 0.1, -0.1
    result = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))
    assert result.conditional_value_at_risk == D("0.1") and result.threshold == D("0.1")
    again = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))
    assert again == result


def test_cvar_value_is_at_most_one_and_mixed_gains_losses() -> None:
    result = calculate_historical_portfolio_cvar(scenarios=_scenarios([["-1", "-1", "0.5", "0.5"]], ["1"]), confidence_level=D("0.5"))
    assert result.conditional_value_at_risk == D("1")  # a total loss: loss = 1 is the bound
    mixed = calculate_historical_portfolio_cvar(scenarios=_scenarios([["-0.5", "0.5", "0", "0.25"]], ["1"]), confidence_level=D("0.5"))
    assert mixed.conditional_value_at_risk <= 1


def test_result_retains_lineage_and_exposes_derived_properties() -> None:
    panel = _panel(TWO_A, TWO_B)
    scenarios = build_cvar_scenario_series(portfolio=_weights(panel, "0.25", "0.75"))
    result = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))
    assert result.source is scenarios and result.source.source.source is panel
    assert result.instrument_ids == panel.instrument_ids and result.periods == panel.periods and result.scenario_count == 4
    assert [f.name for f in dataclasses.fields(HistoricalPortfolioCVar)] == ["source", "confidence_level", "threshold", "conditional_value_at_risk"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.threshold = D(0)  # type: ignore[misc]


def test_result_forge_resistance() -> None:
    scenarios = _scenarios([TWO_A, TWO_B], ["0.25", "0.75"])
    good = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))

    def make(**changes):
        base = dict(source=scenarios, confidence_level=D("0.5"), threshold=good.threshold, conditional_value_at_risk=good.conditional_value_at_risk)
        base.update(changes)
        return HistoricalPortfolioCVar(**base)

    assert make() == good
    with pytest.raises(ValueError):
        make(threshold=D("0.0625"))                      # another minimizer, not the canonical (smallest) one
    with pytest.raises(ValueError):
        make(conditional_value_at_risk=D("0.14"))
    with pytest.raises(ValueError):
        make(confidence_level=D("0.6"))
    with pytest.raises(TypeError):
        make(threshold=-0.025)
    with pytest.raises(TypeError):
        make(conditional_value_at_risk="0.14375")
    with pytest.raises(TypeError):
        make(source=scenarios.source)
    with pytest.raises(ValueError):
        make(conditional_value_at_risk=D("NaN"))
    with pytest.raises(ValueError):
        make(conditional_value_at_risk=D("1.5"), threshold=D("1.5"))


# --- Decimal isolation / determinism -----------------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_cannot_change_results_or_be_mutated() -> None:
    columns = [TWO_A, TWO_B, ["0.01", "-0.03", "0.07", "0.02"]]
    panel = _panel(*columns)
    third = D("0.3333333333333333333333333333333333333333333333333")
    portfolio = CVarPortfolioWeights(source=panel, weights=(third, third, D("0.3333333333333333333333333333333333333333333333334")))
    baseline_scen = build_cvar_scenario_series(portfolio=portfolio)
    baseline = calculate_historical_portfolio_cvar(scenarios=baseline_scen, confidence_level=D("0.6"))
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            scen = build_cvar_scenario_series(portfolio=portfolio)
            result = calculate_historical_portfolio_cvar(scenarios=scen, confidence_level=D("0.6"))
            objective = evaluate_cvar_objective(scenarios=scen, confidence_level=D("0.6"), threshold=D("0.01"))
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert scen == baseline_scen and result == baseline
        assert [(p.portfolio_return.as_tuple(), p.loss.as_tuple()) for p in scen.points] == [
            (p.portfolio_return.as_tuple(), p.loss.as_tuple()) for p in baseline_scen.points]
        assert result.conditional_value_at_risk.as_tuple() == baseline.conditional_value_at_risk.as_tuple()
        assert objective == evaluate_cvar_objective(scenarios=baseline_scen, confidence_level=D("0.6"), threshold=D("0.01"))


def test_repeated_calculations_are_identical() -> None:
    scenarios = _scenarios([TWO_A, TWO_B], ["0.25", "0.75"])
    first = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))
    for _ in range(3):
        again = calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))
        assert again == first and again.threshold.as_tuple() == first.threshold.as_tuple()
        assert again.conditional_value_at_risk.as_tuple() == first.conditional_value_at_risk.as_tuple()


def test_decimal_overflow_is_a_static_range_error(monkeypatch) -> None:
    panel = _panel(["50", "90", "10", "30"])
    portfolio = _weights(panel, "1")
    scenarios = build_cvar_scenario_series(portfolio=portfolio)

    def _tiny_range_context() -> decimal.Context:  # genuine decimal.Overflow inside the module's own arithmetic
        return decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=-5, Emax=0)

    monkeypatch.setattr(module_under_test, "_analytics_context", _tiny_range_context)
    with pytest.raises(ValueError, match="portfolio CVaR exceeds supported Decimal analytics range"):
        build_cvar_scenario_series(portfolio=portfolio)
    with pytest.raises(ValueError, match="portfolio CVaR exceeds supported Decimal analytics range"):
        calculate_historical_portfolio_cvar(scenarios=scenarios, confidence_level=D("0.5"))
    with pytest.raises(ValueError, match="portfolio CVaR exceeds supported Decimal analytics range"):
        evaluate_cvar_objective(scenarios=scenarios, confidence_level=D("0.5"), threshold=D("0"))


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def test_imports_are_standard_library_plus_the_two_allocation_authorities_only() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {
        "__future__", "dataclasses", "decimal", "uuid",
        "backend.engine.private.allocation_matrix", "backend.engine.private.allocation_benchmarks",
    }
    for forbidden in ("allocation_risk_parity", "allocation_hrp", "fund_expected_shortfall", "deprecated", "numpy", "pandas",
                      "scipy", "cvxpy", "pulp", "highspy", "statistics", "math", "random"):
        assert not any(forbidden in name for name in plain), forbidden


def test_exact_sum_authority_is_reused_from_phase_18b() -> None:
    assert "_sums_to_exactly_one" in _SOURCE
    assert not {n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)} & {"_sums_to_exactly_one", "_aligned", "_close_to_one"}


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


def test_scenarios_never_touch_covariance_or_sample_means() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not (attributes | names) & {"covariance", "sample_means", "AllocationCovarianceMatrix", "build_allocation_covariance_matrix"}


_FORBIDDEN_FRAGMENTS = (
    "linprog", "scipy", "highs", "simplex", "solver", "optimal", "optimiz", "minimum_return", "target_return", "utility",
    "sharpe", "sortino", "turnover", "rebalance", "bootstrap", "monte", "stress", "black_litterman", "conviction",
    "expected_return", "nearest_rank", "tefas", "decision_variable", "coefficient_matrix",
)


def test_no_optimizer_or_decision_surface() -> None:
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
        for fragment in _FORBIDDEN_FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    public = {name for name in dir(module_under_test) if not name.startswith("_")}
    assert {"CVarPortfolioWeights", "CVarScenarioPoint", "CVarScenarioSeries", "HistoricalPortfolioCVar",
            "build_cvar_scenario_series", "evaluate_cvar_objective", "calculate_historical_portfolio_cvar"} <= public


def test_documents_the_objective_loss_convention_and_positive_part() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("Rockafellar", "L_t(w) = -R_t(w)", "positive part", "not missing-data", "smallest threshold", "negative CVaR"):
        assert needle in doc, needle
