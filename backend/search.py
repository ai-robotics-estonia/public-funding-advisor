"""Retrieval over the measure catalogue: which measures does this project need?

The hard filter (measures.filter_candidates) removes measures the applicant is
ineligible for. It knows nothing about what the project is actually about, so
everything it keeps used to go to the LLM in full. That works at 18 measures and
stops working as the catalogue grows — the ranking prompt is one call over all
survivors.

This module narrows that set by content. Every measure is chunked (one chunk for
the master row, one per detail-CSV row) into the `chunks` table and indexed by
SQLite's FTS5. A query built from the intake is scored against those chunks with
BM25, the per-chunk scores are folded up per measure, and only the best ones go
on to rank().

Why sparse vectors and not embeddings: the app talks to OpenRouter (chat
completions only) and Anthropic (no embeddings API), so dense vectors would mean
a third provider or a local model — a new key, new dependency and a much bigger
image, for a catalogue of 18 documents. BM25 ships inside the sqlite3 that is
already here. `rank_measures()` is the single seam an embedding backend would
replace; nothing above it knows how the scores are produced.
"""

import hashlib
import logging
import math
import os
import re

from backend import db, measures as measures_mod

log = logging.getLogger(__name__)

# The chunk index is derived from the CSVs. This key holds a hash of them, so a
# restart rebuilds only when the catalogue actually changed.
_INDEX_HASH_KEY = "chunks_source_hash"

# How many measures survive retrieval. Below this the retrieval step is a no-op,
# which keeps today's behaviour (18 measures, all sent) bit-for-bit identical.
DEFAULT_TOP_N = 10

# A measure's score is the sum of its best chunks, not of all of them — otherwise
# a measure with a long detail CSV outranks a better-matching short one purely on
# length.
CHUNKS_PER_MEASURE = 5

# Estonian function words. They appear in nearly every chunk, so they carry no
# signal and only cost query time.
STOP_WORDS = {
    "ja", "ning", "või", "ega", "ehk", "aga", "kuid", "sest", "et", "kui",
    "on", "oli", "olid", "olema", "see", "need", "selle", "neid", "mis", "kes",
    "oma", "kõik", "keegi", "miski", "kus", "millal", "miks", "kuidas",
    "ma", "sa", "ta", "me", "te", "nad", "mulle", "sulle", "talle",
    "mitte", "ei", "ära", "üle", "all", "läbi", "ka", "veel", "ju",
    "ei", "jah", "ole", "peab", "saab", "võib", "tuleb", "meede", "meetme",
}


def tokenize(text: str) -> list[str]:
    """Split text into lowercase word tokens, keeping Estonian letters intact.

    Deliberately mirrors what FTS5's unicode61 tokenizer does to the indexed
    side, so a token produced here can actually match one in the index.
    """
    if not text:
        return []
    text = text.lower()
    text = re.sub(r"[^a-z0-9õäöüšž]+", " ", text)
    return [t for t in text.split() if len(t) > 1 and t not in STOP_WORDS]


def fts_query(text: str) -> str:
    """Turn free user text into a safe FTS5 MATCH expression, or '' if empty.

    User text must never reach MATCH unescaped: bare `AND`, `OR`, `NOT`, `*`,
    `"`, `^` and `:` are all FTS5 syntax, so a project description containing
    any of them raises OperationalError instead of searching. Every token is
    quoted, which makes it a literal string, and the tokens are OR-ed because
    we want the best partial match, not documents containing all the words.
    """
    tokens = tokenize(text)
    if not tokens:
        return ""
    # Doubling '"' is the FTS5 escape for a quote inside a quoted string. After
    # tokenize() there are none left, but the escape keeps this correct if the
    # tokenizer is ever loosened.
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _chunk_texts(measure: dict) -> list[tuple[str, str | None, str]]:
    """Chunks for one measure as (kind, vali, text).

    The measure name is repeated in every chunk on purpose: a chunk is scored on
    its own, so without the name a detail row about "abikõlblikud kulud" is
    indistinguishable from the same row under a different measure.
    """
    name = measure["name"]
    chunks = [(
        "master",
        None,
        " | ".join(filter(None, [
            f"Meede: {name}",
            f"Rahastaja: {measure['funder']}",
            f"Kirjeldus: {measure['description']}",
            f"Valdkond: {measure['field']}",
            f"Sektor: {measure['sector']}",
            f"Projekti tüüp: {measure['project_type']}",
            f"Sobiv taotleja: {measure['applicant']}",
            f"Arengufaas: {measure['phase']}",
        ])),
    )]
    for row in measure["detail"]:
        text = f"Meede: {name} | {row['vali']}: {row['vaartus']} | {row['pohjendus']}"
        chunks.append(("detail", row["vali"], text))
    return chunks


