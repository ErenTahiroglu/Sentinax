"""Tests for market_detector.py — Pazar algılama modülü."""

import pytest
from data.market_detector import detect_market


class TestDetectMarketKnownTickers:
    """Bilinen BIST hisselerinin doğru algılanması."""

    @pytest.mark.parametrize("ticker,expected_market", [
        ("THYAO", "TR"),
        ("ASELS", "TR"),
        ("GARAN", "TR"),
        ("AKBNK", "TR"),
    ])
    def test_known_bist_tickers(self, ticker, expected_market):
        market, fetcher, is_tefas = detect_market(ticker)
        assert market == expected_market
        assert fetcher.endswith(".IS")
        assert is_tefas is False

    def test_explicit_is_suffix(self):
        market, fetcher, is_tefas = detect_market("THYAO.IS")
        assert market == "TR"
        assert fetcher == "THYAO.IS"
        assert is_tefas is False


class TestDetectMarketTEFAS:
    """Kısa kodlu TEFAS fonlarının doğru algılanması."""

    @pytest.mark.parametrize("ticker", ["TP2", "ZP8", "AKB"])
    def test_short_codes_are_tefas(self, ticker):
        market, _, is_tefas = detect_market(ticker)
        assert market == "TR"
        assert is_tefas is True


class TestDetectMarketCaseInsensitive:
    """Büyük/küçük harf duyarsızlığı."""

    def test_lowercase_input(self):
        market, fetcher, _ = detect_market("thyao")
        assert market == "TR"
        assert "THYAO" in fetcher


class TestDetectMarketOutOfScope:
    """Kripto paraların kapsam dışı (UNKNOWN) olarak algılanması."""

    @pytest.mark.parametrize("ticker", ["BTC-USD", "ETHUSDT"])
    def test_out_of_scope_tickers_return_unknown(self, ticker):
        """
        Kripto varlıklar Private Engine scope'unda değil.
        detect_market() UNKNOWN döndürmeli, CRYPTO değil.
        """
        market, _, is_tefas = detect_market(ticker)
        assert market == "UNKNOWN"
        assert is_tefas is False

