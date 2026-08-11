"""把只有公司名稱的名單補齊。

這一支串起三個模組，所以測試分兩層：候選網址的挑選規則各自獨立測，整條
流程用假的 fetcher 與假的搜尋來源跑完整趟。不連任何網路。

真正要守住的兩件事，各有一組測試盯著：

* **不覆蓋使用者已經有的資料**——補完的價值全部建立在「它不會弄壞你手上
  的東西」上面。這條破了整個功能就不能用。
* **寧可留白也不要存錯的網址**——搜尋引擎的第一筆不保證是對的。
"""

from __future__ import annotations

import pytest

from crawler.complete import (
    FILLABLE_FIELDS,
    candidate_sites,
    complete_companies,
    is_aggregator,
    looks_like_a_directory_entry,
    search_query,
)
from crawler.fetcher import FetchResult
from crawler.websearch import SearchHit, SearchUnavailable
from database.models import Company

#: 商業司的名稱查詢實際回來的形狀。
TSMC_BY_NAME = """[{"Business_Accounting_NO":"22099131",
"Company_Name":"台灣積體電路製造股份有限公司","Company_Status":"01",
"Company_Status_Desc":"核准設立","Capital_Stock_Amount":280500000000,
"Responsible_Name":"魏哲家","Company_Location":"新竹科學園區新竹市力行六路8號",
"Company_Setup_Date":"0760221"}]"""

OFFICIAL_PAGE = """
<html><body>
  <h1>台灣積體電路製造股份有限公司</h1>
  聯絡人： 林采薇<br>
  電話： (03) 563-6688<br>
  信箱： <a href="mailto:info@tsmc.example">info@tsmc.example</a>
</body></html>
"""

#: 內容跟這家公司無關的頁面。搜尋引擎的第一筆有時就長這樣。
WRONG_PAGE = "<html><body><h1>某某大學</h1>校園導覽</body></html>"


class _Fetcher:
    """依網址分派：商業司的 API 回 JSON，其餘回登記好的頁面。"""

    def __init__(self, pages: dict[str, str], registry_body: str = "[]") -> None:
        self.pages = pages
        self.registry_body = registry_body
        self.urls: list[str] = []
        self.closed = False

    def fetch(self, url: str, **_kwargs) -> FetchResult:
        self.urls.append(url)
        if "data.gcis.nat.gov.tw" in url:
            return FetchResult(url=url, status_code=200, html=self.registry_body)
        from core.errors import CrawlError

        if url not in self.pages:
            raise CrawlError(f"404 {url}")
        return FetchResult(url=url, status_code=200, html=self.pages[url])

    def close(self) -> None:
        self.closed = True


class _Provider:
    """假的搜尋來源。回固定的結果，或每次都丟出指定的例外。"""

    name = "fake"
    label = "測試用來源"

    def __init__(self, hits: list[SearchHit] | None = None, error: Exception | None = None):
        self.hits = hits or []
        self.error = error
        self.queries: list[str] = []
        self.closed = False

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.hits[:limit]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def only_a_name(db_session):
    """一家只有名字的公司——使用者從 Excel 匯進來最常見的樣子。"""
    company = Company(company_name="台灣積體電路製造股份有限公司", dedupe_key="name:tsmc")
    db_session.add(company)
    db_session.commit()
    return company


# ------------------------------------------------------------ 候選的挑選


@pytest.mark.parametrize(
    "url",
    [
        "https://www.104.com.tw/company/abc",
        "https://zh.wikipedia.org/wiki/台積電",
        "https://www.facebook.com/tsmc",
        "https://twincn.com/item.aspx?no=22099131",
        "https://shopee.tw/shop/123",
        "https://www.google.com/search?q=x",
    ],
)
def test_aggregators_are_not_official_sites(url):
    """這些頁面的標題就是公司全名，驗證那一關完全攔不住，只能先排除。"""
    assert is_aggregator(url)


