import json
import logging
import re

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import get_settings
from app.enrichment.base import BaseEnricher
from app.models.company import CompanyProfile, EnrichmentContext
from app.registry.enricher_registry import EnricherRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section parser (ported from .refs/flipped/section_parser.py)
# ---------------------------------------------------------------------------


class ParsedSections(BaseModel):
    section_about_us: str | None = None
    section_tasks: str | None = None
    section_profile: str | None = None
    section_benefits: str | None = None
    formality_score: float = 0.5


class SectionParser:
    PATTERNS = {
        "section_about_us": r"(?i)(?:Über uns|Das Unternehmen|Wer wir sind|Unser Unternehmen)",
        "section_tasks": r"(?i)(?:Ihre? Aufgaben|Das erwartet (?:dich|Sie)|Deine Rolle|Aufgabenbereich)",
        "section_profile": r"(?i)(?:Ihre? Profil|Das bringst du mit|Anforderungen|Was Sie mitbringen)",
        "section_benefits": r"(?i)(?:Wir bieten|Benefits|Das bieten wir|Unsere Benefits|Was wir bieten)",
    }

    def parse(self, description: str) -> ParsedSections:
        """Split German job posting into sections via regex."""
        matches: list[tuple[int, str, str]] = []
        for field, pattern in self.PATTERNS.items():
            for m in re.finditer(pattern, description):
                matches.append((m.start(), field, m.group()))

        if not matches:
            return ParsedSections(
                section_about_us=description.strip() or None,
                formality_score=self._formality_score(description),
            )

        matches.sort(key=lambda x: x[0])
        data: dict[str, str | None] = {}
        for i, (pos, field, header) in enumerate(matches):
            start = pos + len(header)
            end = matches[i + 1][0] if i + 1 < len(matches) else len(description)
            text = description[start:end].strip()
            if field not in data:
                data[field] = text

        return ParsedSections(
            section_about_us=data.get("section_about_us"),
            section_tasks=data.get("section_tasks"),
            section_profile=data.get("section_profile"),
            section_benefits=data.get("section_benefits"),
            formality_score=self._formality_score(description),
        )

    def _formality_score(self, text: str) -> float:
        """0.0 = all du, 1.0 = all Sie."""
        sie = len(re.findall(r"\b(Sie|Ihnen|Ihrer?)\b", text))
        du = len(re.findall(r"\b(du|dich|deiner?|dir)\b", text, re.IGNORECASE))
        total = sie + du
        return sie / total if total > 0 else 0.5


# ---------------------------------------------------------------------------
# CVF posting result
# ---------------------------------------------------------------------------

_FALLBACK_SCORES = {
    "clan": 0.25,
    "adhocracy": 0.25,
    "market": 0.25,
    "hierarchy": 0.25,
}


# ---------------------------------------------------------------------------
# CVF enricher
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Du bist ein Experte für Organisationskultur-Analyse nach dem Competing Values Framework (CVF).\n"
    "\n"
    "Analysiere die folgende Stellenanzeige und klassifiziere die Unternehmenskultur.\n"
    "\n"
    "## CVF-Quadranten und ihre Signale in Stellenanzeigen:\n"
    "\n"
    '**Clan** (Zusammenarbeit): familiäres Team, Wir-Kultur, flache Hierarchien, Teamevents, Du-Kultur, '
    "gemeinsam wachsen, kurze Entscheidungswege, offene Feedbackkultur\n"
    '**Adhocracy** (Innovation): Startup, remote-first, disruptiv, Hackathon, Weiterbildungsbudget, '
    "Equity/VSOP, Greenfield, mission-driven, agil, Prototypen\n"
    '**Market** (Wettbewerb): marktführend, Performance, Provision/Bonus, leistungsbezogen, KPIs, '
    "Umsatz, Wachstumskurs, Karrierepfad, ergebnisorientiert, Direktvertrieb\n"
    '**Hierarchy** (Kontrolle): Tarifvertrag, Betriebsrat, ISO-zertifiziert, Compliance, Konzern, '
    "bAV, Kantine, Prozesse dokumentieren, Richtlinien, Sie-Form\n"
    "\n"
    "## Wichtige Bewertungsregeln:\n"
    '- "Wir bieten" (60%) und "Über uns" (40%) sind die stärksten Company-Level-Signale\n'
    "- Beachte auch Meta-Signale: Konzern-Größe, Branche, Gründungsjahr, Du/Sie-Form\n"
    "- Ein Konzern mit Du-Form ist trotzdem Hierarchy (Coconut-Pattern)\n"
    "- Scores müssen sich zu 1.0 summieren\n"
    "\n"
    "Antworte NUR mit validem JSON. Kein weiterer Text."
)

