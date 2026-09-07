"""Second pass: what would the applicant have to change for a measure to fit?

The first report (rank.py) judges the measures as the project stands today. Here
the user hand-picks measures from that same hard-filter survivor pool and asks
the opposite question: which changes to the application would make each one fit
better. The model may only propose changing the project itself — goal, project
type, development phase, and the R&D / AI / partner / university dimensions.
The company facts the hard filter ran on (type, size, region, sector, budget,
co-financing) are locked, because they are not things an applicant can edit in a
document.

Measures go out in small batches. rank.py sends everything in one call and
salvages whatever parses, which silently drops the tail on a max_tokens cutoff.
That is tolerable when the model writes one paragraph per measure; it is not
tolerable here, where each measure needs a list of changes and the user
explicitly ticked the measures they want back.
"""

import logging

from backend import measures, rank, sources
from backend.llm import complete_chat

log = logging.getLogger(__name__)

# Measures per LLM call. Small enough that the answer for a full batch fits in
# max_tokens with room to spare, so no card is ever lost to truncation.
BATCH_SIZE = 3
MAX_TOKENS = 4096

# Effort ordering for the sort. Anything the model invents falls back to the
# middle so an unrecognized value can never look like the cheapest option.
EFFORT_RANK = {"väike": 0, "keskmine": 1, "suur": 2}
DEFAULT_EFFORT = "keskmine"

MAX_CHANGES_PER_MEASURE = 5

# The four dimensions the model may propose flipping to "Jah". These are the
# keys of measures.SOFT_FILTER_FIELDS, which is what turns them into a score.
PROPOSABLE_DIMENSIONS = tuple(measures.SOFT_FILTER_FIELDS)

SYSTEM_PROMPT = """Sa oled Eesti innovatsioonirahastuse nõustaja abiline. Sulle antakse \
projekti kirjeldus, ettevõtte taotlusdokumentide sisu ja väike hulk rahastusmeetmeid, \
mille taotleja ise välja valis.

Nende meetmete puhul EI OLE küsimus, kas projekt sobib praegusel kujul — see on juba \
hinnatud. Sinu ülesanne on öelda, MIDA taotleja peaks projektis või taotlusdokumendis \
muutma, et meede talle paremini sobiks.

LUKUSTATUD — neid EI TOHI kunagi muuta ega soovitada muuta:
- taotleja tüüp (ettevõte, MTÜ, SA, konsortsium)
- ettevõtte suurus
- piirkond ja asukoht
- ettevõtte tegevusala ja välistatud sektorid
- varem taotletud meetmed
- taotletav toetuse summa ja omafinantseeringu määr

MUUDETAV — ainult neid tohid soovitada muuta:
- projekti eesmärk, kirjeldus ja sõnastatud tulemused
- projekti tüüp ja arengufaas
- rakendusuuringu või tootearenduse (T&A) sisu ja osakaal
- tehisintellekti (AI) komponendi sisu
- partneri või konsortsiumi kaasamine
- ülikooli või teadusasutuse kaasamine

Reeglid:
- Anna iga meetme kohta 2-5 konkreetset muudatust. Iga muudatus peab olema tegelik \
tegevus, mitte üldsõnaline soovitus.
- Iga muudatuse juures ütle väljal "where" TÄPSELT, kus seda teha: kui allpool on \
jaotis "Ettevõtte failide sisu", viita faili nimele ja selle osale; kui faile ei ole, \
kirjuta "projekti kirjeldus".
- Iga muudatuse juures hinda väljal "effort" töömahtu: "väike", "keskmine" või "suur".
- Väljal "proposed_answers" loetle ainult need dimensioonid, mille sinu muudatused \
taotleja jaoks tegelikult "Jah"-iks pööravad. Lubatud väärtused: "rnd", "ai", \
"partner", "university". Kui ükski ei muutu, jäta massiiv tühjaks.
- "current_fit" kirjeldab sobivust PRAEGU, "projected_fit" sobivust PÄRAST sinu \
muudatuste elluviimist. Mõlemal kolm välja, väärtusega "yes", "partial", "no" või \
"unknown".
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
- Kui meede ei saa ka muudatustega sobida, ütle seda ausalt "summary" väljal ja jäta \
"changes" lühikeseks.
- JSON peab olema kehtiv: ära pane tekstidesse ASCII jutumärke ("); kasuta vajadusel \
ülakomasid või «».

Vasta AINULT kehtiva JSON-massiiviga, ilma lisatekstita, üks element iga antud meetme \
kohta:
{"name": "<meetme täpne nimi>", \
"current_fit": {"project_type_fit": "yes|partial|no|unknown", \
"field_fit": "yes|partial|no|unknown", "purpose_fit": "yes|partial|no|unknown"}, \
"changes": [{"text": "<muudatus>", "effort": "väike|keskmine|suur", "where": "<koht>"}], \
"projected_fit": {"project_type_fit": "yes|partial|no|unknown", \
"field_fit": "yes|partial|no|unknown", "purpose_fit": "yes|partial|no|unknown"}, \
"proposed_answers": ["rnd"], "summary": "<eestikeelne kokkuvõte>"}"""


