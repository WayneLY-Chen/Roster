"""Engine and session management.

One engine per process, created lazily. SQLite gets ``PRAGMA foreign_keys=ON``
(off by default, and without it the ``ondelete="CASCADE"`` rules are ignored)
and WAL mode so the GUI can read while a crawl thread writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import DatabaseError
from core.logging_setup import get_logger
from database.models import Base

log = get_logger(LogCategory.DATABASE)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def create_db_engine(config: AppConfig | None = None, url: str | None = None) -> Engine:
    """Build a fresh engine. Callers usually want :func:`get_engine` instead."""
    config = config or get_config()
    target_url = url or config.database.resolved_url

    if target_url.startswith("sqlite:///"):
        db_path = Path(target_url[len("sqlite:///") :])
        db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        target_url,
        echo=config.database.echo,
        future=True,
        # A GUI worker thread hands sessions back to the main thread's models.
        connect_args={"check_same_thread": False} if target_url.startswith("sqlite") else {},
    )
    if target_url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def get_engine() -> Engine:
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
        log.debug("engine created for {}", _engine.url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _session_factory


def init_db(engine: Engine | None = None) -> None:
    """Create missing tables, add missing columns, reconcile encryption.

    ``create_all`` alone is not enough once the app has shipped: it ignores
    tables that already exist, so a column added later would never appear in a
    user's database. :func:`database.migrate.migrate` fills that gap.

    The encryption pass has to run here too, not lazily. Encryption is
    deterministic, so ``WHERE email = ?`` binds ciphertext -- rows still stored
    in clear would simply stop matching, and the next crawl would happily
    insert every one of them again as "new".
    """
    engine = engine or get_engine()
    try:
        Base.metadata.create_all(engine)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not initialise the database: {exc}") from exc

    from database.migrate import migrate

    report = migrate(engine)
    if report.changed:
        log.info(
            "schema updated: {} new column(s)", len(report.added_columns)
        )

    from database.encryption import apply as apply_encryption

    conversion = apply_encryption(engine)
    if conversion.changed:
        log.info(
            "encryption reconciled: {} encrypted, {} decrypted",
            conversion.encrypted,
            conversion.decrypted,
        )
    log.info("database ready at {}", engine.url)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception."""
    factory = factory or get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        log.error("transaction rolled back: {}", exc)
        raise DatabaseError(str(exc)) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Dispose the cached engine and factory. Used by tests and DB switching."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