@pytest.mark.parametrize(
    "url", ["https://www.tsmc.com/chinese", "https://example.com.tw/", "https://foo.tw"]
)
def test_an_ordinary_site_is_a_candidate(url):
    assert not is_aggregator(url)


def test_candidates_drop_aggregators():
    hits = [
        SearchHit("https://www.104.com.tw/company/abc"),
        SearchHit("https://www.tsmc.com/chinese"),
        SearchHit("https://zh.wikipedia.org/wiki/x"),
        SearchHit("https://other.com.tw/"),
    ]
    assert candidate_sites(hits) == ["https://www.tsmc.com/chinese", "https://other.com.tw"]


@pytest.mark.parametrize(
    "url",
    [
        # 實際跑真實資料時被存成官網的那一個。網域不在黑名單上，而它的頁面
        # 標題就是公司全名，所以「頁面有提到這家公司」那一關完全攔不住。
        "https://www.findcompany.com.tw/"
        "%E4%BF%A1%E9%82%A6%E9%9B%BB%E5%AD%90%E8%82%A1%E4%BB%BD%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8",
        "https://some-unknown-directory.example/company/信邦電子股份有限公司",
        "https://another.example/firms/信邦電子",
    ],
)
def test_a_url_with_the_company_name_in_its_path_is_a_directory_entry(url):
    """公司自己的網站不會把自家名字放在網址路徑裡。

    這一關存在是因為黑名單一定列不完——台灣的公司資料聚合站多到列不出來。
    """
    assert looks_like_a_directory_entry(url, "信邦電子股份有限公司")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sinbon.com/tw/contact",
        "https://www.sinbon.com/",
        "https://sinbon.com.tw/about",
    ],
)
def test_a_companys_own_site_is_not_a_directory_entry(url):
    assert not looks_like_a_directory_entry(url, "信邦電子股份有限公司")


def test_directory_entries_are_dropped_from_the_candidates():
    hits = [
        SearchHit("https://unknown-directory.example/company/信邦電子股份有限公司"),
        SearchHit("https://www.sinbon.com/tw/contact"),
    ]
    picked = candidate_sites(hits, company_name="信邦電子股份有限公司")
    assert picked == ["https://www.sinbon.com/tw/contact"]


def test_the_search_engine_order_is_kept_exactly():
    """不要自作聰明重排。

    這裡曾經把 ``.tw`` 排到前面（名單是台灣公司，看起來很合理），實測結果
    整批變差：台灣的公司資料聚合站與虛擬主機平台正好都在 ``.tw``，真正的
    公司官網很多是 ``.com``。那個規則做的事情正好是把垃圾排到正牌前面。
    """
    hits = [
        SearchHit("https://example.com/"),
        SearchHit("https://example.com.tw/"),
        SearchHit("https://example.jp/"),
    ]
    assert candidate_sites(hits) == [
        "https://example.com",
        "https://example.com.tw",
        "https://example.jp",
    ]


@pytest.mark.parametrize(
    "url",
    [
        # 中小企業的虛擬主機／型錄平台。網址是流水號或電話號碼、不含公司名，
        # 所以 looks_like_a_directory_entry 抓不到，只能靠網域黑名單。
        "https://0226989999.web66.com.tw/web/Comp?command=Intro",
        "https://www.tggo.com.tw/index.cgi?user=tggo03&mnm=page",
    ],
)
def test_smb_hosting_platforms_are_not_official_sites(url):
    assert is_aggregator(url)


def test_one_candidate_per_host():
    """搜尋結果常常同一個站佔掉前三名的首頁、關於我們、產品頁。"""
    hits = [
        SearchHit("https://www.tsmc.com/chinese"),
        SearchHit("https://www.tsmc.com/about"),
        SearchHit("https://other.com.tw/"),
    ]
    assert len(candidate_sites(hits)) == 2


def test_the_limit_is_honoured():
    hits = [SearchHit(f"https://site{n}.tw/") for n in range(10)]
    assert len(candidate_sites(hits, limit=2)) == 2


