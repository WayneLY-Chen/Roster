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
from crawler.fetcher import BaseFetcher, build_fetcher
from crawler.parser import extract_record, make_soup
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
        )


# --------------------------------------------------------------- selectors


def _signature(node: Tag) -> str:
    """Structural fingerprint of an element: tag plus its class list."""
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return f"{node.name}.{'.'.join(sorted(classes))}" if classes else node.name


def _css_for(node: Tag) -> str:
    """Shortest stable CSS selector for an element (tag or tag.class)."""
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    # Skip utility classes that carry no meaning and change between builds.
    meaningful = [
        c for c in classes
        if len(c) > 1 and not c.isdigit() and not re.fullmatch(r"(col|row|p|m|d)-?\w{0,3}\d*", c)
    ]
    return f"{node.name}.{meaningful[0]}" if meaningful else node.name


def _text(node: Tag) -> str:
    return node.get_text(" ", strip=True)


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

        score = (
            len(items) * 1.0
            + emails * 4.0
            + phones * 3.0
            + headings * 2.5
            + min(links, len(items) * 3) * 0.5
            + (10.0 if "." in signature else 0.0)   # a class-based block is a real component
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

    # A tag-only selector may over-match; check it and warn rather than silently
    # collecting the wrong nodes.
    matched = soup.select(best_selector)
    if len(matched) > len(best_items) * 1.5:
        notes.append(
            f"選擇器 {best_selector} 在整頁比對到 {len(matched)} 個區塊，"
            f"但偵測到的清單只有 {len(best_items)} 筆，建議手動確認。"
        )
    return best_selector, best_items, notes


#: 內層元素要比外層多這麼多倍，才值得往下鑽。
_DRILL_DOWN_RATIO = 1.5


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

    counter: Counter[str] = Counter()
    examples: dict[str, Tag] = {}
    for item in outer_items:
        for child in item.find_all(True):
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
        return candidate, inner_items

    return None


# ------------------------------------------------------------------ fields


def _validated(field_name: str, selector: str, attr: str, items: list[Tag],
               regex: str | None = None) -> FieldGuess | None:
    """Apply a candidate rule to every item and keep it if it hits often enough."""
    rule = FieldRule(selector=selector, attr=attr, regex=regex)
    samples: list[str] = []
    hits = 0
    for item in items:
        from crawler.parser import extract_field

        value = extract_field(item, rule)
        if value:
            hits += 1
            if len(samples) < 3:
                samples.append(value[:80])

    hit_rate = hits / len(items) if items else 0.0
    if hit_rate < MIN_FIELD_HIT_RATE:
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
    if len(text) >= 4 and text.endswith(_COMPANY_SUFFIXES):
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
        guess = _validated("website", selector, "href", items)
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

    list_selector, items, notes = find_list_selector(soup)
    result.notes.extend(notes)
    if not items:
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

    result.preview = build_preview(items, result, url)
    _add_quality_notes(result)
    return result


def build_preview(items: list[Tag], result: DiscoveryResult, url: str, limit: int = 10):
    """Extract records exactly as a real crawl would, for the preview table."""
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
        preview.append(RawCompany(**values, source="preview", source_url=url))

    if skipped_ads:
        result.notes.append(
            f"已濾除 {skipped_ads} 筆疑似廣告或文章（可在 config.yaml 的 "
            "verifier.filter_advertisements 關閉）。"
        )
    return preview


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


def discover(
    url: str,
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
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

    log.info("analysing {} ({} bytes)", url, len(page.html))
    result = discover_from_html(page.html, page.url)
    log.info(
        "discovery: list={!r} items={} fields={}",
        result.list_selector, result.item_count, sorted(result.fields),
    )
    return result
