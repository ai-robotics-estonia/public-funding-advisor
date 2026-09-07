"""
Riigi Teataja õigusaktide tõmbamine.

MIKS SEE MOODUL OLEMAS ON: riigiteataja.ee akti lehed on Angular SPA. Tavaline
HTML-i tõmbamine tagastab kõigil URL-idel identse ~52 KB skeleti, millest
tekstiks puhastub 35 tähemärki ("Riigi Teataja"). Naiivne scraper salvestaks
13 identset tühja lehte ja arvaks, et töötab.

Sisu tuleb avalikust API-st (leitav lehe main.js bundle'ist; CSP header viitab
/public-api/-le):

    GET /public-api/api/v1/akt/{id}?leiaKehtiv=true   Accept: application/json
        -> {"kehtivId": <praegu kehtiva redaktsiooni id>, ...}
    GET /public-api/api/v1/akt/{kehtivId}/blob-html
        -> akti terviktekst HTML-ina

TEINE, SISULINE PÕHJUS: master CSV lingib akti ID-le, mis oli kehtiv CSV
koostamise ajal. Õigusakti muutmisel saab muudetud tekst UUE ID — vana ID
jääb kehtima ajaloolise redaktsioonina. Seetõttu on `kehtivId != lingitud id`
signaal, et akti on muudetud.

AGA ENAMASTI MITTE VIGA: `?leiaKehtiv` parameetriga link laseb Riigi Teatajal
klõpsajale ISE kehtiva redaktsiooni serveerida, seega ei näe kasutaja kunagi
vananenud teksti. Master-CSV 13 lingist kannavad 10 seda parameetrit. Ainult
kinnistatud (parameetrita) link jääb päriselt kehtetule redaktsioonile pidama —
`pins_revision()` eristab neid kaht ja ainult kinnistatud lingist teeb
`extract.find_conflicts()` vastuolu.

MIDA KASUTAJA NÄEB: alati kehtivat redaktsiooni. Lingitud redaktsiooni andmed
(`linked_*`, `valid_until`) jäävad andmebaasi tõendiks, aga UI-sse ega
vestlusprompti need ei lähe — vt `backend/sources.py`.
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import config, fetch


ACT_ID_RE = re.compile(r"/akt/(\d+)")
_META_RE = re.compile(r"^(.*?):\s*(.*)$")
# Päisekuupäevad on RT-s alati pp.kk.aaaa. `_META_RE` on tahtlikult üldine
# ('silt:väärtus'), seega valideerime kuju enne, kui number tsitaati jõuab —
# tsitaadis olev vale kuupäev on hullem kui kuupäeva puudumine.
_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")


@dataclass
class ActResult:
    url: str                    # algne, CSV-s olev link
    linked_id: str              # akti ID, millele CSV lingib
    kehtiv_id: str              # praegu kehtiv redaktsioon
    act_title: str | None       # akti pealkiri ("Ettevõtja tootearenduse ...")
    resolved_url: str           # kehtiva redaktsiooni inimloetav URL
    linked_url: str             # lingitud redaktsiooni inimloetav URL
    text: str
    content_hash: str
    superseded: bool            # kehtiv_id != linked_id -> akti on muudetud
    valid_until: str | None     # lingitud redaktsiooni "Sõnastuse kehtivuse lõpp"
    valid_from: str | None      # kehtiva redaktsiooni "Sõnastuse jõustumise kp"
    linked_valid_from: str | None   # lingitud redaktsiooni "Sõnastuse jõustumise kp"
    linked_text: str | None     # lingitud redaktsiooni terviktekst (tõend)
    pins_revision: bool         # link on kinnistatud, s.t. ilma ?leiaKehtiv-ita


def is_act_url(url: str) -> bool:
    return "riigiteataja.ee" in url and bool(ACT_ID_RE.search(url))


def act_id(url: str) -> str | None:
    match = ACT_ID_RE.search(url)
    return match.group(1) if match else None


def pins_revision(url: str) -> bool:
    """Kas link on kinnistatud ühele redaktsioonile.

    `?leiaKehtiv` (väärtusega või ilma) paneb RT serveerima hetkel kehtivat
    teksti, seega selline link EI ole kinnistatud. Loeme query-stringist, mitte
    substringiga: `act_id()` viskab query osa minema ja meede nagu
    `/et/akt/117102023001?leiaKehtiv` peab samuti õigesti lahenema.
    """
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    return not any(k.lower() == "leiakehtiv" for k in query)


def _date_or_none(value: str | None) -> str | None:
    return value if value and _DATE_RE.fullmatch(value.strip()) else None


def _get_text(url: str, accept: str | None = None) -> str:
    headers = {"Accept": accept} if accept else None
    with fetch.http_get(url, headers) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_meta(text: str) -> dict[str, str]:
    """Akti alguses on 'Väljaandja: ...', 'Sõnastuse kehtivuse lõpp: ...' read.

    Puhastatud tekstis on need eraldi ridadel kujul 'silt:väärtus'. Loeme ainult
    esimesed ~40 rida, et mitte akti sisust valepositiivseid leida.
    """
    meta: dict[str, str] = {}
    for line in text.splitlines()[:40]:
        match = _META_RE.match(line)
        if match:
            key, value = match.group(1).strip(), match.group(2).strip()
            if key and value and key not in meta:
                meta[key] = value
    return meta


def fetch_act(url: str) -> ActResult:
    """Lahendab CSV lingi kehtivaks redaktsiooniks ja tõmbab terviktekstiga.

    Teeb kolm päringut: lingitud akti meta (kehtivId leidmiseks), lingitud akti
    tekst (kui akt on vahepeal muudetud) ja kehtiva redaktsiooni tekst. Kui akt
    on juba kehtiv, jääb keskmine päring ära.

    Vana redaktsiooni tekst SÄILITATAKSE `linked_text`-is. Kehtivuse lõpu
    kuupäev on nähtav ainult selle redaktsiooni päises — kui me teksti ära
    viskaksime, ei saaks tsitaadis olevat kuupäeva kuskilt kontrollida ja see
    näiks väljamõelduna.
    """
    linked = act_id(url)
    if not linked:
        raise ValueError(f"URL-ist ei leia akti ID-d: {url}")

    meta_raw = _get_text(f"{config.RT_API}/akt/{linked}?leiaKehtiv=true", "application/json")
    meta_json = json.loads(meta_raw)
    kehtiv = str(meta_json.get("kehtivId") or linked)
    superseded = kehtiv != linked
    # `?leiaKehtiv=true` tõttu kirjeldab `aktiParameetrid` juba KEHTIVAT
    # redaktsiooni, seega pealkiri tuleb tasuta — eraldi päringut ei ole vaja.
    act_title = ((meta_json.get("aktiParameetrid") or {}).get("pealkiri") or "").strip() or None

    text = fetch.clean_html(_get_text(f"{config.RT_API}/akt/{kehtiv}/blob-html"))
    meta = _parse_meta(text)

    valid_until = None
    linked_valid_from = None
    linked_text = None
    if superseded:
        # Aegunud redaktsiooni enda meta ütleb, MILLAL see kehtivuse kaotas —
        # see on ülevaatajale kõige kõnekam number, seega tasub lisapäringut.
        try:
            linked_text = fetch.clean_html(_get_text(f"{config.RT_API}/akt/{linked}/blob-html"))
            old_meta = _parse_meta(linked_text)
            valid_until = _date_or_none(old_meta.get("Sõnastuse kehtivuse lõpp"))
            linked_valid_from = _date_or_none(old_meta.get("Sõnastuse jõustumise kp"))
        except Exception as exc:
            print(f"[riigiteataja] aegunud redaktsiooni {linked} meta ei saadud: {exc}")

    return ActResult(
        url=url,
        linked_id=linked,
        kehtiv_id=kehtiv,
        act_title=act_title,
        resolved_url=f"https://www.riigiteataja.ee/akt/{kehtiv}",
        linked_url=f"https://www.riigiteataja.ee/akt/{linked}",
        text=text,
        content_hash=fetch.content_hash(text),
        superseded=superseded,
        valid_until=valid_until,
        valid_from=_date_or_none(meta.get("Sõnastuse jõustumise kp")),
        linked_valid_from=linked_valid_from,
        linked_text=linked_text,
        pins_revision=pins_revision(url),
    )
