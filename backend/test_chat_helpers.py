"""Tests for chat.py's pure helper functions (no Claude, no DB)."""

from backend.chat import (
    _comparison_block,
    _detail_block,
    _to_api_messages,
    find_focus_card,
    seed_text,
)


def _recommendation(cards):
    return {"candidates": [], "dropped": [], "ranked_cards": cards}


def test_find_focus_card_matches_and_misses():
    rec = _recommendation([{"name": "A", "measure_id": "01"}, {"name": "B", "measure_id": "02"}])
    assert find_focus_card(rec, "02")["name"] == "B"
    assert find_focus_card(rec, "99") is None


def test_seed_text_with_and_without_checks():
    with_checks = seed_text({"explanation": "Sobib hästi.", "checks": ["Kontrolli A", "Kontrolli B"]})
    assert "Sobib hästi." in with_checks
    assert "Kontrolli A" in with_checks
    assert "Kontrollkohad enne taotlemist" in with_checks

    without_checks = seed_text({"explanation": "Sobib.", "checks": []})
    assert without_checks == "Sobib."

    assert seed_text(None) == ""


def test_detail_block_empty_when_no_detail_rows():
    assert _detail_block({"name": "X", "detail": []}) == ""


def test_detail_block_renders_rows_with_content():
    measure = {
        "name": "X",
        "detail": [
            {"vali": "Nõue", "vaartus": "VKE", "kindlus": "kõrge", "pohjendus": "EIS"},
            {"vali": "Tühi", "vaartus": "", "pohjendus": "", "kindlus": ""},
        ],
    }
    block = _detail_block(measure)
    assert "Meede: X" in block
    assert "Nõue | VKE | kõrge | EIS" in block
    assert "Tühi" not in block  # rows with no value/reason are skipped


def test_comparison_block_excludes_focus_and_formats_others():
    rec = _recommendation([
        {"name": "Focus", "measure_id": "01", "score": 5, "explanation": "e1", "checks": []},
        {"name": "Other", "measure_id": "02", "score": 3, "explanation": "e2", "checks": ["c1"]},
    ])
    block = _comparison_block(rec, "01")
    assert "Focus" not in block
    assert "Other" in block
    assert "c1" in block


def test_comparison_block_empty_when_no_others():
    rec = _recommendation([{"name": "Focus", "measure_id": "01"}])
    assert _comparison_block(rec, "01") == ""


def test_to_api_messages_drops_leading_seed():
    messages = [{"role": "assistant", "content": "seed"}, {"role": "user", "content": "hi"}]
    result = _to_api_messages(messages)
    assert result == [{"role": "user", "content": "hi"}]


def test_to_api_messages_keeps_valid_alternation():
    messages = [
        {"role": "assistant", "content": "seed"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    result = _to_api_messages(messages)
    assert [m["role"] for m in result] == ["user", "assistant", "user"]
    assert result[0]["content"] == "q1"
    assert result[-1]["content"] == "q2"


def test_to_api_messages_collapses_dangling_user_run():
    # A failed reply left two consecutive user turns; only the latest should survive.
    messages = [
        {"role": "assistant", "content": "seed"},
        {"role": "user", "content": "first try (no reply)"},
        {"role": "user", "content": "second try"},
    ]
    result = _to_api_messages(messages)
    assert result == [{"role": "user", "content": "second try"}]
