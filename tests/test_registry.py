import pytest

# Trigger all registrations at module level
import app.sources.indeed  # noqa: F401
import app.sources.greenhouse  # noqa: F401
import app.sources.adzuna  # noqa: F401
import app.sources.rss  # noqa: F401
import app.scoring.keyword  # noqa: F401
import app.scoring.embedding  # noqa: F401
import app.enrichment.domain_resolver  # noqa: F401
import app.enrichment.hunter  # noqa: F401
import app.enrichment.cvf  # noqa: F401

from app.registry.source_registry import SourceRegistry
from app.registry.scorer_registry import ScorerRegistry
from app.registry.enricher_registry import EnricherRegistry


def test_source_registry_has_all_sources():
    ids = SourceRegistry.registered_ids()
    assert "indeed" in ids
    assert "greenhouse" in ids
    assert "adzuna" in ids
    assert "rss" in ids


def test_scorer_registry_has_all_scorers():
    ids = ScorerRegistry.registered_ids()
    assert "keyword" in ids
    assert "embedding" in ids


def test_enricher_registry_has_all_enrichers():
    ids = EnricherRegistry.registered_ids()
    assert "domain_resolver" in ids
    assert "hunter" in ids
    assert "cvf" in ids


def test_source_registry_get_returns_class():
    cls = SourceRegistry.get("indeed")
    assert cls is not None
    assert hasattr(cls, "fetch")


def test_source_registry_get_unknown_raises():
    with pytest.raises(KeyError):
        SourceRegistry.get("nonexistent_source")


def test_scorer_registry_get_unknown_raises():
    with pytest.raises(KeyError):
        ScorerRegistry.get("nonexistent_scorer")
