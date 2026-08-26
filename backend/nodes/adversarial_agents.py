import json
import logging
from typing import List
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

from backend.engine.agent_states import GraphState
from backend.infrastructure.llm_factory import get_quick_think_llm, get_deep_think_llm

logger = logging.getLogger(__name__)

def _stringify_state_reports(state: GraphState) -> str:
    market = state.get("market_report", {})
    fund = state.get("fundamentals_report", {})
    news = state.get("news_report", {})
    if "klines" in market:
        del market["klines"]
    
    comp = {
        "Market": market,
        "Fundamentals": fund,
        "News": news,
    }
    return json.dumps(comp, indent=2, ensure_ascii=False)

async def bull_researcher_node(state: GraphState) -> dict:
    ticker = state.get("company_of_interest", state.get("ticker"))
    reports_str = _stringify_state_reports(state)
    history = state.get("investment_debate_state", {}).get("history", [])
    
    prompt = f"""
    Sen 'Bull (İyimser) Araştırmacı' ajansın. 
    Görevin: {ticker} sembolü için piyasada sadece GÜÇLÜ FIRSATLARI ve BÜYÜME POTANSİYELİNİ savunmak.
    
    [VERİ TABANIN]:
    {reports_str}
    """
    
    messages: List[BaseMessage] = [SystemMessage(content=prompt)]
    for past_turn in history:
        messages.append(HumanMessage(content=past_turn))
        
    if not state.get("use_ai", True):
        content = "AI Analizi (Boğa Araştırmacı) devre dışı."
    else:
        llm = get_quick_think_llm(model_name="gemini-2.5-flash")
        response = await llm.ainvoke(messages)
        content = str(response.content)
    
    return {
        "investment_debate_state": {
            "bull_history": [content],
            "history": [f"[BULL]: {content}"],
            "current_response": content
        }
    }

async def bear_researcher_node(state: GraphState) -> dict:
    ticker = state.get("company_of_interest", state.get("ticker"))
    reports_str = _stringify_state_reports(state)
    
    prompt = f"""
    Sen 'Bear (Kötümser) Araştırmacı' ajansın. 
    Görevin: {ticker} sembolündeki GİZLİ TEHLİKELERİ ve EN KÖTÜ SENARYOLARI ortaya çıkarmak.
    
    [VERİ TABANIN]:
    {reports_str}
    """

    
    messages: List[BaseMessage] = [SystemMessage(content=prompt)]
    for past_turn in (state.get("bull_history", []) or []):
        messages.append(HumanMessage(content=past_turn))
        
    if not state.get("use_ai", True):
        content = "AI Analizi (Ayı Araştırmacı) devre dışı."
    else:
        llm = get_quick_think_llm(model_name="gemini-2.5-flash")
        response = await llm.ainvoke(messages)
        content = str(response.content)
    
    return {
        "investment_debate_state": {
            "bear_history": [content],
            "history": [f"[BEAR]: {content}"],
            "current_response": content
        }
    }

async def research_manager_node(state: GraphState) -> dict:
    ticker = state.get("company_of_interest", state.get("ticker"))
    debate_history = state.get("investment_debate_state", {}).get("history", [])
    
    prompt = f"""
    Sen 'Araştırma Yöneticisi' (Judge) ajansın. {ticker} üzerine Bull ve Bear ajanlarının münazarasını okudun.
    Görevin: Bu tartışmanın kazananını ilan etmek ve uygulanacak 'Teklif (Proposal)' sunmaktır.
    Formatın net, analitik ve tarafsız olsun.
    
    [KUTUPLAŞMIŞ TARTIŞMA GEÇMİŞİ]:
    {chr(10).join(debate_history)}
    """
    
    if not state.get("use_ai", True):
        content = "AI Karar Mekanizması devre dışı. Nötr kalınması önerilir."
    else:
        llm = get_deep_think_llm(model_name="gemini-2.5-pro")
        response = await llm.ainvoke([SystemMessage(content=prompt)])
        content = str(response.content)
    
    return {
        "investment_debate_state": {
            "judge_decision": content
        },
        "turn_count": 0  # Reset for Risk debate
    }

async def trader_node(state: GraphState) -> dict:
    judge_decision = state.get("investment_debate_state", {}).get("judge_decision", "Karar Yok")
    
    prompt = f"""
    Sen 'Uygulayıcı Trader' ajansın. Yöneticinin kararına (Judge_Decision) uygun olarak, 
    risk oranını, alınacak veya satılacak partileri netleştiren spesifik bir İşlem Planı (Execution Plan) taslağı hazırla.
    Judge Kararı: {judge_decision}
    """
    
    if not state.get("use_ai", True):
        content = "AI İşlem Planı devre dışı."
    else:
        llm = get_quick_think_llm(model_name="gemini-2.5-flash")
        response = await llm.ainvoke([SystemMessage(content=prompt)])
        content = str(response.content)
    
    return {
        "trader_investment_plan": content
    }

