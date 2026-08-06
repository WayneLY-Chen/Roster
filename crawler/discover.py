"""Guess a scraping recipe from a URL.

Point this at a directory page and it works out, without any human help:

* which repeated block on the page is one company (``list_selector``),
* a CSS rule for each field it can recognise,
* whether there is a "next page" link,
* a preview of what a real crawl would store.

The output is a :class:`~core.config.SourceConfig` -- exactly what a
hand-written ``config.yaml`` source would be -- so the guess is a *starting
point the user edits*, not a black box. Everything it produces can be inspected
and overridden before a single record is saved.

robots.txt still applies: discovery fetches through the normal fetcher, so a
page we are not allowed to read is not read here either.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from core.config import AppConfig, FieldRule, PaginationRule, SourceConfig, get_config
from core.constants import LogCategory
from core.errors import CrawlError
from core.logging_setup import get_logger
from core.schemas import RawCompany
from crawler.documents import KIND_BY_KEY
from crawler.fetcher import BaseFetcher, build_fetcher, decode_bytes
from crawler.labels import MIN_PAIRS, parse_record, split_cjk_english
from crawler.parser import extract_record, make_soup, sniff_declared_encoding
from verifier.classify import classify
from verifier.normalize import normalize_phone, normalize_tax_id
from verifier.validators import is_valid_email, is_valid_phone

log = get_logger(LogCategory.CRAWL)

# A repeated block has to appear at least this many times to be a "list".
MIN_ITEMS = 3
# Blocks with more text than this are usually the page wrapper, not one record.
MAX_ITEM_CHARS = 3000
# A field rule is only kept if it hits at least this share of the items.
MIN_FIELD_HIT_RATE = 0.35

_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_PHONE_RE = re.compile(
    r"(?:\+886[\s\-]?|\(0\d{1,2}\)|0)\d{1,3}[\s\-)]?\d{3,4}[\s\-]?\d{3,4}"
)
_TAX_ID_RE = re.compile(r"(?:統一?編號|統編|Tax\s*ID)\D{0,4}(\d{8})|(?<!\d)(\d{8})(?!\d)")
_ADDRESS_RE = re.compile(r"[一-鿿]{1,8}[市縣][一-鿿]{0,8}[區鄉鎮市]?[^,\n]{2,40}?[路街道段巷弄號樓]")
_NEXT_TEXT = ("下一頁", "下一页", "次頁", "next", "»", "›", ">>", "更多")

# Class/id fragments that reliably name a field on Chinese and English sites.
_CLASS_HINTS: dict[str, tuple[str, ...]] = {
    "company_name": ("company-name", "companyname", "firm", "vendor", "title", "name", "公司", "廠商"),
    "phone": ("tel", "phone", "mobile", "電話", "手機"),
    "email": ("mail", "email", "信箱", "郵"),
    "address": ("addr", "address", "location", "地址", "位置"),
    "industry": ("industry", "category", "cate", "type", "產業", "行業", "類別"),
    "contact_person": ("contact", "person", "owner", "聯絡", "窗口"),
    "english_name": ("english", "eng-name", "en-name", "英文"),
    "fax": ("fax", "傳真"),
    "products": ("product", "goods", "items", "產品", "營業項目", "代銷"),
    "website": ("website", "site", "url", "homepage", "網站", "網址"),
    "tax_id": ("tax", "vat", "uniform", "統編", "統一編號"),
}


@dataclass(slots=True)
class FieldGuess:
    """One guessed field rule, with the evidence behind it."""

    field: str
    selector: str
    attr: str = "text"
    regex: str | None = None
    hit_rate: float = 0.0
    samples: list[str] = field(default_factory=list)
    reason: str = ""

    def to_rule(self) -> FieldRule:
        return FieldRule(selector=self.selector, attr=self.attr, regex=self.regex)


@dataclass
class DiscoveryResult:
    """What :func:`discover` worked out about a page."""

    url: str
    list_selector: str = ""
    item_count: int = 0
    fields: dict[str, FieldGuess] = field(default_factory=dict)
    preview: list[RawCompany] = field(default_factory=list)
    next_selector: str | None = None
    notes: list[str] = field(default_factory=list)
    #: Selector for a per-company detail page, when the list only has names.
    detail_link_selector: str | None = None
    #: 從數字分頁推出來的網址樣板（含 ``{page}``），沒有就是 None。
    page_url_template: str | None = None
    #: 這個名錄總共有幾頁。0 代表沒偵測到，1 代表只有一頁。
    #:
    #: 這個數字必須讓使用者看到。頁數上限的預設值是保守的，而使用者沒有辦法
    #: 知道自己該把它調到多少——不告訴他的話，他會存下一個「只爬前幾頁」的
    #: 來源，然後以為程式不會自動翻頁。
    page_count: int = 0
    #: 頁面自己宣告的編碼（例如 ``big5``）。UTF-8 或沒宣告時是 None。
    encoding: str | None = None
    #: 從「標籤︰值」排版讀到、但沒有對應欄位的東西，用來給使用者看預覽。
    #: key 是名錄上的標籤，value 是前幾筆的樣本。
    extra_field_samples: dict[str, list[str]] = field(default_factory=dict)
    #: 偵測到「不點就看不到資料」的按鈕時，建議的頁面動作。
    #: 需要瀏覽器引擎才能執行，所以只是建議，由使用者決定要不要開。
    suggested_actions: list[dict[str, object]] = field(default_factory=list)
    #: 這一頁連出去的檔案，格式 → 幾個。介面靠它決定哪些格式勾得動——
    #: 一個頁面上根本沒有 PDF，卻讓使用者勾「讀 PDF」，勾了也不會發生任何事。
    document_links: dict[str, int] = field(default_factory=dict)
    #: 偵測到的查詢表單（要先選一個條件才有資料的那種）。
    #: ``{"input_selector", "submit_selector", "option_count", "sample"}``。
    #: 選項數的意義跟「總共幾頁」一樣：使用者要知道總共得跑幾趟。
    query_form: dict[str, object] | None = None
    #: 這一頁要用哪一種方式取才看得到資料。``"playwright"`` 代表原始 HTML 裡
    #: 沒有名單、是開了瀏覽器跑完 JavaScript 才出現的，這個來源之後也必須這樣
    #: 爬。None 代表用一般的方式就看得到。
    engine: str | None = None

    @property
    def ok(self) -> bool:
        """True when there is enough here to run a crawl."""
        return bool(self.list_selector) and "company_name" in self.fields

    def to_source_config(self, name: str, enabled: bool = True) -> SourceConfig:
        """Turn the guess into a runnable source definition."""
        if not self.ok:
            raise CrawlError(
                "not enough was detected to build a source; "
                "a company-name selector is required"
            )

        pagination = (
            PaginationRule(type="next_link", next_selector=self.next_selector)
            if self.next_selector
            else PaginationRule(type="none")
        )
        detail_link = (
            FieldRule(selector=self.detail_link_selector, attr="href")
            if self.detail_link_selector
            else None
        )
        return SourceConfig(
            name=name,
            type="generic_html",
            enabled=enabled,
            start_url=self.url,
            list_selector=self.list_selector,
            pagination=pagination,
            fields={key: guess.to_rule() for key, guess in self.fields.items()},
            detail_link=detail_link,
            label=name,
            encoding=self.encoding,
            engine=self.engine,
        )


# --------------------------------------------------------------- selectors


#: 一列一列輪流換色用的 class。它們**不代表結構不同**，一列有一列沒有純粹是
#: 為了斑馬紋。
#:
#: 為什麼要特別處理：一張 20 列的表格會被切成「10 個 tr.odd」與「10 個
#: tr.even」兩組，每一組的分數都只有原本的一半——結果是同一頁上 8 個項目的
#: 導覽選單反而贏過真正的 20 筆廠商資料。這是實際踩到的：一個查詢型名錄查出
#: 20 家公司，偵測回報「找到 8 筆」，內容是「加入會員、找商機、認識公會」。
_ALTERNATING_CLASS = frozenset({
    "odd", "even", "alt", "altrow", "alternate", "stripe", "striped",
    "row-odd", "row-even", "first", "last", "active",
})


def _structural_classes(node: Tag) -> list[str]:
    """這個元素的 class，去掉「一列一個」那種輪替用的。"""
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return [c for c in classes if c.lower() not in _ALTERNATING_CLASS]


def _signature(node: Tag) -> str:
    """Structural fingerprint of an element: tag plus its class list."""
    classes = _structural_classes(node)
    return f"{node.name}.{'.'.join(sorted(classes))}" if classes else node.name


#: Bootstrap 那類「排版用、跟內容無關」的 class，選擇器不該用它們。
#:
#: 規則刻意寫得緊：先前是 ``(col|row|p|m|d)-?\w{0,3}\d*``，那個式子會把
#: ``more``、``main``、``date``、``post`` 這些**真正有意義**的 class 一起濾掉
#: （``m`` + 最多三個字），結果是「載入更多」按鈕的選擇器退化成一個光禿禿的
#: ``button``，在整頁比對到幾十個元素。工具類別的特徵是「帶數字或帶連字號」，
#: 照著這個特徵寫就不會誤傷。
_UTILITY_CLASS = re.compile(
    r"(?:col|row)(?:-[\w-]+)?"      # col, col-6, col-md-4, row
    r"|[pm][xytblrse]?-\d+"          # p-3, mt-2, px-4
    r"|d-[\w-]+"                     # d-flex, d-none
    r"|[gh]-\d+"                     # g-3, h-100
)


def _css_for(node: Tag) -> str:
    """Shortest stable CSS selector for an element (tag or tag.class)."""
    # 輪替用的 class 也不能寫進選擇器：``tr.odd`` 只會抓到一半的資料列。
    classes = _structural_classes(node)
    # Skip utility classes that carry no meaning and change between builds.
    meaningful = [
        c for c in classes
        if len(c) > 1 and not c.isdigit() and not _UTILITY_CLASS.fullmatch(c)
    ]
    return f"{node.name}.{meaningful[0]}" if meaningful else node.name


def _text(node: Tag) -> str:
    return node.get_text(" ", strip=True)


#: 這些標籤永遠不是「一筆資料」。
#:
#: ``option`` 是踩到才知道的：一個有 98 個商品分類的查詢選單，重複度完美、
#: 每一項的長度也很平均，分數輕鬆壓過同一頁上真正的 20 列廠商資料。結果是
#: 分析結束後告訴使用者「找到 98 筆」，而那 98 筆是下拉選單的選項。
_NEVER_A_RECORD = frozenset({
    "option", "optgroup", "script", "style", "template", "noscript",
    "meta", "link", "br", "hr", "source", "track", "param",
})


def find_list_selector(soup: BeautifulSoup) -> tuple[str, list[Tag], list[str]]:
    """Find the repeated block that represents one record.

    Groups siblings by structural fingerprint and scores each group on how
    much it looks like a company listing -- repetition, contact details,
    links, and a sane amount of text per item.
    """
    notes: list[str] = []
    groups: dict[tuple[int, str], list[Tag]] = defaultdict(list)

    for node in soup.find_all(True):
        parent = node.parent
        if parent is None or not isinstance(parent, Tag):
            continue
        if node.name in _NEVER_A_RECORD:
            continue
        groups[(id(parent), _signature(node))].append(node)

    best_score = 0.0
    best_items: list[Tag] = []
    best_selector = ""

    for (_parent_id, signature), items in groups.items():
        if len(items) < MIN_ITEMS or "." not in signature and len(items) < MIN_ITEMS + 2:
            continue

        texts = [_text(node) for node in items]
        lengths = [len(t) for t in texts]
        if not any(lengths) or max(lengths) > MAX_ITEM_CHARS:
            continue

        joined = "\n".join(texts)
        emails = len(_EMAIL_RE.findall(joined))
        phones = len(_PHONE_RE.findall(joined))
        links = sum(len(node.find_all("a")) for node in items)
        headings = sum(1 for node in items if node.find(re.compile("^h[1-6]$")))

        # Items should be similar in size; wildly uneven groups are layout noise.
        average = sum(lengths) / len(lengths)
        spread = max(lengths) / average if average else 99
        if average < 12 or spread > 8:
            continue

        # 內容才是唯一分得出「廠商名錄」與「同樣整齊的導覽選單」的訊號。
        #
        # 沒有這一項的時候，一個 8 項的下拉式導覽選單（每一項底下還有子連結）
        # 分數會跟同一頁上真正的 20 列廠商資料打平——結構上它們一樣整齊，靠
        # 重複度、連結數、有沒有 class 都分不出來。實際踩到的結果是分析回報
        # 「找到 8 筆」，內容是「加入會員、找商機、認識公會」。
        company_like = sum(1 for text in texts if _has_company_marker(text))
        company_ratio = company_like / len(items)

        score = (
            len(items) * 1.0
            + emails * 4.0
            + phones * 3.0
            + headings * 2.5
            + min(links, len(items) * 3) * 0.5
            + (10.0 if "." in signature else 0.0)   # a class-based block is a real component
            + company_ratio * 30.0
        )
        if score > best_score:
            best_score, best_items = score, items
            best_selector = _css_for(items[0])

    if not best_items:
        notes.append(
            "找不到重複的資料區塊。這個頁面可能是用 JavaScript 產生內容，"
            "可以把 config.yaml 的 crawler.engine 改成 playwright 再試一次。"
        )
        return "", [], notes

    # The winning block may be a *group* of records rather than one record --
    # trade-association directories love wrapping companies in per-industry
    # containers. Drilling in is what separates "23 categories" from the 204
    # companies actually on the page.
    refined = _refine_to_inner_items(soup, best_selector, best_items)
    if refined is not None:
        inner_selector, inner_items = refined
        notes.append(
            f"偵測到外層 {best_selector} 其實是分組容器，已改用 "
            f"{inner_selector}，筆數由 {len(best_items)} 提升為 {len(inner_items)}。"
        )
        best_selector, best_items = inner_selector, inner_items

    # A tag-only selector may over-match. Try to narrow it first; only warn if
    # narrowing fails, rather than silently collecting the wrong nodes.
    scoped = _scope_selector(soup, best_selector, best_items)
    if scoped != best_selector:
        best_selector = scoped
    else:
        matched = soup.select(best_selector)
        if len(matched) > len(best_items) * 1.5:
            notes.append(
                f"選擇器 {best_selector} 在整頁比對到 {len(matched)} 個區塊，"
                f"但偵測到的清單只有 {len(best_items)} 筆，建議手動確認。"
            )
    return best_selector, best_items, notes


def _anchor_css(node: Tag) -> str | None:
    """節點能不能當「錨點」——有 id 或有意義的 class 才算得上獨一無二。"""
    node_id = node.get("id")
    if isinstance(node_id, str) and node_id.strip():
        return f"#{node_id.strip()}"
    css = _css_for(node)
    return css if "." in css else None


def _scope_selector(soup: BeautifulSoup, selector: str, items: list[Tag]) -> str:
    """把過於籠統的選擇器縮小到只選中真正的清單。

    ``table`` 這種只有標籤名的選擇器，在真正爬取時會把版面用的表格一起選進來
    ——2000 年代那批用巢狀表格排版的名錄尤其嚴重。偵測階段是靠「兄弟節點分組」
    找到清單的，那個資訊在存成 ``list_selector`` 之後就沒了，所以必須在這裡把
    範圍寫進選擇器本身。

    作法是往上找最近一個帶 id 或 class 的祖先當前綴，並且**驗證**加了前綴之後
    選到的節點與偵測到的清單完全一致——猜一個看起來比較精確的選擇器，卻選到
    另一批節點，比原本的問題更糟。
    """
    wanted = {id(node) for node in items}

    def selects(candidate: str) -> set[int] | None:
        try:
            return {id(node) for node in soup.select(candidate) if isinstance(node, Tag)}
        except Exception:
            return None

    current = selects(selector)
    if current is None or len(current) <= len(items) * 1.2:
        return selector

    for ancestor in items[0].parents:
        if not isinstance(ancestor, Tag) or ancestor.name in ("html", "[document]"):
            break
        anchor = _anchor_css(ancestor)
        if anchor is None:
            continue
        candidate = f"{anchor} {selector}"
        if selects(candidate) == wanted:
            return candidate

    return selector


#: 內層元素要比外層多這麼多倍，才值得往下鑽。
_DRILL_DOWN_RATIO = 1.5

#: 行內元素不會是「一筆紀錄」，它們是紀錄裡面的一個片段。列出來擋掉是因為
#: 光看數量的話它們一定贏——一張卡片裡有十幾個 ``<font>``、好幾個 ``<a>``。
_INLINE_TAGS = frozenset({
    "a", "span", "font", "b", "strong", "em", "i", "u", "small", "big",
    "label", "abbr", "cite", "code", "sub", "sup", "br", "img", "wbr",
})

#: 內層元素的文字量至少要佔外層的這個比例，才可能是「一組紀錄」。
#: 一個分類容器裡的公司卡片幾乎涵蓋容器全部的文字；而容器裡的某一個欄位
#: （或所有連結加起來）只佔一小部分。這是分辨「往下鑽對了」與「鑽過頭、
#: 鑽進欄位裡」最直接的訊號。
_DRILL_DOWN_TEXT_COVERAGE = 0.5

#: 內層元素裡「本身就像一筆紀錄」的比例下限。
_DRILL_DOWN_RECORD_RATIO = 0.5


def _looks_like_a_record(text: str) -> bool:
    """一段文字像不像「一家公司」而不是「一家公司的某一欄」。

    判準是兩個獨立訊號取聯集：帶著公司名稱的特徵字（有限公司、企業社、
    工廠……），或帶著聯絡資訊（信箱、電話）。兩者都沒有的多半是欄位本身
    ——「負責人︰王大明」「地址︰台北市……」。

    為什麼要兩個而不是一個：只看名稱特徵，會擋掉品牌式命名的名錄
    （台積電、Google 都不帶特徵字）；只看聯絡資訊，會擋掉只列名稱、
    聯絡方式在明細頁的名錄。任一成立就放行。
    """
    if _has_company_marker(text):
        return True
    return bool(_EMAIL_RE.search(text) or _PHONE_RE.search(text))


def _looks_like_a_name(node: Tag) -> bool:
    """節點的文字像不像一個公司名稱。"""
    text = _text(node)
    return 2 <= len(text) <= 60 and bool(re.search(r"[\w一-鿿]", text))


def _refine_to_inner_items(
    soup: BeautifulSoup, outer_selector: str, outer_items: list[Tag]
) -> tuple[str, list[Tag]] | None:
    """若外層區塊其實是一組紀錄的容器，回傳更精確的內層選擇器。

    判準是「內層同型元素的總數遠多於外層區塊數」——一個分類容器裡有十幾家
    公司，就會呈現這個特徵；而一張公司卡片裡的 h3、p.tel 各只出現一次，
    比例接近 1，不會被誤判。
    """
    if not outer_items:
        return None

    outer_chars = sum(len(_text(item)) for item in outer_items)
    counter: Counter[str] = Counter()
    examples: dict[str, Tag] = {}
    for item in outer_items:
        for child in item.find_all(True):
            if child.name in _INLINE_TAGS:
                continue
            if not _looks_like_a_name(child):
                continue
            signature = _signature(child)
            counter[signature] += 1
            examples.setdefault(signature, child)

    for signature, count in counter.most_common(5):
        if count < len(outer_items) * _DRILL_DOWN_RATIO:
            continue

        inner_css = _css_for(examples[signature])
        # Scope to the outer block so a bare tag name cannot pick up the page
        # navigation as well.
        candidate = f"{outer_selector} {inner_css}"
        try:
            inner_items = [n for n in soup.select(candidate) if isinstance(n, Tag)]
        except Exception:
            continue

        if len(inner_items) < len(outer_items) * _DRILL_DOWN_RATIO:
            continue
        # Each inner element must still carry a usable name; otherwise we have
        # drilled past the record into its individual fields.
        if sum(1 for n in inner_items if _looks_like_a_name(n)) < len(inner_items) * 0.8:
            continue
        # 內層必須裝得下外層大部分的文字。少了這一關，一個 24 筆的名錄會被
        # 「每張卡片裡的連結」打敗——連結比較多，但每個連結只有幾個字，整筆
        # 資料就這樣被拆散了。
        inner_texts = [_text(node) for node in inner_items]
        inner_chars = sum(len(text) for text in inner_texts)
        if outer_chars and inner_chars < outer_chars * _DRILL_DOWN_TEXT_COVERAGE:
            continue
        # 而且內層自己要像一筆紀錄。文字量這一關擋不掉「一筆紀錄的十個列」
        # ——那十列加起來當然涵蓋整筆資料，但每一列都只是一個欄位。
        records = sum(1 for text in inner_texts if _looks_like_a_record(text))
        if records < len(inner_items) * _DRILL_DOWN_RECORD_RATIO:
            continue
        return candidate, inner_items

    return None


# ------------------------------------------------------------------ fields


#: 這些欄位的值是「這一家公司的」，每筆本來就該不一樣。相異率低於門檻代表
#: 抓到的是全站共用的東西，不是這一家的資料。
#:
#: 這不是理論上的顧慮：實測有名錄把每一筆的 ``<a href>`` 都寫死成同一個網址
#: （顯示的連結文字才是真的），照抓的話 1470 家公司會全部拿到同一個網站。
#: 產業、地址不在這張清單裡——同一個公會的會員本來就可能全是同一個行業、
#: 全在同一個城市。
_PER_COMPANY_FIELDS = frozenset({"email", "phone", "fax", "website", "tax_id"})
_MIN_PER_COMPANY_DISTINCT_RATIO = 0.5


def _validated(field_name: str, selector: str, attr: str, items: list[Tag],
               regex: str | None = None) -> FieldGuess | None:
    """Apply a candidate rule to every item and keep it if it hits often enough."""
    rule = FieldRule(selector=selector, attr=attr, regex=regex)
    samples: list[str] = []
    values: list[str] = []
    for item in items:
        from crawler.parser import extract_field

        value = extract_field(item, rule)
        if value:
            values.append(value)
            if len(samples) < 3:
                samples.append(value[:80])

    hit_rate = len(values) / len(items) if items else 0.0
    if hit_rate < MIN_FIELD_HIT_RATE:
        return None
    if field_name in _PER_COMPANY_FIELDS and len(values) > 2:
        distinct_ratio = len(set(values)) / len(values)
        if distinct_ratio < _MIN_PER_COMPANY_DISTINCT_RATIO:
            log.debug(
                "rejecting {} rule {!r}: {:.0%} of items share the same value",
                field_name, selector, 1 - distinct_ratio,
            )
            return None
    return FieldGuess(
        field=field_name, selector=selector, attr=attr, regex=regex,
        hit_rate=hit_rate, samples=samples,
    )


def _by_class_hint(field_name: str, items: list[Tag]) -> FieldGuess | None:
    """Look for an element whose class or id names the field."""
    hints = _CLASS_HINTS.get(field_name, ())
    counter: Counter[str] = Counter()

    for item in items:
        for node in item.find_all(True):
            tokens = list(node.get("class") or [])
            if isinstance(tokens, str):
                tokens = tokens.split()
            node_id = node.get("id")
            if node_id:
                tokens.append(node_id)
            haystack = " ".join(tokens).lower()
            if any(hint in haystack for hint in hints):
                counter[_css_for(node)] += 1

    for selector, _count in counter.most_common(4):
        guess = _validated(field_name, selector, "text", items)
        if guess:
            guess.reason = "class/id 名稱符合"
            return guess
    return None


def _deepest_match(item: Tag, pattern: re.Pattern[str]) -> Tag | None:
    """Smallest element inside ``item`` whose own text matches ``pattern``."""
    best: Tag | None = None
    for node in item.find_all(True):
        text = _text(node)
        if text and pattern.search(text):
            if best is None or len(text) < len(_text(best)):
                best = node
    return best


def _by_text_pattern(
    field_name: str, items: list[Tag], pattern: re.Pattern[str], regex: str
) -> FieldGuess | None:
    """Find the element that carries a value matching a text pattern."""
    counter: Counter[str] = Counter()
    for item in items:
        node = _deepest_match(item, pattern)
        if node is not None:
            counter[_css_for(node)] += 1

    for selector, _count in counter.most_common(3):
        guess = _validated(field_name, selector, "text", items, regex=regex)
        if guess:
            guess.reason = "內容格式符合"
            return guess
    return None


#: 每張卡片都會出現的導覽／動作文字。這些正是舊版誤抓的東西：一個「詳細
#: 資料」連結出現在每一列，命中率是 100%，比真正的公司名稱還「可靠」，於是
#: 穩穩勝出。要靠內容判斷才擋得掉，光看命中率永遠會選到它們。
_NAME_STOPWORDS = frozenset({
    "更多", "詳細", "詳細資料", "詳細內容", "詳情", "看更多", "閱讀更多",
    "查看", "查看更多", "繼續閱讀", "回上頁", "上一頁", "下一頁", "首頁",
    "回首頁", "登入", "註冊", "會員專區", "聯絡我們", "關於我們", "立即聯絡",
    "洽詢", "詢價", "加入最愛", "分享", "列印", "地圖", "官網", "網站",
    "more", "detail", "details", "read more", "readmore", "view", "view more",
    "login", "sign up", "signup", "home", "back", "next", "previous", "contact",
    "click here", "learn more", "website", "map", "share", "print",
})

#: 台灣公司名稱的組織型態關鍵詞。命中代表這一欄「看起來真的是公司名」。
_COMPANY_MARKERS: tuple[str, ...] = (
    "股份有限公司", "有限公司", "公司", "企業社", "企業", "實業", "工業",
    "科技", "商行", "行號", "工作室", "事務所", "工廠", "製造", "貿易",
    "合作社", "農場", "牧場", "診所", "藥局", "商號", "產業", "集團",
)
_COMPANY_MARKERS_EN: tuple[str, ...] = (
    "co.", "co,", " ltd", "ltd.", " inc", "inc.", "corp", "company", "llc",
    "gmbh", "plc", "industries", "enterprise",
)

#: 台灣中小企業的登記名稱常常只以單一個字結尾，不帶「公司」二字。
#: 這不是猜的——實際資料庫的 215 筆裡就有 8 筆長這樣：「祥發包裝材料行」、
#: 「金龍電工機械廠」、「豫味開封包子店」、「力業鐵工廠」。只認「有限公司」
#: 會讓整個小型商家名錄的評分被低估。
#: 限定「結尾」而非「包含」，避免「旅行社優惠」這種內文被誤判。
_COMPANY_SUFFIXES: tuple[str, ...] = (
    "行", "廠", "店", "社", "號", "坊", "館", "苑", "軒", "堂", "舖", "鋪", "庄",
)

#: 一望即知不是公司名稱的內容形狀（純數字、日期、網址、信箱、只有標點）。
_NOT_NAME_SHAPE = re.compile(
    r"""(?x)
    ^\W*$
  | ^[\d\s\-/年月日時分:.()]+$
  | ^https?://
  | ^[\w.+\-]+@[\w\-]+\.
    """
)

#: 候選值裡至少要有這麼高的比例「每一列都不一樣」，才可能是公司名稱。
#: 樣板連結的相異率趨近 0，正常名錄接近 1。
_MIN_NAME_DISTINCT_RATIO = 0.5

#: 內容評分低於這個門檻就不採用，寧可回報「找不到公司名稱」讓使用者自己填，
#: 也不要塞一堆「詳細資料」進資料庫。
_MIN_NAME_SCORE = 3.0


def _is_obviously_not_a_name(value: str) -> bool:
    text = value.strip()
    if not text or len(text) > 80:
        return True
    if text.lower() in _NAME_STOPWORDS:
        return True
    return bool(_NOT_NAME_SHAPE.match(text))


def _has_company_marker(value: str) -> bool:
    text = value.strip()
    if any(marker in text for marker in _COMPANY_MARKERS):
        return True
    # 單字尾要夠長才算，兩三個字的「更多」「查看」不會誤中。
    #
    # 地址要先排除。「…中山北路二段45號」結尾的「號」正好也在單字尾清單裡
    # （「祥發包裝材料行」「豫味開封包子店」那一類名稱靠的就是它），不擋掉
    # 的話每一個地址都會被當成公司名稱。
    if len(text) >= 4 and text.endswith(_COMPANY_SUFFIXES) and not _ADDRESS_RE.search(text):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _COMPANY_MARKERS_EN)


def _score_name_values(values: list[str], item_count: int) -> float:
    """一組候選值有多像「每一列各自的公司名稱」。

    分數由四個獨立訊號組成，最重的是**相異率**——這是唯一能分辨
    「真名稱」與「每列都一樣的樣板文字」的訊號，而舊版完全沒有用到它。
    """
    if not values or not item_count:
        return 0.0

    coverage = len(values) / item_count
    if coverage < MIN_FIELD_HIT_RATE:
        return 0.0

    distinct_ratio = len(set(values)) / len(values)
    if distinct_ratio < _MIN_NAME_DISTINCT_RATIO:
        return 0.0

    usable = [v for v in values if not _is_obviously_not_a_name(v)]
    if not usable:
        return 0.0
    usable_ratio = len(usable) / len(values)

    # 地址欄長得非常像公司名稱：每一列都不一樣、長度合理、而且「…一段1號」
    # 的「號」正好也是台灣小型商家常見的名稱結尾。少了這一關，一個沒有公司
    # 名稱的頁面會拿地址頂替，而那比誠實回報「找不到」糟得多。
    if sum(1 for v in usable if _ADDRESS_RE.search(v)) > len(usable) / 2:
        return 0.0

    marker_ratio = sum(1 for v in usable if _has_company_marker(v)) / len(usable)

    average_length = sum(len(v) for v in usable) / len(usable)
    length_factor = 1.0 if 3 <= average_length <= 40 else 0.4

    return (
        distinct_ratio * 5.0
        + marker_ratio * 4.0
        + usable_ratio * 2.0
        + coverage * 1.0
    ) * length_factor


def _values_for(items: list[Tag], selector: str, attr: str = "text") -> list[str]:
    """把候選規則實際套用到每一列，取回真正會存進資料庫的值。"""
    from crawler.parser import extract_field

    rule = FieldRule(selector=selector, attr=attr)
    values: list[str] = []
    for item in items:
        value = extract_field(item, rule)
        if value:
            values.append(value.strip())
    return values


def _first_text_node(item: Tag) -> Tag | None:
    """區塊裡第一個「自己就帶著文字」的最小元素。

    取最小的那一層是重點：``<td><font>公司名</font></td>`` 要的是 ``font``，
    因為 ``td`` 選出來的文字在別的欄位上會連標籤帶值一起吃進去。

    class 或 id 已經寫明是別的欄位（``tel``、``addr``）的節點直接跳過——那是
    頁面自己給的答案，比「它排在最前面」可靠。
    """
    for node in item.find_all(True):
        if node.find(True) is not None:
            continue                    # 還有子元素，不是最小的那一層
        if _names_another_field(node):
            continue
        text = _text(node)
        if text and not _is_obviously_not_a_name(text):
            return node
    return None


def _names_another_field(node: Tag) -> bool:
    """節點的 class/id 是不是指名了「公司名稱以外」的某個欄位。"""
    tokens = list(node.get("class") or [])
    if isinstance(tokens, str):
        tokens = tokens.split()
    if node.get("id"):
        tokens.append(str(node.get("id")))
    haystack = " ".join(tokens).lower()
    if not haystack:
        return False
    return any(
        hint in haystack
        for field_name, hints in _CLASS_HINTS.items()
        if field_name != "company_name"
        for hint in hints
    )


def _guess_company_name(items: list[Tag]) -> FieldGuess | None:
    """Company name: pick the column that actually *reads* like company names.

    舊版是「數哪個選擇器出現最多次，再檢查命中率」。那個作法必然會選到每張
    卡片都有的「詳細資料／更多」連結——它們的命中率剛好是最高的 100%。

    現在改成：先蒐集結構上合理的候選（標題、連結、粗體、class 名稱像的），
    再把每個候選**實際套用一次**，用內容本身評分，取最高分者。
    """
    if not items:
        return None

    # 結構加分：標題最可能是名稱，其次是連結，粗體再次之。
    structural: Counter[str] = Counter()
    for item in items:
        heading = item.find(re.compile("^h[1-6]$"))
        if heading is not None:
            structural[_css_for(heading)] += 3.0
        for node in item.find_all(("a", "strong", "b")):
            if 2 <= len(_text(node)) <= 80:
                structural[_css_for(node)] += 1.0

    # class/id 名稱帶有 company/name/公司 之類的字也算候選。
    for item in items:
        for node in item.find_all(True):
            tokens = list(node.get("class") or [])
            if isinstance(tokens, str):
                tokens = tokens.split()
            if node.get("id"):
                tokens.append(str(node.get("id")))
            haystack = " ".join(tokens).lower()
            if any(hint in haystack for hint in _CLASS_HINTS["company_name"]):
                structural[_css_for(node)] += 2.0

    # 每一筆的第一段文字。上面三種候選都需要頁面有標題標籤、連結或 class，
    # 而舊式的公會名錄三者皆無——整頁只有 <table> 與 <font>，公司名稱就是
    # 卡片裡的第一段字。少了這一條，那類名錄一律回報「找不到公司名稱」。
    for item in items:
        first = _first_text_node(item)
        if first is not None:
            structural[_css_for(first)] += 2.0

    best: FieldGuess | None = None
    best_score = 0.0
    for selector, structure_bonus in structural.most_common(12):
        values = _values_for(items, selector)
        content_score = _score_name_values(values, len(items))
        if content_score <= 0:
            continue
        # 結構只是輔助；內容才是主要依據，所以結構加分壓得很小。
        score = content_score + min(structure_bonus / len(items), 1.0)
        if score > best_score:
            best_score = score
            best = FieldGuess(
                field="company_name",
                selector=selector,
                attr="text",
                hit_rate=len(values) / len(items),
                samples=values[:3],
                reason=f"內容像公司名稱（評分 {content_score:.1f}）",
            )

    if best is not None and best_score >= _MIN_NAME_SCORE:
        return best
    return None


def _guess_email(items: list[Tag]) -> FieldGuess | None:
    guess = _validated("email", "a[href^='mailto:']", "href", items)
    if guess:
        guess.reason = "mailto 連結"
        return guess
    return _by_text_pattern("email", items, _EMAIL_RE, _EMAIL_RE.pattern)


def _guess_phone(items: list[Tag]) -> FieldGuess | None:
    guess = _validated("phone", "a[href^='tel:']", "href", items)
    if guess:
        guess.reason = "tel 連結"
        return guess
    return _by_class_hint("phone", items) or _by_text_pattern(
        "phone", items, _PHONE_RE, _PHONE_RE.pattern
    )


#: 連結文字長得像網址時才拿來當網址用（而不是「官方網站」這種說明文字）。
_URL_IN_TEXT = r"(?:https?://|www\.)[^\s<>\"']+"


def _guess_website(items: list[Tag], page_host: str) -> FieldGuess | None:
    """An outbound link is usually the company's own site."""
    counter: Counter[str] = Counter()
    for item in items:
        for node in item.find_all("a", href=True):
            href = node["href"]
            if not isinstance(href, str) or not href.startswith("http"):
                continue
            host = urlsplit(href).netloc.lower()
            if host and page_host not in host:
                counter[_css_for(node)] += 1

    for selector, _count in counter.most_common(3):
        # ``[href^='http']`` 不能省。候選選擇器常常只是「a」，而同一筆資料裡
        # 排在前面的往往是 mailto 連結——取第一個相符節點就會把信箱當成網址。
        # 這個條件把 mailto:／tel:／javascript: 全部排除在外。
        scoped = f"{selector}[href^='http']"

        # 先看連結**文字**。理由是實測踩到的：有名錄在公司沒有網址時，直接把
        # 上一家的 href 留在那裡，只把連結文字清空——照 href 抓的話，一整串
        # 沒有網站的公司會被填上別人的網址，而畫面上根本看不到那個網址。
        # 連結文字是使用者實際看得到的東西，兩者不一致時它才是對的。
        # 文字不像網址（「官方網站」「前往」）時這一條命中率不足，自動落到
        # 下面用 href，一般網站的行為完全不變。
        guess = _validated("website", scoped, "text", items, regex=_URL_IN_TEXT)
        if guess:
            guess.reason = "連結文字就是網址"
            return guess

        guess = _validated("website", scoped, "href", items)
        if guess:
            guess.reason = "指向外部網域的連結"
            return guess
    return _by_class_hint("website", items)


