from typing import List, Dict, Any
from .models import BuffettScore, FinancialSnapshot, CompanyProfile

class ScoringModel:
    """
    Combines module scores into a final 0-100 Buffett Score.
    Handles veto gates (hard fails).
    """

    def check_veto_gates(self, history: List[FinancialSnapshot], bs_passed_veto: bool) -> tuple[bool, str]:
        # Gate 1: Negative Equity
        if history and history[-1].total_equity <= 0:
            return False, "Negative Equity"

        # Gate 2: Net Loss in 2 of last 3 years
        if len(history) >= 3:
            recent_years = history[-3:]
            loss_years = sum(1 for s in recent_years if s.net_income < 0)
            if loss_years >= 2:
                return False, f"Net loss in {loss_years} of last 3 years"

        # Gate 3: High Leverage (already handled in balance sheet analyzer, pass result here)
        if not bs_passed_veto:
            return False, "High Leverage (D/E ratio too high)"

        return True, ""

    def calculate_data_confidence(self, history: List[FinancialSnapshot]) -> float:
        """
        Calculates data confidence. If history < 5 years, confidence drops.
        """
        if not history:
            return 0.0
            
        years = len(history)
        if years >= 10:
            return 100.0
        elif years >= 4: # yfinance provides 4 years for free, this should be considered sufficient
            return 80.0
        elif years >= 3:
            return 60.0
        else:
            return 40.0

    def generate_score(self, 
                       profile: CompanyProfile,
                       history: List[FinancialSnapshot],
                       moat_score: float, 
                       profit_score: float, 
                       bs_score: float, 
                       val_score: float,
                       bs_passed_veto: bool,
                       llm_moat_adjustment: float = 0.0) -> BuffettScore:
        
        # Adjust Moat Score with LLM qualitative input (capped at 20)
        final_moat = min(20.0, moat_score + llm_moat_adjustment)
        
        # Calculate totals
        total_score = final_moat + profit_score + bs_score + val_score
        
        # Check Gates
        passed_veto, reason = self.check_veto_gates(history, bs_passed_veto)
        
        if not passed_veto:
            total_score = 0.0

        confidence = self.calculate_data_confidence(history)

        return BuffettScore(
            moat_score=round(final_moat, 2),
            profitability_score=round(profit_score, 2),
            balance_sheet_score=round(bs_score, 2),
            valuation_score=round(val_score, 2),
            total_score=round(total_score, 2),
            data_confidence=confidence,
            passed_veto=passed_veto,
            veto_reason=reason if not passed_veto else None
        )
