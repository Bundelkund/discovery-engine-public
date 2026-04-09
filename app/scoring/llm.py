import json
import logging
from typing import Optional

import anthropic

from app.config import get_settings
from app.models.job import NormalizedJob, ScorerResult
from app.models.profile import UserProfile
from app.registry.scorer_registry import ScorerRegistry
from app.scoring.base import BaseScorer

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


@ScorerRegistry.register("llm")
class LLMScorer(BaseScorer):
    scorer_id = "llm"
    stage = 3

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.model = self.config.get("model", "claude-haiku-4-5-20251001")
        self.max_jobs = self.config.get("max_jobs", 30)

    async def score(self, job: NormalizedJob, profile: UserProfile) -> ScorerResult:
        try:
            client = _get_client()
            job_description = (job.description or "")[:2000]

            system_prompt = '''Du bist ein Job-Matching-Analyst. Analysiere wie gut ein Job zum Kandidatenprofil passt.

Antworte NUR mit validem JSON in diesem Format:
{
  "reasoning": "2-3 Sätze warum dieser Job passt oder nicht passt",
  "fit_score": 0-100,
  "highlights": ["Highlight 1", "Highlight 2", "Highlight 3"]
}

Fokus auf:
1. Welche Skills aus dem Profil passen zu den Job-Anforderungen?
2. Wie gut passt die Rolle zu den Zielrollen des Kandidaten?
3. Was sind die wichtigsten Match-Highlights oder Lücken?'''

            keywords = (profile.keywords_positive or [])
            roles = (profile.target_roles_primary or []) + (profile.target_roles_secondary or [])
            cv_summary = (profile.cv_text or "")[:1000]

            user_content = f'''JOB:
- Titel: {job.title}
- Firma: {job.company}
- Ort: {job.location}
- Beschreibung: {job_description}

KANDIDATEN-PROFIL:
- Keywords: {', '.join(keywords) if keywords else 'Keine angegeben'}
- Zielrollen: {', '.join(roles) if roles else 'Keine angegeben'}
- Archetypes: {', '.join(f'{k} ({v})' for k, v in (profile.archetypes or {}).items())}
{f"- CV: {cv_summary}" if cv_summary else ""}'''

            message = client.messages.create(
                model=self.model,
                max_tokens=500,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}]
            )

            response_text = message.content[0].text
            result = json.loads(response_text)

            return ScorerResult(
                scorer_id=self.scorer_id,
                stage=self.stage,
                score=float(result.get("fit_score", 0)),
                details={
                    "reasoning": result.get("reasoning", ""),
                    "highlights": result.get("highlights", []),
                },
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response: {e}")
            return ScorerResult(scorer_id=self.scorer_id, stage=self.stage, score=0.0, details={"error": str(e)})
        except Exception as e:
            logger.error(f"LLM scoring failed for '{job.title}': {e}")
            return ScorerResult(scorer_id=self.scorer_id, stage=self.stage, score=0.0, details={"error": str(e)})