def _guess_tax_id(items: list[Tag]) -> FieldGuess | None:
    guess = _by_class_hint("tax_id", items)
    if guess:
        return guess
    return _by_text_pattern("tax_id", items, _TAX_ID_RE, r"\d{8}")


def find_detail_link_selector(items: list[Tag], page_host: str) -> str | None:
    """Find the per-company detail link inside each list item.

    Taiwanese trade-association directories overwhelmingly list only the
    company name, linked to a detail page holding the contact details. A link
    qualifies when it appears in most items, points at the same site, and each
    item's target is *distinct* -- a shared "join us" link in every row is
    navigation, not a per-company page.
    """
    if not items:
        return None

    counter: Counter[str] = Counter()
    for item in items:
        for anchor in item.find_all("a", href=True):
            href = anchor["href"]
            if not isinstance(href, str) or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            host = urlsplit(href).netloc.lower().removeprefix("www.")
            if host and host != page_host:
                continue          # outbound link, not a detail page
            counter[_css_for(anchor)] += 1

    for selector, count in counter.most_common(4):
        if count < len(items) * MIN_FIELD_HIT_RATE:
            continue
        targets = {
            anchor["href"]
            for item in items
            for anchor in item.select(selector)
            if anchor.has_attr("href")
        }
        # Distinct targets per item is what separates a detail link from a
        # repeated navigation link.
        if len(targets) >= max(2, count * 0.7):
            return selector
    return None


