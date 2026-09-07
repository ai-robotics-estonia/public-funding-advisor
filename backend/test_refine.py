"""Tests for backend/refine.py and POST /vestlused/{id}/refine.

Covers the parts the second pass owns: batching (so no selected measure is lost
to a truncated answer), the code-owned current/projected scores, rejection of
measures the hard filter dropped, and the LLM-call budget that bounds the
fan-out. The LLM itself is faked — no network call.
"""

import json

import pytest
from fastapi.testclient import TestClient

import backend.refine as refine_module
from backend import db, rate_limit
from backend.main import app
from backend.measures import SOFT_FILTER_FIELDS

client = TestClient(app)

FOCUS_NAME = "Innovatsiooniosak"
OTHER_NAME = "Arendusosak"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    rate_limit.reset()
    yield


def _measure(**overrides):
    m = {
        "name": "Meede A", "funder": "F", "max_grant": "100 000 €", "max_grant_eur": 100000,
        "cofinancing": "20%", "total_budget": "", "status": "avatud", "description": "",
        "link": "", "applicant": "", "size": "", "region": "", "sector": "", "field": "",
        "project_type": "", "ai": "ei ole vajalik", "rnd": "ei ole vajalik", "phase": "",
        "partner": "ei", "university": "ei ole vajalik", "exclusions": "", "checkpoints": "",
        "links": [], "detail": [], "measure_id": "01", "detail_file": None,
    }
    m.update(overrides)
    return m


def _verdict(name, current="partial", projected="yes", proposed=None, changes=None):
    fit = lambda v: {"project_type_fit": v, "field_fit": v, "purpose_fit": v}
    return {
        "name": name,
        "current_fit": fit(current),
        "projected_fit": fit(projected),
        "changes": changes if changes is not None else [
            {"text": "Sõnasta eesmärk ümber", "effort": "väike", "where": "projekti kirjeldus"}
        ],
        "proposed_answers": proposed or [],
        "summary": "Kokkuvõte.",
    }


def _fake_llm(monkeypatch, per_call):
    """Patch refine.complete_chat to return `per_call[i]` for the i-th call.

    A list entry may be a list of verdict dicts, or an Exception to raise.
    Returns the calls list so a test can assert how many batches went out.
    """
    calls = []

    def fake(**kwargs):
        result = per_call[len(calls)]
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return json.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(refine_module, "complete_chat", fake)
    return calls


# --- refine() ---------------------------------------------------------------


def test_batches_in_groups_of_three(monkeypatch):
    names = [f"Meede {i}" for i in range(7)]
    selected = [_measure(name=n) for n in names]
    per_call = [
        [_verdict(n) for n in names[0:3]],
        [_verdict(n) for n in names[3:6]],
        [_verdict(n) for n in names[6:7]],
    ]
    calls = _fake_llm(monkeypatch, per_call)

    cards, failed = refine_module.refine(selected, {}, {})
    assert len(calls) == 3  # ceil(7 / 3)
    assert len(cards) == 7
    assert failed == []


def test_failed_batch_does_not_lose_the_others(monkeypatch):
    names = [f"Meede {i}" for i in range(6)]
    selected = [_measure(name=n) for n in names]
    # Batch 1 fails both attempts, batch 2 succeeds.
    per_call = [
        RuntimeError("timeout"),
        RuntimeError("timeout"),
        [_verdict(n) for n in names[3:6]],
    ]
    calls = _fake_llm(monkeypatch, per_call)

    cards, failed = refine_module.refine(selected, {}, {})
    assert len(calls) == 3  # 2 attempts on the failing batch, 1 on the good one
    assert sorted(c["name"] for c in cards) == ["Meede 3", "Meede 4", "Meede 5"]
    assert sorted(failed) == ["Meede 0", "Meede 1", "Meede 2"]


def test_batch_retried_once_before_giving_up(monkeypatch):
    selected = [_measure(name="Meede A")]
    calls = _fake_llm(monkeypatch, [RuntimeError("boom"), [_verdict("Meede A")]])

    cards, failed = refine_module.refine(selected, {}, {})
    assert len(calls) == 2
    assert [c["name"] for c in cards] == ["Meede A"]
    assert failed == []