def _source_hash(measures: list[dict]) -> str:
    """Fingerprint of everything that would go into the index."""
    h = hashlib.sha256()
    for m in measures:
        h.update(measures_mod.measure_key(m).encode("utf-8"))
        for kind, vali, text in _chunk_texts(m):
            h.update(f"{kind}\x00{vali}\x00{text}\x00".encode("utf-8"))
    return h.hexdigest()


def reindex(measures: list[dict]) -> int:
    """Rebuild the chunk index from scratch. Returns the number of chunks.

    Delete-then-insert rather than an incremental update: the whole catalogue is
    a few hundred rows, and an external-content FTS5 table left out of step with
    its content table returns wrong results silently.
    """
    with db.connect() as conn:
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM chunks")
        rows = []
        for m in measures:
            key = measures_mod.measure_key(m)
            for kind, vali, text in _chunk_texts(m):
                rows.append((key, m["measure_id"], kind, vali, text))
        conn.executemany(
            "INSERT INTO chunks (measure_key, measure_id, kind, vali, text) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        # External-content tables are not populated by the insert above; 'rebuild'
        # reads the content table and regenerates the whole index.
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    return len(rows)


def reindex_if_stale(measures: list[dict]) -> int:
    """Rebuild the index only when the catalogue changed. Returns chunks written, or 0."""
    fingerprint = _source_hash(measures)
    if db.get_meta(_INDEX_HASH_KEY) == fingerprint:
        with db.connect() as conn:
            existing = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if existing:
            log.info("chunk index up to date (%d chunks)", existing)
            return 0
    n = reindex(measures)
    db.set_meta(_INDEX_HASH_KEY, fingerprint)
    log.info("chunk index rebuilt: %d chunks from %d measures", n, len(measures))
    return n


def build_query(intake: dict, clarifications: dict | None = None) -> str:
    """The retrieval query: what the applicant said their project is about.

    Only the free-text fields go in. The structured intake (applicant type,
    size, region) is what the hard filter already applied — feeding it here
    again would just match the boilerplate that every measure repeats.
    """
    parts = [intake.get("project_description", "")]
    for question, answer in (clarifications or {}).items():
        if answer:
            parts.append(f"{question} {answer}")
    return " ".join(p for p in parts if p)


def rank_measures(candidates: list[dict], query: str) -> dict[str, float]:
    """BM25 relevance per measure_key, for the given candidates only.

    Keys absent from the result matched nothing. FTS5's bm25() returns *negative*
    numbers with the better match more negative, so scores are negated here and
    every caller above sees "higher is better".
    """
    match = fts_query(query)
    if not match:
        return {}

    keys = {measures_mod.measure_key(m) for m in candidates}
    if not keys:
        return {}

    placeholders = ",".join("?" * len(keys))
    sql = (
        "SELECT c.measure_key AS key, -bm25(chunks_fts) AS score "
        "FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
        f"WHERE chunks_fts MATCH ? AND c.measure_key IN ({placeholders}) "
        "ORDER BY score DESC"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, [match, *keys]).fetchall()

    per_measure: dict[str, list[float]] = {}
    for row in rows:
        per_measure.setdefault(row["key"], []).append(row["score"])

    # Diminishing weight down each measure's chunk list, so one very strong chunk
    # beats several mediocre ones instead of being averaged away by them.
    return {
        key: sum(s / math.log2(i + 2) for i, s in enumerate(scores[:CHUNKS_PER_MEASURE]))
        for key, scores in per_measure.items()
    }


def top_n() -> int:
    """How many measures survive retrieval; RAG_TOP_N overrides the default."""
    try:
        n = int(os.environ.get("RAG_TOP_N", DEFAULT_TOP_N))
    except ValueError:
        return DEFAULT_TOP_N
    return n if n > 0 else DEFAULT_TOP_N


def select_candidates(candidates: list[dict], query: str, n: int | None = None):
    """Narrow candidates to the n most relevant. Returns (kept, cut).

    'cut' has the shape filter_candidates() uses for dropped measures, so the
    frontend lists them with a reason instead of them vanishing silently.

    Retrieval never fires when it cannot help or could only hurt: too few
    candidates to narrow, an unusable query, or an index that matched nothing.
    In each of those cases every candidate is kept, in its original order.
    """
    n = top_n() if n is None else n
    if len(candidates) <= n:
        return candidates, []

    scores = rank_measures(candidates, query)
    if not scores:
        log.info("retrieval matched nothing, keeping all %d candidates", len(candidates))
        return candidates, []

    # Unmatched measures sort last but keep their catalogue order among
    # themselves, so the cut is stable when scores tie.
    ordered = sorted(
        enumerate(candidates),
        key=lambda pair: (-scores.get(measures_mod.measure_key(pair[1]), 0.0), pair[0]),
    )
    kept_idx = {i for i, _ in ordered[:n]}

    kept = [m for i, m in enumerate(candidates) if i in kept_idx]
    cut = [
        {"name": m["name"], "reason": "ei olnud projekti kirjeldusega piisavalt seotud"}
        for i, m in enumerate(candidates)
        if i not in kept_idx
    ]
    return kept, cut
