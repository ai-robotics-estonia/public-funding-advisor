"""Snapshot the SQLite databases. Safe to run while the app is serving.

    python -m tools.backup
    python -m tools.backup --keep 30

Uses sqlite3's online backup API rather than copying files: under WAL a plain
copy can catch a torn state where the -wal file holds committed pages the .db
file does not. The API also takes a read lock only, so a running server keeps
serving throughout.

Pure stdlib on purpose — the sqlite3 command-line tool ships neither with WSL by
default nor in the python:3.12-slim image, so a shell version would need an apt
install on both sides.

Daily via WSL systemd (user units survive because systemd runs in this distro):
    ~/.config/systemd/user/rahastusmeetmed-backup.service   (ExecStart=... -m tools.backup)
    ~/.config/systemd/user/rahastusmeetmed-backup.timer     (OnCalendar=daily)
    systemctl --user enable --now rahastusmeetmed-backup.timer
"""

import argparse
import gzip
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from backend import db  # noqa: E402

DATABASES = ("app.db", "scraper_state.db")


def snapshot(src: Path, dest: Path) -> None:
    """Copy a live database to dest using the online backup API."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def compress(path: Path) -> Path:
    """gzip the snapshot in place and return the compressed path."""
    gz = path.with_suffix(path.suffix + ".gz")
    with open(path, "rb") as raw, gzip.open(gz, "wb") as out:
        shutil.copyfileobj(raw, out)
    path.unlink()
    return gz


def prune(backup_dir: Path, stem: str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` snapshots of one database."""
    existing = sorted(backup_dir.glob(f"{stem}-*.db.gz"), reverse=True)
    removed = []
    for old in existing[keep:]:
        old.unlink()
        removed.append(old)
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.backup", description=__doc__)
    parser.add_argument("--keep", type=int, default=14, help="mitu koopiat alles hoida")
    args = parser.parse_args(argv)

    data_dir = Path(db.DATA_DIR)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    made = 0
    for filename in DATABASES:
        src = data_dir / filename
        if not src.exists():
            print(f"vahele: {src} puudub")
            continue
        stem = src.stem
        dest = backup_dir / f"{stem}-{stamp}.db"
        snapshot(src, dest)
        gz = compress(dest)
        print(f"varundatud: {gz}  ({gz.stat().st_size / 1024:.0f} KB)")
        made += 1

        for old in prune(backup_dir, stem, args.keep):
            print(f"  kustutatud vana: {old.name}")

    if made == 0:
        print("Ühtegi andmebaasi ei leitud — kas APP_DATA_DIR on õige?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
