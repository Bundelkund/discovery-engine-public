from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from app.scoring.types import ScoringProfile

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
def load_scoring_config() -> dict:
    return load_yaml("scoring.yaml")


@lru_cache
def load_enrichment_config() -> dict:
    return load_yaml("enrichment.yaml")


@lru_cache
def load_resolution_config() -> dict:
    return load_yaml("resolution.yaml")


@lru_cache
def load_archetypes_config() -> dict:
    return load_yaml("archetypes.yaml")


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


# ---------------------------------------------------------------------------
# Single-User Scoring Profile (optional override)
# ---------------------------------------------------------------------------


@lru_cache
def load_scoring_profile() -> "ScoringProfile | None":
    """Load the single-user scoring profile.

    Resolution order (analogous to portals.yaml / portals.local.yaml):
      1. config/scoring-profile.local.yaml  (user override, gitignored)
      2. config/scoring-profile.yaml        (committed default)
      3. None                               (no file at all)

    Returns None only if neither file exists, so the orchestrator can
    fall back to an empty profile. The Apply Skill's onboarding flow
    is the canonical writer of the .local.yaml form. Cache invalidates
    only on process restart; profiles change rarely and a restart is
    cheap.

    History: before DE-FOLLOWUP-11 fix (2026-05-31) only the
    .local.yaml path was checked. Production Coolify deploys did not
    have the file -> empty ScoringProfile -> jobs.archetype universally
    empty. Committing the .yaml default unblocks single-tenant deploys
    without taking away the .local override.
    """
    from app.scoring.types import ScoringProfile

    path = resolve_local_override(REPO_ROOT / "config" / "scoring-profile.yaml")
    if not path.exists():
        return None
    with open(path) as f:
        return ScoringProfile.model_validate(yaml.safe_load(f))
