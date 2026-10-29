"""SQLite connection management, initialisation and backups."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger("eii.db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


class DatabaseError(RuntimeError):
    """Raised with a user-readable message when the research database cannot be used."""


def connect(db_path: Path) -> sqlite3.Connection:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not open research database at {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a transaction; commit on success, roll back on error."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    """Create tables if missing. Safe to call on every app start."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not initialise research database at {db_path}: {exc}") from exc
    finally:
        conn.close()
    log.info("Research database ready at %s", db_path)


def backup_db(db_path: Path, backup_dir: Path, max_backups: int = 20) -> Path:
    """Consistent online backup using SQLite's backup API. Keeps the newest `max_backups`."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"{db_path.stem}_{stamp}.db"
    src = connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _prune(backup_dir, f"{db_path.stem}_*.db", max_backups)
    log.info("Database backed up to %s", target)
    return target


def _prune(directory: Path, pattern: str, keep: int) -> None:
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            log.warning("Could not remove old backup %s", old)


def copy_with_backup(path: Path, backup_dir: Path, max_backups: int) -> Path | None:
    """Copy an existing file into backup_dir with a timestamp; returns the backup path."""
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, target)
    _prune(backup_dir, f"{path.stem}_*{path.suffix}", max_backups)
    return target
