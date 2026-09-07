"""
Scraperi tsükkel.

    python -m scraper.run

Allikate nimekiri tuleb `backend.measures.load_measures()`-ist, mitte omaenda
CSV-parserist: nii jäävad veerunihked ühte kohta ja saame `measure_id`,
`link` ning `links` (Täiendavad lingid) tasuta.

KULUKONTROLL: LLM-i kutsutakse ainult siis, kui mõni meetme allikatest
tegelikult muutus (või kui meede on esimest korda nähtud). Muutumatu nädal
maksab null kutsungit. Kutsungeid on üks MEETME kohta, mitte lehe kohta.

Master CSV-i EI kirjutata kunagi ja ridu EI eemaldata kunagi. Kõrge riskiga
erinevused lähevad `pending_changes`-i, kus inimene need `python -m scraper.review`
kaudu üle vaatab.
"""

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# Tsükkel kestab minuteid. Torusse (cron, logifail, taustalõim) kirjutades
# puhverdab Python stdout'i plokkide kaupa, mistõttu jooks paistab minutite
# kaupa vaikiv. Reapuhverdus kehtib kogu protsessile, seega ka extract.py
# ja fetch.py teadetele.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # nt kinni pandud või ümber suunatud voog
        pass

# Peab jooksma enne backend.* importe, sest backend/llm.py loeb env-i.
# `python -m scraper.run` ei käivitu backend/main.py kaudu, seega ilma selleta
# jääks API võti leidmata.
load_dotenv()

from backend import measures  # noqa: E402

from . import config, db, extract, fetch, riigiteataja  # noqa: E402


def measure_sources(measure: dict) -> list[dict]:
    """Meetme jälgitavad allikad: põhilink + Täiendavad lingid.

    'Täiendavad lingid' veerg sisaldab ka mitte-URL teksti (nt "Digitaliseerimise
    teekaardi HEA TAVA"), seega filtreerime http-prefiksi järgi.

    Tüüp tuleb URL-ist ENDAST, mitte veerust, kust ta pärineb. Põhilink oli varem
    kõvakodeeritud 'eis'-iks, sest kõigil senistel meetmetel oligi seal EIS-i leht
    ja õigusakt seisis 'Täiendavates linkides'. Meede, mis on alles tulemas, ei
    oma veel EIS-i lehte — tema põhilink ON määrus, ja 'eis'-iks sildistatuna
    jäi see backend/sources.py jaoks nähtamatuks: kaardilt kadus õigusakti rida
    ja vestlus ei saanud akti teksti, millele §-viiteid teha.
    """
    sources = []
    if measure["link"].startswith("http"):
        sources.append({
            "url": measure["link"],
            "source_type": "legal" if riigiteataja.is_act_url(measure["link"]) else "eis",
        })
    for link in measure["links"]:
        link = link.strip()
        if not link.startswith("http"):
            continue
        sources.append({
            "url": link,
            "source_type": "legal" if riigiteataja.is_act_url(link) else "other",
        })
    return sources


