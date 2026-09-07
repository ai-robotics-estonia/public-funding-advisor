"""
SQLite kiht scraperi oleku ja allikakihi jaoks.

Neli tabelit, kaks eri eesmärki:

AUTOMAATNE (inimkinnitust ei vaja, sest need on TÕENDID viidetega, mitte
kuvatavad väärtused — sama loogika mis üleslaaditud failidel backend/files.py-s):
- page_state:       iga jälgitava URL-i viimane seis (hash + puhastatud tekst)
- measure_facts:    allikast ekstraheeritud struktureeritud väärtused + tsitaadid
- source_conflicts: kohad, kus allikas ja CSV lähevad lahku

INIMKINNITUST VAJAV (need muudaksid kuvatavat CSV väärtust):
- pending_changes:  kõrge riskiga muudatusettepanekud, mille inimene
                    scraper/review.py kaudu kinnitab või tagasi lükkab

Lisaks `meta` võti-väärtus tabel ajastaja `last_run_at` jaoks.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
-- PK on (measure_key, url), MITTE ainult url: kaks meedet võivad seaduslikult
-- jagada sama õigusakti (nt innovatsiooniosak ja arendusosak on mõlemad
-- määruses 115022023009; RUP ja RUP väikeprojektid määruses 109092025002).
-- Ainult url-i peal olev PK laseks hiljem töödeldud meetmel varasema
-- measure_key üle kirjutada, mille tagajärjel esimene meede kaotaks oma
-- õigusakti ja saaks vestluses tühja allikakihi.
CREATE TABLE IF NOT EXISTS page_state (
    url TEXT NOT NULL,
    measure_key TEXT NOT NULL,
    measure_name TEXT NOT NULL,
    source_type TEXT NOT NULL,      -- eis | legal | other
    resolved_url TEXT,              -- legal: kehtiva redaktsiooni URL
    etag TEXT,
    last_modified TEXT,
    content_hash TEXT,
    cleaned_text TEXT,
    superseded INTEGER NOT NULL DEFAULT 0,
    valid_until TEXT,
    valid_from TEXT,
    linked_id TEXT,                 -- legal: akti ID, millele CSV lingib
    kehtiv_id TEXT,                 -- legal: praegu kehtiva redaktsiooni ID
    linked_url TEXT,                -- legal: lingitud redaktsiooni URL
    linked_valid_from TEXT,
    -- Lingitud (vananenud) redaktsiooni tekst. Ainus koht, kus `valid_until`
    -- kuupäev on nähtav — ilma selleta ei saa vastuolu tsitaati kontrollida.
    linked_text TEXT,
    pins_revision INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_changed_at TEXT,
    PRIMARY KEY (measure_key, url)
);
CREATE INDEX IF NOT EXISTS idx_page_state_measure ON page_state(measure_key);

CREATE TABLE IF NOT EXISTS measure_facts (
    measure_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence TEXT NOT NULL,       -- kõrge / keskmine / madal
    citation TEXT NOT NULL,         -- VERBATIM väljavõte allikast
    source_url TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY (measure_key, field)
);

CREATE TABLE IF NOT EXISTS source_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measure_key TEXT NOT NULL,
    field TEXT NOT NULL,
    csv_value TEXT,
    source_value TEXT,
    severity TEXT NOT NULL,         -- kõrge / keskmine / madal
    citation TEXT,
    source_url TEXT,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_measure ON source_conflicts(measure_key);

CREATE TABLE IF NOT EXISTS pending_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measure_key TEXT NOT NULL,
    measure_name TEXT NOT NULL,
    url TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    confidence TEXT NOT NULL,
    citation TEXT NOT NULL,
    diff_snippet TEXT,
    is_high_risk INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    reviewed_at TEXT,
    approved INTEGER                -- NULL = otsustamata, 0 = tagasi lükatud, 1 = kinnitatud
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Veerud, mis lisandusid pärast esimest väljalaset. `CREATE TABLE IF NOT EXISTS`
# ei puutu olemasolevat tabelit, seega vajab juba loodud andmebaas ALTER-it.
_ADDED_COLUMNS = {
    "page_state": {
        "linked_id": "TEXT",
        "kehtiv_id": "TEXT",
        "linked_url": "TEXT",
        "linked_valid_from": "TEXT",
        "linked_text": "TEXT",
        "pins_revision": "INTEGER NOT NULL DEFAULT 1",
        "act_title": "TEXT",
    },
}


def _migrate(conn) -> None:
    """Lisab puuduvad veerud. Idempotentne — uue andmebaasi puhul ei tee midagi."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- page_state ------------------------------------------------------------

