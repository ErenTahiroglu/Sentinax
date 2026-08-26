from typing import List, Dict, Any, Tuple
from .models import CompanyProfile, BuffettScore

class SelectionManager:
    """
    Applies the Max 10 rule and constructs the final portfolio based on strict criteria.
    """
    
    def __init__(self, max_holdings: int = 10, min_confidence: float = 70.0, min_score: float = 50.0):
        self.max_holdings = max_holdings
        self.min_confidence = min_confidence
        self.min_score = min_score

    def select_portfolio(self, analyzed_companies: List[Tuple[CompanyProfile, BuffettScore]]) -> List[Tuple[CompanyProfile, BuffettScore]]:
        """
        Filters and selects the top companies for the portfolio.
        """
        valid_candidates = []
        
        for profile, score in analyzed_companies:
            # Must pass veto gates
            if not score.passed_veto:
                continue
                
            # Must have reliable data
            if score.data_confidence < self.min_confidence:
                continue
                
            # Must meet minimum score threshold
            if score.total_score < self.min_score:
                continue
                
            valid_candidates.append((profile, score))

        # Sort by total score descending
        valid_candidates.sort(key=lambda x: x[1].total_score, reverse=True)

        # Enforce Max 10 rule
        # Even if only 4 pass, we return 4. We don't relax criteria to fill 10 slots.
        return valid_candidates[:self.max_holdings]
