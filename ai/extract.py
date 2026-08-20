"""從一頁網頁裡把公司資料抽出來。

## 這個模組的立場：模型指出資料在哪裡，頁面才是資料的來源

抽資料時最危險的失敗不是漏抓，是**補一個看起來很合理的信箱**。漏抓看得出來
（那一格是空的）；編出來的看不出來——它格式正確、網域也像那家公司，混在三千
筆名單裡沒有人會發現，直到寄出去大量退信。

所以模型回來的每一個值，都會回頭到**原始頁面文字**裡對一次，對不到就丟掉並
記進 :class:`ExtractResult` 讓使用者看得到。公司名稱對不回去的話整筆丟掉——
那通常代表模型在「整理」而不是在「抄寫」。

這一關擋不掉的只有一種：頁面上真的有那個字串，但模型把它配到錯的公司。那還
在「頁面上的資料」範圍內，而且使用者在預覽表格上看得出來。

## 這個模組不抓網頁

一次都不會。HTML 是呼叫端（:mod:`controllers.ai`）用 :mod:`crawler.fetcher`
抓好之後傳進來的，而那一層才有 robots.txt 檢查與請求間隔。理由見
:mod:`ai.prompts`。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ai.prompts import EXTRACT_SYSTEM_PROMPT
from ai.provider import ChatMessage
from core.constants import LogCategory
from core.errors import AIError
from core.logging_setup import get_logger
from core.schemas import RawCompany

log = get_logger(LogCategory.GUI)

#: 送給模型的頁面文字上限。
#:
#: 這不是「模型吃得下多少」，是「一次要付多少」。名錄頁的純文字通常兩三萬
#: 字元就涵蓋整頁，再長多半是頁尾的法律條文與導覽選單——那些字對抽公司資料
#: 沒有任何幫助，但每一個字都要付錢（OpenRouter）或等時間（本機模型）。
#: 超過就截掉，而且**一定要告訴使用者截掉了**，否則他會以為那一頁抓完了。
MAX_PAGE_CHARS = 24_000

#: 一頁最多接受幾筆。
#:
#: 模型偶爾會陷入重複輸出的迴圈（同一家公司連續吐幾百次），這是止血點。
#: 真的有超過 300 家公司的單頁名錄極少見，而那種頁面本來就該用「爬取」頁
#: 設一個正式的來源去跑，不是靠模型讀一次。
MAX_RECORDS = 300

#: 抽取時一次回覆的 token 上限。
#:
#: 聊天用的 ``ai.max_output_tokens`` 預設 2048，那是「一段回話」的長度；
#: 一頁 200 筆的 JSON 遠遠不止。取兩者較大值：使用者自己調高過就尊重他的
#: 設定，沒調過的話至少要有這個數字，否則 JSON 會在中間被切斷。
EXTRACT_MAX_TOKENS = 8192

#: 這一批資料在「公司資訊」頁的來源標記。
AI_SOURCE = "ai"

#: 讓模型填的欄位，以及顯示用的中文標籤。
#:
#: 刻意只開這幾個，而且每一個都是**頁面上會原樣印出來的字串**。欄位開越多，
#: 模型越傾向「每一格都填點東西」，而那正是編造的來源。
#:
#: 產業、主要產品這類要「判斷」才寫得出來的欄位一律不開——它們照定義就對不
#: 回原文，開了只會讓使用者看到一整排被丟棄的值。聯絡人也不開：那是頁面上
#: 最常沒有、模型最愛自己填一個「業務部」上去的一格。這些欄位交給既有的
#: 補齊流程（:mod:`crawler.enrich`）去查，那些是有來源的。
EXTRACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("company_name", "公司名稱"),
    ("tax_id", "統一編號"),
    ("email", "電子信箱"),
    ("phone", "電話"),
    ("fax", "傳真"),
    ("website", "網址"),
    ("address", "地址"),
)

_FIELD_LABELS = dict(EXTRACT_FIELDS)

#: 這幾個欄位比對時要先把分隔符號拿掉。
#:
#: 電話與統編在頁面上的寫法千奇百怪（``(02)2740-7278``、``02 2740 7278``、
#: ``+886-2-2740-7278``），但數字本身是同一串。純文字欄位就不能這樣寬鬆——
#: 把標點全部拿掉之後，比對會開始穿過原本分屬兩格的內容。
_NUMERIC_FIELDS = frozenset({"tax_id", "phone", "fax"})

#: 純粹是分隔符號、不帶資訊的字元。只用在 :data:`_NUMERIC_FIELDS`。
_NUMBER_NOISE = frozenset("-()+#./,_")

#: 這些標籤裡的東西不是給人看的內容。
#:
#: ``script`` 佔的份量最誇張：一個現代網頁八成的位元組是 JavaScript，送出去
#: 只是讓使用者多付錢，而且會把真正的內容擠出 :data:`MAX_PAGE_CHARS` 之外。
#: 它還有一個更麻煩的地方：script 裡常常有信箱格式的字串（追蹤碼、範例資料），
#: 留著的話模型會把它們當成聯絡資訊，而且那種值**會通過原文比對**——它真的
#: 在頁面上，只是不在給人看的那一半。
#:
#: ``head`` 不在裡面是刻意的：``<title>`` 常常是單一公司官網上唯一寫出全名的
#: 地方，丟掉它等於讓那種頁面一筆都抽不到。head 裡其餘的標籤本來就沒有文字。
_NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "canvas")

#: 模型愛用來表示「這一格沒有」的字。它們不是資料。
_EMPTY_VALUES = frozenset({
    "", "-", "--", "n/a", "na", "null", "none", "無", "沒有", "不詳", "未提供",
    "未知", "無資料", "查無", "不適用",
})

ChatFn = Callable[[Sequence[ChatMessage]], str]
"""把一串訊息送給模型、拿回完整回覆的函式。

