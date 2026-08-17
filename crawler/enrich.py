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

## 不只是信箱

:func:`enrich_missing_emails` 是原本就有的那一支，只補信箱，行為沒有變。
:func:`harvest_site_contacts` 是同一趟路上順便多帶回來的東西——電話、傳真、
聯絡人。既然頁面都已經抓下來了，只解析信箱等於白白丟掉另外三個欄位；而
**多解析幾個欄位不會多送任何一次請求**，對方的負擔完全一樣。

電話與傳真靠 :mod:`crawler.labels` 認標籤區分（「傳真︰」才是傳真），沒有
標籤時退回頁面上任何電話形狀的字串。這在公司自己的網站上是安全的——頁尾
那支電話就是這家公司的；同一招用在名錄的列表頁上就會抓到公會自己的總機，
所以這支函式只給「已經確定是這家公司的網站」使用。
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
from crawler.labels import parse_record
from crawler.parser import harvest_emails, harvest_phones, make_soup, phones_in_text
from database.models import now
from database.repository import CompanyRepository
from database.session import session_scope
from verifier.normalize import (
    company_name_key,
    comparison_text,
    normalize_email,
    normalize_person_name,
    normalize_phone,
)
from verifier.validators import (
    is_role_address,
    is_tracking_address,
    is_valid_email,
    is_valid_phone,
)

log = get_logger(LogCategory.CRAWL)

#: 依序嘗試的聯絡頁路徑，命中率高的排前面。
#:
#: 這一串只在**首頁上一個像聯絡頁的連結都找不到**時才會用到，所以它猜錯的
#: 成本是實打實的請求。順序就是台灣公司網站的實際分佈：``/contact`` 系列
#: 最常見，其次是繁中語系前綴（多語系網站的根目錄常常是英文版或跳轉頁，
#: 連結抓不到），最後才是 ``/about`` 系列。
CONTACT_PATHS = (
    "/contact",
    "/contact-us",
    "/contactus",
    "/zh-tw/contact",
    "/tw/contact",
    "/contact.html",
    "/contact.php",
    "/about",
    "/about-us",
    "/about.html",
)

#: 連結文字符合這些字樣時，視為聯絡頁。
_CONTACT_TEXT = re.compile(
    r"聯絡|連絡|聯繫|连络|客服|洽詢|詢價|據點|關於我們|關於|"
    r"contact|about|inquiry|enquiry",
    re.IGNORECASE,
)

