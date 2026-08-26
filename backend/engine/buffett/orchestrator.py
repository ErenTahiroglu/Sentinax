from typing import List, Tuple
from .models import CompanyProfile, BuffettScore, FinancialSnapshot
from .data_fetcher import BaseDataFetcher
from .inflation_adjuster import InflationAdjuster
from .moat_analyzer import MoatAnalyzer
from .profitability_analyzer import ProfitabilityAnalyzer
from .balance_sheet_analyzer import BalanceSheetAnalyzer
from .valuation import DCFValuation
from .scoring_model import ScoringModel
from .selection_manager import SelectionManager
from .llm_integration import LLMMoatAnalyzer
from .supabase_logger import SnapshotLogger

class BuffettEngine:
    """
    Orchestrates the entire Buffett Stock Selection process.
    """
    
    def __init__(self, 
                 fetcher: BaseDataFetcher, 
                 cpi_data: dict, 
                 db_client=None, 
                 ai_agent=None):
        
        self.fetcher = fetcher
        self.inflation_adjuster = InflationAdjuster(cpi_data)
        
        self.moat_analyzer = MoatAnalyzer()
        self.profit_analyzer = ProfitabilityAnalyzer()
        self.bs_analyzer = BalanceSheetAnalyzer()
        self.dcf = DCFValuation()
        
        self.scoring = ScoringModel()
        self.selection = SelectionManager()
        
        self.llm = LLMMoatAnalyzer(ai_agent)
        self.logger = SnapshotLogger(db_client)

    def analyze_company(self, ticker: str, current_price: float, target_year: int = None) -> Tuple[CompanyProfile, BuffettScore]:
        profile = self.fetcher.get_company_profile(ticker)
        if not profile:
            raise ValueError(f"Company profile not found for {ticker}")
            
        raw_history = self.fetcher.get_historical_financials(ticker)
        
        # Phase 2: Inflation Adjustment
        history = self.inflation_adjuster.adjust_history(raw_history, target_year)
        
        # Phase 2: Analyzers
        moat_res = self.moat_analyzer.analyze_moat(history, profile)
        profit_res = self.profit_analyzer.analyze_profitability(history)
        
        if history:
            bs_res = self.bs_analyzer.analyze(history[-1], profile)
        else:
            bs_res = {"score": 0.0, "passed_veto": False, "is_financial_bypass": False}
            
        val_res = self.dcf.evaluate(history, current_price)
        val_score = self.dcf.score(val_res)
        
        # Phase 4: Qualitative LLM
        news_context = "Mock news context" # In reality, fetch from news API
        llm_moat_adj = self.llm.analyze_moat_qualitative(profile, news_context)
        
        # Phase 3: Final Scoring
        score = self.scoring.generate_score(
            profile=profile,
            history=history,
            moat_score=moat_res["score"],
            profit_score=profit_res["score"],
            bs_score=bs_res["score"],
            val_score=val_score,
            bs_passed_veto=bs_res["passed_veto"],
            llm_moat_adjustment=llm_moat_adj
        )
        
        return profile, score

    def run_portfolio_selection(self, tickers_with_prices: dict, target_year: int = None):
        """
        Runs the engine on a batch of tickers and selects the top 10.
        :param tickers_with_prices: Dict mapping ticker to current market price.
        """
        analyzed = []
        for ticker, price in tickers_with_prices.items():
            try:
                result = self.analyze_company(ticker, price, target_year)
                analyzed.append(result)
            except Exception as e:
                print(f"Failed to analyze {ticker}: {e}")
                
        # Snapshot
        self.logger.log_snapshot(analyzed)
        
        # Selection
        portfolio = self.selection.select_portfolio(analyzed)
        
        return portfolio
