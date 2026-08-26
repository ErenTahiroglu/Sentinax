"""
backend/services/analysis_service.py
======================================
Service katmanı. Eski portfolio optimization endpoint'i kaldırıldı.
optimize_portfolio (Monte-Carlo MVO) deprecated/ klasörüne taşındı.

Private Engine allocation engine ileride ayrı bir module olarak yazılacak.
"""
import asyncio
from typing import List, Optional, Dict

from fastapi import HTTPException


async def calculate_portfolio_risk_service(tickers: List[str], weights: Optional[Dict[str, float]]):
    """
    Portfolio risk metrics placeholder.
    Risk analyzer logic integrated into individual analyzer reports.
    """
    return {
        "status": "success",
        "message": "Risk analysis metrics integrated into individual analyzer reports."
    }
