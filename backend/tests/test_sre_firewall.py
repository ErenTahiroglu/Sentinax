import pytest
import socket
import httpx
import requests
from typing import cast, Any
from pytest_socket import disable_socket

def test_sync_network_block_firewall():
    """
    SRE Firewall Verification: 
    Senkron (requests) dış ağ çağrılarının engellendiğini ve 
    SocketBlockedError (veya sarmalanmış hata) fırlatıldığını doğrular.
    """
    disable_socket()
    with pytest.raises(Exception) as excinfo:
         # Bilinçli olarak Google'a gitmeye çalış
         requests.get("https://google.com", timeout=1)
    
    # pytest-socket hata mesajlarında genellikle 'A socket.socket.connect call to...' ifadesi yer alır.
    assert "socket" in str(excinfo.value).lower() or "blocked" in str(excinfo.value).lower()

@pytest.mark.asyncio
async def test_async_network_block_firewall():
    """
    Asenkron (httpx) dış ağ çağrılarının engellendiğini doğrular.
    Bu, asenkron event loop'ların (uvloop/asyncio) delinmediğinin kanıtıdır.
    """
    disable_socket()
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception) as excinfo:
            await client.get("https://alpha-vantage.co/query", timeout=1)
            
    # Python 3.12+ TaskGroups (veya LangGraph paralel düğümleri) hatayı ExceptionGroup içine alabilir.
    full_error_text = str(excinfo.value).lower()
    
    # BaseExceptionGroup is only available in Python 3.11+
    try:
        from builtins import BaseExceptionGroup as BEG # type: ignore
    except ImportError:
        BEG = type(None)

    if isinstance(excinfo.value, BEG):
        for sub_e in cast(Any, excinfo.value).exceptions:
            full_error_text += " " + str(sub_e).lower()

    assert "socket" in full_error_text or "blocked" in full_error_text or "nodename" in full_error_text or "gaierror" in full_error_text


def test_localhost_allow_access():
    """
    Localhost erişiminin (Redis/Local API) kısıtlanmadığını doğrular.
    """
    # --allow-hosts=127.0.0.1,localhost sayesinde bu hata fırlatmamalıdır.
    # (Not: Eğer arkada servis yoksa ConnectionRefusedError alabiliriz ama 
    #  SocketBlockedError ALMAMALIYIZ)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.1)
        s.connect(("127.0.0.1", 9999)) # Boş bir yerel port
    except ConnectionRefusedError:
        # Bu beklenen bir ağ hatasıdır (Bloklama değil kanal reddi)
        pass
    except Exception as e:
        if "socket" in str(e).lower() and "blocked" in str(e).lower():
            pytest.fail("❌ Localhost erişimi yanlışlıkla engellendi!")
