"""
Taustaajastaja scraperi tsükli jaoks.

Miks lõim, mitte cron: rakendus deploy'itakse ühe uvicorn protsessina ja
cron nõuaks eraldi seadistust hostis. Miks mitte "jooksuta käivitumisel":
uvicorn --reload käivitub arenduses kümneid kordi päevas.

Lahendus: lõim ärkab kord tunnis ja vaatab `meta.last_run_at`. Tsükkel
käivitub ainult siis, kui viimasest jooksust on möödas vähemalt
SCRAPER_INTERVAL_DAYS. Väravaks SCRAPER_ENABLED (vaikimisi väljas), et
testid ja arendus võrku ei läheks.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import config

log = logging.getLogger(__name__)

WAKE_INTERVAL_SECONDS = 3600

_started = False
_lock = threading.Lock()


def _claim_run() -> bool:
    """Võtab jooksu enda alla, kui aeg on käes. Tagastab True, kui tohib joosta.

    `BEGIN IMMEDIATE` võtab kirjutuslukku KOHE, ja `last_run_at` kirjutatakse
    ETTE, mitte pärast. Nii ei saa kaks uvicorni töölist (või lõimet) sama
    tsüklit korraga käivitada — teine näeb juba uuendatud ajatemplit.
    """
    config.STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.STATE_DB_PATH), timeout=10.0)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key = 'last_run_at'").fetchone()

        if row and row[0]:
            try:
                last = datetime.fromisoformat(row[0])
                if datetime.now(timezone.utc) - last < timedelta(days=config.interval_days()):
                    conn.rollback()
                    return False
            except ValueError:
                pass  # vigane ajatempel -> jookse ja kirjuta üle

        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_run_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning("ajastaja ei saanud andmebaasi luku: %s", exc)
        return False
    finally:
        conn.close()


def _loop() -> None:
    while True:
        try:
            if _claim_run():
                from . import run  # laisk import — võrguviga ei blokeeri käivitumist
                log.info("tsükkel algab")
                totals = run.run_cycle()
                log.info(
                    "valmis: %s allikat, %s muutunud, %s LLM-kutsungit, %s vastuolu",
                    totals["sources"], totals["changed"],
                    totals["llm_calls"], totals["conflicts"],
                )
        except Exception as exc:
            # Scraperi viga ei tohi API-t maha võtta.
            log.error("tsükkel ebaõnnestus: %s", exc)
        threading.Event().wait(WAKE_INTERVAL_SECONDS)


def start_background_scheduler() -> bool:
    """Käivitab taustalõime, kui SCRAPER_ENABLED on seatud. Tagastab, kas käivitus."""
    global _started
    if not config.scraper_enabled():
        log.info("scheduler off (SCRAPER_ENABLED pole seatud)")
        return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_loop, name="scraper-scheduler", daemon=True).start()
    log.info("scheduler started (intervall %s päeva)", config.interval_days())
    return True