def test_measure_missing_from_the_answer_is_reported_not_dropped(monkeypatch):
    selected = [_measure(name="Meede A"), _measure(name="Meede B")]
    _fake_llm(monkeypatch, [[_verdict("Meede A")]])  # model forgot Meede B

    cards, failed = refine_module.refine(selected, {}, {})
    assert [c["name"] for c in cards] == ["Meede A"]
    assert failed == ["Meede B"]


def test_projected_score_beats_current_when_fit_improves(monkeypatch):
    selected = [_measure(name="Meede A")]
    _fake_llm(monkeypatch, [[_verdict("Meede A", current="no", projected="yes")]])

    cards, _ = refine_module.refine(selected, {}, {})
    assert cards[0]["current_score"] == 1     # 0 points
    assert cards[0]["projected_score"] == 5   # 45 points


def test_proposed_answer_lifts_the_projected_score_past_the_penalty(monkeypatch):
    # The measure requires a partner and the user said "Ei" -> penalized today.
    # Proposing to add one must remove that penalty from the projected score only.
    partner_question = SOFT_FILTER_FIELDS["partner"][1]
    selected = [_measure(name="Meede A", partner="nõutav")]
    clarifications = {partner_question: "Ei"}
    _fake_llm(monkeypatch, [[
        _verdict("Meede A", current="yes", projected="yes", proposed=["partner"])
    ]])

    cards, _ = refine_module.refine(selected, {}, clarifications)
    assert cards[0]["current_score"] == 4     # 45 - 12 = 33
    assert cards[0]["projected_score"] == 5   # 45, penalty lifted
    assert cards[0]["proposed_answers"] == ["partner"]


def test_unknown_proposed_dimension_is_ignored(monkeypatch):
    selected = [_measure(name="Meede A")]
    _fake_llm(monkeypatch, [[
        _verdict("Meede A", proposed=["partner", "ettevõtte_suurus"])
    ]])

    cards, _ = refine_module.refine(selected, {}, {})
    assert cards[0]["proposed_answers"] == ["partner"]


def test_unrecognized_effort_falls_back_to_keskmine(monkeypatch):
    selected = [_measure(name="Meede A")]
    _fake_llm(monkeypatch, [[_verdict("Meede A", changes=[
        {"text": "Muuda midagi", "effort": "triviaalne", "where": "x"},
        {"text": "", "effort": "väike", "where": "y"},  # empty text is dropped
    ])]])

    cards, _ = refine_module.refine(selected, {}, {})
    assert cards[0]["changes"] == [
        {"text": "Muuda midagi", "effort": "keskmine", "where": "x"}
    ]


def test_sorted_by_projected_score_then_effort(monkeypatch):
    selected = [_measure(name=n) for n in ("A", "B", "C")]
    cheap = [{"text": "x", "effort": "väike", "where": "w"}]
    costly = [{"text": "x", "effort": "suur", "where": "w"}]
    _fake_llm(monkeypatch, [[
        _verdict("A", projected="partial", changes=cheap),
        _verdict("B", projected="yes", changes=costly),
        _verdict("C", projected="yes", changes=cheap),
    ]])

    cards, _ = refine_module.refine(selected, {}, {})
    # C and B both project to 5; C is cheaper so it leads. A projects lower.
    assert [c["name"] for c in cards] == ["C", "B", "A"]


def test_locked_fields_rule_is_in_the_prompt():
    assert "LUKUSTATUD" in refine_module.SYSTEM_PROMPT
    assert "ettevõtte suurus" in refine_module.SYSTEM_PROMPT
    assert "omafinantseeringu määr" in refine_module.SYSTEM_PROMPT


def test_willing_to_change_reaches_the_prompt():
    message = refine_module.build_user_message(
        [_measure(name="Meede A")], {}, {}, "", {}, "Partnerit ei kaasa."
    )
    assert "Partnerit ei kaasa." in message