async def aggressive_analyst_node(state: GraphState) -> dict:
    plan = state.get("trader_investment_plan", "")
    prompt = f"Sen Agresif (High-Risk) Analistsin. Trader'ın planını maks kâr odaklı nasıl patlatırız onu savun.\n[PLAN]: {plan}"
    if not state.get("use_ai", True):
        res_content = "AI Agresif Analiz devre dışı."
    else:
        res = await get_quick_think_llm().ainvoke([SystemMessage(content=prompt)])
        res_content = str(res.content)
    return {"risk_debate_state": {"aggressive_history": [res_content], "history": [f"[AGRESSIVE]: {res_content}"]}}

async def conservative_analyst_node(state: GraphState) -> dict:
    plan = state.get("trader_investment_plan", "")
    hist = state.get("risk_debate_state", {}).get("history", [])
    prompt = f"Sen Muhafazakar (Zero-Risk) Analistsin. Agresifin teklifini reddet, en güvenli defansif planı savun.\n[PLAN/HISTORY]: {plan}\n{hist}"
    if not state.get("use_ai", True):
        res_content = "AI Muhafazakar Analiz devre dışı."
    else:
        res = await get_quick_think_llm().ainvoke([SystemMessage(content=prompt)])
        res_content = str(res.content)
    return {"risk_debate_state": {"conservative_history": [res_content], "history": [f"[CONSERVATIVE]: {res_content}"]}}

async def neutral_analyst_node(state: GraphState) -> dict:
    hist = state.get("risk_debate_state", {}).get("history", [])
    prompt = f"Sen Nötr (Dengeleyici) Analistsin. Hem agresif kârı hem defansif riski harmanla.\n[HISTORY]: {hist}"
    if not state.get("use_ai", True):
        res_content = "AI Nötr Analiz devre dışı."
    else:
        res = await get_quick_think_llm().ainvoke([SystemMessage(content=prompt)])
        res_content = str(res.content)
    return {"risk_debate_state": {"neutral_history": [res_content], "history": [f"[NEUTRAL]: {res_content}"]}}

async def portfolio_manager_node(state: GraphState) -> dict:
    judge = state.get("investment_debate_state", {}).get("judge_decision", "")
    plan = state.get("trader_investment_plan", "")
    
    skip_risk = state.get("skip_risk_debate", False)
    cb_reason = state.get("circuit_breaker_reason", "")
    
    risk_debate = state.get("risk_debate_state", {}).get("history", [])
    
    context = f"""
    Yatırım Yargıcı: {judge}
    Trader Planı: {plan}
    
    {'RİSK TARTIŞMASI DEVRE KESİCİ (CIRCUIT BREAKER) TARAFINDAN ESNESİL (BYPASS) EDİLDİ NEDENİ: ' + cb_reason if skip_risk else 'AĞIR RİSK TARTIŞMASI SONUÇLARI: ' + chr(10).join(risk_debate)}
    """
    
    prompt = f"""
    [TÜM BAĞLAM VE GEÇMİŞ]:
    {context}
    
    [İŞLEM MALİYETLERİ (DR) - KRUİSAL]:
    - Aracı Kurum Komisyonu: %{state.get('commission_rate', 0.002) * 100}
    - Tahmini Kayma (Slippage): %{state.get('slippage_rate', 0.001) * 100}
    - Toplam Tek Yön Maliyet (Friction): %{(state.get('commission_rate', 0.002) + state.get('slippage_rate', 0.001)) * 100}
    
    [MALİYET KALKANI (PROFIT-COST SHIELD)]:
    Eğer önerilen işlemin beklenen kârı, gidiş-dönüş (round-trip) maliyet olan %{(state.get('commission_rate', 0.002) + state.get('slippage_rate', 0.001)) * 200} değerinden düşükse, işlemi yapma ve 'HOLD' (BEKLE) emri ver.
    """
    
    if not state.get("use_ai", True):
        content = "AI Portföy Yönetimi (CIO) devre dışı. Temel finansal veriler ve İslami uygunluk raporu yukarıdadır."
    else:
        res = await get_deep_think_llm("gemini-2.5-pro").ainvoke([SystemMessage(content=prompt)])
        content = str(res.content)
    
    return {
        "final_trade_decision": content,
        "messages": [f"[PORTFOLIO MANAGER]: {content}"]
    }
