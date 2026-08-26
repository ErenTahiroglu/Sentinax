import pytest
from datetime import date
from backend.engine.buffett.models import CompanyProfile, FinancialSnapshot
from backend.engine.buffett.data_fetcher import MockKAPFetcher
from backend.engine.buffett.orchestrator import BuffettEngine

def test_buffett_engine_zero_trust():
    """
    Test the deterministic engine logic.
    Mocks are used to ensure no network calls are made (Zero Trust SRE).
    """
    fetcher = MockKAPFetcher()
    cpi_data = {2022: 100.0, 2023: 165.0} # Example inflation data
    
    engine = BuffettEngine(fetcher, cpi_data)
    
    # Valid ticker selection
    tickers = {"BIMAS": 150.0}
    portfolio = engine.run_portfolio_selection(tickers, target_year=2023)
    assert len(portfolio) == 1, "Healthy company (BIMAS) should pass selection"
    
    # Failing ticker should be rejected by vetoes
    portfolio_fail = engine.run_portfolio_selection({"FAIL": 150.0}, target_year=2023)
    assert len(portfolio_fail) == 0, "Failing company should be vetoed"


def test_inflation_adjustment():
    from backend.engine.buffett.inflation_adjuster import InflationAdjuster
    
    cpi = {2022: 100.0, 2023: 150.0}
    adjuster = InflationAdjuster(cpi)
    
    snap = FinancialSnapshot(
        period_date=date(2022, 12, 31),
        revenue=1000,
        gross_profit=400,
        net_income=100,
        total_assets=2000,
        total_liabilities=1000,
        total_equity=1000,
        short_term_debt=200,
        long_term_debt=300,
        cash_and_equivalents=150,
        operating_cash_flow=120,
        capital_expenditure=50,
        shares_outstanding=10,
        eps=10
    )
    
    # Adjust to 2023 (50% inflation)
    adj = adjuster.adjust_snapshot(snap, 2023)
    
    assert adj.revenue == 1500
    assert adj.net_income == 150