def fetch_source(conn, measure: dict, source: dict, cache: dict | None = None) -> dict | None:
    """Tõmbab ühe allika. Tagastab seisu dict-i või None, kui tõmbamine ebaõnnestus.

    Tagastatav dict sisaldab alati `text` (ka muutumatu lehe puhul, andmebaasist),
    et ekstraheerimine saaks töötada kõigi allikatega korraga.

    `cache` on tsüklisisene URL -> tulemus vahemälu. Kaks meedet võivad jagada
    sama õigusakti (innovatsiooni-/arendusosak; RUP ja RUP väikeprojektid) —
    ilma vahemäluta tõmbaksime selle kaks korda. Seis salvestatakse siiski
    MÕLEMA meetme jaoks eraldi reana, sest akt kuulub sisuliselt mõlemale.
    """
    url = source["url"]
    key = measures.measure_key(measure)
    prev = db.get_page_state(conn, key, url)
    cache = cache if cache is not None else {}

    try:
        if source["source_type"] == "legal":
            act = cache.get(url)
            if act is None:
                act = riigiteataja.fetch_act(url)
                cache[url] = act
            changed = act.content_hash != (prev or {}).get("content_hash")
            db.upsert_page_state(
                conn, url=url, measure_key=key, measure_name=measure["name"],
                source_type="legal", resolved_url=act.resolved_url,
                content_hash=act.content_hash, cleaned_text=act.text, changed=changed,
                superseded=act.superseded, valid_until=act.valid_until,
                valid_from=act.valid_from,
                linked_id=act.linked_id, kehtiv_id=act.kehtiv_id,
                linked_url=act.linked_url, linked_valid_from=act.linked_valid_from,
                linked_text=act.linked_text, pins_revision=act.pins_revision,
                act_title=act.act_title,
            )
            return {
                "url": url, "source_type": "legal", "resolved_url": act.resolved_url,
                "text": act.text, "changed": changed, "superseded": act.superseded,
                "valid_until": act.valid_until, "act_title": act.act_title,
                "linked_id": act.linked_id, "kehtiv_id": act.kehtiv_id,
                "linked_url": act.linked_url, "pins_revision": act.pins_revision,
            }

        result = cache.get(url)
        if result is None:
            result = fetch.fetch_if_changed(url, prev)
            # Vahemällu ainult sisuga tulemus. 304-vastusel on cleaned_text None
            # ja see kõlbaks ainult sellele meetmele, kelle ETag saadeti.
            if result.cleaned_text is not None:
                cache[url] = result
        # Vahemälust tulnud tulemus arvutati TEISE meetme eelmise seisu vastu,
        # seega otsustame muutuse alati oma prev_state järgi.
        changed = (result.content_hash != (prev or {}).get("content_hash")
                   if result.content_hash is not None else False)
        text = result.cleaned_text if result.cleaned_text is not None else (prev or {}).get("cleaned_text")
        db.upsert_page_state(
            conn, url=url, measure_key=key, measure_name=measure["name"],
            source_type=source["source_type"], etag=result.etag,
            last_modified=result.last_modified, content_hash=result.content_hash,
            cleaned_text=result.cleaned_text, changed=changed,
        )
        return {
            "url": url, "source_type": source["source_type"], "resolved_url": None,
            "text": text, "changed": changed, "superseded": False,
            "valid_until": None,
        }

    except fetch.ScrapeNotAllowed as exc:
        print(f"    [vahele jäetud] {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"    [viga tõmbamisel] {url}: {exc}", file=sys.stderr)
    return None


def refresh_conflicts(conn, measure: dict, key: str, fetched: list[dict],
                      facts: list[dict] | None = None) -> list[dict]:
    """Arvutab meetme vastuolud üle ja hoiab ülevaatuse töölaua nendega kooskõlas.

    Kutsutakse ka siis, kui ükski allikas ei muutunud, ja see on tahtlik.
    Vastuolu ei sõltu ainult LLM-i faktidest: `õigusakt`-vastuolu tuleb tervenisti
    `page_state`-ist (milline redaktsioon, kehtivuse lõpp, kas link on kinnistatud)
    ja meie enda võrdlusloogikast. Mõlemad muutuvad ilma, et allika sisu liiguks.
    Ilma selle ümberarvutuseta jäid ekraanile eelmise koodiversiooniga tehtud
    tsitaadid — kuupäev osutas ühele redaktsioonile, link teisele, ja kasutaja ei
    leidnud tsiteeritud kuupäeva mitte kuskilt.

    LLM-i siin ei kutsuta: `facts` loetakse vajadusel andmebaasist.
    """
    if facts is None:
        facts = db.get_measure_facts(conn, key)
    conflicts = extract.find_conflicts(measure, facts, fetched)
    db.replace_conflicts(conn, key, conflicts)
    db.sync_pending_changes(conn, key, conflicts)
    return conflicts


def process_measure(conn, measure: dict, cache: dict | None = None) -> dict:
    """Töötleb ühe meetme kõik allikad. Tagastab kokkuvõtte dict-i."""
    key = measures.measure_key(measure)
    name = measure["name"]
    summary = {"name": name, "sources": 0, "changed": 0, "llm_calls": 0,
               "facts": 0, "conflicts": 0, "pending": 0}

    is_new = not db.get_pages_for_measure(conn, key)

    fetched = []
    for source in measure_sources(measure):
        state = fetch_source(conn, measure, source, cache)
        if state:
            fetched.append(state)
            summary["sources"] += 1
            summary["changed"] += int(state["changed"])

    if not fetched:
        print(f"  [allikateta] {name}")
        return summary

    if not (summary["changed"] or is_new):
        summary["conflicts"] = len(refresh_conflicts(conn, measure, key, fetched))
        print(f"  [muutuseta] {name} ({summary['sources']} allikat)")
        return summary

    label = "alusjoon" if is_new else "MUUTUS"
    print(f"  [{label}] {name} — ekstraheerin {summary['sources']} allikast...")

    # Loendame katse, mitte õnnestumise: ebaõnnestunud kutsung võib olla samuti
    # arveldatud, ja kulukokkuvõte peab seda näitama.
    summary["llm_calls"] = 1
    try:
        facts = extract.extract_facts(measure, fetched)
    except Exception as exc:
        # Üks võrgu- või LLM-viga ei tohi ülejäänud 16 meedet maha võtta.
        print(f"    [viga ekstraheerimisel] {name}: {exc}", file=sys.stderr)
        return summary

    db.replace_measure_facts(conn, key, facts)
    summary["facts"] = len(facts)

    conflicts = refresh_conflicts(conn, measure, key, fetched, facts)
    summary["conflicts"] = len(conflicts)

    # Ainult kõrge riskiga vastuolud lähevad inimese töölauale. Ülejäänud
    # elavad tõendikihis ja jõuavad promptidesse ilma kinnituseta.
    for conflict in conflicts:
        if conflict["severity"] != "kõrge":
            continue
        added = db.add_pending_change(
            conn,
            measure_key=key,
            measure_name=name,
            url=conflict.get("source_url") or measure["link"],
            field=conflict["field"],
            old_value=conflict.get("csv_value") or "",
            new_value=conflict.get("source_value") or "",
            confidence=next((f["confidence"] for f in facts if f["field"] == conflict["field"]), "keskmine"),
            citation=conflict.get("citation", ""),
            diff_snippet="",
            # Järgib severity't, mitte HIGH_RISK_FIELDS-i. Ainult kõrge
            # raskusastmega vastuolud jõuavad siia, aga väli 'õigusakt' ei ole
            # skeemiväli ega seega HIGH_RISK_FIELDS-is — ilma selleta kuvaks
            # ülevaatus kehtetut õigusakti "madala riskina".
            is_high_risk=conflict["severity"] == "kõrge",
        )
        summary["pending"] += int(added)

    return summary


def write_review_markdown(conn) -> str:
    """Kirjutab kinnitamata muudatused ühte .md faili, meetme kaupa grupeeritult."""
    changes = db.get_unreviewed_changes(conn)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = config.REVIEW_OUTPUT_DIR / f"scraper-review-{stamp}.md"
    config.REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not changes:
        out_path.write_text("# Scraperi ülevaatus\n\nOotel muudatusi ei ole.\n", encoding="utf-8")
        return str(out_path)

    by_measure: dict[str, list[dict]] = {}
    for change in changes:
        by_measure.setdefault(change["measure_name"], []).append(change)

    lines = [f"# Scraperi ülevaatus — {stamp}", "",
             f"{len(changes)} ootel muudatust. Kinnita või lükka tagasi: `python -m scraper.review`", ""]
    for name, items in by_measure.items():
        lines.append(f"## {name}")
        for change in items:
            risk = "⚠️ KÕRGE RISK" if change["is_high_risk"] else "madal risk"
            lines.append(f"- **{change['field']}** ({risk}, kindlus: {change['confidence']})")
            lines.append(f"  - CSV-s praegu: `{change['old_value']}`")
            lines.append(f"  - allikas ütleb: `{change['new_value']}`")
            lines.append(f"  - tsitaat: {change['citation']}")
            lines.append(f"  - allikas: {change['url']}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return str(out_path)


def run_cycle() -> dict:
    """Jooksutab terve tsükli. Tagastab kokkuvõtte (ajastaja logib seda)."""
    all_measures = [m for m in measures.load_measures() if m["link"].strip()]
    print(f"Kontrollin {len(all_measures)} meetme allikaid...")

    totals = {"measures": len(all_measures), "sources": 0, "changed": 0,
              "llm_calls": 0, "facts": 0, "conflicts": 0, "pending": 0}

    fetch_cache: dict = {}  # tsüklisisene, et jagatud õigusakti ei tõmmataks kaks korda
    with db.connect(config.STATE_DB_PATH) as conn:
        for measure in all_measures:
            summary = process_measure(conn, measure, fetch_cache)
            # Commit meetme kaupa: terve tsükkel kestab minuteid ja ilma selleta
            # kaotaks katkestus (või viga 17. meetmel) kõik varasema töö —
            # sealhulgas juba makstud LLM-kutsungid.
            conn.commit()
            for key in ("sources", "changed", "llm_calls", "facts", "conflicts", "pending"):
                totals[key] += summary[key]
        review_path = write_review_markdown(conn)
        db.set_meta(conn, "last_run_at", datetime.now(timezone.utc).isoformat())

    totals["review_path"] = review_path
    return totals


def main():
    totals = run_cycle()
    print(
        f"\nValmis. {totals['sources']} allikat, {totals['changed']} muutunud, "
        f"{totals['llm_calls']} LLM-kutsungit, {totals['facts']} fakti, "
        f"{totals['conflicts']} vastuolu, {totals['pending']} uut ootel muudatust."
    )
    print(f"Ülevaatuse fail: {totals['review_path']}")


if __name__ == "__main__":
    main()
