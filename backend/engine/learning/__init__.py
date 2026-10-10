"""
backend/engine/learning — Phase 28B-0 learning evidence & preregistration contracts
===================================================================================

Isolated bounded context. Immutable, deterministic evidence-contract value objects only: preregistration, source authority, universe coverage, observation status and
temporal provenance. NON-AUTHORITATIVE for every financial decision: no model, label, prediction, return/volatility computation, unit-price adjustment, lifecycle
interpretation, recommendation, portfolio or trade authority. No network, scheduling, clock, entropy or persistence.

Closed private economic-decision modules must never import this package (enforced by backend/tests/test_learning_boundary.py).
"""