def get_page_state(conn, measure_key: str, url: str):
    row = conn.execute(
        "SELECT * FROM page_state WHERE measure_key = ? AND url = ?", (measure_key, url)
    ).fetchone()
    return dict(row) if row else None


def upsert_page_state(conn, *, url, measure_key, measure_name, source_type,
                      content_hash, cleaned_text, changed: bool,
                      etag=None, last_modified=None, resolved_url=None,
                      superseded=False, valid_until=None, valid_from=None,
                      linked_id=None, kehtiv_id=None, linked_url=None,
                      linked_valid_from=None, linked_text=None, pins_revision=True,
                      act_title=None):
    """Salvestab lehe seisu.

    Kutsutakse ka siis, kui midagi ei muutunud — nii jääb `last_checked_at`
    ajakohaseks ja UI saab näidata "kontrollitud <kuupäev>" ka muutumatu lehe
    puhul. Muutumatu lehe korral säilitatakse olemasolev tekst ja hash.
    """
    existing = get_page_state(conn, measure_key, url) or {}
    now = _now()
    conn.execute(
        """
        INSERT INTO page_state (url, measure_key, measure_name, source_type, resolved_url,
                                etag, last_modified, content_hash, cleaned_text,
                                superseded, valid_until, valid_from,
                                linked_id, kehtiv_id, linked_url, linked_valid_from,
                                linked_text, pins_revision, act_title,
                                last_checked_at, last_changed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(measure_key, url) DO UPDATE SET
            measure_name=excluded.measure_name,
            source_type=excluded.source_type,
            resolved_url=excluded.resolved_url,
            etag=excluded.etag,
            last_modified=excluded.last_modified,
            content_hash=excluded.content_hash,
            cleaned_text=excluded.cleaned_text,
            superseded=excluded.superseded,
            valid_until=excluded.valid_until,
            valid_from=excluded.valid_from,
            linked_id=excluded.linked_id,
            kehtiv_id=excluded.kehtiv_id,
            linked_url=excluded.linked_url,
            linked_valid_from=excluded.linked_valid_from,
            linked_text=excluded.linked_text,
            pins_revision=excluded.pins_revision,
            act_title=excluded.act_title,
            last_checked_at=excluded.last_checked_at,
            last_changed_at=excluded.last_changed_at
        """,
        (
            url, measure_key, measure_name, source_type, resolved_url,
            etag, last_modified,
            content_hash if content_hash is not None else existing.get("content_hash"),
            cleaned_text if cleaned_text is not None else existing.get("cleaned_text"),
            int(superseded), valid_until, valid_from,
            linked_id, kehtiv_id, linked_url, linked_valid_from,
            # Lingitud redaktsioon on muutumatu ajalooline tekst — kui selle
            # tõmbamine seekord ebaõnnestus, jäägu varem salvestatud tõend alles.
            linked_text if linked_text is not None else existing.get("linked_text"),
            int(pins_revision), act_title,
            now, now if changed else existing.get("last_changed_at"),
        ),
    )


