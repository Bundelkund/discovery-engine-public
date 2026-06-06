"""Unit tests for the title-level storage gate (T6 / storage-gate)."""
import app.scoring.keyword  # noqa: F401  (ensures _word_match importable)

from app.scoring.storage_gate import title_gate
from app.scoring.types import ScoringProfile


def _profile() -> ScoringProfile:
    """Minimal Florian-shaped profile covering all gate branches."""
    return ScoringProfile(
        id="test",
        keywords_positive=["AI", "KI", "Agile", "Coach", "Trainer", "Innovation"],
        keywords_negative=["Vertrieb", "Sales", "Software Engineer", "Pflege"],
        target_roles_primary=["Agile Coach", "KI Trainer", "AI Consultant"],
        target_roles_secondary=["Dozent", "Customer Education"],
    )


def test_primary_role_keeps_and_prioritizes():
    keep, priority = title_gate("Agile Coach", _profile())
    assert keep is True
    assert priority is True


def test_keyword_hit_keeps_without_priority():
    # 'Innovation' is a positive keyword but not a target role -> keep, no priority.
    keep, priority = title_gate("Innovation Manager", _profile())
    assert keep is True
    assert priority is False


def test_ki_trainer_kept():
    keep, _ = title_gate("KI Trainer", _profile())
    assert keep is True


def test_negative_title_dropped():
    keep, priority = title_gate("Vertriebsmitarbeiter", _profile())
    assert keep is False
    assert priority is False


def test_no_signal_title_dropped():
    # No positive keyword, no role, no negative -> still dropped (gate active).
    keep, priority = title_gate("Heizungsmonteur", _profile())
    assert keep is False
    assert priority is False


def test_negative_vetoes_positive():
    # 'Sales Coach' hits positive 'Coach' but negative 'Sales' wins -> drop.
    keep, _ = title_gate("Sales Coach", _profile())
    assert keep is False


def test_empty_profile_disables_gate():
    # No positive signals at all -> gate disabled, everything kept (demo).
    empty = ScoringProfile(id="demo")
    assert title_gate("Heizungsmonteur", empty) == (True, False)
    assert title_gate("Anything At All", empty) == (True, False)


def test_empty_title_dropped_when_gate_active():
    keep, priority = title_gate("", _profile())
    assert keep is False
    assert priority is False
