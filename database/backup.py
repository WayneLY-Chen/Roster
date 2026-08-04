"""SQLite backup, rotation and restore.

Backups use SQLite's online backup API, so they are consistent even while the
app is writing -- a plain file copy of a WAL database can be torn.

Naming: ``crm-<kind>-YYYYMMDD-HHMMSS.db`` where kind is ``daily``, ``weekly`` or
``manual``. :func:`run_scheduled_backups` is idempotent: it is called on every
start-up and only writes when the period's backup is missing.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import BackupError
from core.logging_setup import get_logger

log = get_logger(LogCategory.DATABASE)

_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


@dataclass(frozen=True, slots=True)
class BackupFile:
    """One backup on disk."""

    path: Path
    kind: str
    created_at: datetime
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


def _db_path(config: AppConfig) -> Path:
    path = config.database.sqlite_path
    if path is None:
        raise BackupError("backup is only supported for file-backed SQLite databases")
    return path


def _parse(path: Path) -> BackupFile | None:
    """Parse ``crm-<kind>-<timestamp>.db``; ``None`` for unrelated files."""
    stem = path.stem
    parts = stem.split("-")
    if len(parts) != 4 or parts[0] != "crm":
        return None
    kind = parts[1]
    try:
        created = datetime.strptime(f"{parts[2]}-{parts[3]}", _TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return BackupFile(
        path=path, kind=kind, created_at=created, size_bytes=path.stat().st_size
    )


def list_backups(config: AppConfig | None = None) -> list[BackupFile]:
    """All backups, newest first."""
    config = config or get_config()
    backup_dir = config.backup.resolved_dir
    if not backup_dir.exists():
        return []
    found = [_parse(p) for p in backup_dir.glob("crm-*.db")]
    return sorted(
        (b for b in found if b is not None), key=lambda b: b.created_at, reverse=True
    )


def create_backup(kind: str = "manual", config: AppConfig | None = None) -> BackupFile:
    """Write a consistent snapshot of the database."""
    config = config or get_config()
    source = _db_path(config)
    if not source.exists():
        raise BackupError(f"database file does not exist yet: {source}")

    backup_dir = config.backup.resolved_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"crm-{kind}-{datetime.now().strftime(_TIMESTAMP_FORMAT)}.db"

    # closing() is not optional here: sqlite3's context manager commits or
    # rolls back but never *closes*. On Windows the still-open handle on the
    # half-written target makes the cleanup unlink below raise PermissionError,
    # which would mask the real error.
    try:
        with closing(sqlite3.connect(source)) as src, closing(
            sqlite3.connect(target)
        ) as dst:
            src.backup(dst)
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"backup failed: {exc}") from exc

    log.info("backup written: {} ({:.2f} MB)", target.name, target.stat().st_size / 1048576)
    result = _parse(target)
    if result is None:  # pragma: no cover - name is generated above
        raise BackupError(f"unexpected backup filename: {target.name}")
    return result


def prune_backups(config: AppConfig | None = None) -> list[Path]:
    """Delete backups beyond the configured retention. Returns removed paths."""
    config = config or get_config()
    keep = {"daily": config.backup.keep_daily, "weekly": config.backup.keep_weekly}
    removed: list[Path] = []

    for kind, limit in keep.items():
        of_kind = [b for b in list_backups(config) if b.kind == kind]
        for stale in of_kind[limit:]:
            stale.path.unlink(missing_ok=True)
            removed.append(stale.path)
            log.info("pruned old backup {}", stale.name)
    return removed


def run_scheduled_backups(config: AppConfig | None = None) -> list[BackupFile]:
    """Create the day's and week's backups if they are missing, then prune.

    Called on start-up. Doing nothing is the normal outcome.
    """
    config = config or get_config()
    if config.database.sqlite_path is None or not config.database.sqlite_path.exists():
        return []

    existing = list_backups(config)
    today = date.today()
    created: list[BackupFile] = []

    if config.backup.daily:
        has_today = any(
            b.kind == "daily" and b.created_at.date() == today for b in existing
        )
        if not has_today:
            created.append(create_backup("daily", config))

    if config.backup.weekly:
        this_week = today.isocalendar()[:2]
        has_week = any(
            b.kind == "weekly" and b.created_at.date().isocalendar()[:2] == this_week
            for b in existing
        )
        if not has_week:
            created.append(create_backup("weekly", config))

    if created:
        prune_backups(config)
    return created


def restore_backup(backup: Path | str, config: AppConfig | None = None) -> Path:
    """Replace the live database with a backup.

    The current database is first copied aside as a ``pre-restore`` backup, so
    an accidental restore is itself recoverable.
    """
    config = config or get_config()
    source = Path(backup)
    if not source.is_absolute():
        source = config.backup.resolved_dir / source
    if not source.exists():
        raise BackupError(f"backup not found: {source}")

    try:
        with sqlite3.connect(source) as conn:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"{source.name} is not a readable SQLite database: {exc}") from exc

    target = _db_path(config)
    if target.exists():
        safety = target.parent / (
            f"pre-restore-{datetime.now().strftime(_TIMESTAMP_FORMAT)}.db"
        )
        shutil.copy2(target, safety)
        log.warning("previous database saved to {} before restore", safety.name)

    # WAL sidecars belong to the replaced database; leaving them corrupts state.
    for sidecar in (target.with_suffix(".db-wal"), target.with_suffix(".db-shm")):
        sidecar.unlink(missing_ok=True)

    shutil.copy2(source, target)
    log.info("restored database from {}", source.name)
    return target
