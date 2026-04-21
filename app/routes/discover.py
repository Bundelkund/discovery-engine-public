import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.dependencies import get_supabase, require_api_key
from app.utils.profile_mapper import map_profile_data
from app.models.profile import UserProfile
from app.repositories.profiles import ProfileRepository

logger = logging.getLogger(__name__)

discover_router = APIRouter(tags=["discover"])


class DiscoverRequest(BaseModel):
    profile_id: str
    limit: int = 10


class Opportunity(BaseModel):
    company: str
    domain: str
    city: Optional[str] = None
    industry: Optional[str] = None
    signal_score: float = 0.0
    signal_type: Optional[str] = None
    signal_evidence: Optional[str] = None
    kununu_score: Optional[float] = None
    kununu_sentiment: Optional[str] = None
    pitch: Optional[str] = None
    outreach_hook: Optional[str] = None


class DiscoverResponse(BaseModel):
    opportunities: list[Opportunity] = []
    scanned: int = 0
    duration_ms: int = 0


@discover_router.post(
    "/discover/opportunities", dependencies=[Depends(require_api_key)]
)
async def discover_opportunities(
    request: DiscoverRequest, supabase=Depends(get_supabase)
):
    start = time.time()

    profile_repo = ProfileRepository(supabase)
    profile_data = await profile_repo.get(request.profile_id)
    if not profile_data:
        raise HTTPException(status_code=404, detail="Profile not found")

    map_profile_data(profile_data)
    profile = UserProfile(
        **{k: v for k, v in profile_data.items() if k in UserProfile.model_fields}
    )

    # Get companies with high signal scores that DON'T already have matching jobs
    result = supabase.table("company_watchlist").select(
        "domain, name, city, industry"
    ).eq("active", True).execute()
    watchlist = result.data or []

    if not watchlist:
        return DiscoverResponse(
            duration_ms=int((time.time() - start) * 1000)
        )

    # Get signal + culture data from company_profiles
    domains = [c["domain"] for c in watchlist]
    profiles_result = supabase.table("company_profiles").select(
        "domain, transformation_signal_score, signal_type, signal_evidence, "
        "kununu_score, kununu_sentiment"
    ).in_("domain", domains).execute()
    company_data = {c["domain"]: c for c in (profiles_result.data or [])}

    # Get domains that already have high-scoring jobs for this profile
    existing_result = supabase.table("jobs").select(
        "company_domain"
    ).eq("profile_id", request.profile_id).gte(
        "score_stage_1", 50
    ).not_.is_("company_domain", "null").execute()
    domains_with_jobs = {
        j["company_domain"] for j in (existing_result.data or [])
    }

    # Build candidate list: high signal, no existing match
    candidates = []
    for company in watchlist:
        domain = company["domain"]
        data = company_data.get(domain, {})
        signal_score = data.get("transformation_signal_score", 0) or 0

        # Skip companies that already have matching jobs
        if domain in domains_with_jobs:
            continue

        # Only include companies with some signal
        if signal_score < 0.2:
            continue

        candidates.append({
            "company": company["name"],
            "domain": domain,
            "city": company.get("city"),
            "industry": company.get("industry"),
            "signal_score": signal_score,
            "signal_type": data.get("signal_type"),
            "signal_evidence": data.get("signal_evidence"),
            "kununu_score": data.get("kununu_score"),
            "kununu_sentiment": data.get("kununu_sentiment"),
        })

    # Sort by signal score descending
    candidates.sort(key=lambda c: c["signal_score"], reverse=True)
    top_candidates = candidates[: request.limit]

    # LLM pitch for top candidates
    opportunities = []
    settings = get_settings()
    if settings.openai_api_key and top_candidates:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        roles = (profile.target_roles_primary or []) + (
            profile.target_roles_secondary or []
        )
        role_str = ", ".join(roles) if roles else "AI Coach / KI Trainer"

        for candidate in top_candidates:
            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=300,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Du bist ein Company-Intelligence-Analyst. "
                                "Bestimme ob diese Firma gerade einen "
                                f"{role_str} braucht — auch ohne Stellenanzeige. "
                                "Antworte NUR mit JSON: "
                                '{"pitch": "1-2 Sätze: Welches Problem hat die '
                                'Firma, das der Kandidat lösen kann?", '
                                '"outreach_hook": "Ein Satz Aufhänger für '
                                'Initiativbewerbung"}'
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Firma: {candidate['company']}\n"
                                f"Branche: {candidate.get('industry', 'unbekannt')}\n"
                                f"Stadt: {candidate.get('city', 'unbekannt')}\n"
                                f"Signal: {candidate.get('signal_evidence', 'keine Daten')}\n"
                                f"Kununu: {candidate.get('kununu_score', 'k.A.')}/5 "
                                f"({candidate.get('kununu_sentiment', 'unbekannt')})\n\n"
                                f"Kandidat: {role_str}"
                            ),
                        },
                    ],
                )
                text = response.choices[0].message.content or "{}"
                parsed = json.loads(text)
                candidate["pitch"] = parsed.get("pitch", "")
                candidate["outreach_hook"] = parsed.get("outreach_hook", "")
            except Exception as e:
                logger.warning(
                    f"LLM pitch failed for {candidate['company']}: {e}"
                )
                candidate["pitch"] = ""
                candidate["outreach_hook"] = ""

            opportunities.append(Opportunity(**candidate))
    else:
        opportunities = [Opportunity(**c) for c in top_candidates]

    return DiscoverResponse(
        opportunities=opportunities,
        scanned=len(watchlist),
        duration_ms=int((time.time() - start) * 1000),
    )
