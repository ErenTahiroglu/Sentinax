from typing import Annotated, TypedDict, List, Dict, Any

def merge_dicts(left: dict, right: dict) -> dict:
    if not left:
        return right.copy()
    if not right:
        return left.copy()
    left.update(right)
    return left

def sliding_window_reducer(left: list, right: list) -> list:
    if not left:
        left = []
    if not right:
        right = []
    # Maximum 5 messages kept in RAM to prevent OOM
    combined = left + right
    return combined[-5:]

class GraphState(TypedDict):
    ticker: str
    market: str
    company_of_interest: str
    turn_count: int
    
    messages: Annotated[List[str], sliding_window_reducer]
    
    market_report: Annotated[Dict, merge_dicts]
    fundamentals_report: Annotated[Dict, merge_dicts]
    news_report: Annotated[Dict, merge_dicts]
    
    investment_debate_state: Annotated[Dict, merge_dicts]
    trader_investment_plan: str
    
    skip_risk_debate: bool
    circuit_breaker_reason: str
    
    risk_debate_state: Annotated[Dict, merge_dicts]
    final_trade_decision: str
    
    # Intent & Execution Metadata
    intent: str # "analyze" | "trade" | "unknown"
    execution_payload: Annotated[Dict[str, Any], merge_dicts]
    
    # Final Analysis Result
    final_report: Annotated[Dict, merge_dicts]

    # Analysis Mode Flags (Modular Analysis)
    check_financials: bool
    use_ai: bool


    # Dynamic Transaction Costs (User-Specific)
    commission_rate: float
    slippage_rate: float
