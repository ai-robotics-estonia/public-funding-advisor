"""Load, merge, and filter the Estonian funding-measure CSVs.

The data lives in rahastusmeetmed/:
- Rahastusmeetmed_koik.csv: the master table, one row per measure (~18 rows).
- per-measure detail files: long-format (Väli, Soovitatud väärtus, Kindlus, Põhjendus)
  with confidence ratings and citations to EIS / Riigi Teataja.

Everything is UTF-8. Detail files are matched to master rows by a normalized,
fuzzy comparison of the measure name, because a few detail files spell the name
slightly differently (en-dashes, parentheses, reordered words).
"""

import csv
import difflib
import logging
import os
import re

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rahastusmeetmed")
MASTER_FILE = "Rahastusmeetmed_koik.csv"

# Column positions in the master table (Rahastusmeetmed_koik.csv). The header has
# 28 columns; index 22 onward are the "Täiendavad lingid" links (mostly empty).
COL_FUNDER = 0
COL_NAME = 1
COL_MAX_GRANT = 3
COL_COFINANCING = 4
COL_TOTAL_BUDGET = 5
COL_STATUS = 6
COL_DESCRIPTION = 7
COL_LINK = 8
COL_APPLICANT = 9
COL_SIZE = 10
COL_REGION = 11
COL_SECTOR = 12
COL_FIELD = 13
COL_PROJECT_TYPE = 14
COL_AI = 15
COL_RND = 16
COL_PHASE = 17
COL_PARTNER = 18
COL_UNIVERSITY = 19
COL_EXCLUSIONS = 20
COL_CHECKPOINTS = 21
COL_LINKS_START = 22


# Clarifying-question rules. Each rule fires (the question is shown) when at least
# one surviving candidate has the given field value in the trigger set. The trigger
# values below are the actual values found in the data, not idealized ones.
QUESTION_RULES = [
    ("rnd", {"nõutav", "jah"}, "Kas projekt hõlmab rakendusuuringut või tootearendust (TRL 3–7)?"),
    ("university", {"sobiv"}, "Kas soovite projekti kaasata ülikooli või teadusasutuse?"),
    ("partner", {"nõutav", "konsortsium nõutav"}, "Kas teil on projekti jaoks vajalikud partnerid või konsortsium olemas?"),
    ("ai", {"sobib"}, "Kas projekt on seotud tehisintellekti (AI) arendusega?"),
]

# Soft-requirement classification for the score penalty + recommendation tips.
# A measure is never dropped for these — an unmet requirement only lowers its
# score (if it's a hard requirement the user explicitly said "Ei" to) and/or
# surfaces a tip suggesting the angle that would improve fit or grant size.
REQUIRED_VALUES = {"nõutav", "jah", "konsortsium nõutav", "teenusepakkuja vajalik"}
OPTIONAL_VALUES = {"sobib", "sobiv", "soovitatav", "ei ole kohustuslik"}
NONE_VALUES = {"ei ole vajalik", "ei"}


def classify_requirement(value):
    """Classify a measure's soft-requirement field value: 'required', 'optional', or 'none'."""
    v = value.strip().lower()
    if not v or v in NONE_VALUES:
        return "none"
    if v in REQUIRED_VALUES or "vähemalt" in v:  # Eureka-style consortium clause
        return "required"
    return "optional"  # OPTIONAL_VALUES + anything unrecognized (conservative default)


# field -> (label used in tip templates, exact QUESTION_RULES question text used as
# the lookup key into the clarifications dict — same global Jah/Ei answer per dimension).
SOFT_FILTER_FIELDS = {
    "rnd": ("rakendusuuringut või tootearendust (T&A)",
            "Kas projekt hõlmab rakendusuuringut või tootearendust (TRL 3–7)?"),
    "ai": ("tehisintellekti (AI) arendust",
           "Kas projekt on seotud tehisintellekti (AI) arendusega?"),
    "partner": ("partnerit või konsortsiumi",
                "Kas teil on projekti jaoks vajalikud partnerid või konsortsium olemas?"),
    "university": ("ülikooli või teadusasutust",
                   "Kas soovite projekti kaasata ülikooli või teadusasutuse?"),
}

PENALTY_PER_DIMENSION = 12

