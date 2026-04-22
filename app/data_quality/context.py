"""Shared DQ context — single process-wide singleton for MinHash, GeoNames, Rules.

The orchestrator and the /health endpoint must observe the same state: if they
each own their own MinHashDedup / LocationNormalizer / RulesEngine instances,
metrics drift and duplicate detection becomes inconsistent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import load_data_quality_config
from app.data_quality.location import LocationNormalizer
from app.data_quality.minhash import MinHashDedup
from app.data_quality.rules import RulesEngine, compute_activation_date

logger = logging.getLogger(__name__)

_GEONAMES_CSV = Path(__file__).parent.parent.parent / "data" / "geonames-de-subset.csv"


class DQContext:
    """Shared DQ state: MinHash LSH, location normalizer, rules engine."""

    def __init__(self) -> None:
        cfg = load_data_quality_config()
        mh_cfg = cfg.minhash
        self.minhash = MinHashDedup(
            threshold=mh_cfg.threshold,
            num_perm=mh_cfg.num_perm,
            shingle_size=mh_cfg.shingle_size,
            seed=getattr(mh_cfg, "seed", 42),
        )
        self.location_normalizer = LocationNormalizer(_GEONAMES_CSV)

        try:
            activation = compute_activation_date(
                cfg.rules.model_dump(),
                datetime.now(tz=timezone.utc).date(),
                cfg.activation_file_path,
            )
        except ValueError as exc:
            logger.warning(
                "activation_file_corrupt_flag_only_mode",
                extra={"error": str(exc)},
            )
            activation = None

        self.rules_engine = RulesEngine(
            cfg.rules.model_dump(),
            activation_date=activation,
        )

    @property
    def rules_mode(self) -> str:
        return self.rules_engine.mode

    @property
    def geonames_loaded(self) -> bool:
        return self.location_normalizer.is_loaded


_instance: DQContext | None = None


def get_dq_context() -> DQContext:
    """Return the process-level DQContext singleton (lazy init)."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = DQContext()
    return _instance


def reset_dq_context() -> None:
    """Testing hook — forces re-creation on next get_dq_context() call."""
    global _instance  # noqa: PLW0603
    _instance = None
