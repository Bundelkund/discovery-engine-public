from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings

CONFIG_DIR = Path(__file__).parent.parent / "config"


class Settings(BaseSettings):
    supabase_url: str
    supabase_key: str
    de_api_key: str
    hunter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

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
