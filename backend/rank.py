"""Single Claude call that scores the pre-filtered candidate measures.

The hard-filter (measures.py) has already removed ineligible measures. Here we
send all survivors to Claude in one call and ask it to judge each one's semantic
fit (enum fields, not a score) plus write a cited explanation in Estonian. The
1-5 score, sorting, and top-10 cut are computed in code from those enums and the
soft-filter penalties/tips (measures.soft_filter_notes) — never by the model.
Display fields (grant, co-financing, status, links) are taken from our own data,
so they can never be hallucinated by the model.
"""

import json
import re

from backend import measures, sources
from backend.llm import complete_chat


SYSTEM_PROMPT = """Sa oled Eesti innovatsioonirahastuse nõustaja abiline. Sulle antakse \
projekti kirjeldus ja nimekiri juba eelfiltreeritud rahastusmeetmetest koos nende \
struktureeritud andmetega. Sinu ülesanne on hinnata IGA meetme sobivust projekti jaoks.

Iga meetme kohta anna kolm sobivushinnangut, väärtusega "yes", "partial", "no" või \
"unknown":
- project_type_fit: kas meetme lubatud projekti tüüp klapib projekti kirjeldusega.
- field_fit: kas meetme sektor/valdkond klapib projekti valdkonna/tegevusalaga.
- purpose_fit: kas meetme üldine eesmärk ja kirjeldus klapib projekti eesmärgiga.

Reeglid:
- Hinda KÕIKI antud meetmeid, mitte ainult parimaid — järjestuse ja valiku teeb meid \
esindav kood, mitte sina.
- Selgita sobivust ühe lõiguga eesti keeles, viidates projekti sisule ja meetme tingimustele.
- Kui on lisatud ettevõtte failide sisu, kasuta seda konteksti sobivuse hindamisel ja \
mainimisel selgituses.
- Loetle 2-4 kõige olulisemat kontrollkohta, mille nõustaja peab enne taotlemist \
käsitsi üle kontrollima. Võta need otse meetme väljadest "Välistavad tingimused" ja \
"Kontrollkohad".
- Kui meetmel on plokk "Ajakohane allikainfo" VASTUOLU-märkega, tähendab see, et \
meetme ametlik allikas ei ühti meie andmetega. Arvesta seda sobivuse hindamisel, \
maini seda selgituses ja lisa vastav kontrollkoht.
- Meetme real "Summade kontroll" on toetussumma võrdlus JUBA KOODIS ära tehtud. Arvesta \
seda hinnangut andes, aga ära arvuta summasid ise ega vaidle verdiktiga.
- See rida on AINULT sinu teadmiseks. ÄRA kirjuta sellest selgituses ega kontrollkohtades — \
ei sõnu "summade kontroll", "VERDIKT", "MAHUB" ega arve sellelt realt. Kasutaja näeb \
summade mittevastavust eraldi märkusena. Selgitus räägib ainult projekti sisulisest \
sobivusest meetme tegevuste, valdkonna ja eesmärgiga.
- Kui verdikt on "EI MAHU", ära väida vastupidist ega kiida meedet sobivaks summa poolest.
- Verdikt "PAKUB VÄHEM" tähendab, et meede on sobiv, aga annab taotletust väiksema summa. \
See EI ole mittevastavus — hinda meedet tavaliselt selle sisulise sobivuse järgi.
- Ära leiuta fakte ega linke. Kasuta ainult antud andmeid.
- JSON peab olema kehtiv: ära pane explanation ega checks tekstidesse ASCII jutumärke \
("); kasuta vajadusel ülakomasid või «».

Vasta AINULT kehtiva JSON-massiiviga, ilma lisatekstita, üks element iga antud meetme \
kohta:
{"name": "<meetme täpne nimi>", "project_type_fit": "yes|partial|no|unknown", \
"field_fit": "yes|partial|no|unknown", "purpose_fit": "yes|partial|no|unknown", \
"explanation": "<eestikeelne selgitus>", "checks": ["<kontrollkoht 1>", "<kontrollkoht 2>"]}"""

