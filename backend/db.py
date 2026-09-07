"""SQLite persistence for vestlused (projects), files, recommendations, chat threads."""

import json
import os
import random
import sqlite3
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Overridable so a container can mount a volume outside the source tree.
# Defaults to the in-tree ./data that every existing checkout already uses.
DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (ROOT / "data"))
DB_PATH = DATA_DIR / "app.db"
UPLOADS_DIR = DATA_DIR / "uploads"

# Short readable codes: uppercase letters + digits (e.g. AB3K9RT2).
#
# Length 8, up from the original 5. Once a saved vestlus can be opened by any
# logged-in tester holding its code, the code IS the access token for it, and
# 36^5 (60 million) is small enough to walk through. 36^8 is ~2.8e12. Existing
# 5-character ids keep working — lookup is an exact match, not a length check.
_ID_ALPHABET = string.ascii_uppercase + string.digits
_ID_LEN = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    """Open a connection with row access by column name and FK enforcement on.

    Assumes init_db() has already run (data dir + tables exist) — call it once
    at startup, not on every query.

    WAL matters once more than one person is using the app: under the default
    rollback journal a writer blocks every reader database-wide, so two testers
    overlapping produced 'database is locked'. WAL lets readers run during a
    write; busy_timeout makes the remaining writer-vs-writer case wait instead
    of failing instantly. journal_mode is persistent on the file, but setting it
    per connection is idempotent and keeps fresh test databases correct too.
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the data/upload directories and tables if they do not exist.

    Call once at process startup (main.py) or at the top of a test fixture —
    every function below assumes this has already run.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vestlused (
                vestluse_id TEXT PRIMARY KEY,
                intake_json TEXT NOT NULL DEFAULT '{}',
                clarifications_json TEXT,
                title TEXT,
                tester_id TEXT,
                -- Set only when the tester explicitly saves. Unsaved vestlused
                -- are scratch work and tools/purge.py eventually removes them;
                -- saved ones are listable by their owner and openable by anyone
                -- they hand the code to.
                saved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vestluse_id TEXT NOT NULL REFERENCES vestlused(vestluse_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                extracted_text TEXT,
                status TEXT NOT NULL CHECK (status IN ('ok', 'extract_failed')),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recommendations (
                vestluse_id TEXT PRIMARY KEY REFERENCES vestlused(vestluse_id) ON DELETE CASCADE,
                candidates_json TEXT NOT NULL,
                dropped_json TEXT NOT NULL,
                ranked_cards_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Second-pass "what would we have to change" runs. Unlike
            -- recommendations this is not a one-shot snapshot: the user picks a
            -- different set of measures and runs it again, so every run is its
            -- own row and the newest one wins on read.
            CREATE TABLE IF NOT EXISTS refinements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vestluse_id TEXT NOT NULL REFERENCES vestlused(vestluse_id) ON DELETE CASCADE,
                measure_names_json TEXT NOT NULL,
                willing_to_change TEXT NOT NULL DEFAULT '',
                cards_json TEXT NOT NULL,
                failed_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS measure_threads (
                thread_id TEXT PRIMARY KEY,
                vestluse_id TEXT NOT NULL REFERENCES vestlused(vestluse_id) ON DELETE CASCADE,
                measure_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (vestluse_id, measure_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL REFERENCES measure_threads(thread_id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS testers (
                tester_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );

            -- Audit tables. Deliberately NOT foreign-keyed to vestlused: an
            -- audit row has to outlive the vestlus it describes, otherwise
            -- deleting a session would erase the evidence that it existed.
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                request_id TEXT,
                tester_id TEXT,
                ip TEXT,
                method TEXT,
                path TEXT,
                action TEXT,
                vestluse_id TEXT,
                thread_id TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                detail_json TEXT
            );

            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                request_id TEXT,
                tester_id TEXT,
                vestluse_id TEXT,
                purpose TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                system_text TEXT,
                messages_json TEXT,
                response_text TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cost_usd REAL,
                latency_ms INTEGER,
                ok INTEGER NOT NULL,
                error TEXT
            );

            -- Retrieval index over the measure catalogue (backend/search.py).
            -- Derived data only: it is rebuilt from the CSVs whenever their
            -- content hash changes, so losing it costs a startup, not data.
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                measure_key TEXT NOT NULL,
                measure_id TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('master', 'detail')),
                vali TEXT,
                text TEXT NOT NULL
            );

            -- External-content FTS5: the text lives in `chunks`, the index only
            -- holds the postings. 'remove_diacritics 0' is mandatory — the
            -- default folds õäöüšž into oaous and Estonian queries stop matching.
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                content='chunks',
                content_rowid='chunk_id',
                tokenize="unicode61 remove_diacritics 0"
            );

            -- Small key/value store for things that are neither user data nor
            -- schema: currently the hash of the CSVs the chunk index was built from.
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_measure ON chunks(measure_key);
            CREATE INDEX IF NOT EXISTS idx_user_files_vestlus
                ON user_files(vestluse_id);
            CREATE INDEX IF NOT EXISTS idx_threads_vestlus
                ON measure_threads(vestluse_id);
            CREATE INDEX IF NOT EXISTS idx_messages_thread
                ON messages(thread_id);
            CREATE INDEX IF NOT EXISTS idx_refinements_vestlus
                ON refinements(vestluse_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_testers_code ON testers(code_hash);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_tester ON events(tester_id);
            CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);
            CREATE INDEX IF NOT EXISTS idx_llm_calls_request ON llm_calls(request_id);
            """
        )
        # Runs after the tables exist but before any index that depends on a
        # migrated column, so an existing data/app.db upgrades in place.
        _migrate(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vestlused_tester ON vestlused(tester_id)"
        )