# Two different severities, because the two bounds mean different things.
# Below the floor the applicant simply cannot apply for that amount — a harder
# signal than one unmet soft requirement, so it outweighs PENALTY_PER_DIMENSION.
# Above the ceiling they can still apply and just receive less, so it costs much
# less. Neither ever removes a card: both only move it down the list.
PENALTY_BELOW_MIN_GRANT = 20
PENALTY_ABOVE_MAX_GRANT = 8

TEMPLATE_PENALIZED = 'Kui kaasate {label}, võib „{name}" sobida paremini või pakkuda suuremat toetust (kuni {max_grant}).'
TEMPLATE_OPPORTUNITY = 'Kui kaasate {label}, on teil võimalus „{name}" raames toetust taotleda.'


def soft_filter_notes(measure, clarifications):
    """Score penalty + recommendation tips from unmet or optional soft requirements.

    Returns (penalty, tips): penalty is <= 0 (PENALTY_PER_DIMENSION per unmet hard
    requirement), tips is a list of up to 2 short Estonian strings, penalized ones
    first. A missing/unknown answer never penalizes — it only ever produces an
    inviting "opportunity" tip.
    """
    clarifications = clarifications or {}
    penalty = 0
    penalized_tips = []
    opportunity_tips = []

    for field, (label, question) in SOFT_FILTER_FIELDS.items():
        level = classify_requirement(measure[field])
        if level == "none":
            continue
        answer = clarifications.get(question)
        if answer == "Jah":
            continue

        if level == "required" and answer == "Ei":
            penalty -= PENALTY_PER_DIMENSION
            penalized_tips.append(TEMPLATE_PENALIZED.format(
                label=label, name=measure["name"], max_grant=measure["max_grant"]))
        else:
            opportunity_tips.append(TEMPLATE_OPPORTUNITY.format(label=label, name=measure["name"]))

    return penalty, (penalized_tips + opportunity_tips)[:2]


