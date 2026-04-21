import json
import logging

from openai import AsyncOpenAI

from app.config import get_settings
from app.models.job import NormalizedJob, ScorerResult
from app.models.profile import UserProfile
from app.registry.scorer_registry import ScorerRegistry
from app.scoring.base import BaseScorer

logger = logging.getLogger(__name__)


@ScorerRegistry.register("llm")
class LLMScorer(BaseScorer):
    scorer_id = "llm"
    stage = 3

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.model = self.config.get("model", "gpt-4o-mini")
        self.max_jobs = self.config.get("max_jobs", 30)
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            settings = get_settings()
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY not configured")
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def score(self, job: NormalizedJob, profile: UserProfile) -> ScorerResult:
        try:
            job_description = (job.description or "")[:2000]

            system_prompt = '''Du bist ein Job-Matching-Analyst mit Problem-Solver-Mentalität.
Analysiere nicht nur ob der Kandidat zum Job passt, sondern welches PROBLEM
der Firma der Kandidat lösen kann — auch wenn es nicht explizit in der
Ausschreibung steht.

Antworte NUR mit validem JSON in diesem Format:
{
  "reasoning": "2-3 Sätze warum dieser Job passt oder nicht passt",
  "fit_score": 0-100,
  "highlights": ["Highlight 1", "Highlight 2", "Highlight 3"],
  "pitch": "1-2 Sätze: Welches Problem hat die Firma, das der Kandidat lösen kann? Formuliere es als Aufhänger für eine Bewerbung."
}

Fokus auf:
1. Welche Skills aus dem Profil passen zu den Job-Anforderungen?
2. Wie gut passt die Rolle zu den Zielrollen des Kandidaten?
3. Was sind die wichtigsten Match-Highlights oder Lücken?
4. PROBLEM-SOLVER: Was braucht die Firma eigentlich — auch zwischen den Zeilen?
   Denke: "Sie suchen X, aber eigentlich brauchen sie jemanden der Y kann."'''

            keywords = profile.keywords_positive or []
            roles = (profile.target_roles_primary or []) + (
                profile.target_roles_secondary or []
            )
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

            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=500,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )

            response_text = response.choices[0].message.content or "{}"
            result = json.loads(response_text)

            return ScorerResult(
                scorer_id=self.scorer_id,
                stage=self.stage,
                score=float(result.get("fit_score", 0)),
                details={
                    "reasoning": result.get("reasoning", ""),
                    "highlights": result.get("highlights", []),
                    "pitch": result.get("pitch", ""),
                },
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response: {e}")
            return ScorerResult(
                scorer_id=self.scorer_id,
                stage=self.stage,
                score=0.0,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(f"LLM scoring failed for '{job.title}': {e}")
            return ScorerResult(
                scorer_id=self.scorer_id,
                stage=self.stage,
                score=0.0,
                details={"error": str(e)},
            )