# Columns added after the first release. Applied idempotently so an existing
# data/app.db picks them up without a manual migration step.
_ADDED_COLUMNS = {
    "vestlused": [("tester_id", "TEXT"), ("saved", "INTEGER NOT NULL DEFAULT 0")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any post-release columns missing from an existing database."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def get_meta(key: str) -> str | None:
    """Read a value from the small key/value store, or None if unset."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    """Write a value to the small key/value store, replacing any previous one."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _new_vestluse_id(conn: sqlite3.Connection) -> str:
    """Generate a vestluse_id not already in use, retrying on collision."""
    for _ in range(50):
        code = "".join(random.choices(_ID_ALPHABET, k=_ID_LEN))
        row = conn.execute(
            "SELECT 1 FROM vestlused WHERE vestluse_id = ?", (code,)
        ).fetchone()
        if row is None:
            return code
    raise RuntimeError("could not allocate unique vestluse_id")


def create_vestlus(title: str | None = None, tester_id: str | None = None) -> str:
    """Create an empty project and return its vestluse_id."""
    now = _now()
    with connect() as conn:
        vid = _new_vestluse_id(conn)
        conn.execute(
            """
            INSERT INTO vestlused (vestluse_id, intake_json, clarifications_json, title,
                                   tester_id, created_at, updated_at)
            VALUES (?, '{}', NULL, ?, ?, ?, ?)
            """,
            (vid, title, tester_id, now, now),
        )
    return vid


# --- testers -------------------------------------------------------------
# The access gate. A tester row is created from the CLI (tools/testers.py);
# the plaintext code is shown once there and only its keyed hash is stored.


def add_tester(name: str, code_hash: str) -> str:
    """Register a tester and return the generated tester_id."""
    tester_id = uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO testers (tester_id, name, code_hash, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (tester_id, name, code_hash, _now()),
        )
    return tester_id


def get_tester(tester_id: str) -> dict | None:
    """Return a tester row as a dict, or None if missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM testers WHERE tester_id = ?", (tester_id,)
        ).fetchone()
    return dict(row) if row else None


def get_tester_by_code_hash(code_hash: str) -> dict | None:
    """Look up an *active* tester by the keyed hash of their login code.

    A direct lookup is safe because the hash is a deterministic HMAC over a
    high-entropy code — there is no per-row salt to iterate past.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM testers WHERE code_hash = ? AND active = 1", (code_hash,)
        ).fetchone()
    return dict(row) if row else None


def list_testers() -> list[dict]:
    """All testers, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT tester_id, name, active, created_at, last_seen_at "
            "FROM testers ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def set_tester_active(tester_id: str, active: bool) -> bool:
    """Enable/disable a tester's code. Returns False if no such tester."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE testers SET active = ? WHERE tester_id = ?",
            (1 if active else 0, tester_id),
        )
    return cur.rowcount > 0


