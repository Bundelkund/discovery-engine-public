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


def load_sources_config() -> dict:
    return load_yaml("sources.yaml")


def load_scoring_config() -> dict:
    return load_yaml("scoring.yaml")


def load_enrichment_config() -> dict:
    return load_yaml("enrichment.yaml")


def load_archetypes_config() -> dict:
    return load_yaml("archetypes.yaml")


# ---------------------------------------------------------------------------
# Data Quality Config
# ---------------------------------------------------------------------------


class MinHashConfig(BaseModel):
    threshold: float = 0.9
    num_perm: int = 128
    shingle_size: int = 5


class RulesConfig(BaseModel):
    flag: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)
    grace_period_days: int = 7
    activation_file: str = "data/dq_rules_activation.txt"


class DataQualityConfig(BaseModel):
    minhash: MinHashConfig = Field(default_factory=MinHashConfig)
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
