from typing import List, Dict, Any
from .models import FinancialSnapshot, CompanyProfile

class MoatAnalyzer:
    """
    Analyzes economic moat using deterministic proxies: Gross Margin, ROIC, EPS Growth.
    """
    def __init__(self, hurdle_rate: float = 0.10):
        self.hurdle_rate = hurdle_rate

    def calculate_roic(self, snapshot: FinancialSnapshot) -> float:
        invested_capital = snapshot.total_debt + snapshot.total_equity - snapshot.cash_and_equivalents
        if invested_capital <= 0:
            return 0.0
        # NOPAT approximation using Net Income + Interest Exp (simplification since interest isn't in snapshot)
        # We will use Net Income / Invested Capital as a proxy if NOPAT isn't available
        return snapshot.net_income / invested_capital

    def calculate_gross_margin(self, snapshot: FinancialSnapshot) -> float:
        if snapshot.revenue <= 0:
            return 0.0
        return snapshot.gross_profit / snapshot.revenue

    def analyze_moat(self, history: List[FinancialSnapshot], profile: CompanyProfile) -> Dict[str, Any]:
        if not history:
            return {"score": 0.0, "average_roic": 0.0, "average_gross_margin": 0.0}

        roics = [self.calculate_roic(s) for s in history]
        margins = [self.calculate_gross_margin(s) for s in history]

        avg_roic = sum(roics) / len(roics)
        avg_margin = sum(margins) / len(margins)

        # Basic deterministic moat scoring (Max 20 points, qualitative LLM will add separately or override)
        # ROIC > Hurdle Rate -> up to 10 points
        roic_score = min(10.0, (avg_roic / self.hurdle_rate) * 10.0) if avg_roic > 0 else 0.0
        
        # High Gross Margin (Target > 30% generally indicates pricing power) -> up to 10 points
        margin_score = min(10.0, (avg_margin / 0.30) * 10.0) if avg_margin > 0 else 0.0

        total_score = roic_score + margin_score

        return {
            "score": round(total_score, 2),
            "average_roic": round(avg_roic, 4),
            "average_gross_margin": round(avg_margin, 4)
        }
