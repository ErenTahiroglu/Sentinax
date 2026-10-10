"""
backend/engine/learning/preregistration.py
==========================================
Provisional preregistration record for the first learning research task (Phase 28B-0).

Everything here is PROVISIONAL: the proposed domain (TEFAS TRY) and research target (forward volatility research) are proposals, no label definition can be FROZEN, every
sample-size / power-analysis field must stay POWER_ANALYSIS_REQUIRED, and no state or status is named CLOSED. Training and live collection are never authorized by this
record. Revisions after outcome inspection are structurally forbidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from backend.engine.learning._checks import (
    canonical_sha256,
    iso_utc,
    optional,
    require_aware_datetime,
    require_canonical_token,
    require_enum,
    require_exact_bool,
    require_exact_type,
    require_nonblank_text,
    require_semver,
    require_sha256,
    require_tuple_of,
)


class ProposedDomain(Enum):
    TEFAS_TRY = "tefas_try"


class ResearchTarget(Enum):
    FORWARD_VOLATILITY_RESEARCH = "forward_volatility_research"


class ResearchStatus(Enum):
    PROVISIONAL = "provisional"
    WITHDRAWN = "withdrawn"


class DecisionState(Enum):
    FROZEN = "frozen"
    PROVISIONAL = "provisional"
    UNRESOLVED = "unresolved"
    POWER_ANALYSIS_REQUIRED = "power_analysis_required"


REQUIRED_POWER_ANALYSIS_KEYS: Tuple[str, ...] = (
    "label.min_observations_in_window",
    "label.max_observation_gap",
    "power.min_development_period",
    "power.min_evaluation_origins",
    "power.min_eligible_instruments",
    "power.min_holdout_length",
    "power.min_detectable_loss_improvement",
)
_LABEL_PREFIX = "label."


@dataclass(frozen=True)
class PreregistrationDecision:
    decision_key: str
    state: DecisionState
    statement: Optional[str] = None

    def __post_init__(self) -> None:
        require_canonical_token("decision_key", self.decision_key)
        require_enum("state", self.state, DecisionState)
        optional(require_nonblank_text, "statement", self.statement)
        if self.decision_key.startswith(_LABEL_PREFIX) and self.state is DecisionState.FROZEN:
            raise ValueError("no label decision may be FROZEN in Phase 28B-0")
        if self.decision_key in REQUIRED_POWER_ANALYSIS_KEYS and (self.state is not DecisionState.POWER_ANALYSIS_REQUIRED or self.statement is not None):
            raise ValueError(f"{self.decision_key} must stay POWER_ANALYSIS_REQUIRED without a value until a power analysis exists")
        if self.state in (DecisionState.UNRESOLVED, DecisionState.POWER_ANALYSIS_REQUIRED) and self.statement is not None:
            raise ValueError("unresolved decisions must not carry a value")
        if self.state in (DecisionState.FROZEN, DecisionState.PROVISIONAL) and self.statement is None:
            raise ValueError("frozen/provisional decisions require a statement")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {"decision_key": self.decision_key, "state": self.state.value, "statement": self.statement}


def initial_provisional_decisions() -> Tuple[PreregistrationDecision, ...]:
    """Phase 28B-0 starting point. Pure constant data: scope facts are frozen, label facts provisional, power fields unresolved."""
    frozen = DecisionState.FROZEN
    return (
        PreregistrationDecision("scope.no_model_training", frozen, "No model is trained and no model registry exists in Phase 28B-0."),
        PreregistrationDecision("scope.no_live_collection", frozen, "No live or scheduled collection is performed in Phase 28B-0."),
        PreregistrationDecision("authority.shadow_only", frozen, "Any future learning output is research/shadow only and carries no economic-decision authority."),
        PreregistrationDecision("label.definition", DecisionState.PROVISIONAL, "Candidate only: realized variance from daily NAV returns over a window starting strictly after the prediction origin."),
        PreregistrationDecision("label.unit_continuity", DecisionState.UNRESOLVED),
        PreregistrationDecision("label.distribution_handling", DecisionState.UNRESOLVED),
        PreregistrationDecision("label.lifecycle_handling", DecisionState.UNRESOLVED),
        PreregistrationDecision("label.maturity_rule", DecisionState.UNRESOLVED),
        PreregistrationDecision("universe.target_population", DecisionState.UNRESOLVED),
        PreregistrationDecision("validation.protocol", DecisionState.PROVISIONAL, "Candidate only: forward-only expanding-window walk-forward with label maturity at the evaluation origin."),
    ) + tuple(PreregistrationDecision(k, DecisionState.POWER_ANALYSIS_REQUIRED) for k in REQUIRED_POWER_ANALYSIS_KEYS)


@dataclass(frozen=True)
class VolatilityPreregistration:
    protocol_version: str
    registered_at: datetime
    domain: ProposedDomain
    target: ResearchTarget
    status: ResearchStatus
    decisions: Tuple[PreregistrationDecision, ...]
    source_basis_sha256s: Tuple[str, ...] = ()
    supersedes_protocol_version: Optional[str] = None
    outcomes_inspected_before_registration: bool = False

    training_authorized = False
    live_collection_authorized = False

    def __post_init__(self) -> None:
        version = require_semver("protocol_version", self.protocol_version)
        require_aware_datetime("registered_at", self.registered_at)
        require_enum("domain", self.domain, ProposedDomain)
        require_enum("target", self.target, ResearchTarget)
        require_enum("status", self.status, ResearchStatus)
        decisions = require_tuple_of("decisions", self.decisions, PreregistrationDecision, unique=False)
        keys = [d.decision_key for d in decisions]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate decision_key")
        missing = [k for k in REQUIRED_POWER_ANALYSIS_KEYS if k not in keys]
        if missing:
            raise ValueError(f"required power-analysis decisions missing: {missing}")
        for h in require_tuple_of("source_basis_sha256s", self.source_basis_sha256s, str):
            require_sha256("source_basis_sha256s item", h)
        if self.supersedes_protocol_version is not None and not (require_semver("supersedes_protocol_version", self.supersedes_protocol_version) < version):
            raise ValueError("supersedes_protocol_version must be strictly lower than protocol_version")
        if require_exact_bool("outcomes_inspected_before_registration", self.outcomes_inspected_before_registration):
            raise ValueError("a registration made after outcome inspection is not admissible")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "registered_at": iso_utc(self.registered_at),
            "domain": self.domain.value,
            "target": self.target.value,
            "status": self.status.value,
            "decisions": sorted((d.to_canonical_dict() for d in self.decisions), key=lambda d: d["decision_key"]),
            "source_basis_sha256s": sorted(self.source_basis_sha256s),
            "supersedes_protocol_version": self.supersedes_protocol_version,
            "outcomes_inspected_before_registration": self.outcomes_inspected_before_registration,
            "training_authorized": False,
            "live_collection_authorized": False,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.to_canonical_dict())
