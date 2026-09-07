"""Tests for the measure-CSV validator.

Each test builds a throwaway measure directory and points backend.measures at it,
so the checks are exercised against deliberately broken data rather than the real
catalogue (which is asserted separately to be clean).
"""

import pytest

from backend import measures as m
from tools import check_measures

MASTER_HEADER = ",".join([f"c{i}" for i in range(28)])

# Column positions that matter here, per backend/measures.py.
COLS = {
    "funder": 0, "name": 1, "max_grant": 3, "cofinancing": 4, "status": 6,
    "description": 7, "link": 8, "applicant": 9, "size": 10, "region": 11,
    "sector": 12, "field": 13, "project_type": 14, "ai": 15, "rnd": 16,
    "phase": 17, "partner": 18, "university": 19, "exclusions": 20,
    "checkpoints": 21, "extra_link": 22,
}


def _master_row(**over):
    row = [""] * 28
    row[COLS["funder"]] = "EAS"
    row[COLS["name"]] = over.pop("name", "Testmeede")
    row[COLS["status"]] = over.pop("status", "avatud")
    row[COLS["link"]] = over.pop("link", "https://eis.ee/teenused/test")
    row[COLS["applicant"]] = over.pop("applicant", "VKE")
    row[COLS["ai"]] = over.pop("ai", "sobib")
    row[COLS["rnd"]] = over.pop("rnd", "nõutav")
    row[COLS["partner"]] = over.pop("partner", "ei ole vajalik")
    row[COLS["university"]] = over.pop("university", "sobiv")
    if "extra_link" in over:
        row[COLS["extra_link"]] = over.pop("extra_link")
    assert not over, f"unknown override: {over}"
    return ",".join(f'"{c}"' for c in row)


def _detail(name):
    return (
        "Väli,Soovitatud väärtus,Kindlus,Põhjendus\n"
        f'"Meetme nimetus","{name}","kõrge","allikas"\n'
        '"Toetuse määr","50%","kõrge","§ 5"\n'
    )


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    """Build a measure directory and point measures.DATA_DIR at it."""
    def build(master_rows, detail_files):
        (tmp_path / m.MASTER_FILE).write_text(
            MASTER_HEADER + "\n" + "\n".join(master_rows) + "\n", encoding="utf-8"
        )
        for fname, content in detail_files.items():
            (tmp_path / fname).write_text(content, encoding="utf-8")
        monkeypatch.setattr(m, "DATA_DIR", str(tmp_path))
        return check_measures.run()
    return build


def test_clean_catalogue_has_no_errors(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert report.errors == []
    assert report.count == 1


def test_unmatched_detail_file_is_an_error(catalogue):
    """A name too far from the master row leaves measure_id=None — the silent killer."""
    report = catalogue(
        [_master_row(name="Innovatsiooniosak")],
        {"01 - Hoopis Muu Asi.csv": _detail("Täiesti erinev pealkiri siin")},
    )
    assert any("measure_id puudub" in e for e in report.errors)


def test_detail_file_without_meetme_nimetus_is_an_error(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede")],
        {"01 - Testmeede.csv": "Väli,Soovitatud väärtus,Kindlus,Põhjendus\nToetus,50%,kõrge,x\n"},
    )
    assert any("Meetme nimetus" in e for e in report.errors)


def test_duplicate_normalised_names_are_an_error(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede"), _master_row(name="Testmeede")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert any("esineb 2 korda" in e for e in report.errors)


def test_non_http_link_is_an_error(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede", link="vaata EIS lehelt")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert any("ei alga http" in e for e in report.errors)


def test_unknown_requirement_value_is_a_warning(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede", rnd="võib-olla vahel")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert report.errors == []
    assert any("classify_requirement" in w for w in report.warnings)


def test_unrecognised_applicant_is_a_warning(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede", applicant="kõik huvilised")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert any("KÕIGILE taotlejatüüpidele" in w for w in report.warnings)


def test_pinned_riigiteataja_link_is_a_warning(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede", extra_link="https://www.riigiteataja.ee/akt/109012026041")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert any("leiaKehtiv" in w for w in report.warnings)


def test_riigiteataja_link_with_leiakehtiv_is_clean(catalogue):
    report = catalogue(
        [_master_row(name="Testmeede",
                     extra_link="https://www.riigiteataja.ee/akt/109012026041?leiaKehtiv")],
        {"01 - Testmeede.csv": _detail("Testmeede")},
    )
    assert not any("leiaKehtiv" in w for w in report.warnings)


def test_real_catalogue_loads_without_errors():
    """The shipped CSVs must stay error-free; warnings are allowed."""
    report = check_measures.run()
    assert report.errors == [], report.errors
    assert report.count == 18
