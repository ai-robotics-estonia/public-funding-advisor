"""
Allikatekstist struktureeritud faktide ekstraheerimine ja vastuolude leidmine.

Kaks sammu, teadlikult selles järjekorras:

1. `extract_facts()` — ÜKS LLM-kutsung meetme kohta (mitte lehe kohta), mis loeb
   meetme lehe + kehtiva õigusakti ja tagastab skeemiväljade väärtused koos
   VERBATIM tsitaadiga allikast. Tsitaadinõue on hallutsinatsioonipiirang: kui
   mudel ei suuda väidet tsiteerida, jääb väli välja.

2. `find_conflicts()` — võrdleb ekstraheeritud väärtusi CSV omadega.
   Deterministlik võrdlus teeb põhitöö (summad, protsendid, staatus); LLM-i EI
   kutsuta üldse. Nii ei sõltu "kas siin on vastuolu" otsus mudeli tujust ja
   '7 500' vs '7500' ei tekita valehäiret.

Diff-arvutus (`compute_diff`) on alles ülevaatajale konteksti näitamiseks.
"""

import difflib
import json
import re

from backend.llm import complete_chat
from backend.measures import parse_amount, parse_percent_set

from . import config


# Samad väljanimed, mis backend/measures.py kasutab.
SCHEMA_FIELDS = [
    "funder", "max_grant", "cofinancing", "total_budget", "status",
    "description", "applicant", "size", "region", "sector", "field",
    "project_type", "ai", "rnd", "phase", "partner", "university",
    "exclusions", "checkpoints",
]

# Väljad, mille puhul deterministlik võrdlus on usaldusväärne. Proosaväljad
# (description, exclusions, checkpoints) jäävad välja: seal on sõnastuserinevus
# reegel, mitte erand, ja iga ümbersõnastus tekitaks valehäire.
COMPARABLE_NUMERIC = {"max_grant", "total_budget"}

# Ainult `status` on vaba tekstina usaldusväärselt võrreldav. `applicant`, `size`
# ja `region` olid siin varem ja tootsid peaaegu ainult müra — mõõdetud päris
# jooksul: CSV "ettevõte" vs allikas "ettevõtja" / "äriühing" / "Eesti
# äriregistrisse kantud äriühing". Need ei ole vastuolud, vaid sünonüümid või
# allikas, mis on lihtsalt täpsem. Valehäire on siin kallis: see õpetab
# ülevaatajat hoiatusi eirama, mistõttu päris leid (nt staatus "Suletud")
# upub müra sisse.
COMPARABLE_TEXT = {"status"}

EXTRACTION_SYSTEM_PROMPT = """Sa aitad hoida Eesti innovatsioonitoetuste andmebaasi ajakohasena.

Sulle antakse ÜHE rahastusmeetme ametlikud allikatekstid: meetme veebileht ja
(enamasti) selle kehtiv õigusakt. Sinu ülesanne on lugeda neist välja meetme
struktureeritud väljade väärtused.

Väljad: {fields}

Reeglid:
- Iga välja kohta anna VERBATIM tsitaat allikatekstist, mis seda väärtust tõendab.
  Kui sa ei suuda täpselt tsiteerida, JÄTA SEE VÄLI VASTUSEST VÄLJA.
- Ära leiuta väärtusi. Ära tuleta väärtust "üldiselt teadaolevast" — ainult tekstist.
- Väärtused hoia lühikesed ja samas stiilis, mis on antud praegustes väärtustes
  (nt summa "7 500", omafinantseering "20%", staatus "avatud").
- Kui õigusakt ja veebileht on omavahel vastuolus, eelista ÕIGUSAKTI ja märgi
  see tsitaadis ära.
- Väljad, mille kohta allikas midagi ei ütle, jäta lihtsalt välja.

Vasta AINULT JSON massiiviga, ilma lisatekstita:
[
  {{
    "field": "<üks ülalolevatest väljanimedest>",
    "value": "<väärtus>",
    "confidence": "kõrge" | "keskmine" | "madal",
    "citation": "<verbatim väljavõte allikast>",
    "source": "<'leht' või 'õigusakt'>"
  }}
]"""


def compute_diff(old_text: str | None, new_text: str) -> str:
    """Kompaktne unified diff ülevaatajale konteksti näitamiseks."""
    if old_text is None:
        return "\n".join(f"+{line}" for line in new_text.splitlines()[:200])
    return "\n".join(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(), lineterm="", n=2,
    ))


