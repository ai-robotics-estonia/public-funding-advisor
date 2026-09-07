"""Read-only access to the scraper's evidence layer, for use in LLM prompts.

This is the module that makes scraping worth doing: without it the scraper is a
write-only notification system and the model's knowledge stays exactly as stale
as the CSV.

Three consumers, three different budgets:
- rank.py  -> rank_block():  a few hundred chars per measure. The ranking prompt
              covers every candidate at once, so only deltas and conflicts fit.
- chat.py  -> chat_sources(): the whole legal act plus the measure page. One
              measure in focus, so we can afford ~60k chars and the advisor gets
              paragraph-level legal answers.
- main.py  -> card_status(): freshness + conflicts for the UI.

The scraper is entirely optional. If the state database does not exist, or the
scraper has never run, every function here returns an empty value and the app
behaves exactly as it did before.

That graceful degradation is also why the path below must come from
scraper.config rather than being spelled out again: a wrong path here does not
raise, it just silently looks like "the scraper never ran" and the legal-act
boxes quietly vanish from every card.
"""

import os
import sqlite3

from scraper import config

DB_PATH = str(config.STATE_DB_PATH)

# Keep the ranking prompt compact — it holds every surviving candidate at once.
RANK_BLOCK_CHAR_BUDGET = 900
# One measure in focus, so the whole legal act fits comfortably.
CHAT_SOURCE_CHAR_BUDGET = 60_000


def _connect():
    """Open the scraper DB read-only, or return None if it isn't usable.

    Read-only (mode=ro) so a web request can never block or corrupt a scraper
    write that is running concurrently in the background thread.
    """
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _query(sql, params):
    """Run a query, tolerating a missing/older schema (returns [])."""
    conn = _connect()
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _key(measure):
    # Imported lazily to avoid a circular import at module load time.
    from backend.measures import measure_key
    return measure_key(measure)


def _pages(measure):
    return _query(
        "SELECT * FROM page_state WHERE measure_key = ? ORDER BY source_type, url",
        (_key(measure),),
    )


def conflicts(measure):
    return _query(
        "SELECT * FROM source_conflicts WHERE measure_key = ? ORDER BY severity, field",
        (_key(measure),),
    )


def facts(measure):
    return _query(
        "SELECT * FROM measure_facts WHERE measure_key = ? ORDER BY field",
        (_key(measure),),
    )


def _legal_act(pages):
    """Kehtiva õigusakti tunnused kaardi neutraalse info-rea jaoks.

    See ei ole hoiatus. Skreeper loeb alati kehtivat redaktsiooni, seega ainus
    asi, mida kasutajale öelda on, MILLIST redaktsiooni ta praegu vaatab.
    Lingitud (vana) redaktsiooni siit tahtlikult ei tule — see on andmebaasis
    tõendina alles, aga UI-s ajas see kasutaja segadusse: kaart nimetas vana
    ID-d ja link viis vanale tekstile, mistõttu tundus, nagu töötaks rakendus
    aegunud andmetega.

    Tagastab None, kui meetmel ei ole õigusakti allikat.
    """
    for page in pages:
        if page.get("source_type") != "legal":
            continue
        return {
            "title": page.get("act_title") or "",
            "kehtiv_id": page.get("kehtiv_id") or "",
            "valid_from": page.get("valid_from") or "",
            "url": page.get("resolved_url") or page.get("url") or "",
        }
    return None


def _act_line(act):
    """Üherealine kirjeldus promptide jaoks: 'pealkiri (redaktsioon X, jõust. Y)'."""
    if not act:
        return ""
    detail = ", ".join(filter(None, [
        f"redaktsioon {act['kehtiv_id']}" if act["kehtiv_id"] else "",
        f"jõust. {act['valid_from']}" if act["valid_from"] else "",
    ]))
    title = act["title"] or act["url"]
    return f"{title} ({detail})" if detail else title


def _checked_at(pages):
    """Most recent check across the measure's sources, as a plain YYYY-MM-DD."""
    stamps = [p["last_checked_at"] for p in pages if p.get("last_checked_at")]
    return max(stamps)[:10] if stamps else None


