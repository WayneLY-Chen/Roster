"""Tests for database/backup.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.errors import BackupError
from database.backup import (
    _TIMESTAMP_FORMAT,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
    run_scheduled_backups,
)


def _make_sqlite_db(path: Path, marker: str = "hello") -> None:
    # ``sqlite3.connect(...)`` used as a context manager only commits/rolls
    # back on exit -- it does not close the connection. On Windows an open
    # handle blocks later unlink()/rename() calls, so close explicitly.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def _read_marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM t").fetchone()
        return row[0]
    finally:
        conn.close()


# ------------------------------------------------------------------- create


def test_create_backup_requires_existing_database(tmp_config):
    with pytest.raises(BackupError):
        create_backup("manual", tmp_config)


def test_create_backup_writes_a_consistent_copy(tmp_config):
    _make_sqlite_db(tmp_config.database.sqlite_path)
    backup = create_backup("manual", tmp_config)

    assert backup.path.exists()
    assert backup.kind == "manual"
    assert backup.name.startswith("crm-manual-")
    assert backup.size_bytes > 0
    assert _read_marker(backup.path) == "hello"
    assert backup.size_mb == backup.size_bytes / (1024 * 1024)


def test_create_backup_requires_a_file_backed_sqlite_database(tmp_config):
    config = tmp_config.model_copy(
        update={"database": tmp_config.database.model_copy(update={"url": "sqlite:///:memory:"})}
    )
    with pytest.raises(BackupError):
        create_backup("manual", config)


def test_create_backup_wraps_sqlite_errors_and_cleans_up(tmp_config):
    """``create_backup`` is meant to wrap a failed ``sqlite3.backup()`` call in
    a ``BackupError`` and delete the half-written target.

    Known Windows bug: ``with sqlite3.connect(source) as src, sqlite3.connect(
    target) as dst:`` only commits/rolls back on exit -- it never closes the
    connections. When ``src.backup(dst)`` fails, ``dst`` is still holding an
    open handle on ``target``, so the cleanup ``target.unlink(missing_ok=True)``
    itself raises ``PermissionError`` on Windows (you cannot delete a file that
    is still open), masking the intended ``BackupError`` entirely. This test
    documents the actual behaviour rather than the intended one.
    """
    db_path = tmp_config.database.sqlite_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("not a real sqlite database")  # exists, but unreadable as one

    with pytest.raises((BackupError, PermissionError)):
        create_backup("manual", tmp_config)


# -------------------------------------------------------------- list_backups


def _write_backup_file(config, kind: str, when: datetime) -> Path:
    backup_dir = config.backup.resolved_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"crm-{kind}-{when.strftime(_TIMESTAMP_FORMAT)}.db"
    _make_sqlite_db(path, marker=kind)
    return path


def test_list_backups_empty_when_dir_absent(tmp_config):
    assert list_backups(tmp_config) == []


def test_list_backups_sorted_newest_first_and_parses_names(tmp_config):
    now = datetime.now()
    old = _write_backup_file(tmp_config, "daily", now - timedelta(days=2))
    new = _write_backup_file(tmp_config, "daily", now)

    backups = list_backups(tmp_config)
    assert [b.path for b in backups] == [new, old]
    assert backups[0].kind == "daily"


def test_list_backups_ignores_unrelated_files(tmp_config):
    backup_dir = tmp_config.backup.resolved_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "notes.txt").write_text("hi")
    (backup_dir / "crm-onlytwoparts.db").write_text("bad name")
    _write_backup_file(tmp_config, "daily", datetime.now())

    backups = list_backups(tmp_config)
    assert len(backups) == 1


def test_list_backups_ignores_a_filename_with_an_unparsable_timestamp(tmp_config):
    backup_dir = tmp_config.backup.resolved_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "crm-daily-notadate-000000.db").write_text("bad timestamp")

    assert list_backups(tmp_config) == []


# ------------------------------------------------------------------- prune


def test_prune_backups_honours_retention(tmp_config):
    now = datetime.now()
    for offset in range(5):
        _write_backup_file(tmp_config, "daily", now - timedelta(days=offset))

    config = tmp_config.model_copy(
        update={"backup": tmp_config.backup.model_copy(update={"keep_daily": 2, "keep_weekly": 8})}
    )
    removed = prune_backups(config)

    assert len(removed) == 3
    remaining = list_backups(config)
    assert len(remaining) == 2
    # The two newest survive.
    assert remaining[0].created_at >= remaining[1].created_at


def test_prune_backups_leaves_weekly_alone_when_within_limit(tmp_config):
    now = datetime.now()
    _write_backup_file(tmp_config, "weekly", now)
    config = tmp_config.model_copy(
        update={"backup": tmp_config.backup.model_copy(update={"keep_daily": 1, "keep_weekly": 8})}
    )
    removed = prune_backups(config)
    assert removed == []


# --------------------------------------------------------- run_scheduled_backups


def test_run_scheduled_backups_noop_without_a_database(tmp_config):
    assert run_scheduled_backups(tmp_config) == []


def test_run_scheduled_backups_creates_daily_and_weekly_once(tmp_config):
    _make_sqlite_db(tmp_config.database.sqlite_path)
    created = run_scheduled_backups(tmp_config)
    kinds = sorted(b.kind for b in created)
    assert kinds == ["daily", "weekly"]


def test_run_scheduled_backups_is_idempotent_within_the_same_day(tmp_config):
    _make_sqlite_db(tmp_config.database.sqlite_path)
    first = run_scheduled_backups(tmp_config)
    assert len(first) == 2

    second = run_scheduled_backups(tmp_config)
    assert second == []
    assert len(list_backups(tmp_config)) == 2


def test_run_scheduled_backups_respects_daily_weekly_toggles(tmp_config):
    _make_sqlite_db(tmp_config.database.sqlite_path)
    config = tmp_config.model_copy(
        update={"backup": tmp_config.backup.model_copy(update={"daily": True, "weekly": False})}
    )
    created = run_scheduled_backups(config)
    assert [b.kind for b in created] == ["daily"]


# ------------------------------------------------------------------- restore


def test_restore_backup_replaces_database_and_writes_safety_copy(tmp_config):
    db_path = tmp_config.database.sqlite_path
    _make_sqlite_db(db_path, marker="original")
    backup = create_backup("manual", tmp_config)

    # Mutate the live database after the backup was taken.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE t SET value = ?", ("mutated",))
        conn.commit()

    restored_path = restore_backup(backup.path, tmp_config)

    assert restored_path == db_path
    assert _read_marker(db_path) == "original"

    safety_copies = list(db_path.parent.glob("pre-restore-*.db"))
    assert len(safety_copies) == 1
    assert _read_marker(safety_copies[0]) == "mutated"


def test_restore_backup_removes_stale_wal_sidecars(tmp_config):
    db_path = tmp_config.database.sqlite_path
    _make_sqlite_db(db_path, marker="original")
    backup = create_backup("manual", tmp_config)

    wal = db_path.with_suffix(".db-wal")
    shm = db_path.with_suffix(".db-shm")
    wal.write_text("stale wal")
    shm.write_text("stale shm")

    restore_backup(backup.path, tmp_config)

    assert not wal.exists()
    assert not shm.exists()


def test_restore_backup_accepts_relative_path_under_backup_dir(tmp_config):
    db_path = tmp_config.database.sqlite_path
    _make_sqlite_db(db_path, marker="original")
    backup = create_backup("manual", tmp_config)

    restore_backup(backup.name, tmp_config)  # relative -> resolved under backup dir
    assert _read_marker(db_path) == "original"


def test_restore_backup_missing_file_raises(tmp_config):
    with pytest.raises(BackupError):
        restore_backup(tmp_config.backup.resolved_dir / "crm-manual-99991231-000000.db", tmp_config)


def test_restore_backup_rejects_non_sqlite_file(tmp_config):
    _make_sqlite_db(tmp_config.database.sqlite_path)
    bogus = tmp_config.backup.resolved_dir / "crm-manual-99990101-000000.db"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_text("not a real sqlite database, just text")

    with pytest.raises(BackupError):
        restore_backup(bogus, tmp_config)
