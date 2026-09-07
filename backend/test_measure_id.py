"""Tests for stable measure_id derivation from detail-CSV filenames."""

from backend.measures import _measure_id_from_filename, get_measure_by_id, load_measures


def test_numbered_filename_uses_leading_number():
    assert _measure_id_from_filename("02 - Innovatsiooniosak.csv") == "02"
    assert _measure_id_from_filename("11 - (Ettevõtja) tootearenduse toetu.csv") == "11"


def test_unnumbered_filename_falls_back_to_normalized_stem():
    measure_id = _measure_id_from_filename("Rahastusmeetmed_Biotech_Call_September_2026.csv")
    assert measure_id == "rahastusmeetmed_biotech_call_september_2026"


def test_every_loaded_measure_has_a_measure_id():
    measures = load_measures()
    assert len(measures) > 0
    for m in measures:
        assert m["measure_id"], f"missing measure_id for {m['name']!r}"


def test_measure_ids_are_unique():
    measures = load_measures()
    ids = [m["measure_id"] for m in measures]
    assert len(ids) == len(set(ids))


def test_get_measure_by_id_roundtrip():
    measures = load_measures()
    target = measures[0]
    found = get_measure_by_id(measures, target["measure_id"])
    assert found is target

    assert get_measure_by_id(measures, "does-not-exist") is None
