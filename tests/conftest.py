"""Shared fixtures for the test suite.

Nothing here touches the network or writes outside ``tmp_path``. Every fixture
that pokes at process-wide caches (``core.config.get_config``,
``database.session``'s module-level engine) resets them on teardown so tests
stay independent of run order.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

import core.config as config_module
import database.session as session_module
from core.config import AppConfig
from core.constants import EmailVerdict, RecordStatus
from core.schemas import CleanCompany, RawCompany
from database.models import Base


@pytest.fixture(scope="session", autouse=True)
def _preload_worker_modules() -> None:
    """Import the worker-thread modules once, on the main thread, up front.

    Without this the suite aborts at random: the controllers import their
    heavy dependencies lazily, so the first import happens on a
    ``QThreadPool`` thread, where PySide6's import hook calls
    ``inspect.getsource`` and kills the process outright -- no exception, no
    traceback pytest can catch, just a missing run. See ``core/preload.py``
    for the captured stack. The app does the same thing in ``run_gui_qt``.
    """
    from core.preload import preload

    preload()


@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    """An :class:`AppConfig` rooted entirely under ``tmp_path``.

    ``verifier.check_mx`` is off so nothing in the suite ever performs a DNS
    lookup. ``crawler.sources`` includes the offline "sample" source so
    pipeline tests can resolve it by name.
    """
    db_file = tmp_path / "data" / "crm.db"
    log_dir = tmp_path / "logs"
    output_dir = tmp_path / "output"
    backup_dir = tmp_path / "backups"

    return AppConfig.model_validate(
        {
            "database": {"url": f"sqlite:///{db_file.as_posix()}", "echo": False},
            "logging": {"dir": str(log_dir), "console": False},
            "exporter": {"output_dir": str(output_dir)},
            "backup": {"dir": str(backup_dir), "keep_daily": 3, "keep_weekly": 2},
            "verifier": {
                "check_mx": False,
                "reject_disposable": True,
                "disposable_domains": [
                    "mailinator.com",
                    "10minutemail.com",
                    "guerrillamail.com",
                    "tempmail.com",
                    "yopmail.com",
                ],
            },
            "crawler": {
                # No real politeness delay in tests -- nothing here talks to a
                # real server, and a 2s-per-request default would make the
                # suite slow for no benefit.
                "delay_seconds": 0.0,
                "delay_jitter": 0.0,
                "max_retries": 0,
                "sources": [
                    {"name": "sample", "type": "sample", "enabled": True},
                ],
            },
        }
    )


@pytest.fixture
def patch_config(tmp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """Route every ``get_config()`` call (anywhere in the app) to ``tmp_config``.

    ``get_config`` is a single ``lru_cache``-wrapped callable shared by every
    module that imported it, so patching the private ``load_config`` it calls
    on a cache miss -- and clearing the cache -- affects the whole process
    without having to patch each module's own ``get_config`` reference.
    """
    monkeypatch.setattr(config_module, "load_config", lambda path=None: tmp_config)
    config_module.reset_config()
    yield tmp_config
    config_module.reset_config()


@pytest.fixture
def db_session(patch_config: AppConfig):
    """A session against a fresh schema, also wired up as the process engine.

    Wiring it up as the global engine lets code that opens its own session via
    :func:`database.session.session_scope` (repositories, the crawl pipeline,
    the exporter/importer, gmail harvesting) share the same database as
    assertions made directly through this fixture's session.
    """
    engine = session_module.create_db_engine(patch_config, url=patch_config.database.resolved_url)
    Base.metadata.create_all(engine)

    session_module._engine = engine
    session_module._session_factory = sessionmaker(
        bind=engine, expire_on_commit=False, future=True
    )

    session = session_module._session_factory()
    try:
        yield session
    finally:
        session.close()
        session_module.reset_engine()


class _MemoryKeyring:
    """Stand-in backend. ``credentials.keyring_available`` rejects any backend
    whose class name contains "fail", so the name matters."""


@pytest.fixture
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """An in-memory replacement for the OS credential vault.

    ``tests/__init__`` disables the real vault process-wide, which is what
    keeps the suite from reading the developer's own Gmail password. Encryption
    still needs *a* vault to hold the master key, so this puts a fake one back
    for the duration of a single test -- fresh, and therefore with a fresh key.
    """
    import keyring

    from core import credentials, crypto
    from database import types

    store: dict[tuple[str, str], str] = {}

    monkeypatch.delenv(credentials.DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(keyring, "get_keyring", lambda: _MemoryKeyring())
    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get((s, n)))
    monkeypatch.setattr(
        keyring, "set_password", lambda s, n, v: store.__setitem__((s, n), v)
    )
    monkeypatch.setattr(keyring, "delete_password", lambda s, n: store.pop((s, n), None))

    crypto.reset_key_cache()
    types.reset_encryption_state()
    yield store
    crypto.reset_key_cache()
    types.reset_encryption_state()


@pytest.fixture
def encryption_on(patch_config: AppConfig, fake_vault) -> AppConfig:
    """Field encryption genuinely switched on, key held in :func:`fake_vault`."""
    from core import crypto

    assert patch_config.database.encrypt, "tmp_config should default to encrypted"
    assert crypto.available(), "encryption should be usable once a vault exists"
    return patch_config


@pytest.fixture
def sample_raw_companies() -> list[RawCompany]:
    """A handful of representative scraped records."""
    return [
        RawCompany(
            company_name="測試精密機械股份有限公司",
            tax_id="22099131",
            email="Sales@Test-Precision.com.TW",
            phone="+886-2-2723-1234",
            website="test-precision.com.tw",
            address="110台北市信義區松高路1號",
            industry="金屬加工",
            contact_person="王小明",
            source="unit-test",
            source_url="https://example.test/1",
        ),
        RawCompany(
            company_name="  ",
            source="unit-test",
        ),
    ]


@pytest.fixture
def sample_companies() -> list[CleanCompany]:
    """A couple of already-cleaned records, ready to be persisted directly."""
    return [
        CleanCompany(
            company_name="測試精密機械股份有限公司",
            name_key="測試精密機械",
            dedupe_key="tax:22099131",
            tax_id="22099131",
            email="sales@test-precision.com.tw",
            phone="02-27231234",
            website="https://test-precision.com.tw",
            address="台北市信義區松高路1號",
            industry="金屬加工",
            contact_person="王小明",
            source="unit-test",
            source_url="https://example.test/1",
            email_verdict=EmailVerdict.UNKNOWN,
            status=RecordStatus.ACTIVE,
        ),
        CleanCompany(
            company_name="第二測試有限公司",
            name_key="第二測試",
            dedupe_key="mail:info@second-test.tw",
            email="info@second-test.tw",
            source="unit-test",
            email_verdict=EmailVerdict.UNKNOWN,
            status=RecordStatus.ACTIVE,
        ),
    ]