def test_the_query_asks_for_the_official_site():
    """不加「官網」的話第一頁常常整頁都是人力銀行的職缺頁。"""
    assert search_query("台積電") == "台積電 官網"


# ------------------------------------------------------------ 整條流程


def test_a_company_with_only_a_name_gets_filled_in(db_session, only_a_name, patch_config):
    fetcher = _Fetcher({"https://tsmc.example": OFFICIAL_PAGE}, registry_body=TSMC_BY_NAME)
    provider = _Provider([SearchHit("https://tsmc.example")])

    summary = complete_companies(fetcher=fetcher, provider=provider)

    db_session.refresh(only_a_name)
    # 第一關：商業司。
    assert only_a_name.tax_id == "22099131"
    assert only_a_name.address == "新竹科學園區新竹市力行六路8號"
    assert only_a_name.registration_status == "核准設立"
    # 第二關：找官網。
    assert only_a_name.website == "https://tsmc.example"
    # 第三關：抓聯絡資料。
    assert only_a_name.email == "info@tsmc.example"
    assert only_a_name.phone == "03-5636688"

    assert summary.updated == 1
    assert summary.registry_matched == 1
    assert summary.websites_found == 1
    assert summary.filled["tax_id"] == 1
    assert summary.filled["website"] == 1


def test_the_registered_officer_fills_an_empty_contact_person(
    db_session, only_a_name, patch_config
):
    """商業司的負責人比什麼都沒有好。網站上找得到真的窗口時它會先被填掉。"""
    fetcher = _Fetcher({}, registry_body=TSMC_BY_NAME)

    complete_companies(fetcher=fetcher, provider=_Provider(), fields=["tax_id", "contact_person"])

    db_session.refresh(only_a_name)
    assert only_a_name.contact_person == "魏哲家"


def test_existing_values_are_never_overwritten(db_session, patch_config):
    """這一條破了整個功能就不能用了。"""
    company = Company(
        company_name="台灣積體電路製造股份有限公司",
        dedupe_key="name:tsmc",
        address="使用者自己填的地址",
        contact_person="我認識的窗口",
        email="my-contact@tsmc.example",
    )
    db_session.add(company)
    db_session.commit()

    fetcher = _Fetcher({"https://tsmc.example": OFFICIAL_PAGE}, registry_body=TSMC_BY_NAME)
    complete_companies(
        fetcher=fetcher, provider=_Provider([SearchHit("https://tsmc.example")])
    )

    db_session.refresh(company)
    assert company.address == "使用者自己填的地址"
    assert company.contact_person == "我認識的窗口"
    assert company.email == "my-contact@tsmc.example"
    # 空著的仍然補得到。
    assert company.tax_id == "22099131"


def test_overwrite_is_available_but_off_by_default(db_session, patch_config):
    company = Company(
        company_name="台灣積體電路製造股份有限公司",
        dedupe_key="name:tsmc",
        address="舊地址",
    )
    db_session.add(company)
    db_session.commit()

    fetcher = _Fetcher({}, registry_body=TSMC_BY_NAME)
    complete_companies(fetcher=fetcher, provider=_Provider(), overwrite=True)

    db_session.refresh(company)
    assert company.address == "新竹科學園區新竹市力行六路8號"


def test_a_site_that_does_not_mention_the_company_is_not_stored(
    db_session, only_a_name, patch_config
):
    """寧可留白也不要存錯的網址。錯的網址會一路帶到寄信名單上。"""
    fetcher = _Fetcher({"https://wrong.example": WRONG_PAGE}, registry_body="[]")
    provider = _Provider([SearchHit("https://wrong.example")])

    summary = complete_companies(fetcher=fetcher, provider=provider)

    db_session.refresh(only_a_name)
    assert not only_a_name.website
    assert summary.websites_found == 0
    assert summary.rejected_unconfirmed == 1