#: 這些字樣「一定是聯絡頁」，比 :data:`_CONTACT_TEXT` 其餘的（``about``、
#: ``關於``）更值得先點。
#:
#: 這個區分有用是因為頁數有上限：``關於我們`` 常常是公司沿革、董事長的話，
#: 信箱不在那裡。兩者都符合時先點真正的聯絡頁，等於用同樣的請求次數換到
#: 更高的命中率。
_STRONG_CONTACT_TEXT = re.compile(
    r"聯絡|連絡|聯繫|连络|客服|洽詢|詢價|contact|inquiry|enquiry", re.IGNORECASE
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
    # 機器產生的識別碼（Sentry 的 DSN、圖檔名）。這裡就要擋掉，不能只靠存進
    # 資料庫前那一關：擋在這裡它才不會**排在真正的信箱前面被選走**——用 Wix
    # 架的網站幾乎每一個都有一個 Sentry DSN 埋在 JavaScript 裡。
    return not is_tracking_address(email)


def _contact_links(html: str, base_url: str, limit: int = 3) -> list[str]:
    """頁面上指向聯絡頁的連結，真正的聯絡頁排在「關於我們」前面。

    只收同網域的連結。排序是穩定的：先照 :data:`_STRONG_CONTACT_TEXT` 分成
    兩組，組內維持原本在頁面上的順序。
    """
    soup = make_soup(html)
    host = _site_host(base_url)
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not isinstance(href, str) or href.startswith(("mailto:", "tel:", "#")):
            continue
        text = f"{anchor.get_text(' ', strip=True)} {href}"
        if not _CONTACT_TEXT.search(text):
            continue
        absolute = urljoin(base_url, href)
        if _site_host(absolute) != host or absolute in seen:
            continue
        seen.add(absolute)
        found.append((0 if _STRONG_CONTACT_TEXT.search(text) else 1, absolute))
    found.sort(key=lambda pair: pair[0])
    return [url for _, url in found[:limit]]


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


#: 聯絡人姓名再長就不是姓名了，是把一整句話收進來。
#: 「總經理 林伯洲」是 7 個字，複姓加職稱也到不了 30。
MAX_PERSON_CHARS = 30

#: 這些字出現在「聯絡人」欄位裡代表那不是一個人名，是版面文字。
_NOT_A_PERSON = re.compile(
    r"\d|@|http|www|公司|有限|股份|地址|電話|傳真|信箱|服務|時間|表單|我們",
    re.IGNORECASE,
)

#: :func:`harvest_site_contacts` 認得的欄位，也是 ``wanted`` 的合法值。
HARVESTABLE_FIELDS = ("email", "phone", "fax", "contact_person")


@dataclass
class SiteContacts:
    """在一個公司網站上找到的聯絡資料。每個欄位都可能是 ``None``。"""

    email: str | None = None
    phone: str | None = None
    fax: str | None = None
    contact_person: str | None = None
    #: 這個網站確實是這家公司的嗎？
    #:
    #: 只有在 :func:`harvest_site_contacts` 被要求驗證（``confirm_name``）時
    #: 才有意義。沒要求驗證時一律是 ``True``——呼叫端本來就已經知道網址是
    #: 對的（例如名錄上直接列出來的），沒有什麼要確認的。
    confirmed: bool = True

    def get(self, field_name: str) -> str | None:
        return getattr(self, field_name, None)

    def fill_from(self, other: "SiteContacts") -> None:
        """把自己還空著的欄位用 ``other`` 補起來。已經有值的不動。

        逐頁累積用：首頁只有電話、聯絡頁才有信箱是很常見的排版。
        """
        for field_name in HARVESTABLE_FIELDS:
            if self.get(field_name) is None:
                setattr(self, field_name, other.get(field_name))

    def satisfies(self, wanted: Iterable[str]) -> bool:
        """``wanted`` 裡的每一個欄位都已經有值了嗎？"""
        return all(self.get(field_name) is not None for field_name in wanted)

    def is_empty(self) -> bool:
        return all(self.get(field_name) is None for field_name in HARVESTABLE_FIELDS)


def _usable_person(value: str | None) -> str | None:
    """把一個候選值收成聯絡人姓名，不像姓名就回 ``None``。"""
    name = normalize_person_name(value)
    if not name or len(name) > MAX_PERSON_CHARS:
        return None
    return None if _NOT_A_PERSON.search(name) else name


def _usable_phone(value: str | None) -> str | None:
    """把一個候選值收成電話號碼。看起來不是電話就回 ``None``。

    先從值裡切出第一段電話形狀的字串再正規化，不要整串丟進去。標籤解析出來
    的值常常不只一個號碼——``傳真： 02-2723-5678 02-2723-1234`` 後面沒有第二
    個標籤可以斷開，整段都是「傳真」的值。整串正規化的結果是一個哪一支都不
    是的號碼，而且它會被 :func:`~verifier.validators.is_valid_phone` 擋掉，
    於是傳真變成空的、電話再去頁面上撿——撿到的正是傳真那一支。
    """
    for candidate in (*phones_in_text(value), value):
        number = normalize_phone(candidate)
        if number and is_valid_phone(number):
            return number
    return None


def contacts_from_page(html: str, base_url: str) -> SiteContacts:
    """一個頁面上找得到的聯絡資料。

    信箱沿用 :func:`emails_from_page` 的排序（同網域、公開窗口優先）。
    電話、傳真、聯絡人先看有沒有「標籤︰值」的排版——那是唯一能可靠區分
    電話與傳真的線索——沒有的話電話退回「頁面上任何電話形狀的字串」，
    傳真與聯絡人則寧可留白。

    傳真沒有標籤時不猜：猜錯的傳真號碼會被當成電話撥出去。
    """
    soup = make_soup(html or "")
    contacts = SiteContacts()

    emails = emails_from_page(html or "", base_url)
    if emails:
        contacts.email = emails[0]

    labelled = parse_record(soup.get_text(" ", strip=True))
    contacts.phone = _usable_phone(labelled.fields.get("phone"))
    contacts.fax = _usable_phone(labelled.fields.get("fax"))
    contacts.contact_person = _usable_person(labelled.fields.get("contact_person"))

    if contacts.phone is None:
        for candidate in harvest_phones(soup):
            number = _usable_phone(candidate)
            # 已經認出來的傳真不要再當成電話。
            if number and number != contacts.fax:
                contacts.phone = number
                break

    return contacts


def page_mentions_company(html: str, company_name: str) -> bool:
    """這一頁的文字裡有沒有出現這家公司？

    比對用 :func:`~verifier.normalize.company_name_key` 的形式，所以
    「台灣積體電路製造股份有限公司」對得上頁面上寫的「臺灣積體電路製造」——
    後綴、全半形、台/臺的差別都已經在鍵裡抹平了。

    用途是驗證「搜尋引擎給的這個網址，真的是這家公司的官網嗎」。名稱太短
    （鍵少於兩個字）時一律回 ``False``：一兩個字的鍵在任何一頁上都找得到，
    那種比對等於沒比對。
    """
    key = company_name_key(company_name)
    if len(key) < 2:
        return False
    text = make_soup(html or "").get_text(" ", strip=True)
    return key in comparison_text(text)


def harvest_site_contacts(
    website: str,
    fetcher: BaseFetcher,
    wanted: Iterable[str] = HARVESTABLE_FIELDS,
    max_pages: int = 3,
    confirm_name: str | None = None,
) -> tuple[SiteContacts, int]:
    """在一個公司網站上找聯絡資料。回傳 ``(找到的東西, 實際請求次數)``。

    走法跟 :func:`find_email_for_site` 一樣（首頁 → 聯絡頁），差別只在停止
    條件：那一支找到信箱就停，這一支要等 ``wanted`` 全部有值才停。頁數上限
    仍然是 ``max_pages``，所以最壞情況的請求次數沒有變。

    ``confirm_name`` 有值時，首頁的文字裡必須出現這家公司才繼續——這是給
    「網址是搜尋引擎猜的」那條路用的。沒對上就立刻收手（不再點聯絡頁），
    回傳的 :attr:`SiteContacts.confirmed` 是 ``False``，呼叫端據此丟掉整筆。
    多花的成本是零：那一次首頁請求本來就要送。

    :raises RobotsDisallowedError: 對方的 robots.txt 不允許。
    """
    wanted = tuple(wanted)
    # 要求驗證時，``confirmed`` 從 False 開始，只有真的看過頁面、而且頁面
    # 上有這家公司，才會翻成 True。
    #
    # 一開始這裡是 ``SiteContacts()``（預設 True），結果是：連不上的網站
    # 回傳的東西宣稱「已確認是這家公司的官網」，呼叫端照單全收，把一個
    # **從來沒讀成功過**的網址存進資料庫。實際跑真實資料時就這樣存進去一個
    # 404 的網址——那正是整套驗證要防的事，卻被預設值繞過去了。
    found = SiteContacts(confirmed=confirm_name is None)
    requests_made = 0

    try:
        first = fetcher.fetch(website)
    except RobotsDisallowedError:
        raise
    except CrawlError as exc:
        log.debug("無法讀取 {}：{}", website, exc)
        return found, requests_made
    requests_made += 1

    if confirm_name:
        if not page_mentions_company(first.html, confirm_name):
            log.debug("{} 的內容沒有提到「{}」，不當成它的官網", first.url, confirm_name)
            return SiteContacts(confirmed=False), requests_made
        found.confirmed = True

    found.fill_from(contacts_from_page(first.html, first.url))
    if found.satisfies(wanted):
        return found, requests_made

    # 首頁已經用掉一次請求，剩下的額度是還能**成功讀到**幾頁。候選要給得比
    # 額度多一點，不能剛好：讀不到的候選（404、逾時）不算進額度裡，所以多給
    # 的部分只有在前面幾個真的失敗時才會用到。
    #
    # 以前這裡是寫死的 3，跟 ``max_pages`` 完全沒有關係——把上限調到 5 也只
    # 會拿到 3 個候選，額度根本用不完。
    budget = max(3, max_pages - requests_made)
    targets = _contact_links(first.html, first.url, limit=budget)
    if not targets:
        origin = f"{urlsplit(first.url).scheme}://{urlsplit(first.url).netloc}"
        targets = [origin + path for path in CONTACT_PATHS[:budget]]

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
        found.fill_from(contacts_from_page(page.html, page.url))
        if found.satisfies(wanted):
            break

    return found, requests_made


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
