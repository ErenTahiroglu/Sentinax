"""Private owner-only portfolio read API (Phase 25D1E1).

First guarded private route: GET /private/portfolio/summary. It is read-only, owner-only (verified signed Supabase JWT, exact operator-configured
owner, AAL2 with signed TOTP evidence, via the existing require_private_owner dependency) and, in this phase, ALWAYS answers UNAVAILABLE: no qualified
live data source is connected, so no database, provider, service-role or network call is made and nothing is fabricated.

EMPTY means "an authoritative source was checked and confirmed to hold no holdings"; AVAILABLE means "qualified evidence exists". Both require a named
source and an as-of time (enforced by the model), so a missing connection can never be reported as an empty portfolio. Neither can be produced here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, model_validator

from backend.infrastructure.private_access import OwnerContext, require_private_owner

CONTRACT_VERSION = "private_portfolio_summary.v1"
REASON_CODE = "NO_QUALIFIED_SOURCE_CONNECTED"
REASON = (
    "Özel portföy verisi için doğrulanmış bir kaynak henüz bağlı değil; "
    "bu nedenle portföy değeri, pozisyon, maliyet, getiri ve risk gösterilmiyor."
)
WARNINGS = [
    "Bu ekran karar desteğidir; yatırım tavsiyesi veya emir değildir.",
    "Üretilmiş, varsayılan veya örnek finansal veri gösterilmez.",
    "Kaynak bağlı olmadığı için boş görünen alanlar 'portföy boş' anlamına gelmez.",
]


class PrivatePortfolioSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["private_portfolio_summary.v1"]
    data_status: Literal["UNAVAILABLE", "EMPTY", "AVAILABLE"]
    reason_code: str
    reason: str
    source: Optional[str]
    as_of: Optional[datetime]
    generated_at: datetime
    warnings: List[str]
    portfolios: List[dict]
    positions: List[dict]

    @model_validator(mode="after")
    def _status_invariants(self):
        if self.data_status == "UNAVAILABLE":
            if self.source is not None or self.as_of is not None or self.portfolios or self.positions:
                raise ValueError("UNAVAILABLE carries no source, as_of or data")
        else:
            if not self.source or self.as_of is None:
                raise ValueError(f"{self.data_status} requires a named source and an as_of time")
            if self.data_status == "EMPTY" and (self.portfolios or self.positions):
                raise ValueError("EMPTY means no holdings")
        return self


def build_summary() -> PrivatePortfolioSummaryV1:
    """The only builder: no source is connected in this phase, so the answer is UNAVAILABLE."""
    return PrivatePortfolioSummaryV1(
        contract_version=CONTRACT_VERSION,
        data_status="UNAVAILABLE",
        reason_code=REASON_CODE,
        reason=REASON,
        source=None,
        as_of=None,
        generated_at=datetime.now(timezone.utc),
        warnings=list(WARNINGS),
        portfolios=[],
        positions=[],
    )


router = APIRouter(prefix="/private", tags=["Private (owner only)"])


@router.get("/portfolio/summary", response_model=PrivatePortfolioSummaryV1)
async def get_private_portfolio_summary(response: Response, owner: OwnerContext = Depends(require_private_owner)):
    response.headers["Cache-Control"] = "private, no-store"
    return build_summary()
