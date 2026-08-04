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
    list_stored,
    load_for_sending,
    remove,
    resolve,
    safe_name,
    store,
)


@pytest.fixture
def attach_config(tmp_config, tmp_path):
    """把附件資料夾指到臨時目錄，並把上限調小方便測。"""
    return tmp_config.model_copy(
        update={
            "mailer": tmp_config.mailer.model_copy(
                update={
                    "attachments_dir": str(tmp_path / "attachments"),
                    "max_attachment_bytes": 1024,       # 1 KB，測試好寫
                }
            )
        }
    )


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
