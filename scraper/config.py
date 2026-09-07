"""
Konfiguratsioon scraperi jaoks.

Allikate nimekiri EI ole siia kõvakodeeritud — see loetakse
`backend.measures.load_measures()` kaudu master CSV-i `Link` ja
`Täiendavad lingid` veergudest, et allikad püsiksid ühes kohas.
See fail hoiab ainult käitumisreegleid.
"""

import os
from pathlib import Path

# Teed ankurdatakse faili asukoha peale, mitte cwd peale — sama muster mis
# backend/db.py ROOT. Cwd-suhtelised teed katkeksid taustalõimes ja cron'i all.
ROOT = Path(__file__).resolve().parent.parent

# APP_DATA_DIR peab siin kehtima TÄPSELT nagu backend/db.py-s. Konteineris on
# ainus kirjutatav koht /data ja lähtekood ise on read-only, seega ilma selle
# ülekirjutuseta otsis scraperi olek end /app/data-st: kausta, mida ei ole ja
# mida ei saa luua. Tagajärg ei olnud vigane vastus vaid VAIKNE tühjus —
# backend/sources.py ei leidnud ühtegi lehte, card_status() tagastas None ja
# soovituskaartidelt kadusid õigusakti kastid ilma ühegi veateateta.
DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (ROOT / "data"))

# Scraperi enda olek (sisu-hash, puhastatud tekst, ekstraheeritud faktid).
# Kõrvuti app.db-ga; data/ on juba .gitignore-s.
STATE_DB_PATH = DATA_DIR / "scraper_state.db"

# Inimesele ülevaatamiseks mõeldud .md kokkuvõte. Sama põhjus: konteineris ei
# saa seda lähtepuusse kirjutada, seega läheb see samuti andmekausta.
REVIEW_OUTPUT_DIR = (
    DATA_DIR / "katsetused" if os.environ.get("APP_DATA_DIR") else ROOT / "katsetused"
)

# Riigi Teataja avalik API. Akti lehed ise on Angular SPA — HTML-i tõmbamine
# annab 35 tähemärki tühja skeletti, seega sisu tuleb SIIT. Vt scraper/riigiteataja.py.
RT_API = "https://www.riigiteataja.ee/public-api/api/v1"

# Identifitseerime end selgelt — hea tava, ja lubab allika omanikul meid
# vajadusel robots.txt kaudu blokeerida või meiega ühendust võtta.
#
# HOIATUS: HOIA SEE STRING PUHTALT ASCII-s. Cloudflare (nii eis.ee kui
# riigiteataja.ee ees) vastab 403-ga IGALE päringule, kui User-Agent sisaldab
# mitte-ASCII tähemärke. Mõõdetud: "ATI sisemine töövahend" -> 403 kõigile
# URL-idele; sama string ilma täpitähtedeta -> 200. Viga näeks välja nagu
# robots.txt blokeering ja on seetõttu tülikas diagnoosida.
USER_AGENT = "RahastusmeetmedBot/1.0 (ATI internal tool; +https://eis.ee)"

# Minimaalne paus järjestikuste päringute vahel SAMA domeeni piires (sekundites).
MIN_DELAY_PER_DOMAIN_SECONDS = 5.0

HTTP_TIMEOUT_SECONDS = 30

# Kui palju allikateksti mahub ühte vestluse süsteemipromptisse. Õigusakt on
# keskmiselt ~34 000 tähemärki, meetme leht ~30 000 — eelarve katab mõlemad.
# Ületamisel kärbitakse meetme lehte, mitte õigusakti (vt backend/sources.py).
CHAT_SOURCE_CHAR_BUDGET = 60_000

# Ranking-prompt käib korraga KÕIGI kandidaatide kohta, seega seal on ruumi
# ainult delta'le ja vastuoludele, mitte täistekstile.
RANK_BLOCK_CHAR_BUDGET = 900

# Kui pikk tekstijupp maksimaalselt LLM-i ekstraheerimiskutsungisse läheb.
EXTRACT_CHAR_BUDGET = 60_000


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def scraper_enabled() -> bool:
    """Taustaülesande värav. Vaikimisi VÄLJAS, et testid ja arendus võrku ei läheks.

    Loetakse kutsungi hetkel (mitte impordi ajal), sest backend/main.py kutsub
    load_dotenv() alles pärast mooduli importi.
    """
    return _flag("SCRAPER_ENABLED", False)


def interval_days() -> int:
    try:
        return int(os.environ.get("SCRAPER_INTERVAL_DAYS", "7"))
    except ValueError:
        return 7


# Väljad, mille muutus on "kõrge risk" ja vajab ALATI inimese kinnitust enne
# master CSV-i kandmist, isegi kui LLM on kindel.
HIGH_RISK_FIELDS = {
    "status", "max_grant", "max_grant_eur", "applicant", "size",
    "region", "exclusions", "cofinancing",
}
