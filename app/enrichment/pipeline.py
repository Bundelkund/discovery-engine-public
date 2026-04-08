import logging

from app.models.company import CompanyProfile, EnrichmentContext
from app.registry.enricher_registry import EnricherRegistry

logger = logging.getLogger(__name__)


class EnrichmentPipeline:
    def __init__(self, config: dict):
        self.steps = []
        for step_cfg in config.get("steps", []):
            if not step_cfg.get("enabled", True):
                continue
            enricher_cls = EnricherRegistry.get(step_cfg["enricher_id"])
            instance = enricher_cls(config=step_cfg)
            self.steps.append(instance)
        self._validate_dependencies()

    def _validate_dependencies(self):
        seen = set()
        for step in self.steps:
            for req in step.requires:
                if req not in seen:
                    raise ValueError(
                        f"Enricher '{step.enricher_id}' requires '{req}' before it in pipeline"
                    )
            seen.add(step.enricher_id)

    async def run(
        self, companies: list[CompanyProfile], ctx: EnrichmentContext
    ) -> list[CompanyProfile]:
        results = []
        for company in companies:
            enriched = company
            for step in self.steps:
                try:
                    enriched = await step.enrich(enriched, ctx)
                except Exception as e:
                    if step.optional:
                        logger.warning(
                            f"Optional enricher '{step.enricher_id}' failed for {company.domain}: {e}"
                        )
                        continue
                    else:
                        logger.error(
                            f"Required enricher '{step.enricher_id}' failed for {company.domain}: {e}"
                        )
                        break
            results.append(enriched)
        return results