def _norm(s):
    """Normalize a measure name for fuzzy matching: lowercase, drop parentheses,
    unify dashes, strip punctuation and collapse whitespace."""
    s = s.lower().replace("–", "-").replace("—", "-")
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[^a-zõäöüšž0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cell(row, i):
    """Safe cell access; returns stripped string or ''."""
    return row[i].strip() if i < len(row) else ""


# One run of digits, allowing space / non-breaking-space thousand separators.
_AMOUNT_GROUP_RE = re.compile(r"\d[\d\s\u00a0\u202f]*\d|\d")


def parse_amount(s):
    """Turn a messy amount string into an int euro value, or None if not a single amount.

    Handles space thousand-separators ('35 000'), leading tabs, euro signs and a
    leading qualifier ('kuni 7 500 eurot'). Returns None rather than a wrong
    number for the two shapes that used to slip through, because stripping every
    non-digit character silently fused or mistyped them:

      'ebaselge: 500 000 / 300 000'            -> 500000300000, a nonsense figure
      'määramata; kuni 50% TA-töötaja tulumaksust' -> 50, a percentage read as euros

    Both are real values in Rahastusmeetmed_koik.csv. They were harmless while
    nothing compared the parsed number, and became wrong answers the moment
    grant_range_note() started deciding fit from it. Callers keep the raw string
    for display either way.
    """
    if not s or "%" in s:
        return None
    groups = _AMOUNT_GROUP_RE.findall(s)
    if len(groups) != 1:  # no number at all, or several competing ones
        return None
    digits = re.sub(r"[^\d]", "", groups[0])
    return int(digits) if digits else None


def parse_percent_set(value):
    """Every percentage in a co-financing value, sorted, or None if there is none.

    Omafinantseering is written five different ways across the CSVs: '0.2' in a
    detail file, '20%' in the master row for the same measure, ranges as '20–75%'
    or '30-50', and prose like 'vähemalt 55%'. Returning the whole set (not just
    the first number) is what makes those spellings comparable — the scraper's
    conflict check relies on subset containment, so 'vähemalt 55%' (55,) does not
    contradict '55–75%' (55, 75).
    """
    if not value:
        return None
    # A percent sign anywhere means EVERY number in the string is a percentage.
    # In '55–75%' the % follows only the last number, so matching r'(\d+)\s*%'
    # would return (75,) and silently lose the lower bound.
    if "%" in value:
        found = [int(m) for m in re.findall(r"\d+", value)]
        if found:
            return tuple(sorted(found))
    # '0.2' in a detail CSV means 20%.
    match = re.fullmatch(r"0[.,](\d+)", value.strip())
    if match:
        return (int(round(float("0." + match.group(1)) * 100)),)
    # A range with no percent sign: '30-50'.
    bare = [int(m) for m in re.findall(r"\b(\d{1,3})\b", value)]
    return tuple(sorted(bare)) if bare else None


# The lower bound of a measure's grant is never its own column — it only exists
# inside the free-text 'Välistavad tingimused' sentence ("taotletav toetus alla
# 250 000 € või üle 2 000 000 €"). Requiring 'alla' to follow the word 'toetus'
# is what keeps this off the other 'alla' clauses in the same field, such as
# "abikõlblikud kulud jäävad alla 100 mln €" or "hinnapakkumuste arvu: alla
# 20 000 euro üks".
MIN_GRANT_RE = re.compile(
    r"toetus(?:e\s+summa|summa)?\s+alla\s+([\d\s\u00a0]+?)\s*(?:€|eur)",
    re.IGNORECASE,
)


def parse_min_grant(measure):
    """The measure's minimum grant in euros, or None when it states no floor.

    Looks at the master row's exclusions first, then the detail CSV rows — a few
    measures (e.g. 'Üliõpilaste inseneri valdkonna arendusprojektid') carry the
    floor only in their detail file.
    """
    texts = [measure.get("exclusions", "")]
    for d in measure.get("detail") or []:
        texts.append(d.get("vaartus", ""))
        texts.append(d.get("pohjendus", ""))
    for text in texts:
        match = MIN_GRANT_RE.search(text or "")
        if match:
            amount = parse_amount(match.group(1))
            if amount:
                return amount
    return None


def _format_eur(n):
    """42500 -> '42 500'. Estonian thousands separator is a space, not a comma."""
    return f"{n:,}".replace(",", " ")


def requested_grant_eur(intake):
    """The grant amount the applicant asked for, in euros, or None if unset."""
    return parse_amount((intake or {}).get("requested_grant", "") or "")


def grant_range_note(measure, intake):
    """Compare the requested grant against the measure's own range.

    Returns (penalty, level, text) where level is None, "warn" or "info". The
    severity is decided here rather than in the frontend, because the two bounds
    are not the same kind of problem:

    - Below min_grant: the applicant cannot apply for that amount at all. A real
      obstacle -> "warn", the larger penalty.
    - Above max_grant: the measure simply pays less than they hoped. They can
      still apply -> "info", a small penalty. The card stays a genuine
      recommendation, just ranked below the ones that can fund the full amount.

    Neither case removes the measure. An unstated amount, or a measure with no
    numeric bound on that side, produces nothing at all.
    """
    requested = requested_grant_eur(intake)
    if requested is None:
        return 0, None, None

    low = measure.get("min_grant_eur")
    high = measure.get("max_grant_eur")

    if low is not None and requested < low:
        return -PENALTY_BELOW_MIN_GRANT, "warn", (
            f"Taotletav toetus {_format_eur(requested)} € jääb alla meetme "
            f"alammäära {_format_eur(low)} €."
        )
    if high is not None and requested > high:
        return -PENALTY_ABOVE_MAX_GRANT, "info", (
            f"Meede pakub kuni {_format_eur(high)} €, soovisite "
            f"{_format_eur(requested)} €."
        )
    return 0, None, None


def grant_context_line(measure, intake):
    """The already-computed comparison, as one prompt line. '' when there is nothing to say.

    The model gets this so it never contradicts the card, and is told not to
    repeat it: a previous version asked it to mention the result, and explanations
    came back reading "summade kontroll näitas «MAHUB» (projektist saaks kuni
    20 655 eurot)" instead of saying anything about the project.
    """
    low = measure.get("min_grant_eur")
    high = measure.get("max_grant_eur")
    requested = requested_grant_eur(intake)
    if requested is None or (low is None and high is None):
        return ""

    if low is not None and high is not None:
        bounds = f"meetme toetus {_format_eur(low)}–{_format_eur(high)} €"
    elif high is not None:
        bounds = f"meetme suurim toetus {_format_eur(high)} €"
    else:
        bounds = f"meetme alammäär {_format_eur(low)} €"

    _, level, _ = grant_range_note(measure, intake)
    verdict = {"warn": "EI MAHU", "info": "PAKUB VÄHEM"}.get(level, "MAHUB")
    return (
        "Summade kontroll (arvutatud koodis, ainult sinu teadmiseks, ära kirjuta "
        f"sellest selgituses): {bounds}, taotletav {_format_eur(requested)} €. "
        f"VERDIKT: {verdict}."
    )


def load_measures():
    """Load the master table and merge in per-measure detail files.

    Returns a list of measure dicts. Each dict has the master-table fields (used
    for filtering and card display) plus a 'detail' list of the confidence-rated
    rows from the matching detail file (used as enriched context for the LLM).
    """
    master_path = os.path.join(DATA_DIR, MASTER_FILE)
    rows = list(csv.reader(open(master_path, encoding="utf-8")))

    measures = []
    for row in rows[1:]:
        name = _cell(row, COL_NAME)
        if not name:  # skip the hundreds of trailing empty rows
            continue
        links = [_cell(row, i) for i in range(COL_LINKS_START, len(row)) if _cell(row, i)]
        measures.append({
            "name": name,
            "funder": _cell(row, COL_FUNDER),
            "max_grant": _cell(row, COL_MAX_GRANT),
            "max_grant_eur": parse_amount(_cell(row, COL_MAX_GRANT)),
            "cofinancing": _cell(row, COL_COFINANCING),
            "min_grant_eur": None,  # needs the detail rows; filled in below
            "total_budget": _cell(row, COL_TOTAL_BUDGET),
            "status": _cell(row, COL_STATUS),
            "description": _cell(row, COL_DESCRIPTION),
            "link": _cell(row, COL_LINK),
            "applicant": _cell(row, COL_APPLICANT),
            "size": _cell(row, COL_SIZE),
            "region": _cell(row, COL_REGION),
            "sector": _cell(row, COL_SECTOR),
            "field": _cell(row, COL_FIELD),
            "project_type": _cell(row, COL_PROJECT_TYPE),
            "ai": _cell(row, COL_AI),
            "rnd": _cell(row, COL_RND),
            "phase": _cell(row, COL_PHASE),
            "partner": _cell(row, COL_PARTNER),
            "university": _cell(row, COL_UNIVERSITY),
            "exclusions": _cell(row, COL_EXCLUSIONS),
            "checkpoints": _cell(row, COL_CHECKPOINTS),
            "links": links,
            "detail": [],
            "measure_id": None,
            "detail_file": None,
        })

    _merge_detail_files(measures)
    # After the merge, because a few measures state their grant floor only in
    # their detail CSV rather than in the master row's exclusions.
    for m in measures:
        m["min_grant_eur"] = parse_min_grant(m)
    return measures


def _measure_id_from_filename(fname):
    """Derive a stable measure_id from a detail-CSV filename.

    Filenames are fixed once created, so the ID they produce is stable across
    runs and safe to store in threads/recommendation snapshots even though
    display names (matched fuzzily) could shift.

    Numbered files ('02 - Innovatsiooniosak.csv') use the leading number
    ('02'). Unnumbered files (ad-hoc call CSVs) fall back to a normalized
    version of the filename itself.
    """
    stem = os.path.splitext(fname)[0]
    numbered = re.match(r"^(\d+)\s*-", stem)
    if numbered:
        return numbered.group(1)
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")


def _merge_detail_files(measures):
    """Attach each detail file's rows (and derived measure_id) via fuzzy name matching."""
    by_norm = {_norm(m["name"]): m for m in measures}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".csv") or fname == MASTER_FILE:
            continue
        path = os.path.join(DATA_DIR, fname)
        rows = list(csv.reader(open(path, encoding="utf-8")))

        detail_name = next((_cell(r, 1) for r in rows if _cell(r, 0) == "Meetme nimetus"), "")
        if not detail_name:
            continue
        hit = difflib.get_close_matches(_norm(detail_name), list(by_norm), n=1, cutoff=0.6)
        if not hit:
            log.warning("no master match for detail file %r (%r)", fname, detail_name)
            continue

        measure = by_norm[hit[0]]
        measure["measure_id"] = _measure_id_from_filename(fname)
        measure["detail_file"] = fname
        for r in rows[1:]:  # skip the 'Väli, Soovitatud väärtus, ...' header
            vali = _cell(r, 0)
            if not vali or vali == "Meetme nimetus":
                continue
            measure["detail"].append({
                "vali": vali,
                "vaartus": _cell(r, 1),
                "kindlus": _cell(r, 2),
                "pohjendus": _cell(r, 3),
            })


