"""
backend/engine/private/precious_metals/comparator.py
====================================================
Point-in-time cross-source semantic comparability evaluator for Precious Metals observations.

Strict Invariants:
    - Never converts currency (no USD -> TRY conversion).
    - Never converts units (no kg -> oz or gram conversion).
    - Never averages differing prices or reconciles by synthetic fabrication.
    - Fails to NOT_COMPARABLE if any dimension (metal, currency, unit, purity, price_type, settlement, date) differs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from backend.engine.private.precious_metals.models import (
    ComparabilityResult,
    ComparabilityStatus,
    PreciousMetalMarketObservation,
)


class PreciousMetalCrossSourceComparator:
    """
    Evaluates semantic comparability between two precious metal market observations.
    """

    @classmethod
    def compare(
        cls,
        obs_a: PreciousMetalMarketObservation,
        obs_b: PreciousMetalMarketObservation,
    ) -> ComparabilityResult:
        """
        Compares obs_a and obs_b. Returns CONSISTENT, DIVERGENT, or NOT_COMPARABLE.
        """
        reasons: List[str] = []

        # 1. Effective date
        if obs_a.effective_date != obs_b.effective_date:
            reasons.append(f"EFFECTIVE_DATE_MISMATCH: {obs_a.effective_date} vs {obs_b.effective_date}")

        # 2. Metal
        if obs_a.metal != obs_b.metal:
            reasons.append(f"METAL_MISMATCH: {obs_a.metal.value} vs {obs_b.metal.value}")

        # 3. Currency
        if obs_a.price_currency != obs_b.price_currency:
            reasons.append(f"CURRENCY_MISMATCH: {obs_a.price_currency.value} vs {obs_b.price_currency.value}")

        # 4. Quantity Unit
        if obs_a.quantity_unit != obs_b.quantity_unit:
            reasons.append(f"UNIT_MISMATCH: {obs_a.quantity_unit.value} vs {obs_b.quantity_unit.value}")

        # 5. Price Quantity
        if obs_a.price_quantity != obs_b.price_quantity:
            reasons.append(f"PRICE_QUANTITY_MISMATCH: {obs_a.price_quantity} vs {obs_b.price_quantity}")

        # 6. Price Type
        if obs_a.price_type != obs_b.price_type:
            reasons.append(f"PRICE_TYPE_MISMATCH: {obs_a.price_type.value} vs {obs_b.price_type.value}")

        # 7. Purity / Fineness Semantics
        # If canonical fineness is available on both, compare fineness_per_mille
        if obs_a.fineness_per_mille is not None and obs_b.fineness_per_mille is not None:
            if obs_a.fineness_per_mille != obs_b.fineness_per_mille:
                reasons.append(
                    f"PURITY_FINENESS_MISMATCH: {obs_a.fineness_per_mille}‰ vs {obs_b.fineness_per_mille}‰"
                )
        else:
            # Fallback to raw purity value, scale, and legacy purity
            if (
                obs_a.raw_purity_value != obs_b.raw_purity_value
                or obs_a.purity_scale != obs_b.purity_scale
                or obs_a.purity != obs_b.purity
            ):
                reasons.append(
                    f"PURITY_MISMATCH: val={obs_a.raw_purity_value or obs_a.purity} (scale={obs_a.purity_scale}) vs "
                    f"val={obs_b.raw_purity_value or obs_b.purity} (scale={obs_b.purity_scale})"
                )

        # 8. Settlement Term (Strict exact comparison; None is UNKNOWN, never defaulted to T+0)
        if obs_a.settlement_term != obs_b.settlement_term:
            reasons.append(
                f"SETTLEMENT_MISMATCH: '{obs_a.settlement_term}' vs '{obs_b.settlement_term}'"
            )

        # 9. Value Date (if specified on either side)
        if obs_a.value_date != obs_b.value_date:
            reasons.append(
                f"VALUE_DATE_MISMATCH: {obs_a.value_date} vs {obs_b.value_date}"
            )

        # If any dimension differs, the observations are NOT_COMPARABLE
        if reasons:
            return ComparabilityResult(
                status=ComparabilityStatus.NOT_COMPARABLE,
                is_comparable=False,
                price_a=obs_a.price,
                price_b=obs_b.price,
                difference=None,
                reasons=reasons,
            )

        # Observations match on all semantic dimensions
        if obs_a.price is None or obs_b.price is None:
            return ComparabilityResult(
                status=ComparabilityStatus.NOT_COMPARABLE,
                is_comparable=False,
                price_a=obs_a.price,
                price_b=obs_b.price,
                difference=None,
                reasons=["MISSING_PRICE: One or both observations have None price."],
            )

        diff = abs(obs_a.price - obs_b.price)
        if diff == Decimal("0"):
            return ComparabilityResult(
                status=ComparabilityStatus.CONSISTENT,
                is_comparable=True,
                price_a=obs_a.price,
                price_b=obs_b.price,
                difference=Decimal("0"),
                reasons=[],
            )
        else:
            return ComparabilityResult(
                status=ComparabilityStatus.DIVERGENT,
                is_comparable=True,
                price_a=obs_a.price,
                price_b=obs_b.price,
                difference=diff,
                reasons=[f"PRICE_DIVERGENCE: Source A price={obs_a.price} diverges from Source B price={obs_b.price} by {diff}."],
            )