def test_a_site_that_cannot_be_read_is_not_stored_as_the_website(
    db_session, only_a_name, patch_config
):
    """讀不到的網址一律不採用。

    實際跑真實資料時存進去過一個 404 的網址：讀取失敗回傳的 ``SiteContacts``
    帶著預設的 ``confirmed=True``，於是「沒讀成功」被當成「已驗證」。
    """
    fetcher = _Fetcher({})  # 什麼網址都讀不到
    provider = _Provider([SearchHit("https://unreachable.example")])

    summary = complete_companies(fetcher=fetcher, provider=provider)

    db_session.refresh(only_a_name)
    assert not only_a_name.website
    assert summary.websites_found == 0


def test_a_robots_blocked_candidate_does_not_abandon_the_whole_company(
    db_session, only_a_name, patch_config
):
    """第一個候選被 robots.txt 擋下，還有第二個候選要試。

    這些例外原本是往上丟給批次迴圈的，於是一個候選讀不到就等於整家公司
    放棄——剩下的候選一個都不會試。
    """
    from core.errors import RobotsDisallowedError

    class _Blocking(_Fetcher):
        def fetch(self, url: str, **kwargs) -> FetchResult:
            if "blocked.example" in url:
                self.urls.append(url)
                raise RobotsDisallowedError(url, "test-agent")
            return super().fetch(url, **kwargs)

    fetcher = _Blocking({"https://tsmc.example": OFFICIAL_PAGE})
    provider = _Provider(
        [SearchHit("https://blocked.example"), SearchHit("https://tsmc.example")]
    )

    summary = complete_companies(fetcher=fetcher, provider=provider)

    db_session.refresh(only_a_name)
    assert only_a_name.website == "https://tsmc.example"
    assert summary.skipped_robots == 1


def test_the_registry_result_survives_a_search_that_breaks(db_session, only_a_name, patch_config):
    """搜尋壞掉不該讓前一關已經補好的東西消失。

    往上丟例外的話這家公司會被 ``continue`` 掉，連帶跳過 ``session.commit()``
    與計數——結果是畫面說「更新 0 家」，但資料其實已經改了。兩邊對不起來。
    """
    fetcher = _Fetcher({}, registry_body=TSMC_BY_NAME)
    provider = _Provider(error=SearchUnavailable("額度用完了"))

    summary = complete_companies(fetcher=fetcher, provider=provider)

    db_session.refresh(only_a_name)
    assert only_a_name.tax_id == "22099131"
    assert summary.updated == 1  # 有改到就要算數
    assert "額度用完了" in summary.search_stopped


def test_the_next_candidate_is_tried_when_the_first_one_is_wrong(
    db_session, only_a_name, patch_config
):
    fetcher = _Fetcher(
        {"https://wrong.example": WRONG_PAGE, "https://tsmc.example": OFFICIAL_PAGE}
    )
    provider = _Provider(
        [SearchHit("https://wrong.example"), SearchHit("https://tsmc.example")]
    )

    complete_companies(fetcher=fetcher, provider=provider)

    db_session.refresh(only_a_name)
    assert only_a_name.website == "https://tsmc.example"


def test_a_company_that_already_has_a_website_is_not_searched_for(db_session, patch_config):
    """搜尋是最貴的一步，已經知道網址就別浪費。"""
    company = Company(
        company_name="台灣積體電路製造股份有限公司",
        dedupe_key="name:tsmc",
        website="https://tsmc.example",
        tax_id="22099131",
        address="有了",
        contact_person="有了",
    )
    db_session.add(company)
    db_session.commit()

    fetcher = _Fetcher({"https://tsmc.example": OFFICIAL_PAGE}, registry_body=TSMC_BY_NAME)
    provider = _Provider([SearchHit("https://should-not-be-used.example")])

    complete_companies(fetcher=fetcher, provider=provider)

    assert provider.queries == []
    db_session.refresh(company)
    assert company.email == "info@tsmc.example"