#: 把資料藏在按鈕後面的常見寫法。這些字出現在按鈕上，代表「這一頁的原始
#: HTML 裡沒有那筆資料」——不點它就永遠抓不到。
_REVEAL_WORDS = (
    "顯示電話", "查看電話", "看電話", "顯示聯絡", "查看聯絡", "顯示信箱",
    "查看信箱", "顯示手機", "展開", "看詳細", "顯示完整",
    "show phone", "show contact", "show email", "reveal", "view number",
)

#: 「還有更多沒載入」的按鈕。
_LOAD_MORE_WORDS = (
    "載入更多", "看更多", "更多結果", "顯示更多", "load more", "show more",
    "view more", "more results",
)

#: 一個按鈕文字要在多少比例的項目上出現，才算「每一列都有一顆」。
_REVEAL_HIT_RATE = 0.5


def _clickable_selector(node: Tag) -> str:
    """給一個按鈕，回傳指得到它、而且不會誤中別的東西的選擇器。"""
    css = _css_for(node)
    return css if "." in css else f"{node.name}"


def find_page_actions(soup: BeautifulSoup, items: list[Tag]) -> list[dict[str, object]]:
    """找出「不點就看不到資料」的按鈕，回傳建議的頁面動作。

    兩種情形分開處理，因為做法不一樣：

    * **每一列各有一顆**（「顯示電話」）→ ``click_all``，全部按一次。
    * **整頁只有一顆**（「載入更多」）→ ``click``，連按數次把清單展開。

    只做建議。真正要不要執行由使用者決定——這件事需要瀏覽器引擎，比一般爬取
    慢得多，不該自作主張替他打開。
    """
    actions: list[dict[str, object]] = []

    if items:
        counter: Counter[str] = Counter()
        for item in items:
            for node in item.find_all(("button", "a", "span", "div")):
                text = _text(node).lower()
                if any(word in text for word in _REVEAL_WORDS):
                    counter[_clickable_selector(node)] += 1
        for selector, count in counter.most_common(2):
            if count >= len(items) * _REVEAL_HIT_RATE:
                actions.append({"type": "click_all", "selector": selector})

    for node in soup.find_all(("button", "a")):
        text = _text(node).lower()
        if any(word in text for word in _LOAD_MORE_WORDS):
            actions.append(
                {"type": "click", "selector": _clickable_selector(node), "times": 10}
            )
            break

    return actions


