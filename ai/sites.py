"""給一個關鍵字，找出值得抓的網站。

## 這個模組在做什麼

搜尋層（:mod:`crawler.websearch`）回一串「標題、網址、摘要」，這裡請模型替
每一筆貼一個標籤：**名錄**（一頁很多家）、**單一公司官網**、還是**不相關**。
使用者看完標籤與理由自己勾，勾完才走 :mod:`ai.extract` 那條抽取流程。

## 兩件事在程式碼裡擋，不是靠 prompt

**一、模型不能自己生出網址。** 它拿到的是一份編號清單，回的也只能是編號——
:func:`classify` 只認編號，範圍外的直接丟掉。這不是防惡意，是防「順手補一個
看起來應該存在的網址」：那個網址接下來會被真的送出請求，變成程式對一個沒有
人要求過的網站發連線。

**二、模型不能決定要抓什麼。** 這個模組只回一份清單，一個請求都不發。真正
去抓是使用者在畫面上勾選並按下去之後的事（見 :mod:`controllers.ai`）。一個
關鍵字展開成幾十個網站、每個網站幾十頁，那是使用者要承擔的，他得先看到。

## 搜尋只走一條路

搜尋來源交給 :func:`crawler.websearch.build_search_provider` 決定，這裡不碰。
免金鑰的那條是 ``html.duckduckgo.com``——它的 robots.txt 明文 ``Allow: /``，
而 Google／Bing／Mojeek 的 ``/search`` 全是 ``Disallow``，這支程式的 fetcher
自己就會擋下來。理由寫在 :mod:`crawler.websearch` 開頭。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ai.extract import ChatFn, parse_reply
from ai.prompts import SITES_SYSTEM_PROMPT
from ai.provider import ChatMessage
from core.constants import LogCategory
from core.logging_setup import get_logger
from crawler.websearch import SearchHit

log = get_logger(LogCategory.GUI)

#: 一次拿幾筆搜尋結果給模型看。
#:
#: 搜尋引擎第一頁大約就是這個數量，而且越後面的結果越發散。多要幾頁的代價
#: 不只是 token——每一頁都是對 DuckDuckGo 的一次請求，而它會對高頻查詢回
#: 「異常流量」頁。要更多結果的正確做法是換一個更精確的關鍵字。
MAX_HITS = 20

#: 抽取用的 token 上限對這裡太浪費（一筆只要幾十個字），但預設的 2048 又擋
#: 得住 20 筆理由。取中間。
SITES_MAX_TOKENS = 3072

#: 理由最多留幾個字。模型偶爾會把一句話寫成一段，那在表格裡沒有辦法看。
MAX_REASON_CHARS = 60

#: 名錄。一頁上有很多家公司，這一類最有價值。
DIRECTORY = "directory"
#: 單一公司自己的官網。
COMPANY = "company"
#: 不相關：新聞、論壇、求職網、購物網……
UNRELATED = "unrelated"
#: 模型沒有提到這一筆。**不是**模型說它不相關，兩者要分得開。
UNJUDGED = "unjudged"

KIND_LABELS: dict[str, str] = {
    DIRECTORY: "名錄",
    COMPANY: "單一公司",
    UNRELATED: "不相關",
    UNJUDGED: "沒判斷",
}

#: 預設會勾起來的類別。
#:
#: 「不相關」與「沒判斷」預設不勾——使用者仍然看得到、也仍然勾得動，但要他
#: 主動去勾。預設全勾的話，模型判斷失準的那幾筆會安靜地變成真的請求。
DEFAULT_CHECKED = frozenset({DIRECTORY, COMPANY})


@dataclass(frozen=True, slots=True)
class Candidate:
    """一筆候選網站：搜尋結果本身，加上模型貼的標籤。"""

    url: str
    title: str = ""
    snippet: str = ""
    kind: str = UNJUDGED
    #: 模型為什麼這樣判斷。這是它的說法，不是查證過的事實——所以它顯示在
    #: 使用者眼前讓他自己決定，而不是拿來自動篩掉東西。
    reason: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def worth_crawling(self) -> bool:
        return self.kind in DEFAULT_CHECKED


@dataclass(slots=True)
class SiteSearchResult:
    """一次「用關鍵字找網站」的結果。"""

    query: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    #: 搜尋回了幾筆（含後來被模型標成不相關的）。
    found: int = 0
    #: 模型回了幾個範圍外或重複的編號。這個數字大就代表模型在亂猜。
    ignored: int = 0

    @property
    def worth_crawling(self) -> list[Candidate]:
        return [item for item in self.candidates if item.worth_crawling]

    def notes(self) -> list[str]:
        lines: list[str] = []
        if not self.candidates:
            return lines
        directories = sum(1 for c in self.candidates if c.kind == DIRECTORY)
        companies = sum(1 for c in self.candidates if c.kind == COMPANY)
        lines.append(
            f"搜到 {self.found} 筆：名錄 {directories}、單一公司 {companies}、"
            f"其餘 {self.found - directories - companies} 筆判斷為不相關或沒判斷。"
        )
        unjudged = sum(1 for c in self.candidates if c.kind == UNJUDGED)
        if unjudged:
            lines.append(
                f"其中 {unjudged} 筆模型沒有提到，預設不勾——那不等於它說不相關。"
            )
        if self.ignored:
            lines.append(
                f"模型回了 {self.ignored} 個對不上清單的編號，已經忽略。"
                "這個數字大就代表它在亂猜，換一個模型會比較準。"
            )
        return lines


def build_messages(query: str, hits: Sequence[SearchHit]) -> list[ChatMessage]:
    """組出分類用的訊息串。

    每一筆前面掛一個編號，模型回的也是編號——它沒有辦法把一個清單上沒有的
    網址塞進來，因為程式根本不讀它寫的網址。
    """
    listing = "\n\n".join(
        f"[{index}]\n標題：{hit.title or '（沒有標題）'}\n"
        f"網址：{hit.url}\n摘要：{hit.snippet or '（沒有摘要）'}"
        for index, hit in enumerate(hits)
    )
    return [
        ChatMessage("system", SITES_SYSTEM_PROMPT),
        ChatMessage(
            "user",
            f"使用者要找的是：{query}\n\n"
            f"以下是搜尋結果，替每一筆貼標籤。\n\n"
            f"----- 搜尋結果開始 -----\n{listing}\n----- 搜尋結果結束 -----",
        ),
    ]


def classify(
    hits: Sequence[SearchHit], items: Sequence[dict], query: str = ""
) -> SiteSearchResult:
    """把模型貼的標籤套回搜尋結果上。

    只認編號。模型自己寫進來的網址、範圍外的編號、重複的編號一律丟掉並計數
    ——那個數字是「這個模型可不可信」最直接的訊號。
    """
    result = SiteSearchResult(query=query, found=len(hits))
    labels: dict[int, tuple[str, str]] = {}

    for item in items:
        index = _as_index(item.get("index"))
        if index is None or not (0 <= index < len(hits)) or index in labels:
            result.ignored += 1
            continue
        labels[index] = (_as_kind(item.get("kind")), _as_reason(item.get("reason")))

    for index, hit in enumerate(hits):
        kind, reason = labels.get(index, (UNJUDGED, "模型沒有提到這一筆。"))
        result.candidates.append(
            Candidate(
                url=hit.url,
                title=hit.title,
                snippet=hit.snippet,
                kind=kind,
                reason=reason,
            )
        )

    # 名錄排最前面：一頁很多家，那是使用者最想先看到的。
    order = {DIRECTORY: 0, COMPANY: 1, UNJUDGED: 2, UNRELATED: 3}
    result.candidates.sort(key=lambda c: order.get(c.kind, 9))

    if result.ignored:
        log.info("找網站「{}」：模型回了 {} 個對不上的編號", query, result.ignored)
    return result


def find_sites(
    query: str, hits: Sequence[SearchHit], chat: ChatFn
) -> SiteSearchResult:
    """一份搜尋結果進去，一份貼好標籤的候選清單出來。**不發任何請求。**

    搜尋本身由呼叫端做完再傳進來（:mod:`controllers.ai`），跟
    :func:`ai.extract.extract_from_html` 同一個形狀：這一層只跟模型講話。
    """
    hits = list(hits)[:MAX_HITS]
    if not hits:
        return SiteSearchResult(query=query, found=0)
    return classify(hits, parse_reply(chat(build_messages(query, hits))), query)


def _as_index(value: object) -> int | None:
    """模型很常把編號寫成字串，或寫成 ``"[3]"``。那是格式不是資料。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = re.search(r"-?\d+", value)
        if digits:
            return int(digits.group())
    return None


def _as_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in (DIRECTORY, COMPANY, UNRELATED) else UNRELATED


def _as_reason(value: object) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > MAX_REASON_CHARS:
        text = text[:MAX_REASON_CHARS].rstrip() + "…"
    return text


__all__ = [
    "COMPANY",
    "DEFAULT_CHECKED",
    "DIRECTORY",
    "KIND_LABELS",
    "MAX_HITS",
    "SITES_MAX_TOKENS",
    "UNJUDGED",
    "UNRELATED",
    "Candidate",
    "SiteSearchResult",
    "build_messages",
    "classify",
    "find_sites",
]
