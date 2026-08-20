"""Shared fixtures for the test suite.

Nothing here touches the network or writes outside ``tmp_path``. Every fixture
that pokes at process-wide caches (``core.config.get_config``,
``database.session``'s module-level engine) resets them on teardown so tests
stay independent of run order.
"""

from __future__ import annotations

import gc
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

import core.config as config_module
import database.session as session_module
from core.config import AppConfig
from core.constants import EmailVerdict, RecordStatus
from core.schemas import CleanCompany, RawCompany
from database.models import Base

# ---------------------------------------------------------------------------
# 日誌一定要在任何測試跑起來之前就導離使用者的 logs/ 資料夾。
#
# 這不能用 fixture 做。每個模組在 **import 的當下**就執行
# ``log = get_logger(...)``，而 get_logger() 會呼叫 setup_logging()，讀的是
# **真實設定**——那比任何 fixture 都早。setup_logging() 又是 idempotent 的
# （``_configured`` 旗標），所以之後再怎麼換設定都不會重新安裝輸出目標，
# 整場測試就這樣寫進使用者的正式日誌裡。
#
# 實際後果：使用者打開「日誌」頁，看到滿滿的 pytest 堆疊與測試故意製造的
# 假錯誤（例如 RuntimeError("pipeline exploded")），完全分不出哪些是真的。
#
# 這裡在 conftest 被 import 的時候就用 force=True 把輸出目標換掉。conftest
# 早於所有測試模組被 import，所以測試期間的每一筆日誌都會落在暫存目錄。
# ---------------------------------------------------------------------------
_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="roster-tests-logs-"))


def _redirect_logging_to_a_temp_dir() -> None:
    from core.logging_setup import setup_logging

    setup_logging(
        AppConfig.model_validate(
            {"logging": {"dir": str(_TEST_LOG_DIR), "console": False}}
        ),
        force=True,
    )


_redirect_logging_to_a_temp_dir()


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


#: 排乾執行緒池要來回幾次。見 :func:`drain_qt_thread_pool`。
#:
#: 一次「等工作跑完 → 送出回呼」只擋得住一層。這支專案裡最長的鏈是兩層
#: （查詢完成 → ``_start_pending_if_any()`` 再開一次查詢），六次是給它的
#: 安全餘裕，而閒置時每一次的成本趨近於零。
_DRAIN_ROUNDS = 6


def drain_qt_thread_pool(timeout_ms: int = 10_000, rounds: int = _DRAIN_ROUNDS) -> None:
    """等 ``QThreadPool`` 借出去的執行緒全部跑完，**並且**把回呼送完。

    為什麼一定要做：``BackgroundTask`` 把工作丟進**全域**的執行緒池，那個池
    活得比任何一個測試都久。一個測試如果沒等它的工作結束就結束了，那條執行緒
    會繼續跑——而下一個測試的 fixture 在拆除時會 ``engine.dispose()``，把它
    正在用的 SQLite 連線關掉。之後那條執行緒再送一次 SQL 就是在用已經釋放的
    記憶體，直譯器直接以 access violation 收場，而且回報的位置是**後面某個
    測試**，跟真正的肇事者完全對不起來。

    ## 為什麼光 ``waitForDone()`` 不夠——整場測試偶爾當掉的真正原因

    ``waitForDone()`` 等的是**工作執行緒**，不是**結果**。worker 跑完之後
    ``succeeded`` 那個 signal 是跨執行緒發的，Qt 會把它排進 UI 執行緒的事件
    佇列等著送；這時候工作執行緒已經還回池子裡了，``waitForDone()`` 立刻就
    回來，而那個回呼**還躺在佇列裡沒有送出去**。

    測試結束，fixture 把引擎 ``dispose()`` 掉。那個回呼就這樣一直躺著，直到
    **後面某一個測試**呼叫 ``processEvents()``——GUI 測試幾乎每一個都會，那
    是等自己那份背景工作的標準寫法——順手把它送了出去。於是公司頁的
    ``_apply_result()`` 在一個跟它無關的測試中間被呼叫，而它最後一行是
    ``_start_pending_if_any()``：**再開一次查詢**。那次查詢對著已經
    ``dispose()`` 掉的引擎要一條新連線，在 ``PRAGMA journal_mode=WAL`` 上
    access violation，整個直譯器當場結束。

    實際抓到的堆疊（``-X faulthandler``）就是這個樣子：崩潰的執行緒是
    ``gui_qt/tasks.py`` → ``pages/companies.py:_fetch`` → SQLAlchemy 開新連線
    → ``database/session.py:_on_connect``，而主執行緒當時正停在
    ``tests/test_gui_qt_logs.py`` 的 ``_wait_for``（也就是 ``processEvents()``）。
    兩個檔案之間毫無關係——這正是這個 flake 幾年來都對不出肇事者的原因。

    所以排乾必須是「等工作 → 送回呼 → 再等工作」交替進行：送出去的回呼可能
    再開一個新工作，而那個新工作也得在**引擎還活著的現在**跑完，不能留到
    下一個測試。

    ## 中間那個 ``gc.collect()`` 不是保險，是修好這個 flake 的那一行

    光加上 ``processEvents()`` 沒有解決問題，只是把崩潰的位置從「後面某個
    測試」搬到這裡。真正的第二個原因是 **Python 的循環垃圾回收在 Qt 正在
    送事件的當下去刪 Qt 物件**。

    頁面物件之間幾乎全是循環參照（Qt 的 parent/child 一份、Python 的回呼
    closure 一份），所以它們只能靠循環回收器釋放，而循環回收是由「配置了
    多少物件」觸發的——時間點完全是隨機的。它一旦落在 ``processEvents()``
    裡面，PySide6 就會在 Qt 派送事件的途中把底層的 C++ QWidget 刪掉，那是
    重入式的解構，直譯器直接 access violation。這解釋了這個 flake 全部的
    特徵：位置隨機、跟改了什麼無關、測試越多越容易發生、而崩潰點永遠是某
    一個 ``processEvents()``。

    所以在**每個測試結束、而且不在任何 Qt 派送裡面**的這個時間點主動收一次，
    上一個測試留下的循環垃圾就會在安全的地方被釋放，不會累積到後面某次
    ``processEvents()`` 中間才引爆。

    實測（``tests/test_gui_qt_companies.py`` + ``_feedback`` + ``_import_page``
    + ``_logs`` 四個檔案一起跑，這是最短的可重現組合）：

        原本                                    6 / 30 崩潰
        只加 processEvents()                    位置變了，照崩
        把 _Signals 物件全部留著不回收          3 / 12 崩潰（假設不成立）
        整個 gc.disable()                       0 / 12 崩潰（指向 GC）
        每一圈都 gc.collect()                   0 / 24 崩潰（但慢十幾倍，見下）
        目前這個版本（收兩次）                  0 / 24 崩潰

    **不要因為「看起來像沒必要的效能負擔」就把這兩行拿掉。**

    ## 但也不要每一個測試都收——那個代價量過，是十幾倍

    一次完整的循環回收要掃過整個堆積，而這支測試在 SQLAlchemy 與 Qt 之間撐
    著一個很大的堆積，實測每次 50–200ms。一開始這裡是在上面那個迴圈裡每一圈
    都收一次，1502 個測試乘以六圈，整場從 100 秒變成二十幾分鐘。

    所以收兩次就好（進迴圈前一次、收尾一次），而且只有**真的建過 Qt 元件的
    測試**才收——判斷方式是那個測試有沒有要 ``qt_app`` 這個 fixture。沒有
    widget 就沒有那種循環垃圾，也就沒有這個崩潰。
    """
    try:
        from PySide6.QtCore import QCoreApplication, QThreadPool
    except Exception:      # pragma: no cover - 沒有 Qt 的環境
        return

    pool = QThreadPool.globalInstance()
    app = QCoreApplication.instance()
    if app is None:        # pragma: no cover - 沒建過 QApplication 的測試
        pool.waitForDone(timeout_ms)
        return

    if _TEST_USES_QT:
        gc.collect()
    for _ in range(rounds):
        pool.waitForDone(timeout_ms)
        # 這一行才是重點：把排隊中的 succeeded/failed/progress 送出去。
        app.processEvents()
    # 最後一定要以「等工作跑完」收尾——剛剛那次 processEvents() 可能又開了
    # 一個新工作，不能讓它活著離開這個測試。
    pool.waitForDone(timeout_ms)
    if _TEST_USES_QT:
        gc.collect()


