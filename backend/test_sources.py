"""Tests for the scraper evidence layer as consumed by rank.py and chat.py.

The most important property here is the negative one: with no scraper database,
every accessor must return empty and the existing prompts must be byte-identical
to what they were before this feature existed.
"""

from pathlib import Path

import pytest

from backend import chat, rank, sources
from scraper import db as scraper_db


MEASURE = {
    "name": "Innovatsiooniosak", "measure_id": "02", "funder": "EIS",
    "max_grant": "7 500", "cofinancing": "20%", "total_budget": "3 254 309",
    "status": "avatud", "description": "Kirjeldus", "link": "https://eis.ee/x",
    "applicant": "VKE", "size": "VKE", "region": "Eesti", "sector": "kõik",
    "field": "innovatsioon", "project_type": "tootearendus", "ai": "sobib",
    "rnd": "sobib", "phase": "prototüüp", "partner": "vajalik",
    "university": "sobiv", "exclusions": "mitte-VKE", "checkpoints": "kontrolli VTA",
    "links": [], "detail": [], "detail_file": None,
}


@pytest.fixture
def no_scraper_db(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "DB_PATH", str(tmp_path / "missing.db"))


@pytest.fixture
def scraper_db_with_conflict(monkeypatch, tmp_path):
    """A populated scraper DB: one EIS page, one superseded act, one conflict."""
    path = tmp_path / "scraper_state.db"
    with scraper_db.connect(path) as conn:
        scraper_db.upsert_page_state(
            conn, url="https://eis.ee/x", measure_key="02", measure_name="Innovatsiooniosak",
            source_type="eis", content_hash="h1", cleaned_text="Staatus Suletud", changed=True,
        )
        scraper_db.upsert_page_state(
            conn, url="https://www.riigiteataja.ee/akt/115022023009", measure_key="02",
            measure_name="Innovatsiooniosak", source_type="legal",
            resolved_url="https://www.riigiteataja.ee/akt/126062026007",
            content_hash="h2", cleaned_text="§ 1. Toetuse maksimaalne suurus on 7500 eurot.",
            changed=True, superseded=True, valid_until="27.07.2024", valid_from="29.06.2026",
            linked_id="115022023009", kehtiv_id="126062026007",
            act_title="Innovatsiooniosaku toetuse tingimused",
        )
        scraper_db.replace_conflicts(conn, "02", [
            {"field": "status", "csv_value": "avatud", "source_value": "suletud",
             "severity": "kõrge", "citation": "Staatus Suletud", "source_url": "https://eis.ee/x"},
            {"field": "õigusakt", "csv_value": "https://www.riigiteataja.ee/akt/115022023009",
             "source_value": "https://www.riigiteataja.ee/akt/126062026007", "severity": "kõrge",
             "citation": ('CSV link on kinnistatud redaktsioonile 115022023009, mis enam ei '
                          'kehti. Rakendus loeb andmed kehtivast redaktsioonist 126062026007; '
                          'CSV link tuleks parandada.'),
             "source_url": "https://www.riigiteataja.ee/akt/126062026007"},
        ])
    monkeypatch.setattr(sources, "DB_PATH", str(path))
    return path


# --- graceful degradation --------------------------------------------------

def test_all_accessors_empty_without_db(no_scraper_db):
    assert sources.rank_block(MEASURE) == ""
    assert sources.chat_sources(MEASURE) == ""
    assert sources.card_status(MEASURE) is None
    assert sources.conflicts(MEASURE) == []


def test_rank_prompt_unchanged_without_scraper(no_scraper_db):
    """The candidate block must not gain stray whitespace or headers."""
    block = rank._candidate_block(MEASURE, {})
    assert "Ajakohane allikainfo" not in block
    assert block.endswith("Kontrollkohad: kontrolli VTA")


def test_chat_context_unchanged_without_scraper(no_scraper_db, monkeypatch):
    monkeypatch.setattr(chat.files, "build_context_text", lambda vid: "")
    context = chat.build_system_context("V1", MEASURE, {"ranked_cards": []}, None)
    # The rule text mentions "Allikatekstid"; what must be absent is the section.
    assert "## Allikatekstid" not in context


