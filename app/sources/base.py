from abc import ABC, abstractmethod

from app.models.job import NormalizedJob, RawJob


class BaseScraper(ABC):
    source_id: str

    @abstractmethod
    async def fetch(self, config: dict) -> list[RawJob]:
        """Fetch jobs from source. MUST catch all exceptions, return [] on failure."""

    def normalize(self, raw: RawJob) -> NormalizedJob:
        """Thin shim — the canonical zerlegen (parse) step now lives in the refine
        pipeline (app/services/refine_pipeline.py `parse_raw`). The store-first fetch
        path does NOT call this; it is retained only for adapter-level unit tests that
        assert a scraper can map its RawJob to a NormalizedJob with a content_hash.
        """
        from app.services.refine_pipeline import parse_raw

        return parse_raw(raw, default_source=self.source_id)
