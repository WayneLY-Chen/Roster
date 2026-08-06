"""信件內文的編輯器：PySide6 版，直接建立在 ``QTextEdit`` 之上。

## 跟 ``gui/composer.py``（Tk 版）的差異

Tk 版是手刻的富文本編輯器：拿 ``tk.Text`` 的 **tag** 當文件模型，存檔時用
``Text.dump()`` 把 tag 開關與圖片位置的事件流走一遍、轉成一小段自訂的 HTML
子集（:mod:`gmail.richtext`），全部都是為了遷就 Tk 沒有內建所見即所得編輯器
才存在的機制。

``QTextEdit`` 本身就是一個完整的富文本編輯器：粗體/斜體/底線/清單/連結/內嵌
圖片都是內建功能，``toHtml()`` 直接吐出可用的 HTML，``setHtml()`` 也讀得懂
一般的 HTML 子集（包含 :mod:`gmail.richtext` 產生的 ``<strong>``/``<em>``/
``<u>``/``<h2>``/``<h3>``/``<ul><li>``/``<a href>``/``<img src>`` 這些標籤，
所以舊樣板檔案不必轉檔就能在這裡正常打開）。這個檔案不重新發明那一套機制，
只是薄薄地包一層工具列跟圖片管理。

## 這份 HTML 要怎麼餵給 gmail/sender.py 現成的邏輯（沒有改 gmail/ 任何一行）

``gmail/sender.py`` 的 ``_set_body()``／``_resolve_images()`` 認得的內文格式，
拆開來看只有三個要求：

    1. ``looks_like_html()``（:mod:`gmail.richtext`）能判斷這段字串是不是
       HTML——它只是找有沒有出現 ``p|br|div|ul|li|h[1-6]|strong|b|em|i|u|a|img``
       這幾個標籤之一，不管標籤上的屬性/style 寫什麼。
    2. 圖片一定要用 ``<img src="images/檔名">`` 這種相對路徑指到樣板資料夾
       旁邊的 ``images/`` 目錄，``_resolve_images()`` 用一個正規表示式
       ``r'<img\\s+src="([^"]*)"'`` 找出每一個 ``src``，再拿 ``Path(source).name``
       取檔名、限制在 ``images_dir`` 底下讀取，最後轉成 CID 附件寄出
       （Gmail 會擋 ``data:`` URI，所以不能內嵌 base64）。
    3. ``html_to_plain_text()``（給純文字備援內容用）用一個很簡單的
       ``HTMLParser`` 子類別把同一段 HTML 轉回純文字，只認得上面那幾個
       標籤，不認得的標籤會被忽略（但保留裡面的文字）。

``QTextEdit.toHtml()`` 預設吐出來的是一整份文件（``<!DOCTYPE ...><html><head>
<style>...</style></head><body ...>...</body></html>``），格式（粗體等）是用
``<span style="font-weight:700;">`` 而不是 ``<strong>``——這對「判斷是不是
HTML」沒有影響（``<p>`` 標籤本身就會命中），但 ``<head>`` 裡的 ``<style>``
內容會被上面第 3 點的簡易 parser 當成普通文字整段吃進純文字版本裡（``<style>``
是 CDATA 內容，parser 不會把它當標籤處理）。所以這裡一律只取
``<body>...</body>`` 的**內層片段**（:func:`extract_body_fragment`）當作
「內文」本身，不含外層 ``<html>``/``<head>``——這也剛好是
``document_to_html()`` 原本產生的形狀，兩者完全相容。

圖片走 :meth:`QTextCursor.insertImage`：資源名稱刻意設成 ``images/<檔名>``
（透過 ``QTextDocument.addResource()`` 登記），這樣 ``toHtml()`` 吐出來的
``<img src="images/檔名.png" width="..." height="..." />`` 裡，``src`` 精確
就是 ``_resolve_images()`` 認得的相對路徑格式（已經用真實圖片＋
``gmail.sender.SmtpSender._resolve_images()`` 實測驗證過，見
``tests/test_gui_qt_composer.py``）。

## 純文字 vs. 有格式

沒有任何粗體/斜體/底線/清單/連結/圖片/放大字級的內容，回傳的是
``toPlainText()``（保留多行斷行），跟舊版存起來的樣式一致；只要用到其中
任何一種格式，才回傳 HTML 片段。理由：把「使用者根本沒格式化過的簡單樣板」
一律包成 ``<p>...</p>`` 送出去，會讓 ``looks_like_html()`` 把它誤判成 HTML
樣板，使用者原本熟悉的「純文字、多行編輯」體驗就會被迫套上不必要的
「這份樣板含格式，請用放大編輯」提示。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
    QTextListFormat,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.logging_setup import get_logger
from gui_qt import theme
from gui_qt.widgets import WideComboBox

log = get_logger(LogCategory.GUI)

#: 內文裡最寬的圖片，超過的等比例縮小顯示——編輯區塞不下，收件者的信箱通常
#: 也是。跟 Tk 版 ``composer.MAX_IMAGE_WIDTH`` 同一個數字。只影響編輯區裡的
#: 顯示尺寸，寄送時 gmail/sender.py 讀的是磁碟上原始檔案的位元組，不受影響。
MAX_IMAGE_WIDTH = 520

#: 連結套用的顏色（亮色主題用這個值就夠了；這裡不像 gui_qt/theme.py 那樣
#: 需要跟著亮/暗模式切換，深色模式下 QTextEdit 本身的底色由 QSS 決定，這個
#: 藍色在兩種背景上都看得清楚）。
LINK_COLOUR = "#1a56b3"

#: 字級下拉可以選的值，單位是 px。
#:
#: 用 px 而不是 pt，是因為信最後是在網頁郵件裡被讀的，而 px 就是那邊的
#: 單位——``QTextEdit.toHtml()`` 對這個屬性吐出來的正是 ``font-size:20px``，
#: 使用者選幾就是幾，中間沒有換算。
#:
#: 這一組數字跟 Gmail 的字級選單同一個範圍：小到註腳，大到標題，中間不放
#: 每一個整數——選單太長反而找不到想要的那一個。
FONT_SIZES_PX = (10, 11, 12, 13, 14, 16, 18, 20, 24, 28, 32, 36)

#: 沒有指定字級時，網頁郵件實際會用的大小。下拉要停在這一格。
DEFAULT_FONT_SIZE_PX = 14


_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
_IMG_SRC_RE = re.compile(r'<img\s+[^>]*?src="([^"]+)"', re.IGNORECASE)


def images_dir(config: AppConfig) -> Path:
    """樣板圖片資料夾，跟 gmail/sender.py 的 ``_resolve_images()`` 讀的是同一個。"""
    target = config.mailer.resolved_templates_dir / "images"
    target.mkdir(parents=True, exist_ok=True)
    return target


def extract_body_fragment(html: str) -> str:
    """只取 ``<body>`` 內層片段，見檔案開頭「這份 HTML 要怎麼餵給
    gmail/sender.py」那一段——避免 ``<head>`` 裡的 ``<style>`` 內容污染純文字
    備援內容。找不到 ``<body>`` 時（例如已經是片段本身）原樣回傳。
    """
    match = _BODY_RE.search(html)
    return (match.group(1) if match else html).strip()


def register_body_images(document: QTextDocument, body: str, images: Path) -> None:
    """把 ``body`` 裡引用的每張圖片登記成 ``document`` 的資源。

    ``QTextEdit.setHtml()`` 遇到 ``<img src="...">`` 只有在事先用
    ``addResource()`` 登記過同名資源時才畫得出圖片，否則會是一個破圖示。
    讀不到的檔案（被刪除、改名）略過即可，不讓一張壞掉的圖片擋掉整份內文的
    載入——跟 Tk 版 ``_load_photo()`` 讀不到就顯示 ``[圖片：檔名]`` 的用意
    一樣，只是這裡改成保留原始 ``<img>`` 標記讓它顯示成破圖示。
    """
    for name in dict.fromkeys(_IMG_SRC_RE.findall(body)):  # 去重、保留原順序
        if name.startswith(("http://", "https://", "cid:", "data:")):
            continue
        path = images / Path(name).name
        image = QImage(str(path))
        if image.isNull():
            log.warning("信件圖片讀不到，編輯器裡顯示不出來：{}", name)
            continue
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)


def populate_preview(edit: QTextEdit, body: str, config: AppConfig | None = None) -> None:
    """把 ``body`` 畫進一個唯讀的 ``QTextEdit``，給「預覽第一封」這類用途用。"""
    from gmail.richtext import looks_like_html

    cfg = config or get_config()
    edit.clear()
    register_body_images(edit.document(), body or "", images_dir(cfg))
    if looks_like_html(body or ""):
        edit.setHtml(body)
    else:
        edit.setPlainText(body or "")
    edit.setReadOnly(True)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


class RichTextEditor(QWidget):
    """工具列 + ``QTextEdit``，內文編輯的核心元件。

    郵件頁內嵌一顆（直接編輯用）、放大視窗（:class:`ComposerDialog`）裡又用
    一顆——兩邊共用同一個類別，格式操作跟輸出格式才不會兜不起來。

    ``show_toolbar=False``：不建立那排 9 顆格式按鈕。郵件頁把這顆編輯器塞在
    「郵件樣板」跟「收件對象」左右並排的窄面板裡，9 顆按鈕排成一列需要的
    寬度（比照 Tk 版每顆按鈕的寬度算，加總遠遠超過 700px）會把這個面板的
    最小寬度硬撐大，擠壓旁邊「收件對象」表格的可用寬度，這正是「信箱」欄
    被截斷看不到的根因之一；同時工具列列自己需要的高度也會跟下面「插入
    變數」那一列搶垂直空間，兩者才會疊在一起。格式功能本身完全沒有少：
    所有 ``_toggle_*``/``_insert_*`` 方法都還在，「放大編輯」開的
    :class:`ComposerDialog` 一律用完整工具列（``show_toolbar`` 預設
    ``True``），視窗夠寬，不必省。
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
        show_toolbar: bool = True,
    ) -> None:
        super().__init__(parent)
        self.config_data = config or get_config()
        self._images_dir = images_dir(self.config_data)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if show_toolbar:
            layout.addLayout(self._build_toolbar())

        self.edit = QTextEdit()
        self.edit.setAcceptRichText(True)
        #: 目前介面字型的預設字級，判斷「是不是標題」（放大字級）跟「清除
        #: 格式」都需要一個基準值可以比較。
        self._base_point_size = self.edit.font().pointSizeF() or 10.0
        layout.addWidget(self.edit, 1)

        # 下拉要跟著游標走：點到一段 24px 的字，選單就該停在 24 px。不同步的
        # 話使用者看到的永遠是上一次選的那個數字，等於在騙他。
        if show_toolbar:
            self.edit.cursorPositionChanged.connect(self._sync_size_combo)
            self._sync_size_combo()

    # --------------------------------------------------------------- 工具列

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        # 字級。用 px 而不是 pt：收信的一端是網頁郵件，而 ``toHtml()`` 對這個
        # 屬性吐出來的正是 ``font-size:20px``——寫幾就是幾，不必在腦袋裡換算。
        self.size_combo = WideComboBox()
        self.size_combo.addItems([f"{size} px" for size in FONT_SIZES_PX])
        self.size_combo.setToolTip("字級（會套用到選取的文字）")
        self.size_combo.setFixedHeight(theme.toolbar_button_height())
        self.size_combo.activated.connect(self._apply_font_size)
        bar.addWidget(self.size_combo)

        buttons = (
            ("粗體", self._toggle_bold),
            ("斜體", self._toggle_italic),
            ("底線", self._toggle_underline),
            ("大標題", lambda: self._toggle_heading(1)),
            ("小標題", lambda: self._toggle_heading(2)),
            ("項目符號", self._toggle_bullet),
            ("連結", self._insert_link),
            ("插入圖片", self._insert_image),
            ("清除格式", self._clear_formatting),
        )
        for text, handler in buttons:
            button = QPushButton(text)
            button.setFixedHeight(theme.toolbar_button_height())
            button.clicked.connect(handler)
            bar.addWidget(button)
        bar.addStretch(1)
        return bar

    # --------------------------------------------------------------- 內容存取

    def set_body(self, body: str) -> None:
        """載入 ``body``（純文字或 HTML 皆可）。圖片資源會先登記好才顯示得出來。"""
        from gmail.richtext import looks_like_html

        self.edit.clear()
        register_body_images(self.edit.document(), body or "", self._images_dir)
        if looks_like_html(body or ""):
            self.edit.setHtml(body)
        else:
            self.edit.setPlainText(body or "")

    def to_body_string(self) -> str:
        """目前編輯區的內容：有格式就是 HTML 片段，否則是純文字。"""
        if self._has_rich_formatting():
            return extract_body_fragment(self.edit.toHtml())
        return self.edit.toPlainText()

    def insert_text_at_cursor(self, text: str) -> None:
        """在目前游標位置插入一段文字（給「插入變數」用）。"""
        cursor = self.edit.textCursor()
        cursor.insertText(text)
        self.edit.setTextCursor(cursor)
        self.edit.setFocus()

    def _has_rich_formatting(self) -> bool:
        """這份內容有沒有用到任何格式：粗體/斜體/底線/連結/圖片/清單/放大字級。

        逐個 block、逐個 fragment 檢查字元格式，而不是整份直接判斷
        ``toHtml()`` 裡有沒有特定標籤——``QTextEdit`` 對「粗體」這類格式一律
        輸出成 ``<span style="font-weight:700;">``，沒有語意化標籤可以找。
        """
        document = self.edit.document()
        block = document.begin()
        while block.isValid():
            if block.textList() is not None:
                return True
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    if fmt.isImageFormat() or fmt.isAnchor():
                        return True
                    if fmt.fontWeight() >= QFont.Weight.Bold:
                        return True
                    if fmt.fontItalic() or fmt.fontUnderline():
                        return True
                    size = fmt.fontPointSize()
                    if size and abs(size - self._base_point_size) > 0.5:
                        return True
                    # 字級下拉設的是 px，不是 pt。不看這一項的話，使用者把
                    # 整封信調成 24px 之後仍然會被當成「沒有格式」用純文字
                    # 寄出去，字級一路掉光——而編輯區裡看起來明明是大的。
                    pixels = fmt.property(QTextFormat.Property.FontPixelSize)
                    if isinstance(pixels, (int, float)) and pixels > 0:
                        return True
                it += 1
            block = block.next()
        return False

    # --------------------------------------------------------------- 格式操作

    def _selection_cursor(self) -> QTextCursor | None:
        cursor = self.edit.textCursor()
        if not cursor.hasSelection():
            QMessageBox.information(self, "請先選取文字", "請先用滑鼠選取要套用格式的文字。")
            return None
        return cursor

    def _toggle_bold(self) -> None:
        cursor = self._selection_cursor()
        if cursor is None:
            return
        bold = cursor.charFormat().fontWeight() >= QFont.Weight.Bold
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Normal if bold else QFont.Weight.Bold)
        cursor.mergeCharFormat(fmt)

    def _toggle_italic(self) -> None:
        cursor = self._selection_cursor()
        if cursor is None:
            return
        fmt = QTextCharFormat()
        fmt.setFontItalic(not cursor.charFormat().fontItalic())
        cursor.mergeCharFormat(fmt)

    def _toggle_underline(self) -> None:
        cursor = self._selection_cursor()
        if cursor is None:
            return
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not cursor.charFormat().fontUnderline())
        cursor.mergeCharFormat(fmt)

    def _line_or_selection_cursor(self) -> QTextCursor:
        """有選取就用選取範圍；否則套用到游標所在的整行（跟 Tk 版標題/項目
        符號按鈕的行為一致：不強制要求先選字）。
        """
        cursor = self.edit.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        return cursor

    def _heading_size(self, level: int) -> float:
        return self._base_point_size + (7 if level == 1 else 3)

    def _toggle_heading(self, level: int) -> None:
        """整行套用大/小標題；已經是該級標題時再按一次會還原成內文字級。"""
        cursor = self._line_or_selection_cursor()
        target = self._heading_size(level)
        current = cursor.charFormat()
        already = (
            current.fontWeight() >= QFont.Weight.Bold
            and bool(current.fontPointSize())
            and abs(current.fontPointSize() - target) < 0.5
        )
        fmt = QTextCharFormat()
        if already:
            fmt.setFontWeight(QFont.Weight.Normal)
            fmt.setFontPointSize(self._base_point_size)
        else:
            fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setFontPointSize(target)
        # 標題用 pt，字級下拉用 px。兩個屬性同時掛在同一段字上時誰贏要看 Qt
        # 的心情，所以套標題就把 px 清掉——按了「大標題」卻沒有變大，是最難
        # 查的那種問題。
        fmt.setProperty(QTextFormat.Property.FontPixelSize, 0)
        cursor.mergeCharFormat(fmt)

    # --------------------------------------------------------------- 字級

    def _apply_font_size(self, index: int) -> None:
        """把選到的字級套上去。

        有選字就套在選取範圍上；沒選字就設定「接下來打的字」——跟 Gmail 一樣。
        不強迫先選字：使用者常常是先把字級調好再開始打。
        """
        if not 0 <= index < len(FONT_SIZES_PX):
            return
        pixels = FONT_SIZES_PX[index]
        fmt = QTextCharFormat()
        fmt.setProperty(QTextFormat.Property.FontPixelSize, pixels)
        # pt 也要一起清掉。大/小標題設的是 pt，兩個屬性同時存在時到底以哪個
        # 為準要看 Qt 的心情——留一個就不必猜。
        fmt.setFontPointSize(0)

        cursor = self.edit.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        self.edit.mergeCurrentCharFormat(fmt)
        self.edit.setFocus()

    def _current_font_size_px(self) -> int:
        """游標所在位置的字級（px）。沒有明確設過就回預設值。"""
        fmt = self.edit.textCursor().charFormat()
        pixels = fmt.property(QTextFormat.Property.FontPixelSize)
        if isinstance(pixels, (int, float)) and pixels > 0:
            return int(pixels)
        points = fmt.fontPointSize()
        if points:
            # 大/小標題是用 pt 設的。換算成 px 只是為了讓下拉停在最接近的
            # 那一格，不會反過來去改文件裡的值。
            return int(round(points * 96 / 72))
        return DEFAULT_FONT_SIZE_PX

    def _sync_size_combo(self) -> None:
        current = self._current_font_size_px()
        closest = min(
            range(len(FONT_SIZES_PX)),
            key=lambda i: abs(FONT_SIZES_PX[i] - current),
        )
        self.size_combo.blockSignals(True)
        self.size_combo.setCurrentIndex(closest)
        self.size_combo.blockSignals(False)

    def _toggle_bullet(self) -> None:
        """選取範圍（或目前這行）已經是清單就移出清單，否則建立一個。"""
        cursor = self._line_or_selection_cursor()
        current_list = cursor.currentList()
        if current_list is not None:
            start = min(cursor.selectionStart(), cursor.selectionEnd())
            end = max(cursor.selectionStart(), cursor.selectionEnd())
            block = self.edit.document().findBlock(start)
            while block.isValid() and block.position() <= end:
                block_list = block.textList()
                if block_list is not None:
                    block_list.remove(block)
                block = block.next()
        else:
            list_fmt = QTextListFormat()
            list_fmt.setStyle(QTextListFormat.Style.ListDisc)
            cursor.createList(list_fmt)

    def _insert_link(self) -> None:
        cursor = self._selection_cursor()
        if cursor is None:
            return
        url, ok = QInputDialog.getText(self, "插入連結", "請輸入網址：")
        if not ok or not url.strip():
            return
        url = url.strip()
        if not url.startswith(("http://", "https://", "mailto:")):
            url = "https://" + url
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setForeground(QColor(LINK_COLOUR))
        fmt.setFontUnderline(True)
        cursor.mergeCharFormat(fmt)

    def _clear_formatting(self) -> None:
        cursor = self._selection_cursor()
        if cursor is None:
            return
        # setCharFormat()（不是 mergeCharFormat()）整個取代掉選取範圍的格式，
        # 才會真的清掉粗體/底線/連結，而不是疊加一個「空白」格式上去。
        cursor.setCharFormat(QTextCharFormat())

    # --------------------------------------------------------------- 圖片

    def _insert_image(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "選擇圖片", "", "圖片檔 (*.png *.jpg *.jpeg *.gif *.bmp);;所有檔案 (*.*)"
        )
        if not chosen:
            return
        try:
            filename = self._store_image(Path(chosen))
        except OSError as exc:
            QMessageBox.critical(self, "插入圖片失敗", str(exc))
            return
        self._place_image(filename)

    def _store_image(self, source: Path) -> str:
        """把選到的圖片複製進樣板資料夾，回傳存起來的檔名。

        使用者選的往往是桌面上隨手放的檔案，過幾天就被刪或改名了；複製一份
        進來，樣板才不會哪天突然少一張圖。跟 Tk 版 ``_store_image()`` 邏輯
        一致：檔名衝突時加上流水號，同一個檔案再插入一次不會重複複製。
        """
        target = self._images_dir / source.name
        index = 1
        while target.exists() and not _same_file(source, target):
            target = self._images_dir / f"{source.stem}-{index}{source.suffix}"
            index += 1
        if not target.exists():
            shutil.copy2(source, target)
        return target.name

    def _place_image(self, filename: str) -> None:
        path = self._images_dir / filename
        image = QImage(str(path))
        cursor = self.edit.textCursor()
        if image.isNull():
            # 顯示不出來也要留住它，否則存檔後圖片就從內文裡消失了。
            cursor.insertText(f"[圖片：{filename}]")
            return
        if image.width() > MAX_IMAGE_WIDTH:
            image = image.scaledToWidth(
                MAX_IMAGE_WIDTH, Qt.TransformationMode.SmoothTransformation
            )
        # 資源名稱刻意用 "images/<檔名>"：toHtml() 吐出來的 <img src="..."> 就會
        # 精確是這個字串，跟 gmail/sender.py 認得的相對路徑格式一致。
        resource_name = f"images/{filename}"
        self.edit.document().addResource(
            QTextDocument.ResourceType.ImageResource, QUrl(resource_name), image
        )
        image_fmt = QTextImageFormat()
        image_fmt.setName(resource_name)
        image_fmt.setWidth(image.width())
        image_fmt.setHeight(image.height())
        cursor.insertImage(image_fmt)
        self.edit.setTextCursor(cursor)


