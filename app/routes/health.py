import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from app.registry.enricher_registry import EnricherRegistry
from app.registry.scorer_registry import ScorerRegistry
from app.registry.source_registry import SourceRegistry

logger = logging.getLogger(__name__)

health_router = APIRouter(tags=["health"])

# Module-level singletons — initialised lazily on first /health call.
_dq_state: dict | None = None


def _get_dq_state() -> dict:
    """Lazily build data-quality state dict for /health."""
    global _dq_state  # noqa: PLW0603
    if _dq_state is not None:
        return _dq_state

    try:
        from app.config import load_data_quality_config
        from app.data_quality.location import LocationNormalizer
        from app.data_quality.rules import RulesEngine, compute_activation_date

        cfg = load_data_quality_config()

        # GeoNames
        geonames_path = Path(__file__).parent.parent.parent / "data" / "geonames-de-subset.csv"
        loc = LocationNormalizer(geonames_path)
        geonames_loaded = loc.is_loaded

        # Rules mode
        activation_file = cfg.activation_file_path
        try:
            activation = compute_activation_date(
                cfg.rules.model_dump(),
                datetime.now(tz=timezone.utc).date(),
                activation_file,
            )
        except ValueError:
            activation = None

        engine = RulesEngine(cfg.rules.model_dump(), activation_date=activation)
        rules_mode = engine.mode

        _dq_state = {
            "minhash_enabled": True,
            "rules_mode": rules_mode,
            "geonames_loaded": geonames_loaded,
        }
    except Exception as exc:
        logger.error("Failed to build DQ state for health endpoint", extra={"error": str(exc)})
        _dq_state = {
            "minhash_enabled": False,
            "rules_mode": "unknown",
            "geonames_loaded": False,
        }

    return _dq_state


@health_router.get("/health")
async def health():
    dq_state = _get_dq_state()

    # Coverage metrics — return zero values when DB is not available
    coverage = {
        "jobs_total": 0,
        "location_normalized_pct": 0.0,
        "dq_flags_pct": 0.0,
        "jobs_last_24h": 0,
    }

    return {
        "status": "ok",
        "sources": SourceRegistry.registered_ids(),
        "scorers": ScorerRegistry.registered_ids(),
        "enrichers": EnricherRegistry.registered_ids(),
        "data_quality": dq_state,
        "coverage": coverage,
    }