FIT_FIELDS = ("project_type_fit", "field_fit", "purpose_fit")

# Points per enum value, per fit field. Three fields -> 0-45 raw semantic points.
ENUM_POINTS = {"yes": 15, "partial": 8, "unknown": 5, "no": 0}

# (min total, display score), checked high to low. Never hides a card: the floor
# bucket is always 1, not 0/dropped.
SCORE_THRESHOLDS = [(38, 5), (30, 4), (20, 3), (10, 2)]


def _display_score(total):
    """Map a semantic+penalty total to the 1-5 display score. Floors at 1."""
    total = max(total, 0)
    for minimum, score in SCORE_THRESHOLDS:
        if total >= minimum:
            return score
    return 1


def _candidate_block(m, intake):
    """Render one measure as a compact text block for the prompt."""
    lines = [
        f"Meede: {m['name']}",
        f"Rahastaja: {m['funder']}",
        f"Suurim toetus: {m['max_grant']}",
        f"Omafinantseering: {m['cofinancing']}",
        f"Staatus: {m['status']}",
        f"Kirjeldus: {m['description']}",
        f"Sobiv taotleja: {m['applicant']} | Suurus: {m['size']} | Piirkond: {m['region']}",
        f"Sektor: {m['sector']} | Valdkond: {m['field']}",
        f"Projekti tüüp: {m['project_type']}",
        f"AI nõue: {m['ai']} | T&A nõue: {m['rnd']} | Arengufaas: {m['phase']}",
        f"Partneri nõue: {m['partner']} | Ülikoolipartner: {m['university']}",
        f"Välistavad tingimused: {m['exclusions']}",
        f"Kontrollkohad: {m['checkpoints']}",
    ]
    # Amount arithmetic, done in code. Without it the model was handed the
    # applicant's euro figure, the measure's co-financing percentage and the
    # measure's euro ceiling as three raw strings and left to compare them.
    grant_line = measures.grant_context_line(m, intake)
    if grant_line:
        lines.append(grant_line)
    # Enriched, confidence-rated detail (with EIS / Riigi Teataja citations).
    if m["detail"]:
        lines.append("Täpsustatud andmed (väli | väärtus | kindlus | põhjendus):")
        for d in m["detail"]:
            if d["vaartus"] or d["pohjendus"]:
                lines.append(f"  - {d['vali']} | {d['vaartus']} | {d['kindlus']} | {d['pohjendus']}")

    # Live source layer: only freshness and CSV-vs-source conflicts, never the
    # full scraped text — this prompt carries every candidate at once. Empty
    # string when the scraper has never run.
    source_note = sources.rank_block(m)
    if source_note:
        lines.append(source_note)
    return "\n".join(lines)


def build_user_message(candidates, intake, clarifications, files_context=""):
    """Assemble the full user message: intake + company files + candidate blocks."""
    parts = ["## Projekti taotleja sisend", ""]
    parts.append(f"Taotleja tüüp: {intake.get('applicant_type', '')}")
    parts.append(f"Ettevõtte suurus: {intake.get('company_size', '')}")
    parts.append(f"Piirkond: {intake.get('region', '')}")
    parts.append(f"Taotletav toetuse summa (eurodes): {intake.get('requested_grant', '')}")
    parts.append(f"Projekti kirjeldus: {intake.get('project_description', '')}")

    if clarifications:
        parts.append("")
        parts.append("## Täpsustavad vastused")
        for q, a in clarifications.items():
            parts.append(f"- {q} -> {a}")

    if files_context:
        parts.append("")
        parts.append("## Ettevõtte failide sisu")
        parts.append(files_context)

    parts.append("")
    parts.append("## Eelfiltreeritud meetmed")
    for m in candidates:
        parts.append("")
        parts.append(_candidate_block(m, intake))

    parts.append("")
    parts.append("Hinda kõiki ülaltoodud meetmeid projekti jaoks ja vasta ainult JSON-massiiviga.")
    return "\n".join(parts)


