"""把一份只有公司名稱的名單補成一份能用的名單。

## 這一步在補什麼

使用者手上的 Excel 常常只有兩欄：公司名稱和一支電話。名錄爬回來的資料不
一樣——那些至少會帶著網址，所以 :mod:`crawler.enrich`（有網址→找信箱）和
:mod:`crawler.registry`（有統編→查登記）各自都接得上。只有公司名稱的話，
那兩支的前提都不成立，補完到此為止。

這個模組把缺的那一段接起來，一家公司走三關，每一關都只在「這家公司真的
缺這些欄位」時才跑：

1. **商業司**（:mod:`crawler.registry`）
   有統編就用統編查，只有名稱就用名稱查。補回統一編號、負責人、登記地址、
   登記狀態。這一關的資料是政府開放資料，最可靠，所以排第一——而且它補出
   來的統編會讓下一次執行走上更準的那條路。

2. **找官網**（:mod:`crawler.websearch`）
   還是沒有網址的話才搜尋。搜尋來源可抽換，預設是免金鑰的 DuckDuckGo。

3. **抓聯絡資料**（:func:`crawler.enrich.harvest_site_contacts`）
   有了網址就去公司自己的網站上，把公開刊登的信箱、電話、傳真、聯絡人
   一次帶回來。

## 搜尋來的網址一定要驗證

搜尋引擎的第一筆不保證是對的。「大同公司」可能回大同大學，「永豐」可能
回永豐銀行。把錯的網址存進去比留白糟得多——留白看得出來是缺的，錯的網址
會一路帶著跑到寄信名單上。

所以每一個候選網址都必須通過同一關：**抓下來的首頁文字裡要出現這家公司**
（:func:`crawler.enrich.page_mentions_company`）。過不了就換下一個候選，
候選用完就留白。驗證不需要額外的請求——那一次首頁請求本來就要送。

另外，聚合網站（人力銀行、維基百科、社群、購物平台）在候選階段就直接排除。
它們幾乎一定會提到公司名字，驗證那一關攔不住，但它們不是公司的官網。

## 分批跑，而且自己記得上次跑到哪

一份 2699 家的名單整批跑要六個小時，中間任何一個中斷都會讓人不知道跑到哪
了。所以呼叫端只需要說「這次跑幾家」（``limit``），**從第幾家開始是這裡的
事**：:func:`_targets` 照 :func:`queue_position` 排序——還沒跑過的排最前面，
跑過的照上次跑的時間由舊到新——所以再呼叫一次就是接著上一次。

書籤是 ``Company.completion_checked_at``。它的重點只有一個，而且反直覺：
**補不到東西的公司也要蓋章。** 少了這一條整個功能是壞的——真正補不到的公司
永遠都還缺欄位，所以每一批挑出來的都是同一批，名單會永遠停在前 N 家。

唯一不蓋章的情況是「該跑的關卡根本沒跑成」：搜尋來源被限流、商業司忙線。
那不是「查過了沒有」，是「還沒查」，蓋了章等於把它排到隊伍最後面。這兩者
在程式裡分別是 ``search_exhausted`` 與 :class:`~crawler.registry.RegistryBusy`。

## 不覆蓋使用者已經有的資料

預設只填空欄位。使用者手動改過的、或原始檔案裡就有的值，這一步一律不動——
``overwrite=True`` 才會覆蓋，而那個選項不在畫面上，只留給命令列。

## 規矩

跟爬名錄完全相同，一條都不放寬：遵守每一個網域自己的 robots.txt、沿用同一
個速率限制器、誠實表明 User-Agent、只讀公開頁面。詳見 :mod:`crawler.enrich`
與 :mod:`crawler.websearch` 各自的說明。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import CrawlError, RobotsDisallowedError
from core.logging_setup import get_logger
from crawler.enrich import HARVESTABLE_FIELDS, SiteContacts, harvest_site_contacts
from crawler.fetcher import BaseFetcher, build_fetcher
from crawler.registry import (
    RegistryBusy,
    apply_registration,
    lookup,
    lookup_by_name,
)
from crawler.websearch import (
    SearchHit,
    SearchProvider,
    SearchUnavailable,
    build_search_provider,
)
from database.models import now
from database.repository import CompanyRepository
from database.session import session_scope
from verifier.normalize import company_name_key, comparison_text, normalize_website

log = get_logger(LogCategory.CRAWL)

#: 這一步可以補的欄位，也是 ``fields`` 參數的合法值。
#:
#: ``tax_id``/``address``/``contact_person`` 走商業司，``website`` 走搜尋，
#: 其餘走公司自己的網站。使用者在畫面上勾選要補哪些，順序就是這個順序。
FILLABLE_FIELDS: tuple[str, ...] = (
    "tax_id",
    "address",
    "contact_person",
    "website",
    "email",
    "phone",
    "fax",
)

#: 商業司那一關補得到的欄位。
REGISTRY_FIELDS = frozenset({"tax_id", "address", "contact_person"})

#: 要去公司網站上抓的欄位。
SITE_FIELDS = frozenset(HARVESTABLE_FIELDS)

#: 這些網域再怎麼提到公司名字也不是公司的官網。
#:
#: 分成幾類：人力銀行與商業資料聚合站（它們的頁面標題就是公司全名，
#: :func:`crawler.enrich.page_mentions_company` 那一關完全攔不住）、百科與
#: 新聞、社群與影音、購物與部落格平台。
#:
#: **這張表一定列不完**，所以它不是唯一的防線，見 :func:`looks_like_a_directory_entry`。
AGGREGATOR_HOSTS: tuple[str, ...] = (
    # 人力銀行
    "104.com.tw", "1111.com.tw", "518.com.tw", "yes123.com.tw", "cakeresume.com",
    "jobbank.gov.tw",
    # 公司資料聚合
    "twincn.com", "iyp.com.tw", "findcompany.com.tw", "company.g0v.ronny.tw",
    "tw.piliapp.com", "taiwanbizdirectory", "alibaba.com", "made-in-china.com",
    "kompass.com", "yellowpages", "yellowpage", "tianyancha.com", "qcc.com",
    "cens.com", "taiwantrade.com", "bizvibe.com", "dnb.com", "opengovtw.com",
    "twcompany", "cmoney.tw", "bizsearch", "registry.gov.tw", "gcis.nat.gov.tw",
    # 台灣中小企業的虛擬主機／型錄平台。它們替上千家公司各開一頁，頁面上
    # 當然有公司全名，而網址是流水號或電話號碼，不含公司名——所以
    # looks_like_a_directory_entry 抓不到，只能列在這裡。
    "web66.com.tw", "tw66.com.tw", "tggo.com.tw", "web139.com.tw", "url.com.tw",
    "bizman.com.tw", "twnic.net", "hotcom.com.tw", "superbuy.com.tw",
    "comptw.com", "inc.com.tw", "chanchao.com.tw", "taiwanmachinery",
    # 百科與新聞
    "wikipedia.org", "wikiwand.com", "cnyes.com", "moneydj.com", "ettoday.net",
    "udn.com", "chinatimes.com", "ltn.com.tw", "businesstoday.com.tw",
    "technews.tw", "bnext.com.tw",
    # 社群與影音
    "facebook.com", "instagram.com", "line.me", "youtube.com", "linkedin.com",
    "twitter.com", "x.com", "threads.net", "tiktok.com", "plurk.com",
    # 購物與部落格平台
    "shopee.tw", "ruten.com.tw", "momoshop.com.tw", "pchome.com.tw",
    "books.com.tw", "blogspot.com", "pixnet.net", "wordpress.com", "medium.com",
    # 搜尋引擎自己
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
)


@dataclass
class CompletionSummary:
    """一次補齊作業的結果。

    數字刻意分得細：使用者最想知道的不是「更新了幾筆」，而是「為什麼有
    150 家沒補到」——是查不到、被 robots.txt 擋下、還是搜尋額度用完了。
    """

    considered: int = 0
    #: 三個階段各自的成果。
    registry_matched: int = 0
    searches_made: int = 0
    websites_found: int = 0
    sites_visited: int = 0
    #: 逐欄位補到幾筆。key 是 :data:`FILLABLE_FIELDS` 裡的名字。
    filled: dict[str, int] = field(default_factory=dict)
    #: 至少被改到一個欄位的公司數。
    updated: int = 0
    #: 蓋上「跑過了」書籤的家數，也就是下一批不會再挑到的家數。
    #:
    #: 這個數字跟 :attr:`updated` 分開看才有意義：``處理 200 家、更新 43 家、
    #: 標記完成 200 家`` 代表這一批確實往前推了 200 家；如果標記只有 12 家，
    #: 那就是搜尋來源中途被限流了，剩下的下一批會重跑。
    marked_done: int = 0
    #: 這一批之外還剩幾家待補、其中幾家還沒跑過。做完才知道，供畫面顯示
    #: 「還剩 2499 家」。
    remaining: int = 0
    remaining_untried: int = 0
    #: 本來就什麼都不缺，連一次請求都沒送。
    skipped_complete: int = 0
    #: 搜尋回了結果，但沒有一個通得過「首頁要提到這家公司」。
    rejected_unconfirmed: int = 0
    skipped_robots: int = 0
    registry_busy: int = 0
    failed: int = 0
    #: 實際用到的搜尋來源，供畫面顯示。沒有搜尋時是空字串。
    search_provider: str = ""
    #: 搜尋來源中途不能用了（限流、額度用完），後面就不再搜尋。
    search_stopped: str = ""
    errors: list[str] = field(default_factory=list)

    def count(self, field_name: str) -> None:
        self.filled[field_name] = self.filled.get(field_name, 0) + 1

    @property
    def fields_filled(self) -> int:
        return sum(self.filled.values())


def is_aggregator(url: str) -> bool:
    """這個網址的網域是已知的聚合網站嗎？"""
    host = urlsplit(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(bad in host for bad in AGGREGATOR_HOSTS)


def looks_like_a_directory_entry(
    url: str, company_name: str, tax_id: str | None = None
) -> bool:
    """這個網址是「某個名錄裡的一筆」而不是這家公司自己的網站嗎？

    判斷依據是一個很硬的規律：**公司自己的網站不會把自己的識別資料放在網址
    路徑裡**。``tsmc.com/chinese``、``sinbon.com/tw/contact``——識別在網域上，
    路徑講的是「這一頁是什麼」。名錄剛好相反，路徑就是那一筆的鍵：

        findcompany.com.tw/信邦電子股份有限公司     ← 名字
        104.com.tw/company/信邦電子                 ← 名字
        comptw.com/item/28388051                    ← 統一編號
        inc.com.tw/c/84666583                       ← 統一編號

    統編這一條是拿 TAMI 名冊實測出來的：12 家裡有 3 家被這種頁面騙過去。
    能查得到是因為第一關（商業司）已經先跑完，所以搜尋的時候手上就有統編。

    這一關存在的理由是 :data:`AGGREGATOR_HOSTS` **一定列不完**。台灣的公司
    資料聚合站多到列不出來，而它們每一個都通得過「頁面上有沒有提到這家公司」
    ——它們的頁面標題就是公司全名。少了這一關，漏列一個網域的代價就是把名錄
    頁當成官網存進去，然後從那一頁抓回**名錄業者自己的**信箱。實測踩過：
    某家公司的「官網」抓成展覽公司的雜誌頁，信箱抓成那家展覽公司的 info@。

    網址是百分之百編碼過的（中文網址一定是），所以要先還原再比。
    """
    path = unquote(urlsplit(url or "").path)

    key = company_name_key(company_name)
    if len(key) >= 2 and key in comparison_text(path):
        return True

    digits = "".join(ch for ch in str(tax_id or "") if ch.isdigit())
    return len(digits) == 8 and digits in path


def candidate_sites(
    hits: Iterable[SearchHit],
    limit: int = 3,
    company_name: str = "",
    tax_id: str | None = None,
) -> list[str]:
    """搜尋結果裡值得一試的官網候選，依序排列。

    只做兩件事：丟掉聚合網站與看起來是名錄條目的網址，以及同一個網域只留
    第一筆（搜尋結果常常同一個站佔掉前三名的首頁、關於我們、產品頁）。

    **順序原封不動照搜尋引擎給的。** 這一點是實測換來的：這支程式的名單是
    台灣公司，所以曾經在這裡把 ``.tw`` 網域排到前面——結果整批變差。台灣的
    公司資料聚合站與虛擬主機平台（web66、tggo 這類）正好都在 ``.tw``，而
    真正的公司官網很多是 ``.com``（``tsmc.com``、``sinbon.com``）。那個規則
    做的事情正好是**把垃圾排到正牌前面**。

    搜尋引擎的排序本身就是目前手上最好的訊號，別自作聰明去改它。

    **不**在這裡判斷哪一個才對——那要看頁面內容，而這裡還沒有頁面內容。
    """
    picked: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        url = normalize_website(hit.url)
        if not url or is_aggregator(url):
            continue
        if (company_name or tax_id) and looks_like_a_directory_entry(
            url, company_name, tax_id
        ):
            continue
        host = urlsplit(url).netloc.lower()
        if host in seen:
            continue
        seen.add(host)
        picked.append(url)
        if len(picked) >= limit:
            break
    return picked


def search_query(company_name: str) -> str:
    """找官網用的查詢字串。

    加「官網」兩個字有用：不加的話第一頁常常整頁都是人力銀行的職缺頁，
    那些在 :func:`candidate_sites` 會被丟掉，等於白搜一次。
    """
    return f"{company_name} 官網"


def _missing(company, fields: Iterable[str], overwrite: bool) -> set[str]:
    """這家公司還缺 ``fields`` 裡的哪幾個。``overwrite`` 時全部都算缺。"""
    if overwrite:
        return set(fields)
    return {name for name in fields if not (getattr(company, name, None) or "").strip()}


def _fill(company, field_name: str, value: str | None, overwrite: bool) -> bool:
    """填一個欄位。回傳有沒有真的改到。"""
    if not value:
        return False
    if not overwrite and (getattr(company, field_name, None) or "").strip():
        return False
    setattr(company, field_name, value)
    return True


def _run_registry(
    company, fetcher: BaseFetcher, summary: CompletionSummary, overwrite: bool
) -> bool:
    """商業司那一關。回傳有沒有改到東西。

    有統編就用統編查（準確），只有名稱就用名稱查（可能對不上，對不上就
    算了）。查完一律蓋上 ``registration_checked_at``，跟
    :func:`crawler.registry.enrich_registrations` 的行為一致——否則每一次
    補完都會把同一批查不到的再問一遍。

    :func:`~crawler.registry.apply_registration` 負責登記狀態、資本額與那些
    只該原樣留著的自由欄位；三個「使用者也會自己填」的欄位（統編、地址、
    負責人）在這裡另外處理，因為只有這裡知道 ``overwrite``——那一支被
    ``registry`` 指令共用，它永遠只補空的。
    """
    tax_id = (company.tax_id or "").strip()
    if tax_id:
        registration = lookup(tax_id, fetcher)
    else:
        registration = lookup_by_name(company.company_name or "", fetcher)

    if registration is None:
        company.registration_checked_at = now()
        return False

    summary.registry_matched += 1
    was_empty = {
        name: not (getattr(company, name, None) or "").strip()
        for name in REGISTRY_FIELDS
    }

    changed = apply_registration(company, registration)
    for name, value in (
        ("tax_id", registration.tax_id),
        ("address", registration.location),
        ("contact_person", registration.responsible_name),
    ):
        changed |= _fill(company, name, value, overwrite)
        if was_empty[name] and (getattr(company, name, None) or "").strip():
            summary.count(name)
    return changed


def _run_search(
    company,
    provider: SearchProvider,
    summary: CompletionSummary,
    max_candidates: int,
) -> list[str]:
    """搜尋這家公司的官網候選。搜不到回空 list。

    :raises SearchUnavailable: 搜尋來源不能用了，呼叫端應該停止再搜尋。
    """
    name = company.company_name or ""
    summary.searches_made += 1
    hits = provider.search(search_query(name), limit=10)
    # 統編是第一關剛補回來的。名錄用它當網址路徑上的鍵，所以它是這裡最好用
    # 的線索之一——見 looks_like_a_directory_entry。
    return candidate_sites(
        hits, limit=max_candidates, company_name=name, tax_id=company.tax_id
    )


def _harvest(
    company,
    website: str,
    wanted: set[str],
    fetcher: BaseFetcher,
    summary: CompletionSummary,
    confirm: bool,
    max_pages: int = 3,
) -> SiteContacts:
    """去一個網站上抓聯絡資料。``confirm`` 時首頁必須提到這家公司。"""
    contacts, requests_made = harvest_site_contacts(
        website,
        fetcher,
        wanted=wanted or HARVESTABLE_FIELDS,
        max_pages=max_pages,
        confirm_name=company.company_name if confirm else None,
    )
    summary.sites_visited += requests_made
    return contacts


def complete_companies(
    limit: int | None = None,
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
    provider: SearchProvider | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    company_ids: Iterable[int] | None = None,
    fields: Iterable[str] | None = None,
    overwrite: bool = False,
) -> CompletionSummary:
    """把缺資料的公司補齊。

    :param limit: 最多處理幾家。
    :param company_ids: 只處理這幾家；``None`` 代表全部（只挑真的缺東西的）。
    :param fields: 要補哪些欄位，預設 :data:`FILLABLE_FIELDS` 全部。
    :param overwrite: 連已經有值的欄位也覆蓋。畫面上沒有這個選項。
    :param provider: 搜尋來源。``None`` 時依設定自己建一個；設定成
        ``none``、或建立時就發現金鑰沒填，這一步會照實記在
        :attr:`CompletionSummary.search_stopped` 裡並繼續跑其他兩關。
    """
    config = config or get_config()
    wanted_fields = tuple(fields) if fields is not None else FILLABLE_FIELDS
    unknown = set(wanted_fields) - set(FILLABLE_FIELDS)
    if unknown:
        raise ValueError(f"不認得的欄位：{'、'.join(sorted(unknown))}")

    summary = CompletionSummary()
    settings = config.completion

    owns_fetcher = fetcher is None
    fetcher = fetcher or build_fetcher(config)

    owns_provider = provider is None
    if owns_provider and "website" in wanted_fields:
        try:
            provider = build_search_provider(config, fetcher)
        except SearchUnavailable as exc:
            # 設定有問題不該讓整個補完不能跑——商業司與「已經有網址」那兩關
            # 照樣有用。照實說一次，然後繼續。
            summary.search_stopped = str(exc)
            log.warning("不會搜尋官網：{}", exc)
            provider = None
    if provider is not None:
        summary.search_provider = provider.label or provider.name

    # ``provider`` 在迴圈裡會被設成 None（搜尋來源中途壞掉時），所以要關的是
    # 哪一個物件不能等到 finally 才問。少了這一行，被停用的那個 provider 的
    # httpx client 就永遠不會關。
    provider_to_close = provider if owns_provider else None

    # 「搜尋這一關是壞掉了，不是沒有」。兩者要分開，因為它決定要不要蓋
    # ``completion_checked_at`` 這個書籤：
    #
    #   設定成 ``none``  → 使用者自己關的，那就是這個名單的正常狀態，
    #                      蓋章，讓隊伍往前走。
    #   限流／額度用完  → 這一關**根本沒跑**。蓋了章就等於把這家公司排到
    #   ／金鑰沒填        隊伍最後面，而它其實一次都沒被搜尋過——下一批
    #                      換成別人，這一家要等整整一輪才輪得到。不蓋。
    search_exhausted = bool(summary.search_stopped)

    try:
        with session_scope() as session:
            repo = CompanyRepository(session)
            targets = _targets(repo, company_ids, wanted_fields, overwrite, limit)
            summary.considered = len(targets)

            for index, company in enumerate(targets, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    log.warning("補齊公司資料已取消")
                    break
                if progress is not None:
                    progress(index, len(targets), company.company_name)

                changed = False
                # 這一家算不算「跑過了」。見 ``search_exhausted`` 的說明與
                # ``Company.completion_checked_at`` 的欄位註解。
                deferred = False
                try:
                    changed, deferred = _complete_one(
                        company, wanted_fields, overwrite, fetcher,
                        provider, summary, settings, search_exhausted,
                    )
                except RobotsDisallowedError as exc:
                    summary.skipped_robots += 1
                    log.info("{}：robots.txt 不允許，略過（{}）", company.company_name, exc)
                except RegistryBusy as exc:
                    # 商業司只是現在忙，不是「查過了」。蓋章會把它排到隊伍
                    # 最後面，而它其實什麼都還沒查——下一批要換成別人。
                    summary.registry_busy += 1
                    deferred = True
                    log.info("{}：{}", company.company_name, exc)
                except CrawlError as exc:
                    summary.failed += 1
                    message = f"{company.company_name}：{exc}"
                    summary.errors.append(message)
                    log.warning(message)
                except Exception as exc:  # noqa: BLE001 - 一家壞掉不該停掉整批
                    summary.failed += 1
                    message = f"{company.company_name}：{type(exc).__name__}: {exc}"
                    summary.errors.append(message)
                    log.warning(message)

                if changed:
                    summary.updated += 1
                    company.updated_at = now()
                if not deferred:
                    # 補不到東西的也要蓋章——而且**它們才是重點**。真正補不到
                    # 的公司永遠都還缺欄位，不蓋章的話下一批挑出來的又是同一批，
                    # 名單永遠停在前 N 家。
                    company.completion_checked_at = now()
                    summary.marked_done += 1
                session.commit()

                # 搜尋來源已經壞了（限流、額度用完）。停掉搜尋那一關，但不要
                # 停掉整批——剩下的公司仍然走得完商業司與「已經有網址」兩條路。
                if summary.search_stopped:
                    provider = None
                    search_exhausted = True

            # 這一批做完之後還剩多少。在同一個 session 裡算完再帶出去，
            # 呼叫端（GUI／CLI）就不必為了顯示一句話再開一次資料庫。
            left = repo.completion_progress(wanted_fields)
            summary.remaining = left.pending
            summary.remaining_untried = left.untried
    finally:
        if owns_fetcher:
            fetcher.close()
        if provider_to_close is not None:
            provider_to_close.close()

    log.info(
        "補齊完成：處理 {} 家、更新 {} 家、補上 {} 個欄位、標記完成 {} 家，還剩 {} 家"
        "（商業司對到 {}、搜尋 {} 次、找到官網 {} 個、造訪 {} 頁）",
        summary.considered, summary.updated, summary.fields_filled,
        summary.marked_done, summary.remaining,
        summary.registry_matched, summary.searches_made,
        summary.websites_found, summary.sites_visited,
    )
    return summary


def queue_position(company) -> tuple[int, float, int]:
    """排隊用的鍵：沒跑過的排最前面，跑過的照時間由舊到新，同分再照 id。

    這一個函式就是「使用者輸入 200，程式自己知道要從第幾家開始」的全部
    機制。分開寫出來是為了讓它能被單獨測試——排錯的話症狀是「每次都跑同
    一批」，那種錯在整合測試裡很難一眼看出來。
    """
    stamp = getattr(company, "completion_checked_at", None)
    if stamp is None:
        return (0, 0.0, company.id or 0)
    return (1, stamp.timestamp(), company.id or 0)


def _targets(
    repo: CompanyRepository,
    company_ids: Iterable[int] | None,
    wanted_fields: tuple[str, ...],
    overwrite: bool,
    limit: int | None,
) -> list:
    """要處理哪幾家。

    指定 ``company_ids`` 時照單全收、順序照給的（那是使用者自己挑的，即使
    看起來不缺東西也照跑）；沒指定時只挑真的缺欄位的——把已經完整的公司也
    送進去，只會浪費請求。

    ## 分批的順序

    沒指定 ``company_ids`` 時照 :func:`queue_position` 排序：**還沒跑過的
    排前面，跑過的照上次跑的時間由舊到新**。所以使用者在畫面上輸入「這次
    跑 200 家」，按第二次就是第 201–400 家，不需要自己記到第幾家，也沒有
    「從第幾筆開始」那種一填錯就跳過一整段的輸入框。

    這件事非做不可的理由：``_missing()`` 是唯一的篩選條件時，**補不到東西
    的公司永遠留在名單最前面**——它補不到，所以下一批它還是缺，所以還是它。
    2699 家的名單會永遠停在前 200 家，而且每一批都重跑同樣的請求。
    """
    if company_ids is not None:
        targets = [c for c in (repo.get(i) for i in company_ids) if c is not None]
    else:
        targets = [
            company for company in repo.all()
            if overwrite or _missing(company, wanted_fields, overwrite=False)
        ]
        targets.sort(key=queue_position)
    return targets[:limit] if limit else targets


def _complete_one(
    company,
    wanted_fields: tuple[str, ...],
    overwrite: bool,
    fetcher: BaseFetcher,
    provider: SearchProvider | None,
    summary: CompletionSummary,
    settings,
    search_exhausted: bool = False,
) -> tuple[bool, bool]:
    """一家公司的三關。回傳 ``(有沒有改到東西, 這一家算不算沒跑過)``。

    第二個值是給 ``completion_checked_at`` 這個書籤用的。``True`` 代表「該跑
    的關卡有一關根本沒跑成」，呼叫端就不蓋章，這家公司會留在隊伍原本的位置
    等下一批——而不是被當成試過了、排到 2699 家的最後面。
    """
    missing = _missing(company, wanted_fields, overwrite)
    if not missing:
        summary.skipped_complete += 1
        return False, False

    changed = False
    deferred = False

    # 第一關：商業司。
    if settings.use_registry and (missing & REGISTRY_FIELDS):
        changed |= _run_registry(company, fetcher, summary, overwrite)
        missing = _missing(company, wanted_fields, overwrite)

    # 第二關：找官網。已經有網址就不搜尋——那是最貴的一步。
    website = (company.website or "").strip()
    confirm = False
    candidates = [website] if website else []
    if not website and "website" in missing:
        if provider is not None:
            try:
                candidates = _run_search(
                    company, provider, summary, settings.max_candidates
                )
                confirm = True
            except SearchUnavailable as exc:
                # 記在 summary 上而不是往上丟。往上丟的話這家公司會被 continue
                # 掉，連帶把商業司那一關**已經補好的東西**排除在統計外、而且
                # 跳過 session.commit()。搜尋壞掉不該讓前一關的成果消失。
                # 呼叫端看到 search_stopped 有值就會停掉後續的搜尋。
                summary.search_stopped = str(exc)
                log.warning("停止搜尋官網：{}", exc)
                candidates = []
                deferred = True
        elif search_exhausted:
            # 前面某一家已經把搜尋來源用到限流／額度用完了，所以這一家的
            # 搜尋那一關連送都沒送出去。不算跑過。
            deferred = True

    # 第三關：到網站上抓聯絡資料。
    site_wanted = missing & SITE_FIELDS
    if not candidates or not (site_wanted or "website" in missing):
        return changed, deferred

    # 有沒有哪個候選是「真的讀到了、但內容證明不是這家公司」。
    #
    # 這個跟「一個都讀不到」要分開算。全部候選都被 robots.txt 擋下時，
    # 那是 skipped_robots，不是「無法確認」——兩個數字同時加一的話，報表上
    # 12 家公司可以生出 3 + 5 = 8 次失敗，讀的人沒辦法從數字回推發生什麼事。
    read_but_rejected = False

    for candidate in candidates:
        # 一個候選讀不到，不該讓這家公司整筆放棄。
        #
        # 這些例外原本是往上丟給批次迴圈處理的，於是「第一個候選的 robots.txt
        # 不允許」等於「這家公司不處理了」——剩下的候選一個都不會試，而且
        # 商業司那一關明明已經補到的東西也不會被計入結果。
        try:
            contacts = _harvest(
                company, candidate, site_wanted, fetcher, summary, confirm,
                max_pages=getattr(settings, "max_pages", 3),
            )
        except RobotsDisallowedError:
            summary.skipped_robots += 1
            log.info("{}：{} 的 robots.txt 不允許，換下一個候選", company.company_name, candidate)
            continue
        except CrawlError as exc:
            log.debug("{}：讀不到 {}（{}），換下一個候選", company.company_name, candidate, exc)
            continue

        if confirm and not contacts.confirmed:
            read_but_rejected = True
            continue

        if confirm:
            summary.websites_found += 1
            if _fill(company, "website", normalize_website(candidate), overwrite):
                summary.count("website")
                changed = True

        for field_name in ("email", "phone", "fax", "contact_person"):
            if field_name not in site_wanted:
                continue
            if _fill(company, field_name, contacts.get(field_name), overwrite):
                summary.count(field_name)
                changed = True
        return changed, deferred

    # 讀到了，但每一個都證明不是這家公司的網站。
    if read_but_rejected:
        summary.rejected_unconfirmed += 1
    return changed, deferred
