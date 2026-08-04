"""信件附件：儲存、大小限制、路徑安全，以及真的有掛到信上。

附件是唯一一個「使用者從系統任意位置挑檔案」的入口，所以路徑安全在這裡
特別重要——附件名稱會被拿去組路徑，而它可能來自使用者手改過的設定或
資料庫。
"""

from __future__ import annotations

import pytest

from core.errors import GmailError
from gmail.attachments import (
    check_total_size,
    human_size,
    library,
    list_stored,
    load_for_sending,
    remove,
    resolve,
    safe_name,
    store,
)


@pytest.fixture
def attach_config(tmp_config, tmp_path, monkeypatch):
    """附件資料夾指到臨時目錄、上限調小，並讓 get_config() 也看到同一份。

    附件庫的索引存在資料庫，而模組內部是自己開 session 的（``session_scope``），
    那條路徑只認得 ``get_config()``——不把設定掛上去的話，測試會對著使用者
    真正的資料庫寫入。
    """
    import core.config as config_module

    config = tmp_config.model_copy(
        update={
            "mailer": tmp_config.mailer.model_copy(
                update={
                    "attachments_dir": str(tmp_path / "attachments"),
                    "max_attachment_bytes": 1024,       # 1 KB，測試好寫
                }
            )
        }
    )
    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    config_module.reset_config()
    yield config
    config_module.reset_config()


@pytest.fixture(autouse=True)
def _database(attach_config):
    """附件庫需要資料庫——索引就存在那裡。"""
    from sqlalchemy.orm import sessionmaker

    import database.session as session_module
    from database.models import Base

    engine = session_module.create_db_engine(
        attach_config, url=attach_config.database.resolved_url
    )
    Base.metadata.create_all(engine)
    session_module._engine = engine
    session_module._session_factory = sessionmaker(
        bind=engine, expire_on_commit=False, future=True
    )
    yield
    session_module.reset_engine()


def _file(tmp_path, name: str, size: int = 10):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


# ------------------------------------------------------------------ 儲存


def test_store_copies_the_file_into_the_attachments_folder(attach_config, tmp_path):
    source = _file(tmp_path, "型錄.pdf", 100)
    stored = store(source, attach_config)

    assert stored.name == "型錄.pdf"
    assert stored.size_bytes == 100
    assert stored.path.parent == attach_config.mailer.resolved_attachments_dir
    assert stored.path.read_bytes() == source.read_bytes()


def test_the_original_can_be_deleted_afterwards(attach_config, tmp_path):
    """複製而不是記路徑的整個理由：排程寄信是幾天後才真的寄。"""
    source = _file(tmp_path, "報價單.xlsx", 50)
    store(source, attach_config)
    source.unlink()

    loaded = load_for_sending(["報價單.xlsx"], attach_config)
    assert loaded[0][1] == b"x" * 50


def test_same_name_does_not_overwrite(attach_config, tmp_path):
    """兩個不同資料夾的同名檔案，加進來不能互相蓋掉。"""
    store(_file(tmp_path, "a.txt", 10), attach_config)

    other = tmp_path / "other"
    other.mkdir()
    stored = store(_file(other, "a.txt", 20), attach_config)

    assert stored.name == "a (2).txt"
    assert stored.size_bytes == 20
    assert {item.name for item in list_stored(attach_config)} == {"a.txt", "a (2).txt"}


def test_remove_deletes_the_file(attach_config, tmp_path):
    store(_file(tmp_path, "刪我.txt"), attach_config)
    remove("刪我.txt", attach_config)
    assert list_stored(attach_config) == []


def test_removing_a_missing_attachment_is_not_an_error(attach_config):
    remove("根本不存在.txt", attach_config)


# -------------------------------------------------------------- 大小限制


def test_a_file_over_the_limit_is_refused(attach_config, tmp_path):
    with pytest.raises(GmailError, match="超過"):
        store(_file(tmp_path, "太大.bin", 2048), attach_config)


def test_an_empty_file_is_refused(attach_config, tmp_path):
    with pytest.raises(GmailError, match="空檔案"):
        store(_file(tmp_path, "空的.txt", 0), attach_config)


def test_the_limit_applies_to_the_total_not_each_file(attach_config, tmp_path):
    """三個各自合法的檔案，加起來可能就寄不出去了。"""
    for index in range(3):
        store(_file(tmp_path, f"檔案{index}.bin", 400), attach_config)

    names = [f"檔案{index}.bin" for index in range(3)]
    with pytest.raises(GmailError, match="總共"):
        check_total_size(names, attach_config)


def test_total_size_reports_a_missing_file_before_sending_starts(attach_config, tmp_path):
    store(_file(tmp_path, "在.txt"), attach_config)
    with pytest.raises(GmailError, match="不在附件資料夾"):
        check_total_size(["在.txt", "不在.txt"], attach_config)


# -------------------------------------------------------------- 路徑安全


@pytest.mark.parametrize(
    "dangerous",
    ["../../.env", "..\\..\\secrets.txt", "/etc/passwd", "C:\\Windows\\win.ini"],
)
def test_path_traversal_cannot_escape_the_attachments_folder(attach_config, dangerous):
    """附件名稱來自可被手動編輯的地方，不能假設它乾淨。"""
    resolved = resolve(dangerous, attach_config)
    assert resolved.parent == attach_config.mailer.resolved_attachments_dir.resolve()