做成參數而不是在這裡自己 :func:`~ai.provider.get_provider`，是為了讓這個
模組在測試裡完全不必碰網路，也不必知道使用者選了哪一家供應商。
"""


# --------------------------------------------------------------- HTML 轉純文字


def html_to_text(html: str) -> str:
    """把一頁 HTML 變成給模型讀的純文字。

    每個區塊之間用換行隔開而不是空白：名錄頁的一列就是一格一格的欄位，用空白
    黏起來之後「公司名稱」與後面的地址會連成一串，模型更容易切錯。
    """
    from bs4 import Comment

    from crawler.parser import make_soup

    soup = make_soup(html)
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()

    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(re.sub(r"\s+", " ", line) for line in lines if line.strip())


def trim_for_model(text: str) -> tuple[str, bool]:
    """截到 :data:`MAX_PAGE_CHARS`。回傳 ``(文字, 有沒有被截掉)``。"""
    if len(text) <= MAX_PAGE_CHARS:
        return text, False
    return text[:MAX_PAGE_CHARS], True


# ------------------------------------------------------------------- 比對原文


def _fold(value: str) -> str:
    """比對用的折疊：吃掉空白、全形半形、大小寫與台／臺的差異。

    刻意**不**做模糊比對。折掉的每一項都是「同一個字串的不同寫法」——頁面把
    公司名稱拆在兩個標籤裡（折疊後換行消失）、地址用全形數字、信箱大小寫不
    一致。這些都還是同一串字。再寬鬆一點（例如拿掉標點、算相似度）就會開始
    放行模型自己拼出來的值，那正是這一關要擋的東西。
    """
    folded = unicodedata.normalize("NFKC", value).casefold().replace("臺", "台")
    return "".join(ch for ch in folded if not ch.isspace())


def _fold_number(value: str) -> str:
    """電話／傳真／統編用的折疊：連分隔符號也拿掉，只留數字與其他字元。"""
    return "".join(ch for ch in _fold(value) if ch not in _NUMBER_NOISE)


@dataclass(frozen=True, slots=True)
class _Haystack:
    """一頁的原始文字，兩種折疊各先算一次。

    一頁三萬字、一筆七個欄位、一頁三百筆——每次都重折一遍是兩千一百次全頁掃描。
    """

    plain: str
    numeric: str

    @classmethod
    def of(cls, page_text: str) -> "_Haystack":
        folded = _fold(page_text)
        return cls(plain=folded, numeric="".join(
            ch for ch in folded if ch not in _NUMBER_NOISE
        ))

    def contains(self, value: str, *, field_name: str) -> bool:
        if field_name in _NUMERIC_FIELDS:
            needle = _fold_number(value)
            return bool(needle) and needle in self.numeric

        needle = _fold(value)
        if not needle:
            return False
        if needle in self.plain:
            return True
        # 通訊協定前綴不是頁面上的資料，是寫法。頁面印「www.abc.com.tw」而
        # 模型補成「https://www.abc.com.tw」時，被補的那一段本來就不該算進
        # 「這個值有沒有出現過」。mailto: 同理。
        stripped = re.sub(r"^(?:https?://|mailto:)", "", needle)
        return stripped != needle and bool(stripped) and stripped in self.plain


# --------------------------------------------------------------------- 結果


@dataclass(frozen=True, slots=True)
class DroppedValue:
    """一個對不回原始頁面、因此被丟掉的值。

    這份清單就是「這個模型在這個網站上可不可信」的證據，所以它要留得住細節：
    是哪一家、哪一格、模型填了什麼。只給一個數字的話使用者沒有辦法判斷那是
    「模型把電話重新排版了」還是「模型在編信箱」。
    """

    company_name: str
    field: str
    value: str
    #: True 代表整筆被丟掉（公司名稱對不回原文）。
    whole_record: bool = False

    def describe(self) -> str:
        label = _FIELD_LABELS.get(self.field, self.field)
        if self.whole_record:
            return f"整筆丟棄：公司名稱「{self.value}」在頁面上找不到"
        who = self.company_name or "（沒有名稱）"
        return f"{who}　{label}：{self.value}"


@dataclass(slots=True)
class ExtractResult:
    """一頁抽完的結果，含所有「誠實地講出丟掉了什麼」需要的數字。"""

    records: list[RawCompany] = field(default_factory=list)
    dropped: list[DroppedValue] = field(default_factory=list)
    #: 原始頁面純文字有多長。
    page_chars: int = 0
    #: 實際送給模型的長度。
    sent_chars: int = 0
    truncated: bool = False
    #: 模型總共回了幾筆（含後來被丟掉的）。
    returned: int = 0
    #: 超過 :data:`MAX_RECORDS` 而沒有處理的筆數。
    over_limit: int = 0

    @property
    def dropped_records(self) -> int:
        return sum(1 for item in self.dropped if item.whole_record)

    @property
    def dropped_values(self) -> int:
        return sum(1 for item in self.dropped if not item.whole_record)

    def notes(self) -> list[str]:
        """畫面上要照實寫出來的每一句話。"""
        lines: list[str] = []
        if self.truncated:
            lines.append(
                f"這一頁的文字有 {self.page_chars:,} 字元，只送了前 "
                f"{self.sent_chars:,} 字元給模型，後面的沒有讀到。"
            )
        if self.over_limit:
            lines.append(
                f"模型回了 {self.returned} 筆，超過一頁 {MAX_RECORDS} 筆的上限，"
                f"後面 {self.over_limit} 筆沒有處理。"
            )
        if self.dropped_records:
            lines.append(
                f"有 {self.dropped_records} 筆的公司名稱在頁面上找不到，整筆丟棄。"
            )
        if self.dropped_values:
            lines.append(
                f"有 {self.dropped_values} 個值在原始頁面上找不到，已經丟棄。"
            )
        return lines


# ----------------------------------------------------------------- 跟模型講話


def build_messages(page_text: str, url: str) -> list[ChatMessage]:
    """組出抽取用的訊息串。

    頁面文字放在 user 訊息裡而不是 system 裡，而且前面明講它是「一頁網頁的
    內容」。這不是防線（防線是模型手上沒有連網工具，見 :mod:`ai.prompts`），
    是讓模型分得出哪一段是我們的指示、哪一段是別人網站上的字。
    """
    return [
        ChatMessage("system", EXTRACT_SYSTEM_PROMPT),
        ChatMessage(
            "user",
            f"以下是 {url} 這一頁的純文字內容。把上面刊登的公司抄出來。\n\n"
            f"----- 頁面內容開始 -----\n{page_text}\n----- 頁面內容結束 -----",
        ),
    ]


_FENCE_START = re.compile(r"^```[A-Za-z0-9_-]*\s*")
_FENCE_END = re.compile(r"\s*```$")


def parse_reply(reply: str) -> list[dict]:
    """把模型的回覆解析成一串 dict。

    刻意寬容：模型很常把 JSON 包在 markdown 的程式碼區塊裡、或在前面加一句
    「好的，以下是結果：」。那是格式問題不是資料問題，為了它整趟重跑（使用者
    要再等一次、再付一次錢）不合理。真的找不到 JSON 才報錯。
    """
    text = (reply or "").strip()
    if not text:
        raise AIError("模型沒有回任何東西。換一個模型，或稍後再試一次。")

    if text.startswith("```"):
        text = _FENCE_END.sub("", _FENCE_START.sub("", text)).strip()

    payload = _loads(text)
    if payload is None:
        preview = text[:200].replace("\n", " ")
        raise AIError(
            "模型沒有照格式回傳資料，這一頁抽不出東西。\n\n"
            f"它回的是：{preview}\n\n"
            "小模型比較容易發生這種事，換一個大一點的模型通常就好了。"
        )

    if isinstance(payload, dict):
        # 有些模型硬要包一層 {"companies": [...]}。取第一個是陣列的值。
        for value in payload.values():
            if isinstance(value, list):
                payload = value
                break
        else:
            payload = [payload]

    if not isinstance(payload, list):
        raise AIError("模型回的 JSON 不是一份清單，這一頁抽不出東西。")

    return [item for item in payload if isinstance(item, dict)]


def _loads(text: str) -> object | None:
    """試著把一段文字讀成 JSON，包含「前後有雜訊」的情況。"""
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------- 主流程


def ground(items: Sequence[dict], page_text: str, url: str) -> ExtractResult:
    """把模型回來的資料逐值對回原始頁面，對得到的才留下。

    ``page_text`` 是**完整**的頁面文字，不是截短後送給模型的那一份。截短是
    為了省錢，不該連帶讓「這個值是不是頁面上的」變得比較難通過。
    """
    result = ExtractResult(returned=len(items), page_chars=len(page_text))
    haystack = _Haystack.of(page_text)

    if len(items) > MAX_RECORDS:
        result.over_limit = len(items) - MAX_RECORDS
        items = items[:MAX_RECORDS]

    for item in items:
        record = _ground_one(item, haystack, url, result.dropped)
        if record is not None:
            result.records.append(record)

    if result.dropped:
        log.info(
            "AI 抽取 {}：模型回 {} 筆，丟掉 {} 筆、{} 個值",
            url, result.returned, result.dropped_records, result.dropped_values,
        )
    return result


def _ground_one(
    item: dict,
    haystack: _Haystack,
    url: str,
    dropped: list[DroppedValue],
) -> RawCompany | None:
    name = _clean(item.get("company_name"))
    if not name:
        return None

    if not haystack.contains(name, field_name="company_name"):
        # 名稱對不回頁面就整筆丟掉，不是只丟名稱那一格。名稱是這筆資料的
        # 身分；身分是編的，底下的電話信箱配給誰就沒有意義了。
        dropped.append(DroppedValue("", "company_name", name, whole_record=True))
        return None

    values: dict[str, str] = {}
    for field_name, _label in EXTRACT_FIELDS:
        if field_name == "company_name":
            continue
        value = _clean(item.get(field_name))
        if not value:
            continue
        if haystack.contains(value, field_name=field_name):
            values[field_name] = value
        else:
            dropped.append(DroppedValue(name, field_name, value))

    return RawCompany(
        company_name=name,
        source=AI_SOURCE,
        source_url=url,
        **values,
    )


def _clean(value: object) -> str:
    """把模型填的一格變成字串；表示「沒有」的各種寫法一律當成空的。"""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if text.casefold() in _EMPTY_VALUES else text


def extract_from_html(html: str, url: str, chat: ChatFn) -> ExtractResult:
    """一頁 HTML 進去，一份對得回原文的公司清單出來。

    ``html`` 由呼叫端事先抓好——這個模組不連網，見模組說明。
    """
    page_text = html_to_text(html)
    if not page_text.strip():
        return ExtractResult(page_chars=0, sent_chars=0)

    sent, truncated = trim_for_model(page_text)
    reply = chat(build_messages(sent, url))
    result = ground(parse_reply(reply), page_text, url)
    result.sent_chars = len(sent)
    result.truncated = truncated
    return result