def test_empty_selection_makes_no_call(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("complete_chat() should not be called for an empty selection")

    monkeypatch.setattr(refine_module, "complete_chat", boom)
    assert refine_module.refine([], {}, {}) == ([], [])


# --- POST /vestlused/{id}/refine --------------------------------------------


def _vestlus_with_recommendation():
    vid = client.post("/vestlused").json()["vestluse_id"]
    client.post("/recommend", json={"vestluse_id": vid, "intake": {"applicant_type": "ettevote"}})
    db.save_recommendation(
        vid,
        [FOCUS_NAME, OTHER_NAME],
        [{"name": "Suletud meede", "reason": "meede on suletud"}],
        [{"name": FOCUS_NAME, "measure_id": "02", "score": 5, "explanation": "Sobib.",
          "checks": [], "fit": {}}],
    )
    return vid


def test_refine_404_unknown_vestlus():
    r = client.post("/vestlused/ZZZZZ/refine", json={"measure_names": [FOCUS_NAME]})
    assert r.status_code == 404


def test_refine_409_without_recommendation():
    vid = client.post("/vestlused").json()["vestluse_id"]
    r = client.post(f"/vestlused/{vid}/refine", json={"measure_names": [FOCUS_NAME]})
    assert r.status_code == 409


def test_refine_400_for_a_dropped_measure(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("a dropped measure must be rejected before the LLM call")

    monkeypatch.setattr(refine_module, "complete_chat", boom)
    vid = _vestlus_with_recommendation()
    r = client.post(f"/vestlused/{vid}/refine", json={"measure_names": ["Suletud meede"]})
    assert r.status_code == 400
    assert "Suletud meede" in r.json()["detail"]


def test_refine_400_for_empty_selection():
    vid = _vestlus_with_recommendation()
    r = client.post(f"/vestlused/{vid}/refine", json={"measure_names": []})
    assert r.status_code == 400


def test_refine_persists_and_is_returned_on_resume(monkeypatch):
    _fake_llm(monkeypatch, [[_verdict(FOCUS_NAME), _verdict(OTHER_NAME)]])
    vid = _vestlus_with_recommendation()

    r = client.post(
        f"/vestlused/{vid}/refine",
        json={"measure_names": [FOCUS_NAME, OTHER_NAME], "willing_to_change": "Kõike."},
    )
    assert r.status_code == 200
    assert sorted(c["name"] for c in r.json()["cards"]) == sorted([FOCUS_NAME, OTHER_NAME])

    resumed = client.get(f"/vestlused/{vid}").json()["refinement"]
    assert len(resumed["cards"]) == 2
    assert resumed["willing_to_change"] == "Kõike."


def test_refine_is_rerunnable_with_a_different_selection(monkeypatch):
    _fake_llm(monkeypatch, [[_verdict(FOCUS_NAME)], [_verdict(OTHER_NAME)]])
    vid = _vestlus_with_recommendation()

    client.post(f"/vestlused/{vid}/refine", json={"measure_names": [FOCUS_NAME]})
    second = client.post(f"/vestlused/{vid}/refine", json={"measure_names": [OTHER_NAME]})
    assert second.status_code == 200

    latest = client.get(f"/vestlused/{vid}").json()["refinement"]
    assert [c["name"] for c in latest["cards"]] == [OTHER_NAME]


def test_refine_502_when_every_batch_fails(monkeypatch):
    _fake_llm(monkeypatch, [RuntimeError("boom")] * 2)
    vid = _vestlus_with_recommendation()

    r = client.post(f"/vestlused/{vid}/refine", json={"measure_names": [FOCUS_NAME]})
    assert r.status_code == 502
    assert client.get(f"/vestlused/{vid}").json()["refinement"] is None


def test_llm_budget_counts_calls_not_requests(monkeypatch):
    """A fan-out request is charged per batch, so the budget bounds LLM work."""
    monkeypatch.setattr(rate_limit, "LLM_MAX_CALLS", 2)
    _fake_llm(monkeypatch, [[_verdict(FOCUS_NAME)], [_verdict(OTHER_NAME)]])
    vid = _vestlus_with_recommendation()

    assert client.post(
        f"/vestlused/{vid}/refine", json={"measure_names": [FOCUS_NAME]}
    ).status_code == 200
    assert client.post(
        f"/vestlused/{vid}/refine", json={"measure_names": [OTHER_NAME]}
    ).status_code == 200
    # Budget spent: the third run has no calls left.
    blocked = client.post(f"/vestlused/{vid}/refine", json={"measure_names": [FOCUS_NAME]})
    assert blocked.status_code == 429

    # The request bucket is untouched, so plain reads still work.
    assert client.get(f"/vestlused/{vid}").status_code == 200
