from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import date

class CompanyProfile(BaseModel):
    ticker: str
    name: str
    sector: str
    industry: str
    
class FinancialSnapshot(BaseModel):
    period_date: date
    revenue: float
    gross_profit: float
    net_income: float
    total_assets: float
    total_liabilities: float
    total_equity: float
    short_term_debt: float
    long_term_debt: float
    cash_and_equivalents: float
    operating_cash_flow: float
    capital_expenditure: float
    shares_outstanding: int
    eps: float
    
    @property
    def free_cash_flow(self) -> float:
        return self.operating_cash_flow - self.capital_expenditure
        
    @property
    def total_debt(self) -> float:
        return self.short_term_debt + self.long_term_debt

class ValuationResult(BaseModel):
    intrinsic_value: float
    base_case: float
    bear_case: float
    bull_case: float
    margin_of_safety_pct: float
    is_undervalued: bool

class BuffettScore(BaseModel):
    moat_score: float # 0-20
    profitability_score: float # 0-30
    balance_sheet_score: float # 0-20
    valuation_score: float # 0-30
    total_score: float # 0-100
    data_confidence: float # 0-100
    passed_veto: bool
    veto_reason: Optional[str] = None
