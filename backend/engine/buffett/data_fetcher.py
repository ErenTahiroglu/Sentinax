from abc import ABC, abstractmethod
from typing import List, Optional
import random
from datetime import date
from .models import CompanyProfile, FinancialSnapshot

class BaseDataFetcher(ABC):
    @abstractmethod
    def get_company_profile(self, ticker: str) -> Optional[CompanyProfile]:
        pass

    @abstractmethod
    def get_historical_financials(self, ticker: str, years: int = 10) -> List[FinancialSnapshot]:
        pass

class MockKAPFetcher(BaseDataFetcher):
    """
    Mock fetcher for MVP. In production, this would integrate with 
    Is Yatirim or Fintables API.
    """
    def get_company_profile(self, ticker: str) -> Optional[CompanyProfile]:
        if ticker == "BIMAS":
            return CompanyProfile(ticker="BIMAS", name="BIM Birlesik Magazalar", sector="Perakende", industry="Gida Perakendeciligi")
        if ticker == "AKBNK":
            return CompanyProfile(ticker="AKBNK", name="Akbank", sector="Finans", industry="Banka")
        return CompanyProfile(ticker=ticker, name=f"{ticker} AS", sector="Sanayi", industry="Bilinmiyor")
        
    def get_historical_financials(self, ticker: str, years: int = 5) -> List[FinancialSnapshot]:
        # Return mock data for testing deterministic logic
        history = []
        base_year = date.today().year - years
        
        # Use pseudo-random deterministic data based on ticker
        import hashlib
        seed = int(hashlib.md5(ticker.encode()).hexdigest(), 16)
        
        # BIST şirketleri milyar TL seviyesinde ciro yapar. 
        # Hisse fiyatlarının 100-1000 TL aralığında anlamlı DCF üretmesi için revenue'yi büyütüyoruz.
        base_revenue = 20000.0 + (seed % 80000)
        equity = base_revenue * 0.5
        margin = 0.10 + ((seed % 15) / 100.0) # 10% to 25% margin
        growth = 1.05 + ((seed % 10) / 100.0) # 5% to 15% growth
        
        if ticker == "FAIL":
            base_revenue = 100
            equity = -100
            margin = -0.10
            
        for i in range(years):
            revenue = base_revenue * (growth ** i)
            net_income = revenue * margin
            
            history.append(FinancialSnapshot(
                period_date=date(base_year + i, 12, 31),
                revenue=revenue,
                gross_profit=revenue * 0.40,
                net_income=net_income,
                total_assets=revenue * 2,
                total_liabilities=revenue * 0.8,
                total_equity=equity + (net_income * i),
                short_term_debt=revenue * 0.2,
                long_term_debt=revenue * 0.3,
                cash_and_equivalents=revenue * 0.1,
                operating_cash_flow=net_income * 1.2,
                capital_expenditure=net_income * 0.4,
                shares_outstanding=100,
                eps=net_income / 100
            ))
        return history

class YFinanceDataFetcher(BaseDataFetcher):
    """
    Real data fetcher using Yahoo Finance API (yfinance).
    For BIST stocks, the .IS suffix is added.
    """
    def __init__(self):
        self._profile_cache = {}
        self._history_cache = {}

    def get_company_profile(self, ticker: str) -> Optional[CompanyProfile]:
        if ticker in self._profile_cache:
            return self._profile_cache[ticker]
            
        import yfinance as yf
        try:
            ticker_obj = yf.Ticker(f"{ticker}.IS")
            info = ticker_obj.info
            profile = CompanyProfile(
                ticker=ticker,
                name=info.get("longName", f"{ticker} AS"),
                sector=info.get("sector", "Bilinmiyor"),
                industry=info.get("industry", "Bilinmiyor")
            )
            self._profile_cache[ticker] = profile
            return profile
        except Exception as e:
            return None

    def get_historical_financials(self, ticker: str, years: int = 5) -> List[FinancialSnapshot]:
        if ticker in self._history_cache:
            return self._history_cache[ticker]
            
        import yfinance as yf
        import pandas as pd
        try:
            ticker_obj = yf.Ticker(f"{ticker}.IS")
            
            inc_stmt = ticker_obj.financials
            bal_sheet = ticker_obj.balance_sheet
            cash_flow = ticker_obj.cashflow
            
            if inc_stmt is None or inc_stmt.empty:
                self._history_cache[ticker] = []
                return []
                
            info = ticker_obj.info
            # Default to 1M shares if not found, to avoid division by zero
            shares_outstanding = info.get("sharesOutstanding", 1000000)
            
            history = []
            cols = sorted(inc_stmt.columns)
            
            for dt in cols:
                try:
                    def get_val(df, key, default=0.0):
                        if df is not None and key in df.index and dt in df.columns:
                            val = df.loc[key, dt]
                            return float(val) if pd.notna(val) else default
                        return default

                    revenue = get_val(inc_stmt, 'Total Revenue')
                    if revenue == 0:
                        continue
                        
                    snapshot = FinancialSnapshot(
                        period_date=dt.date() if hasattr(dt, 'date') else date.today(),
                        revenue=revenue,
                        gross_profit=get_val(inc_stmt, 'Gross Profit', revenue * 0.4),
                        net_income=get_val(inc_stmt, 'Net Income'),
                        total_assets=get_val(bal_sheet, 'Total Assets', revenue * 2),
                        total_liabilities=get_val(bal_sheet, 'Total Liabilities Net Minority Interest', revenue),
                        total_equity=get_val(bal_sheet, 'Stockholders Equity', get_val(bal_sheet, 'Total Equity Gross Minority Interest', revenue)),
                        short_term_debt=get_val(bal_sheet, 'Current Debt', 0),
                        long_term_debt=get_val(bal_sheet, 'Long Term Debt', get_val(bal_sheet, 'Total Debt', 0)),
                        cash_and_equivalents=get_val(bal_sheet, 'Cash And Cash Equivalents', 0),
                        operating_cash_flow=get_val(cash_flow, 'Operating Cash Flow', get_val(inc_stmt, 'Net Income') * 1.2),
                        capital_expenditure=abs(get_val(cash_flow, 'Capital Expenditure', 0)),
                        shares_outstanding=int(shares_outstanding),
                        eps=get_val(inc_stmt, 'Diluted EPS', get_val(inc_stmt, 'Net Income') / shares_outstanding if shares_outstanding else 0)
                    )
                    history.append(snapshot)
                except Exception:
                    continue
                    
            final_history = history[-years:] if years else history
            self._history_cache[ticker] = final_history
            return final_history
        except Exception as e:
            self._history_cache[ticker] = []
            return []

class EventListener(ABC):
    """
    Listens for KAP new balance sheet events.
    """
    @abstractmethod
    def listen(self, callback):
        pass