def _escape_inner_quotes(text):
    """Escape raw " characters that appear inside JSON string values.

    Claude often wraps Estonian phrases in ASCII quotes inside explanation text,
    which breaks json.loads. A closing quote is assumed when the next non-space
    char is ',', '}', ']', or ':' (the last covers object keys).
    """
    out = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and in_string and i + 1 < n:
            out.append(c)
            out.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
                out.append(c)
            else:
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                if j >= n or text[j] in ",}]:":
                    in_string = False
                    out.append(c)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _salvage_objects(text):
    """Parse complete top-level objects from a JSON array, stopping at first bad one."""
    start = text.find("[")
    if start == -1:
        return []
    decoder = json.JSONDecoder()
    items = []
    pos = start + 1
    length = len(text)
    while pos < length:
        while pos < length and (text[pos].isspace() or text[pos] == ","):
            pos += 1
        if pos >= length or text[pos] == "]":
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        items.append(obj)
        pos = end
    return items


def _parse_json_array(text):
    """Parse Claude's reply into a list. Tolerates ```json fences, stray text,
    unescaped quotes inside string values, and a response cut off mid-object.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if text.find("[") == -1:
        raise ValueError("model response contained no JSON array")

    # Prefer salvaging complete objects from the raw text (handles max_tokens cutoff).
    items = _salvage_objects(text)
    if items:
        return items

    # First object already broken — usually raw " inside explanation text.
    repaired = _escape_inner_quotes(text)
    if repaired != text:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            items = _salvage_objects(repaired)
            if items:
                return items

    raise ValueError("could not parse any candidate objects from model response")


def rank(candidates, intake, clarifications, files_context=""):
    """Run the single Claude scoring call and return a list of card dicts.

    Claude returns per-candidate enum fit judgements (not a score); the score is
    computed in code from those enums plus soft-filter penalties, cards are sorted
    by that total, and the top 10 are kept. Each card merges the computed score,
    the model's explanation/checks, and our own verbatim display fields (grant,
    co-financing, status, links) with a code-generated recommendation tip list.
    """
    if not candidates:
        return []

    text = complete_chat(
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_user_message(candidates, intake, clarifications, files_context),
        }],
        max_tokens=8192,
        purpose="rank",
    )
    evaluated = _parse_json_array(text)

    by_norm = {measures._norm(m["name"]): m for m in candidates}
    cards = []
    for item in evaluated:
        m = by_norm.get(measures._norm(item.get("name", "")))
        if not m:  # model returned a name we don't recognize; skip it
            continue

        semantic_points = sum(
            ENUM_POINTS.get(item.get(f), ENUM_POINTS["unknown"]) for f in FIT_FIELDS
        )
        penalty, tips = measures.soft_filter_notes(m, clarifications)
        grant_penalty, grant_level, grant_text = measures.grant_range_note(m, intake)
        total = semantic_points + penalty + grant_penalty

        warning = "tulemas" in m["status"].lower()
        cards.append({
            "name": m["name"],
            "measure_id": m["measure_id"],
            "funder": m["funder"],
            "score": _display_score(total),
            "explanation": item.get("explanation", ""),
            "checks": item.get("checks", []),
            "recommendation": tips,
            "grant_warning": grant_text if grant_level == "warn" else None,
            "grant_info": grant_text if grant_level == "info" else None,
            "fit": {f: item.get(f, "unknown") for f in FIT_FIELDS},
            "max_grant": m["max_grant"],
            "cofinancing": m["cofinancing"],
            "status": m["status"],
            "status_warning": warning,
            "link": m["link"],
            "links": m["links"],
            "source_status": sources.card_status(m),
            "_total": total,
        })

    cards.sort(key=lambda c: (-c["_total"], c["name"]))
    for c in cards:
        del c["_total"]
    return cards[:10]
