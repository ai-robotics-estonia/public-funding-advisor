"""Tests for previously_applied hard filter."""
from backend.measures import filter_candidates


def _m(name):
    return {
        "name": name,
        "status": "avatud",
        "applicant": "VKE",
        "size": "VKE",
        "region": "Eesti",
        "exclusions": "",
    }


def test_previously_applied_drops_named_measures():
    measures = [_m("Innovatsiooniosak"), _m("Arendusosak")]
    intake = {
        "applicant_type": "ettevote",
        "company_size": "VKE",
        "region": "",
        "previously_applied": ["Innovatsiooniosak"],
    }
    candidates, dropped = filter_candidates(measures, intake)
    assert [c["name"] for c in candidates] == ["Arendusosak"]
    assert any(
        d["name"] == "Innovatsiooniosak" and d["reason"] == "varem taotletud"
        for d in dropped
    )


def test_previously_applied_empty_keeps_all():
    measures = [_m("Innovatsiooniosak"), _m("Arendusosak")]
    intake = {
        "applicant_type": "ettevote",
        "company_size": "VKE",
        "region": "",
        "previously_applied": [],
    }
    candidates, dropped = filter_candidates(measures, intake)
    assert [c["name"] for c in candidates] == ["Innovatsiooniosak", "Arendusosak"]
    assert not any(d["reason"] == "varem taotletud" for d in dropped)