def find_document_links(soup: BeautifulSoup, base_url: str) -> dict[str, int]:
    """這一頁連出去哪些可以讀的檔案，以及各有幾個。

    介面靠這個決定「讀 PDF」那些勾選框哪幾個打得動。一個頁面上根本沒有 PDF，
    卻讓使用者勾「讀 PDF」，勾了不會發生任何事——那比不給選項更讓人困惑。
    """
    from urllib.parse import urljoin

    from crawler.documents import kind_for

    counts: dict[str, int] = {}
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not isinstance(href, str) or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(base_url, href)
        if target in seen:
            continue
        seen.add(target)
        kind = kind_for(target)
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def find_next_selector(soup: BeautifulSoup) -> str | None:
    """Locate a "next page" link by rel, text, class, title or aria-label."""
    node = soup.select_one("a[rel='next']")
    if node is not None:
        return "a[rel='next']"

    for anchor in soup.find_all("a", href=True):
        haystack = " ".join(
            [
                _text(anchor).lower(),
                " ".join(anchor.get("class") or []),
                str(anchor.get("title") or ""),
                # 只有圖示、沒有文字的「下一頁」按鈕，可讀性資訊都放在
                # aria-label 裡——不看它就等於看不到那顆按鈕。
                str(anchor.get("aria-label") or ""),
            ]
        ).lower()
        if any(marker in haystack for marker in _NEXT_TEXT):
            selector = _css_for(anchor)
            if len(soup.select(selector)) <= 3:
                return selector
    return None


