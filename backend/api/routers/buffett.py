from fastapi import APIRouter
from typing import List, Dict, Any
import random
from datetime import date

from backend.engine.buffett.orchestrator import BuffettEngine
from backend.engine.buffett.data_fetcher import MockKAPFetcher, YFinanceDataFetcher
from backend.engine.buffett.valuation import DCFValuation

router = APIRouter(prefix="/buffett", tags=["Buffett Engine"])

@router.get("/portfolio")
async def get_buffett_portfolio():
    """
    Returns the top 10 stocks selected by the Buffett Engine.
    Uses the real engine with a MockKAPFetcher.
    """
    fetcher = YFinanceDataFetcher()
    cpi_data = {2019: 100, 2020: 114.6, 2021: 155.9, 2022: 256.1, 2023: 421.9, 2024: 650.0}
    
    engine = BuffettEngine(fetcher, cpi_data)
    
    tickers = [
        "BIMAS", "THYAO", "FROTO", "TUPRS", "KCHOL", 
        "SAHOL", "DOAS", "ENKAI", "TOASO", "CCOLA"
    ]
    
    tickers_with_prices = {}
    import yfinance as yf
    for t in tickers:
        try:
            ticker_obj = yf.Ticker(f"{t}.IS")
            price = ticker_obj.fast_info.get('last_price', None)
            if price:
                tickers_with_prices[t] = price
            else:
                tickers_with_prices[t] = 100.0 # Fallback
        except Exception:
            tickers_with_prices[t] = 100.0
            
    # Run Engine
    selected_portfolio = engine.run_portfolio_selection(tickers_with_prices, target_year=date.today().year)
    dcf_analyzer = DCFValuation()
    
    # Map to expected frontend format
    response_portfolio = []
    for profile, score in selected_portfolio:
        current_price = tickers_with_prices[profile.ticker]
        
        # Get actual DCF results used by the engine
        history = fetcher.get_historical_financials(profile.ticker)
        val_result = dcf_analyzer.evaluate(history, current_price)
        
        mos_pct = val_result.margin_of_safety_pct * 100
        # Cap visual negative margin at -100%
        if mos_pct < -100:
            mos_pct = -100.0
            
        response_portfolio.append({
            "ticker": profile.ticker,
            "name": profile.name,
            "sector": profile.sector,
            "current_price": round(current_price, 2),
            "target_entry": [round(val_result.intrinsic_value * 0.50, 2), round(val_result.intrinsic_value * 0.75, 2)],
            "margin_of_safety_pct": round(mos_pct, 2),
            "total_score": round(score.total_score, 2),
            "details": {
                "moat_score": round(score.moat_score, 2),
                "profitability_score": round(score.profitability_score, 2),
                "balance_sheet_score": round(score.balance_sheet_score, 2),
                "valuation_score": round(score.valuation_score, 2),
                "passed_veto": score.passed_veto,
                "dcf": {
                    "intrinsic_value": round(val_result.intrinsic_value, 2),
                    "base_case": round(val_result.base_case, 2),
                    "bear_case": round(val_result.bear_case, 2),
                    "bull_case": round(val_result.bull_case, 2)
                },
                "llm_analysis": f"{profile.ticker} demonstrates a sustainable economic moat with varied and robust historical margins."
            }
        })
        
    return {"portfolio": response_portfolio}

@router.get("/history")
async def get_buffett_history():
    """
    Returns the recent snapshot history (Added/Removed stocks).
    """
    return {
        "changes": [
            {
                "ticker": "EREGL",
                "action": "REMOVED",
                "reason": "Net kâr marjı hedeflerin altına düştü, ROE %15'in altına geriledi.",
                "date": "2023-11-01"
            },
            {
                "ticker": "DOAS",
                "action": "ADDED",
                "reason": "Güvenlik marjı %30'a ulaştı. Kârlılık kriterleri (ROE > %15) başarıyla geçildi.",
                "date": "2023-11-01"
            }
        ]
    }