def _applicant_categories(measure):
    """Return which taotleja types a measure accepts, from its 'Sobiv taotleja' text."""
    t = measure["applicant"].lower()
    cats = set()
    if "konsortsium" in t:
        cats.add("konsortsium")
    if "mtü" in t:
        cats.add("mtu")
    if re.search(r"(?:^|[,\s])sa(?:$|[,\s])", t):
        cats.add("sa")
    if "vke" in t or "ettevõte" in t or "ettevõtja" in t:
        cats.add("ettevote")
    return cats


def filter_candidates(measures, intake):
    """Deterministic hard-filter. Returns (candidates, dropped).

    'dropped' is a list of {'name', 'reason'} so the advisor can see why each
    measure was removed. Filters are deliberately conservative — anything
    ambiguous is kept and left to the LLM.
    """
    applicant_type = intake.get("applicant_type", "")
    company_size = intake.get("company_size", "")
    region = intake.get("region", "").lower()
    previously_applied = set(intake.get("previously_applied") or [])

    # UI shows a static sector list; user answers yes/no whether their activity
    # falls in excluded domains. No per-sector keyword matching.
    if intake.get("excluded_sector_activity", "").lower() == "jah":
        return [], [{"name": "—", "reason": "ettevõtte tegevusala on välistatud valdkondades"}]

    candidates = []
    dropped = []

    for m in measures:
        reason = _drop_reason(
            m, applicant_type, company_size, region, previously_applied
        )
        if reason:
            dropped.append({"name": m["name"], "reason": reason})
        else:
            candidates.append(m)

    for d in dropped:
        log.debug("filter dropped %r: %s", d["name"], d["reason"])
    return candidates, dropped