def _parse_json_array(text: str) -> list:
    """Sama tolerants mis backend/rank.py:96 — ```json aiad ja ümbritsev tekst."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            # Ei ürita vigast JSON-i "ära arvata" — parem jätta muutus
            # tuvastamata kui kirjutada andmebaasi prügi.
            return []


def _current_values_block(measure: dict) -> str:
    lines = []
    for field in SCHEMA_FIELDS:
        value = (measure.get(field) or "").strip()
        if value:
            lines.append(f"- {field}: {value}")
    return "\n".join(lines)


def build_extraction_message(measure: dict, sources: list[dict]) -> str:
    """Koostab kasutajasõnumi: praegused väärtused + allikatekstid.

    Praegused väärtused on kaasas, et mudel teaks, mis stiilis väärtusi ootame
    ja mida andmebaas juba sisaldab — ilma selleta pakub ta ümbersõnastusi,
    mis on sisuliselt samad väärtused.
    """
    parts = [
        f"Meede: {measure['name']}",
        "",
        "## Praegused väärtused andmebaasis",
        _current_values_block(measure) or "(tühi)",
    ]

    # Õigusakt esimesena: see on autoriteetsem allikas, ja kui eelarve otsa
    # saab, kärbitakse tagant (ehk meetme lehte, mitte õigusakti).
    ordered = sorted(sources, key=lambda s: 0 if s["source_type"] == "legal" else 1)
    budget = config.EXTRACT_CHAR_BUDGET
    for source in ordered:
        text = source.get("text") or ""
        if not text or budget <= 0:
            continue
        label = "Õigusakt" if source["source_type"] == "legal" else "Meetme leht"
        chunk = text[:budget]
        budget -= len(chunk)
        parts.append("")
        parts.append(f"## {label}: {source.get('resolved_url') or source['url']}")
        if len(chunk) < len(text):
            chunk += "\n[... tekst kärbitud pikkuse tõttu ...]"
        parts.append(chunk)

    parts.append("")
    parts.append("Loe ülaltoodud allikatest välja väljade väärtused ja vasta ainult JSON-massiiviga.")
    return "\n".join(parts)


def extract_facts(measure: dict, sources: list[dict]) -> list[dict]:
    """Üks LLM-kutsung meetme kohta. Tagastab valideeritud faktide listi."""
    usable = [s for s in sources if (s.get("text") or "").strip()]
    if not usable:
        return []

    raw = complete_chat(
        system=EXTRACTION_SYSTEM_PROMPT.format(fields=", ".join(SCHEMA_FIELDS)),
        messages=[{"role": "user", "content": build_extraction_message(measure, usable)}],
        max_tokens=4000,
        purpose="scraper_extract",
    )

    by_type = {s["source_type"]: (s.get("resolved_url") or s["url"]) for s in usable}
    # Kõik allikatekstid ühes normaliseeritud stringis tsitaadi kontrolliks.
    haystack = _collapse(" ".join(s.get("text") or "" for s in usable))

    facts = []
    for item in _parse_json_array(raw):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", "")).strip()
        value = str(item.get("value", "")).strip()
        citation = str(item.get("citation", "")).strip()
        # Valideerime väljanime skeemi vastu — ilma selleta läheks mudeli
        # väljamõeldud väljanimi otse andmebaasi ja sealt promptidesse.
        if field not in SCHEMA_FIELDS or not value or not citation:
            continue
        # Tsitaat peab päriselt allikatekstis esinema, mitte lihtsalt olemas olema.
        if not _citation_supported(citation, haystack):
            print(f"    [tsiteerimata] {field}: {citation[:60]!r}")
            continue
        source_type = "legal" if str(item.get("source", "")).startswith("õigus") else "eis"
        facts.append({
            "field": field,
            "value": value,
            "confidence": item.get("confidence", "madal"),
            "citation": citation,
            "source_url": by_type.get(source_type) or next(iter(by_type.values()), ""),
        })
    return facts


def _normalize_text(value: str) -> str:
    """Väärtuste võrdluseks: väiketähed, kirjavahemärgid ja tühikud maha."""
    return re.sub(r"[^a-zõäöüšž0-9]", "", value.lower())


def _collapse(value: str) -> str:
    """Tühikute ja tõstu normaliseerimine tsitaadi otsimiseks allikatekstist."""
    return re.sub(r"\s+", " ", value.lower()).strip()


def _citation_supported(citation: str, haystack: str) -> bool:
    """Kas tsitaat esineb päriselt allikatekstis?

    Ilma selle kontrollita on tsitaadinõue ainult nominaalne: mudel võib selle
    rahuldada, kirjutades tsitaadi asemel proosat. Mõõdetud näide Gemmalt:
        "ei leidnud otsest tsitaati, kuid toetatakse teadus- ja arendustegevust"
    See läbis tühjuse-kontrolli ja oleks jõudnud andmebaasi "tõendina".

    Väga lühikesi tsitaate ei kontrolli — need on liiga üldised, et otsing
    oleks tähenduslik, ja nende puhul otsustab niikuinii inimene.
    """
    citation = _collapse(citation)
    if len(citation) < 12:
        return True
    return citation in haystack


def _percent_set(value: str) -> tuple[int, ...] | None:
    """Kõik protsendid väärtuses, järjestatuna.

    Normaliseerimine ise elab backend/measures.py-s, sest sama teisendust vajab
    ka soovituste pool (omafinantseeringu %-st taotleja enda panuse arvutamiseks).
    Siin on ainult scraperi nimi sellele.
    """
    return parse_percent_set(value)


def _values_conflict(field: str, csv_value: str, source_value: str) -> bool:
    """Kas need kaks väärtust on SISULISELT erinevad (mitte lihtsalt vormistuselt)?"""
    if not csv_value.strip() or not source_value.strip():
        return False

    if field in COMPARABLE_NUMERIC:
        # '7 500' ja '7500' peavad olema võrdsed — parse_amount viskab
        # tühikud ja eurosümbolid minema.
        a, b = parse_amount(csv_value), parse_amount(source_value)
        return a is not None and b is not None and a != b

    if field == "cofinancing":
        a, b = _percent_set(csv_value), _percent_set(source_value)
        if a is not None and b is not None:
            # 'vähemalt 55%' vs '55–75%': allikas nimetab ainult alampiiri.
            # Kui üks hulk sisaldub teises, ei ole see vastuolu.
            return not (set(a) <= set(b) or set(b) <= set(a))
        return _normalize_text(csv_value) != _normalize_text(source_value)

    if field in COMPARABLE_TEXT:
        a, b = _normalize_text(csv_value), _normalize_text(source_value)
        if not a or not b:
            return False
        # Üks sisaldub teises ('avatud' vs 'avatud taotlusvoor') ei ole vastuolu.
        return a not in b and b not in a

    return False


def find_conflicts(measure: dict, facts: list[dict], pages: list[dict]) -> list[dict]:
    """Võrdleb allikast loetut CSV-ga. Deterministlik — LLM-i ei kutsuta.

    Kaks vastuolu liiki:
    - väljaväärtuse erinevus (staatus, summa, omafinantseering, ...)
    - `õigusakt` (kõrge): CSV link on KINNISTATUD redaktsioonile, mis enam ei kehti

    AKTI UUENEMINE EI OLE VASTUOLU. Skreeper loeb alati kehtivat redaktsiooni
    (vt `riigiteataja.fetch_act()`), seega uuenenud akt ei tähenda, et meie
    andmed oleksid valed — kui midagi päriselt muutus, tuleb see välja
    väljaväärtuste võrdluses allpool. `?leiaKehtiv` lingiga akte on 10 neljast-
    teistkümnest ja neist igaühele alalise hoiatuse näitamine uputaks päris
    vastuolud müra alla. Kehtiva redaktsiooni number ja jõustumiskuupäev
    kuvatakse eraldi neutraalse infona (`backend/sources.py: _legal_act()`).

    Kinnistatud link on seevastu päris defekt: sellele klõpsaja saab kehtetu
    teksti. `source_url` osutab siin KEHTIVALE redaktsioonile — sinna me tahame
    lugeja saata — ja tsitaat nimetab mõlemad ID-d, et parandus oleks tehtav.
    """
    conflicts = []

    for page in pages:
        if not page.get("superseded") or not page.get("pins_revision", True):
            continue
        linked = page.get("linked_id") or ""
        kehtiv = page.get("kehtiv_id") or ""
        resolved_url = page.get("resolved_url") or page["url"]
        conflicts.append({
            "field": "õigusakt",
            "csv_value": page["url"],
            "source_value": resolved_url,
            "severity": "kõrge",
            "citation": (
                (f"CSV link on kinnistatud redaktsioonile {linked}, mis enam ei kehti."
                 if linked else "CSV link on kinnistatud vanale redaktsioonile.")
                + " Rakendus loeb andmed kehtivast redaktsioonist"
                + (f" {kehtiv}" if kehtiv else "")
                + "; CSV link tuleks parandada."
            ),
            "source_url": resolved_url,
        })

    for fact in facts:
        field = fact["field"]
        csv_value = (measure.get(field) or "").strip()
        if not _values_conflict(field, csv_value, fact["value"]):
            continue
        conflicts.append({
            "field": field,
            "csv_value": csv_value,
            "source_value": fact["value"],
            "severity": "kõrge" if field in config.HIGH_RISK_FIELDS else "keskmine",
            "citation": fact["citation"],
            "source_url": fact["source_url"],
        })

    return conflicts