#: 一個查詢參數要出現這麼多個不同的數字，才算是分頁而不是巧合。
_MIN_PAGE_LINKS = 2

#: 常見的頁碼參數名稱。不在這個清單裡也還是會被認出來（判斷依據是「值是
#: 連續的數字」），列出來只是為了在多個候選之間挑一個最像的。
_PAGE_PARAM_HINTS = ("page", "p", "pg", "pageno", "page_no", "pageindex", "start", "offset")


#: 送出查詢的按鈕上會寫的字。
_SUBMIT_TEXT = (
    "查詢", "搜尋", "查　詢", "送出", "確定", "開始查詢",
    "search", "query", "qry", "submit", "find",
)

#: 彈出視窗裡的按鈕不是查詢鈕。「確定」「關閉」那幾顆長得跟查詢鈕一模一樣，
#: 但按下去只會關掉一個對話框。
_MODAL_CLASSES = ("modal", "dialog", "popup", "lightbox")

#: 一個下拉選單至少要有這麼多個選項，才像是「查詢條件」而不是「每頁幾筆」。
MIN_QUERY_OPTIONS = 4


def find_query_form(soup: BeautifulSoup) -> dict[str, object] | None:
    """偵測「要先選一個條件才會有資料」的查詢表單。

    回傳 ``{"input_selector", "submit_selector", "option_count", "sample"}``，
    沒有就是 ``None``。

    這跟偵測頁數是同一件事的兩種長相：一個名錄有 24 頁，跟一個名錄有 98 個
    分類要各查一次，對使用者來說都是「總共要跑幾趟」。不偵測、不顯示的話，
    使用者存下來的來源就只會有第一個分類的資料，而畫面上寫著「完成」。
    """
    best = _best_select_form(soup)
    text_form = _best_text_form(soup)

    if best is None:
        return text_form
    if text_form:
        # 兩種都有的時候（查詢頁常常做成好幾個分頁籤）主推選單——選項是現成的，
        # 使用者不必自己想關鍵字。但文字查詢的位置也一起交出去，因為有些網站
        # 選單那條路要再點好幾層才看得到廠商，關鍵字反而一步到位。
        best["text_input_selector"] = text_form["input_selector"]
        best["text_submit_selector"] = text_form["submit_selector"]
    return best


def _best_text_form(soup: BeautifulSoup) -> dict[str, object] | None:
    """打關鍵字查詢的那一種查詢框。"""
    for node in soup.find_all("input"):
        node_type = (node.get("type") or "text").lower()
        if node_type not in ("text", "search"):
            continue
        submit = _find_submit_near(node)
        if submit is None:
            continue
        return {
            "input_selector": _anchor_css(node) or _css_for(node),
            "submit_selector": _submit_css(soup, submit),
            "option_count": 0,
            "sample": [],
        }
    return None


def _best_select_form(soup: BeautifulSoup) -> dict[str, object] | None:
    best: dict[str, object] | None = None
    for select in soup.find_all("select"):
        values = [
            (option.get("value") or "").strip()
            for option in select.find_all("option")
        ]
        real = [value for value in values if value]
        if len(real) < MIN_QUERY_OPTIONS:
            continue

        submit = _find_submit_near(select)
        if submit is None:
            continue

        candidate = {
            "input_selector": _anchor_css(select) or _css_for(select),
            "submit_selector": _submit_css(soup, submit),
            "option_count": len(real),
            "sample": [
                option.get_text(" ", strip=True)
                for option in select.find_all("option")
                if (option.get("value") or "").strip()
            ][:3],
        }
        # 選項最多的那一個最可能是主要的查詢條件；「每頁顯示 10／25／50 筆」
        # 這種選單永遠只有三四個選項，自然排在後面。
        if best is None or candidate["option_count"] > best["option_count"]:
            best = candidate
    return best


def _submit_css(soup: BeautifulSoup, submit: Tag) -> str:
    """查詢按鈕的 CSS 選擇器，而且要**只指到這一顆**。

    有分頁籤的查詢頁（公司名稱／商品類別／商品名稱各一個）會有好幾顆長得一模
    一樣的查詢鈕。挑到錯的那一顆，按下去什麼都不會發生——而且完全不會報錯，
    只會看到「查了 97 次，一筆都沒有」。所以這裡把選擇器綁在「跟這個輸入欄位
    同一區」的容器底下。
    """
    node_id = submit.get("id")
    if isinstance(node_id, str) and node_id.strip():
        return f"#{node_id.strip()}"

    own = _distinctive_css(submit)
    if len(soup.select(own)) == 1:
        return own

    # 往外一層一層加上容器，直到整頁只剩這一顆比得上。
    #
    # 「有 class 就算指得出來」是不夠的：四個分頁籤各有一個 div.input-group，
    # 用它當範圍照樣是四顆。唯一的判準只有「整頁比對到幾個」。
    fallback = own
    parent = submit.parent
    while isinstance(parent, Tag):
        scope = _anchor_css(parent)
        if scope:
            candidate = f"{scope} {own}"
            matched = soup.select(candidate)
            if len(matched) == 1:
                return candidate
            if len(matched) < len(soup.select(fallback)):
                fallback = candidate
        parent = parent.parent
    return fallback


def _distinctive_css(node: Tag) -> str:
    """節點的 ``tag.class``，class 挑最有辨識度的那一個。

    ``_css_for`` 拿的是第一個 class，Bootstrap 的按鈕第一個永遠是 ``btn``
    ——那會選中整頁所有的按鈕。真正認得出這顆按鈕的是 ``btnqry`` 那一種。
    """
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    usable = [
        c for c in classes
        if len(c) > 1 and not c.isdigit() and not _UTILITY_CLASS.fullmatch(c)
    ]
    if not usable:
        return node.name

    for name in usable:
        if any(word in name.lower() for word in _SUBMIT_TEXT):
            return f"{node.name}.{name}"
    # 沒有一個帶關鍵字的話，最長的那一個通常最具體（btn-info 勝過 btn）。
    return f"{node.name}.{max(usable, key=len)}"