def get_pages_for_measure(conn, measure_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM page_state WHERE measure_key = ? ORDER BY source_type, url",
        (measure_key,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- measure_facts ---------------------------------------------------------

def replace_measure_facts(conn, measure_key: str, facts: list[dict]) -> None:
    """Kirjutab meetme faktid üle. Allikas on tõde — vana hetktõmmis kaob."""
    conn.execute("DELETE FROM measure_facts WHERE measure_key = ?", (measure_key,))
    now = _now()
    conn.executemany(
        """
        INSERT OR REPLACE INTO measure_facts
            (measure_key, field, value, confidence, citation, source_url, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (measure_key, f["field"], f["value"], f.get("confidence", "madal"),
             f.get("citation", ""), f.get("source_url", ""), now)
            for f in facts
        ],
    )


def get_measure_facts(conn, measure_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM measure_facts WHERE measure_key = ? ORDER BY field", (measure_key,)
    ).fetchall()
    return [dict(r) for r in rows]


# --- source_conflicts ------------------------------------------------------

def replace_conflicts(conn, measure_key: str, conflicts: list[dict]) -> None:
    """Asendab meetme vastuolud. Lahendatud vastuolu kaob nimekirjast iseenesest."""
    conn.execute("DELETE FROM source_conflicts WHERE measure_key = ?", (measure_key,))
    now = _now()
    conn.executemany(
        """
        INSERT INTO source_conflicts
            (measure_key, field, csv_value, source_value, severity, citation, source_url, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (measure_key, c["field"], c.get("csv_value"), c.get("source_value"),
             c.get("severity", "keskmine"), c.get("citation", ""), c.get("source_url", ""), now)
            for c in conflicts
        ],
    )


def get_conflicts(conn, measure_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM source_conflicts WHERE measure_key = ? ORDER BY severity, field",
        (measure_key,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- pending_changes -------------------------------------------------------

def sync_pending_changes(conn, measure_key: str, conflicts: list[dict]) -> int:
    """Viib läbi vaatamata muudatused praeguste vastuoludega kooskõlla.

    Ootel rida on koopia vastuolust, mis tehti selle avastamise hetkel. Kui
    vastuolu hiljem kaob (nt CSV link parandatakse või selgub, et see kannabki
    `?leiaKehtiv` parameetrit ega ole kinnistatud), jääks vana rida ülevaatajale
    ette koos tsitaadi ja lingiga, mis enam kokku ei käi — täpselt see tekitas
    väite kuupäeva kohta, mida lingitud lehelt ei leidnud.

    Kinnitatud/tagasi lükatud ridu ei puutu: need on inimese otsuse ajalugu.
    Tagastab kustutatud ridade arvu.
    """
    current = {(c["field"], c.get("source_value") or ""): c for c in conflicts}

    stale = []
    for row in conn.execute(
        "SELECT id, field, new_value FROM pending_changes "
        "WHERE measure_key = ? AND approved IS NULL", (measure_key,)
    ).fetchall():
        conflict = current.get((row["field"], row["new_value"] or ""))
        if conflict is None:
            stale.append(row["id"])
        else:
            # Sama muudatus, aga tsitaat või tõendi link võis muutuda. Tühja
            # source_url'i korral jääb rea senine url alles — `add_pending_change`
            # kasutab seal meetme enda linki ja seda ei tohi ära kaotada.
            conn.execute(
                "UPDATE pending_changes SET citation = ?, url = COALESCE(?, url) WHERE id = ?",
                (conflict.get("citation", ""), conflict.get("source_url") or None, row["id"]),
            )

    for change_id in stale:
        conn.execute("DELETE FROM pending_changes WHERE id = ?", (change_id,))
    return len(stale)


def add_pending_change(conn, *, measure_key, measure_name, url, field, old_value,
                       new_value, confidence, citation, diff_snippet,
                       is_high_risk: bool) -> bool:
    """Lisab ülevaatust ootava muudatuse. Tagastab False, kui see oli duplikaat.

    Dedup on hädavajalik: ilma selleta lisaks iga nädalane jooks sama
    lahendamata muudatuse uuesti, kuni tabel on koopiaid täis. Juba
    LÄBIVAATATUD (kinnitatud/tagasi lükatud) rida ei blokeeri uut kirjet —
    kui sama muudatus tuleb hiljem tagasi, tahame seda uuesti näha.
    """
    duplicate = conn.execute(
        """
        SELECT 1 FROM pending_changes
        WHERE measure_key = ? AND field = ? AND new_value = ? AND approved IS NULL
        """,
        (measure_key, field, new_value),
    ).fetchone()
    if duplicate:
        return False

    conn.execute(
        """
        INSERT INTO pending_changes
            (measure_key, measure_name, url, field, old_value, new_value,
             confidence, citation, diff_snippet, is_high_risk, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (measure_key, measure_name, url, field, old_value, new_value,
         confidence, citation, diff_snippet, int(is_high_risk), _now()),
    )
    return True


def get_unreviewed_changes(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM pending_changes WHERE approved IS NULL ORDER BY is_high_risk DESC, measure_name, field"
    ).fetchall()
    return [dict(r) for r in rows]


def set_review_decision(conn, change_id: int, approved: bool) -> None:
    conn.execute(
        "UPDATE pending_changes SET approved = ?, reviewed_at = ? WHERE id = ?",
        (int(approved), _now(), change_id),
    )


# --- meta ------------------------------------------------------------------

def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
