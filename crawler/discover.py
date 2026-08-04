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


def _guess_company_name(items: list[Tag]) -> FieldGuess | None:
    """Company name: a heading, a prominent link, or the item's first line."""
    candidates: Counter[str] = Counter()
    for item in items:
        heading = item.find(re.compile("^h[1-6]$"))
        if heading is not None:
            candidates[_css_for(heading)] += 3
        for node in item.find_all(("a", "strong", "b")):
            text = _text(node)
            if 2 <= len(text) <= 60:
                candidates[_css_for(node)] += 1

    for selector, _score in candidates.most_common(6):
        guess = _validated("company_name", selector, "text", items)
        if guess and all(len(s) >= 2 for s in guess.samples):
            guess.reason = "標題或主要連結"
            return guess

    return _by_class_hint("company_name", items)


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
    """Locate a "next page" link by rel, text or class."""
    node = soup.select_one("a[rel='next']")
    if node is not None:
        return "a[rel='next']"

    for anchor in soup.find_all("a", href=True):
        haystack = " ".join(
            [_text(anchor).lower(), " ".join(anchor.get("class") or []),
             str(anchor.get("title") or "")]
        ).lower()
        if any(marker in haystack for marker in _NEXT_TEXT):
            selector = _css_for(anchor)
            if len(soup.select(selector)) <= 3:
                return selector
    return None


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
    if result.next_selector is None:
        result.notes.append("找不到「下一頁」連結，將只爬取這一頁。")

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
