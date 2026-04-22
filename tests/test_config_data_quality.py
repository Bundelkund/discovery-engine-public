"""Tests for load_data_quality_config() — YAML loads, Pydantic validation."""

from app.config import DataQualityConfig, load_data_quality_config


def test_load_data_quality_config_returns_model() -> None:
    """load_data_quality_config must return a DataQualityConfig instance."""
    # Reset lru_cache so tests are isolated
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    assert isinstance(cfg, DataQualityConfig)


def test_all_top_level_keys_present() -> None:
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    assert cfg.minhash is not None
    assert cfg.rules is not None


def test_minhash_config_values() -> None:
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    assert 0.0 < cfg.minhash.threshold <= 1.0
    assert cfg.minhash.num_perm > 0
    assert cfg.minhash.shingle_size > 0


def test_rules_config_flag_and_reject_are_lists() -> None:
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    assert isinstance(cfg.rules.flag, list)
    assert isinstance(cfg.rules.reject, list)


def test_rules_config_grace_period_is_positive() -> None:
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    assert cfg.rules.grace_period_days > 0


def test_rules_config_activation_file_is_string() -> None:
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    assert isinstance(cfg.rules.activation_file, str)
    assert len(cfg.rules.activation_file) > 0


def test_activation_file_path_is_path_object() -> None:
    load_data_quality_config.cache_clear()
    cfg = load_data_quality_config()
    from pathlib import Path
    assert isinstance(cfg.activation_file_path, Path)


def test_no_hardcoded_iso_dates_in_config() -> None:
    """Ensure the YAML config does not contain hardcoded ISO date strings."""
    from pathlib import Path
    import re

    yaml_path = Path(__file__).parent.parent / "config" / "data-quality.yaml"
    content = yaml_path.read_text(encoding="utf-8")
    # ISO date pattern: YYYY-MM-DD
    iso_date_pattern = re.compile(r"\d{4}-\d{2}-\d{2}")
    assert not iso_date_pattern.search(content), (
        "data-quality.yaml must NOT contain hardcoded ISO dates"
    )
