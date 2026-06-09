import logging

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_supabase, require_scope
from app.enrichment.service import enrich_domain
from app.models.responses import CompanyDetailResponse, CompanySignals
from app.repositories.companies import CompanyRepository

logger = logging.getLogger(__name__)

companies_api_router = APIRouter(prefix="/companies", tags=["companies-api"])


@companies_api_router.get(
    "/{domain}", dependencies=[Depends(require_scope("jobs:read"))]
)
async def get_company(
    domain: str,
    supabase=Depends(get_supabase),
) -> CompanyDetailResponse:
    repo = CompanyRepository(supabase)
    data = await repo.get_with_watchlist(domain)
    if not data:
        # enrich-on-read: refine no longer enriches at scrape time, so the first
        # consumer to need this company (e.g. an apply flow) triggers enrichment
        # here. Best-effort — a failure falls through to 404, never a 500.
        try:
            enriched = await enrich_domain(supabase, domain)
            if enriched:
                data = await repo.get_with_watchlist(domain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("companies_enrich_on_read_failed", extra={"domain": domain, "error": str(exc)})
    if not data:
        raise HTTPException(status_code=404, detail="Company not found")

    signals = None
    signals_data = data.pop("signals", None)
    if signals_data:
        signals = CompanySignals(
            transformation_signal_score=signals_data.get(
                "transformation_signal_score", 0.0
            ),
            signal_type=signals_data.get("signal_type"),
            signal_evidence=signals_data.get("signal_evidence"),
            kununu_score=signals_data.get("kununu_score"),
            kununu_sentiment=signals_data.get("kununu_sentiment"),
        )

    return CompanyDetailResponse(
        domain=data.get("domain", domain),
        name=data.get("name", ""),
        size=data.get("size", ""),
        industry=data.get("industry", ""),
        location=data.get("location", ""),
        linkedin=data.get("linkedin", ""),
        hunter_data=data.get("hunter_data") or {},
        cvf_scores=data.get("cvf_scores") or {},
        hiring_signal_count=data.get("hiring_signal_count", 0),
        enriched_at=data.get("enriched_at"),
        signals=signals,
    )