USER_PROMPT_TEMPLATE = (
    "Analysiere diese Stellenanzeige:\n"
    "\n"
    "**Firma:** {company}\n"
    "**Titel:** {title}\n"
    "\n"
    "**Beschreibung:**\n"
    "{description}\n"
    "\n"
    "Antworte als JSON:\n"
    '{{"dominant_quadrant": "clan|adhocracy|market|hierarchy", '
    '"cvf_scores": {{"clan": 0.XX, "adhocracy": 0.XX, "market": 0.XX, "hierarchy": 0.XX}}}}'
)


@EnricherRegistry.register("cvf")
class CvfEnricher(BaseEnricher):
    enricher_id = "cvf"
    requires = ["domain_resolver"]
    optional = True

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.min_postings = self.config.get("min_postings", 3)
        self.model = self.config.get("model", "gpt-4o-mini")
        self._parser = SectionParser()

    async def enrich(
        self, company: CompanyProfile, ctx: EnrichmentContext
    ) -> CompanyProfile:
        jobs = ctx.jobs
        if len(jobs) < self.min_postings:
            logger.info(
                f"CVF: skipping {company.domain}, only {len(jobs)} postings "
                f"(need {self.min_postings})"
            )
            return company

        settings = get_settings()
        if not settings.openai_api_key:
            logger.warning("CVF: no OpenAI API key configured")
            return company

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        posting_scores: list[dict[str, float]] = []

        for job in jobs:
            description = job.get("description", "")
            if not description:
                continue

            sections = self._parser.parse(description)
            scores = await self._classify_posting(
                client,
                sections,
                company_name=company.name or company.domain,
                title=job.get("title", ""),
            )
            posting_scores.append(scores)

        company.cvf_scores = self._aggregate(posting_scores)
        logger.info(f"CVF: enriched {company.domain} from {len(posting_scores)} postings")
        return company

    async def _classify_posting(
        self,
        client: AsyncOpenAI,
        sections: ParsedSections,
        company_name: str,
        title: str,
    ) -> dict[str, float]:
        """Classify a single posting via GPT. Returns CVF score dict."""
        parts = []
        if sections.section_about_us:
            parts.append(sections.section_about_us)
        if sections.section_tasks:
            parts.append(sections.section_tasks)
        if sections.section_profile:
            parts.append(sections.section_profile)
        if sections.section_benefits:
            parts.append(sections.section_benefits)
        description = "\n\n".join(parts) if parts else ""

        user_content = USER_PROMPT_TEMPLATE.format(
            company=company_name,
            title=title,
            description=description,
        )

        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
                timeout=30,
            )
            if not response.choices:
                raise ValueError("Empty choices in LLM response")

            content = response.choices[0].message.content
            data = json.loads(content)
            return data.get("cvf_scores", _FALLBACK_SCORES.copy())

        except Exception as e:
            logger.error(f"CVF classification failed for {company_name}: {e}")
            return _FALLBACK_SCORES.copy()

    def _aggregate(self, posting_scores: list[dict[str, float]]) -> dict:
        """Aggregate posting-level CVF scores into company-level result."""
        if not posting_scores:
            return {
                "cvf_scores": _FALLBACK_SCORES.copy(),
                "confidence": 0.0,
                "n_postings": 0,
            }

        quadrants = ["clan", "adhocracy", "market", "hierarchy"]
        n = len(posting_scores)

        means = {
            q: sum(s.get(q, 0.0) for s in posting_scores) / n for q in quadrants
        }

        # Normalize to sum to 1.0
        total = sum(means.values())
        if total > 0:
            means = {q: v / total for q, v in means.items()}

        confidence = min(0.9, 0.3 + 0.07 * n)

        return {
            "cvf_scores": means,
            "confidence": round(confidence, 2),
            "n_postings": n,
        }
