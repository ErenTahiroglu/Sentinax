"""
backend/engine/private/precious_metals/constants.py
===================================================
Domain constants and enumerations for Precious Metals (Gold / Silver) Market Backbone.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum


class PreciousMetalType(Enum):
    """Supported precious metals in Sentinax market backbone."""
    GOLD = "GOLD"
    SILVER = "SILVER"


class PreciousMetalMarket(Enum):
    """Originating or disseminating market venue."""
    BIST_KMTP = "BIST_KMTP"  # Borsa İstanbul Kıymetli Madenler ve Kıymetli Taşlar Piyasası
    TCMB_EVDS = "TCMB_EVDS"  # TCMB Elektronik Veri Dağıtım Sistemi


class PreciousMetalUnit(Enum):
    """Physical quantity weight/mass unit."""
    KG = "KG"              # Kilogram (1000 grams)
    TROY_OZ = "TROY_OZ"    # Troy Ounce (precious metals standard oz)
    GRAM = "GRAM"          # Gram


class PreciousMetalPriceType(Enum):
    """
    Market price basis/type as exposed by the originating exchange or dissemination authority.
    """
    REFERENCE = "REFERENCE"                  # Official exchange daily reference benchmark (e.g. BIST Referans Fiyat)
    METAL_PRICE = "METAL_PRICE"              # Official daily metal price benchmark (e.g. BIST Metal Fiyatı)
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE"    # Volume-weighted average price (AOF / WAP)
    HIGH = "HIGH"                            # Daily highest executed transaction price
    LOW = "LOW"                              # Daily lowest executed transaction price
    CLOSE = "CLOSE"                          # Daily closing transaction price
    FIXING = "FIXING"                        # Fixing session benchmark price
    AOF_2 = "AOF_2"                          # Secondary weighted average benchmark (AOF-2)


# Discovery & Host Whitelisting
BIST_KMTP_DATA_URL = "https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri"
BIST_DATA_FILE_PATHS_URL = "https://www.borsaistanbul.com/files/DataFilePaths.zip"
BIST_OFFICIAL_HOSTS = frozenset({"www.borsaistanbul.com", "borsaistanbul.com"})

# Manifest Mapping Keys
BIST_KMTP_MANIFEST_KEY_TR = "Kıymetli Madenler Piyasası Günlük Bülten"
BIST_KMTP_MANIFEST_KEY_EN = "Precious Metals Market Bulletins Daily Bulletin"

# Provider Identifiers
BIST_KMTP_PROVIDER_NAME = "BIST_KMTP"
BIST_KMTP_PROVIDER_VERSION = "1.0.0"
TCMB_EVDS_PROVIDER_NAME = "TCMB_EVDS"