def _inside_a_modal(node: Tag) -> bool:
    """這個元素是不是在一個彈出視窗裡。"""
    current: Tag | None = node
    for _ in range(8):
        if current is None:
            return False
        names = " ".join(
            [" ".join(current.get("class") or []), str(current.get("id") or "")]
        ).lower()
        if any(word in names for word in _MODAL_CLASSES):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _find_submit_near(select: Tag) -> Tag | None:
    """找出跟這個選單成對的「查詢」按鈕。

    先在同一個表單裡找，找不到再往上找兩層容器——很多頁面根本沒有 ``<form>``，
    整組查詢條件只是擺在同一個 ``<div>`` 裡。
    """
    # 由近而遠。整頁包一個 <form> 是 ASP.NET 的常態，那個範圍等於「整個網站」
    # ——先看它的話，找到的會是頁尾某個彈出視窗裡的「確定」，不是旁邊那顆查詢鈕。
    scopes: list[Tag] = []
    parent = select.parent
    for _ in range(4):
        if parent is None:
            break
        scopes.append(parent)
        parent = parent.parent
    form = select.find_parent("form")
    if form is not None and form not in scopes:
        scopes.append(form)

    for scope in scopes:
        for node in scope.find_all(["button", "input", "a"]):
            if node is select or _inside_a_modal(node):
                continue
            node_type = (node.get("type") or "").lower()
            if node.name == "input" and node_type not in ("submit", "button", "image"):
                continue
            haystack = " ".join(
                [
                    node.get_text(" ", strip=True),
                    str(node.get("value") or ""),
                    str(node.get("title") or ""),
                    str(node.get("aria-label") or ""),
                    " ".join(node.get("class") or []),
                    str(node.get("id") or ""),
                ]
            ).lower()
            if any(word in haystack for word in _SUBMIT_TEXT):
                return node
            # 只有一個放大鏡圖示、完全沒有文字的按鈕也很常見。
            if node.name == "button" and node.find("i") is not None:
                return node
    return None


def find_query_pagination(soup: BeautifulSoup, url: str) -> tuple[str, int] | None:
    """從「1 2 3 4 5」這種數字分頁推出帶 ``{page}`` 的網址樣板。

    回傳 ``(網址樣板, 看到的最大頁碼)``，找不到就是 ``None``。

    為什麼需要這個：:func:`find_next_selector` 只認得「下一頁」這種**文字**
    連結。但台灣的名錄網站絕大多數是純數字分頁——底下一排 1 2 3 4 5，沒有
    任何一個連結寫著「下一頁」。對那些站台，文字比對永遠找不到東西，來源就
    被存成「只爬第一頁」，而使用者完全不會知道自己只拿到第一頁。

    判斷方式是比較每個連結與目前網址的查詢參數：只有一個參數不同、而且那個
    參數的值是數字，就是頁碼。要看到至少兩個不同的數字才算數——只有一個的話
    可能只是某個帶編號的連結，不是分頁。
    """
    from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

    base = urlsplit(url)
    base_params = dict(parse_qsl(base.query))

    # 參數名稱 -> 看到的頁碼集合
    candidates: dict[str, set[int]] = {}

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not isinstance(href, str) or href.startswith(("#", "javascript:")):
            continue

        target = urlsplit(urljoin(url, href))
        if target.netloc != base.netloc or target.path != base.path:
            continue        # 連到別的頁面，不是同一份清單的分頁

        params = dict(parse_qsl(target.query))
        differing = [
            key for key in set(params) | set(base_params)
            if params.get(key) != base_params.get(key)
        ]
        if len(differing) != 1:
            continue

        key = differing[0]
        value = params.get(key, "")
        if not value.isdigit():
            continue
        candidates.setdefault(key, set()).add(int(value))

    usable = {k: v for k, v in candidates.items() if len(v) >= _MIN_PAGE_LINKS}
    if not usable:
        return None

    # 多個候選時，挑名稱最像頁碼的那個；都不像就挑頁碼數量最多的。
    key = min(
        usable,
        key=lambda k: (
            _PAGE_PARAM_HINTS.index(k.lower()) if k.lower() in _PAGE_PARAM_HINTS else 99,
            -len(usable[k]),
        ),
    )

    template_params = {**base_params, key: "{page}"}
    template = urlunsplit(
        (base.scheme, base.netloc, base.path, urlencode(template_params), base.fragment)
    )
    # urlencode 會把大括號轉成 %7B/%7D，換頁時代入才找得到佔位符。
    return template.replace("%7Bpage%7D", "{page}"), max(usable[key])


# ---------------------------------------------------------------- discovery


def discover_from_html(html: str, url: str) -> DiscoveryResult:
    """Analyse already-fetched HTML. Separated out so tests need no network."""
    soup = make_soup(html)
    result = DiscoveryResult(url=url)

    # 查詢表單要在「有沒有清單」之前先看。這一類頁面本來就什麼都沒有——正是
    # 因為還沒選條件——如果先因為「找不到清單」就回去，使用者只會看到
    # 「這一頁沒有資料」，而看不到「它有 98 個分類可以查」。
    result.query_form = find_query_form(soup)

    list_selector, items, notes = find_list_selector(soup)
    result.notes.extend(notes)
    if not items:
        if result.query_form:
            count = result.query_form["option_count"]
            result.notes.append(
                f"這一頁要先選一個條件才會有資料。偵測到一個有 {count} 個選項的"
                "下拉選單，勾「逐項查詢」就會一個一個查過去。"
            )
        return result

    result.list_selector = list_selector
    result.item_count = len(items)
    page_host = urlsplit(url).netloc.lower().removeprefix("www.")

    guessers = {
        "company_name": lambda: _guess_company_name(items),
        "email": lambda: _guess_email(items),
        "phone": lambda: _guess_phone(items),
        "website": lambda: _guess_website(items, page_host),
        "address": lambda: _by_class_hint("address", items)
        or _by_text_pattern("address", items, _ADDRESS_RE, _ADDRESS_RE.pattern),
        "industry": lambda: _by_class_hint("industry", items),
        "contact_person": lambda: _by_class_hint("contact_person", items),
        # 這三個只靠 class/id 名稱猜。它們沒有像信箱或電話那樣的內容特徵，
        # 硬用內容比對只會抓到一堆不相干的文字。
        "english_name": lambda: _by_class_hint("english_name", items),
        "fax": lambda: _by_class_hint("fax", items),
        "products": lambda: _by_class_hint("products", items),
        "tax_id": lambda: _guess_tax_id(items),
    }
    for name, guesser in guessers.items():
        try:
            guess = guesser()
        except Exception as exc:  # one bad field must not sink the whole guess
            log.warning("field guess for {} failed: {}", name, exc)
            continue
        if guess is not None:
            result.fields[name] = guess

    if "company_name" not in result.fields:
        result.notes.append(
            "找不到公司名稱欄位，請自行填入 CSS 選擇器（例如 h3.name）後再開始爬取。"
        )

    if result.query_form:
        count = result.query_form["option_count"]
        result.notes.append(
            f"這一頁上有一個 {count} 個選項的查詢選單。勾「逐項查詢」可以把每一個"
            "條件各查一次，抓到的會比現在這一頁多。"
        )

    result.detail_link_selector = find_detail_link_selector(items, page_host)
    if result.detail_link_selector and not (result.fields.keys() & {"email", "phone"}):
        result.notes.append(
            "列表頁沒有聯絡資訊，但每筆都連向各自的明細頁——爬取時會自動進入"
            "明細頁補抓信箱與電話（每筆多一次請求）。"
        )

    result.next_selector = find_next_selector(soup)

    # 數字分頁（1 2 3 4 5）——台灣的名錄網站絕大多數是這一種，而且沒有任何
    # 連結寫著「下一頁」，光靠文字比對永遠找不到。這裡順便算出總頁數。
    query_pagination = find_query_pagination(soup, url)
    if query_pagination is not None:
        result.page_url_template, result.page_count = query_pagination

    if result.next_selector is None and result.page_url_template is None:
        result.notes.append("找不到「下一頁」連結，將只爬取這一頁。")
    elif result.page_count > 1:
        estimated = result.page_count * result.item_count
        result.notes.append(
            f"偵測到共 {result.page_count} 頁（每頁 {result.item_count} 筆，"
            f"全部爬完約 {estimated} 筆）。「頁數上限」已自動填成 {result.page_count}"
            "，要少爬一點可以自己調小。"
        )

    result.document_links = find_document_links(soup, url)
    if result.document_links:
        listed = "、".join(
            f"{count} 個 {KIND_BY_KEY[kind].label.split('（')[0]}"
            for kind, count in result.document_links.items()
        )
        result.notes.append(
            f"這一頁還連出去 {listed}。名冊做成檔案掛在網站上是常見做法——"
            "要一起讀的話，在下面把對應的格式勾起來。"
        )

    result.suggested_actions = find_page_actions(soup, items)
    if result.suggested_actions:
        result.notes.append(
            "這一頁有「顯示電話 / 載入更多」之類的按鈕——那些資料不在原始網頁裡，"
            "要按下去才會出現。勾選「先點開頁面上的按鈕」就會自動點，"
            "但那需要用瀏覽器引擎，比一般爬取慢很多。"
        )

    result.preview = build_preview(items, result, url)

    if result.extra_field_samples:
        listed = "、".join(list(result.extra_field_samples)[:8])
        result.notes.append(
            f"這個名錄還有程式沒有固定欄位的資料：{listed}。"
            "會照原本的名稱一起收下來，在公司的「詳細資料」裡看得到、也可以修改。"
        )

    _add_quality_notes(result)
    return result


