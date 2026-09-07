"""Tests for the soft-requirement classifier and score-penalty/tip generator.

classify_requirement values below are the actual strings found in
rahastusmeetmed/Rahastusmeetmed_koik.csv (see the data-model check in
.cursor/plans/nr2_scoring_plan.plan.md), not idealized ones.
"""

from backend.measures import SOFT_FILTER_FIELDS, classify_requirement, soft_filter_notes


def test_classify_required_values():
    assert classify_requirement("nõutav") == "required"
    assert classify_requirement("jah") == "required"
    assert classify_requirement("konsortsium nõutav") == "required"
    assert classify_requirement("teenusepakkuja vajalik") == "required"
    assert classify_requirement(
        "vähemalt 2 sõltumatut juriidilist isikut vähemalt 2 osalevast Eureka riigist"
    ) == "required"


def test_classify_optional_values():
    assert classify_requirement("sobib") == "optional"
    assert classify_requirement("sobiv") == "optional"
    assert classify_requirement("soovitatav") == "optional"
    assert classify_requirement("ei ole kohustuslik") == "optional"


def test_classify_none_values():
    assert classify_requirement("") == "none"
    assert classify_requirement("ei ole vajalik") == "none"
    assert classify_requirement("ei") == "none"


def test_classify_is_case_and_whitespace_insensitive():
    assert classify_requirement("  NÕUTAV  ") == "required"
    assert classify_requirement("  Sobib ") == "optional"


def _question(field):
    return SOFT_FILTER_FIELDS[field][1]


def _measure(**overrides):
    m = {
        "name": "Testmeede",
        "max_grant": "100 000 €",
        "rnd": "ei ole vajalik",
        "ai": "ei ole vajalik",
        "partner": "ei",
        "university": "ei ole vajalik",
    }
    m.update(overrides)
    return m


def test_penalty_fires_only_for_required_and_explicit_ei():
    m = _measure(rnd="nõutav")
    penalty, tips = soft_filter_notes(m, {_question("rnd"): "Ei"})
    assert penalty == -12
    assert len(tips) == 1
    assert "Testmeede" in tips[0]
    assert "100 000" in tips[0]


def test_required_with_missing_answer_gives_opportunity_tip_not_penalty():
    m = _measure(partner="nõutav")
    penalty, tips = soft_filter_notes(m, {})
    assert penalty == 0
    assert len(tips) == 1
    assert "võimalus" in tips[0]


def test_optional_with_ei_gives_tip_not_penalty():
    m = _measure(university="sobiv")
    penalty, tips = soft_filter_notes(m, {_question("university"): "Ei"})
    assert penalty == 0
    assert len(tips) == 1
    assert "võimalus" in tips[0]


def test_optional_with_missing_answer_gives_tip_not_penalty():
    m = _measure(ai="sobib")
    penalty, tips = soft_filter_notes(m, {})
    assert penalty == 0
    assert len(tips) == 1


def test_none_level_produces_nothing_even_with_ei():
    m = _measure(ai="ei ole vajalik")
    penalty, tips = soft_filter_notes(m, {_question("ai"): "Ei"})
    assert penalty == 0
    assert tips == []


def test_jah_answer_skips_regardless_of_level():
    m = _measure(rnd="nõutav")
    penalty, tips = soft_filter_notes(m, {_question("rnd"): "Jah"})
    assert penalty == 0
    assert tips == []


def test_multiple_penalties_stack_and_tips_cap_at_two_penalized_first():
    m = _measure(rnd="nõutav", partner="nõutav", ai="sobib")
    clarifications = {
        _question("rnd"): "Ei",
        _question("partner"): "Ei",
        _question("ai"): "Ei",
    }
    penalty, tips = soft_filter_notes(m, clarifications)
    assert penalty == -24
    assert len(tips) == 2
    # penalized tips (pitch framing) come first, the optional/opportunity tip is dropped
    assert all("võimalus" not in t for t in tips)
