"""HTML parsing helpers shared by every source.

The extraction model is deliberately declarative: a source maps field names to
:class:`~core.config.FieldRule` objects in ``config.yaml``, and this module
applies them. Adding a new directory means writing YAML, not Python.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from core.config import FieldRule
from core.constants import LogCategory
from core.logging_setup import get_logger
from verifier.normalize import extract_emails

log = get_logger(LogCategory.CRAWL)

# 前後的 (?<!\d) / (?!\d) 是必要的：少了它，比對會從一長串數字的中間切進去。
# TAMI 的列表把統一編號和地址的郵遞區號排在一起，"30878408 110台北市" 就會從
# 統編中間的 0 開始，被讀成電話 "0878408 110"——實測 190 筆裡有 37 筆這樣誤判。
_PHONE_IN_TEXT = re.compile(
    r"(?<!\d)(?:\+886[\s\-]?|0)\d{1,3}[\s\-]?\d{3,4}[\s\-]?\d{3,4}"
    r"(?:\s*(?:#|轉|分機)\s*\d{1,6})?(?!\d)"
)


def make_soup(html: str) -> BeautifulSoup:
    """Parse HTML with lxml, falling back to the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - only when lxml is missing/broken
        log.warning("lxml unavailable; falling back to html.parser")
        return BeautifulSoup(html, "html.parser")


def _raw_value(node: Tag, attr: str) -> str:
    if attr == "text":
        return node.get_text(" ", strip=True)
    if attr == "html":
        return node.decode_contents()
    value = node.get(attr)
    if isinstance(value, list):  # e.g. class="a b"
        return " ".join(value)
    return str(value) if value is not None else ""


def extract_field(scope: Tag, rule: FieldRule, base_url: str | None = None) -> str | None:
    """Apply one :class:`FieldRule` within ``scope``.

    Returns ``None`` when the selector matches nothing or the value is empty,
    so a missing optional field never becomes an empty string in the database.
    """
    try:
        nodes = scope.select(rule.selector)
    except Exception as exc:
        log.warning("invalid CSS selector {!r}: {}", rule.selector, exc)
        return None
    if not nodes:
        return None

    chosen = nodes if rule.multiple else nodes[:1]
    values: list[str] = []
    for node in chosen:
        value = _raw_value(node, rule.attr).strip()
        if not value:
            continue
        if base_url and rule.attr in ("href", "src"):
            value = urljoin(base_url, value)
        if rule.regex:
            match = re.search(rule.regex, value)
            if not match:
                continue
            value = (match.group(1) if match.groups() else match.group(0)).strip()
        if value:
            values.append(value)

    if not values:
        return None
    return rule.separator.join(values) if rule.multiple else values[0]


def extract_record(
    scope: Tag, rules: dict[str, FieldRule], base_url: str | None = None
) -> dict[str, str | None]:
    """Apply every rule to one list item."""
    return {name: extract_field(scope, rule, base_url) for name, rule in rules.items()}


def select_items(soup: BeautifulSoup, selector: str) -> list[Tag]:
    """All list items on a page. An empty result usually means end-of-results."""
    try:
        return [node for node in soup.select(selector) if isinstance(node, Tag)]
    except Exception as exc:
        log.error("invalid list selector {!r}: {}", selector, exc)
        return []


def find_next_url(soup: BeautifulSoup, selector: str, base_url: str) -> str | None:
    """Resolve a "next page" link to an absolute URL."""
    node = soup.select_one(selector)
    if node is None:
        return None
    href = node.get("href")
    if not href or (isinstance(href, str) and href.strip().startswith("#")):
        return None
    return urljoin(base_url, href if isinstance(href, str) else href[0])


def harvest_emails(scope: Tag) -> list[str]:
    """Every address on the page: ``mailto:`` links first, then body text."""
    found: list[str] = []
    for link in scope.select("a[href^='mailto:']"):
        href = link.get("href")
        if isinstance(href, str):
            found.extend(extract_emails(href))
    for address in extract_emails(scope.get_text(" ", strip=True)):
        if address not in found:
            found.append(address)
    return found


def harvest_phones(scope: Tag) -> list[str]:
    """Every phone-shaped string on the page, ``tel:`` links first."""
    found: list[str] = []
    for link in scope.select("a[href^='tel:']"):
        href = link.get("href")
        if isinstance(href, str):
            candidate = href[len("tel:") :].strip()
            if candidate and candidate not in found:
                found.append(candidate)
    for match in _PHONE_IN_TEXT.findall(scope.get_text(" ", strip=True)):
        if match not in found:
            found.append(match.strip())
    return found


def page_title(soup: BeautifulSoup) -> str | None:
    node = soup.find("title")
    return node.get_text(strip=True) if node else None
