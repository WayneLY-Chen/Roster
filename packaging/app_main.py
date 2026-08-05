"""打包成 exe 時的進入點：直接開視窗。

`main.py` 是 Typer 命令列程式，沒給子指令時只會印出說明。把它當成 exe 的
進入點、又設定不顯示主控台的話，使用者點兩下之後畫面上什麼都不會發生——
說明文字送進了一個不存在的主控台。

所以視窗版另外用這支當進入點。命令列仍然可以用（見 命令列.bat）。
"""

from __future__ import annotations

import sys
import traceback


def main() -> int:
    try:
        # 打包版第一次執行時，把預設設定與範例樣板複製到 exe 旁邊，
        # 使用者才有東西可以改。從原始碼跑的話這行不做任何事。
        from core.config import ensure_user_files

        ensure_user_files()

        from gui_qt.app import run_gui_qt

        run_gui_qt()
        return 0
    except Exception:  # noqa: BLE001 - 這是最外層，沒有別人能接了
        # 打包後沒有主控台，例外訊息會消失得無影無蹤，使用者只會看到
        # 「點了沒反應」。至少寫成檔案，並且盡量彈一個視窗出來。
        detail = traceback.format_exc()
        try:
            from core.config import PROJECT_ROOT

            crash_file = PROJECT_ROOT / "crash.log"
            crash_file.write_text(detail, encoding="utf-8")
            hint = f"\n\n完整訊息已寫入：\n{crash_file}"
        except Exception:  # pragma: no cover - 連設定都載不起來時
            hint = ""

        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(
                None,
                "名單匠 Roster 啟動失敗",
                f"程式無法啟動。{hint}\n\n{detail[-1500:]}",
            )
        except Exception:  # pragma: no cover - Qt 本身就掛掉時
            print(detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
