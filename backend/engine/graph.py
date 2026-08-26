import logging
import re
from typing import List, cast
from pydantic import BaseModel, Field
from langgraph.graph import START, END, StateGraph

from backend.engine.agent_states import GraphState
from backend.infrastructure.llm_factory import get_quick_think_llm

from backend.nodes.data_nodes import (
    market_data_node,
    news_node
)

async def islamic_node(state) -> dict:  # islamic_analyzer removed — no-op stub
    """Islamic analysis removed from scope. Returns empty report."""
    return {"islamic_report": {}}

from backend.engine.circuit_breaker import evaluate_risk_circuit_breaker

from backend.nodes.adversarial_agents import (
    bull_researcher_node,
    bear_researcher_node,
    research_manager_node,
    trader_node,
    aggressive_analyst_node,
    conservative_analyst_node,
    neutral_analyst_node,
    portfolio_manager_node
)

logger = logging.getLogger(__name__)

MAX_TURNS = 3
TOKEN_LIMIT = 4000

def route_investment_debate(state: GraphState) -> str:
    turn_count = state.get("turn_count", 0)
    
    if turn_count >= MAX_TURNS:
        logger.info(f"[LOOP BREAKER] Investment Debate max tur limitine ({MAX_TURNS}) ulaştı. Yargıca (Manager) aktarılıyor.")
        return "Research Manager"
        
    return "Bull Researcher"
    

def route_risk_debate(state: GraphState) -> str:
    turn_count = state.get("turn_count", 0)
    
    if turn_count >= MAX_TURNS:
        logger.info(f"[LOOP BREAKER] Risk Debate max tur limitine ({MAX_TURNS}) ulaştı. Nihai Kararcıya (CIO) aktarılıyor.")
        return "Portfolio Manager"
        
    return "Aggressive Analyst"


def route_circuit_breaker(state: GraphState) -> str:
    decision = evaluate_risk_circuit_breaker(state)
    logger.info(f"[ROUTER] Circuit Breaker decision: {decision}")
    if decision == "bypass_risk_debate":
        return "Portfolio Manager"
    return "Aggressive Analyst"


class SummarizedDebate(BaseModel):
    korunan_metrikler: List[str] = Field(description="Rakamlar ve metrikler. Finansal veri yoksa boş liste döndürün.")
    boga_argumanlari: List[str] = Field(description="İyimser/Agresif tarafın temel argüman maddeleri")
    ayi_argumanlari: List[str] = Field(description="Kötümser/Defansif tarafın temel argüman ve risk maddeleri")
    uzlasma_noktalari: str = Field(description="Tarafların ortak kabullendiği piyasa gerçekleri")

async def summarizer_node(state: GraphState) -> dict:
    inv_hist = state.get("investment_debate_state", {}).get("history", [])
    risk_hist = state.get("risk_debate_state", {}).get("history", [])
    
    if not state.get("use_ai", True):
        return {"messages": ["Kısa özet: AI devre dışı bırakıldı."]}
        
    if len(inv_hist) < 4 and len(risk_hist) < 4:
        return {}
        
    prompt = f"""
    Sen sistemin DİKKATLİ ÖZETLEYİCİSİSİN (Strict State Summarizer).
    Görevin: Aşağıda geçen uzun tartışmayı kompakt bir Pydantic JSON yapısına çevirmek.
    
    [YATIRIM TARTIŞMASI]:
    {chr(10).join(inv_hist[-6:]) if inv_hist else 'Yok'}
    
    [RİSK TARTIŞMASI]:
    {chr(10).join(risk_hist[-6:]) if risk_hist else 'Yok'}
    """
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            llm = get_quick_think_llm(model_name="gemini-2.5-flash")
            structured_llm = llm.with_structured_output(SummarizedDebate)
            res_data = await structured_llm.ainvoke(prompt)
            if not res_data or not isinstance(res_data, SummarizedDebate):
                raise ValueError("Model valid bir özet döndüremedi.")
            
            res = cast(SummarizedDebate, res_data)
            summary_text = f"[SIKIŞTIRILMIŞ BAĞLAM]\nMetrikler: {res.korunan_metrikler}\nBoğa: {res.boga_argumanlari}\nAyı: {res.ayi_argumanlari}\nUzlaşma: {res.uzlasma_noktalari}"
            logger.info("[PRUNING] State context strictly compressed via Pydantic Schema.")
            
            return {
                "messages": [summary_text]
            }
        except Exception as e:
            from pydantic import ValidationError
            if isinstance(e, ValidationError):
                from backend.api.metrics import SUMMARIZER_VALIDATION_ERROR_TOTAL
                logger.warning(f"Summarizer Schema Validation Error (Attempt {attempt+1}/{max_retries}): {e}")
                SUMMARIZER_VALIDATION_ERROR_TOTAL.labels(agent_node="SummarizerNode").inc()
                # Feed the error back to the LLM
                prompt += f"\n\n[SİSTEM UYARISI - ÖNCEKİ DENEME BAŞARISIZ]: Lütfen JSON şemasına tam uyunuz. Aldığınız Hata: {e}"
                continue
            
            logger.error(f"Summarizer failed unexpectedly: {e}")
            break

    # If all retries failed, force HOLD state to prevent corrupted context propagation
    logger.error("🛑 Summarizer failed to validate schema after max retries. Forcing secure HOLD mode.")
    return {
        "final_trade_decision": "[HOLD] Sistem güvenlik amacıyla işlemi askıya aldı (JSON Context Error).",
        "messages": ["[HOLD] Şema doğrulama (Validation) kalıcı olarak çöktü. Tüm kararlar iptal edildi."],
        "turn_count": 0  # Reset for Risk debate
    }

