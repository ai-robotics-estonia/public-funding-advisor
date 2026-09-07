"""Tests for the deterministic grant-amount comparison.

The regression these guard against: a 49 500 € request was described on the
Rakendusuuringute programm card as falling "nõutud vahemikku 250 000–2 000 000
euro". Nothing in the code compared the two numbers — the model was handed three
similarly labelled quantities (the applicant's euros, the measure's co-financing
percentage, the measure's euro ceiling) and left to do the arithmetic itself.

Every literal value below is taken from rahastusmeetmed/, not invented.
"""

import pytest

from backend import measures


def _measure(**overrides):
    m = {
        "name": "Testmeede", "exclusions": "", "detail": [],
        "min_grant_eur": None, "max_grant_eur": None,
    }
    m.update(overrides)
    return m


# --- parse_amount: the two real CSV values that used to parse into nonsense ---

@pytest.mark.parametrize("value,expected", [
    ("7 500", 7500),
    ("7500", 7500),
    ("2 000 000", 2000000),
    ("49 500 €", 49500),
    ("kuni 7 500 eurot", 7500),          # a qualifier must not block the parse
    ("3 254 309", 3254309),
    ("0", 0),
    ("", None),
    ("määramata", None),
    # Rahastusmeetmed_koik.csv, Tootearendustoetus Eurostars: two competing
    # figures used to be fused into 500000300000.
    ("ebaselge: 500 000 / 300 000", None),
    # Ettevõtja teadus- ja arendustöötaja toetus: a percentage used to be read
    # as a 50 € ceiling, which would reject almost every request.
    ("määramata; kuni 50% TA-töötaja tulumaksust", None),
])
def test_parse_amount(value, expected):
    assert measures.parse_amount(value) == expected


# --- co-financing percentages, in all five spellings the CSVs use ------------

def test_parse_percent_set_covers_every_spelling_in_the_csvs():
    """Five formats for the same field across the data; the scraper compares these."""
    assert measures.parse_percent_set("20%") == (20,)
    assert measures.parse_percent_set("0.2") == (20,)          # detail-CSV style
    assert measures.parse_percent_set("20–75%") == (20, 75)
    assert measures.parse_percent_set("40%-65%") == (40, 65)
    assert measures.parse_percent_set("vähemalt 55%") == (55,)  # only the floor stated
    assert measures.parse_percent_set("30-50") == (30, 50)      # no percent sign
    assert measures.parse_percent_set("määramata") is None
    assert measures.parse_percent_set("") is None


# --- the grant floor, which lives only in free text -------------------------

def test_min_grant_from_master_exclusions():
    m = _measure(exclusions=(
        "ettevõte ei ole Eesti äriregistris; projektiga on alustatud enne taotluse "
        "esitamist; taotletav toetus alla 250 000 € või üle 2 000 000 €; maksu- või "
        "maksevõlg üle 100 € ja ajatamata"
    ))
    assert measures.parse_min_grant(m) == 250000


def test_min_grant_from_detail_rows():
    """A few measures state the floor only in their detail CSV, not the master row."""
    m = _measure(detail=[
        {"vali": "Välistavad tingimused",
         "vaartus": "taotletav toetus alla 20 000 € või üle 100 000 €",
         "kindlus": "kõrge", "pohjendus": ""},
    ])
    assert measures.parse_min_grant(m) == 20000


def test_min_grant_absent_when_measure_states_no_floor():
    assert measures.parse_min_grant(_measure(exclusions="ettevõte on raskustes")) is None


@pytest.mark.parametrize("text", [
    # Every 'alla' clause in the data that is NOT a grant floor.
    "maksu- või maksevõlg üle 100 € ja ajatamata",
    "põhivarainvesteering või abikõlblikud kulud jäävad alla 100 mln €",
    "kontrolli hinnapakkumuste arvu: alla 20 000 euro üks, vähemalt 20 000 eurot kaks",
])
def test_min_grant_ignores_other_alla_clauses(text):
    assert measures.parse_min_grant(_measure(exclusions=text)) is None


# --- the comparison itself --------------------------------------------------

RUP = _measure(name="Rakendusuuringute programm", min_grant_eur=250000,
               max_grant_eur=2000000)
# Innovatsiooniosak: a low ceiling and no statutory floor.
OSAK = _measure(name="Innovatsiooniosak", min_grant_eur=None, max_grant_eur=7500)


