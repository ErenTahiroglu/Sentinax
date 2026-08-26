from typing import List, Dict, Any
from .models import FinancialSnapshot, ValuationResult

class DCFValuation:
    """
    Discounted Cash Flow (DCF) model for Margin of Safety calculation.
    """
    
    def __init__(self, risk_free_rate: float = 0.25, equity_risk_premium: float = 0.08, terminal_growth: float = 0.15):
        # High rates for Turkish inflationary environment if using nominal values,
        # but since we use inflation-adjusted values, these should be real rates.
        # Let's assume real WACC is lower.
        self.real_wacc = 0.12 # Example 12% real WACC
        self.real_terminal_growth = 0.03 # 3% real long-term growth

    def calculate_wacc(self) -> float:
        # Simplified: Using a static real WACC for MVP
        return self.real_wacc

    def calculate_intrinsic_value(self, latest_fcf: float, growth_rate: float, shares_outstanding: int, net_debt: float) -> float:
        if latest_fcf <= 0 or shares_outstanding <= 0:
            return 0.0
            
        wacc = self.calculate_wacc()
        
        # 5-year projection
        projected_fcf = []
        current_fcf = latest_fcf
        for _ in range(5):
            current_fcf *= (1 + growth_rate)
            projected_fcf.append(current_fcf)
            
        # Terminal Value
        terminal_value = (projected_fcf[-1] * (1 + self.real_terminal_growth)) / (wacc - self.real_terminal_growth)
        
        # Discounting
        enterprise_value = 0.0
        for i, fcf in enumerate(projected_fcf, 1):
            enterprise_value += fcf / ((1 + wacc) ** i)
            
        enterprise_value += terminal_value / ((1 + wacc) ** 5)
        
        equity_value = enterprise_value - net_debt
        
        if equity_value <= 0:
            return 0.0
            
        return equity_value / shares_outstanding

    def evaluate(self, history: List[FinancialSnapshot], current_price: float) -> ValuationResult:
        if not history:
            return ValuationResult(intrinsic_value=0, base_case=0, bear_case=0, bull_case=0, margin_of_safety_pct=0, is_undervalued=False)
            
        latest = history[-1]
        
        # Buffett's Owner Earnings proxy: Use Net Income if FCF is negative or artificially depressed by growth CapEx.
        # For MVP, we will use Net Income as a more stable proxy for BIST companies.
        owner_earnings = max(latest.free_cash_flow, latest.net_income * 0.70)
        
        net_debt = latest.total_debt - latest.cash_and_equivalents
        
        # Scenarios (Real Growth Rates)
        base_growth = 0.05  # 5% real growth
        bear_growth = 0.02  # 2% real growth
        bull_growth = 0.08  # 8% real growth
        
        base_case = self.calculate_intrinsic_value(owner_earnings, base_growth, latest.shares_outstanding, net_debt)
        bear_case = self.calculate_intrinsic_value(owner_earnings, bear_growth, latest.shares_outstanding, net_debt)
        bull_case = self.calculate_intrinsic_value(owner_earnings, bull_growth, latest.shares_outstanding, net_debt)
        
        # Conservative intrinsic value (average of base and bear)
        intrinsic_value = (base_case + bear_case) / 2.0
        
        mos_pct = (intrinsic_value - current_price) / intrinsic_value if intrinsic_value > 0 else -1.0
        
        return ValuationResult(
            intrinsic_value=intrinsic_value,
            base_case=base_case,
            bear_case=bear_case,
            bull_case=bull_case,
            margin_of_safety_pct=mos_pct,
            is_undervalued=current_price <= (intrinsic_value * 0.75) # 25% discount rule
        )

    def score(self, result: ValuationResult) -> float:
        """
        Calculates valuation score (Max 30) based on Margin of Safety.
        """
        if result.margin_of_safety_pct <= 0:
            return 0.0
        
        # 0% MoS = 0 points. 25% MoS = 15 points. 50%+ MoS = 30 points.
        mos_score = (result.margin_of_safety_pct / 0.50) * 30.0
        return min(30.0, mos_score)