async def intent_detector_node(state: GraphState) -> dict:
    """
    🎯 [IntentNode]: Business Logic sığınağı.
    Router'daki kirli Regex'ler buraya taşınmıştır.
    """
    messages = state.get("messages", [])
    user_msg = messages[-1] if messages else ""
    
    match_current = re.search(r'Mevcut Varlık Dağılımım:\s*(\{.*?\})', user_msg)
    match_optimal = re.search(r'Matematiksel Optimum Dağılım:\s*(\{.*?\})', user_msg)
    
    if match_current and match_optimal:
        try:
             import json
             curr_weights = json.loads(match_current.group(1).replace("'", '"'))
             opt_weights = json.loads(match_optimal.group(1).replace("'", '"'))
             return {
                 "intent": "trade",
                 "execution_payload": {"current": curr_weights, "optimal": opt_weights}
             }
        except Exception:
             pass
             
    return {"intent": "analyze", "execution_payload": {}}

async def output_mapper_node(state: GraphState) -> dict:
    """
    🧹 [OutputMapperNode]: Legacy Uyumluluk Katmanı.
    GraphState (JSON) yapısını eski frontend'in beklediği formata dönüştürür.
    """
    return {
        "final_report": {
            "summary": state.get("messages")[-1] if state.get("messages") else "",
            "trade_decision": state.get("final_trade_decision"),
            "ticker": state.get("ticker"),
            "market_data": state.get("market_report", {}).get("market_data"),
            "klines": state.get("market_report", {}).get("klines", []),
            "intent": state.get("intent"),
            "check_financials": state.get("check_financials", True)
        }
    }


async def data_sync_node(state: GraphState) -> dict:
    """
    📡 [DataSyncNode]: Paralel dalların (Market, Islamic, News) 
    birleştiği senkronizasyon noktası. Fan-In pattern.
    """
    logger.info("📡 Veri toplama dalları birleşiyor...")
    return {"messages": ["Paralel veri analizi tamamlandı, sentez aşamasına geçiliyor."]}


async def data_join_and_circuit_node(state: GraphState) -> dict:
    """
    🔄 [DataJoinAndCircuit]: Yatırım münazarası döngü sayacı.
    """
    turn_count = state.get("turn_count", 0)
    return {"turn_count": turn_count + 1}

async def risk_join_and_circuit_node(state: GraphState) -> dict:
    """
    🛡️ [RiskJoinAndCircuit]: Risk münazarası döngü sayacı.
    """
    turn_count = state.get("turn_count", 0)
    # Eğer ilk defa giriliyorsa (Yatırım'dan geliniyorsa) 100 bandına alabiliriz
    # veya sadece artırabiliriz. Burada MAX_TURNS ortak kullanıldığı için 
    # Risk döngüsünün başında turn_count'u resetlemek en iyisidir.
    return {"turn_count": turn_count + 1}

