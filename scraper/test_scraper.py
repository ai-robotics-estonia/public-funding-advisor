"""Scraper tests. No network, no LLM — everything is monkeypatched.

The cost-control guarantees (first run extracts, second run costs nothing) are
asserted by counting mock LLM calls, because that is the property that actually
decides whether this system is affordable to run weekly.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scraper import db, extract, fetch, riigiteataja, robots, run, scheduler


# robots.txt bodies copied verbatim from the real sources, including the blank
# lines inside the 'User-agent: *' group that break urllib's parser.
EIS_ROBOTS = """User-agent: *
Disallow: /*/?s=

# Wordpress core
Disallow: /wp-admin/
Disallow: /wp-include/
Disallow:/wp-json/

Allow: /wp-admin/admin-ajax.php

# Pages
Disallow: /toetatud-projektid
Disallow: /toetatud-projektid/*

# Index
Sitemap: https://eis.ee/sitemap_index.xml

User-agent: Amazonbot
Disallow: /uudised
"""

RT_ROBOTS = """User-agent: *
Disallow: */kohtulahendid/*
Disallow: */kohtuteave/*
"""

UA = "RahastusmeetmedBot/1.0 (ATI internal tool)"


# --- robots ----------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://eis.ee/teenused/innovatsiooniosak/", True),
    ("https://eis.ee/wp-admin/", False),
    ("https://eis.ee/wp-admin/admin-ajax.php", True),   # Allow beats Disallow
    ("https://eis.ee/toetatud-projektid", False),
    ("https://eis.ee/teenused/?s=otsing", False),       # wildcard mid-path + query
])
def test_eis_robots_rules(url, expected):
    assert robots.parse(EIS_ROBOTS, UA).allows(url) is expected


def test_blank_lines_do_not_orphan_rules():
    """urllib.robotparser treats the blank line as a record end and would allow
    /wp-admin/. Our parser must not."""
    assert robots.parse(EIS_ROBOTS, UA).allows("https://eis.ee/wp-admin/") is False


def test_mid_path_wildcard_is_honoured():
    """'*/kohtulahendid/*' never matches under a startswith-based parser."""
    rules = robots.parse(RT_ROBOTS, UA)
    assert rules.allows("https://www.riigiteataja.ee/kohtulahendid/x") is False
    assert rules.allows("https://www.riigiteataja.ee/akt/115022023009") is True


def test_agent_specific_group_does_not_apply_to_us():
    assert robots.parse(EIS_ROBOTS, UA).allows("https://eis.ee/uudised") is True


def test_missing_robots_means_allowed():
    assert robots.parse("", UA).allows("https://example.org/x") is True


# --- HTML cleaning ---------------------------------------------------------

def test_clean_html_drops_chrome_and_keeps_content():
    html = """
    <html><head><style>.a{color:red}</style><script>var x=1;</script></head>
    <body>
      <nav>Menüü Avaleht Kontakt</nav>
      <main><h1>Innovatsiooniosak</h1><p>Suurim toetus 7 500</p>
      <p>Staatus Avatud</p></main>
      <footer>Jalus tekst</footer>
    </body></html>
    """
    text = fetch.clean_html(html)
    assert "Innovatsiooniosak" in text
    assert "Suurim toetus 7 500" in text
    assert "Staatus" in text
    for noise in ("var x=1", "color:red", "Menüü", "Jalus tekst"):
        assert noise not in text


def test_clean_html_hash_is_stable():
    html = "<main><p>Toetus 7 500</p></main>"
    assert fetch.content_hash(fetch.clean_html(html)) == fetch.content_hash(fetch.clean_html(html))


# --- Riigi Teataja ---------------------------------------------------------

def test_act_url_detection():
    assert riigiteataja.is_act_url("https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv")
    assert riigiteataja.act_id("https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv") == "115022023009"
    assert not riigiteataja.is_act_url("https://eis.ee/teenused/innovatsiooniosak/")


@pytest.mark.parametrize("url,expected", [
    ("https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv", False),
    ("https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv=true", False),
    ("https://www.riigiteataja.ee/et/akt/117102023001?leiaKehtiv", False),  # /et/ prefix
    ("https://www.riigiteataja.ee/akt/109012026041", True),                 # bare -> pinned
])
def test_pins_revision(url, expected):
    """?leiaKehtiv makes RT serve the current redaction to whoever clicks, so
    such a link is never 'stuck' on an expired text. Only bare links are."""
    assert riigiteataja.pins_revision(url) is expected


def _fake_act_get(url, accept=None):
    if "leiaKehtiv" in url:
        return ('{"kehtivId": 126062026007,'
                ' "aktiParameetrid": {"pealkiri": "Testmeetme tingimused ja kord"}}')
    if "/126062026007/" in url:
        return "<p>Sõnastuse jõustumise kp:29.06.2026</p><p>§ 1. Uus tekst</p>"
    return ("<p>Sõnastuse jõustumise kp:18.02.2023</p>"
            "<p>Sõnastuse kehtivuse lõpp:27.07.2024</p><p>§ 1. Vana tekst</p>")


def test_fetch_act_resolves_current_redaction(monkeypatch):
    """The CSV links an expired redaction; fetch_act must follow kehtivId and
    report superseded=True, because that is itself a conflict signal."""
    monkeypatch.setattr(riigiteataja, "_get_text", _fake_act_get)
    act = riigiteataja.fetch_act("https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv")

    assert act.linked_id == "115022023009"
    assert act.kehtiv_id == "126062026007"
    assert act.superseded is True
    assert act.valid_until == "27.07.2024"
    assert act.valid_from == "29.06.2026"
    assert "Uus tekst" in act.text
    # Pealkiri tuleb sellest samast JSON-ist, mis kehtivId — kaardi info-rida
    # ("Õigusakt: <nimi> — kehtiv redaktsioon X") ei tohi maksta lisapäringut.
    assert act.act_title == "Testmeetme tingimused ja kord"


def test_fetch_act_keeps_linked_redaction_as_evidence(monkeypatch):
    """valid_until is only visible in the OLD redaction's header. Discarding
    that text left the date unverifiable and it looked invented."""
    monkeypatch.setattr(riigiteataja, "_get_text", _fake_act_get)
    act = riigiteataja.fetch_act("https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv")

    assert act.linked_url == "https://www.riigiteataja.ee/akt/115022023009"
    assert "Vana tekst" in act.linked_text
    assert act.valid_until in act.linked_text     # the claim is checkable offline
    assert act.linked_valid_from == "18.02.2023"
    assert act.pins_revision is False


def test_fetch_act_rejects_non_date_validity(monkeypatch):
    """Currently-valid acts render 'hetkel kehtiv' in that slot. _META_RE is a
    generic 'label:value' split, so anything non-date must be dropped rather
    than quoted as if it were a date."""
    def fake_get(url, accept=None):
        if "leiaKehtiv" in url:
            return '{"kehtivId": 126062026007}'
        if "/126062026007/" in url:
            return "<p>§ 1. Uus tekst</p>"
        return "<p>Sõnastuse kehtivuse lõpp:hetkel kehtiv</p><p>§ 1. Vana</p>"

    monkeypatch.setattr(riigiteataja, "_get_text", fake_get)
    act = riigiteataja.fetch_act("https://www.riigiteataja.ee/akt/115022023009")

    assert act.valid_until is None
    assert act.act_title is None      # puuduv aktiParameetrid ei tohi visata


def test_fetch_act_current_redaction_not_superseded(monkeypatch):
    monkeypatch.setattr(riigiteataja, "_get_text", lambda url, accept=None:
                        '{"kehtivId": 109092025002}' if "leiaKehtiv" in url else "<p>§ 1. Tekst</p>")
    act = riigiteataja.fetch_act("https://www.riigiteataja.ee/akt/109092025002?leiaKehtiv")
    assert act.superseded is False
    assert act.valid_until is None


# --- conflict detection (deterministic, no LLM) ----------------------------

@pytest.mark.parametrize("field,csv_value,source_value,expected", [
    ("max_grant", "7 500", "7500", False),        # formatting only
    ("max_grant", "7 500", "10 000", True),
    ("cofinancing", "20%", "0.2", False),         # detail-CSV style
    ("cofinancing", "20%", "30%", True),
    ("status", "avatud", "Avatud", False),
    ("status", "avatud", "avatud taotlusvoor", False),  # one contains the other
    ("status", "avatud", "suletud", True),
    ("description", "üks sõnastus", "hoopis teine sõnastus", False),  # prose never conflicts
    ("status", "avatud", "", False),              # missing source value
])
def test_values_conflict(field, csv_value, source_value, expected):
    assert extract._values_conflict(field, csv_value, source_value) is expected


# Every pair below was produced by a real scraper run against the live sources.
# The first four were false alarms that the first implementation reported as
# conflicts; the rest are the finds that must keep firing.
@pytest.mark.parametrize("field,csv_value,source_value,expected", [
    ("cofinancing", "30–50%", "30% -50%", False),          # same range, other spelling
    ("cofinancing", "20–75%", "20%-75%", False),
    ("cofinancing", "55–75%", "vähemalt 55%", False),      # source states only the floor
    ("applicant", "ettevõte", "ettevõtja", False),         # synonym
    ("applicant", "ettevõte", "Eesti äriregistrisse kantud äriühing", False),  # more specific
    ("size", "kõik", "vähemalt 8 täistööajaga inimest", False),  # different dimension
    ("cofinancing", "40–65%", "vähemalt 55%", True),       # genuine mismatch
    ("status", "avatud", "Suletud", True),                 # the find that matters
    ("max_grant", "10 000", "15 000", True),
])
def test_values_conflict_against_real_observed_pairs(field, csv_value, source_value, expected):
    assert extract._values_conflict(field, csv_value, source_value) is expected


def test_percent_range_keeps_both_bounds():
    """'%' trails only the last number in '55–75%'; both bounds must survive."""
    assert extract._percent_set("55–75%") == (55, 75)
    assert extract._percent_set("30% -50%") == (30, 50)
    assert extract._percent_set("vähemalt 55%") == (55,)


def _act_page(**overrides):
    page = {"url": "https://www.riigiteataja.ee/akt/115022023009",
            "resolved_url": "https://www.riigiteataja.ee/akt/126062026007",
            "linked_url": "https://www.riigiteataja.ee/akt/115022023009",
            "linked_id": "115022023009", "kehtiv_id": "126062026007",
            "superseded": True, "valid_until": "27.07.2024", "pins_revision": True,
            "act_title": "Testmeetme tingimused ja kord"}
    page.update(overrides)
    return page


def test_pinned_superseded_act_produces_high_severity_conflict():
    measure = {"name": "Test", "link": "https://eis.ee/x"}
    conflicts = extract.find_conflicts(measure, [], [_act_page()])

    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "õigusakt"
    assert conflicts[0]["severity"] == "kõrge"
    # Mõlemad ID-d peavad tsitaadis olema: ilma nendeta ei tea ülevaataja, MIDA
    # MILLEGA CSV-s asendada.
    assert "115022023009" in conflicts[0]["citation"]
    assert "126062026007" in conflicts[0]["citation"]
    # Link viib KEHTIVALE redaktsioonile. Varem osutas see vanale ja jättis
    # mulje, nagu tugineks rakendus aegunud tekstile — tegelikult loeb ta
    # kehtivat, mida tsitaat nüüd ka ütleb.
    assert conflicts[0]["source_url"] == "https://www.riigiteataja.ee/akt/126062026007"
    assert "kehtivast redaktsioonist" in conflicts[0]["citation"]


def test_leiakehtiv_link_produces_no_act_conflict():
    """?leiaKehtiv suunab lugeja kehtivale tekstile ja skreeper loeb kehtivat
    teksti, seega akti uuenemine ei ole vastuolu. Varem tekitas see keskmise
    'õigusakt_muutunud' hoiatuse 10 aktile 14-st ja mattis päris vastuolud."""
    measure = {"name": "Test", "link": "https://eis.ee/x"}
    page = _act_page(url="https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv",
                     pins_revision=False)

    assert extract.find_conflicts(measure, [], [page]) == []


def test_current_act_produces_no_conflict():
    conflicts = extract.find_conflicts({"name": "T"}, [], [_act_page(superseded=False)])
    assert conflicts == []


def test_conflict_citation_omits_missing_ids():
    """Kui redaktsiooni ID-d puuduvad, ei tohi tsitaati sattuda 'None'."""
    page = _act_page(linked_id=None, kehtiv_id=None)
    citation = extract.find_conflicts({"name": "T"}, [], [page])[0]["citation"]
    assert "None" not in citation


# --- extraction validation -------------------------------------------------

def _fake_llm(payload):
    def _call(system, messages, max_tokens, **kwargs):
        return payload
    return _call


def test_extract_drops_unknown_fields_and_uncited_values(monkeypatch):
    monkeypatch.setattr(extract, "complete_chat", _fake_llm("""[
      {"field": "status", "value": "avatud", "confidence": "kõrge", "citation": "Staatus Avatud", "source": "leht"},
      {"field": "väljamõeldud_väli", "value": "x", "confidence": "kõrge", "citation": "tsitaat", "source": "leht"},
      {"field": "max_grant", "value": "7 500", "confidence": "kõrge", "citation": "", "source": "leht"}
    ]"""))
    measure = {"name": "Test", "status": "avatud"}
    sources = [{"url": "https://eis.ee/x", "source_type": "eis", "text": "Staatus Avatud"}]

    facts = extract.extract_facts(measure, sources)

    assert [f["field"] for f in facts] == ["status"]  # unknown field and uncited value dropped


def test_extract_drops_fabricated_citations(monkeypatch):
    """The citation requirement must be real, not nominal. Measured against the
    live model: it satisfied "always cite" by writing prose about not having a
    citation, and by paraphrasing ('võib olla' where the source says 'saab olla').
    Both must be dropped."""
    monkeypatch.setattr(extract, "complete_chat", _fake_llm("""[
      {"field": "status", "value": "avatud", "citation": "Staatus Avatud", "confidence": "kõrge", "source": "leht"},
      {"field": "rnd", "value": "sobiv", "citation": "Ei leidnud otsest viidet", "confidence": "madal", "source": "leht"},
      {"field": "applicant", "value": "VKE", "citation": "Taotlejaks võib olla VKE", "confidence": "kõrge", "source": "leht"}
    ]"""))
    sources = [{"url": "https://eis.ee/x", "source_type": "eis",
                "text": "Staatus\nAvatud\nTaotlejaks saab olla VKE"}]

    facts = extract.extract_facts({"name": "T"}, sources)

    assert [f["field"] for f in facts] == ["status"]


def test_citation_matching_tolerates_whitespace():
    """Cleaned text puts label and value on separate lines; the model quotes them
    inline. That must still count as a match."""
    assert extract._citation_supported("Suurim toetus 7 500",
                                       extract._collapse("Suurim toetus\n7 500"))


def test_extract_returns_empty_on_bad_json(monkeypatch):
    monkeypatch.setattr(extract, "complete_chat", _fake_llm("Vabandust, ma ei saa aidata."))
    facts = extract.extract_facts({"name": "T"}, [{"url": "u", "source_type": "eis", "text": "x"}])
    assert facts == []


def test_extract_skips_llm_when_no_text(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("LLM-i ei tohi kutsuda ilma allikatekstita")
    monkeypatch.setattr(extract, "complete_chat", explode)
    assert extract.extract_facts({"name": "T"}, [{"url": "u", "source_type": "eis", "text": ""}]) == []


# --- database --------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    with db.connect(tmp_path / "state.db") as connection:
        yield connection


def test_pending_change_dedup(conn):
    args = dict(measure_key="02", measure_name="Innovatsiooniosak", url="https://eis.ee/x",
                field="status", old_value="avatud", new_value="suletud", confidence="kõrge",
                citation="c", diff_snippet="", is_high_risk=True)
    assert db.add_pending_change(conn, **args) is True
    assert db.add_pending_change(conn, **args) is False       # duplicate ignored
    assert len(db.get_unreviewed_changes(conn)) == 1

    # Once reviewed, the same change may be recorded again if it reappears.
    db.set_review_decision(conn, db.get_unreviewed_changes(conn)[0]["id"], approved=False)
    assert db.add_pending_change(conn, **args) is True


def test_shared_act_belongs_to_both_measures(conn):
    """Innovatsiooniosak (02) and Arendusosak (03) share act 115022023009.
    Keying page_state on url alone let the second measure steal the first's
    legal source, leaving 02 with no act text in chat."""
    url = "https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv"
    for key in ("02", "03"):
        db.upsert_page_state(conn, url=url, measure_key=key, measure_name=f"M{key}",
                             source_type="legal", content_hash="h", cleaned_text="§ 1.",
                             changed=True)

    for key in ("02", "03"):
        pages = db.get_pages_for_measure(conn, key)
        assert [p["source_type"] for p in pages] == ["legal"], f"meede {key} kaotas õigusakti"


def test_connect_adds_columns_to_an_older_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a db
    written before these columns existed must be ALTERed, not recreated."""
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(str(path))
    legacy.execute("""CREATE TABLE page_state (
        url TEXT NOT NULL, measure_key TEXT NOT NULL, measure_name TEXT NOT NULL,
        source_type TEXT NOT NULL, resolved_url TEXT, etag TEXT, last_modified TEXT,
        content_hash TEXT, cleaned_text TEXT, superseded INTEGER NOT NULL DEFAULT 0,
        valid_until TEXT, valid_from TEXT, last_checked_at TEXT, last_changed_at TEXT,
        PRIMARY KEY (measure_key, url))""")
    legacy.execute("INSERT INTO page_state (url, measure_key, measure_name, source_type)"
                   " VALUES ('u', '02', 'M', 'legal')")
    legacy.commit()
    legacy.close()

    with db.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(page_state)")}
        assert {"linked_id", "kehtiv_id", "linked_url", "linked_text",
                "linked_valid_from", "pins_revision", "act_title"} <= columns
        assert conn.execute("SELECT COUNT(*) FROM page_state").fetchone()[0] == 1

    with db.connect(path) as conn:      # second open must be a no-op
        assert conn.execute("SELECT COUNT(*) FROM page_state").fetchone()[0] == 1


def test_page_state_keeps_linked_text_when_refetch_fails(conn):
    """The old redaction is immutable history and the only evidence for the
    quoted expiry date; a failed refetch must not erase it."""
    common = dict(url="https://www.riigiteataja.ee/akt/115022023009", measure_key="02",
                  measure_name="M", source_type="legal", superseded=True)
    db.upsert_page_state(conn, **common, content_hash="h1", cleaned_text="uus",
                         changed=True, linked_text="Sõnastuse kehtivuse lõpp:27.07.2024")
    db.upsert_page_state(conn, **common, content_hash="h1", cleaned_text="uus",
                         changed=False, linked_text=None)

    state = db.get_page_state(conn, "02", common["url"])
    assert "27.07.2024" in state["linked_text"]


def test_page_state_keeps_text_when_unchanged(conn):
    common = dict(url="https://eis.ee/x", measure_key="02", measure_name="M", source_type="eis")
    db.upsert_page_state(conn, **common, content_hash="h1", cleaned_text="sisu", changed=True)
    db.upsert_page_state(conn, **common, content_hash=None, cleaned_text=None, changed=False)

    state = db.get_page_state(conn, "02", "https://eis.ee/x")
    assert state["cleaned_text"] == "sisu"      # not wiped by the unchanged check
    assert state["content_hash"] == "h1"
    assert state["last_checked_at"] is not None


def test_replace_facts_and_conflicts_are_idempotent(conn):
    for _ in range(2):
        db.replace_measure_facts(conn, "02", [
            {"field": "status", "value": "avatud", "confidence": "kõrge",
             "citation": "c", "source_url": "u"}])
        db.replace_conflicts(conn, "02", [
            {"field": "status", "csv_value": "a", "source_value": "b", "severity": "kõrge"}])
    assert len(db.get_measure_facts(conn, "02")) == 1
    assert len(db.get_conflicts(conn, "02")) == 1


# --- full cycle: the cost guarantees ---------------------------------------

@pytest.fixture
def cycle_env(tmp_path, monkeypatch):
    """One measure, one page, mocked network + LLM, with an LLM call counter."""
    monkeypatch.setattr(run.config, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(run.config, "REVIEW_OUTPUT_DIR", tmp_path / "review")

    page = {"text": "<main><p>Staatus Avatud</p><p>Suurim toetus 7 500</p></main>"}
    calls = {"llm": 0}

    measure = {
        "name": "Innovatsiooniosak", "measure_id": "02",
        "link": "https://eis.ee/teenused/innovatsiooniosak/", "links": [],
        "status": "avatud", "max_grant": "7 500", "detail": [],
    }
    monkeypatch.setattr(run.measures, "load_measures", lambda: [measure])

    def fake_fetch(url, prev_state):
        cleaned = fetch.clean_html(page["text"])
        digest = fetch.content_hash(cleaned)
        changed = digest != (prev_state or {}).get("content_hash")
        return fetch.FetchResult(url=url, changed=changed, status_code=200, etag=None,
                                 last_modified=None, cleaned_text=cleaned if changed else None,
                                 content_hash=digest)
    monkeypatch.setattr(run.fetch, "fetch_if_changed", fake_fetch)

    def fake_llm(system, messages, max_tokens, **kwargs):
        calls["llm"] += 1
        return ('[{"field": "status", "value": "avatud", "confidence": "kõrge",'
                ' "citation": "Staatus Avatud", "source": "leht"}]')
    monkeypatch.setattr(extract, "complete_chat", fake_llm)

    return {"calls": calls, "page": page, "measure": measure}


def test_first_run_seeds_and_extracts(cycle_env):
    """Seeding without extraction (the old plan) leaves the evidence layer empty,
    so the first run must extract."""
    totals = run.run_cycle()
    assert totals["llm_calls"] == 1
    assert totals["facts"] == 1
    assert cycle_env["calls"]["llm"] == 1


def test_second_run_unchanged_costs_nothing(cycle_env):
    run.run_cycle()
    cycle_env["calls"]["llm"] = 0

    totals = run.run_cycle()

    assert totals["llm_calls"] == 0, "muutumatu leht ei tohi maksta LLM-kutsungit"
    assert cycle_env["calls"]["llm"] == 0
    assert totals["changed"] == 0


def test_changed_page_triggers_extraction_and_conflict(cycle_env):
    run.run_cycle()
    cycle_env["calls"]["llm"] = 0
    cycle_env["page"]["text"] = "<main><p>Staatus Suletud</p></main>"

    totals = run.run_cycle()

    assert totals["llm_calls"] == 1
    assert totals["changed"] == 1


def _stub_act(monkeypatch, cycle_env, url):
    cycle_env["measure"]["links"] = [url]
    monkeypatch.setattr(run.riigiteataja, "fetch_act", lambda u: riigiteataja.ActResult(
        url=u, linked_id="115022023009", kehtiv_id="126062026007",
        act_title="Testmeetme tingimused ja kord",
        resolved_url="https://www.riigiteataja.ee/akt/126062026007",
        linked_url="https://www.riigiteataja.ee/akt/115022023009",
        text="§ 1. Tekst", content_hash="abc", superseded=True,
        valid_until="27.07.2024", valid_from="29.06.2026",
        linked_valid_from="18.02.2023", linked_text="Sõnastuse kehtivuse lõpp:27.07.2024",
        pins_revision=riigiteataja.pins_revision(u),
    ))


def test_one_llm_call_per_measure_not_per_source(tmp_path, monkeypatch, cycle_env):
    """A measure with a page AND a legal act still costs exactly one call."""
    _stub_act(monkeypatch, cycle_env, "https://www.riigiteataja.ee/akt/115022023009")

    totals = run.run_cycle()

    assert totals["sources"] == 2
    assert totals["llm_calls"] == 1
    # pinned + superseded -> high-severity conflict -> lands on the human's desk
    assert totals["conflicts"] >= 1
    assert totals["pending"] >= 1


def test_leiakehtiv_act_produces_no_conflict_at_all(tmp_path, monkeypatch, cycle_env):
    """10 of the 13 CSV act links carry ?leiaKehtiv. The scraper reads the
    current redaction and the link sends the reader there too, so an amendment
    is not a conflict — it is just which redaction we are on, shown as neutral
    info on the card. Flagging it left a permanent warning on 10 of 14 acts."""
    _stub_act(monkeypatch, cycle_env, "https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv")

    totals = run.run_cycle()

    assert totals["conflicts"] == 0
    assert totals["pending"] == 0

    key = run.measures.measure_key(cycle_env["measure"])
    with db.connect(run.config.STATE_DB_PATH) as conn:
        assert db.get_conflicts(conn, key) == []
        page = db.get_page_state(conn, key, cycle_env["measure"]["links"][0])
        # Evidence is still kept in the DB, it just no longer reaches the user.
        assert "27.07.2024" in page["linked_text"]
        assert page["act_title"] == "Testmeetme tingimused ja kord"


def test_unchanged_measure_still_recomputes_conflicts(monkeypatch, cycle_env):
    """A conflict is not only a function of the LLM's facts — the act conflict
    comes entirely from page_state and our own comparison rules. Recomputing it
    only when the page content changes froze citations written by an older
    version of the code: the quoted date and the evidence link stopped matching
    and the date became impossible to verify. Must cost no LLM call."""
    _stub_act(monkeypatch, cycle_env, "https://www.riigiteataja.ee/akt/115022023009")
    run.run_cycle()

    key = run.measures.measure_key(cycle_env["measure"])
    with db.connect(run.config.STATE_DB_PATH) as conn:
        db.replace_conflicts(conn, key, [
            {"field": "õigusakt", "csv_value": "x", "source_value": "y",
             "severity": "kõrge", "citation": "vana sõnastus", "source_url": "u"}])

    cycle_env["calls"]["llm"] = 0
    totals = run.run_cycle()

    assert totals["llm_calls"] == 0
    with db.connect(run.config.STATE_DB_PATH) as conn:
        citations = [c["citation"] for c in db.get_conflicts(conn, key)]
    assert "vana sõnastus" not in citations
    assert any("kehtivast redaktsioonist" in c for c in citations)


def test_resolved_conflict_leaves_the_review_desk(monkeypatch, cycle_env):
    """A pending change is a copy of the conflict as it looked when detected. If
    the conflict later disappears, the old row would sit on the desk with a
    citation and link that no longer agree — the same defect, one surface over.
    Rows a human already decided on are history and must survive."""
    _stub_act(monkeypatch, cycle_env, "https://www.riigiteataja.ee/akt/115022023009")
    run.run_cycle()

    key = run.measures.measure_key(cycle_env["measure"])
    with db.connect(run.config.STATE_DB_PATH) as conn:
        pending = [c for c in db.get_unreviewed_changes(conn) if c["field"] == "õigusakt"]
        assert len(pending) == 1, "kinnistatud link peab jõudma töölauale"
        db.add_pending_change(
            conn, measure_key=key, measure_name="M", url="u", field="max_grant",
            old_value="7 500", new_value="15 000", confidence="kõrge",
            citation="c", diff_snippet="", is_high_risk=True)
        db.set_review_decision(conn, pending[0]["id"], approved=False)

    # Now the same act resolves as a ?leiaKehtiv link -> no act conflict at all.
    _stub_act(monkeypatch, cycle_env, "https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv")
    run.run_cycle()

    with db.connect(run.config.STATE_DB_PATH) as conn:
        unreviewed = db.get_unreviewed_changes(conn)
        reviewed = conn.execute(
            "SELECT COUNT(*) FROM pending_changes WHERE approved IS NOT NULL").fetchone()[0]

    # The obsolete max_grant row is gone; the human's rejection is still on record.
    assert [c["field"] for c in unreviewed] == []
    assert reviewed == 1


# --- scheduler claim ---------------------------------------------------------

@pytest.fixture
def sched_env(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.config, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("SCRAPER_INTERVAL_DAYS", "7")
    return tmp_path


def test_first_claim_succeeds(sched_env):
    assert scheduler._claim_run() is True


def test_second_claim_is_refused_within_interval(sched_env):
    """uvicorn --reload restarts many times a day; only the first may run."""
    assert scheduler._claim_run() is True
    assert scheduler._claim_run() is False
    assert scheduler._claim_run() is False


def test_claim_succeeds_again_after_interval(sched_env):
    scheduler._claim_run()
    stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with db.connect(sched_env / "state.db") as conn:
        db.set_meta(conn, "last_run_at", stale)

    assert scheduler._claim_run() is True


def test_corrupt_timestamp_does_not_wedge_the_scheduler(sched_env):
    with db.connect(sched_env / "state.db") as conn:
        db.set_meta(conn, "last_run_at", "not-a-timestamp")

    assert scheduler._claim_run() is True


def test_scheduler_off_by_default(monkeypatch):
    """Tests and local dev must never reach the network."""
    monkeypatch.delenv("SCRAPER_ENABLED", raising=False)
    assert scheduler.start_background_scheduler() is False


# --- allika tüübi tuletamine ------------------------------------------------
# Vale tüüp ei anna viga: backend/sources.py lihtsalt ei leia õigusakti ja
# kaardilt kaob akti rida vaikselt ära. Seepärast on siin oma testid.


def _measure(link, links=()):
    return {"link": link, "links": list(links)}


def test_riigi_teataja_pohilink_on_legal_mitte_eis():
    """Tulemas olev meede lingib otse määrusele — EIS-i lehte tal veel ei ole."""
    sources = run.measure_sources(
        _measure("https://www.riigiteataja.ee/akt/126062026024?leiaKehtiv")
    )
    assert [s["source_type"] for s in sources] == ["legal"]


def test_eis_pohilink_jaab_eis_iks():
    sources = run.measure_sources(
        _measure("https://eis.ee/teenused/innovatsiooniosak/",
                 ["https://www.riigiteataja.ee/akt/115022023009?leiaKehtiv"])
    )
    assert [s["source_type"] for s in sources] == ["eis", "legal"]


def test_mitte_url_taiendav_link_jaetakse_vahele():
    sources = run.measure_sources(
        _measure("https://eis.ee/x", ["Digitaliseerimise teekaardi HEA TAVA"])
    )
    assert [s["url"] for s in sources] == ["https://eis.ee/x"]


def test_iga_meetme_pohilink_saab_oige_tuubi_paris_kataloogis():
    """Ükski Riigi Teataja link ei tohi kataloogis 'eis'-iks sildistuda."""
    from backend import measures

    for m in measures.load_measures():
        for s in run.measure_sources(m):
            if riigiteataja.is_act_url(s["url"]):
                assert s["source_type"] == "legal", (m["name"], s["url"])
