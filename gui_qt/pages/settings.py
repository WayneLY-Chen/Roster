"""設定頁：設定總覽、加密狀態、Gmail 帳號、郵件寄送上限、使用條款、備份。

幾乎整頁都是唯讀的——這支程式是靠 ``config.yaml`` 加重新啟動來設定的，不是
靠這頁——例外是外觀（Qt 可以直接切換 QSS，不需要重開）、每日寄送上限與備份，
這幾個簡單到可以直接同步操作，不需要 :class:`~gui_qt.tasks.BackgroundTask`。

## 使用者回報的三個問題，Qt 版怎麼修

    1. 「gmail 帳號那邊也沒辦法設定」—— Tk 版五張卡片疊起來比視窗還高，
       沒有捲動機制，下面幾張直接被切在畫面外。Qt 版把整個內容區包進
       ``QScrollArea``（``setWidgetResizable(True)``），視窗多小都能捲得到。
    2. 標籤被按鈕排在同一列時被擠掉（例如設定檔路徑那行被「開啟設定資料夾」
       按鈕吃掉）—— Tk 沒有「按實際版面寬度換行」的標籤，只能用一個唯讀
       Textbox 繞過去。Qt 的 ``QLabel.setWordWrap(True)`` 本來就是照當下
       版面實際寬度換行，只要把按鈕放進同一個 ``QHBoxLayout``、標籤給
       stretch=1、按鈕給 stretch=0，兩者就不會互搶版面。
    3. 加密金鑰的匯出／匯入（``export_encryption_key``/``import_encryption_key``）
       ——資料能不能救回來的關鍵功能，完整移植，中文警告文字照抄 Tk 版。

## 為什麼整頁的讀取用一個 BackgroundTask，不是逐段同步查

這頁要顯示的狀態包含兩次系統憑證保管庫查詢（Gmail 帳號/密碼各一次）跟一次
資料庫加密狀態查詢——這些個別看都不像「長任務」，但保管庫的實際延遲取決於
作業系統與當下負載，不是穩定的個位數毫秒；為了不讓換頁延遲的驗收指標
（20ms）繫於這些外部呼叫的不確定性，做法跟儀表板一致：切頁先用「上一次看到
的資料」立刻重繪，查詢結果回來再局部更新——見 ``gui_qt/pages/dashboard.py``
開頭的說明，這裡不重複。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import legal
from core.credentials import SecretSource, SecretStatus
from core.errors import CRMError
from core.i18n import ALL_OPTION, STAGE_LABELS, stage_labels, to_value
from controllers.core import SettingsController

# gui.controllers_mail.MailController 是跟 gui.controllers 同一種東西：純資料層
# controller，沒有任何 Tk（或其他介面框架）相依，只是拆成了獨立檔案給郵件頁
# 用。這裡只用它讀/寫「每日寄送上限」，不 import 任何 gui/pages 或 gui/widgets
# 之類的 Tk 元件，不違反「不耦合 Tk」這條規則背後真正要防的事。
from controllers.mail import MailController
from gui_qt import theme
from gui_qt.pages.base import BasePage
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable, LabeledEntry, Section, WideComboBox, caption, inline_caption

#: 外觀選單：值對照 ``config.yaml`` 的 ``app.theme``（英文小寫），顯示用中文標籤。
APPEARANCE_MODES: tuple[str, ...] = ("system", "light", "dark")
APPEARANCE_LABELS: dict[str, str] = {"system": "系統", "light": "淺色", "dark": "深色"}
APPEARANCE_REVERSE: dict[str, str] = {v: k for k, v in APPEARANCE_LABELS.items()}

#: 排程的動作與頻率：鍵是寫進設定檔的值，值是介面上顯示的中文。
#: 順序就是下拉選單的順序，所以第一個要是最常用的。
SCHEDULE_ACTIONS: dict[str, str] = {
    "crawl": "只爬取",
    "send": "只寄信",
    "crawl_and_send": "先爬取，再把名單寄出去",
}
SCHEDULE_MODES: dict[str, str] = {
    "daily": "每天",
    "monthly": "每個月",
    "hourly": "每小時",
    "interval": "每隔一段時間",
}


def _key_for(mapping: dict[str, str], label: str) -> str:
    """從顯示標籤反查設定檔用的值。找不到就用第一個，不讓介面卡住。"""
    for key, text in mapping.items():
        if text == label:
            return key
    return next(iter(mapping))


def _fill_checklist(widget: QListWidget, names: list[str], checked: set[str]) -> None:
    """用勾選清單呈現「多選其中幾個」。"""
    widget.clear()
    for name in names:
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked
        )
        widget.addItem(item)


def _checked_values(widget: QListWidget) -> list[str]:
    return [
        widget.item(index).text()
        for index in range(widget.count())
        if widget.item(index).checkState() == Qt.CheckState.Checked
    ]


def _fill_options(combo: QComboBox, values: list[str], selected: str) -> None:
    """「全部」永遠是第一個選項，代表不限制。"""
    combo.clear()
    combo.addItem(ALL_OPTION)
    combo.addItems(values)
    combo.setCurrentText(selected if selected and selected in values else ALL_OPTION)


def _selected_option(combo: QComboBox) -> str:
    """選了「全部」就回空字串——設定檔用空字串表示不限制。"""
    text = combo.currentText().strip()
    return "" if text == ALL_OPTION else text


class SettingsPage(BasePage):
    title = "設定"
    icon = "⚙️"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = SettingsController()
        self.mail_controller = MailController()

        self._refresh_task = BackgroundTask(
            self, self._fetch, on_done=self._apply_refresh, on_error=self._handle_refresh_error
        )
        self.test_connection_task = BackgroundTask(
            self,
            self.controller.test_gmail_connection,
            on_done=self._on_test_connection_done,
            on_error=self._on_test_connection_error,
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title_label = QLabel("設定")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header.addWidget(title_label)
        header.addStretch(1)

        header.addWidget(caption("外觀"))
        self.appearance_combo = WideComboBox()
        self.appearance_combo.addItems([APPEARANCE_LABELS[mode] for mode in APPEARANCE_MODES])
        config_data = getattr(self.app, "config_data", None)
        initial_theme = getattr(getattr(config_data, "app", None), "theme", None) or "system"
        self.appearance_combo.setCurrentText(
            APPEARANCE_LABELS.get(initial_theme, APPEARANCE_LABELS["system"])
        )
        # 先設好初始值再接訊號：setCurrentText() 本身不該被當成一次使用者操作。
        self.appearance_combo.currentTextChanged.connect(self._on_appearance_changed)
        header.addWidget(self.appearance_combo)
        outer.addLayout(header)

        # 五張卡片疊起來比視窗還高：包進 QScrollArea，視窗多小都能捲得到，
        # 不會再讓 Gmail 帳號表單被切在畫面外。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 6, 0)
        self._body_layout.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._build_overview_section()
        self._build_encryption_section()
        self._build_gmail_section()
        self._build_mailer_section()
        self._build_scheduler_section()
        self._build_legal_section()
        self._build_backup_section()
        self._body_layout.addStretch(1)

    def _build_overview_section(self) -> None:
        section = Section("總覽")
        self.overview_table = DataTable(columns=[("key", "設定項目", 220), ("value", "數值", 440)])
        section.body_layout.addWidget(self.overview_table)

        config_row = QHBoxLayout()
        # QLabel + setWordWrap(True) + stretch=1，旁邊的按鈕 stretch=0：
        # 按鈕永遠保留自己需要的寬度，標籤把剩下的空間拿去換行，不會互搶。
        self.config_label = QLabel("")
        self.config_label.setObjectName("MutedLabel")
        self.config_label.setWordWrap(True)
        config_row.addWidget(self.config_label, 1)
        open_folder_button = QPushButton("開啟設定資料夾")
        open_folder_button.clicked.connect(self._open_config_folder)
        config_row.addWidget(open_folder_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(config_row)

        self._body_layout.addWidget(section)

    def _build_encryption_section(self) -> None:
        """個資欄位加密的狀態。

        這裡只顯示、不提供開關。開關牽動的是整個資料庫的重寫，那件事該在
        ``config.yaml`` 裡明確做出決定並重新啟動，而不是一個順手可以誤點的按鈕。
        """
        section = Section("資料庫加密")

        self.encryption_status_label = QLabel("")
        self.encryption_status_label.setWordWrap(True)
        section.body_layout.addWidget(self.encryption_status_label)

        key_row = QHBoxLayout()
        backup_key_button = QPushButton("備份金鑰")
        backup_key_button.clicked.connect(self._show_encryption_key)
        key_row.addWidget(backup_key_button)
        restore_key_button = QPushButton("還原金鑰")
        restore_key_button.clicked.connect(self._restore_encryption_key)
        key_row.addWidget(restore_key_button)
        key_row.addStretch(1)
        section.body_layout.addLayout(key_row)

        note = QLabel(
            "信箱、電話、地址、聯絡人姓名、備註與寄出的信件內容會以 AES-GCM 加密後才寫入"
            "資料庫；公司名稱、統一編號、產業別維持明文，搜尋才不會變慢。\n"
            "金鑰存放在系統憑證保管庫（Windows 認證管理員），不會出現在專案資料夾裡——"
            "因此把資料庫檔案複製走的人無法讀到任何個資。\n"
            "但金鑰也不會跟著資料庫一起被複製：請按「備份金鑰」把它抄下來收好，"
            "否則重灌或換電腦之後，連 backups/ 裡的備份都解不開。\n"
            "要改變加密設定請編輯 config.yaml 的 database.encrypt 後重新啟動，"
            "程式會自動轉換既有資料。"
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        self._body_layout.addWidget(section)

    def _build_gmail_section(self) -> None:
        section = Section("Gmail 帳號")

        self._keyring_ok = self.controller.keyring_available()
        if not self._keyring_ok:
            warning = QLabel(
                "此系統無法使用憑證保管庫，密碼無法安全儲存。請改用 .env 檔手動設定 "
                "GMAIL_ADDRESS 與 GMAIL_APP_PASSWORD（明文，僅建議在無圖形介面或測試"
                "環境使用）。"
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
            section.body_layout.addWidget(warning)

        self.gmail_status_label = QLabel("")
        self.gmail_status_label.setWordWrap(True)
        section.body_layout.addWidget(self.gmail_status_label)

        # 兩個輸入框各吃一半寬度，不要在後面加 addStretch()——那個彈簧會把
        # 多出來的空間全部吃掉，兩個框就只剩下最小寬度，右邊卻空著一大片。
        # 信箱與應用程式密碼都是會填滿整格的長字串，該給它們空間。
        form_row = QHBoxLayout()
        self.gmail_address_entry = LabeledEntry("Gmail 帳號", placeholder="you@gmail.com")
        form_row.addWidget(self.gmail_address_entry, 1)
        self.gmail_password_entry = LabeledEntry("應用程式密碼", placeholder="16 碼，不是你的帳號密碼")
        self.gmail_password_entry.entry.setEchoMode(QLineEdit.EchoMode.Password)
        form_row.addWidget(self.gmail_password_entry, 1)
        section.body_layout.addLayout(form_row)

        button_row = QHBoxLayout()
        self.save_credential_button = QPushButton("儲存到系統")
        self.save_credential_button.clicked.connect(self._save_gmail_credentials)
        if not self._keyring_ok:
            self.save_credential_button.setEnabled(False)
        button_row.addWidget(self.save_credential_button)
        clear_button = QPushButton("清除")
        clear_button.clicked.connect(self._clear_gmail_credentials)
        button_row.addWidget(clear_button)
        self.test_connection_button = QPushButton("測試連線")
        self.test_connection_button.clicked.connect(self._test_gmail_connection)
        button_row.addWidget(self.test_connection_button)
        button_row.addStretch(1)
        section.body_layout.addLayout(button_row)

        note = QLabel(
            "需先在 Google 帳戶開啟兩步驟驗證，再至 "
            "https://myaccount.google.com/apppasswords 產生應用程式密碼。密碼會存在"
            "作業系統的憑證保管庫（Windows 認證管理員／macOS 鑰匙圈／Linux Secret "
            "Service），專案資料夾中不會出現這筆密碼，因此整個資料夾可以安全地上傳到 git。"
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        self._body_layout.addWidget(section)

    def _build_mailer_section(self) -> None:
        """每日寄送上限：使用者實際上是在問「今日已寄這個，為什麼上限不是我要的
        數字？」——是一個他們會想當場調整的數字，不該逼他們去找設定檔、手動編輯、
        再重開程式。存檔走的是 :meth:`gui.controllers_mail.MailController.set_daily_limit`，
        失敗會整個回滾。
        """
        section = Section("郵件寄送")

        row = QHBoxLayout()
        self.daily_limit_entry = LabeledEntry("每日寄送上限")
        row.addWidget(self.daily_limit_entry)
        save_button = QPushButton("儲存")
        save_button.clicked.connect(self._save_daily_limit)
        row.addWidget(save_button, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        section.body_layout.addLayout(row)

        note = QLabel(
            "超過上限後「郵件」頁會停止繼續寄送，直到隔天重置（可設 1～2000）。"
            "Gmail 個人帳號本身約在每天 500 封左右就會開始擋信，把這裡設得比"
            "那個高並不會讓 Gmail 多寄。"
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        self._body_layout.addWidget(section)

    # ------------------------------------------------------------- 排程

    def _build_scheduler_section(self) -> None:
        """自動排程：什麼時候、做什麼、對誰做。

        欄位會依「動作」與「頻率」顯示或隱藏——一次把十幾個欄位全攤開，
        使用者得自己判斷哪些跟目前的選擇有關；只留相關的，選什麼看到什麼。
        """
        section = Section("自動排程")

        self.schedule_enabled_check = QCheckBox("啟用自動排程")
        self.schedule_enabled_check.toggled.connect(self._update_schedule_visibility)
        section.body_layout.addWidget(self.schedule_enabled_check)

        # --- 做什麼 ---
        action_row = QHBoxLayout()
        action_row.addWidget(caption("要做什麼"))
        self.schedule_action_combo = WideComboBox()
        for label in SCHEDULE_ACTIONS.values():
            self.schedule_action_combo.addItem(label)
        self.schedule_action_combo.currentTextChanged.connect(
            lambda _: self._update_schedule_visibility()
        )
        action_row.addWidget(self.schedule_action_combo, 1)
        action_row.addStretch(1)
        section.body_layout.addLayout(action_row)

        # --- 什麼時候 ---
        when_row = QHBoxLayout()
        when_row.addWidget(caption("多久一次"))
        self.schedule_mode_combo = WideComboBox()
        for label in SCHEDULE_MODES.values():
            self.schedule_mode_combo.addItem(label)
        self.schedule_mode_combo.currentTextChanged.connect(
            lambda _: self._update_schedule_visibility()
        )
        when_row.addWidget(self.schedule_mode_combo)

        self.schedule_day_label = caption("每月")
        when_row.addWidget(self.schedule_day_label)
        self.schedule_day_spin = QSpinBox()
        self.schedule_day_spin.setRange(1, 31)
        self.schedule_day_spin.setSuffix(" 號")
        theme.match_control_height(self.schedule_day_spin)
        when_row.addWidget(self.schedule_day_spin)

        self.schedule_at_label = caption("時間")
        when_row.addWidget(self.schedule_at_label)
        # 這裡用純 QLineEdit 而不是 LabeledEntry：後者會在輸入框上面多一行
        # 說明文字，在這種橫排的一列裡會把高度撐高、跟旁邊的元件對不齊。
        self.schedule_at_entry = QLineEdit()
        self.schedule_at_entry.setPlaceholderText("HH:MM")
        self.schedule_at_entry.setFixedWidth(theme.input_width(7))
        when_row.addWidget(self.schedule_at_entry)

        self.schedule_every_label = caption("每隔")
        when_row.addWidget(self.schedule_every_label)
        self.schedule_every_spin = QSpinBox()
        self.schedule_every_spin.setRange(15, 10_080)
        self.schedule_every_spin.setSuffix(" 分鐘")
        theme.match_control_height(self.schedule_every_spin)
        when_row.addWidget(self.schedule_every_spin)

        when_row.addStretch(1)
        section.body_layout.addLayout(when_row)

        self.schedule_day_hint = caption(
            "遇到沒有這一天的月份（例如二月的 31 號）會改在當月最後一天執行。"
        )
        section.body_layout.addWidget(self.schedule_day_hint)

        # --- 爬取設定 ---
        self.schedule_crawl_label = caption("要爬哪些來源（不勾＝全部已啟用的來源）")
        section.body_layout.addWidget(self.schedule_crawl_label)
        self.schedule_sources_list = QListWidget()
        self.schedule_sources_list.setFixedHeight(theme.text_box_height(4))
        section.body_layout.addWidget(self.schedule_sources_list)

        self.schedule_verify_check = QCheckBox("爬完後自動驗證信箱")
        section.body_layout.addWidget(self.schedule_verify_check)

        # --- 寄信設定 ---
        template_row = QHBoxLayout()
        self.schedule_template_label = caption("郵件樣板")
        template_row.addWidget(self.schedule_template_label)
        self.schedule_template_combo = WideComboBox()
        template_row.addWidget(self.schedule_template_combo, 1)
        template_row.addStretch(1)
        section.body_layout.addLayout(template_row)

        # 寄給誰。沒有這一段的話，排程就是「寄給資料庫裡每一家公司」，而且是
        # 在無人看顧的時候寄——那不是任何人想要的預設行為。欄位刻意跟「郵件」
        # 頁右邊那組篩選一致，使用者在兩個地方看到的是同一套概念。
        self.schedule_recipient_label = caption("寄給誰（留「全部」代表不限制）")
        section.body_layout.addWidget(self.schedule_recipient_label)

        recipient_row = QHBoxLayout()
        self.schedule_industry_combo = WideComboBox()
        recipient_row.addWidget(self.schedule_industry_combo, 1)
        self.schedule_stage_combo = WideComboBox()
        self.schedule_stage_combo.addItems(stage_labels(with_all=True))
        recipient_row.addWidget(self.schedule_stage_combo, 1)
        self.schedule_tag_combo = WideComboBox()
        recipient_row.addWidget(self.schedule_tag_combo, 1)
        section.body_layout.addLayout(recipient_row)

        self.schedule_verified_check = QCheckBox("只寄給已通過驗證的信箱")
        section.body_layout.addWidget(self.schedule_verified_check)

        campaign_row = QHBoxLayout()
        self.schedule_campaign_entry = LabeledEntry("活動名稱（會自動接上執行日期）")
        campaign_row.addWidget(self.schedule_campaign_entry, 1)
        self.schedule_batch_label = inline_caption("單次最多")
        campaign_row.addWidget(self.schedule_batch_label, 0, Qt.AlignmentFlag.AlignBottom)
        self.schedule_batch_spin = QSpinBox()
        self.schedule_batch_spin.setRange(1, 2000)
        self.schedule_batch_spin.setSuffix(" 封")
        theme.match_control_height(self.schedule_batch_spin)
        campaign_row.addWidget(self.schedule_batch_spin, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(campaign_row)

        self.schedule_attachment_label = caption("隨信附件")
        section.body_layout.addWidget(self.schedule_attachment_label)
        self.schedule_attachments_list = QListWidget()
        self.schedule_attachments_list.setFixedHeight(theme.text_box_height(3))
        section.body_layout.addWidget(self.schedule_attachments_list)

        # --- 共用 ---
        self.schedule_catchup_check = QCheckBox(
            "錯過的排程在下次開啟程式時補跑一次"
        )
        section.body_layout.addWidget(self.schedule_catchup_check)

        save_row = QHBoxLayout()
        self.schedule_status_label = QLabel("")
        self.schedule_status_label.setObjectName("MutedLabel")
        self.schedule_status_label.setWordWrap(True)
        save_row.addWidget(self.schedule_status_label, 1)
        save_schedule_button = QPushButton("儲存排程")
        save_schedule_button.clicked.connect(self._save_scheduler)
        save_row.addWidget(save_schedule_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(save_row)

        note = QLabel(
            "排程只在本程式開啟時執行——這是桌面程式，沒有背景服務。要無人值守"
            "請改用命令列的 python main.py schedule。"
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        self._body_layout.addWidget(section)

    def _update_schedule_visibility(self) -> None:
        """只顯示跟目前選擇有關的欄位。"""
        enabled = self.schedule_enabled_check.isChecked()
        action = _key_for(SCHEDULE_ACTIONS, self.schedule_action_combo.currentText())
        mode = _key_for(SCHEDULE_MODES, self.schedule_mode_combo.currentText())

        crawls = action in ("crawl", "crawl_and_send")
        sends = action in ("send", "crawl_and_send")

        for widget in (
            self.schedule_action_combo,
            self.schedule_mode_combo,
            self.schedule_catchup_check,
        ):
            widget.setEnabled(enabled)

        # 時間欄位：每天/每月要時間，每隔要分鐘數，每小時什麼都不用。
        wants_clock = mode in ("daily", "monthly")
        for widget in (self.schedule_at_label, self.schedule_at_entry):
            widget.setVisible(enabled and wants_clock)
        for widget in (self.schedule_day_label, self.schedule_day_spin, self.schedule_day_hint):
            widget.setVisible(enabled and mode == "monthly")
        for widget in (self.schedule_every_label, self.schedule_every_spin):
            widget.setVisible(enabled and mode == "interval")

        for widget in (
            self.schedule_crawl_label,
            self.schedule_sources_list,
            self.schedule_verify_check,
        ):
            widget.setVisible(enabled and crawls)

        for widget in (
            self.schedule_template_label,
            self.schedule_template_combo,
            self.schedule_recipient_label,
            self.schedule_industry_combo,
            self.schedule_stage_combo,
            self.schedule_tag_combo,
            self.schedule_verified_check,
            self.schedule_campaign_entry,
            self.schedule_batch_label,
            self.schedule_batch_spin,
            self.schedule_attachment_label,
            self.schedule_attachments_list,
        ):
            widget.setVisible(enabled and sends)

    def _load_scheduler(self) -> None:
        """把設定填進表單。"""
        try:
            values = self.controller.scheduler_settings()
        except CRMError as exc:
            self.report_error(exc)
            return

        self.schedule_enabled_check.setChecked(bool(values["enabled"]))
        self.schedule_action_combo.setCurrentText(
            SCHEDULE_ACTIONS.get(values["action"], SCHEDULE_ACTIONS["crawl"])
        )
        self.schedule_mode_combo.setCurrentText(
            SCHEDULE_MODES.get(values["mode"], SCHEDULE_MODES["daily"])
        )
        self.schedule_at_entry.setText(values["at"])
        self.schedule_every_spin.setValue(int(values["every_minutes"]))
        self.schedule_day_spin.setValue(int(values["day_of_month"]))
        self.schedule_verify_check.setChecked(bool(values["verify_after_crawl"]))
        self.schedule_catchup_check.setChecked(bool(values["catch_up"]))
        self.schedule_campaign_entry.set(values["mail_campaign"])
        self.schedule_batch_spin.setValue(int(values["mail_batch_limit"]))
        self.schedule_verified_check.setChecked(bool(values["mail_verified_only"]))

        # 產業與標籤是資料庫裡實際有的值，每次載入都重查——使用者剛爬完一批
        # 新資料，選單就該立刻看得到新的產業。
        _fill_options(
            self.schedule_industry_combo,
            self.controller.industry_options(),
            values["mail_industry"],
        )
        _fill_options(
            self.schedule_tag_combo,
            self.controller.tag_options(),
            values["mail_tag"],
        )
        stage = values["mail_stage"]
        self.schedule_stage_combo.setCurrentText(
            STAGE_LABELS.get(stage, ALL_OPTION) if stage else ALL_OPTION
        )

        _fill_checklist(
            self.schedule_sources_list,
            self.controller.crawl_source_names(),
            set(values["sources"]),
        )
        _fill_checklist(
            self.schedule_attachments_list,
            [item.name for item in self.mail_controller.attachments()],
            set(values["mail_attachments"]),
        )

        templates = self.controller.mail_template_names()
        self.schedule_template_combo.clear()
        self.schedule_template_combo.addItems(templates or ["（尚未建立任何樣板）"])
        if values["mail_template"] in templates:
            self.schedule_template_combo.setCurrentText(values["mail_template"])

        self.schedule_status_label.setText(self.controller.scheduler_next_run_text())
        self._update_schedule_visibility()

    def _save_scheduler(self) -> None:
        action = _key_for(SCHEDULE_ACTIONS, self.schedule_action_combo.currentText())
        mode = _key_for(SCHEDULE_MODES, self.schedule_mode_combo.currentText())

        values = {
            "enabled": self.schedule_enabled_check.isChecked(),
            "action": action,
            "mode": mode,
            "at": self.schedule_at_entry.text().strip() or "03:00",
            "every_minutes": self.schedule_every_spin.value(),
            "day_of_month": self.schedule_day_spin.value(),
            "sources": _checked_values(self.schedule_sources_list),
            "verify_after_crawl": self.schedule_verify_check.isChecked(),
            "catch_up": self.schedule_catchup_check.isChecked(),
            "mail_campaign": self.schedule_campaign_entry.get().strip() or "排程寄送",
            "mail_batch_limit": self.schedule_batch_spin.value(),
            "mail_attachments": _checked_values(self.schedule_attachments_list),
            "mail_industry": _selected_option(self.schedule_industry_combo),
            "mail_tag": _selected_option(self.schedule_tag_combo),
            "mail_stage": to_value(self.schedule_stage_combo.currentText(), STAGE_LABELS),
            "mail_verified_only": self.schedule_verified_check.isChecked(),
        }
        template = self.schedule_template_combo.currentText().strip()
        values["mail_template"] = (
            template if template in self.controller.mail_template_names() else ""
        )

        try:
            self.controller.save_scheduler_settings(values)
        except CRMError as exc:
            self.report_error(exc)
            return

        self.schedule_status_label.setText(self.controller.scheduler_next_run_text())
        self.status("排程設定已儲存，重新啟動程式後生效", "success")

    def _build_legal_section(self) -> None:
        """使用條款與免責聲明。文字與 README 共用 :mod:`core.legal`。"""
        section = Section(legal.TITLE)

        summary_label = QLabel(legal.SUMMARY)
        summary_label.setWordWrap(True)
        summary_label.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
        section.body_layout.addWidget(summary_label)

        terms_box = QPlainTextEdit()
        terms_box.setReadOnly(True)
        terms_box.setPlainText(legal.TERMS)
        terms_box.setFixedHeight(theme.text_box_height(9))
        section.body_layout.addWidget(terms_box)

        self._body_layout.addWidget(section)

    def _build_backup_section(self) -> None:
        section = Section("備份")
        self.backup_table = DataTable(
            columns=[
                ("name", "檔名", 260),
                ("kind", "類型", 90),
                ("created_at", "建立時間", 160),
                ("size_mb", "大小（MB）", 100),
            ]
        )
        section.body_layout.addWidget(self.backup_table)

        buttons = QHBoxLayout()
        create_button = QPushButton("立即建立備份")
        create_button.clicked.connect(self._create_backup)
        buttons.addWidget(create_button)
        restore_button = QPushButton("還原所選")
        restore_button.clicked.connect(self._restore_selected)
        buttons.addWidget(restore_button)
        delete_button = QPushButton("刪除所選")
        delete_button.clicked.connect(self._delete_selected_backup)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        section.body_layout.addLayout(buttons)

        note = QLabel(
            "每日與每週備份會依保留數量自動清理，這裡的刪除是給手動備份、"
            "或想立刻騰出空間時用的。"
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        self._body_layout.addWidget(section)

    # ------------------------------------------------------------- 生命週期

    def on_show(self, force: bool = False) -> None:
        # 這頁顯示的狀態（憑證保管庫、備份清單……）不是靠 bump_data_version()
        # 追蹤的那種「資料庫寫入」，每次顯示都該真的重查一次，理由跟儀表板一樣。
        super().on_show(force=True)

    def refresh(self) -> None:
        if self._refresh_task.running:
            return
        self._refresh_task.start()

    # ------------------------------------------------------------- 查詢（背景執行緒）

    def _fetch(self, *, report, cancel_event) -> dict[str, Any]:
        """在背景執行緒被呼叫；千萬不能在這裡碰任何 widget。"""
        mail_controller = MailController()  # 見 _refresh_mailer_settings 的舊註解：
        # 拿一份新的，才能反映剛剛在別處（或這頁自己）save_user_setting 之後的值。
        return {
            "summary": self.controller.summary(),
            "config_path": self.controller.config_path(),
            "encryption_status": self.controller.encryption_status(),
            "address_status": self.controller.credential_status("gmail_address"),
            "password_status": self.controller.credential_status("gmail_app_password"),
            "daily_limit": mail_controller.daily_limit(),
            "backups": self.controller.backups(),
        }

    def _apply_refresh(self, data: dict[str, Any]) -> None:
        if getattr(self.app, "current_page", None) != self.title:
            return  # 查詢跑的期間使用者已經切走了
        self._apply_overview(data["summary"], data["config_path"])
        self._apply_encryption_status(data["encryption_status"])
        self._apply_gmail_status(data["address_status"], data["password_status"])
        self.daily_limit_entry.set(str(data["daily_limit"]))
        self._apply_backups(data["backups"])
        # 排程表單在 UI 執行緒直接讀設定就好——都是已經載入的設定值與檔名
        # 清單，沒有資料庫查詢，不值得再多開一個背景工作。
        self._load_scheduler()

    def _handle_refresh_error(self, exc: Exception) -> None:
        self.report_error(exc)

    # ------------------------------------------------------------- 套用結果（UI 執行緒）

    def _apply_overview(self, summary: dict[str, str], config_path: Path) -> None:
        self.overview_table.set_rows([{"key": key, "value": value} for key, value in summary.items()])
        self.config_label.setText(f"設定值於 {config_path} 中編輯，重新啟動應用程式後才會生效。")

    def _apply_encryption_status(self, report: Any) -> None:
        lines = [f"狀態：{report.describe()}"]
        if report.active:
            lines.append(f"已加密的欄位值：{report.encrypted_values} 個")
        if not report.fully_converted:
            lines.append(f"尚有 {report.pending} 個欄位值與設定不一致，請重新啟動程式完成轉換。")
        self.encryption_status_label.setText("\n".join(lines))
        color = theme.MUTED if (report.active and report.fully_converted) else theme.DANGER
        self.encryption_status_label.setStyleSheet(f"color: {theme.pick(color)};")

    def _apply_gmail_status(self, address_status: SecretStatus, password_status: SecretStatus) -> None:
        def _line(field_label: str, status: SecretStatus) -> str:
            text = f"{field_label}：{status.source.value}"
            if status.hint:
                text += f"　{status.hint}"
            return text

        lines = [_line("Gmail 帳號", address_status), _line("應用程式密碼", password_status)]
        self.gmail_status_label.setText("\n".join(lines))
        any_in_env = (address_status.is_set and not address_status.is_secure) or (
            password_status.is_set and not password_status.is_secure
        )
        color = theme.DANGER if any_in_env else theme.MUTED
        self.gmail_status_label.setStyleSheet(f"color: {theme.pick(color)};")

    def _apply_backups(self, backups: list[Any]) -> None:
        self.backup_table.set_rows(
            [
                {
                    "name": backup.name,
                    "kind": backup.kind,
                    "created_at": backup.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "size_mb": f"{backup.size_mb:.2f}",
                }
                for backup in backups
            ]
        )

    # ------------------------------------------------------ 局部重整（動作觸發）

    def _refresh_encryption_status_now(self) -> None:
        try:
            report = self.controller.encryption_status()
        except CRMError as exc:
            self.report_error(exc)
            return
        self._apply_encryption_status(report)

    def _refresh_gmail_status_now(self) -> None:
        try:
            address_status = self.controller.credential_status("gmail_address")
            password_status = self.controller.credential_status("gmail_app_password")
        except CRMError as exc:
            self.report_error(exc)
            return
        self._apply_gmail_status(address_status, password_status)

    def _refresh_backups_now(self) -> None:
        try:
            backups = self.controller.backups()
        except CRMError as exc:
            self.report_error(exc)
            return
        self._apply_backups(backups)

    # ------------------------------------------------------------------ 總覽

    def _open_config_folder(self) -> None:
        try:
            config_path = self.controller.config_path()
        except CRMError as exc:
            self.report_error(exc)
            return
        folder = config_path.parent
        if hasattr(os, "startfile"):
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            self.status(f"設定資料夾：{folder}")

    def _on_appearance_changed(self, mode_label: str) -> None:
        mode = APPEARANCE_REVERSE.get(mode_label, mode_label)
        qt_app = QApplication.instance()
        if qt_app is not None:
            theme.apply_theme(qt_app, mode)
        self.status(f"外觀已設為 {mode_label}")

    # ------------------------------------------------------------- 資料庫加密

    def _show_encryption_key(self) -> None:
        """把金鑰顯示出來讓使用者自己保存。

        密碼類的東西一律不顯示，這裡是唯一的例外——金鑰無法還原時資料就真的
        沒了，看得到才抄得下來。用可選取的唯讀文字框，方便直接複製。
        """
        try:
            key = self.controller.export_encryption_key()
        except CRMError as exc:
            self.report_error(exc)
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("資料庫金鑰")
        dialog.resize(620, 260)
        layout = QVBoxLayout(dialog)

        warning = QLabel(
            "這串字是唯一能解開資料庫與所有備份的東西。\n"
            "請存到密碼管理員，或印出來收好——不要和資料庫放在同一個地方。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
        layout.addWidget(warning)

        box = QPlainTextEdit(key)
        box.setReadOnly(True)
        box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        box.setFixedHeight(theme.text_box_height(4))
        layout.addWidget(box)

        def _copy() -> None:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(key)
            self.status("金鑰已複製到剪貼簿", "success")

        buttons = QHBoxLayout()
        copy_button = QPushButton("複製")
        copy_button.clicked.connect(_copy)
        buttons.addWidget(copy_button)
        close_button = QPushButton("關閉")
        close_button.clicked.connect(dialog.close)
        buttons.addWidget(close_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        dialog.exec()

    def _restore_encryption_key(self) -> None:
        """換電腦或重灌後，把先前備份的金鑰貼回來。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("還原金鑰")
        dialog.resize(620, 240)
        layout = QVBoxLayout(dialog)

        hint = QLabel("貼上先前用「備份金鑰」抄下來的那串字：")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        entry = QPlainTextEdit()
        entry.setFixedHeight(theme.text_box_height(4))
        layout.addWidget(entry)

        def _apply() -> None:
            if self._perform_key_restore(entry.toPlainText(), parent=dialog):
                dialog.accept()

        buttons = QHBoxLayout()
        apply_button = QPushButton("還原")
        apply_button.clicked.connect(_apply)
        buttons.addWidget(apply_button)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        dialog.exec()

    def _perform_key_restore(self, value: str, parent: QWidget | None = None) -> bool:
        """實際還原金鑰的邏輯，跟對話框本身拆開，方便測試直接呼叫。"""
        text = (value or "").strip()
        try:
            self.controller.import_encryption_key(text)
        except CRMError as exc:
            # 保管庫裡已經有另一把金鑰時要問過才覆蓋——蓋掉就再也解不開了。
            if "--force" not in str(exc):
                self.report_error(exc)
                return False
            reply = QMessageBox.question(
                parent or self,
                "覆蓋現有金鑰",
                "系統中已經有一把不同的金鑰。\n\n"
                "覆蓋它會讓目前資料庫裡的加密資料永遠無法還原。\n"
                "只有在你確定要改用另一份資料庫時才該這麼做。\n\n確定要覆蓋嗎？",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False
            try:
                self.controller.import_encryption_key(text, force=True)
            except CRMError as inner:
                self.report_error(inner)
                return False

        self._refresh_encryption_status_now()
        self.status("金鑰已還原 -- 請重新啟動應用程式", "success")
        return True

    # --------------------------------------------------------------- Gmail 帳號

    def _save_gmail_credentials(self) -> None:
        address = self.gmail_address_entry.get()
        password = self.gmail_password_entry.get()
        if not address and not password:
            self.status("請輸入帳號或應用程式密碼", "error")
            return

        summaries: list[str] = []
        all_secure = True
        try:
            if address:
                result = self.controller.save_credential("gmail_address", address)
                summaries.append(f"帳號：{result.value}")
                all_secure = all_secure and result is SecretSource.KEYRING
            if password:
                result = self.controller.save_credential("gmail_app_password", password)
                summaries.append(f"應用程式密碼：{result.value}")
                all_secure = all_secure and result is SecretSource.KEYRING
        except CRMError as exc:
            self.report_error(exc)
            return

        # 密碼一旦處理完，絕對不留在輸入框裡——就算是「未能安全儲存」的那個值也一樣。
        self.gmail_address_entry.set("")
        self.gmail_password_entry.set("")
        self._refresh_gmail_status_now()

        if all_secure:
            self.status("已安全儲存至系統憑證保管庫：" + "；".join(summaries), "success")
        else:
            self.status(
                "無法安全儲存（系統憑證保管庫不可用，密碼未被寫入任何地方）："
                + "；".join(summaries),
                "error",
            )

    def _clear_gmail_credentials(self) -> None:
        reply = QMessageBox.question(
            self,
            "清除 Gmail 憑證",
            "確定要從系統憑證保管庫清除已儲存的 Gmail 帳號與應用程式密碼嗎？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete_credential("gmail_address")
            self.controller.delete_credential("gmail_app_password")
        except CRMError as exc:
            self.report_error(exc)
            return
        self.gmail_address_entry.set("")
        self.gmail_password_entry.set("")
        self._refresh_gmail_status_now()
        self.status("已清除已儲存的 Gmail 憑證", "success")

    def _test_gmail_connection(self) -> None:
        if self.test_connection_task.running:
            return
        self.test_connection_button.setEnabled(False)
        self.status("正在測試 Gmail 連線...", "normal")
        self.test_connection_task.start()

    def _on_test_connection_done(self, message: str) -> None:
        self.test_connection_button.setEnabled(True)
        self.status(message, "success")

    def _on_test_connection_error(self, exc: Exception) -> None:
        self.test_connection_button.setEnabled(True)
        self.report_error(exc)

    # ------------------------------------------------------------- 郵件寄送

    def _save_daily_limit(self) -> None:
        text = self.daily_limit_entry.get()
        try:
            value = int(text)
        except ValueError:
            self.status("每日寄送上限必須是整數", "error")
            return
        try:
            self.mail_controller.set_daily_limit(value)
        except CRMError as exc:
            self.report_error(exc)
            return
        self.daily_limit_entry.set(str(value))
        self.status(f"已儲存每日寄送上限：{value}", "success")

    # ------------------------------------------------------------------ 備份

    def _create_backup(self) -> None:
        try:
            backup = self.controller.create_backup()
        except CRMError as exc:
            self.report_error(exc)
            return
        self._refresh_backups_now()
        self.status(f"已建立備份：{backup.name}", "success")

    def _restore_selected(self) -> None:
        row = self.backup_table.selected_row()
        if row is None:
            self.status("請先選擇要還原的備份", "error")
            return
        name = row["name"]
        reply = QMessageBox.question(
            self,
            "還原備份",
            f"確定要從 {name} 還原資料庫嗎？目前的資料庫會先另外備份保存，"
            "但還原完成後必須重新啟動應用程式，還原的資料才會正確載入。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.restore(name)
        except CRMError as exc:
            self.report_error(exc)
            return
        self.status(f"已從 {name} 還原 -- 請重新啟動應用程式", "success")

    def _delete_selected_backup(self) -> None:
        row = self.backup_table.selected_row()
        if row is None:
            self.status("請先選擇要刪除的備份", "error")
            return
        name = row["name"]

        reply = QMessageBox.question(
            self,
            "刪除備份",
            f"確定要刪除備份 {name} 嗎？\n"
            "檔案會真的從硬碟移除，這個動作無法復原。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.controller.delete_backup(name)
        except CRMError as exc:
            self.report_error(exc)
            return

        self.status(f"已刪除備份 {name}", "success")
        self.refresh()
