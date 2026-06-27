from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

CONFIG_DIR = Path(__file__).parent.parent / "config"
REPO_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    supabase_url: str
    supabase_key: str
    # de_api_key removed in Phase 5 — replaced by per-consumer keys in config/api-keys.yaml
    de_api_key: str = ""
    hunter_api_key: str = ""
    apify_api_token: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    careerjet_affid: str = ""
    jooble_api_key: str = ""
    themuse_api_key: str = ""
    # Cutover read-switch: controls which jobs shelf JobRepository and DeduplicationService
    # query. Default = "jobs_v2" (target architecture). Set to "jobs" to read from v1 shelf
    # during rollback. Production cutover runbook:
    #   1. python scripts/migrate_jobs_v2.py --copy     (idempotent upsert v1 → v2)
    #   2. python scripts/migrate_jobs_v2.py --report   (gate: exit 0 = all keys copied)
    #   3. Set JOBS_TABLE=jobs_v2 in Coolify → Redeploy helpful-hyena
    #   4. Keep v1 table for rollback (revert JOBS_TABLE=jobs); drop only after stable.
    #   5. python scripts/migrate_jobs_v2.py --apply-drop (requires --report pass same run)
    jobs_table: str = "jobs_v2"

    # Autonomous refine scheduler (app/services/refine_runner.py). The engine
    # drains raw_jobs(status='new') → jobs_v2 on its own loop, so the pipeline no
    # longer depends on an external n8n cron hitting POST /refine. Set
    # REFINE_AUTO_ENABLED=false to fall back to purely manual/external triggering.
    refine_auto_enabled: bool = True
    refine_interval_seconds: int = 300   # gap between drain cycles
    refine_batch_limit: int = 200        # raw_jobs fetched per pass
    refine_max_passes: int = 100         # safety cap on passes per cycle (drain-until-empty)

    # Autonomous scrape scheduler (app/services/scrape_runner.py). The engine
    # triggers its own source scrapes so the fetch path no longer depends on an
    # external n8n cron hitting POST /scrape/{source}. The cadence gate is
    # persistent (scrape_runs table): a source is scraped at most once per
    # scrape_min_interval_hours, so a redeploy does NOT re-hit external/paid APIs.
    # Set SCRAPE_AUTO_ENABLED=false to fall back to purely manual/external triggering.
    scrape_auto_enabled: bool = True
    scrape_check_interval_seconds: int = 3600   # loop wake interval
    scrape_min_interval_hours: int = 24         # once per day per source (fallback gate)
    scrape_source_timeout_seconds: int = 1800   # per-source cap so one hung source
                                                # can't wedge the whole cycle (the big
                                                # ATS boards fetch in ~15min; 30min is slack)
    # Anchor the daily cadence to a FIXED wall-clock hour (UTC) instead of "24h since
    # last run". The interval gate drifts ~1h later each day (run completes a bit later
    # than the day before), eventually crossing downstream consumers like the Telegram
    # digest. Anchored: a source is due once per day after this hour and skips for the
    # rest of the day — same redeploy/quota safety as the interval gate. Set to None to
    # restore the pure interval behaviour. 3 = 03:00 UTC, long before the 12:35 digest.
    scrape_daily_anchor_hour_utc: int | None = 3

    model_config = {
        "env_file": Path(__file__).parent.parent / ".env",
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


def resolve_local_override(path: str | Path) -> Path:
    """Resolve a config path with .local.yaml override.

    If `<stem>.local.yaml` exists alongside `<path>`, prefer it.
    Otherwise return the original path. Lets users keep a private
    portals.local.yaml gitignored without touching sources.yaml.
    """
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    local = p.with_name(p.stem + ".local.yaml")
    return local if local.exists() else p


@lru_cache
def load_sources_config() -> dict:
    return load_yaml("sources.yaml")


@lru_cache
def load_enrichment_config() -> dict:
    return load_yaml("enrichment.yaml")


@lru_cache
def load_resolution_config() -> dict:
    return load_yaml("resolution.yaml")


# ---------------------------------------------------------------------------
# Data Quality Config
# ---------------------------------------------------------------------------


class MinHashConfig(BaseModel):
    threshold: float = 0.9
    num_perm: int = 128
    shingle_size: int = 5
    band_width: int = 4   # bands = num_perm / band_width = 32
    seed: int = 42


class DedupConfig(BaseModel):
    window_days: int = 42  # retention window for dedup_memory rows


class RulesConfig(BaseModel):
    flag: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)
    grace_period_days: int = 7
    activation_file: str = "data/dq_rules_activation.txt"


class DataQualityConfig(BaseModel):
    minhash: MinHashConfig = Field(default_factory=MinHashConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)

    @property
    def activation_file_path(self) -> Path:
        """Resolve activation_file relative to repo root."""
        p = Path(self.rules.activation_file)
        if p.is_absolute():
            return p
        return REPO_ROOT / p


@lru_cache
def load_data_quality_config() -> DataQualityConfig:
    """Load and validate data-quality.yaml, returning a Pydantic model."""
    raw: dict[str, Any] = load_yaml("data-quality.yaml")
    return DataQualityConfig.model_validate(raw)


# Engine scoring profile removed 2026-06-09: the engine is profile-agnostic.
# Per-profile scoring lives in the tenant module (tenant.search_terms +
# tenant matching.py). The refine pipeline no longer scores or gates on a profile.
