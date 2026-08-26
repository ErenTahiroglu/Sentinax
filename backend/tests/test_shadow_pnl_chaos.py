import pytest
from backend.api.models import UserSettingsRequest
from pydantic import ValidationError
from unittest.mock import patch, MagicMock
from decimal import Decimal

pytestmark = pytest.mark.skip(
    reason="shadow_pnl_tracker moved to backend/deprecated/. Preserved for reference."
)

try:
    from backend.deprecated.shadow_pnl_tracker import _get_user_rates
except ImportError:
    _get_user_rates = None  # type: ignore


from unittest.mock import patch, MagicMock
from decimal import Decimal
import httpx

# ── 1. API Level Validation (Pydantic / ge=0) ───────────────────────────────

def test_negative_rates_rejection():
    """Negatif komisyon veya kayma oranlarının API katmanında reddedildiğini doğrular."""
    with pytest.raises(ValidationError):
        UserSettingsRequest(commission_rate=-0.001, slippage_rate=0.001)
    
    with pytest.raises(ValidationError):
        UserSettingsRequest(commission_rate=0.001, slippage_rate=-0.01)

def test_type_mismatch_rejection():
    """Sayı yerine string gönderildiğinde Pydantic'in strict modda reddetmesini doğrular."""
    # strict=True olduğu için pydantic "0.002" stringini kabul etmez
    with pytest.raises(ValidationError):
         UserSettingsRequest(commission_rate="0.002", slippage_rate=0.001)  # type: ignore

# ── 2. Database Fallback (Resilience) ───────────────────────────────────────

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_supabase_down_fallback(mock_get):
    """
    Veritabanı bağlantısı koptuğunda (500 veya Timeout) 
    varsayılan %0.2 / %0.1 oranlarına dönüldüğünü doğrular.
    """
    mock_get.side_effect = httpx.ConnectError("Connection Refused")
    
    client = httpx.AsyncClient()
    headers = {"apikey": "fake"}
    
    comm, slip = await _get_user_rates(client, headers, "user_123")
    
    # Beklenen: Varsayılan (Safe Default) değerler
    assert comm == Decimal("0.002")
    assert slip == Decimal("0.001")

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_empty_settings_fallback(mock_get):
    """
    Kullanıcı kaydı var ama oranlar boşsa varsayılana dönüldüğünü doğrular.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [] # Kayıt yok (boş liste)
    mock_get.return_value = mock_resp
    
    client = httpx.AsyncClient()
    headers = {"apikey": "fake"}
    
    comm, slip = await _get_user_rates(client, headers, "user_new")
    
    assert comm == Decimal("0.002")
    assert slip == Decimal("0.001")
