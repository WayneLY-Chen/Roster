"""在 Tk 的 ``Text`` 元件與一小段 HTML 之間互相轉換。

## 為什麼要自己寫

Tk 沒有內建的所見即所得編輯器，而信件內文需要粗體、標題、連結和圖片——使用者
說的「正常格式那樣」。可行的做法只有一種：用 ``Text`` 元件的 **tag** 當作格式，
存檔時把 tag 轉成 HTML，讀檔時再把 HTML 轉回 tag。

## 為什麼只支援一小段 HTML

支援的標籤刻意限制成下面 :data:`INLINE_TAGS` 與 :data:`BLOCK_TAGS` 這幾個。
理由不是偷懶，而是**雙向**轉換必須無損：使用者存檔、關掉、重新打開，看到的東西
要和存檔前一模一樣。任意 HTML 做不到這件事（巢狀表格、CSS、浮動排版都無法用
Text 的 tag 表達），與其做到一半在某些情況下靜靜地弄丟排版，不如一開始就只支援
確定能來回轉換的那些。

這個限制對開發信來說也夠用了——收信端（尤其是 Gmail 與 Outlook）本來就會把
複雜的 CSS 砍掉，樸素的 HTML 反而顯示得最穩。

## 圖片

圖片以檔名記錄（``<img src="images/foo.png">``），實際檔案放在樣板資料夾旁邊的
``images/``。寄送時才轉成 CID 附件——Gmail 收信端會把 ``data:`` URI 的圖片擋掉，
所以不能用內嵌 base64。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: tag 名稱 -> (開始標籤, 結束標籤)。這些會互相重疊，所以逐字元判斷。
INLINE_TAGS: dict[str, tuple[str, str]] = {
    "bold": ("<strong>", "</strong>"),
    "italic": ("<em>", "</em>"),
    "underline": ("<u>", "</u>"),
}

#: 整行套用的樣式。一行只會有一種。
BLOCK_TAGS: dict[str, tuple[str, str]] = {
    "h1": ("<h2>", "</h2>"),          # 信件內文不該出現 <h1>，那是整封信的標題
    "h2": ("<h3>", "</h3>"),
    "bullet": ("<li>", "</li>"),
}

#: 連結 tag 的前綴；完整名稱是 ``link:<網址>``。
LINK_PREFIX = "link:"

#: 圖片在內文中的佔位字元。Text 元件裡是一張真的圖，轉成 HTML 時換成 <img>。
IMAGE_PREFIX = "image:"

_ALL_STYLE_TAGS = tuple(INLINE_TAGS) + tuple(BLOCK_TAGS)


@dataclass
class Segment:
    """一段格式相同的文字。"""

    text: str
    styles: frozenset[str] = frozenset()
    link: str | None = None
    image: str | None = None


@dataclass
class Document:
    """整篇內文，切成一行一行、一段一段。"""

    lines: list[list[Segment]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            segment.text.strip() or segment.image
            for line in self.lines
            for segment in line
        )


# --------------------------------------------------------------- 轉成 HTML


def document_to_html(document: Document) -> str:
    """把 :class:`Document` 轉成 HTML。

    連續的 ``bullet`` 行會包在同一個 ``<ul>`` 裡——一行一個 ``<ul>`` 在信件中會
    被渲染成項目之間有多餘空白的清單。
    """
    out: list[str] = []
    in_list = False

    for line in document.lines:
        block = _line_block(line)

        if block == "bullet":
            if not in_list:
                out.append("<ul>")
                in_list = True
        elif in_list:
            out.append("</ul>")
            in_list = False

        inner = "".join(_segment_to_html(segment) for segment in line)

        if block in BLOCK_TAGS:
            open_tag, close_tag = BLOCK_TAGS[block]
            out.append(f"{open_tag}{inner}{close_tag}")
        elif inner.strip():
            out.append(f"<p>{inner}</p>")
        else:
            out.append("<p><br></p>")     # 保留使用者刻意留下的空行

    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _line_block(line: list[Segment]) -> str | None:
    for segment in line:
        for name in BLOCK_TAGS:
            if name in segment.styles:
                return name
    return None


def _segment_to_html(segment: Segment) -> str:
    if segment.image:
        return f'<img src="{html.escape(segment.image, quote=True)}">'

    inner = html.escape(segment.text).replace("\n", "<br>")
    if not inner:
        return ""

    for name, (open_tag, close_tag) in INLINE_TAGS.items():
        if name in segment.styles:
            inner = f"{open_tag}{inner}{close_tag}"

    if segment.link:
        inner = f'<a href="{html.escape(segment.link, quote=True)}">{inner}</a>'
    return inner


# ------------------------------------------------------------- 從 HTML 讀回


class _Reader(HTMLParser):
    """把支援的那一小段 HTML 解析回 :class:`Document`。

    不認得的標籤一律忽略但**保留其中的文字**——寧可掉格式，也不要掉內容。
    """

    _BLOCK_STARTERS = {"p", "h1", "h2", "h3", "h4", "li", "div"}
    _INLINE_BY_HTML = {
        "strong": "bold", "b": "bold",
        "em": "italic", "i": "italic",
        "u": "underline",
    }
    _BLOCK_BY_HTML = {"h2": "h1", "h3": "h2", "h4": "h2", "li": "bullet"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = Document()
        self._line: list[Segment] = []
        self._styles: list[str] = []
        self._link: str | None = None
        self._block: str | None = None
        #: 這個區塊裡是否已經送出過一行。用來分辨 ``<p><br></p>``（一個空行）
        #: 與 ``<p>A<br>B</p>``（兩行）——少了它，前者的 </p> 會再補一個空行，
        #: 每存一次檔空行就多一行。
        self._emitted = False

    # -- 行的處理 --

    def _flush_line(self) -> None:
        self.document.lines.append(self._line)
        self._line = []
        self._block = None

    def _current_styles(self) -> frozenset[str]:
        styles = set(self._styles)
        if self._block:
            styles.add(self._block)
        return frozenset(styles)

    # -- HTMLParser 介面 --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)

        if tag == "br":
            self._flush_line()
            self._emitted = True
            return
        if tag == "img":
            source = values.get("src") or ""
            if source:
                self._line.append(Segment("", self._current_styles(), image=source))
            return

        if tag in self._BLOCK_STARTERS:
            if self._line:
                self._flush_line()
            self._block = self._BLOCK_BY_HTML.get(tag)
            self._emitted = False
            return

        if tag in self._INLINE_BY_HTML:
            self._styles.append(self._INLINE_BY_HTML[tag])
        elif tag == "a":
            self._link = values.get("href") or None

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_STARTERS:
            # An empty line here is only real if the block produced nothing at
            # all; if a <br> already ended a line, this is just the paragraph
            # closing behind it.
            if self._line or not self._emitted:
                self._flush_line()
                self._emitted = True
        elif tag in self._INLINE_BY_HTML:
            style = self._INLINE_BY_HTML[tag]
            if style in self._styles:
                self._styles.remove(style)
        elif tag == "a":
            self._link = None

    def handle_data(self, data: str) -> None:
        # HTML 裡的換行與縮排只是原始碼的排版，不是內容。
        text = re.sub(r"\s+", " ", data)
        if not text.strip() and not self._line:
            return
        self._line.append(Segment(text, self._current_styles(), link=self._link))

    def close(self) -> None:  # noqa: D102
        super().close()
        if self._line:
            self._flush_line()


def html_to_document(source: str) -> Document:
    """把 :func:`document_to_html` 產生的 HTML 讀回來。

    也吃得下純文字（沒有任何標籤時，每一行就是一段沒有格式的文字），因為舊的
    樣板都是純文字存的。
    """
    if not source:
        return Document(lines=[[]])

    if not looks_like_html(source):
        return Document(
            lines=[[Segment(line)] if line else [] for line in source.split("\n")]
        )

    reader = _Reader()
    reader.feed(source)
    reader.close()
    if not reader.document.lines:
        reader.document.lines = [[]]
    return reader.document


_TAG_RE = re.compile(r"<(p|br|div|ul|li|h[1-6]|strong|b|em|i|u|a|img)\b[^>]*>", re.I)


def looks_like_html(source: str) -> bool:
    """這段內文是 HTML 還是純文字。

    只認 :data:`_TAG_RE` 裡那幾個標籤，避免把使用者寫的 ``<3`` 或
    ``價格 < 100 > 80`` 誤判成 HTML。
    """
    return bool(_TAG_RE.search(source or ""))


# ------------------------------------------------------------- 純文字版本


def html_to_plain_text(source: str) -> str:
    """給信件的 text/plain 部分用的純文字版。

    不是所有收件軟體都顯示 HTML，而且只有 HTML 的信比較容易被判定成垃圾郵件，
    所以每一封都要附一份純文字。
    """
    if not looks_like_html(source):
        return source or ""

    document = html_to_document(source)
    lines: list[str] = []
    for line in document.lines:
        block = _line_block(line)
        text = "".join(
            (f"[圖片：{segment.image}]" if segment.image else segment.text)
            for segment in line
        )
        text = text.strip()
        if block == "bullet" and text:
            text = f"・{text}"
        # 連結在純文字裡看不到 href，把網址補在後面才點得到。
        for segment in line:
            if segment.link and segment.link not in text:
                text = f"{text}（{segment.link}）"
        lines.append(text)
    return "\n".join(lines).strip()