def _drop_reason(m, applicant_type, company_size, region, previously_applied):
    """Return a reason string if the measure should be dropped, else None."""

    if m["name"] in previously_applied:
        return "varem taotletud"

    # Status: drop closed measures; keep 'avatud' and 'tulemas' (tulemas is
    # flagged as a warning in the card, not dropped).
    status = m["status"].lower()
    if status.startswith("suletud") or "suletud" in status.split():
        return "meede on suletud"

    # Applicant type — drop only when the measure explicitly lists allowed types
    # and the user's type isn't among them.
    allowed = _applicant_categories(m)
    if allowed and applicant_type not in allowed:
        return f"sobib taotlejale: {m['applicant']}"

    # Company size: a large company can't use VKE-only measures.
    if company_size == "suur" and m["size"].lower() == "vke":
        return "meede on ainult VKE-dele"

    # Region: handle the 'Eesti, v.a Harju maakond ja Tartu linn' style exclusion.
    region_text = m["region"].lower()
    if "v.a" in region_text and region:
        excluded_part = region_text.split("v.a", 1)[1]
        region_head = region.split()[0]  # 'harju maakond' -> 'harju'
        if region_head and region_head in excluded_part:
            return f"piirkond välistatud: {m['region']}"

    return None


def measure_key(m):
    """Stable key joining a measure to its scraped source layer.

    Prefers measure_id (derived from the detail-CSV filename, so it survives
    display-name edits). Two of the 17 measures have no detail file and so no
    measure_id — those fall back to the normalized name. Both the scraper and
    backend/sources.py call this, so the key is defined in exactly one place.
    """
    return m["measure_id"] or _norm(m["name"])


def get_measure_by_id(measures, measure_id):
    """Look up a loaded measure dict by its stable measure_id, or None."""
    for m in measures:
        if m["measure_id"] == measure_id:
            return m
    return None


PHASE_OPTIONS = ["kontseptsioon", "prototüüp", "piloot", "turuleviimine"]


def get_clarifying_questions(candidates):
    """Deterministic clarifying questions derived from the surviving candidates.

    A question is only shown when at least one candidate's relevant field has a
    trigger value, so the answer could actually change the ranking. No LLM call.

    Each question is a dict: {"text": ..., "options": [...] or None}. `options`
    is None for a plain Jah/Ei question, or a list of choice labels for a
    single-select question (currently only the development-phase question).
    """
    questions = []
    for field, triggers, question in QUESTION_RULES:
        if any(c[field].strip().lower() in triggers for c in candidates):
            questions.append({"text": question, "options": None})

    # Development phase: ask only if candidates span several distinct phases.
    phases = {c["phase"].strip() for c in candidates if c["phase"].strip()}
    if len(phases) > 2:
        questions.append({
            "text": "Mis arengufaasis on projekt?",
            "options": PHASE_OPTIONS,
        })

    return questions
