"""反饋頁：使用者直接從程式裡把問題回報給作者。

## 為什麼是獨立的一頁而不是「設定」裡的一個區塊

回報問題不是一種設定。使用者是在某個地方卡住之後才想回報的，那個當下他要
的是「哪裡可以講」，而不是「捲到設定頁最下面」。側邊欄看得到才找得到。

## 為什麼有兩條寄送路徑

直接用 SMTP 寄需要使用者已經設定好自己的 Gmail。但會想回報的人，很大一部分
正是「還沒設定起來」或「設定失敗」的那一群——要求他們先設定完 Gmail 才能
回報「Gmail 設定不起來」，這個功能就等於不存在。細節見 :mod:`core.feedback`。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.errors import CRMError
from controllers.mail import MailController
from gui_qt import theme
from gui_qt.pages.base import BasePage
from gui_qt.widgets import LabeledEntry, Section, caption


class FeedbackPage(BasePage):
    title = "反饋"
    icon = "💬"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        #: 借用郵件的附件庫存截圖——已經有複製進 attachments/、驗大小、
        #: 擋路徑穿越的完整實作，沒有理由為了截圖再寫一份。
        self.mail_controller = MailController()
        self._attachments: list[str] = []

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        title_label = QLabel("反饋")
        title_font = title_label.font()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 6, 0)
        body_layout.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        section = Section("告訴作者哪裡有問題")
        body_layout.addWidget(section)

        intro = QLabel(
            "遇到問題、有建議、或哪裡怪怪的，都可以從這裡直接告訴作者。"
            "版本與作業系統資訊會自動附上，不含你的資料夾路徑。"
        )
        intro.setObjectName("MutedLabel")
        intro.setWordWrap(True)
        section.body_layout.addWidget(intro)

        self.reply_entry = LabeledEntry(
            "你的信箱（選填，想收到回覆再填）", placeholder="you@example.com"
        )
        section.body_layout.addWidget(self.reply_entry)

        section.body_layout.addWidget(caption("問題或建議"))
        self.message_box = QPlainTextEdit()
        self.message_box.setPlaceholderText(
            "發生什麼事？做了哪些操作之後出現的？預期應該要怎樣？"
        )
        self.message_box.setMinimumHeight(theme.text_box_height(8))
        section.body_layout.addWidget(self.message_box, 1)

        attach_row = QHBoxLayout()
        attach_row.addWidget(caption("截圖或檔案（選填）"))
        attach_row.addStretch(1)
        attach_button = QPushButton("加入截圖／檔案")
        attach_button.clicked.connect(self._add_attachment)
        attach_row.addWidget(attach_button)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear_attachments)
        attach_row.addWidget(clear_button)
        section.body_layout.addLayout(attach_row)

        self.attachment_label = QLabel("尚未加入任何檔案。")
        self.attachment_label.setObjectName("MutedLabel")
        self.attachment_label.setWordWrap(True)
        section.body_layout.addWidget(self.attachment_label)

        send_row = QHBoxLayout()
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("MutedLabel")
        self.hint_label.setWordWrap(True)
        send_row.addWidget(self.hint_label, 1)
        self.send_button = QPushButton("送出反饋")
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.clicked.connect(self._send)
        send_row.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(send_row)

        issue_note = QLabel(
            "也可以直接到 GitHub 開一則 issue（需要 GitHub 帳號）。"
        )
        issue_note.setObjectName("MutedLabel")
        issue_note.setWordWrap(True)
        section.body_layout.addWidget(issue_note)

        issue_button = QPushButton("開啟 GitHub Issues")
        issue_button.clicked.connect(self._open_issues)
        issue_row = QHBoxLayout()
        issue_row.addWidget(issue_button)
        issue_row.addStretch(1)
        section.body_layout.addLayout(issue_row)

    # ------------------------------------------------------------- 生命週期

    def refresh(self) -> None:
        """每次進到這一頁都重新判斷寄送路徑。

        使用者很可能是「剛剛才去設定頁把 Gmail 設好」，然後才回來這裡。
        """
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        from core.feedback import can_send_directly

        config = self.mail_controller.config
        if not config.app.feedback_email:
            self.hint_label.setText(
                "作者沒有公開回報信箱，「送出反饋」會開啟 GitHub 的 issue 頁面。"
            )
        elif can_send_directly(config):
            self.hint_label.setText("會用你在「設定」頁設好的 Gmail 帳號寄出。")
        else:
            # 講清楚附件帶不過去。mailto: 沒有帶附件的標準做法，讓使用者以為
            # 截圖跟著寄出去了，比一開始就說沒辦法更糟。
            #
            # 這裡不要用 **粗體** 之類的 Markdown 語法——QLabel 預設是純文字，
            # 星號會原封不動印在畫面上。
            self.hint_label.setText(
                "尚未設定 Gmail 帳號，按下去會開啟你自己的郵件軟體並填好內容。"
                "注意：截圖要在郵件軟體裡自己拖進去。"
            )

    # --------------------------------------------------------------- 附件

    def _add_attachment(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "選擇截圖或檔案",
            "",
            "圖片與文件 (*.png *.jpg *.jpeg *.gif *.pdf *.txt *.log);;所有檔案 (*.*)",
        )
        if not paths:
            return
        for path in paths:
            try:
                stored = self.mail_controller.add_attachment(path)
            except CRMError as exc:
                self.report_error(exc)
                continue
            self._attachments.append(stored.name)
        self._update_attachment_label()

    def _clear_attachments(self) -> None:
        self._attachments = []
        self._update_attachment_label()

    def _update_attachment_label(self) -> None:
        if not self._attachments:
            self.attachment_label.setText("尚未加入任何檔案。")
            return
        self.attachment_label.setText(
            "已加入："
            + "、".join(self._attachments)
            + "。檔案會存進 attachments/，送出後可以到「郵件」頁的附件管理刪掉。"
        )

    # --------------------------------------------------------------- 送出

    def _open_issues(self) -> None:
        QDesktopServices.openUrl(QUrl(self.mail_controller.config.app.issue_url))

    def _send(self) -> None:
        from core.feedback import Feedback, can_send_directly, mailto_url, send, validate

        config = self.mail_controller.config
        feedback = Feedback(
            message=self.message_box.toPlainText(),
            reply_to=self.reply_entry.get(),
            attachments=list(self._attachments),
        )

        # 在這裡先驗一次，不要只靠底下的 send()/mailto_url() 各自去驗。
        # 兩條路徑的驗證時機不一樣，錯誤訊息出現的時機就會不一致；而且
        # 「開啟 GitHub issue」那條路完全不經過它們。
        try:
            validate(feedback)
        except CRMError as exc:
            self.report_error(exc)
            return

        if not config.app.feedback_email:
            self._open_issues()
            self.status("已開啟 GitHub issue 頁面", "success")
            return

        try:
            if can_send_directly(config):
                send(feedback, config)
                self.status("反饋已寄出，謝謝你的回報", "success")
            else:
                QDesktopServices.openUrl(QUrl(mailto_url(feedback, config)))
                self.status("已開啟郵件軟體，截圖請自行附加後寄出", "success")
        except CRMError as exc:
            self.report_error(exc)
            return

        self.message_box.clear()
        self._clear_attachments()
