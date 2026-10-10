"""Phase 28B-0: preregistration. Provisional only: no label may be CLOSED/FROZEN, power-analysis fields stay unresolved, training is never authorized."""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.engine.learning.preregistration import (
    REQUIRED_POWER_ANALYSIS_KEYS,
    DecisionState,
    PreregistrationDecision,
    ProposedDomain,
    ResearchStatus,
    ResearchTarget,
    VolatilityPreregistration,
    initial_provisional_decisions,
)
from backend.tests.learning_support import HASH_A, HASH_B, plus


def prereg(**over) -> VolatilityPreregistration:
    base = dict(protocol_version="0.1.0", registered_at=plus(0), domain=ProposedDomain.TEFAS_TRY, target=ResearchTarget.FORWARD_VOLATILITY_RESEARCH,
                status=ResearchStatus.PROVISIONAL, decisions=initial_provisional_decisions(), source_basis_sha256s=(HASH_A,))
    base.update(over)
    return VolatilityPreregistration(**base)


def test_no_state_or_status_is_named_closed() -> None:
    assert not any("CLOSED" in s.name for s in DecisionState) and not any("CLOSED" in s.name for s in ResearchStatus)


def test_default_registration_is_provisional_and_never_authorizes_training() -> None:
    p = prereg()
    assert p.status is ResearchStatus.PROVISIONAL
    assert p.training_authorized is False and p.live_collection_authorized is False
    assert p.domain is ProposedDomain.TEFAS_TRY and p.target is ResearchTarget.FORWARD_VOLATILITY_RESEARCH


def test_power_analysis_fields_are_present_and_unresolved() -> None:
    p = prereg()
    by_key = {d.decision_key: d for d in p.decisions}
    for key in REQUIRED_POWER_ANALYSIS_KEYS:
        assert by_key[key].state is DecisionState.POWER_ANALYSIS_REQUIRED
        assert by_key[key].statement is None
    assert "label.min_observations_in_window" in REQUIRED_POWER_ANALYSIS_KEYS and "power.min_holdout_length" in REQUIRED_POWER_ANALYSIS_KEYS


def test_power_field_cannot_be_given_a_value_or_frozen() -> None:
    key = REQUIRED_POWER_ANALYSIS_KEYS[0]
    with pytest.raises(ValueError):
        PreregistrationDecision(decision_key=key, state=DecisionState.FROZEN, statement="24 months")
    others = tuple(d for d in initial_provisional_decisions() if d.decision_key != key)
    with pytest.raises(ValueError):
        prereg(decisions=others + (PreregistrationDecision(decision_key=key, state=DecisionState.PROVISIONAL, statement="24 months"),))


def test_missing_power_key_rejected() -> None:
    with pytest.raises(ValueError):
        prereg(decisions=tuple(d for d in initial_provisional_decisions() if d.decision_key != REQUIRED_POWER_ANALYSIS_KEYS[1]))


def test_label_decisions_can_never_be_frozen() -> None:
    with pytest.raises(ValueError):
        PreregistrationDecision(decision_key="label.definition", state=DecisionState.FROZEN, statement="sum of squared returns")
    assert PreregistrationDecision(decision_key="label.definition", state=DecisionState.PROVISIONAL, statement="candidate: sum of squared daily simple returns").state is DecisionState.PROVISIONAL


def test_non_label_scope_decisions_may_be_frozen() -> None:
    by_key = {d.decision_key: d for d in initial_provisional_decisions()}
    assert by_key["authority.shadow_only"].state is DecisionState.FROZEN
    assert by_key["scope.no_model_training"].state is DecisionState.FROZEN


def test_unresolved_state_must_not_carry_a_value() -> None:
    with pytest.raises(ValueError):
        PreregistrationDecision(decision_key="x.y", state=DecisionState.UNRESOLVED, statement="something")
    with pytest.raises(ValueError):
        PreregistrationDecision(decision_key="x.y", state=DecisionState.FROZEN, statement=None)


def test_duplicate_decision_keys_rejected() -> None:
    d = initial_provisional_decisions()
    with pytest.raises(ValueError):
        prereg(decisions=d + (d[0],))


def test_outcome_inspection_forbidden_and_revision_lineage_ordered() -> None:
    with pytest.raises(ValueError):
        prereg(outcomes_inspected_before_registration=True)
    with pytest.raises(TypeError):
        prereg(outcomes_inspected_before_registration=0)
    assert prereg(protocol_version="0.2.0", supersedes_protocol_version="0.1.0").supersedes_protocol_version == "0.1.0"
    with pytest.raises(ValueError):
        prereg(protocol_version="0.1.0", supersedes_protocol_version="0.1.0")
    with pytest.raises(ValueError):
        prereg(protocol_version="0.1.0", supersedes_protocol_version="0.2.0")


def test_type_and_format_strictness() -> None:
    for kw in (dict(protocol_version="1"), dict(protocol_version="v0.1.0"), dict(domain="tefas_try"), dict(target="vol"), dict(status="provisional"),
               dict(decisions=list(initial_provisional_decisions())), dict(source_basis_sha256s=("zz",)), dict(source_basis_sha256s=(HASH_A, HASH_A))):
        with pytest.raises((TypeError, ValueError)):
            prereg(**kw)
    with pytest.raises(TypeError):
        prereg(registered_at=plus(0).replace(tzinfo=None))


def test_deterministic_and_order_independent_representation() -> None:
    d = initial_provisional_decisions()
    a, b = prereg(decisions=d), prereg(decisions=tuple(reversed(d)))
    assert a.canonical_sha256() == b.canonical_sha256()
    assert replace(a, source_basis_sha256s=(HASH_B,)).canonical_sha256() != a.canonical_sha256()