def test_below_the_floor_is_a_real_obstacle():
    """The original bug: 49 500 € was described as fitting 250 000–2 000 000 €."""
    penalty, level, text = measures.grant_range_note(RUP, {"requested_grant": "49 500 €"})
    assert (penalty, level) == (-measures.PENALTY_BELOW_MIN_GRANT, "warn")
    assert "49 500" in text and "250 000" in text and "alla" in text


def test_above_the_ceiling_only_means_the_measure_pays_less():
    """Still a recommendation — the applicant can apply, they just receive less."""
    penalty, level, text = measures.grant_range_note(OSAK, {"requested_grant": "100 000 €"})
    assert level == "info"
    assert penalty == -measures.PENALTY_ABOVE_MAX_GRANT
    assert "7 500" in text and "100 000" in text


def test_paying_less_costs_less_than_being_ineligible():
    assert measures.PENALTY_ABOVE_MAX_GRANT < measures.PENALTY_BELOW_MIN_GRANT


def test_an_amount_inside_the_range_says_nothing():
    assert measures.grant_range_note(RUP, {"requested_grant": "500 000"}) == (0, None, None)


@pytest.mark.parametrize("intake", [{}, {"requested_grant": ""}, {"requested_grant": "ei tea"}])
def test_unstated_amount_never_penalizes(intake):
    """Behaviour must be identical to before this feature when the field is empty."""
    assert measures.grant_range_note(RUP, intake) == (0, None, None)


def test_measure_without_numeric_bounds_never_penalizes():
    """'määramata' and 'ebaselge: ...' must not decide anything."""
    open_ended = _measure(min_grant_eur=None, max_grant_eur=None)
    assert measures.grant_range_note(open_ended, {"requested_grant": "49 500"}) == (0, None, None)


# --- the prompt line --------------------------------------------------------

def test_context_line_carries_the_verdict_without_any_derivation():
    line = measures.grant_context_line(RUP, {"requested_grant": "49 500 €"})
    assert "meetme toetus 250 000–2 000 000 €" in line
    assert "taotletav 49 500 €" in line
    assert "VERDIKT: EI MAHU." in line
    # No co-financing arithmetic reaches the prompt any more.
    assert "omafinantseering" not in line.lower()


def test_context_line_distinguishes_paying_less_from_not_fitting():
    assert "VERDIKT: PAKUB VÄHEM." in measures.grant_context_line(
        OSAK, {"requested_grant": "100 000 €"})
    assert "VERDIKT: MAHUB." in measures.grant_context_line(
        RUP, {"requested_grant": "500 000"})


@pytest.mark.parametrize("measure,intake", [
    (RUP, {}),                       # no amount given
    (_measure(), {"requested_grant": "50 000"}),   # measure has no bounds
])
def test_context_line_is_empty_when_there_is_nothing_to_compare(measure, intake):
    assert measures.grant_context_line(measure, intake) == ""


# --- against the real catalogue ---------------------------------------------

def test_real_catalogue_parses_without_nonsense_figures():
    """Every loaded measure must have sane bounds — no fused digits, no percentages."""
    for m in measures.load_measures():
        low, high = m["min_grant_eur"], m["max_grant_eur"]
        if high is not None:
            assert 1000 <= high <= 100_000_000, f"{m['name']}: max_grant_eur={high}"
        if low is not None and high is not None:
            assert low <= high, f"{m['name']}: {low} > {high}"


def test_real_rup_row_carries_its_floor():
    rup = next(m for m in measures.load_measures()
               if m["name"] == "Rakendusuuringute programm")
    assert rup["min_grant_eur"] == 250000
    assert rup["max_grant_eur"] == 2000000


def test_a_small_request_across_the_real_catalogue():
    """49 500 €: only the three measures with a statutory floor above it are blocked,
    and no measure is ever silently dropped."""
    levels = {m["name"]: measures.grant_range_note(m, {"requested_grant": "49 500"})[1]
              for m in measures.load_measures()}
    assert {n for n, lv in levels.items() if lv == "warn"} == {
        "Rakendusuuringute programm",
        "Rakendusuuringute programmi väikeprojektide taotlusvoor",
        "Kaitsetööstuse tootearenduse toetus",
    }
    # Innovatsiooniosak (7 500 €) and Tehisaru (20 000 €) pay less than asked.
    assert levels["Innovatsiooniosak"] == "info"
    assert levels["Tehisaru kasutuselevõtmise toetus"] == "info"
