from typing import Dict, Any
from .models import FinancialSnapshot, CompanyProfile

class BalanceSheetAnalyzer:
    """
    Analyzes balance sheet safety (D/E ratio) and applies sector exceptions.
    """
    
    # Financial sectors that are highly leveraged by nature
    FINANCIAL_SECTORS = ["Finans", "Banka", "Sigorta", "Araci Kurum", "Holding"]

    def __init__(self, target_de_ratio: float = 0.50, max_de_ratio: float = 1.50):
        self.target_de_ratio = target_de_ratio
        self.max_de_ratio = max_de_ratio # Veto threshold

    def analyze(self, latest_snapshot: FinancialSnapshot, profile: CompanyProfile) -> Dict[str, Any]:
        
        is_financial = any(fin.lower() in profile.sector.lower() or fin.lower() in profile.industry.lower() for fin in self.FINANCIAL_SECTORS)

        if is_financial:
            # Bypass balance sheet criteria for MVP
            return {
                "score": 20.0, # Full points for bypassed sectors to not penalize them
                "debt_to_equity": 0.0,
                "passed_veto": True,
                "is_financial_bypass": True,
                "reason": f"Bypassed D/E check for financial sector: {profile.sector}/{profile.industry}"
            }

        if latest_snapshot.total_equity <= 0:
            return {
                "score": 0.0,
                "debt_to_equity": float('inf'),
                "passed_veto": False,
                "is_financial_bypass": False,
                "reason": "Negative Equity"
            }

        de_ratio = latest_snapshot.total_debt / latest_snapshot.total_equity
        
        passed_veto = de_ratio <= self.max_de_ratio

        # Score calculation (Max 20 points)
        if de_ratio <= self.target_de_ratio:
            score = 20.0
        elif de_ratio > self.max_de_ratio:
            score = 0.0
        else:
            # Linear penalty between target (0.50) and max (1.50)
            penalty_ratio = (de_ratio - self.target_de_ratio) / (self.max_de_ratio - self.target_de_ratio)
            score = 20.0 * (1.0 - penalty_ratio)

        return {
            "score": round(score, 2),
            "debt_to_equity": round(de_ratio, 4),
            "passed_veto": passed_veto,
            "is_financial_bypass": False,
            "reason": f"D/E Ratio is {de_ratio:.2f}" if not passed_veto else "Healthy Balance Sheet"
        }
