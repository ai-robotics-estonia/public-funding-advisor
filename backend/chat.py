"""Claude-backed chat for one funding measure, scoped to one project (vestlus).

Each thread is created via POST /vestlused/{id}/threads and stays tied to one
measure_id within one vestlus. The model only ever sees: the focus measure's
detail CSV, the company's uploaded files, and the other ranked cards'
name/score/explanation/checks for comparison — never the full measure catalog.
"""


from backend import files, sources
from backend.llm import complete_chat


# Chat history sent to Claude (and returned to the frontend) is capped to the
# most recent N messages.
MAX_HISTORY_MESSAGES = 20

SYSTEM_PROMPT = """Sa oled Eesti innovatsioonirahastuse nõustaja abiline, kes vestleb \
kasutajaga ÜHE konkreetse rahastusmeetme kohta.

Reeglid:
- Põhjenda meetme sobivust ettevõtte failide ja projekti konteksti valguses.
- Võrdlusküsimusi teiste meetmetega tohib vastata ainult allpool antud teiste \
soovituskaartide (nimi, hinne, selgitus, kontrollkohad) põhjal — sul pole nende \
täielikku CSV-d.
- Kui allpool on jaotis "Allikatekstid", sisaldab see meetme KEHTIVAT õigusakti \
tervikuna ja meetme ametlikku lehte. Õiguslikele küsimustele (abikõlblikkus, \
välistused, taotleja nõuded, tähtajad, toetuse määr) vasta nende põhjal ja \
VIITA ALATI paragrahvile ja lõikele, nt "(§ 5 lg 2)".
- Kui allikatekst on vastuolus ülal olevate struktureeritud andmetega, ütle see \
selgelt välja, eelista allikateksti ja märgi erinevus ära.
- Kui info puudub, ütle seda ausalt; ära leiuta fakte.
- Vasta eesti keeles, tavalises jutustavas vormis (mitte JSON)."""


def find_focus_card(recommendation, measure_id):
    """Find the ranked card matching measure_id in a locked recommendation, or None."""
    for card in recommendation["ranked_cards"]:
        if card.get("measure_id") == measure_id:
            return card
    return None


def find_refine_card(refinement, measure_id):
    """Find the refine card matching measure_id in the latest refinement, or None."""
    if not refinement:
        return None
    for card in refinement.get("cards") or []:
        if card.get("measure_id") == measure_id:
            return card
    return None


def _refine_lines(refine_card):
    """The refine card's proposed changes as plain text lines."""
    lines = [
        f"Sobivus praegu {refine_card.get('current_score')}/5, "
        f"pärast muudatusi {refine_card.get('projected_score')}/5.",
        refine_card.get("summary", ""),
    ]
    changes = refine_card.get("changes") or []
    if changes:
        lines.append("")
        lines.append("Soovitatud muudatused:")
        for c in changes:
            where = f" (kus: {c['where']})" if c.get("where") else ""
            lines.append(f"- [{c.get('effort', '')}] {c.get('text', '')}{where}")
    return [line for line in lines if line != ""] or [""]


def seed_text(focus_card, refine_card=None):
    """The thread's opening assistant message.

    Normally the focus card's own pitch. A measure the user only reached through
    the second pass has no ranked card, so its proposed changes open the thread
    instead — otherwise that thread would start blank.
    """
    if not focus_card:
        if refine_card:
            return "\n".join(_refine_lines(refine_card)).strip()
        return ""
    lines = [focus_card.get("explanation", "")]
    checks = focus_card.get("checks") or []
    if checks:
        lines.append("")
        lines.append("Kontrollkohad enne taotlemist:")
        lines.extend(f"- {c}" for c in checks)
    recommendation = focus_card.get("recommendation") or []
    if recommendation:
        lines.append("")
        lines.append("Soovitus:")
        lines.extend(f"- {r}" for r in recommendation)
    return "\n".join(lines).strip()


