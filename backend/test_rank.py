"""Tests for backend/rank.py's code-owned scoring: enum->points, total->1-5
display mapping, sorting/top-5 cut, and recommendation tips on penalized cards.

The LLM call is faked (no network call) by monkeypatching rank.complete_chat
with a stub that returns canned JSON text.
"""

import json

import pytest

import backend.rank as rank_module
from backend.measures import SOFT_FILTER_FIELDS


def _fake_anthropic(monkeypatch, evaluated):
    """Patch rank.complete_chat so rank() receives `evaluated` (a list of dicts) as JSON."""
    text = json.dumps(evaluated)
    monkeypatch.setattr(rank_module, "complete_chat", lambda **kwargs: text)


def _fake_anthropic_raw(monkeypatch, text):
    """Patch rank.complete_chat so rank() receives the raw `text` verbatim (untouched by json.dumps)."""
    monkeypatch.setattr(rank_module, "complete_chat", lambda **kwargs: text)


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


def _fit(name, project_type_fit="yes", field_fit="yes", purpose_fit="yes", explanation="", checks=None):
    return {
        "name": name,
        "project_type_fit": project_type_fit,
        "field_fit": field_fit,
        "purpose_fit": purpose_fit,
        "explanation": explanation,
        "checks": checks or [],
    }


def test_display_score_thresholds():
    assert rank_module._display_score(45) == 5
    assert rank_module._display_score(38) == 5
    assert rank_module._display_score(37) == 4
    assert rank_module._display_score(30) == 4
    assert rank_module._display_score(29) == 3
    assert rank_module._display_score(20) == 3
    assert rank_module._display_score(19) == 2
    assert rank_module._display_score(10) == 2
    assert rank_module._display_score(9) == 1
    assert rank_module._display_score(0) == 1
    assert rank_module._display_score(-100) == 1  # negative totals clamp to 0, floor at 1


def test_all_yes_with_no_soft_requirements_scores_5(monkeypatch):
    candidates = [_measure(name="Meede A")]
    _fake_anthropic(monkeypatch, [_fit("Meede A")])

    cards = rank_module.rank(candidates, {}, {})
    assert len(cards) == 1
    assert cards[0]["score"] == 5
    assert cards[0]["recommendation"] == []


def test_unrecognized_candidate_name_is_skipped(monkeypatch):
    candidates = [_measure(name="Meede A")]
    _fake_anthropic(monkeypatch, [_fit("Meede mida pole olemas")])

    cards = rank_module.rank(candidates, {}, {})
    assert cards == []


def test_sorting_by_total_desc_then_name(monkeypatch):
    candidates = [_measure(name="Meede A"), _measure(name="Meede B")]
    _fake_anthropic(monkeypatch, [
        _fit("Meede A", purpose_fit="no"),   # 15+15+0 = 30
        _fit("Meede B"),                     # 15+15+15 = 45
    ])

    cards = rank_module.rank(candidates, {}, {})
    assert [c["name"] for c in cards] == ["Meede B", "Meede A"]


def test_top_10_cut(monkeypatch):
    names = [f"Meede {i:02d}" for i in range(12)]
    candidates = [_measure(name=n) for n in names]
    _fake_anthropic(monkeypatch, [_fit(n) for n in names])

    cards = rank_module.rank(candidates, {}, {})
    assert len(cards) == 10


def test_recommendation_tip_appears_on_penalized_card(monkeypatch):
    rnd_question = SOFT_FILTER_FIELDS["rnd"][1]
    candidates = [_measure(name="Meede A", rnd="nõutav")]
    clarifications = {rnd_question: "Ei"}
    _fake_anthropic(monkeypatch, [_fit("Meede A")])

    cards = rank_module.rank(candidates, {}, clarifications)
    assert len(cards) == 1
    # semantic 45 - penalty 12 = 33 -> display bucket 4, not the unpenalized 5
    assert cards[0]["score"] == 4
    assert len(cards[0]["recommendation"]) == 1
    assert "Meede A" in cards[0]["recommendation"][0]


def test_unanswered_clarification_gives_opportunity_tip_no_penalty(monkeypatch):
    rnd_question = SOFT_FILTER_FIELDS["rnd"][1]
    candidates = [_measure(name="Meede A", rnd="nõutav")]
    _fake_anthropic(monkeypatch, [_fit("Meede A")])

    cards = rank_module.rank(candidates, {}, {})  # rnd question left unanswered
    assert cards[0]["score"] == 5  # no penalty
    assert len(cards[0]["recommendation"]) == 1
    assert "võimalus" in cards[0]["recommendation"][0]


def test_empty_candidates_returns_empty_without_calling_client(monkeypatch):
    called = {"yes": False}

    def _boom(**kwargs):
        called["yes"] = True
        raise AssertionError("complete_chat() should not be called for empty candidates")

    monkeypatch.setattr(rank_module, "complete_chat", _boom)
    assert rank_module.rank([], {}, {}) == []
    assert called["yes"] is False


