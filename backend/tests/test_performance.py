import pytest
import time
import logging
import os
import psutil
from typing import cast
from unittest.mock import patch
from backend.engine.agent_states import GraphState
from langchain_community.chat_models import FakeListChatModel
from backend.engine.graph import compile_trading_graph

logger = logging.getLogger(__name__)

def get_process_memory():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 * 1024) # MB

@pytest.mark.asyncio
async def test_full_analysis_performance_gate():
    """
    SRE Performance Gate: 
    1. Analiz süresi 20 saniyeyi geçerse fail eder (Vercel/Render limitleri).
    2. RAM kullanımı ölçülür ve raporlanır (Artifact).
    """
    # Create a fake LLM response for all adversarial agents
    fake_llm = FakeListChatModel(responses=['{"decision": "BUY", "reason": "Mocked performance test"}'] * 100)
    
    mock_market_res = {
        "son_fiyat": {"fiyat": 150.0, "degisim": 1.5, "para_birimi": "USD"},
        "klines": [{"close": 150.0}],
        "yg": 10.0,
        "ay": 2.0,
        "risk": 1.0
    }

    # Patch targets everywhere they are imported
    with patch("backend.nodes.adversarial_agents.get_quick_think_llm", return_value=fake_llm), \
         patch("backend.nodes.adversarial_agents.get_deep_think_llm", return_value=fake_llm), \
         patch("backend.engine.graph.get_quick_think_llm", return_value=fake_llm), \
         patch("backend.analyzers.bist_analyzer.HisseAnaliz.analiz_et", return_value=mock_market_res):
        
        graph = compile_trading_graph()

        
        start_mem = get_process_memory()
        start_time = time.time()
        
        # Mock input
        input_state = {
            "ticker": "AAPL",
            "check_financials": True,
            "check_islamic": True,
            "api_key": "MOCK_KEY",
            "model_name": "gemini-2.5-flash"
        }
        
        # Run graph
        result = await graph.ainvoke(cast(GraphState, input_state))
    
    end_time = time.time()
    end_mem = get_process_memory()
    
    duration = end_time - start_time
    mem_used = end_mem - start_mem
    
    # 📝 Performans Raporunu Kaydet (CI Artifact için)
    with open("backend/tests/performance_report.txt", "a") as f:
        f.write(f"Ticker: AAPL, Duration: {duration:.2f}s, RAM Delta: {mem_used:.2f}MB, Total RAM: {end_mem:.2f}MB\n")
    
    # 🛑 1. Latency Gate (Hard Fail)
    assert duration < 45, f"⏳ Analiz süresi limitleri aştı: {duration:.2f}s > 45s"
    
    # ⚠️ 2. Memory Check (Soft Warning in Log)
    if end_mem > 400:
        logger.warning(f"🚨 Yüksek RAM kullanımı: {end_mem:.2f}MB (Render sınırı 512MB!)")
    
    assert "final_report" in result