def test_safe_name_strips_separators_and_reserved_characters():
    assert "/" not in safe_name("a/b.txt")
    assert "\\" not in safe_name("a\\b.txt")
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("") == "attachment"
    assert safe_name("   ") == "attachment"


def test_safe_name_keeps_the_extension_when_shortening():
    long_name = "檔" * 300 + ".pdf"
    result = safe_name(long_name)
    assert result.endswith(".pdf")
    assert len(result) <= 120


# ------------------------------------------------------------ 顯示用大小


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (1536, "1.5 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_human_size(size, expected):
    assert human_size(size) == expected


def test_default_limit_is_twenty_megabytes(tmp_config):
    """Gmail 說 25MB，但那是 base64 編碼後的大小，原始檔要抓 20MB。"""
    assert tmp_config.mailer.max_attachment_bytes == 20 * 1024 * 1024


# ------------------------------------------------------------ 附件庫（DB）


def test_label_and_note_do_not_touch_the_file(attach_config, tmp_path):
    """顯示名稱是給人看的，改它不該動到檔案——檔名一旦改了，正在引用它的
    排程設定就會指向不存在的檔案。"""
    from gmail.attachments import get, update

    store(_file(tmp_path, "2026Q1_catalog_final_v3.pdf", 50), attach_config)
    update(
        "2026Q1_catalog_final_v3.pdf",
        attach_config,
        label="2026 春季型錄",
        note="舊版，別再寄了",
    )

    item = get("2026Q1_catalog_final_v3.pdf", attach_config)
    assert item.label == "2026 春季型錄"
    assert item.display_name == "2026 春季型錄"
    assert item.note == "舊版，別再寄了"
    # 檔案本身完全沒變。
    assert item.name == "2026Q1_catalog_final_v3.pdf"
    assert item.path.is_file()


def test_display_name_falls_back_to_the_filename(attach_config, tmp_path):
    store(_file(tmp_path, "型錄.pdf"), attach_config)
    assert library(attach_config)[0].display_name == "型錄.pdf"


def test_sync_adopts_files_dropped_into_the_folder_by_hand(attach_config, tmp_path):
    """使用者會直接開資料夾丟檔案進去，程式不能假設只有自己會動它。"""
    from gmail.attachments import attachments_dir, sync

    (attachments_dir(attach_config) / "手動放的.pdf").write_bytes(b"x" * 30)

    assert sync(attach_config) == 1
    names = [item.name for item in library(attach_config)]
    assert "手動放的.pdf" in names


def test_a_missing_file_keeps_its_record_and_is_flagged(attach_config, tmp_path):
    """檔案被搬走時保留紀錄——那一筆帶著使用者自己打的顯示名稱與備註，
    為了一個可能是暫時的狀況把它丟掉並不合理。"""
    from gmail.attachments import update

    store(_file(tmp_path, "會不見.pdf"), attach_config)
    update("會不見.pdf", attach_config, label="重要型錄")
    (attach_config.mailer.resolved_attachments_dir / "會不見.pdf").unlink()

    items = library(attach_config)
    assert len(items) == 1
    assert items[0].exists is False
    assert items[0].status_text == "檔案不見了"
    assert items[0].label == "重要型錄"      # 使用者輸入的東西沒有被丟掉


def test_sync_updates_the_size_when_a_file_is_replaced(attach_config, tmp_path):
    """同名覆蓋後大小要跟著更新，否則總量會算錯、上限就形同虛設。"""
    from gmail.attachments import attachments_dir, sync

    store(_file(tmp_path, "型錄.pdf", 100), attach_config)
    (attachments_dir(attach_config) / "型錄.pdf").write_bytes(b"y" * 700)
    sync(attach_config)

    assert library(attach_config)[0].size_bytes == 700


def test_mark_used_records_that_it_was_actually_sent(attach_config, tmp_path):
    """用來判斷哪些附件還在用、哪些可以清掉——只看「加入時間」看不出這件事。"""
    from gmail.attachments import get, mark_used

    store(_file(tmp_path, "型錄.pdf"), attach_config)
    assert get("型錄.pdf", attach_config).status_text == "尚未寄出"

    mark_used(["型錄.pdf"], attach_config)
    mark_used(["型錄.pdf"], attach_config)

    item = get("型錄.pdf", attach_config)
    assert item.use_count == 2
    assert item.last_used_at is not None
    assert item.status_text == "已寄出 2 次"


def test_removing_an_attachment_also_removes_its_record(attach_config, tmp_path):
    store(_file(tmp_path, "刪我.pdf"), attach_config)
    remove("刪我.pdf", attach_config)
    assert library(attach_config) == []


def test_an_attachment_used_by_the_schedule_is_reported(attach_config, tmp_path):
    """刪掉排程正在用的附件，後果是排程在半夜三點失敗而沒有人看到。"""
    from gmail.attachments import used_by_schedule

    store(_file(tmp_path, "月報附件.pdf"), attach_config)
    assert used_by_schedule("月報附件.pdf", attach_config) is False

    scheduled = attach_config.model_copy(
        update={
            "scheduler": attach_config.scheduler.model_copy(
                update={"mail_attachments": ["月報附件.pdf"]}
            )
        }
    )
    assert used_by_schedule("月報附件.pdf", scheduled) is True
    assert used_by_schedule("別的檔.pdf", scheduled) is False
