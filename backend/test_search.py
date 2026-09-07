"""Tests for the chunk index and measure retrieval (backend/search.py)."""

import pytest

from backend import db, search


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point db module at a temp directory for every test."""
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "app.db")
    monkeypatch.setattr(db, "UPLOADS_DIR", data / "uploads")
    db.init_db()
    yield


def _measure(name, description="", detail=None, measure_id=None):
    """A measure dict with the fields _chunk_texts() reads."""
    return {
        "name": name,
        "measure_id": measure_id or name.lower().replace(" ", "_"),
        "funder": "EIS",
        "description": description,
        "field": "",
        "sector": "",
        "project_type": "",
        "applicant": "ettevõte",
        "phase": "",
        "detail": detail or [],
    }


def _detail_row(vali, vaartus, pohjendus=""):
    return {"vali": vali, "vaartus": vaartus, "kindlus": "kõrge", "pohjendus": pohjendus}


# --- tokenizing and query building ---------------------------------------


def test_tokenize_keeps_estonian_letters():
    assert search.tokenize("Tehisaru kasutuselevõtmise TOETUS") == [
        "tehisaru", "kasutuselevõtmise", "toetus"
    ]


def test_tokenize_drops_stop_words_and_single_characters():
    assert search.tokenize("see on ja a b projekt") == ["projekt"]


def test_fts_query_quotes_every_token():
    assert search.fts_query("tehisaru projekt") == '"tehisaru" OR "projekt"'


def test_fts_query_is_empty_for_text_without_usable_tokens():
    assert search.fts_query("") == ""
    assert search.fts_query("on ja see") == ""
    assert search.fts_query("!!! ??? ...") == ""


@pytest.mark.parametrize(
    "text",
    [
        'AND OR NOT',
        'projekt* AND "tehisaru"',
        'NEAR(a b) ^algus veerg:väärtus',
        'lõpetamata " jutumärk',
    ],
)
def test_fts_syntax_in_user_text_never_reaches_match(text):
    """FTS5 operators in a project description must search, not raise."""
    reindex = search.reindex([_measure("Testmeede", "tehisaru projekt")])
    assert reindex > 0
    # Would raise sqlite3.OperationalError if the tokens were not quoted.
    search.rank_measures([_measure("Testmeede", "tehisaru projekt")], text)


def test_build_query_uses_description_and_clarification_answers():
    query = search.build_query(
        {"project_description": "AI mudel", "region": "Harju maakond"},
        {"Kas projekt on seotud AI-ga?": "Jah"},
    )
    assert "AI mudel" in query
    assert "Jah" in query
    # Structured fields are the hard filter's job, not retrieval's.
    assert "Harju" not in query


# --- indexing -------------------------------------------------------------


def test_reindex_writes_one_chunk_per_master_row_and_detail_row():
    m = _measure("Testmeede", "kirjeldus", [_detail_row("AI nõue", "nõutav")])
    assert search.reindex([m]) == 2

    with db.connect() as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM chunks ORDER BY chunk_id")]
    assert kinds == ["master", "detail"]


def test_reindex_is_idempotent():
    measures = [_measure("A", "üks"), _measure("B", "kaks")]
    first = search.reindex(measures)
    second = search.reindex(measures)
    assert first == second

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == second
        assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == second


def test_reindex_if_stale_skips_the_rebuild_when_nothing_changed():
    measures = [_measure("A", "üks")]
    assert search.reindex_if_stale(measures) > 0
    assert search.reindex_if_stale(measures) == 0


def test_reindex_if_stale_rebuilds_when_a_measure_changes():
    assert search.reindex_if_stale([_measure("A", "üks")]) > 0
    assert search.reindex_if_stale([_measure("A", "hoopis midagi muud")]) > 0


# --- scoring and selection ------------------------------------------------


def test_rank_measures_scores_the_matching_measure_higher():
    ai = _measure("AI meede", "tehisaru kasutuselevõtt ettevõttes")
    laev = _measure("Laevameede", "laevaehituse investeeringutoetus")
    search.reindex([ai, laev])

    scores = search.rank_measures([ai, laev], "tehisaru kasutuselevõtt")
    assert scores["ai_meede"] > scores.get("laevameede", 0.0)


def test_rank_measures_ignores_measures_outside_the_candidate_set():
    ai = _measure("AI meede", "tehisaru kasutuselevõtt")
    laev = _measure("Laevameede", "laevaehitus")
    search.reindex([ai, laev])

    scores = search.rank_measures([laev], "tehisaru")
    assert "ai_meede" not in scores


def test_select_candidates_keeps_everything_below_the_cutoff():
    measures = [_measure(f"Meede {i}") for i in range(5)]
    kept, cut = search.select_candidates(measures, "tehisaru", n=10)
    assert kept == measures
    assert cut == []


def test_select_candidates_narrows_to_n_and_reports_the_rest():
    ai = [_measure(f"AI meede {i}", "tehisaru masinõpe andmetöötlus") for i in range(3)]
    muu = [_measure(f"Muu meede {i}", "laevaehitus sadamataristu") for i in range(3)]
    search.reindex(ai + muu)

    kept, cut = search.select_candidates(ai + muu, "tehisaru masinõpe", n=3)
    assert len(kept) == 3
    assert len(cut) == 3
    assert {m["name"] for m in kept} == {m["name"] for m in ai}
    assert all(c["reason"] for c in cut)


def test_select_candidates_keeps_all_when_the_query_is_unusable():
    measures = [_measure(f"Meede {i}", "sisu") for i in range(5)]
    search.reindex(measures)

    kept, cut = search.select_candidates(measures, "on ja see", n=2)
    assert kept == measures
    assert cut == []


def test_select_candidates_keeps_all_when_nothing_matches():
    measures = [_measure(f"Meede {i}", "laevaehitus") for i in range(5)]
    search.reindex(measures)

    kept, cut = search.select_candidates(measures, "kosmoseraketid", n=2)
    assert kept == measures
    assert cut == []


def test_select_candidates_preserves_catalogue_order_among_kept():
    measures = [_measure(f"Meede {i}", "tehisaru") for i in range(5)]
    search.reindex(measures)

    kept, _ = search.select_candidates(measures, "tehisaru", n=3)
    assert [m["name"] for m in kept] == [m["name"] for m in measures if m in kept]


def test_top_n_falls_back_to_the_default_on_a_bad_value(monkeypatch):
    monkeypatch.setenv("RAG_TOP_N", "mitte-number")
    assert search.top_n() == search.DEFAULT_TOP_N
    monkeypatch.setenv("RAG_TOP_N", "0")
    assert search.top_n() == search.DEFAULT_TOP_N
    monkeypatch.setenv("RAG_TOP_N", "4")
    assert search.top_n() == 4


# --- against the real catalogue ------------------------------------------


def test_real_catalogue_indexes_and_finds_the_ai_measure():
    from backend import measures as measures_mod

    catalogue = measures_mod.load_measures()
    assert search.reindex(catalogue) > len(catalogue)

    scores = search.rank_measures(catalogue, "tehisaru kasutuselevõtt ettevõtte protsessis")
    best = max(scores, key=scores.get)
    assert best == "17"