def test_a_complete_company_costs_nothing(db_session, patch_config):
    company = Company(
        company_name="齊全股份有限公司",
        dedupe_key="name:complete",
        tax_id="22099131",
        address="台北市信義區松高路1號",
        contact_person="王小明",
        website="https://example.com.tw",
        email="a@example.com.tw",
        phone="02-27231234",
        fax="02-27235678",
    )
    db_session.add(company)
    db_session.commit()

    fetcher = _Fetcher({})
    summary = complete_companies(fetcher=fetcher, provider=_Provider())

    assert summary.considered == 0
    assert fetcher.urls == []


def test_search_running_out_stops_searching_but_not_the_run(db_session, patch_config):
    """限流不該讓整批停下來——商業司那一關對剩下的公司仍然有用。"""
    for index in range(2):
        db_session.add(
            Company(
                company_name=f"台灣積體電路製造股份有限公司{index}",
                dedupe_key=f"name:tsmc-{index}",
            )
        )
    db_session.commit()

    fetcher = _Fetcher({}, registry_body="[]")
    provider = _Provider(error=SearchUnavailable("額度用完了"))

    summary = complete_companies(fetcher=fetcher, provider=provider)

    assert summary.considered == 2
    # 第一家踩到之後就不再送第二次查詢。
    assert len(provider.queries) == 1
    assert "額度用完了" in summary.search_stopped


def test_no_search_provider_still_completes_the_other_two_stages(
    db_session, only_a_name, patch_config
):
    fetcher = _Fetcher({}, registry_body=TSMC_BY_NAME)

    summary = complete_companies(fetcher=fetcher, provider=None, fields=["tax_id", "address"])

    db_session.refresh(only_a_name)
    assert only_a_name.tax_id == "22099131"
    assert summary.searches_made == 0


def test_only_the_named_companies_are_touched(db_session, patch_config):
    """匯入後自動補齊只該處理這一批，不該把資料庫裡既有的幾千家一起重跑。"""
    mine = Company(company_name="台灣積體電路製造股份有限公司", dedupe_key="name:a")
    someone_elses = Company(company_name="別人的公司股份有限公司", dedupe_key="name:b")
    db_session.add_all([mine, someone_elses])
    db_session.commit()

    fetcher = _Fetcher({}, registry_body=TSMC_BY_NAME)
    summary = complete_companies(
        fetcher=fetcher, provider=_Provider(), company_ids=[mine.id]
    )

    assert summary.considered == 1
    db_session.refresh(someone_elses)
    assert not someone_elses.tax_id


def test_the_limit_caps_the_batch(db_session, patch_config):
    for index in range(5):
        db_session.add(Company(company_name=f"公司{index}", dedupe_key=f"name:{index}"))
    db_session.commit()

    fetcher = _Fetcher({}, registry_body="[]")
    summary = complete_companies(fetcher=fetcher, provider=_Provider(), limit=2)

    assert summary.considered == 2


def test_cancelling_stops_the_batch(db_session, patch_config):
    import threading

    for index in range(3):
        db_session.add(Company(company_name=f"公司{index}", dedupe_key=f"name:{index}"))
    db_session.commit()

    cancel = threading.Event()
    cancel.set()

    summary = complete_companies(
        fetcher=_Fetcher({}), provider=_Provider(), cancel_event=cancel
    )

    assert summary.updated == 0


def test_an_unknown_field_is_rejected_up_front(db_session, patch_config):
    """打錯欄位名該當場說，不是安靜地少補一個欄位。"""
    with pytest.raises(ValueError, match="不認得的欄位"):
        complete_companies(fetcher=_Fetcher({}), fields=["company_name"])


def test_a_borrowed_fetcher_is_not_closed(db_session, patch_config):
    """呼叫端傳進來的東西不歸這支函式處置。"""
    fetcher = _Fetcher({})
    complete_companies(fetcher=fetcher, provider=_Provider())
    assert not fetcher.closed


def test_every_fillable_field_exists_on_the_model(db_session):
    """欄位名單跟資料庫欄位對不起來的話，補完會安靜地什麼都不做。"""
    for name in FILLABLE_FIELDS:
        assert hasattr(Company, name), name
