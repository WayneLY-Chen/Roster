"""Tests for the additive schema migration.

The scenario that matters: a database created by an older version of the app,
opened by a newer one whose models have extra columns. ``create_all`` alone
leaves those columns missing, and every later query fails.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import Column, Integer, String, inspect, text

from core.errors import DatabaseError
from database.migrate import MigrationReport, migrate
from database.models import Base, Company
from database.session import create_db_engine


@pytest.fixture
def legacy_engine(tmp_path, tmp_config):
    """An engine over a database missing the newest Company columns."""
    db_path = tmp_path / "legacy.db"
    engine = create_db_engine(tmp_config, url=f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)

    # Simulate the old schema by dropping columns added after release. SQLite
    # refuses to drop a column an index still references, so those go first.
    with engine.begin() as connection:
        for index in ("ix_companies_do_not_contact", "ix_companies_last_emailed_at"):
            connection.execute(text(f'DROP INDEX IF EXISTS "{index}"'))
        for column in ("do_not_contact", "last_emailed_at", "email_count"):
            connection.execute(text(f'ALTER TABLE companies DROP COLUMN "{column}"'))
    return engine


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_adds_missing_columns(legacy_engine):
    assert "do_not_contact" not in _columns(legacy_engine, "companies")

    report = migrate(legacy_engine)

    assert report.changed
    added = set(report.added_columns)
    assert "companies.do_not_contact" in added
    assert "companies.last_emailed_at" in added
    assert "companies.email_count" in added
    assert {"do_not_contact", "last_emailed_at", "email_count"} <= _columns(
        legacy_engine, "companies"
    )


def test_is_idempotent(legacy_engine):
    migrate(legacy_engine)
    second = migrate(legacy_engine)

    assert second.added_columns == []
    assert not second.changed


def test_preserves_existing_rows(legacy_engine):
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO companies (company_name, name_key, dedupe_key, status, "
                "pipeline_stage, priority, email_verdict, created_at, updated_at) "
                "VALUES ('舊資料公司', '舊資料公司', 'n:舊資料公司', 'Active', "
                "'New', 'Medium', 'Unknown', '2020-01-01', '2020-01-01')"
            )
        )

    migrate(legacy_engine)

    with legacy_engine.connect() as connection:
        row = connection.execute(
            text("SELECT company_name, do_not_contact FROM companies")
        ).one()
    assert row[0] == "舊資料公司"
    # The DDL default applies to rows inserted before the column existed.
    assert row[1] in (0, False, None)


def test_defaults_are_applied_to_new_column(legacy_engine):
    migrate(legacy_engine)
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO companies (company_name, name_key, dedupe_key, status, "
                "pipeline_stage, priority, email_verdict, created_at, updated_at) "
                "VALUES ('新公司', '新公司', 'n:新公司', 'Active', 'New', 'Medium', "
                "'Unknown', '2026-01-01', '2026-01-01')"
            )
        )
        value = connection.execute(
            text("SELECT do_not_contact FROM companies WHERE company_name='新公司'")
        ).scalar_one()
    assert value in (0, False)


def test_reports_tables_that_create_all_must_make(tmp_path, tmp_config):
    """A table absent entirely is reported, not ALTERed."""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    engine = create_db_engine(tmp_config, url=f"sqlite:///{db_path.as_posix()}")

    report = migrate(engine)

    assert "companies" in report.created_tables
    assert report.added_columns == []


def test_missing_table_then_create_all_then_migrate(tmp_path, tmp_config):
    """The real start-up order: create_all first, migrate second."""
    db_path = tmp_path / "fresh.db"
    engine = create_db_engine(tmp_config, url=f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)

    report = migrate(engine)

    assert report.added_columns == []
    assert report.skipped == []


def test_wraps_database_errors(monkeypatch, legacy_engine):
    """A failing ALTER surfaces as DatabaseError, not a raw SQLAlchemy error."""
    from sqlalchemy.exc import OperationalError

    def boom(*_args, **_kwargs):
        raise OperationalError("ALTER", {}, Exception("disk is full"))

    monkeypatch.setattr("database.migrate.inspect", boom)

    with pytest.raises(DatabaseError):
        migrate(legacy_engine)


def test_report_changed_flag():
    assert not MigrationReport().changed
    assert MigrationReport(added_columns=["a.b"]).changed
    assert MigrationReport(created_tables=["t"]).changed


def test_migrated_database_is_usable(legacy_engine):
    """End-to-end: after migrating, the ORM can write the new columns."""
    from sqlalchemy.orm import sessionmaker

    migrate(legacy_engine)
    session = sessionmaker(bind=legacy_engine)()
    try:
        company = Company(
            company_name="測試公司",
            name_key="測試公司",
            dedupe_key="n:測試公司",
            do_not_contact=True,
            email_count=3,
        )
        session.add(company)
        session.commit()

        stored = session.query(Company).filter_by(company_name="測試公司").one()
        assert stored.do_not_contact is True
        assert stored.email_count == 3
    finally:
        session.close()
