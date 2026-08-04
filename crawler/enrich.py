"""從公司自己的網站補抓公開信箱。

名錄網站幾乎都只在列表頁放「公司名稱＋網址」，信箱要到該公司自己的網站才有。
這一步就是把那一哩補起來：對已經有網址卻沒有信箱的公司，去它官網找出公開
刊登的聯絡信箱。

規矩與爬名錄時完全相同，一條都不放寬：

* 逐一檢查**每個公司網域自己的** robots.txt，不允許就跳過
* 沿用同一個速率限制器，每次請求之間有延遲
* 只讀公開頁面，不碰登入頁、不填表單
* 只擷取網頁上**明文刊登**的信箱；用 JavaScript 或 Cloudflare 混淆的信箱
  一律不處理——那是對方明示不希望被自動蒐集

找信箱的順序是先首頁、再「聯絡我們」/「關於我們」這類頁面，找到就停，
所以正常情況下一家公司只會產生一到兩次請求。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import CrawlError, RobotsDisallowedError
from core.logging_setup import get_logger
from crawler.fetcher import BaseFetcher, build_fetcher
from crawler.parser import harvest_emails, make_soup
from database.models import now
from database.repository import CompanyRepository
from database.session import session_scope
from verifier.normalize import normalize_email
from verifier.validators import is_role_address, is_valid_email

log = get_logger(LogCategory.CRAWL)

#: 依序嘗試的聯絡頁路徑，命中率高的排前面。
CONTACT_PATHS = (
    "/contact",
    "/contact-us",
    "/contactus",
    "/about",
    "/about-us",
    "/zh-tw/contact",
    "/tw/contact",
    "/contact.html",
    "/contact.php",
    "/about.html",
)

#: 連結文字符合這些字樣時，視為聯絡頁。
_CONTACT_TEXT = re.compile(
    r"聯絡|連絡|聯繫|客服|洽詢|關於我們|contact|about", re.IGNORECASE
)

#: 這些網域不是公司自己的網站，抓到也沒有意義。
_SKIP_HOSTS = (
    "facebook.com", "instagram.com", "line.me", "youtube.com",
    "shopee.tw", "ruten.com.tw", "google.com", "blogspot.com",
)

#: 明顯不是聯絡窗口的信箱。
_JUNK_LOCAL_PARTS = frozenset(
    {"example", "test", "your", "name", "someone", "email", "user", "sample"}
)


@dataclass
class EnrichSummary:
    """一次補抓作業的結果。"""

    considered: int = 0
    visited: int = 0
    emails_found: int = 0
    updated: int = 0
    skipped_robots: int = 0
    skipped_no_site: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _site_host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _is_own_domain(email: str, host: str) -> bool:
    """信箱網域是否就是這家公司的網域（含子網域）。"""
    domain = email.rpartition("@")[2].lower()
    return domain == host or domain.endswith("." + host) or host.endswith("." + domain)


def _rank(email: str, host: str) -> tuple[int, int, str]:
    """排序用：同網域優先，其次是 info@ 這類公開窗口。"""
    return (
        0 if _is_own_domain(email, host) else 1,
        0 if is_role_address(email) else 1,
        email,
    )


def _usable(email: str | None) -> bool:
    if not email or not is_valid_email(email):
        return False
    local = email.partition("@")[0].lower()
    if local in _JUNK_LOCAL_PARTS:
        return False
    # 圖片檔名常被誤判成信箱，例如 logo@2x.png
    return not email.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"))


def _contact_links(html: str, base_url: str) -> list[str]:
    """頁面上指向聯絡頁的連結，同網域者優先。"""
    soup = make_soup(html)
    host = _site_host(base_url)
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not isinstance(href, str) or href.startswith(("mailto:", "tel:", "#")):
            continue
        text = f"{anchor.get_text(' ', strip=True)} {href}"
        if not _CONTACT_TEXT.search(text):
            continue
        absolute = urljoin(base_url, href)
        if _site_host(absolute) != host:
            continue
        if absolute not in found:
            found.append(absolute)
    return found[:3]


def emails_from_page(html: str, base_url: str) -> list[str]:
    """頁面上所有可用的信箱，依「像不像官方窗口」排序。"""
    soup = make_soup(html)
    host = _site_host(base_url)
    candidates: list[str] = []
    for raw in harvest_emails(soup):
        email = normalize_email(raw)
        if _usable(email) and email not in candidates:
            candidates.append(email)
    return sorted(candidates, key=lambda e: _rank(e, host))


def find_email_for_site(
    website: str, fetcher: BaseFetcher, max_pages: int = 3
) -> tuple[str | None, int]:
    """在一個公司網站上找公開信箱。回傳 ``(信箱, 實際請求次數)``。

    找到就立刻停止，不會把整站走完。
    """
    requests_made = 0
    try:
        first = fetcher.fetch(website)
    except RobotsDisallowedError:
        raise
    except CrawlError as exc:
        log.debug("無法讀取 {}：{}", website, exc)
        return None, requests_made
    requests_made += 1

    emails = emails_from_page(first.html, first.url)
    if emails:
        return emails[0], requests_made

    # 首頁沒有，改看聯絡頁。先用頁面上真的存在的連結，再退回常見路徑。
    targets = _contact_links(first.html, first.url)
    if not targets:
        origin = f"{urlsplit(first.url).scheme}://{urlsplit(first.url).netloc}"
        targets = [origin + path for path in CONTACT_PATHS[:3]]

    for target in targets:
        if requests_made >= max_pages:
            break
        try:
            page = fetcher.fetch(target)
        except RobotsDisallowedError:
            raise
        except CrawlError:
            continue
        requests_made += 1
        emails = emails_from_page(page.html, page.url)
        if emails:
            return emails[0], requests_made

    return None, requests_made


def enrich_missing_emails(
    limit: int | None = None,
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    company_ids: Iterable[int] | None = None,
) -> EnrichSummary:
    """對「有網址、沒信箱」的公司補抓官網上的公開信箱。

    每家公司都是獨立網域，因此每一家都會各自檢查一次 robots.txt。
    """
    config = config or get_config()
    summary = EnrichSummary()

    owned = fetcher is None
    fetcher = fetcher or build_fetcher(config)

    try:
        with session_scope() as session:
            repo = CompanyRepository(session)

            if company_ids is not None:
                targets = [c for c in (repo.get(i) for i in company_ids) if c is not None]
            else:
                targets = [
                    c for c in repo.all()
                    if c.website and not c.email
                ]
            if limit:
                targets = targets[:limit]
            summary.considered = len(targets)

            for index, company in enumerate(targets, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    log.warning("補抓信箱已取消")
                    break

                website = company.website or ""
                host = _site_host(website)
                if not host or any(bad in host for bad in _SKIP_HOSTS):
                    summary.skipped_no_site += 1
                    continue

                if progress is not None:
                    progress(index, len(targets), company.company_name)

                try:
                    email, requests_made = find_email_for_site(website, fetcher)
                except RobotsDisallowedError:
                    summary.skipped_robots += 1
                    log.info("{} 的 robots.txt 不允許，略過", host)
                    continue
                except Exception as exc:
                    summary.failed += 1
                    message = f"{company.company_name}：{type(exc).__name__}: {exc}"
                    summary.errors.append(message)
                    log.warning(message)
                    continue

                summary.visited += requests_made
                if not email:
                    continue

                summary.emails_found += 1
                if not company.email:
                    company.email = email
                    company.updated_at = now()
                    summary.updated += 1
                    log.info("{} -> {}", company.company_name, email)

                session.commit()
    finally:
        if owned:
            fetcher.close()

    log.info(
        "補抓完成：檢查 {} 家、送出 {} 次請求、找到 {} 個信箱、更新 {} 筆",
        summary.considered, summary.visited, summary.emails_found, summary.updated,
    )
    return summary
