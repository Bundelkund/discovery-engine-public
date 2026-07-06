"""Adzuna scrape hygiene — dedup-company-noise-escape (Weg A).

Adzuna's apply-page CTA leaks into the structured fields: company.display_name
carries "Bewerbung als" (or "Bewerbung als <role>") instead of the employer, and
some feed variants echo the employer into the title ("Title - Capco"). Both are
presentation garbage that poisoned the canonical content_hash and the UI.
"""
from app.sources.adzuna import _clean_company, _clean_title


def test_clean_company_drops_apply_label():
    assert _clean_company("Bewerbung als") == ""
    assert _clean_company("Bewerbung als Senior Transformation Manager") == ""
    assert _clean_company("bewerbung als pflegekraft") == ""


def test_clean_company_keeps_real_employers():
    assert _clean_company("Capco") == "Capco"
    assert _clean_company("amberra GmbH") == "amberra GmbH"
    # 'als' inside a name is not the label
    assert _clean_company("Bewerbungsals GmbH") == "Bewerbungsals GmbH"


def test_clean_title_strips_company_echo():
    assert (
        _clean_title(
            "(Senior) Consultant* / Transformation Manager* – Asset Management - Capco",
            "Capco",
        )
        == "(Senior) Consultant* / Transformation Manager* – Asset Management"
    )
    # echo with en-dash separator
    assert _clean_title("AI Coach – amberra", "amberra") == "AI Coach"


def test_clean_title_strips_leading_apply_label():
    assert _clean_title("Bewerbung als Pflegekraft (m/w/d)", "") == "Pflegekraft (m/w/d)"


def test_clean_title_keeps_titles_without_artefacts():
    t = "(Senior) Consultant* / Transformation Manager* – Asset Managemen"
    assert _clean_title(t, "Bewerbung als") == t  # garbage company never matches
    assert _clean_title(t, "") == t
    # company mentioned mid-title (not a trailing echo) stays
    assert _clean_title("SAP Consultant for SAP rollouts", "SAP") == (
        "SAP Consultant for SAP rollouts"
    )
