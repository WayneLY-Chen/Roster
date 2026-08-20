"""「有新版本，要更新嗎？」的那個視窗。

自動檢查（開程式時）與手動檢查（「更新資訊」頁的按鈕）走的是同一段程式，
差別只有兩個：自動的一天最多一次，而且**沒有新版時完全不出聲**——每次開程式
都跳一個「已經是最新版」是騷擾，不是服務。

## 為什麼更新完一定要重新啟動

Python 把模組讀進記憶體之後就不會再看檔案了。更新換掉的是磁碟上的
``.py``，正在跑的這個程式手上仍然是舊的那一份。硬要熱載入的話會變成一半新
一半舊，那種狀態產生的錯誤沒有人有辦法診斷。所以這裡只做一件誠實的事：更新
完就明說要重開。
"""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox, QWidget

from core.updates import UpdateCheck, UpdateResult, apply_update, check, due_for_check, remember_check
from gui_qt.tasks import BackgroundTask

#: 更新說明在對話框裡最多顯示幾個字。
#:
#: CHANGELOG 的一則可以很長（這個專案寫得特別詳細），整段塞進 QMessageBox 會
#: 變成一個佔滿螢幕、而且沒有捲軸的視窗。這裡只要讓人判斷「要不要更新」，
#: 細節在「更新資訊」頁。
NOTES_LIMIT = 700


class UpdateHelper(QObject):
    """檢查更新、問使用者、執行更新。

    ``parent`` 要是一個 widget——對話框需要一個母視窗才會出現在正確的位置，
    而且才不會被主視窗蓋住。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._widget = parent
        self._manual = False
        self._on_finished = None

        self.check_task = BackgroundTask(
            self, check, on_done=self._on_checked, on_error=self._on_check_error
        )
        self.update_task = BackgroundTask(
            self,
            apply_update,
            on_progress=self._on_update_progress,
            on_done=self._on_updated,
            on_error=self._on_update_error,
        )

    # --------------------------------------------------------------- 檢查

    def check_on_startup(self) -> None:
        """開程式時的自動檢查。沒到時間、或設定關掉了就什麼都不做。"""
        if not due_for_check():
            return
        self._manual = False
        remember_check()
        self.check_task.start()

    def check_now(self, on_finished=None) -> None:
        """使用者自己按的檢查。一定會有回應，即使是「已經最新」。"""
        if self.check_task.running:
            return
        self._manual = True
        self._on_finished = on_finished
        remember_check()
        self.check_task.start()

    def _finish(self) -> None:
        if self._on_finished:
            self._on_finished()
            self._on_finished = None

    def _on_checked(self, result: UpdateCheck) -> None:
        self._finish()

        if result.error:
            # 自動檢查連不上網路是很常見的（沒開網路、公司防火牆），不要為了
            # 這件事跳視窗打斷使用者。手動按的才回報。
            if self._manual:
                QMessageBox.warning(self._widget, "檢查更新", f"沒辦法檢查：\n\n{result.error}")
            return

        if not result.available:
            if self._manual:
                QMessageBox.information(
                    self._widget,
                    "檢查更新",
                    f"已經是最新版了（v{result.current}）。",
                )
            return

        self._ask_to_update(result)

    def _on_check_error(self, exc: Exception) -> None:
        self._finish()
        if self._manual:
            QMessageBox.warning(self._widget, "檢查更新", f"檢查時出錯：\n\n{exc}")

    # --------------------------------------------------------------- 詢問

    def _ask_to_update(self, result: UpdateCheck) -> None:
        notes = result.notes.strip()
        if len(notes) > NOTES_LIMIT:
            notes = notes[:NOTES_LIMIT].rstrip() + "\n\n（其餘見「更新資訊」頁）"

        if not result.can_update_in_place:
            # 用 ZIP 下載的人沒辦法在程式裡更新。與其給一顆按了會失敗的按鈕，
            # 不如直接告訴他要做什麼，以及哪些檔案是他自己的。
            QMessageBox.information(
                self._widget,
                f"有新版本 v{result.latest}",
                f"目前是 v{result.current}，最新是 v{result.latest}。\n\n"
                "這個資料夾不是用 git 下載的，沒辦法在程式裡直接更新。\n"
                "到 GitHub 下載新版之後，記得把下面這些搬過去——那是你的東西，"
                "新下載的裡面沒有：\n\n"
                "　data/（資料庫）、attachments/、output/、backups/、\n"
                "　user_settings.yaml、templates/mail/\n\n"
                f"{notes}",
            )
            return

        box = QMessageBox(self._widget)
        box.setWindowTitle(f"有新版本 v{result.latest}")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"目前是 v{result.current}，最新是 v{result.latest}。要現在更新嗎？")
        box.setInformativeText(
            "你的公司資料、附件、設定都不會被動到。更新完需要重新啟動程式。"
        )
        if notes:
            box.setDetailedText(notes)
        update_button = box.addButton("更新", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍後", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(update_button)
        box.exec()

        if box.clickedButton() is update_button:
            self._start_update()

    # --------------------------------------------------------------- 執行

    def _start_update(self) -> None:
        if self.update_task.running:
            return
        self._progress = QMessageBox(self._widget)
        self._progress.setWindowTitle("更新中")
        self._progress.setIcon(QMessageBox.Icon.Information)
        self._progress.setText("正在更新，請稍候…")
        self._progress.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self._progress.show()
        self.update_task.start()

    def _on_update_progress(self, message: object) -> None:
        if getattr(self, "_progress", None):
            self._progress.setText(f"正在更新…\n\n{message}")

    def _close_progress(self) -> None:
        progress = getattr(self, "_progress", None)
        if progress is not None:
            progress.close()
            self._progress = None

    def _on_updated(self, result: UpdateResult) -> None:
        self._close_progress()
        if not result.ok:
            QMessageBox.warning(self._widget, "更新沒有完成", result.message)
            return
        version = f"v{result.version}" if result.version else "新版本"
        QMessageBox.information(
            self._widget,
            "更新完成",
            f"已經更新到 {version}。\n\n"
            "**請關掉程式再重新開啟**，新版本才會生效——正在執行的這一份仍然是"
            "更新前的程式碼。",
        )

    def _on_update_error(self, exc: Exception) -> None:
        self._close_progress()
        QMessageBox.warning(self._widget, "更新沒有完成", f"更新時出錯：\n\n{exc}")