class ComposerDialog(QDialog):
    """信件內文的放大編輯視窗，對應 Tk 版 ``BodyComposer``。"""

    def __init__(
        self,
        parent: QWidget | None,
        body: str = "",
        config: AppConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯信件內文")
        self.resize(900, 680)
        self.setMinimumSize(780, 420)

        layout = QVBoxLayout(self)
        self.editor = RichTextEditor(config, self)
        layout.addWidget(self.editor, 1)

        hint = QLabel("選取文字後再按格式按鈕。{公司名稱} 這類變數照樣可以用。")
        hint.setObjectName("MutedLabel")
        layout.addWidget(hint)

        footer = QHBoxLayout()
        footer.addStretch(1)
        apply_button = QPushButton("套用")
        apply_button.clicked.connect(self.accept)
        footer.addWidget(apply_button)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        footer.addWidget(cancel_button)
        layout.addLayout(footer)

        self.editor.set_body(body)
        self._result: str | None = None

    def accept(self) -> None:  # noqa: D102 - QDialog 的覆寫方法
        self._result = self.editor.to_body_string()
        super().accept()

    def result_body(self) -> str | None:
        """按下「套用」時是編輯後的內文；取消則是 ``None``。"""
        return self._result


def edit_body(
    parent: QWidget, body: str, config: AppConfig | None = None
) -> str | None:
    """開啟放大編輯視窗並等它關閉（``exec()`` 是同步呼叫）。

    回傳新的內文；使用者按「取消」或直接關掉視窗則回傳 ``None``。
    """
    dialog = ComposerDialog(parent, body, config)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.result_body()
    return None