def test_parse_json_array_salvages_objects_before_a_mid_object_cutoff():
    # Two complete objects, then the response is cut off mid-string inside the third
    # (hitting max_tokens with many verbose candidates is a real, observed failure).
    full = json.dumps([_fit("A"), _fit("B"), _fit("C")])
    cutoff = full.rfind('"C"')  # sever partway through the third object's content
    truncated = full[:cutoff + 2]

    parsed = rank_module._parse_json_array(truncated)
    assert [item["name"] for item in parsed] == ["A", "B"]


def test_parse_json_array_still_raises_when_nothing_is_recoverable():
    with pytest.raises(ValueError):
        rank_module._parse_json_array("not json at all")


def test_parse_json_array_repairs_unescaped_quotes_inside_explanation():
    # Observed 502 cause: model wraps a project phrase in raw " inside explanation.
    broken = (
        '[{"name": "Arendusosak", "project_type_fit": "yes", "field_fit": "yes", '
        '"purpose_fit": "yes", '
        '"explanation": "Projekt "toote ja äriarendus" sobib meetmega.", '
        '"checks": ["kontroll"]}]'
    )
    parsed = rank_module._parse_json_array(broken)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "Arendusosak"
    assert 'toote ja äriarendus' in parsed[0]["explanation"]


def test_rank_returns_partial_cards_when_response_is_truncated(monkeypatch):
    # Simulates the real failure: 3 candidates evaluated, response cut off inside
    # the 3rd object. Previously this raised JSONDecodeError -> unhandled 500;
    # now rank() should still return cards for the candidates that did parse.
    candidates = [_measure(name=n) for n in ("A", "B", "C")]
    full = json.dumps([_fit("A"), _fit("B"), _fit("C", explanation="x" * 50)])
    cutoff = full.rfind('"x')
    truncated = full[:cutoff + 5]
    _fake_anthropic_raw(monkeypatch, truncated)

    cards = rank_module.rank(candidates, {}, {})
    assert [c["name"] for c in cards] == ["A", "B"]


# --- grant-amount penalties (see backend/test_grant_range.py for the comparison) ---

def _rup():
    """Floor 250 000 € — a small request cannot apply here at all."""
    return _measure(name="Rakendusuuringute programm", max_grant="2 000 000",
                    max_grant_eur=2000000, min_grant_eur=250000)


def _osak():
    """Ceiling 7 500 €, no floor — a large request just receives less."""
    return _measure(name="Innovatsiooniosak", max_grant="7 500",
                    max_grant_eur=7500, min_grant_eur=None)


def test_below_the_floor_warns_in_red(monkeypatch):
    _fake_anthropic(monkeypatch, [_fit("Rakendusuuringute programm")])
    card = rank_module.rank([_rup()], {"requested_grant": "49 500 €"}, {})[0]
    assert "250 000" in card["grant_warning"]
    assert card["grant_info"] is None


def test_above_the_ceiling_is_information_not_a_warning(monkeypatch):
    """The measure stays a recommendation; it simply pays less than asked."""
    _fake_anthropic(monkeypatch, [_fit("Innovatsiooniosak")])
    card = rank_module.rank([_osak()], {"requested_grant": "100 000 €"}, {})[0]
    assert card["grant_warning"] is None
    assert "7 500" in card["grant_info"]


def test_score_order_fits_beats_pays_less_beats_ineligible(monkeypatch):
    """All fits are 'yes' (45 points), so only the amount penalties can separate these."""
    _fake_anthropic(monkeypatch, [_fit("Rakendusuuringute programm")])
    fits = rank_module.rank([_rup()], {"requested_grant": "500 000"}, {})[0]
    ineligible = rank_module.rank([_rup()], {"requested_grant": "49 500 €"}, {})[0]

    _fake_anthropic(monkeypatch, [_fit("Innovatsiooniosak")])
    pays_less = rank_module.rank([_osak()], {"requested_grant": "100 000 €"}, {})[0]

    assert fits["score"] >= pays_less["score"] > ineligible["score"]


def test_no_amount_leaves_the_card_exactly_as_before(monkeypatch):
    _fake_anthropic(monkeypatch, [_fit("Rakendusuuringute programm")])
    stated = rank_module.rank([_rup()], {"requested_grant": "500 000"}, {})[0]
    blank = rank_module.rank([_rup()], {}, {})[0]
    assert (blank["grant_warning"], blank["grant_info"]) == (None, None)
    assert blank["score"] == stated["score"]


def test_prompt_carries_the_verdict_and_forbids_repeating_it(monkeypatch):
    message = rank_module.build_user_message([_rup()], {"requested_grant": "49 500 €"}, {})
    assert "VERDIKT: EI MAHU" in message
    assert "Taotletav toetuse summa (eurodes): 49 500 €" in message
    # The quantities that used to confuse the model are gone from the prompt.
    assert "Eelarve vahemik" not in message
    assert "Projekti kogumaksumus" not in message
    # And the rule that leaked the check into explanations must be gone.
    assert "maini selgituses" not in rank_module.SYSTEM_PROMPT
    assert "ÄRA kirjuta sellest selgituses" in rank_module.SYSTEM_PROMPT
