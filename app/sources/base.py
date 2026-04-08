import hashlib
from abc import ABC, abstractmethod

from app.models.job import NormalizedJob, RawJob


class BaseScraper(ABC):
    source_id: str

    @abstractmethod
    async def fetch(self, config: dict) -> list[RawJob]:
        """Fetch jobs from source. MUST catch all exceptions, return [] on failure."""

    def normalize(self, raw: RawJob) -> NormalizedJob:
        content = f"{raw.url}|{raw.title}|{raw.company}".lower().strip()
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return NormalizedJob(
            title=raw.title,
            url=raw.url,
            company=raw.company,
            location=raw.location,
            description=raw.description,
            salary=raw.salary,
            source=self.source_id,
            external_id=raw.external_id,
            posted_at=raw.posted_at,
            content_hash=content_hash,
        )
