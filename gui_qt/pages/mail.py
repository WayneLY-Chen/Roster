"""郵件頁：編輯外寄樣板、產生收件名單、寄送——PySide6 版。

對照 ``gui/pages/mail.py``（Tk 版），資料層完全沿用同一組控制器：
``gui.controllers_mail.MailController``（樣板/名單/寄送）與
``gui.controllers.CompanyController``（篩選用的產業/標籤清單）。這兩個都是
不依賴 Tk 的乾淨 controller（``controllers_mail.py`` 只 import
``core.config``/``core.errors``/``core.schemas``/``gmail.campaign``/
``gmail.templates``），跟 ``gui.controllers`` 是同一層 MVC 設計，只是拆成
兩個檔案——這頁沿用 Tk 版原本的選擇，不因為換了介面框架而改用別的資料入口。

## 為什麼「寄件狀態」查詢要跑在背景執行緒

``MailController.mailer_status()`` 會呼叫 ``campaign.daily_sent_count()``
查資料庫；``CompanyController.distinct("industry")``/``all_tags()`` 也是
資料庫查詢。這支專案開了欄位加密（``database.encrypt: true``），公司相關的
查詢因此要逐筆解密比對，實測有感的成本（跟 ``gui_qt/pages/dashboard.py``
docstring 描述的成本同一個來源）。所以這頁跟儀表板一樣：``refresh()`` 只
負責「先用舊資料立刻重繪、再丟一個 :class:`~gui_qt.tasks.BackgroundTask`
去查」，不要在 UI 執行緒同步呼叫這些方法，否則換頁延遲量到的就不是 Qt
本身的成本。樣板清單（純粹讀樣板資料夾裡的檔名）跟變數說明（純字典查表）
不碰資料庫，維持同步呼叫即可。

## 內文編輯器

小框直接是可編輯的富文本 :class:`~gui_qt.composer.RichTextEditor`（不像
Tk 版的 ``CTkTextbox`` 只能顯示純文字、格式化樣板必須整個唯讀），「放大
編輯」則開 :class:`~gui_qt.composer.ComposerDialog`，方便在更大的視窗慢慢
寫一封長一點的信。

## 這次順便修掉的兩個回報問題

    1. 插入變數的 8 顆按鈕在 Tk 版會被切掉——這裡改成一個下拉選單
       （``self.placeholder_combo``），選了再按「插入游標處」。
    2. 「每日寄送上限」原本只能改 ``config.yaml``——這頁的「寄件狀態」區塊
       露出一個可以直接改、直接存的欄位，呼叫
       ``MailController.set_daily_limit()``（底層是
       ``core.config.save_user_setting()``，寫入前會整份重建設定驗證一次，
       驗證失敗會回滾，不會讓應用程式下次開不起來）。設定頁不在這次移植
       範圍內，所以放在郵件頁本身。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.constants import EmailVerdict
from core.errors import CRMError
from core.schemas import CompanyFilter
from controllers.core import CompanyController
from controllers.mail import BounceController, MailController, ReplyController, scan_inbox
from core.i18n import ALL_OPTION, STAGE_LABELS, stage_labels, to_value
from gui_qt import theme
from gui_qt.composer import RichTextEditor, edit_body, populate_preview
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import (
    CHECK_KEY,
    DataTable,
    LabeledEntry,
    Section,
    StatCard,
    WideComboBox,
    caption,
)


#: 「處理退信」那張表的欄位。
#:
#: 「為什麼」是對方伺服器原話，不是我們的判斷——擺在使用者眼前讓他自己看。
#: 有些退信的理由是「這個信箱暫停使用中」，那跟「查無此人」該不該標死是兩件事。
BOUNCE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 200),
    ("email", "信箱", 200),
    ("kind", "類型", 110),
    ("code", "代碼", 70),
    ("reason", "對方說的原因", 300),
    ("received", "收到", 90),
)


#: 「回覆與退訂」那張表的欄位。
#:
#: 「比對方式」是誠實那一欄：``確定``代表對方的信直接指到我們寄出的那一封，
#: ``用地址比對``代表只是「同一個地址寄來的」——那可能跟這次開發無關。使用者
#: 有權在勾之前就知道這一筆有多可信。
REPLY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 180),
    ("email", "信箱", 180),
    ("kind", "類型", 110),
    ("confidence", "比對方式", 90),
    ("action", "會做什麼", 150),
    ("subject", "主旨", 220),
    ("received", "收到", 90),
)


class MailPage(BasePage):
    title = "郵件"
    icon = "✉"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = MailController()
        self.company_controller = CompanyController()
        self.bounces = BounceController()
        self.replies = ReplyController()
        #: 上一次掃出來的退信與回信。兩張表的每一列都對應這裡面的一筆。
        self._bounce_hits: list = []
        self._reply_hits: list = []
        self.plan: Any = None
        self._mailer_ready = False
        self._loaded_template_name: str | None = None
        #: 正在用程式（而不是使用者操作）改開關的勾選狀態時設為 True，
        #: 避免 toggled signal 誤觸發「儲存設定」或彈出確認對話框。
        self._updating_switches = False
        #: 目前勾選的附件總量有沒有超過上限。超過就擋住「產生名單」——
        #: 讓使用者在按下去之前就知道，而不是跑完名單才失敗。
        self._attachments_ok = True

        # 三個背景工作對應三種會碰資料庫／會跑一段時間的動作，理由見檔案
        # 開頭的說明；名稱刻意跟 Tk 版 self.build_task/self.send_task 一致。
        self.status_task = BackgroundTask(
            self, self._fetch_status, on_done=self._apply_status, on_error=self._handle_error
        )
        self.build_task = BackgroundTask(
            self,
            self.controller.build_plan,
            on_done=self._on_build_plan_done,
            on_error=self._on_build_plan_error,
        )
        self.send_task = BackgroundTask(
            self,
            self.controller.send,
            on_progress=self._on_send_progress,
            on_done=self._on_send_done,
            on_error=self._on_send_error,
        )
        # 退信那兩步分開成兩個工作，中間隔著使用者的勾選與確認：讀信箱是唯讀
        # 的，寫回去是另一件事。合成一個的話就沒有中間那一眼了。
        self.bounce_task = BackgroundTask(
            self,
            self._scan_worker,
            on_progress=self._on_bounce_progress,
            on_done=self._on_inbox,
            on_error=self._on_bounce_error,
        )
        self.bounce_mark_task = BackgroundTask(
            self,
            self._mark_worker,
            on_progress=self._on_bounce_progress,
            on_done=self._on_bounces_applied,
            on_error=self._on_bounce_mark_error,
        )
        self.reply_mark_task = BackgroundTask(
            self,
            self._reply_worker,
            on_progress=self._on_bounce_progress,
            on_done=self._on_replies_applied,
            on_error=self._on_reply_mark_error,
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(10)

        title_label = QLabel("郵件")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        self._build_status_section(outer)

        # 「郵件樣板」+「收件對象」兩個面板疊起來的最小高度，在一般視窗大小
        # 下常常會比可用空間高（樣板編輯器、收件表格都各自需要一定高度）。
        # 包一層 QScrollArea 而不是直接塞進 outer：內容真的放不下時只會多一條
        # 捲軸，不會像先前那樣讓「插入變數」那一列跟編輯器工具列疊在一起、
        # 或把下面的元件擠出視窗看不到。
        body_container = QWidget()
        body_column = QVBoxLayout(body_container)
        body_column.setContentsMargins(0, 0, 0, 0)
        body_column.setSpacing(10)
        panels = QWidget()
        body_row = QHBoxLayout(panels)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(10)
        self._build_template_section(body_row)
        self._build_recipients_section(body_row)
        body_column.addWidget(panels, 1)
        self._build_bounce_section(body_column)
        self._build_reply_section(body_column)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body_container)
        outer.addWidget(scroll, 1)

        self._build_footer(outer)

    def _build_status_section(self, outer: QVBoxLayout) -> None:
        section = Section("寄件狀態")
        outer.addWidget(section)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        self.account_card = StatCard("寄件帳號")
        self.daily_card = StatCard("今日已寄")
        self.mode_card = StatCard("模式")
        for card in (self.account_card, self.daily_card, self.mode_card):
            cards_row.addWidget(card, 1)
        section.body_layout.addLayout(cards_row)

        switches_row = QHBoxLayout()
        self.enabled_check = QCheckBox("啟用郵件寄送")
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        switches_row.addWidget(self.enabled_check)

        self.live_check = QCheckBox("實際寄出（關閉＝演練，不會真的寄）")
        self.live_check.toggled.connect(self._on_live_toggled)
        switches_row.addWidget(self.live_check)
        switches_row.addStretch(1)
        section.body_layout.addLayout(switches_row)

        # 每日寄送上限：使用者問過為什麼是 100 不是 2000，這裡讓它可讀可改，
        # 不必再手動編輯 config.yaml。
        limit_row = QHBoxLayout()
        limit_row.addWidget(caption("每日寄送上限"))
        self.daily_limit_spin = QSpinBox()
        self.daily_limit_spin.setRange(1, 2000)
        self.daily_limit_spin.setFixedWidth(theme.input_width(7, has_spin_buttons=True))
        # QSpinBox 的 sizeHint 比其他控制項高 4px，QSS 改不動——見該函式說明。
        theme.match_control_height(self.daily_limit_spin)
        limit_row.addWidget(self.daily_limit_spin)
        save_limit_button = QPushButton("儲存上限")
        save_limit_button.clicked.connect(self._save_daily_limit)
        limit_row.addWidget(save_limit_button, 0, Qt.AlignmentFlag.AlignBottom)
        limit_row.addStretch(1)
        section.body_layout.addLayout(limit_row)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
        goto_settings_button = QPushButton("前往設定")
        goto_settings_button.clicked.connect(lambda: self.app.show_page("設定"))

        self.warning_widget = QWidget()
        warning_row = QHBoxLayout(self.warning_widget)
        warning_row.setContentsMargins(0, 0, 0, 0)
        warning_row.addWidget(self.warning_label, 1)
        warning_row.addWidget(goto_settings_button)
        section.body_layout.addWidget(self.warning_widget)
        self.warning_widget.hide()

    def _build_template_section(self, body_row: QHBoxLayout) -> None:
        section = Section("郵件樣板")
        body_row.addWidget(section, 1)

        toolbar = QHBoxLayout()
        toolbar.addWidget(caption("樣板"))
        self.template_combo = WideComboBox()
        self.template_combo.currentTextChanged.connect(self._on_template_selected)
        toolbar.addWidget(self.template_combo, 1)
        new_button = QPushButton("新增樣板")
        new_button.clicked.connect(self._new_template)
        toolbar.addWidget(new_button)
        save_button = QPushButton("儲存樣板")
        save_button.clicked.connect(self._save_template)
        toolbar.addWidget(save_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(toolbar)

        self.subject_entry = LabeledEntry("主旨")
        section.body_layout.addWidget(self.subject_entry)

        body_header = QHBoxLayout()
        body_header.addWidget(caption("內文"))
        body_header.addStretch(1)
        enlarge_button = QPushButton("放大編輯")
        enlarge_button.clicked.connect(self._open_composer)
        body_header.addWidget(enlarge_button)
        section.body_layout.addLayout(body_header)

        # show_toolbar=False：這裡是「郵件樣板」跟「收件對象」左右並排的窄
        # 面板，完整的 9 顆格式按鈕排一列需要的寬度會把這個面板撐大、擠壓
        # 旁邊的收件表格（「信箱」欄被截斷看不到的根因），高度也會跟下面
        # 「插入變數」那一列搶空間、疊在一起。格式功能一個都沒少，只是搬去
        # 「放大編輯」那個有足夠寬度的獨立視窗——下面補一行提示，不然使用者
        # 會以為這裡打不出格式。
        self.body_editor = RichTextEditor(self.controller.config, show_toolbar=False)
        self.body_editor.setMinimumHeight(120)
        section.body_layout.addWidget(self.body_editor, 1)

        toolbar_hint = caption("需要粗體／清單／圖片等格式，請按上面「放大編輯」")
        section.body_layout.addWidget(toolbar_hint)

        # 下拉選單取代 Tk 版那 8 顆會被切掉的變數按鈕。
        insert_row = QHBoxLayout()
        insert_row.addWidget(caption("插入變數"))
        self.placeholder_combo = WideComboBox()
        self.placeholder_combo.setMinimumWidth(220)
        insert_row.addWidget(self.placeholder_combo, 1)
        insert_button = QPushButton("插入游標處")
        insert_button.clicked.connect(self._insert_selected_placeholder)
        insert_row.addWidget(insert_button)
        section.body_layout.addLayout(insert_row)

        self._placeholder_tokens: dict[str, str] = {}
        self._build_placeholder_menu()

        self._build_attachments_row(section)

    # ------------------------------------------------------------- 附件

    def _build_attachments_row(self, section: Section) -> None:
        """附件放在「郵件樣板」面板底部——它跟主旨、內文一樣屬於「要寄什麼」。

        勾選式清單而不是「已加入就一定會寄」：附件資料夾是長期累積的（型錄、
        報價單、公司簡介），每次活動只挑其中幾個，不該為了這次不寄就把檔案
        刪掉。
        """
        header = QHBoxLayout()
        header.addWidget(caption("附件"))
        header.addStretch(1)

        add_button = QPushButton("加入附件")
        add_button.clicked.connect(self._add_attachment)
        header.addWidget(add_button)

        self.remove_attachment_button = QPushButton("移除")
        self.remove_attachment_button.setEnabled(False)
        self.remove_attachment_button.clicked.connect(self._remove_attachment)
        header.addWidget(self.remove_attachment_button)

        # 「移除」是快速刪掉選到的那一個；「管理…」才是完整的整理視窗
        # （改顯示名稱、寫備註、看用過幾次）。整理跟寄信是兩件事，塞在同一頁
        # 只會讓寄信流程變成雜物櫃。
        manage_button = QPushButton("管理…")
        manage_button.setToolTip("改顯示名稱、寫備註、看用過幾次")
        manage_button.clicked.connect(self._manage_attachments)
        header.addWidget(manage_button)
        section.body_layout.addLayout(header)

        self.attachment_list = QListWidget()
        self.attachment_list.setFixedHeight(theme.text_box_height(4))
        self.attachment_list.itemChanged.connect(self._on_attachment_checked)
        self.attachment_list.currentItemChanged.connect(
            lambda current, _previous: self.remove_attachment_button.setEnabled(
                current is not None
            )
        )
        section.body_layout.addWidget(self.attachment_list)

        self.attachment_summary = caption("")
        section.body_layout.addWidget(self.attachment_summary)
        self._refresh_attachments()

    def _refresh_attachments(self, keep_checked: set[str] | None = None) -> None:
        """重新列出附件資料夾的內容，保留原本的勾選狀態。"""
        checked = keep_checked if keep_checked is not None else set(self.selected_attachments())

        try:
            stored = self.controller.attachments()
        except CRMError as exc:
            self.report_error(exc)
            return

        # 重填期間 itemChanged 會連環觸發，先擋掉，不然每加一列就重算一次總量。
        self.attachment_list.blockSignals(True)
        self.attachment_list.clear()
        for item_data in stored:
            item = QListWidgetItem(f"{item_data.name}（{item_data.human_size}）")
            item.setData(Qt.ItemDataRole.UserRole, item_data.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if item_data.name in checked
                else Qt.CheckState.Unchecked
            )
            self.attachment_list.addItem(item)
        self.attachment_list.blockSignals(False)

        self._update_attachment_summary()

    def selected_attachments(self) -> list[str]:
        """目前打勾的附件檔名。"""
        names: list[str] = []
        for index in range(self.attachment_list.count()):
            item = self.attachment_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                names.append(item.data(Qt.ItemDataRole.UserRole))
        return names

    def _update_attachment_summary(self) -> None:
        from gmail.attachments import human_size

        names = self.selected_attachments()
        limit = self.controller.attachment_limit_bytes()
        if not names:
            self.attachment_summary.setText(
                f"未選附件。單封信所有附件合計上限 {human_size(limit)}。"
            )
            self._attachments_ok = True
            return

        total = 0
        for index in range(self.attachment_list.count()):
            item = self.attachment_list.item(index)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            # 大小已經寫在顯示文字裡，但那是給人看的；真的要算總量還是要問檔案。
            try:
                from gmail.attachments import resolve

                total += resolve(item.data(Qt.ItemDataRole.UserRole)).stat().st_size
            except (OSError, CRMError):
                continue

        self._attachments_ok = total <= limit
        over = "" if self._attachments_ok else "　超過上限，請取消勾選幾個檔案。"
        self.attachment_summary.setText(
            f"已選 {len(names)} 個附件，合計 {human_size(total)} / {human_size(limit)}。{over}"
        )

    def _on_attachment_checked(self, _item: object) -> None:
        self._update_attachment_summary()

    def _add_attachment(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "選擇要附加的檔案", "", "所有檔案 (*.*)")
        if not paths:
            return

        checked = set(self.selected_attachments())
        added = 0
        for path in paths:
            try:
                stored = self.controller.add_attachment(path)
            except CRMError as exc:
                self.report_error(exc)
                continue
            checked.add(stored.name)      # 剛加進來的預設就勾選，符合直覺
            added += 1

        self._refresh_attachments(keep_checked=checked)
        if added:
            self.status(f"已加入 {added} 個附件", "success")

    def _manage_attachments(self) -> None:
        """開附件庫的整理視窗。關掉之後重新載入，名稱或備註改過要跟著更新。"""
        from gui_qt.attachment_library import AttachmentLibraryDialog

        checked = set(self.selected_attachments())
        AttachmentLibraryDialog(self, self.controller).exec()
        self._refresh_attachments(keep_checked=checked)

    def _remove_attachment(self) -> None:
        item = self.attachment_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)

        # 排程可能正引用這個附件。刪掉的話排程會在半夜三點失敗，而且不會有人
        # 在當下看到錯誤訊息——等發現時已經漏寄好幾天。
        warning = ""
        try:
            if self.controller.attachment_used_by_schedule(name):
                warning = (
                    "\n\n⚠ 這個附件正被「自動排程」引用，刪掉之後排程寄信會失敗。"
                    "請記得到「設定」頁把它取消勾選。"
                )
        except CRMError:
            pass

        confirm = QMessageBox.question(
            self,
            "移除附件",
            f"要把「{name}」從附件資料夾刪除嗎？\n"
            f"檔案會真的被刪掉，這個動作無法復原。{warning}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            self.controller.remove_attachment(name)
        except CRMError as exc:
            self.report_error(exc)
            return

        checked = set(self.selected_attachments()) - {name}
        self._refresh_attachments(keep_checked=checked)
        self.status(f"已移除附件 {name}", "success")

    def _build_placeholder_menu(self) -> None:
        try:
            placeholders = self.controller.placeholders()
        except CRMError as exc:
            self.report_error(exc)
            placeholders = {}

        self._placeholder_tokens = {
            f"{{{name}}} {description}": f"{{{name}}}"
            for name, description in placeholders.items()
        }
        values = list(self._placeholder_tokens) or ["（無可用變數）"]
        self.placeholder_combo.clear()
        self.placeholder_combo.addItems(values)

    def _build_recipients_section(self, body_row: QHBoxLayout) -> None:
        section = Section("收件對象")
        body_row.addWidget(section, 1)

        filters_row = QHBoxLayout()
        self.industry_combo = WideComboBox()
        self.industry_combo.addItem(ALL_OPTION)
        filters_row.addWidget(self.industry_combo)

        self.stage_combo = WideComboBox()
        self.stage_combo.addItems(stage_labels(with_all=True))
        filters_row.addWidget(self.stage_combo)

        self.tag_combo = WideComboBox()
        self.tag_combo.addItem(ALL_OPTION)
        filters_row.addWidget(self.tag_combo)
        section.body_layout.addLayout(filters_row)

        self.verified_only_check = QCheckBox("只選已驗證信箱")
        self.verified_only_check.setChecked(True)
        section.body_layout.addWidget(self.verified_only_check)

        campaign_row = QHBoxLayout()
        self.campaign_name_entry = LabeledEntry(
            "活動名稱", value=f"開發信-{date.today().isoformat()}"
        )
        campaign_row.addWidget(self.campaign_name_entry, 1)
        self.build_plan_button = QPushButton("產生名單")
        self.build_plan_button.clicked.connect(self._start_build_plan)
        # 靠下對齊。LabeledEntry 是「說明文字在上、輸入框在下」的直向堆疊，
        # 按鈕預設會對齊整個堆疊的垂直中心，看起來就卡在說明文字與輸入框
        # 之間、跟輸入框沒有對齊。
        campaign_row.addWidget(self.build_plan_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(campaign_row)

        # 欄寬總和刻意收斂到明顯小於這個面板的實際寬度：這個面板是跟「郵件
        # 樣板」左右並排的窄欄，早先用 Tk 版原本的欄寬（150/165/110＝425）
        # 會讓「信箱」欄被截斷、看不到——欄寬總和降到 300，「狀態」又是
        # DataTable 裡設了 stretchLastSection 的最後一欄，會自動吃掉剩餘
        # 空間，三欄在正常視窗寬度下都看得到。
        self.table = DataTable(
            columns=[
                ("company_name", "公司名稱", 110),
                ("to_address", "信箱", 130),
                ("status", "狀態", 60),
            ],
            min_rows=6,
        )
        section.body_layout.addWidget(self.table, 1)

        self.summary_label = QLabel("尚未產生名單。")
        self.summary_label.setObjectName("MutedLabel")
        self.summary_label.setWordWrap(True)
        section.body_layout.addWidget(self.summary_label)

    # ------------------------------------------------------------- 處理退信

    def _build_bounce_section(self, column: QVBoxLayout) -> None:
        """讀收件匣找退信，把死信箱標出來。

        放在寄信旁邊而不是「公司資訊」頁：這件事是寄信的一部分，而使用者會
        想到它的時間點就是他準備寄下一批的時候。
        """
        section = Section("處理退信")

        intro = QLabel(
            "寄不到的信箱如果沒有人處理，下一批會再寄一次，"
            "而<b>被拉低送達率的是你自己的 Gmail 帳號</b>。"
            "按下面那顆按鈕會<b>唯讀</b>地讀一次你的信箱，"
            "把退回來的信找出來——不會標成已讀，也不會刪任何東西。"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setObjectName("MutedLabel")
        intro.setWordWrap(True)
        section.body_layout.addWidget(intro)

        row = QHBoxLayout()
        self.bounce_scan_button = QPushButton("讀信箱")
        self.bounce_scan_button.clicked.connect(self._scan_bounces)
        row.addWidget(self.bounce_scan_button)
        self.bounce_apply_button = QPushButton("標記勾起來的")
        self.bounce_apply_button.clicked.connect(self._apply_bounces)
        self.bounce_apply_button.setEnabled(False)
        row.addWidget(self.bounce_apply_button)
        row.addStretch(1)
        section.body_layout.addLayout(row)

        self.bounce_status = QLabel("")
        self.bounce_status.setObjectName("MutedLabel")
        self.bounce_status.setWordWrap(True)
        section.body_layout.addWidget(self.bounce_status)

        self.bounce_table = DataTable(BOUNCE_COLUMNS, min_rows=3, checkable=True)
        section.body_layout.addWidget(self.bounce_table)

        note = QLabel(
            "只會列出<b>這支程式真的寄過</b>的地址——收件匣裡其他的信改不動你的名單。"
            "<b>硬退信</b>（對方說這個信箱不存在）預設勾起來；"
            "<b>軟退信</b>（信箱滿了、對方伺服器暫時掛掉）預設不勾，"
            "因為那種過幾天就好了，標死一個真實客戶的代價大得多。"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        column.addWidget(section)

    def _scan_worker(self, *, report=None, cancel_event=None):
        """在背景執行緒裡讀一次信箱：退信與回覆一起找。

        包一層而不是直接把 controller 的方法交給 BackgroundTask：那樣綁住的是
        **建立當下**那個 controller，而 ``on_show()`` 會換掉它。使用者剛在設定
        頁改完 Gmail 密碼的話，舊的那份會拿一組錯的帳密去登入。
        """
        return scan_inbox(
            self.controller.config, report=report, cancel_event=cancel_event
        )

    def _mark_worker(self, hits, *, report=None, cancel_event=None):
        return self.bounces.apply(hits, report=report, cancel_event=cancel_event)

    def _reply_worker(self, hits, *, report=None, cancel_event=None):
        return self.replies.apply(hits, report=report, cancel_event=cancel_event)

    def _scan_bounces(self) -> None:
        if self.bounce_task.running:
            return
        self.bounce_scan_button.setEnabled(False)
        self.bounce_apply_button.setEnabled(False)
        self.bounce_table.clear()
        self.reply_table.clear()
        self._bounce_hits = []
        self._reply_hits = []
        self.reply_apply_button.setEnabled(False)
        self.bounce_status.setText("正在連你的信箱…")
        self.bounce_task.start()

    def _on_bounce_progress(self, message: object) -> None:
        self.bounce_status.setText(str(message))

    def _on_bounces(self, scan) -> None:
        self.bounce_scan_button.setEnabled(True)
        self._bounce_hits = list(scan.hits)
        rows = []
        for index, hit in enumerate(scan.hits):
            received = hit.bounce.received_at
            rows.append(
                {
                    "company_name": hit.company_name,
                    "email": hit.email,
                    "kind": hit.bounce.kind + ("（已標記）" if hit.already else ""),
                    "code": hit.bounce.code or "—",
                    "reason": hit.bounce.reason or "對方沒有說明",
                    "received": received.strftime("%Y-%m-%d") if received else "",
                    # 硬退信預設勾、軟退信不勾。已經標過的也不勾——重複標一次
                    # 沒有意義，而且會讓「這次改了幾筆」那個數字騙人。
                    CHECK_KEY: bool(hit.bounce.hard and not hit.already),
                    "_index": index,
                }
            )
        self.bounce_table.set_rows(rows)
        self.bounce_apply_button.setEnabled(bool(rows))
        self.bounce_status.setText(scan.describe())
        self.status(scan.describe(), "success" if scan.hits else "normal")

    def _on_bounce_error(self, exc: Exception) -> None:
        self.bounce_scan_button.setEnabled(True)
        self.bounce_status.setText(str(exc).splitlines()[0])
        self._handle_error(exc)

    def _apply_bounces(self) -> None:
        chosen = [
            self._bounce_hits[row["_index"]]
            for row in self.bounce_table.checked_rows()
            if 0 <= row.get("_index", -1) < len(self._bounce_hits)
        ]
        if not chosen:
            self.status("一筆都沒有勾", "warning")
            return
        hard = sum(1 for hit in chosen if hit.bounce.hard)
        confirmed = QMessageBox.question(
            self,
            "要標記這幾筆嗎？",
            f"勾起來的 {len(chosen)} 筆裡有 {hard} 筆是硬退信。\n\n"
            f"那 {hard} 個信箱會被標成「退過信」，之後的寄送一律跳過它們"
            "（把信箱改成別的之後就會解除）。其餘的只會留一則紀錄。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.bounce_apply_button.setEnabled(False)
        self.bounce_mark_task.start(chosen)

    def _on_bounces_applied(self, result) -> None:
        self.bounce_status.setText(result.describe())
        self.status(result.describe(), "success")
        self.bounce_table.clear()
        self._bounce_hits = []
        self.refresh()

    def _on_bounce_mark_error(self, exc: Exception) -> None:
        self.bounce_apply_button.setEnabled(True)
        self._handle_error(exc)

    # ---------------------------------------------------------- 回覆與退訂

    def _build_reply_section(self, column: QVBoxLayout) -> None:
        """同一趟讀信箱找出來的回信與退訂要求。

        跟退信分成兩張表而不是一張：它們寫回去的是完全不同的東西（一個改
        「這個地址還活著嗎」，一個改業務階段與請勿聯絡），混在一張表裡那顆
        「標記」按鈕就說不清楚自己會做什麼。
        """
        section = Section("回覆與退訂")

        intro = QLabel(
            "有人回信了卻沒有人記下來，下一批就會<b>對著已經回過信的人再寄一次"
            "冷開發信</b>。有人說「不要再寄」而沒有人處理，那更嚴重——"
            "每一封信都印著退訂說明，等於邀請了對方卻不去收。"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setObjectName("MutedLabel")
        intro.setWordWrap(True)
        section.body_layout.addWidget(intro)

        row = QHBoxLayout()
        self.reply_apply_button = QPushButton("採用勾起來的")
        self.reply_apply_button.clicked.connect(self._apply_replies)
        self.reply_apply_button.setEnabled(False)
        row.addWidget(self.reply_apply_button)
        row.addStretch(1)
        section.body_layout.addLayout(row)

        self.reply_status = QLabel("")
        self.reply_status.setObjectName("MutedLabel")
        self.reply_status.setWordWrap(True)
        section.body_layout.addWidget(self.reply_status)

        self.reply_table = DataTable(REPLY_COLUMNS, min_rows=3, checkable=True)
        section.body_layout.addWidget(self.reply_table)

        note = QLabel(
            "「會做什麼」那一欄就是採用之後實際發生的事。"
            "業務階段<b>只會往前推</b>——你已經手動改成「會議」的公司，"
            "一封回信不會把它拉回「已聯絡」。"
            "<b>自動回覆</b>（休假通知那種）預設不勾：它證明信寄到了，"
            "但不證明有人讀過。"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.body_layout.addWidget(note)

        column.addWidget(section)

    def _on_inbox(self, scan) -> None:
        """一趟讀完，兩張表一起填。"""
        self._on_bounces(scan.bounces)
        self._on_replies(scan.replies)

    def _on_replies(self, scan) -> None:
        self._reply_hits = list(scan.hits)
        rows = []
        for index, hit in enumerate(scan.hits):
            received = hit.reply.received_at
            rows.append(
                {
                    "company_name": hit.company_name,
                    "email": hit.email,
                    "kind": hit.reply.kind,
                    "confidence": hit.reply.confidence,
                    "action": hit.action,
                    "subject": hit.reply.subject or "（沒有主旨）",
                    "received": received.strftime("%Y-%m-%d") if received else "",
                    CHECK_KEY: hit.suggested,
                    "_index": index,
                }
            )
        self.reply_table.set_rows(rows)
        self.reply_apply_button.setEnabled(bool(rows))
        self.reply_status.setText(scan.describe())

    def _apply_replies(self) -> None:
        chosen = [
            self._reply_hits[row["_index"]]
            for row in self.reply_table.checked_rows()
            if 0 <= row.get("_index", -1) < len(self._reply_hits)
        ]
        if not chosen:
            self.status("一筆都沒有勾", "warning")
            return
        out = sum(1 for hit in chosen if hit.reply.unsubscribe)
        moved = sum(1 for hit in chosen if hit.will_advance)
        confirmed = QMessageBox.question(
            self,
            "要採用這幾筆嗎？",
            f"勾起來的 {len(chosen)} 筆會做這些事：\n\n"
            f"・{out} 家標記為「請勿聯絡」，之後一律不再寄（這個只能加不能減，"
            "要解除得到那家公司的詳細視窗自己取消）\n"
            f"・{moved} 家的業務階段往前推到「已聯絡」\n"
            "・其餘的只留一則紀錄",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.reply_apply_button.setEnabled(False)
        self.reply_mark_task.start(chosen)

    def _on_replies_applied(self, result) -> None:
        self.reply_status.setText(result.describe())
        self.status(result.describe(), "success")
        self.reply_table.clear()
        self._reply_hits = []
        bump_data_version()
        self.refresh()

    def _on_reply_mark_error(self, exc: Exception) -> None:
        self.reply_apply_button.setEnabled(True)
        self._handle_error(exc)


    def _build_footer(self, outer: QVBoxLayout) -> None:
        footer = QHBoxLayout()

        self.preview_button = QPushButton("預覽第一封")
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self._preview_first)
        footer.addWidget(self.preview_button)

        self.dry_run_button = QPushButton("演練（不寄出）")
        self.dry_run_button.setEnabled(False)
        self.dry_run_button.clicked.connect(lambda: self._start_send(force_dry_run=True))
        footer.addWidget(self.dry_run_button)

        self.send_button = QPushButton("開始寄送")
        self.send_button.setEnabled(False)
        # 這是這頁唯一的主要動作，套用 theme.py 的 #PrimaryButton 樣式（強調色
        # 實心、四態都定義好了）；顏色不再自己內嵌 setStyleSheet 硬編碼，改由
        # theme.py 集中管理。「預覽第一封」「演練（不寄出）」維持預設的次要
        # 樣式，不套用這個 objectName。
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.clicked.connect(lambda: self._start_send(force_dry_run=False))
        footer.addWidget(self.send_button)

        self.cancel_send_button = QPushButton("取消")
        self.cancel_send_button.setEnabled(False)
        self.cancel_send_button.clicked.connect(self._cancel_send)
        footer.addWidget(self.cancel_send_button)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        footer.addWidget(self.result_label, 1)

        outer.addLayout(footer)

    # -------------------------------------------------------------- 生命週期

    def on_show(self, force: bool = False) -> None:
        # 一份新的 controller，不是 __init__ 那份：設定頁（或這頁自己的開關／
        # 每日上限）可能剛存檔，只有重新建一個 MailController() 才讀得到
        # get_config() 的新值——跟 Tk 版 on_show() 的理由一模一樣。
        self.controller = MailController()
        # 退信那一個也要跟著換：Gmail 的帳號密碼可能剛在設定頁改過，舊的那份
        # controller 手上是舊設定，連線會用一組不存在的帳密去登入。
        self.bounces = BounceController()
        self.replies = ReplyController()
        # 開關/每日上限的變更不會呼叫 bump_data_version()（那不是「新增/
        # 刪除/編輯公司」那類寫入），所以固定強制重整，不套用資料版本跳過
        # 機制，跟 Tk 版「每次顯示都整份重整」的行為一致。
        super().on_show(force=True)

    def on_hide(self) -> None:
        # 這頁沒有自己的 QTimer；三個背景工作即使還在跑，離開頁面也不去動
        # 它們——跟 Tk 版一樣，讓使用者切走之後寄送工作仍會在背景跑完。
        pass

    def refresh(self) -> None:
        self._refresh_templates()
        self._refresh_status()

    # -------------------------------------------------------------- 樣板

    def _refresh_templates(self, select: str | None = None) -> None:
        try:
            names = self.controller.templates()
        except CRMError as exc:
            self.report_error(exc)
            names = []

        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItems(names or [""])
        self.template_combo.blockSignals(False)

        if not names:
            self.subject_entry.set("")
            self.body_editor.set_body("")
            self._loaded_template_name = None
            return

        current = self.template_combo.currentText()
        target = select or (current if current in names else names[0])
        if target not in names:
            target = names[0]

        self.template_combo.blockSignals(True)
        self.template_combo.setCurrentText(target)
        self.template_combo.blockSignals(False)
        if target != self._loaded_template_name:
            self._load_template(target)

    def _on_template_selected(self, name: str) -> None:
        self._load_template(name)

    def _load_template(self, name: str) -> None:
        if not name:
            return
        try:
            subject, body = self.controller.load(name)
        except CRMError as exc:
            self.report_error(exc)
            return
        self.subject_entry.set(subject)
        self.body_editor.set_body(body)
        self._loaded_template_name = name

    def _new_template(self) -> None:
        name, ok = QInputDialog.getText(self, "新增樣板", "請輸入新樣板名稱：")
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            self.controller.save(name, "", "")
        except CRMError as exc:
            self.report_error(exc)
            return
        self._refresh_templates(select=name)
        self.status(f"已建立樣板「{name}」", "success")

    def _save_template(self) -> None:
        name = self.template_combo.currentText().strip()
        if not name:
            self.status("請先新增或選擇一個樣板", "error")
            return
        subject = self.subject_entry.get()
        body = self.body_editor.to_body_string()
        try:
            self.controller.save(name, subject, body)
        except CRMError as exc:
            self.report_error(exc)
            return
        self._loaded_template_name = name
        self.status(f"已儲存樣板「{name}」", "success")

    def _insert_selected_placeholder(self) -> None:
        token = self._placeholder_tokens.get(self.placeholder_combo.currentText())
        if token:
            self.body_editor.insert_text_at_cursor(token)

    def _open_composer(self) -> None:
        """開大視窗編輯內文，帶著格式與圖片回來。"""
        edited = edit_body(self, self.body_editor.to_body_string(), self.controller.config)
        if edited is None:
            return
        self.body_editor.set_body(edited)
        self.status("內文已更新；別忘了按「儲存樣板」", "success")

    # -------------------------------------------------------------- 寄件狀態

    def _refresh_status(self) -> None:
        if self.status_task.running:
            return  # 上一次查詢還沒回來，不要疊加第二次
        self.status_task.start()

    def _fetch_status(self, *, report, cancel_event) -> tuple[dict, list[str], list[str]]:
        """在背景執行緒被呼叫；千萬不能在這裡碰任何 widget。"""
        status = self.controller.mailer_status()
        industries = self.company_controller.distinct("industry")
        tags = self.company_controller.all_tags()
        return status, industries, tags

    def _apply_status(self, result: tuple) -> None:
        status, industries, tags = result

        account_display = status["address"] or "尚未設定"
        self.account_card.update_values(
            account_display,
            "帳號已就緒" if status["account_ready"] == "是" else "尚未設定應用程式密碼",
        )
        self.daily_card.update_values(
            f"{status['daily_sent']} / {status['daily_limit']}", "封（今日）"
        )
        dry_run = status["dry_run"] == "是"
        enabled = status["enabled"] == "是"
        self.mode_card.update_values(
            "演練（不會寄出）" if dry_run else "實際寄送",
            "不會真的寄信，可安心測試" if dry_run else "信件會真的送出去",
        )

        self._set_checked_silently(self.enabled_check, enabled)
        self._set_checked_silently(self.live_check, not dry_run)

        self.daily_limit_spin.blockSignals(True)
        self.daily_limit_spin.setValue(int(status["daily_limit"]))
        self.daily_limit_spin.blockSignals(False)

        self._mailer_ready = enabled and status["account_ready"] == "是"
        if self._mailer_ready:
            self.warning_widget.hide()
        else:
            reasons = []
            if not enabled:
                reasons.append("郵件寄送功能尚未啟用（請開啟上方「啟用郵件寄送」）")
            if status["account_ready"] != "是":
                reasons.append("尚未設定 Gmail 寄件帳號或應用程式密碼")
            self.warning_label.setText("無法實際寄送：" + "；".join(reasons))
            self.warning_widget.show()

        if dry_run:
            self.result_label.setText("目前為演練模式，按下「開始寄送」也不會真的寄出。")
        elif self.result_label.text().startswith("目前為演練模式"):
            self.result_label.setText("")

        self._refresh_filter_options(industries, tags)
        self._update_action_buttons()

    def _handle_error(self, exc: Exception) -> None:
        self.report_error(exc)

    def _set_checked_silently(self, checkbox: QCheckBox, value: bool) -> None:
        self._updating_switches = True
        try:
            checkbox.setChecked(value)
        finally:
            self._updating_switches = False

    def _on_enabled_toggled(self, checked: bool) -> None:
        if self._updating_switches:
            return
        try:
            self.controller.set_mailer_option("enabled", checked)
        except CRMError as exc:
            self._set_checked_silently(self.enabled_check, not checked)
            self.report_error(exc)
            return
        self.status("已啟用郵件寄送" if checked else "已停用郵件寄送", "success")
        self._refresh_status()

    def _on_live_toggled(self, checked: bool) -> None:
        if self._updating_switches:
            return
        # 關閉演練模式代表下一次「開始寄送」會真的寄到對方信箱，這個方向要
        # 先問過；開回演練永遠安全，不需要打斷使用者。
        if checked:
            confirmed = (
                QMessageBox.question(
                    self,
                    "關閉演練模式",
                    "關閉演練後，按下「開始寄送」會真的把信寄到對方的信箱。\n\n"
                    "寄出去的信收不回來，也無法取消。\n"
                    "建議先用「演練（不寄出）」跑過一次，確認名單與內容都正確。\n\n"
                    "確定要關閉演練模式嗎？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.Yes
            )
            if not confirmed:
                self._set_checked_silently(self.live_check, False)
                return

        try:
            self.controller.set_mailer_option("dry_run", not checked)
        except CRMError as exc:
            self._set_checked_silently(self.live_check, not checked)
            self.report_error(exc)
            return

        self.status(
            "已切換為實際寄送 -- 按下「開始寄送」會真的寄出" if checked else "已切換回演練模式",
            "error" if checked else "success",
        )
        self._refresh_status()

    def _save_daily_limit(self) -> None:
        value = self.daily_limit_spin.value()
        try:
            self.controller.set_daily_limit(value)
        except CRMError as exc:
            self.report_error(exc)
            return
        self.status(f"每日寄送上限已更新為 {value} 封", "success")
        self._refresh_status()

    def _update_action_buttons(self) -> None:
        has_sendable = self.plan is not None and getattr(self.plan, "sendable", 0) > 0
        self.preview_button.setEnabled(has_sendable)
        self.dry_run_button.setEnabled(has_sendable)
        self.send_button.setEnabled(has_sendable and self._mailer_ready)

    def _refresh_filter_options(self, industries: list[str], tags: list[str]) -> None:
        industry_values = [ALL_OPTION, *industries]
        current_industry = self.industry_combo.currentText()
        self.industry_combo.blockSignals(True)
        self.industry_combo.clear()
        self.industry_combo.addItems(industry_values)
        if current_industry in industry_values:
            self.industry_combo.setCurrentText(current_industry)
        self.industry_combo.blockSignals(False)

        tag_values = [ALL_OPTION, *tags]
        current_tag = self.tag_combo.currentText()
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItems(tag_values)
        if current_tag in tag_values:
            self.tag_combo.setCurrentText(current_tag)
        self.tag_combo.blockSignals(False)

    # -------------------------------------------------------------- 收件對象

    def _current_filter(self) -> CompanyFilter:
        industry = self.industry_combo.currentText()
        stage = self.stage_combo.currentText()
        tag = self.tag_combo.currentText()
        return CompanyFilter(
            industry=None if industry == ALL_OPTION else industry,
            stages=[] if stage == ALL_OPTION else [to_value(stage, STAGE_LABELS)],
            tags=[] if tag == ALL_OPTION else [tag],
            email_verdicts=[EmailVerdict.VALID.value] if self.verified_only_check.isChecked() else [],
        )

    def _start_build_plan(self) -> None:
        if self.build_task.running:
            return

        template_name = self.template_combo.currentText().strip()
        if not template_name:
            self.status("請先選擇一個樣板", "error")
            return
        if not self._attachments_ok:
            self.status("附件總量超過上限，請先取消勾選幾個檔案", "error")
            return
        campaign_name = self.campaign_name_entry.get() or template_name
        criteria = self._current_filter()
        attachments = self.selected_attachments()

        self.build_plan_button.setEnabled(False)
        self.app.status_bar.start_progress()
        self.status("正在產生寄送名單...", "normal")

        self.build_task.start(
            criteria, template_name, campaign_name, attachments=attachments
        )

    def _on_build_plan_done(self, plan: Any) -> None:
        self.plan = plan
        self.build_plan_button.setEnabled(True)
        self.app.status_bar.stop_progress()

        rows = []
        for recipient in plan.recipients:
            if recipient.will_send:
                status_text = "會寄送"
            else:
                reason = getattr(recipient.skip_reason, "value", str(recipient.skip_reason))
                status_text = f"略過：{reason}"
            rows.append(
                {
                    "company_name": recipient.company_name,
                    "to_address": recipient.to_address,
                    "status": status_text,
                }
            )
        self.table.set_rows(rows)

        reason_summary = "；".join(
            f"{reason}：{count} 家" for reason, count in plan.skip_counts.items()
        )
        summary = f"共 {len(plan.recipients)} 家，可寄送 {plan.sendable} 家，略過 {plan.skipped} 家。"
        if reason_summary:
            summary += f"（{reason_summary}）"
        summary += f" 今日寄送額度剩餘 {plan.daily_remaining} 封。"
        # 附件寫進名單摘要，使用者按「開始寄送」前看得到自己到底要寄出什麼。
        if getattr(plan, "attachments", None):
            summary += f" 附件：{('、'.join(plan.attachments))}。"
        self.summary_label.setText(summary)

        self._update_action_buttons()
        self.status(f"已產生名單：共 {len(plan.recipients)} 家", "success")

    def _on_build_plan_error(self, exc: Exception) -> None:
        self.build_plan_button.setEnabled(True)
        self.app.status_bar.stop_progress()
        self.report_error(exc)

    # -------------------------------------------------------------- 預覽

    def _preview_first(self) -> None:
        if self.plan is None:
            self.status("請先產生名單", "error")
            return
        try:
            preview = self.controller.preview_first(self.plan)
        except CRMError as exc:
            self.report_error(exc)
            return
        if preview is None:
            self.status("目前沒有可寄送的收件者", "error")
            return
        subject, body = preview
        dialog = PreviewDialog(self, subject, body, self.controller.config)
        dialog.exec()

    # -------------------------------------------------------------- 寄送

    def _start_send(self, *, force_dry_run: bool) -> None:
        if self.send_task.running:
            return
        if self.plan is None or getattr(self.plan, "sendable", 0) == 0:
            self.status("請先產生一份包含可寄送對象的名單", "error")
            return

        if not force_dry_run:
            if not self._mailer_ready:
                self.status("寄件帳號尚未就緒，無法寄送", "error")
                return
            try:
                status = self.controller.mailer_status()
            except CRMError as exc:
                self.report_error(exc)
                return
            confirmed = (
                QMessageBox.question(
                    self,
                    "確認寄送",
                    (
                        f"即將寄出開發信給 {self.plan.sendable} 家公司。\n"
                        f"寄件帳號：{status['address'] or '（尚未設定）'}\n"
                        f"每封間隔：{status['delay_seconds']} 秒\n\n"
                        "請確認你有正當來源取得這些聯絡資訊，並已遵守相關法規（例如允許收件者"
                        "隨時取消訂閱）。\n\n"
                        "確定要開始寄送嗎？"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.Yes
            )
            if not confirmed:
                return

        self.preview_button.setEnabled(False)
        self.send_button.setEnabled(False)
        self.dry_run_button.setEnabled(False)
        self.cancel_send_button.setEnabled(True)
        self.app.status_bar.start_progress()
        self.status("演練中..." if force_dry_run else "寄送中...", "normal")
        self.result_label.setText("")

        self.send_task.start(self.plan, force_dry_run=force_dry_run)

    def _cancel_send(self) -> None:
        if self.send_task.running:
            self.send_task.cancel()
            self.status("取消中...", "muted")
            self.cancel_send_button.setEnabled(False)

    def _on_send_progress(self, payload: dict[str, Any]) -> None:
        self.status(
            f"已處理 {payload.get('company_name', '')}（{payload.get('status', '')}）", "normal"
        )

    def _on_send_done(self, result: Any) -> None:
        self._finish_send()
        mode = "演練" if result.dry_run else "寄送"
        message = (
            f"{mode}完成：成功 {result.sent} 封，失敗 {result.failed} 封，"
            f"略過 {result.skipped} 封。"
        )
        if result.errors:
            shown = "；".join(result.errors[:3])
            message += f" 錯誤：{shown}"
            if len(result.errors) > 3:
                message += f"（等共 {len(result.errors)} 筆）"
        colour = theme.SUCCESS if result.failed == 0 else theme.DANGER
        self.result_label.setStyleSheet(f"color: {theme.pick(colour)};")
        self.result_label.setText(message)
        self.status(message, "success" if result.failed == 0 else "error")
        self._refresh_status()

    def _on_send_error(self, exc: Exception) -> None:
        self._finish_send()
        self.report_error(exc)

    def _finish_send(self) -> None:
        self.cancel_send_button.setEnabled(False)
        self.app.status_bar.stop_progress()
        self._update_action_buttons()


class PreviewDialog(QDialog):
    """唯讀預覽：這份內容如果被寄出去，收件者會看到的樣子。對應 Tk 版
    ``PreviewDialog``。"""

    def __init__(
        self, parent: QWidget | None, subject: str, body: str, config=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("預覽第一封")
        self.resize(560, 540)
        self.setMinimumSize(420, 360)

        layout = QVBoxLayout(self)

        subject_label = QLabel(f"主旨：{subject}")
        subject_font = subject_label.font()
        subject_font.setBold(True)
        subject_label.setFont(subject_font)
        subject_label.setWordWrap(True)
        layout.addWidget(subject_label)

        self.body_view = QTextEdit()
        populate_preview(self.body_view, body, config)
        layout.addWidget(self.body_view, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_button = QPushButton("關閉")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        layout.addLayout(footer)
