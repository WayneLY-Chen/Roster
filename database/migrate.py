"""Additive schema migrations for an existing database.

``Base.metadata.create_all`` creates missing *tables* but never touches a table
that already exists -- so a column added to a model after the user's database
was created is silently absent, and the first query against it fails.

This module closes that gap for the only kind of change this project makes:
adding a nullable column with a default. It compares the model metadata against
``PRAGMA table_info`` and issues ``ALTER TABLE ... ADD COLUMN`` for what is
missing. Anything more involved (renames, type changes, drops) is deliberately
out of scope and reported rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from core.constants import LogCategory
from core.errors import DatabaseError
from core.logging_setup import get_logger
from database.models import Base

log = get_logger(LogCategory.DATABASE)


@dataclass
class MigrationReport:
    """What the migration did, so start-up can log it honestly."""

    added_columns: list[str] = field(default_factory=list)
    created_tables: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added_columns or self.created_tables)


def _sqlite_type(column) -> str:
    """Render a column's type for an ALTER TABLE statement."""
    try:
        return column.type.compile(dialect=None) if column.type else "TEXT"
    except Exception:
        # Some types need a dialect to compile; SQLite is forgiving about
        # declared types, so a sensible textual fallback is fine.
        return {
            "INTEGER": "INTEGER",
            "BOOLEAN": "BOOLEAN",
            "DATETIME": "DATETIME",
            "DATE": "DATE",
        }.get(type(column.type).__name__.upper(), "TEXT")


def _default_clause(column) -> str:
    """SQL default for a new column, or an empty string when there is none."""
    default = column.default
    if default is None or getattr(default, "is_callable", False):
        # A Python-side callable (e.g. ``now``) cannot be expressed in DDL;
        # existing rows get NULL and the application fills it on next write.
        return ""
    value = getattr(default, "arg", None)
    if value is None or callable(value):
        return ""
    if isinstance(value, bool):
        return f" DEFAULT {1 if value else 0}"
    if isinstance(value, (int, float)):
        return f" DEFAULT {value}"
    escaped = str(value).replace("'", "''")
    return f" DEFAULT '{escaped}'"


def migrate(engine: Engine) -> MigrationReport:
    """Bring an existing database up to the current models. Idempotent."""
    report = MigrationReport()

    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        with engine.begin() as connection:
            for table_name, table in Base.metadata.tables.items():
                if table_name not in existing_tables:
                    # create_all (run separately) handles brand-new tables.
                    report.created_tables.append(table_name)
                    continue

                present = {c["name"] for c in inspector.get_columns(table_name)}
                for column in table.columns:
                    if column.name in present:
                        continue
                    if not column.nullable and column.default is None:
                        # SQLite cannot add a NOT NULL column without a default
                        # to a populated table; say so instead of failing later.
                        report.skipped.append(f"{table_name}.{column.name}")
                        log.warning(
                            "cannot add NOT NULL column {}.{} without a default; "
                            "recreate the database or add it by hand",
                            table_name, column.name,
                        )
                        continue

                    ddl = (
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" '
                        f"{_sqlite_type(column)}{_default_clause(column)}"
                    )
                    connection.execute(text(ddl))
                    report.added_columns.append(f"{table_name}.{column.name}")
                    log.info("added column {}.{}", table_name, column.name)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"schema migration failed: {exc}") from exc

    if report.added_columns:
        log.info("migration added {} column(s)", len(report.added_columns))
    return report