def build_preview(items: list[Tag], result: DiscoveryResult, url: str, limit: int = 10):
    """Extract records exactly as a real crawl would, for the preview table.

    「exactly」包含標籤解析——爬取時會補的欄位，預覽也要補，否則使用者會看到
    一片空白就以為這個名錄抓不到東西，而實際上抓得到。
    """
    rules = {name: guess.to_rule() for name, guess in result.fields.items()}
    preview: list[RawCompany] = []
    skipped_ads = 0
    for item in items[:limit]:
        values = extract_record(item, rules, url)
        name = (values.get("company_name") or "").strip()
        if not name:
            continue
        # Show the user what the crawl will actually keep. A preview full of
        # promotional cards that the pipeline later drops is misleading.
        if not classify(name, values.get("website")).is_company:
            skipped_ads += 1
            continue
        extra_fields = _apply_labels_for_preview(item, values, result)
        preview.append(
            RawCompany(
                **values, source="preview", source_url=url, extra_fields=extra_fields
            )
        )

    if skipped_ads:
        result.notes.append(
            f"已濾除 {skipped_ads} 筆疑似廣告或文章（可在 config.yaml 的 "
            "verifier.filter_advertisements 關閉）。"
        )
    return preview


def _apply_labels_for_preview(
    item: Tag, values: dict[str, str | None], result: DiscoveryResult
) -> dict[str, str]:
    """對一筆預覽套用標籤解析，順便記下有哪些自由欄位可以給使用者看。"""
    parsed = parse_record(item.get_text(" ", strip=True))
    if parsed.pair_count < MIN_PAIRS:
        return {}

    for name, value in parsed.fields.items():
        if not values.get(name):
            values[name] = value
    if not values.get("english_name"):
        _chinese, english = split_cjk_english(parsed.heading)
        if english:
            values["english_name"] = english

    for label, value in parsed.extra.items():
        samples = result.extra_field_samples.setdefault(label, [])
        if len(samples) < 3:
            samples.append(value[:60])
    return dict(parsed.extra)


def _add_quality_notes(result: DiscoveryResult) -> None:
    """Tell the user what looks weak, instead of quietly shipping bad data."""
    for name, guess in result.fields.items():
        if guess.hit_rate < 0.6:
            result.notes.append(
                f"欄位「{name}」只在 {guess.hit_rate:.0%} 的項目中找到，請確認選擇器是否正確。"
            )

    emails = [r.email for r in result.preview if r.email]
    bad_emails = [e for e in emails if not is_valid_email(e.replace("mailto:", ""))]
    if emails and len(bad_emails) > len(emails) / 2:
        result.notes.append("偵測到的信箱多數格式不正確，建議改用 mailto 連結的選擇器。")

    phones = [normalize_phone(r.phone) for r in result.preview if r.phone]
    if phones and not any(is_valid_phone(p) for p in phones if p):
        result.notes.append("偵測到的電話號碼看起來都不像有效號碼，請確認電話欄位。")

    tax_ids = [normalize_tax_id(r.tax_id) for r in result.preview if r.tax_id]
    if result.preview and not tax_ids and "tax_id" in result.fields:
        result.notes.append("統一編號欄位沒有抓到任何值。")