def touch_tester(tester_id: str) -> None:
    """Record that this tester was seen just now."""
    with connect() as conn:
        conn.execute(
            "UPDATE testers SET last_seen_at = ? WHERE tester_id = ?", (_now(), tester_id)
        )


def get_vestlus(vestluse_id: str) -> dict | None:
    """Return vestlus row as a dict, or None if missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM vestlused WHERE vestluse_id = ?", (vestluse_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "vestluse_id": row["vestluse_id"],
            "intake": json.loads(row["intake_json"] or "{}"),
            "clarifications": (
                json.loads(row["clarifications_json"])
                if row["clarifications_json"] is not None
                else None
            ),
            "title": row["title"],
            "tester_id": row["tester_id"],
            "saved": bool(row["saved"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def save_vestlus(vestluse_id: str, title: str) -> bool:
    """Mark a vestlus as saved and name it. Returns False if it is missing.

    Saving is what makes a vestlus survive tools/purge.py and what makes its
    code work for anyone the tester shares it with.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE vestlused SET saved = 1, title = ?, updated_at = ? WHERE vestluse_id = ?",
            (title, _now(), vestluse_id),
        )
    return cur.rowcount > 0


def list_saved_vestlused(tester_id: str) -> list[dict]:
    """This tester's saved vestlused, most recently touched first.

    Scoped to the owner on purpose: a shared code opens one vestlus, it does not
    put it into someone else's list.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT v.vestluse_id, v.title, v.created_at, v.updated_at,
                   (r.vestluse_id IS NOT NULL) AS has_recommendation,
                   (SELECT COUNT(*) FROM measure_threads t
                     WHERE t.vestluse_id = v.vestluse_id) AS thread_count
            FROM vestlused v
            LEFT JOIN recommendations r ON r.vestluse_id = v.vestluse_id
            WHERE v.tester_id = ? AND v.saved = 1
            ORDER BY v.updated_at DESC
            """,
            (tester_id,),
        ).fetchall()
    return [
        {**dict(r), "has_recommendation": bool(r["has_recommendation"])} for r in rows
    ]


def count_unsaved_vestlused(cutoff: str) -> int:
    """How many unsaved vestlused predate the cutoff (for purge --dry-run)."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM vestlused WHERE saved = 0 AND updated_at < ?",
            (cutoff,),
        ).fetchone()[0]


def delete_unsaved_vestlused(cutoff: str) -> list[str]:
    """IDs of unsaved vestlused untouched since `cutoff`, deleted with their uploads.

    Deletes one at a time through delete_vestlus() rather than in a single
    statement: the upload folder lives on disk, outside anything ON DELETE
    CASCADE can reach.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT vestluse_id FROM vestlused WHERE saved = 0 AND updated_at < ?",
            (cutoff,),
        ).fetchall()
    ids = [r["vestluse_id"] for r in rows]
    for vid in ids:
        delete_vestlus(vid)
    return ids