def test_corrupt_db_is_treated_as_absent(monkeypatch, tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_text("this is not a sqlite file")
    monkeypatch.setattr(sources, "DB_PATH", str(broken))
    assert sources.rank_block(MEASURE) == ""
    assert sources.card_status(MEASURE) is None


# --- rank layer ------------------------------------------------------------

def test_rank_block_reports_conflicts_compactly(scraper_db_with_conflict):
    block = sources.rank_block(MEASURE)
    assert "VASTUOLU status" in block
    assert 'CSV="avatud"' in block and 'allikas="suletud"' in block
    assert "CSV LINGI VIGA" in block
    # The model must know which redaction the facts came from, otherwise the
    # link complaint below reads as "this measure's data is outdated".
    assert "Innovatsiooniosaku toetuse tingimused" in block
    assert "redaktsioon 126062026007" in block
    # It must stay small: the ranking prompt holds every candidate at once.
    assert len(block) <= sources.RANK_BLOCK_CHAR_BUDGET + 60
    # And it must never carry the full source text.
    assert "§ 1. Toetuse maksimaalne" not in block


def test_rank_candidate_block_includes_source_note(scraper_db_with_conflict):
    assert "VASTUOLU status" in rank._candidate_block(MEASURE, {})


def test_card_status_surfaces_conflicts_for_ui(scraper_db_with_conflict):
    status = sources.card_status(MEASURE)
    assert status["superseded"] is True
    assert status["has_legal_source"] is True
    assert {c["field"] for c in status["conflicts"]} == {"status", "õigusakt"}
    assert status["checked_at"] is not None and len(status["checked_at"]) == 10


def test_act_conflict_links_to_the_current_redaction(scraper_db_with_conflict):
    """The complaint is about the CSV link, not about the act's content — the
    scraper already read the current redaction. Sending the reader to the OLD
    one (as this did before) made it look like the app itself was working off
    expired text."""
    act = next(c for c in sources.card_status(MEASURE)["conflicts"]
               if c["field"] == "õigusakt")
    assert act["source_url"] == "https://www.riigiteataja.ee/akt/126062026007"
    assert "115022023009" in act["citation"]      # what to replace
    assert "126062026007" in act["citation"]      # what to replace it with


# --- legal act info (neutral, not a warning) -------------------------------

def test_card_status_names_the_current_redaction(scraper_db_with_conflict):
    """An amended act used to be a permanent ⚠ on 10 of 14 measures. What the
    user actually needs is which redaction the data reflects."""
    act = sources.card_status(MEASURE)["legal_act"]
    assert act["title"] == "Innovatsiooniosaku toetuse tingimused"
    assert act["kehtiv_id"] == "126062026007"
    assert act["valid_from"] == "29.06.2026"
    assert act["url"] == "https://www.riigiteataja.ee/akt/126062026007"


def _leiakehtiv_db(monkeypatch, tmp_path):
    path = tmp_path / "s.db"
    with scraper_db.connect(path) as conn:
        scraper_db.upsert_page_state(
            conn, url="https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv",
            measure_key="02", measure_name="M", source_type="legal",
            resolved_url="https://www.riigiteataja.ee/akt/126062026007",
            content_hash="h", cleaned_text="§ 1. Tekst", changed=True,
            superseded=True, valid_until="27.07.2024", valid_from="29.06.2026",
            linked_id="115022023009", kehtiv_id="126062026007", pins_revision=False,
            act_title="Innovatsiooniosaku toetuse tingimused")
    monkeypatch.setattr(sources, "DB_PATH", str(path))
    return path


def test_chat_prompt_carries_only_the_current_redaction(monkeypatch, tmp_path):
    """The advisor must answer from the valid text. Mentioning the superseded
    redaction and its expiry date invited answers hedged on outdated law."""
    _leiakehtiv_db(monkeypatch, tmp_path)

    text = sources.chat_sources(MEASURE)

    assert "Innovatsiooniosaku toetuse tingimused" in text
    assert "126062026007" in text
    assert "kinnistatud" not in text
    assert "27.07.2024" not in text            # old redaction's expiry
    assert "115022023009" not in text          # old redaction's id


# --- refreshing locked recommendations -------------------------------------

def test_refresh_cards_replaces_frozen_source_status(scraper_db_with_conflict):
    """ranked_cards_json is a locked snapshot, but the source layer inside it is
    not a decision — it is a claim about the world. Frozen, it kept showing a
    citation whose link no longer matched the quoted date."""
    recommendation = {"ranked_cards": [
        {"name": "Innovatsiooniosak", "measure_id": "02", "score": 4,
         "source_status": {"checked_at": "2026-07-21", "conflicts": [
             {"field": "õigusakt", "citation": "vana külmutatud tekst",
              "source_url": "https://www.riigiteataja.ee/akt/115022023009"}]}},
    ]}

    sources.refresh_cards(recommendation, [MEASURE])

    status = recommendation["ranked_cards"][0]["source_status"]
    act = next(c for c in status["conflicts"] if c["field"] == "õigusakt")
    assert "vana külmutatud tekst" not in act["citation"]
    assert act["source_url"] == "https://www.riigiteataja.ee/akt/126062026007"
    # legal_act did not exist when this snapshot was frozen; the refresh must
    # add it, otherwise older recommendations never gain the act info line.
    assert status["legal_act"]["kehtiv_id"] == "126062026007"
    # The locked parts of the card must not move.
    assert recommendation["ranked_cards"][0]["score"] == 4


def test_refresh_cards_keeps_status_when_scraper_knows_nothing(no_scraper_db):
    """Without a scraper DB card_status() returns None. That must leave the
    snapshot alone rather than wiping a status the scraper once produced."""
    frozen = {"checked_at": "2026-07-21", "conflicts": []}
    recommendation = {"ranked_cards": [
        {"name": "Innovatsiooniosak", "measure_id": "02", "source_status": frozen},
        {"name": "Tundmatu meede", "measure_id": "99", "source_status": None},
    ]}

    sources.refresh_cards(recommendation, [MEASURE])

    assert recommendation["ranked_cards"][0]["source_status"] == frozen
    assert recommendation["ranked_cards"][1]["source_status"] is None


def test_refresh_cards_tolerates_missing_recommendation():
    assert sources.refresh_cards(None, [MEASURE]) is None


# --- chat layer ------------------------------------------------------------

def test_chat_sources_carries_full_legal_text(scraper_db_with_conflict):
    text = sources.chat_sources(MEASURE)
    assert "§ 1. Toetuse maksimaalne suurus on 7500 eurot." in text
    assert "Kehtiv õigusakt" in text
    assert "126062026007" in text
    # The heading must name the redaction the text belongs to, so the advisor
    # can say which version it is answering from.
    assert "jõust. 29.06.2026" in text


def test_chat_sources_puts_legal_act_first(scraper_db_with_conflict):
    text = sources.chat_sources(MEASURE)
    assert text.index("Kehtiv õigusakt") < text.index("Meetme leht")


def test_chat_sources_truncates_page_not_act(monkeypatch, tmp_path):
    path = tmp_path / "s.db"
    with scraper_db.connect(path) as conn:
        scraper_db.upsert_page_state(
            conn, url="https://eis.ee/x", measure_key="02", measure_name="M",
            source_type="eis", content_hash="h", cleaned_text="LEHT" * 20_000, changed=True)
        scraper_db.upsert_page_state(
            conn, url="https://rt/akt/1", measure_key="02", measure_name="M",
            source_type="legal", content_hash="h", cleaned_text="AKT" * 5_000, changed=True)
    monkeypatch.setattr(sources, "DB_PATH", str(path))

    text = sources.chat_sources(MEASURE)

    assert "AKT" * 5_000 in text, "õigusakti ei tohi kärpida"
    assert "kärbitud" in text
    assert len(text) < sources.CHAT_SOURCE_CHAR_BUDGET + 1_000


def test_chat_context_includes_sources_when_available(scraper_db_with_conflict, monkeypatch):
    monkeypatch.setattr(chat.files, "build_context_text", lambda vid: "")
    context = chat.build_system_context("V1", MEASURE, {"ranked_cards": []}, None)
    assert "## Allikatekstid" in context
    assert "§ 1. Toetuse maksimaalne" in context
    # Instructions must precede the long text so they are not buried.
    assert context.index("VIITA ALATI paragrahvile") < context.index("## Allikatekstid")


# --- measure_key -----------------------------------------------------------

def test_measure_key_falls_back_to_name_without_detail_file():
    from backend.measures import measure_key
    assert measure_key({"measure_id": "02", "name": "Innovatsiooniosak"}) == "02"
    assert measure_key({"measure_id": None, "name": "Biotech call – September 2026"}) \
        == "biotech call september 2026"


# --- where the state database lives ----------------------------------------
# A wrong path here does not raise: _connect() just returns None, every accessor
# returns empty, and the UI silently loses its legal-act boxes. That failure mode
# is invisible, so it gets its own tests.


def test_state_db_path_matches_the_scrapers_own_config():
    """One definition, not two. They drifted once and the boxes disappeared."""
    from scraper import config
    assert sources.DB_PATH == str(config.STATE_DB_PATH)


def test_state_db_follows_app_data_dir(monkeypatch):
    """The container mounts its only writable volume elsewhere than the source tree.

    backend/db.py honours APP_DATA_DIR; the scraper state has to sit beside
    app.db or the app reads a path that cannot exist under a read-only rootfs.
    """
    import importlib

    from backend import db as db_module

    monkeypatch.setenv("APP_DATA_DIR", "/srv/andmed")
    config = importlib.reload(importlib.import_module("scraper.config"))
    try:
        assert config.STATE_DB_PATH == Path("/srv/andmed/scraper_state.db")
        # Same parent as app.db, which is the actual invariant.
        assert config.STATE_DB_PATH.parent == Path(
            importlib.reload(db_module).DATA_DIR
        )
        # Review markdown is written at runtime too, so it cannot live in /app.
        assert config.REVIEW_OUTPUT_DIR == Path("/srv/andmed/katsetused")
    finally:
        monkeypatch.delenv("APP_DATA_DIR", raising=False)
        importlib.reload(importlib.import_module("scraper.config"))
        importlib.reload(db_module)