# Her bir düğüm (Node) kendi fonksiyonunu, takip eden kenarlarını (Edges)
# ve Koşullu Yönlendirme (Conditional Edges) mantığını burada tanımlar.
# Bu yapı sayesinde yeni bir Ajan/DataNode eklemek için compile() koduna 
# dokunmaya gerek kalmaz; sadece bu listeye ekleme yapmak yeterlidir.
# ─────────────────────────────────────────────────────────────────────────

NODE_REGISTRY = {  # noqa: F841
    "IntentNode": {
        "func": intent_detector_node,
        "edges": ["MarketNode", "IslamicNode", "NewsNode"]
    },
    "MarketNode": {
        "func": market_data_node,
        "edges": ["DataSyncNode"]
    },
    "IslamicNode": {
        "func": islamic_node,
        "edges": ["DataSyncNode"]
    },
    "NewsNode": {
        "func": news_node,
        "edges": ["DataSyncNode"]
    },
    "DataSyncNode": {
        "func": data_sync_node,
        "edges": ["DataJoinAndCircuit"]
    },
    "DataJoinAndCircuit": {
        "func": data_join_and_circuit_node,
        "edges": ["Bull Researcher"]
    },
    "Bull Researcher": {
        "func": bull_researcher_node,
        "edges": ["Bear Researcher"]
    },
    "Bear Researcher": {
        "func": bear_researcher_node,
        "conditional_edges": {
            "router": route_investment_debate,
            "mapping": {
                "Bull Researcher": "DataJoinAndCircuit",
                "Research Manager": "Research Manager"
            }
        }
    },
    "Research Manager": {
        "func": research_manager_node,
        "edges": ["Trader"]
    },
    "Trader": {
        "func": trader_node,
        "conditional_edges": {
            "router": route_circuit_breaker,
            "mapping": {
                "Aggressive Analyst": "RiskJoinAndCircuit",
                "Portfolio Manager": "Portfolio Manager" 
            }
        }
    },
    "Aggressive Analyst": {
        "func": aggressive_analyst_node,
        "edges": ["Conservative Analyst"]
    },
    "Conservative Analyst": {
        "func": conservative_analyst_node,
        "edges": ["Neutral Analyst"]
    },
    "RiskJoinAndCircuit": {
        "func": risk_join_and_circuit_node,
        "edges": ["Aggressive Analyst"]
    },
    "Neutral Analyst": {
        "func": neutral_analyst_node,
        "conditional_edges": {
            "router": route_risk_debate,
            "mapping": {
                "Aggressive Analyst": "RiskJoinAndCircuit",
                "Portfolio Manager": "Portfolio Manager"
            }
        }
    },
    "Portfolio Manager": {
        "func": portfolio_manager_node,
        "edges": ["SummarizerPruner"]
    },
    "SummarizerPruner": {
        "func": summarizer_node,
        "edges": ["OutputMapper"]
    },
    "OutputMapper": {
        "func": output_mapper_node,
        "edges": [END]
    }
}

def compile_trading_graph():
    """
    🏗️ [The Builder]: Registry'den Graf Ören Döngü.
    Hiçbir düğüm veya kenar ismine bağımlı değildir.
    """
    workflow = StateGraph(GraphState)
    
    # 1. Düğümleri (Nodes) Ekle
    for node_name, config in NODE_REGISTRY.items():
        workflow.add_node(node_name, config["func"])
        
    # 2. Giriş (Entry Points)
    workflow.add_edge(START, "IntentNode")
    
    # 3. Kenarları (Edges) ve Koşulları (Conditionals) Registry'den Çekip Ör
    for node_name, config in NODE_REGISTRY.items():
        # Statik Kenarlar
        for dest in config.get("edges", []):
            workflow.add_edge(node_name, dest)
            
        # Koşullu Kenarlar (Conditional Edges)
        cond = config.get("conditional_edges")
        if cond:
            workflow.add_conditional_edges(
                node_name,
                cond["router"],
                cond["mapping"]
            )
            
    return workflow.compile()
