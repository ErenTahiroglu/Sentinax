"""
backend/engine/private/precious_metals — Precious Metals (Gold / Silver) Market Backbone
"""

from backend.engine.private.precious_metals.comparator import (
    PreciousMetalCrossSourceComparator,
)
from backend.engine.private.precious_metals.constants import (
    BIST_DATA_FILE_PATHS_URL,
    BIST_KMTP_DATA_URL,
    BIST_KMTP_MANIFEST_KEY_EN,
    BIST_KMTP_MANIFEST_KEY_TR,
    BIST_KMTP_PROVIDER_NAME,
    BIST_KMTP_PROVIDER_VERSION,
    BIST_OFFICIAL_HOSTS,
    TCMB_EVDS_PROVIDER_NAME,
    PreciousMetalMarket,
    PreciousMetalPriceType,
    PreciousMetalType,
    PreciousMetalUnit,
)
from backend.engine.private.precious_metals.locator import (
    BISTPreciousMetalsBulletinLocator,
)
from backend.engine.private.precious_metals.models import (
    ComparabilityResult,
    ComparabilityStatus,
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
    PreciousMetalSeriesDefinition,
    PreciousMetalSnapshot,
)
from backend.engine.private.precious_metals.parser import (
    BISTKMTPBulletinParser,
    BISTKMTPParserError,
    BISTKMTPSchemaDriftError,
    parse_kmtp_decimal,
    parse_kmtp_int,
    parse_unit_and_currency,
)
from backend.engine.private.precious_metals.registry import (
    PreciousMetalSeriesRegistry,
)

__all__ = [
    # Comparator
    "PreciousMetalCrossSourceComparator",
    # Constants
    "BIST_DATA_FILE_PATHS_URL",
    "BIST_KMTP_DATA_URL",
    "BIST_KMTP_MANIFEST_KEY_EN",
    "BIST_KMTP_MANIFEST_KEY_TR",
    "BIST_KMTP_PROVIDER_NAME",
    "BIST_KMTP_PROVIDER_VERSION",
    "BIST_OFFICIAL_HOSTS",
    "TCMB_EVDS_PROVIDER_NAME",
    "PreciousMetalMarket",
    "PreciousMetalPriceType",
    "PreciousMetalType",
    "PreciousMetalUnit",
    # Locator
    "BISTPreciousMetalsBulletinLocator",
    # Models
    "ComparabilityResult",
    "ComparabilityStatus",
    "PreciousMetalMarketObservation",
    "PreciousMetalObservationStatus",
    "PreciousMetalSeriesDefinition",
    "PreciousMetalSnapshot",
    # Parser
    "BISTKMTPBulletinParser",
    "BISTKMTPParserError",
    "BISTKMTPSchemaDriftError",
    "parse_kmtp_decimal",
    "parse_kmtp_int",
    "parse_unit_and_currency",
    # Registry
    "PreciousMetalSeriesRegistry",
]
