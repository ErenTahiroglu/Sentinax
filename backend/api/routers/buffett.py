"""Public Buffett research API.

Phase 25D0: fail closed. No qualified, source-bound research evidence exists
yet, so every endpoint returns an explicit UNAVAILABLE state with empty data.
No provider, engine, LLM or network call is made to build these responses.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Response

router = APIRouter(prefix="/buffett", tags=["Buffett Engine"])

_REASON_CODE = "NO_QUALIFIED_EVIDENCE"
_REASON = (
    "Kaynağı doğrulanmış, kullanım hakkı onaylı araştırma kanıtı henüz yok; "
    "fiyat, skor, değerleme ve geçmiş verisi gösterilmiyor."
)
_WARNINGS = [
    "Bu ekran bilgilendirme amaçlıdır; yatırım tavsiyesi veya emir değildir.",
    "Üretilmiş veya varsayılan finansal veri gösterilmez.",
]


def _unavailable(items_key: str, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {
        "data_status": "UNAVAILABLE",
        "reason_code": _REASON_CODE,
        "reason": _REASON,
        items_key: [],
        "as_of": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warnings": list(_WARNINGS),
    }


@router.get("/portfolio")
async def get_buffett_portfolio(response: Response):
    return _unavailable("portfolio", response)


@router.get("/history")
async def get_buffett_history(response: Response):
    return _unavailable("changes", response)
