from typing import List, Dict
from datetime import date
from .models import FinancialSnapshot

class InflationAdjuster:
    """
    Handles TMS 29 Inflation Accounting adjustments to prevent nominal growth illusion.
    """
    def __init__(self, cpi_data: Dict[int, float]):
        """
        :param cpi_data: Dictionary mapping year to CPI index value (e.g., {2021: 68.7, 2022: 112.9, ...})
        """
        self.cpi_data = cpi_data

    def get_deflator(self, base_year: int, target_year: int) -> float:
        """
        Calculate the multiplier to bring base_year money to target_year equivalent.
        """
        if base_year not in self.cpi_data or target_year not in self.cpi_data:
            return 1.0 # Fallback to nominal if CPI data is missing
        
        return self.cpi_data[target_year] / self.cpi_data[base_year]

    def adjust_snapshot(self, snapshot: FinancialSnapshot, target_year: int) -> FinancialSnapshot:
        """
        Adjusts a financial snapshot to the purchasing power of the target_year.
        Returns a new snapshot with adjusted values.
        """
        snapshot_year = snapshot.period_date.year
        deflator = self.get_deflator(snapshot_year, target_year)
        
        if deflator == 1.0:
            return snapshot

        adjusted = snapshot.model_copy()
        adjusted.revenue *= deflator
        adjusted.gross_profit *= deflator
        adjusted.net_income *= deflator
        adjusted.total_assets *= deflator
        adjusted.total_liabilities *= deflator
        adjusted.total_equity *= deflator
        adjusted.short_term_debt *= deflator
        adjusted.long_term_debt *= deflator
        adjusted.cash_and_equivalents *= deflator
        adjusted.operating_cash_flow *= deflator
        adjusted.capital_expenditure *= deflator
        adjusted.eps *= deflator
        
        return adjusted

    def adjust_history(self, snapshots: List[FinancialSnapshot], target_year: int = None) -> List[FinancialSnapshot]:
        """
        Adjusts a list of historical snapshots to the purchasing power of the target_year (defaults to the latest year).
        """
        if not snapshots:
            return []
            
        if target_year is None:
            target_year = max(s.period_date.year for s in snapshots)
            
        return [self.adjust_snapshot(s, target_year) for s in snapshots]
