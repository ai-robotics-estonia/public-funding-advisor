"""Delete data past its retention period.

    python -m tools.purge --older-than 90 --dry-run
    python -m tools.purge --older-than 90
    python -m tools.purge --vestlused 30

The audit trail stores full prompts, which include whatever company documents a
tester uploaded. That is deliberate — it is the only way to reconstruct why the
model said what it said — but it means the tables hold personal and commercial
data and must not accumulate forever. The retention period announced on the
login screen and the number passed here have to agree.

`--older-than` covers `events` and `llm_calls`. `--vestlused` additionally
removes vestlused the tester never saved — the scratch sessions that pile up
from opening the app and not finishing. A SAVED vestlus is never touched by
either flag: saving is the tester saying they want it kept, and only the app's
own delete flow removes it.
"""

import argparse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from backend import db  # noqa: E402

TABLES = ("events", "llm_calls")


def cutoff_iso(days: int) -> str:
    """The ISO timestamp before which rows are considered expired."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def counts_before(cutoff: str) -> dict[str, int]:
    """How many rows in each audit table predate the cutoff."""
    with db.connect() as conn:
        return {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE ts < ?", (cutoff,)
            ).fetchone()[0]
            for table in TABLES
        }


def purge(cutoff: str) -> dict[str, int]:
    """Delete rows older than the cutoff; returns the per-table delete counts."""
    deleted = {}
    with db.connect() as conn:
        for table in TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted[table] = cur.rowcount
    return deleted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.purge", description=__doc__)
    parser.add_argument("--older-than", type=int, default=90,
                        help="auditilogide säilitusaeg päevades (vaikimisi 90)")
    parser.add_argument("--vestlused", type=int, metavar="PÄEVA",
                        help="kustuta ka salvestamata vestlused, mida pole nii mitu "
                             "päeva puudutatud (salvestatud vestlusi ei puudutata)")
    parser.add_argument("--dry-run", action="store_true",
                        help="näita, mida kustutataks, aga ära kustuta")
    args = parser.parse_args(argv)

    db.init_db()
    cutoff = cutoff_iso(args.older_than)
    pending = counts_before(cutoff)
    total = sum(pending.values())

    print(f"Piir: {cutoff} ({args.older_than} päeva)")
    for table, n in pending.items():
        print(f"  {table:<12} {n} rida")

    vestlus_cutoff = None
    if args.vestlused is not None:
        vestlus_cutoff = cutoff_iso(args.vestlused)
        pending_vestlused = db.count_unsaved_vestlused(vestlus_cutoff)
        total += pending_vestlused
        print(f"Piir: {vestlus_cutoff} ({args.vestlused} päeva)")
        print(f"  {'vestlused':<12} {pending_vestlused} salvestamata")

    if args.dry_run:
        print(f"\n--dry-run: {total} kirjet jääks kustutamata.")
        return 0
    if total == 0:
        print("\nMidagi kustutada pole.")
        return 0

    deleted = purge(cutoff)
    print(f"\nKustutatud: {sum(deleted.values())} auditirida.")
    if vestlus_cutoff is not None:
        removed = db.delete_unsaved_vestlused(vestlus_cutoff)
        print(f"Kustutatud: {len(removed)} salvestamata vestlust (koos failidega).")
    # Audit tables are the big ones; reclaim the file space they freed.
    with db.connect() as conn:
        conn.execute("VACUUM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