class SharedBrowser:
    """一個共用的瀏覽器，用到才開，用完由呼叫端關掉。

    一次加入十個名錄時，每一個 :func:`discover` 各自開一次 Chromium——光是啟動
    就 2～4 秒，十個就是半分多鐘純粹在等。這個類別讓那一整批共用同一個。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._fetcher: BaseFetcher | None = None
        #: 開失敗過就不再試。沒裝瀏覽器是正常情況，不必每一個網址都重試一次
        #: 並且各自印一行警告。
        self._unavailable = False

    def get(self) -> BaseFetcher | None:
        if self._unavailable:
            return None
        if self._fetcher is None:
            try:
                self._fetcher = build_fetcher(self._config, engine="playwright")
            except Exception as exc:          # noqa: BLE001 - 沒裝是正常情況
                log.info("no browser engine available: {}", exc)
                self._unavailable = True
                return None
        return self._fetcher

    def close(self) -> None:
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None


def discover(
    url: str,
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
    browser: "SharedBrowser | None" = None,
) -> DiscoveryResult:
    """Fetch ``url`` and work out how to scrape it.

    Goes through the normal fetcher, so robots.txt and the crawl delay apply
    to discovery exactly as they do to a crawl.
    """
    config = config or get_config()
    url = url.strip()
    if not url:
        raise CrawlError("請輸入網址。")
    if "://" not in url:
        url = "https://" + url

    owned = fetcher is None
    fetcher = fetcher or build_fetcher(config)
    try:
        page = fetcher.fetch(url)
    finally:
        if owned:
            fetcher.close()

    # 頁面自己宣告的編碼優先於 HTTP 標頭。台灣不少公協會名錄是 Big5 的舊站，
    # 標頭只寫 text/html 不附 charset，這時 HTTP 用戶端只能假設 UTF-8，整頁
    # 中文會變成亂碼——而亂碼的公司名稱看起來仍然「有值」，不會有任何錯誤。
    # 原始位元組留在 FetchResult 裡，所以換編碼重解不必再送一次請求。
    html = page.html
    declared = sniff_declared_encoding(page.raw) if page.raw else None
    if declared:
        html = decode_bytes(page.raw, declared)
        log.info("page declares charset {!r}; re-decoded", declared)

    log.info("analysing {} ({} bytes)", url, len(html))
    result = discover_from_html(html, page.url)
    result.encoding = declared
    if declared:
        result.notes.append(
            f"這個網站用的是 {declared.upper()} 編碼（不是現在通用的 UTF-8），"
            "已自動處理，中文不會變成亂碼。"
        )

    # 原始 HTML 裡沒有名單時，開一次真的瀏覽器再看一遍。
    #
    # 現在的名錄有一大半是「網頁先送空殼，再用 JavaScript 把資料填上去」。
    # 後端是 PHP、ASP.NET 還是回 JSON 的介面都沒有差別——差別只在於「原始
    # HTML 裡有沒有那些字」。與其每遇到一種新寫法就補一種對應，不如在這裡
    # 退一步：看不到就讓瀏覽器跑完再看。這一步是自動的，使用者不必知道
    # 「這個網站是 JavaScript 做的」，也不必去改任何設定。
    # 判斷「要不要重試」用的是 ``ok``（有清單而且抓得到公司名稱），不是「有沒有
    # 抓到東西」。JavaScript 頁面的空殼裡通常還是有幾個排版用的 div，偵測會挑一
    # 個出來當清單——筆數不是 0，但一個公司名稱都沒有。用筆數判斷的話，這種頁面
    # 永遠不會走到重試，而那正是最需要重試的一種。
    #
    # ``owned`` 才重試：呼叫端自己塞 fetcher 進來（測試、站內探索）時，它要的
    # 就是那一個，不該被我們偷換成瀏覽器。
    if not result.ok and owned:
        retried = _retry_in_a_browser(url, config, result, browser)
        if retried is not None:
            return retried

    log.info(
        "discovery: list={!r} items={} fields={}",
        result.list_selector, result.item_count, sorted(result.fields),
    )
    return result


#: 試查關鍵字查詢框時用的字。挑的是台灣公司行號名稱裡最普遍的幾個詞，
#: 目的只有一個：讓「一筆資料長什麼樣」現出原形。只會用到其中第一個查得
#: 出東西的，最多送出這麼多次查詢。
#: 只留兩個。每多一個就是多一次頁面載入加一次禮貌延遲，而分析是使用者盯著
#: 進度條在等的——第三個字幾乎不會比前兩個多查到什麼。
#
# 不要用「有限公司」這種太廣的字。有些網站對過於廣泛的查詢會直接回「請輸入更
# 完整的查詢條件」——那不是我們該去繞的東西，換一個具體一點的字就好。
_PROBE_WORDS = ("貿易", "企業")


#: 抓到的名稱裡至少要有這麼多比例真的像公司名，才算是一份名錄。
_MIN_COMPANY_RATIO = 0.4


def _looks_like_a_directory(result: DiscoveryResult) -> bool:
    """抓到的東西真的像一份廠商名錄嗎。

    ``ok`` 只問「有沒有清單、有沒有公司名稱欄位」，那在查詢型頁面上會被頁首
    的導覽選單騙過去——「加入會員」「找商機」「認識公會」重複度完美、長度平均，
    偵測會挑中它們，然後回報「找到 8 筆」。真正分得出來的只有內容本身。
    """
    if not result.ok:
        return False
    names = [
        (record.company_name or "").strip()
        for record in result.preview
        if (record.company_name or "").strip()
    ]
    if not names:
        return False
    marked = sum(1 for name in names if _has_company_marker(name))
    return marked / len(names) >= _MIN_COMPANY_RATIO


#: 中間那一層長什麼樣。真的踩到的都是表格；``list_selector`` 那一個由分析
#: 自己找出來，這裡只補上最常見的形狀。
_DRILL_ROW_CANDIDATES = ("table tbody tr", "table tr")


def _drilled_page_has_companies(html: str, shape: DiscoveryResult) -> bool:
    """拿已經學會的欄位規則，去看往下點一層之後的頁面上有沒有廠商。

    為什麼不能再叫一次 :func:`discover_from_html`：往下點一層之後那一頁上
    常常只有一兩家公司（實測 ieatpe 的第一個 HS 分類就只有一家），而「找出
    重複最多的區塊」在那種頁面上會挑中頁首的導覽選單——八個項目、完美重複，
    分數比一家公司的表格高。結論是「這條路走不通」，然後把已經點到的那家
    公司連同整條路線一起丟掉。實際發生過，使用者爬回來 21 筆全是分類代號。

    這裡問的是另一個問題：**已經知道欄位長什麼樣了**（從同一個網站的另一條
    路線學到的），拿它去套這一頁，套得出公司名稱嗎？套得出來就代表這條路
    通了，一家也算——往下點一層本來就是一次點一個分類，分類底下有幾家公司
    是那個分類的事，不是選擇器的事。
    """
    selector = shape.list_selector
    rule = (shape.fields or {}).get("company_name")
    if not selector or rule is None:
        return False
    soup = make_soup(html)
    for item in soup.select(selector)[:200]:
        found = item.select_one(rule.selector) if rule.selector else None
        text = found.get_text(" ", strip=True) if found is not None else ""
        if text and _has_company_marker(text):
            return True
    return False


def _try_drilling_one_level(
    probe, url: str, input_selector: str, submit_selector: str,
    value: str | None, html: str, shape: DiscoveryResult | None = None,
) -> tuple[DiscoveryResult, dict[str, str]] | None:
    """查出來的不是名單時，往下再點一層看看。

    有些網站是三層的：選一個大分類 → 出來一張子分類清單 → 點其中一項才看得到
    廠商。中間那一層看起來很像資料，裡面卻一家公司都沒有。

    ``shape`` 是同一個網站另一條路線已經學會的欄位規則。有它的時候用它來判斷
    （見 :func:`_drilled_page_has_companies`），沒有的話才自己從頭分析一次。

    回傳 ``(分析結果, 往下點的設定)``，點了還是沒有名單就回 ``None``。
    """
    soup = make_soup(html)
    candidates: list[str] = []
    for selector in _DRILL_ROW_CANDIDATES:
        rows = soup.select(selector)
        if rows and any(row.find("a") is not None for row in rows):
            candidates.append(selector)
    if not candidates:
        return None

    for row_selector in candidates[:1]:       # 一次就好，每一次都是一趟請求
        try:
            page = probe(
                url, input_selector, submit_selector,
                value=value, drill_row_selector=row_selector,
            )
        except Exception as exc:              # noqa: BLE001
            log.info("往下點一層失敗（{}）：{}", row_selector, exc)
            continue

        drill = {"row_selector": row_selector, "click_selector": "a"}
        if shape is not None and _drilled_page_has_companies(page.html, shape):
            log.info("往下點一層之後找到廠商（沿用已知欄位）：{}", row_selector)
            return shape, drill

        after = discover_from_html(page.html, page.url)
        if _looks_like_a_directory(after):
            log.info("往下點一層之後找到名單：{}", row_selector)
            return after, drill
    return None


def _analyse_after_one_query(
    browser: BaseFetcher, url: str, before: DiscoveryResult
) -> DiscoveryResult:
    """送出一次查詢，改用結果那一頁來分析。查不出東西就維持原樣。"""
    form = before.query_form or {}
    probe = getattr(browser, "fetch_with_first_query", None)
    if probe is None:
        return before

    select_input = str(form.get("input_selector") or "")
    select_submit = str(form.get("submit_selector") or "")

    attempts: list[tuple[str, str, str | None, str]] = []
    if select_input:
        attempts.append((
            select_input, select_submit, None,
            f"已經自動試查了一次（{form.get('option_count')} 個條件之一）",
        ))
    # 選單那條路查出來未必是廠商——有些網站要再點一層（先給你商品分類，
    # 點下去才是廠商）。同一頁上的關鍵字查詢框常常一步到位，所以也試一次。
    if form.get("text_input_selector"):
        for word in _PROBE_WORDS:
            attempts.append((
                str(form["text_input_selector"]),
                str(form.get("text_submit_selector") or ""),
                word,
                f"已經自動用「{word}」試查了一次",
            ))

    #: 選單那條路查出來的那一頁。它自己走不通的時候先留著——關鍵字那條走通
    #: 之後，會拿學到的欄位規則回頭再確認一次選單＋往下點一層。
    select_html: str | None = None

    for input_selector, submit_selector, value, note in attempts:
        try:
            page = probe(url, input_selector, submit_selector, value=value)
        except Exception as exc:              # noqa: BLE001
            log.info("試查失敗（{}）：{}", value or "選單第一項", exc)
            continue

        after = discover_from_html(page.html, page.url)
        drill: dict[str, str] | None = None
        # 一樣要看內容像不像廠商名錄。選單那條路常常查出來是「商品分類」而不是
        # 廠商，而頁首的導覽選單在任何一頁上都在——只問 ``ok`` 的話，第一次試查
        # 就會帶著一份「加入會員、找商機、認識公會」收工。
        if not _looks_like_a_directory(after):
            if value is not None:
                # 關鍵字查詢的結果本來就直接是廠商，不是的話再點一層多半也沒
                # 有用——而每一次試點都是一趟請求，分析是使用者盯著在等的。
                continue
            select_html = page.html
            drilled = _try_drilling_one_level(
                probe, url, input_selector, submit_selector, value, page.html
            )
            if drilled is None:
                continue
            after, drill = drilled

        route = "select" if value is None else "text"

        # 關鍵字那條走通、而選單那條剛剛沒走通的話，回頭再確認一次。
        #
        # 這一步是必要的，不是附加價值：欄位長什麼樣是「結果表」的性質，不是
        # 「走哪一條路」的性質，兩條路最後渲染的是同一張表。少了這一步，分析
        # 會交出一份自相矛盾的設定——欄位是關鍵字那條學來的，查詢表單卻是選單
        # 那條的，而讓選單那條能用的 drill 被丟掉了。使用者照著跑，抓回來的
        # 全是分類代號，一筆都存不進去。實際發生過。
        if route == "text" and select_html is not None and select_input:
            drilled = _try_drilling_one_level(
                probe, url, select_input, select_submit, None, select_html, shape=after
            )
            if drilled is not None:
                _, drill = drilled
                route = "select"
                note = f"已經自動試查了一次（{form.get('option_count')} 個條件之一）"

        # 查詢表單本身要留著——結果頁上的那個選單可能已經被換過內容了，而使用者
        # 要看到的是「總共有幾個條件可以查」。
        after.query_form = dict(before.query_form or {})
        # 哪一條路是真的驗證過走得通的。沒有這一項，畫面上會理直氣壯地寫著
        # 「有 97 個選項，勾起來一個查一次」，而那條路其實是壞的。
        after.query_form["verified_route"] = route
        if drill:
            after.query_form["drill"] = drill
            after.notes.append(
                "查出來的還不是廠商名單，中間要再點一層才是——已經確認過點下去"
                "會出現廠商，爬取時會自動做這一步（每一列都是一次往返，會慢很多）。"
            )
        after.notes.append(
            f"這一頁要先查詢才有資料，{note}，下面的欄位是從查詢結果推出來的。"
        )
        if route == "text":
            after.notes.append(
                "這個網站的選單查出來不是廠商，只有打關鍵字這條路走得通——"
                "勾「逐項查詢」之後要自己填要查哪些字（一個字查一次）。"
            )
        return after

    return before


def _retry_in_a_browser(
    url: str,
    config: AppConfig,
    before: DiscoveryResult,
    shared: "SharedBrowser | None" = None,
) -> DiscoveryResult | None:
    """用瀏覽器重新分析一次；沒有比較好就回傳 None。

    「比較好」有兩種：抓到了原本沒有的清單，或是看到了原本看不到的查詢選單
    （選單的選項本身也常常是 JavaScript 填上去的）。瀏覽器版比較慢也比較容易
    出錯，拿它換一個同樣的結果沒有意義。

    ``shared`` 是一批網址共用的瀏覽器。一次加入十個名錄時，各開一次 Chromium
    光啟動就要多花半分鐘。給了就借來用，關掉是呼叫端的事。
    """
    if config.crawler.engine == "playwright":
        return None                       # 剛剛那一次就已經是瀏覽器了

    if shared is not None:
        browser = shared.get()
        if browser is None:
            return None
    else:
        try:
            browser = build_fetcher(config, engine="playwright")
        except Exception as exc:          # noqa: BLE001 - 沒裝瀏覽器是正常情況
            log.info("no browser engine available for the retry: {}", exc)
            return None

    try:
        page = browser.fetch(url)
        result = discover_from_html(page.html, page.url)

        # 查詢型頁面：還沒查詢之前一筆資料都沒有，只看那一頁是猜不出「一筆
        # 資料長什麼樣」的——偵測只好把 <option> 之類的東西當成清單。先送出
        # 一次查詢再分析結果，跟人打開網頁隨便選一個分類按下去是一樣的動作。
        if result.query_form and not _looks_like_a_directory(result):
            result = _analyse_after_one_query(browser, url, result)
    except Exception as exc:              # noqa: BLE001
        log.warning("browser retry failed: {}", exc)
        return None
    finally:
        if shared is None:
            browser.close()

    better_list = result.ok
    before_options = int((before.query_form or {}).get("option_count", 0) or 0)
    after_options = int((result.query_form or {}).get("option_count", 0) or 0)
    better_form = after_options > before_options
    if not (better_list or better_form):
        return None

    result.engine = "playwright"
    result.notes.append(
        "這一頁的內容是網頁開起來之後才由程式填上去的，直接讀原始碼看不到。"
        "已經改用內建瀏覽器重新分析，這個來源之後也會自動用同一種方式爬取"
        "（比較慢，但抓得到）。"
    )
    log.info(
        "browser retry: {} items, {} query options on {}",
        result.item_count, after_options, url,
    )
    return result