def update_vestlus_intake(
    vestluse_id: str,
    intake: dict | None = None,
    clarifications: dict | None = None,
) -> bool:
    """Patch intake and/or clarifications. Returns False if vestlus missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM vestlused WHERE vestluse_id = ?", (vestluse_id,)
        ).fetchone()
        if row is None:
            return False
        if intake is not None:
            conn.execute(
                "UPDATE vestlused SET intake_json = ?, updated_at = ? WHERE vestluse_id = ?",
                (json.dumps(intake, ensure_ascii=False), _now(), vestluse_id),
            )
        if clarifications is not None:
            conn.execute(
                "UPDATE vestlused SET clarifications_json = ?, updated_at = ? WHERE vestluse_id = ?",
                (json.dumps(clarifications, ensure_ascii=False), _now(), vestluse_id),
            )
        return True


def delete_vestlus(vestluse_id: str) -> bool:
    """Delete project row (CASCADE) and upload folder. Returns False if missing."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM vestlused WHERE vestluse_id = ?", (vestluse_id,)
        )
        if cur.rowcount == 0:
            return False
    upload_dir = UPLOADS_DIR / vestluse_id
    if upload_dir.exists():
        for root, dirs, files in os.walk(upload_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(upload_dir)
    return True


def has_recommendation(vestluse_id: str) -> bool:
    """True if this vestlus already has a locked recommendation snapshot."""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM recommendations WHERE vestluse_id = ?", (vestluse_id,)
        ).fetchone()
        return row is not None


def save_recommendation(
    vestluse_id: str,
    candidates: list,
    dropped: list,
    ranked_cards: list,
) -> None:
    """Insert immutable recommendation snapshot. Raises if one already exists."""
    now = _now()
    with connect() as conn:
        if conn.execute(
            "SELECT 1 FROM recommendations WHERE vestluse_id = ?", (vestluse_id,)
        ).fetchone():
            raise ValueError("recommendation already exists")
        conn.execute(
            """
            INSERT INTO recommendations
                (vestluse_id, candidates_json, dropped_json, ranked_cards_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                vestluse_id,
                json.dumps(candidates, ensure_ascii=False),
                json.dumps(dropped, ensure_ascii=False),
                json.dumps(ranked_cards, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "UPDATE vestlused SET updated_at = ? WHERE vestluse_id = ?",
            (now, vestluse_id),
        )


def get_recommendation(vestluse_id: str) -> dict | None:
    """Return the locked recommendation snapshot as a dict, or None if missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE vestluse_id = ?", (vestluse_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "vestluse_id": row["vestluse_id"],
            "candidates": json.loads(row["candidates_json"]),
            "dropped": json.loads(row["dropped_json"]),
            "ranked_cards": json.loads(row["ranked_cards_json"]),
            "created_at": row["created_at"],
        }


def save_refinement(
    vestluse_id: str,
    measure_names: list,
    willing_to_change: str,
    cards: list,
    failed: list,
) -> None:
    """Insert one refinement run. Re-running just adds another row."""
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO refinements
                (vestluse_id, measure_names_json, willing_to_change, cards_json,
                 failed_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                vestluse_id,
                json.dumps(measure_names, ensure_ascii=False),
                willing_to_change,
                json.dumps(cards, ensure_ascii=False),
                json.dumps(failed, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "UPDATE vestlused SET updated_at = ? WHERE vestluse_id = ?",
            (now, vestluse_id),
        )


def get_latest_refinement(vestluse_id: str) -> dict | None:
    """Return this vestlus's most recent refinement run as a dict, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM refinements WHERE vestluse_id = ? ORDER BY id DESC LIMIT 1",
            (vestluse_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "vestluse_id": row["vestluse_id"],
            "measure_names": json.loads(row["measure_names_json"]),
            "willing_to_change": row["willing_to_change"],
            "cards": json.loads(row["cards_json"]),
            "failed": json.loads(row["failed_json"]),
            "created_at": row["created_at"],
        }


def add_file(
    vestluse_id: str,
    name: str,
    path: str,
    extracted_text: str | None,
    status: str,
) -> int:
    """Insert a user_file row and return its id."""
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO user_files (vestluse_id, name, path, extracted_text, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (vestluse_id, name, path, extracted_text, status, now),
        )
        conn.execute(
            "UPDATE vestlused SET updated_at = ? WHERE vestluse_id = ?",
            (now, vestluse_id),
        )
        return cur.lastrowid


def get_file(vestluse_id: str, file_id: int) -> dict | None:
    """Return a single file row (with extracted_text), or None if missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_files WHERE id = ? AND vestluse_id = ?",
            (file_id, vestluse_id),
        ).fetchone()
        return dict(row) if row is not None else None