#: 現在跑的這個測試有沒有建過 Qt 元件。由下面那個 autouse fixture 維護。
#:
#: 用模組層的旗標而不是把它當參數傳，是因為 :func:`drain_qt_thread_pool` 有
#: 兩個呼叫端（這裡與 ``db_session``），而 ``db_session`` 拿不到 ``request``
#: ——它比 autouse 那支晚建立、早拆除，正是需要一起保護的那一段。
_TEST_USES_QT = False


@pytest.fixture(autouse=True)
def _no_background_threads_leak_between_tests(request):
    """每一個測試結束都把執行緒池排乾，不管它有沒有用到資料庫。"""
    global _TEST_USES_QT
    # 建過 widget 的測試一定會要 ``qt_app``（每個 GUI 測試模組各自定義一份），
    # 所以這是「這個測試碰過 Qt 嗎」最準也最便宜的判斷。
    _TEST_USES_QT = "qt_app" in request.fixturenames
    try:
        yield
        drain_qt_thread_pool()
    finally:
        _TEST_USES_QT = False


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
    # 附件資料夾一定要一起導到 tmp_path。它的預設值 "./attachments" 是相對於
    # PROJECT_ROOT 解析的，漏掉的話測試會把檔案寫進使用者真正的附件資料夾。
    attachments_dir = tmp_path / "attachments"

    return AppConfig.model_validate(
        {
            "database": {"url": f"sqlite:///{db_file.as_posix()}", "echo": False},
            "logging": {"dir": str(log_dir), "console": False},
            "exporter": {"output_dir": str(output_dir)},
            "backup": {"dir": str(backup_dir), "keep_daily": 3, "keep_weekly": 2},
            "mailer": {"attachments_dir": str(attachments_dir)},
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
        # 先等背景執行緒收工，再拆引擎。順序不能反：dispose() 會把它們正在
        # 用的 SQLite 連線關掉，之後那條執行緒再送一次 SQL 就是在用已經釋放
        # 的記憶體。autouse 的排乾 fixture 不夠——finalizer 是後進先出，
        # db_session 比它晚建立，所以會比它先拆。
        drain_qt_thread_pool()
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


@pytest.fixture(autouse=True)
def _forget_ai_probes():
    """每個測試都從「還沒探測過任何 AI 供應商」開始。

    ``ai.provider`` 會把「Ollama 在不在」的探測結果快取十秒（沒有快取的話畫面
    會卡好幾秒，見那裡的說明）。那份快取是模組層級的，所以會跨測試留下來——
    一個「假裝 Ollama 有在跑」的測試跑完之後，下一個「假裝它沒在跑」的測試會
    讀到上一個留下的答案而失敗。

    失敗的方式還特別討厭：單獨跑那個檔案會過，跑整套才會壞。
    """
    from ai.provider import forget_probes

    forget_probes()
    yield
    forget_probes()
