import logging
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from backend.engine.agent_states import GraphState

logger = logging.getLogger(__name__)

# LLM ve API gecikmelerine karşı üstel (exponential) retry koruması
@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(Exception)
)
async def market_data_node(state: GraphState) -> dict:
    ticker = state.get("ticker", "Genel")
    if not state.get("check_financials", True):
        logger.info(f"[Bypass] Market data for {ticker} skipped (check_financials=False)")
        return {"market_report": {}}
    
    logger.info(f"[DataNode - Market] Fetching market data for {ticker}")
    
    try:
        from backend.analyzers.bist_analyzer import HisseAnaliz
        analyzer = HisseAnaliz()
        res = analyzer.analiz_et(ticker)
        if res:
            return {
                "market_report": {
                    "market_data": res.get("son_fiyat", {}),
                    "klines": res.get("klines", []),
                    "performance": {
                        "annual": res.get("yg"),
                        "monthly": res.get("ay"),
                        "risk": res.get("risk")
                    }
                }
            }
        
        return {"market_report": {"error": f"{ticker} için veri alınamadı."}}
    except Exception as e:
        logger.error(f"[MarketNode] Fail: {e}")
        return {"market_report": {"error": str(e)}}



@retry(wait=wait_exponential(min=2, max=6), stop=stop_after_attempt(3))
async def news_node(state: GraphState) -> dict:
    ticker = state.get("ticker", "")
    if not state.get("check_financials", True):
         logger.info(f"[Bypass] News for {ticker} skipped (check_financials=False)")
         return {"news_report": {}}
    
    logger.info(f"[DataNode - News] Resolving news for {ticker}")
    return {"news_report": {"sentiment": "Nötr", "haberler": ["Şirket bilançosu iyi geldi", "Sektörel daralma endişesi"]}}