def _prior_block(prior_card):
    """Report-1's verdict on this measure, so the model targets what was wrong."""
    if not prior_card:
        return ""
    fit = prior_card.get("fit") or {}
    fit_text = ", ".join(f"{f}={fit.get(f, 'unknown')}" for f in rank.FIT_FIELDS)
    return (
        f"Varasem hinnang: {prior_card.get('score')}/5 ({fit_text})\n"
        f"Varasem selgitus: {prior_card.get('explanation', '')}"
    )


def build_user_message(batch, intake, clarifications, files_context, prior_cards,
                       willing_to_change):
    """Assemble one batch's user message: project context + the batch's measures."""
    parts = ["## Projekti taotleja sisend (LUKUSTATUD osa)", ""]
    parts.append(f"Taotleja tüüp: {intake.get('applicant_type', '')}")
    parts.append(f"Ettevõtte suurus: {intake.get('company_size', '')}")
    parts.append(f"Piirkond: {intake.get('region', '')}")
    parts.append(f"Taotletav toetuse summa (eurodes): {intake.get('requested_grant', '')}")
    parts.append("")
    parts.append("## Projekti kirjeldus (MUUDETAV)")
    parts.append(intake.get("project_description", ""))

    if clarifications:
        parts.append("")
        parts.append("## Täpsustavad vastused")
        for q, a in clarifications.items():
            parts.append(f"- {q} -> {a}")

    if willing_to_change:
        parts.append("")
        parts.append("## Mida taotleja on valmis muutma")
        parts.append(willing_to_change)
        parts.append("Arvesta seda: ära paku muudatusi, mille taotleja on välistanud.")

    if files_context:
        parts.append("")
        parts.append("## Ettevõtte failide sisu")
        parts.append(files_context)

    parts.append("")
    parts.append("## Meetmed, mille kohta muudatusi soovitada")
    for m in batch:
        parts.append("")
        parts.append(rank._candidate_block(m, intake))
        prior = _prior_block(prior_cards.get(m["name"]))
        if prior:
            parts.append(prior)

    parts.append("")
    parts.append(
        f"Soovita muudatusi kõigi {len(batch)} ülaltoodud meetme kohta ja vasta "
        "ainult JSON-massiiviga."
    )
    return "\n".join(parts)


def _clean_changes(raw):
    """Normalize the model's changes list: drop empties, fix effort, cap the length."""
    changes = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        effort = str(item.get("effort", "")).strip().lower()
        changes.append({
            "text": text,
            "effort": effort if effort in EFFORT_RANK else DEFAULT_EFFORT,
            "where": str(item.get("where", "")).strip(),
        })
    return changes[:MAX_CHANGES_PER_MEASURE]


def _semantic_points(fit):
    """Sum the three fit enums into raw semantic points, same scale as rank.py."""
    fit = fit or {}
    return sum(
        rank.ENUM_POINTS.get(fit.get(f), rank.ENUM_POINTS["unknown"]) for f in rank.FIT_FIELDS
    )


