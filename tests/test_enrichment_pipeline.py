import pytest

import app.enrichment.domain_resolver  # noqa: F401
import app.enrichment.hunter  # noqa: F401

from app.enrichment.pipeline import EnrichmentPipeline


def test_pipeline_with_valid_deps():
    pipe = EnrichmentPipeline(
        {
            "steps": [
                {
                    "enricher_id": "domain_resolver",
                    "optional": False,
                    "requires": [],
                },
                {
                    "enricher_id": "hunter",
                    "optional": True,
                    "requires": ["domain_resolver"],
                },
            ]
        }
    )
    assert len(pipe.steps) == 2


def test_pipeline_rejects_missing_deps():
    with pytest.raises(ValueError, match="requires"):
        EnrichmentPipeline(
            {
                "steps": [
                    {
                        "enricher_id": "hunter",
                        "optional": True,
                        "requires": ["domain_resolver"],
                    },
                ]
            }
        )


def test_pipeline_single_step():
    pipe = EnrichmentPipeline(
        {
            "steps": [
                {
                    "enricher_id": "domain_resolver",
                    "optional": False,
                    "requires": [],
                },
            ]
        }
    )
    assert len(pipe.steps) == 1