def _detail_block(measure):
    """Render a measure's confidence-rated detail rows as plain text."""
    if not measure["detail"]:
        return ""
    lines = [f"Meede: {measure['name']}"]
    for d in measure["detail"]:
        if d["vaartus"] or d["pohjendus"]:
            lines.append(f"  - {d['vali']} | {d['vaartus']} | {d['kindlus']} | {d['pohjendus']}")
    return "\n".join(lines)


def _comparison_block(recommendation, exclude_measure_id):
    """Other ranked cards' name/score/explanation/checks, for comparison questions."""
    others = [c for c in recommendation["ranked_cards"] if c.get("measure_id") != exclude_measure_id]
    if not others:
        return ""
    lines = ["## Teised soovitatud meetmed (võrdluseks)"]
    for c in others:
        lines.append(f"- {c['name']} (hinne {c.get('score')}): {c.get('explanation', '')}")
        for check in c.get("checks") or []:
            lines.append(f"    kontroll: {check}")
    return "\n".join(lines)


def build_system_context(vestluse_id, measure, recommendation, focus_card, refine_card=None):
    """Assemble this thread's system prompt: focus measure + files + comparisons."""
    parts = [SYSTEM_PROMPT, "", "## Fookusmeede (täpsustatud andmed)", _detail_block(measure)]

    if focus_card:
        parts.append("")
        parts.append("## Sinu varasem hinnang sellele meetmele")
        parts.append(f"Hinne: {focus_card.get('score')}")
        parts.append(f"Selgitus: {focus_card.get('explanation', '')}")

    if refine_card:
        parts.append("")
        parts.append("## Täiendava analüüsi ettepanekud sellele meetmele")
        parts.extend(_refine_lines(refine_card))
        parts.append(
            "Kui kasutaja küsib, kuidas mõnda muudatust taotluses sõnastada, aita tal "
            "see kirja panna. Ära paku muudatusi ettevõtte tüübi, suuruse, piirkonna, "
            "tegevusala, eelarve ega omafinantseeringu kohta — need on lukus."
        )

    files_context = files.build_context_text(vestluse_id)
    if files_context:
        parts.append("")
        parts.append("## Ettevõtte failide sisu")
        parts.append(files_context)

    comparison = _comparison_block(recommendation, measure["measure_id"])
    if comparison:
        parts.append("")
        parts.append(comparison)

    # The focus measure's full legal act + measure page. This is the whole point
    # of the scraper: legal detail a human would have to dig for. Placed last so
    # the long text does not push the instructions out of the model's attention.
    # Empty string when the scraper has never run.
    source_texts = sources.chat_sources(measure)
    if source_texts:
        parts.append("")
        parts.append(source_texts)

    return "\n".join(parts)


def _to_api_messages(messages):
    """Collapse stored messages into a Claude-valid alternating sequence.

    Drops any leading assistant/system content (our seed message, which Claude
    never actually said) so the sequence starts with 'user', and collapses
    consecutive same-role runs — e.g. a dangling user turn left behind by an
    earlier failed reply — by keeping only the latest of each run.
    """
    result = []
    for m in messages:
        role = m["role"]
        if role not in ("user", "assistant"):
            continue
        if not result and role != "user":
            continue
        if result and result[-1]["role"] == role:
            result[-1] = {"role": role, "content": m["content"]}
        else:
            result.append({"role": role, "content": m["content"]})
    return result


def send_message(vestluse_id, measure, recommendation, history, user_content, refine_card=None):
    """Run one Claude call scoped to this thread and return the assistant's reply text."""
    focus_card = find_focus_card(recommendation, measure["measure_id"])
    system = build_system_context(
        vestluse_id, measure, recommendation, focus_card, refine_card
    )
    api_messages = _to_api_messages(list(history) + [{"role": "user", "content": user_content}])

    return complete_chat(system=system, messages=api_messages, max_tokens=1500, purpose="chat")