def delete_file(vestluse_id: str, file_id: int) -> str | None:
    """Delete a file row. Returns its stored path, or None if missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT path FROM user_files WHERE id = ? AND vestluse_id = ?",
            (file_id, vestluse_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM user_files WHERE id = ?", (file_id,))
        conn.execute(
            "UPDATE vestlused SET updated_at = ? WHERE vestluse_id = ?",
            (_now(), vestluse_id),
        )
        return row["path"]


def get_extracted_texts(vestluse_id: str) -> list[dict]:
    """Return ok-status files with their extracted text, ordered by creation."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT name, extracted_text, created_at
            FROM user_files
            WHERE vestluse_id = ? AND status = 'ok' AND extracted_text IS NOT NULL
            ORDER BY created_at
            """,
            (vestluse_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_files(vestluse_id: str) -> list[dict]:
    """Return metadata for all files in this vestlus (text length, not full text)."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, vestluse_id, name, path, status, created_at,
                   CASE WHEN extracted_text IS NULL THEN 0 ELSE length(extracted_text) END AS text_len
            FROM user_files WHERE vestluse_id = ? ORDER BY id
            """,
            (vestluse_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "vestluse_id": r["vestluse_id"],
                "name": r["name"],
                "path": r["path"],
                "status": r["status"],
                "text_len": r["text_len"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def list_threads(vestluse_id: str) -> list[dict]:
    """Return all measure_threads for this vestlus, oldest first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT thread_id, vestluse_id, measure_id, created_at, updated_at
            FROM measure_threads WHERE vestluse_id = ? ORDER BY created_at
            """,
            (vestluse_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_or_create_thread(vestluse_id: str, measure_id: str) -> tuple[str, bool]:
    """Return (thread_id, created). One thread per (vestlus, measure) — idempotent."""
    with connect() as conn:
        row = conn.execute(
            "SELECT thread_id FROM measure_threads WHERE vestluse_id = ? AND measure_id = ?",
            (vestluse_id, measure_id),
        ).fetchone()
        if row is not None:
            return row["thread_id"], False

        thread_id = uuid.uuid4().hex
        now = _now()
        try:
            conn.execute(
                """
                INSERT INTO measure_threads (thread_id, vestluse_id, measure_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, vestluse_id, measure_id, now, now),
            )
        except sqlite3.IntegrityError:  # lost a race with a concurrent create
            existing = conn.execute(
                "SELECT thread_id FROM measure_threads WHERE vestluse_id = ? AND measure_id = ?",
                (vestluse_id, measure_id),
            ).fetchone()
            return existing["thread_id"], False
        return thread_id, True


def get_thread(thread_id: str) -> dict | None:
    """Return a thread row (with vestluse_id, measure_id), or None if missing."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM measure_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def add_message(thread_id: str, role: str, content: str) -> int:
    """Insert a message and bump the thread's updated_at. Returns the message id."""
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, role, content, now),
        )
        conn.execute(
            "UPDATE measure_threads SET updated_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        return cur.lastrowid


def list_messages(thread_id: str, limit: int | None = None) -> list[dict]:
    """Return this thread's messages oldest-first; if limit given, only the last N."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM messages WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ).fetchall()
    rows = [dict(r) for r in rows]
    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def clear_thread_messages(thread_id: str) -> None:
    """Delete every message in a thread; the thread_id itself is kept."""
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
        conn.execute(
            "UPDATE measure_threads SET updated_at = ? WHERE thread_id = ?",
            (_now(), thread_id),
        )
