"""刪除備份：路徑限制與錯誤處理。

備份是資料出事時唯一的退路，所以刪除它的那條路徑要特別小心。這裡在意的
不是「功能能不能用」，而是「不該被刪的東西不會被刪」。
"""

from __future__ import annotations

import pytest

from core.errors import BackupError
from database.backup import create_backup, delete_backup, list_backups


@pytest.fixture
def with_backup(db_session, patch_config):
    """建立一份真的備份，回傳設定與那份備份。"""
    backup = create_backup("manual", patch_config)
    return patch_config, backup


def test_delete_removes_the_file_and_the_listing(with_backup):
    config, backup = with_backup
    assert backup.path.is_file()

    delete_backup(backup.name, config)

    assert not backup.path.exists()
    assert backup.name not in [b.name for b in list_backups(config)]


def test_deleting_something_that_is_not_there_is_an_error_not_a_silent_pass(with_backup):
    """靜默成功會讓使用者以為刪掉了，實際上檔案還在硬碟上。"""
    config, _ = with_backup
    with pytest.raises(BackupError, match="找不到"):
        delete_backup("crm-manual-19990101-000000.db", config)


@pytest.mark.parametrize(
    "dangerous",
    [
        "../../.env",
        "..\\..\\config.yaml",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
    ],
)
def test_path_traversal_cannot_reach_outside_the_backup_folder(with_backup, dangerous):
    """輸入雖然來自介面上被選取的那一列，但「介面只會傳合法的值」不是可以
    依賴的假設——把不受限的路徑交給 unlink() 的後果太嚴重。"""
    config, _ = with_backup
    # 一律只取檔名的部分，所以要嘛因為「不在備份資料夾」被擋，要嘛因為
    # 「找不到那個檔名」被擋。兩種都不會刪到備份資料夾以外的東西。
    with pytest.raises(BackupError):
        delete_backup(dangerous, config)


def test_deleting_one_backup_leaves_the_others_alone(db_session, patch_config):
    first = create_backup("manual", patch_config)
    import time

    time.sleep(1.1)          # 檔名精確到秒，同一秒建立會撞名
    second = create_backup("daily", patch_config)

    delete_backup(first.name, patch_config)

    remaining = [b.name for b in list_backups(patch_config)]
    assert second.name in remaining
    assert first.name not in remaining
