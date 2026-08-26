from typing import List, Dict, Any
from .models import FinancialSnapshot

class ProfitabilityAnalyzer:
    """
    Analyzes profitability metrics based on Buffett's criteria (ROE > 15%, High FCF Conversion).
    """
    
    def __init__(self, target_roe: float = 0.15):
        self.target_roe = target_roe

    def calculate_roe(self, snapshot: FinancialSnapshot) -> float:
        if snapshot.total_equity <= 0:
            return 0.0
        return snapshot.net_income / snapshot.total_equity

    def calculate_fcf_conversion(self, snapshot: FinancialSnapshot) -> float:
        if snapshot.net_income <= 0:
            return 0.0
        return snapshot.free_cash_flow / snapshot.net_income

    def analyze_profitability(self, history: List[FinancialSnapshot]) -> Dict[str, Any]:
        """
        Calculates long-term profitability metrics.
        Expects history to be sorted by date (oldest to newest).
        """
        if not history:
            return {"score": 0.0, "average_roe": 0.0, "average_fcf_conversion": 0.0, "passed": False}

        roes = [self.calculate_roe(s) for s in history]
        fcf_conversions = [self.calculate_fcf_conversion(s) for s in history]

        avg_roe = sum(roes) / len(roes)
        avg_fcf_conv = sum(fcf_conversions) / len(fcf_conversions)

        # Score calculation (Max 30)
        # Up to 20 points for ROE
        roe_score = min(20.0, (avg_roe / self.target_roe) * 20.0) if avg_roe > 0 else 0.0
        
        # Up to 10 points for FCF Conversion (Target 0.8+)
        fcf_score = min(10.0, (avg_fcf_conv / 0.8) * 10.0) if avg_fcf_conv > 0 else 0.0

        total_score = roe_score + fcf_score
        
        return {
            "score": round(total_score, 2),
            "average_roe": round(avg_roe, 4),
            "average_fcf_conversion": round(avg_fcf_conv, 4),
            "passed": avg_roe >= self.target_roe
        }
