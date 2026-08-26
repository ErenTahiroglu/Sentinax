import json
import logging
from typing import Optional
from langchain_core.messages import SystemMessage, HumanMessage
from backend.infrastructure.llm_factory import get_quick_think_llm

logger = logging.getLogger(__name__)

def generate_macro_advice(portfolio: list, api_key: str, model: str, lang: str, user_id: Optional[str] = None):
    """
    Portföy geneline makro ekonomik bakış açısıyla tavsiye verir (Streaming).
    """
    llm = get_quick_think_llm(model_name=model, api_key=api_key)
    
    prompt = f"""
    Sen kıdemli bir Yatırım Stratejistisin. Aşağıdaki portföyü makro ekonomik dengeler, 
    enflasyon beklentileri ve sektörel riskler açısından analiz et.
    Kullanıcıya kısa, öz ve aksiyon alınabilir bir makro rapor sun.
    Dil: {lang}
    
    Portföy: {json.dumps(portfolio, ensure_ascii=False)}
    """
    
    # Streaming implementation
    for chunk in llm.stream([SystemMessage(content=prompt)]):
        yield str(chunk.content)

def generate_wizard_portfolio(prompt: str, api_key: str, model: str, lang: str, user_id: Optional[str] = None):
    """
    Kullanıcının metinsel isteğinden (örn: "Riskim düşük olsun, teknoloji sevmem") 
    örnek bir portföy (ticker listesi ve ağırlıklar) üretir.
    """
    llm = get_quick_think_llm(model_name=model, api_key=api_key)
    
    system_prompt = f"""
    Sen bir Portföy Oluşturma Sihirbazısın. Kullanıcının kriterlerine göre 
    mantıklı bir varlık dağılımı (hisse/fon) öner.
    Çıktın SADECE JSON formatında olmalı.
    Örnek format: {{"tickers": [{{ "ticker": "AAPL", "weight": 20 }}, {{ "ticker": "THYAO", "weight": 30 }}], "reasoning": "Açıklama" }}
    Dil: {lang}
    """
    
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=prompt)
    ])
    
    try:
        # Clean potential markdown
        content = str(response.content).replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        logger.error(f"Wizard JSON parse error: {e}")
        return {"tickers": [], "reasoning": "Portföy oluşturulurken bir hata oluştu.", "error": str(e)}

def analyze_news_sentiment(news_items: list, api_key: str = "", check_islamic: bool = False):
    """
    Haber listesinin piyasa duyarlılığını analiz eder.
    """
    llm = get_quick_think_llm(api_key=api_key)
    
    prompt = f"""
    Aşağıdaki haber başlıklarını analiz et ve toplu bir duyarlılık skoru (0-100) ile etiket üret.
    JSON formatında dön: {{"score": 75, "sentiment_label": "Pozitif", "risk_reason": ""}}
    
    Haberler: {json.dumps(news_items, ensure_ascii=False)}
    """
    
    response = llm.invoke([SystemMessage(content=prompt)])
    try:
        content = str(response.content).replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception:
        return {"score": 50, "sentiment_label": "Nötr", "risk_reason": "Parse error"}

