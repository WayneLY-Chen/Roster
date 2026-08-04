"""分辨「這是一家公司」還是「這是一則廣告／文章」。

名錄網站的列表頁常把推廣文章、專題報導、置入廣告做成跟公司卡片一模一樣的
版型。自動偵測看的是結構，看不出語意，所以「台中沙鹿房價還有機會?雙港門戶+
科技廊道崛起,解析未來增值潛力」會被當成一家公司抓進來。

這裡用的是**保守**策略：寧可放進一筆廣告，也不要誤刪一家真公司。因此

* 只有明確的訊號才會判定為廣告（文章網址、標題句式、裝飾符號開頭）
* 只要名稱帶有公司型態字樣（有限公司、企業社、工業⋯⋯）就一律保留，
  即使它同時命中了其他訊號
* 「名稱很長」本身**不構成**理由——「東敏企業社(代工製造, 精密機械,
  塑膠加工,工程齒輪)」是真公司
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: **法人型態字尾**。命中就直接視為公司，不再往下判斷。
#:
#: 這裡只放登記型態，不放「工廠」「企業」「實業」這類產業用詞——後者會出現在
#: 句子中間，例如廣告標題「…安裝排風扇怎麼放最涼?工廠、鐵皮屋排熱扇…」就因為
#: 含有「工廠」而被誤判成公司。字尾型態幾乎不會這樣誤命中。
COMPANY_MARKERS = (
    "股份有限公司", "有限公司", "無限公司", "兩合公司",
    "企業社", "實業社", "工業社", "商號", "行號", "工作室",
    "合作社", "產銷班", "事務所",
    "co., ltd", "co.,ltd", "co ltd", "company limited",
    "corporation", "corp.", "inc.", "ltd.", "llc", "gmbh",
)

#: 產業用詞。**不會**單獨用來判定為公司，只在沒有其他負面訊號時作為輔助。
INDUSTRY_WORDS = (
    "實業", "企業", "工廠", "製造廠", "鐵工廠", "科技", "工業",
)

#: 文章／廣告網址的主機前綴與路徑片段。
_ARTICLE_HOST_PREFIXES = ("life.", "blog.", "news.", "article.", "media.", "event.")
_ARTICLE_PATH_PARTS = (
    "/article/", "/articles/", "/blog/", "/news/", "/post/", "/posts/",
    "/story/", "/column/", "/promotion/", "/ad/", "/ads/", "/event/",
    "/knowledge/", "/faq/", "/tips/",
)

#: 標題常見的疑問句與內容行銷句式。
_HEADLINE_PATTERNS = (
    r"[?？]",                       # 疑問句幾乎不會是公司名
    r"[!！]{1,}$",                  # 以驚嘆號結尾
    r"懶人包|攻略|推薦|排行|排名|評比|解析|指南|教學|一次看|報你知",
    r"怎麼[選辦做用挑放]|如何[選挑做用]|要注意什麼|該不該|值不值得",
    r"幾種|大重點|大迷思|個理由|個技巧|步驟教學",
    r"價格|費用|多少錢|行情|房價|優惠|折扣|免費",
    r"^\d{4}\s*年?[最新推薦]",      # 2026最新推薦…
)

#: 標題開頭的裝飾符號（星星、扳手、火焰之類的 emoji 與全形記號）。
_DECORATION_RE = re.compile(
    r"^[\s -⁯←-⯿　-〿"
    r"\U0001F000-\U0001FAFF☀-➿️‍]+"
)

_HEADLINE_RE = re.compile("|".join(_HEADLINE_PATTERNS), re.IGNORECASE)

#: 名稱長過這個字數，且沒有公司字樣時，才把長度當成輔助訊號。
_LONG_NAME_CHARS = 30


@dataclass(frozen=True, slots=True)
class Verdict:
    """判斷結果。``reason`` 為空代表判定為公司。"""

    is_company: bool
    reason: str = ""


def strip_decoration(name: str) -> str:
    """去掉名稱開頭的 emoji 與裝飾符號。"""
    return _DECORATION_RE.sub("", name or "").strip()


def has_company_marker(name: str) -> bool:
    """名稱是否帶有法人型態字尾（強證據）。"""
    lowered = (name or "").lower()
    return any(marker in lowered for marker in COMPANY_MARKERS)


def has_industry_word(name: str) -> bool:
    """名稱是否含產業用詞（弱證據，不足以單獨認定為公司）。"""
    return any(word in (name or "") for word in INDUSTRY_WORDS)


def is_article_url(url: str | None) -> bool:
    """網址看起來是不是文章／廣告頁。"""
    if not url:
        return False
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
    except ValueError:
        return False

    host = parts.netloc.lower()
    if any(host.startswith(prefix) for prefix in _ARTICLE_HOST_PREFIXES):
        return True

    path = parts.path.lower()
    return any(part in path for part in _ARTICLE_PATH_PARTS)


def classify(name: str | None, website: str | None = None) -> Verdict:
    """判斷一筆紀錄是公司還是廣告。

    順序是刻意的：公司字樣的優先權高於一切其他訊號，因為那是最可靠的正面
    證據，而誤刪一家真公司的代價遠高於留下一則廣告。
    """
    raw = (name or "").strip()
    if not raw:
        return Verdict(False, "沒有名稱")

    cleaned = strip_decoration(raw)

    # 強證據優先：有法人型態字尾就是公司，其餘訊號一律不看。
    if has_company_marker(cleaned):
        return Verdict(True)

    if is_article_url(website):
        return Verdict(False, "網址指向文章或廣告頁")

    if _HEADLINE_RE.search(cleaned):
        return Verdict(False, "名稱像文章標題而非公司名")

    if cleaned != raw and len(cleaned) > _LONG_NAME_CHARS:
        # 開頭掛著 emoji 又長得像一整句話，幾乎都是推廣素材。
        return Verdict(False, "名稱以裝飾符號開頭且過長")

    if len(cleaned) > _LONG_NAME_CHARS and ("," in cleaned or "，" in cleaned):
        return Verdict(False, "名稱過長且像句子而非公司名")

    return Verdict(True)


def is_probably_company(name: str | None, website: str | None = None) -> bool:
    """:func:`classify` 的簡易版本。"""
    return classify(name, website).is_company