def _projected_clarifications(clarifications, proposed):
    """Clarifications as they would read once the proposed changes are made.

    A proposal to add a partner is only visible in the score if the soft-filter
    answer it depends on flips with it — otherwise the projected score would
    still carry the penalty for the very thing the change fixes.
    """
    projected = dict(clarifications or {})
    for dimension in proposed:
        if dimension in measures.SOFT_FILTER_FIELDS:
            projected[measures.SOFT_FILTER_FIELDS[dimension][1]] = "Jah"
    return projected


def _card(m, item, clarifications, intake):
    """Merge one model verdict with our own display fields into a refine card."""
    proposed = [
        d for d in (item.get("proposed_answers") or []) if d in PROPOSABLE_DIMENSIONS
    ]
    changes = _clean_changes(item.get("changes"))

    current_penalty, _ = measures.soft_filter_notes(m, clarifications)
    projected_penalty, _ = measures.soft_filter_notes(
        m, _projected_clarifications(clarifications, proposed)
    )
    # An out-of-range amount is not something the proposed changes can fix, so it
    # weighs on the projected score exactly as much as on the current one.
    grant_penalty, grant_level, grant_text = measures.grant_range_note(m, intake)
    current_total = _semantic_points(item.get("current_fit")) + current_penalty + grant_penalty
    projected_total = _semantic_points(item.get("projected_fit")) + projected_penalty + grant_penalty

    return {
        "name": m["name"],
        "measure_id": m["measure_id"],
        "funder": m["funder"],
        "current_score": rank._display_score(current_total),
        "projected_score": rank._display_score(projected_total),
        "current_fit": {f: (item.get("current_fit") or {}).get(f, "unknown") for f in rank.FIT_FIELDS},
        "projected_fit": {f: (item.get("projected_fit") or {}).get(f, "unknown") for f in rank.FIT_FIELDS},
        "summary": item.get("summary", ""),
        "changes": changes,
        "proposed_answers": proposed,
        "grant_warning": grant_text if grant_level == "warn" else None,
        "grant_info": grant_text if grant_level == "info" else None,
        "max_grant": m["max_grant"],
        "cofinancing": m["cofinancing"],
        "status": m["status"],
        "status_warning": "tulemas" in m["status"].lower(),
        "link": m["link"],
        "links": m["links"],
        "source_status": sources.card_status(m),
        "_sort": (-projected_total, max((EFFORT_RANK[c["effort"]] for c in changes), default=0)),
    }


def _run_batch(batch, intake, clarifications, files_context, prior_cards, willing_to_change):
    """One LLM call for one batch, retried once. Returns parsed items, or [] on failure."""
    message = build_user_message(
        batch, intake, clarifications, files_context, prior_cards, willing_to_change
    )
    names = ", ".join(m["name"] for m in batch)
    for attempt in (1, 2):
        try:
            text = complete_chat(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": message}],
                max_tokens=MAX_TOKENS,
                purpose="refine",
            )
            return rank._parse_json_array(text)
        except Exception as e:
            log.warning("batch attempt %s failed for [%s]: %s", attempt, names, e)
    return []


def refine(selected, intake, clarifications, files_context="", prior_cards=None,
           willing_to_change=""):
    """Run the batched change-advice pass over the user's selected measures.

    Returns (cards, failed_names). A measure lands in failed_names when its whole
    batch errored out or when the model's answer simply did not mention it, so a
    silently missing measure is reported rather than quietly dropped.
    """
    if not selected:
        return [], []

    prior_cards = prior_cards or {}
    cards = []
    failed = []

    for start in range(0, len(selected), BATCH_SIZE):
        batch = selected[start:start + BATCH_SIZE]
        items = _run_batch(
            batch, intake, clarifications, files_context, prior_cards, willing_to_change
        )

        by_norm = {measures._norm(m["name"]): m for m in batch}
        answered = set()
        for item in items:
            m = by_norm.get(measures._norm(item.get("name", "")))
            if not m:  # a name we did not ask about
                continue
            cards.append(_card(m, item, clarifications, intake))
            answered.add(m["name"])

        failed.extend(m["name"] for m in batch if m["name"] not in answered)

    cards.sort(key=lambda c: (c["_sort"], c["name"]))
    for c in cards:
        del c["_sort"]
    return cards, failed