def rank_block(measure):
    """Compact source note for one candidate in the ranking prompt.

    Deliberately carries only what the CSV cannot already say: how fresh the
    check is, and where the source disagrees with the CSV. Returns "" when the
    scraper has nothing on this measure.
    """
    pages = _pages(measure)
    if not pages:
        return ""

    lines = []
    checked = _checked_at(pages)
    if checked:
        lines.append(f"Ajakohane allikainfo (kontrollitud {checked}):")

    act = _legal_act(pages)
    if act:
        # Kõigepealt see, MIDA me lugesime — muidu jääb allpool olevast
        # vastuolust mulje, nagu oleks meie andmete aluseks midagi aegunut.
        lines.append(f"  Loetud kehtivast õigusaktist: {_act_line(act)}")

    for conflict in conflicts(measure):
        field = conflict["field"]
        if field == "õigusakt":
            # Puudutab CSV lingi kvaliteeti, mitte meetme sisu — sõnastus peab
            # seda selgelt ütlema, et mudel ei alandaks meetme skoori.
            lines.append(
                f"  ⚠ CSV LINGI VIGA (ei puuduta meetme sisu): "
                f"{conflict.get('citation') or ''}".rstrip()
            )
        else:
            lines.append(
                f"  ⚠ VASTUOLU {field}: CSV=\"{conflict.get('csv_value') or ''}\", "
                f"allikas=\"{conflict.get('source_value') or ''}\""
            )

    has_legal = act is not None
    if has_legal and len(lines) <= 2:
        lines.append("  Vastuolusid ei leitud.")

    if not has_legal and len(lines) <= 1:
        return ""

    block = "\n".join(lines)
    if len(block) > RANK_BLOCK_CHAR_BUDGET:
        block = block[:RANK_BLOCK_CHAR_BUDGET].rstrip() + "\n  [... vastuolude nimekiri kärbitud ...]"
    return block


def chat_sources(measure):
    """Full source texts for the focus measure's chat thread.

    The legal act goes first and is never truncated — it is the authoritative
    text and the reason this layer exists. The measure page is truncated if the
    budget runs out, with a visible marker so the model knows text is missing.
    """
    pages = [p for p in _pages(measure) if (p.get("cleaned_text") or "").strip()]
    if not pages:
        return ""

    ordered = sorted(pages, key=lambda p: 0 if p["source_type"] == "legal" else 1)
    checked = _checked_at(pages)

    parts = [f"## Allikatekstid (automaatselt tõmmatud, kontrollitud {checked or '—'})"]
    budget = CHAT_SOURCE_CHAR_BUDGET

    for page in ordered:
        if budget <= 0:
            break
        text = page["cleaned_text"]
        if page["source_type"] == "legal":
            # Vana redaktsiooni (`linked_text`, `valid_until`) siia ei tule:
            # see kahekordistaks õigusakti mahu ja mudel peab vastama KEHTIVA
            # teksti järgi. Redaktsiooni number on kirjas, et nõustaja saaks
            # vajadusel öelda, millisele tekstile ta tugineb.
            act = _legal_act([page])
            header = f"### Kehtiv õigusakt: {_act_line(act)}\n{act['url']}"
        else:
            header = f"### Meetme leht: {page['url']}"

        chunk = text[:budget]
        budget -= len(chunk)
        if len(chunk) < len(text):
            chunk += "\n[... tekst kärbitud pikkuse tõttu ...]"
        parts.append("")
        parts.append(header)
        parts.append(chunk)

    return "\n".join(parts)


def card_status(measure):
    """Freshness and conflict summary for a recommendation card in the UI."""
    pages = _pages(measure)
    if not pages:
        return None

    items = conflicts(measure)
    act = _legal_act(pages)
    return {
        "checked_at": _checked_at(pages),
        "has_legal_source": act is not None,
        "legal_act": act,
        "superseded": any(p.get("superseded") for p in pages),
        "conflicts": [
            {
                "field": c["field"],
                "csv_value": c.get("csv_value") or "",
                "source_value": c.get("source_value") or "",
                "severity": c.get("severity") or "keskmine",
                "citation": c.get("citation") or "",
                "source_url": c.get("source_url") or "",
            }
            for c in items
        ],
    }


def refresh_cards(recommendation, all_measures):
    """Arvutab salvestatud soovituse kaartide `source_status`-e uuesti.

    `ranked_cards_json` on lukustatud hetktõmmis ja see on tahtlik: skoor,
    selgitus ja kontrollnimekiri ei tohi kasutaja selja taga muutuda. Allikaseis
    EI ole aga soovituse osa — see on väide maailma kohta ("see link viitab
    kehtetule redaktsioonile"). Külmutatuna jääb ekraanile eelmise skreepimise
    tulemus koos kuupäeva ja lingiga, mis enam kokku ei käi, ja parandus ei
    jõuaks kunagi juba loodud vestlusteni.

    Muudab kaardi dict-e kohapeal ja tagastab sama `recommendation` objekti.
    """
    if not recommendation:
        return recommendation

    by_id = {m["measure_id"]: m for m in all_measures if m.get("measure_id")}
    by_name = {m["name"]: m for m in all_measures}

    for card in recommendation.get("ranked_cards") or []:
        # measure_id puudub kahel meetmel, millel ei ole detailfaili — neile
        # jääb nimi ainsaks ühenduslüliks (vt measures.measure_key()).
        measure = by_id.get(card.get("measure_id")) or by_name.get(card.get("name"))
        if measure is None:
            continue
        status = card_status(measure)
        if status is not None:
            card["source_status"] = status

    return recommendation
