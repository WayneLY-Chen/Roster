"""從公司自己的網站上抓公開刊登的聯絡資料。

重點在「什麼時候寧可留白」：

* 傳真沒有標籤就不猜——猜錯的傳真號碼會被當成電話撥出去。
* 看起來不像人名的東西不當成聯絡人。
* 首頁沒提到這家公司，就不承認那是它的網站。

整份測試不連任何網路，用假的 fetcher 餵頁面。
"""

from __future__ import annotations

import pytest

from crawler.enrich import (
    SiteContacts,
    contacts_from_page,
    emails_from_page,
    harvest_site_contacts,
    page_mentions_company,
)
from crawler.fetcher import FetchResult

BASE = "https://example.com.tw/"

#: 台灣公司網站最常見的聯絡頁排版：一行一項，標籤在值前面。
LABELLED_PAGE = """
<html><body>
  <h1>測試精密機械股份有限公司</h1>
  <div class="contact">
    聯絡人： 王小明<br>
    電話： (02) 2723-1234<br>
    傳真： 02-2723-5678<br>
    信箱： <a href="mailto:sales@example.com.tw">sales@example.com.tw</a>
  </div>
</body></html>
"""

#: 沒有任何標籤，電話只是散在頁尾。
UNLABELLED_PAGE = """
<html><body>
  <h1>測試精密機械股份有限公司</h1>
  <footer>台北市信義區松高路1號　02-27231234</footer>
</body></html>
"""


class _Fetcher:
    """依網址回頁面。沒登記的網址視為連不上。"""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def fetch(self, url: str, **_kwargs) -> FetchResult:
        self.urls.append(url)
        from core.errors import CrawlError

        if url not in self.pages:
            raise CrawlError(f"404 {url}")
        return FetchResult(url=url, status_code=200, html=self.pages[url])

    def close(self) -> None:  # pragma: no cover - 介面完整性
        pass


# --------------------------------------------------------- 單一頁面的解析


def test_a_labelled_page_gives_every_field():
    contacts = contacts_from_page(LABELLED_PAGE, BASE)
    assert contacts.email == "sales@example.com.tw"
    assert contacts.phone == "02-27231234"
    assert contacts.fax == "02-27235678"
    assert contacts.contact_person == "王小明"


def test_an_unlabelled_phone_is_still_picked_up():
    """公司自己的網站上，頁尾那支電話就是這家公司的。

    同一招用在名錄的列表頁上會抓到公會的總機，所以這支函式只給「已經確定
    是這家公司的網站」用。
    """
    contacts = contacts_from_page(UNLABELLED_PAGE, BASE)
    assert contacts.phone == "02-27231234"


def test_a_fax_is_never_guessed():
    """沒有「傳真」兩個字就留白。猜錯的傳真會被當成電話撥出去。"""
    assert contacts_from_page(UNLABELLED_PAGE, BASE).fax is None


def test_the_phone_is_not_the_number_already_read_as_a_fax():
    page = """<html><body>傳真： 02-2723-5678<br>02-2723-1234</body></html>"""
    contacts = contacts_from_page(page, BASE)
    assert contacts.fax == "02-27235678"
    assert contacts.phone == "02-27231234"


@pytest.mark.parametrize(
    "value",
    [
        "請填寫下方表單",          # 版面文字
        "service@example.com",     # 信箱不是人名
        "02-27231234",             # 電話不是人名
        "測試精密機械股份有限公司",  # 公司不是人名
        "台北市信義區松高路1號",     # 地址不是人名
    ],
)
def test_things_that_are_not_a_person_are_not_stored_as_one(value):
    page = f"<html><body>聯絡人： {value}</body></html>"
    assert contacts_from_page(page, BASE).contact_person is None


def test_a_role_address_still_wins_over_a_personal_one():
    """``emails_from_page`` 的排序沒有被改動，這裡確認它仍然生效。"""
    page = """<html><body>
      <a href="mailto:jimmy.chen@example.com.tw">Jimmy</a>
      <a href="mailto:info@example.com.tw">聯絡我們</a>
    </body></html>"""
    assert emails_from_page(page, BASE)[0] == "info@example.com.tw"


# ------------------------------------------------------------ 累積與停止


def test_fill_from_only_fills_the_blanks():
    found = SiteContacts(email="a@b.tw")
    found.fill_from(SiteContacts(email="c@d.tw", phone="02-27231234"))
    assert found.email == "a@b.tw"
    assert found.phone == "02-27231234"


def test_it_stops_as_soon_as_everything_asked_for_is_found():
    """首頁就齊了就不要再點聯絡頁——那是白白多送一次請求。"""
    fetcher = _Fetcher({BASE: LABELLED_PAGE})
    contacts, requests = harvest_site_contacts(BASE, fetcher)
    assert requests == 1
    assert contacts.email == "sales@example.com.tw"


def test_it_follows_a_contact_link_when_the_home_page_is_not_enough():
    home = """<html><body>
      <h1>測試精密機械股份有限公司</h1>
      <a href="/contact">聯絡我們</a>
    </body></html>"""
    fetcher = _Fetcher({BASE: home, "https://example.com.tw/contact": LABELLED_PAGE})
    contacts, requests = harvest_site_contacts(BASE, fetcher)
    assert requests == 2
    assert contacts.contact_person == "王小明"


def test_the_real_contact_page_is_tried_before_about_us():
    """頁數有上限，所以順序就是命中率。

    「關於我們」多半是公司沿革與董事長的話，信箱不在那裡。兩種連結都在的
    時候先點真正的聯絡頁，等於用同樣的請求次數換到更高的命中率——這一條
    壞掉不會有人發現，只會看到「命中率就是這樣」。
    """
    home = """<html><body>
      <h1>測試精密機械股份有限公司</h1>
      <a href="/about">關於我們</a>
      <a href="/contact">聯絡我們</a>
    </body></html>"""
    about = "<html><body>本公司創立於民國六十年</body></html>"
    fetcher = _Fetcher({
        BASE: home,
        "https://example.com.tw/about": about,
        "https://example.com.tw/contact": LABELLED_PAGE,
    })

    contacts, _ = harvest_site_contacts(BASE, fetcher, max_pages=2)

    # 上限兩頁 = 首頁 + 一頁。那一頁必須是 /contact，不是頁面上先出現的 /about。
    assert fetcher.urls[1] == "https://example.com.tw/contact"
    assert contacts.email == "sales@example.com.tw"


def test_a_higher_page_limit_actually_gets_more_candidates():
    """把頁數上限調高，候選連結數要跟著調高。

    這兩個數字以前是分開的：上限給 5、候選寫死 3，於是額度根本用不完。
    """
    links = "".join(f'<a href="/p{i}">聯絡我們{i}</a>' for i in range(5))
    home = f"<html><body><h1>測試精密機械股份有限公司</h1>{links}</body></html>"
    pages = {BASE: home}
    # 只有最後一頁有信箱，前面幾頁都是空的。
    for index in range(4):
        pages[f"https://example.com.tw/p{index}"] = "<html><body>敬請洽詢</body></html>"
    pages["https://example.com.tw/p4"] = LABELLED_PAGE

    contacts, requests = harvest_site_contacts(BASE, _Fetcher(pages), max_pages=6)

    assert contacts.email == "sales@example.com.tw"
    assert requests == 6


def test_asking_for_less_means_stopping_sooner():
    """只要電話的話，首頁有電話就該收工。"""
    home = """<html><body>
      <h1>測試精密機械股份有限公司</h1>
      02-2723-1234
      <a href="/contact">聯絡我們</a>
    </body></html>"""
    fetcher = _Fetcher({BASE: home, "https://example.com.tw/contact": LABELLED_PAGE})
    contacts, requests = harvest_site_contacts(BASE, fetcher, wanted=("phone",))
    assert requests == 1
    assert contacts.phone == "02-27231234"


def test_a_site_that_cannot_be_read_is_not_an_error():
    contacts, requests = harvest_site_contacts(BASE, _Fetcher({}))
    assert requests == 0
    assert contacts.is_empty()


def test_a_site_that_cannot_be_read_is_never_reported_as_confirmed():
    """連不上的網站不能宣稱「已確認是這家公司的官網」。

    實際跑真實資料時踩到的：``SiteContacts`` 的 ``confirmed`` 預設是 True
    （給「呼叫端本來就知道網址是對的」那條路用），而讀取失敗時回傳的正是
    一個預設建構的 ``SiteContacts``。於是一個**從來沒讀成功過**的網址被
    當成已驗證存進資料庫——正是整套驗證機制要防的事，卻被預設值繞過去了。
    """
    contacts, requests = harvest_site_contacts(
        BASE, _Fetcher({}), confirm_name="測試精密機械股份有限公司"
    )

    assert requests == 0
    assert contacts.confirmed is False


# ------------------------------------------------------------ 網站的驗證


def test_a_page_that_names_the_company_counts():
    assert page_mentions_company(LABELLED_PAGE, "測試精密機械股份有限公司")


def test_the_legal_suffix_does_not_have_to_match():
    """名錄上的簡稱跟登記全名常常對不起來，那不該讓驗證失敗。"""
    page = "<html><body>歡迎光臨臺灣積體電路製造</body></html>"
    assert page_mentions_company(page, "台灣積體電路製造股份有限公司")


def test_a_page_about_someone_else_does_not_count():
    assert not page_mentions_company(LABELLED_PAGE, "完全不相干的公司")


@pytest.mark.parametrize("name", ["", "  ", "甲", "甲公司"])
def test_a_name_too_short_to_be_evidence_never_confirms(name):
    """一兩個字的鍵在任何一頁上都找得到，那種比對等於沒比對。"""
    page = "<html><body>甲乙丙丁戊己庚辛</body></html>"
    assert not page_mentions_company(page, name)


def test_an_unconfirmed_site_gives_nothing_back_and_stops_immediately():
    """搜尋來的網址不對時，不要再往下點聯絡頁。"""
    other = """<html><body>
      <h1>別家公司</h1>
      <a href="/contact">聯絡我們</a>
      信箱： someone@other.com.tw
    </body></html>"""
    fetcher = _Fetcher({BASE: other, "https://example.com.tw/contact": LABELLED_PAGE})

    contacts, requests = harvest_site_contacts(
        BASE, fetcher, confirm_name="測試精密機械股份有限公司"
    )

    assert requests == 1
    assert contacts.confirmed is False
    assert contacts.is_empty()


def test_a_confirmed_site_is_harvested_normally():
    fetcher = _Fetcher({BASE: LABELLED_PAGE})
    contacts, _ = harvest_site_contacts(
        BASE, fetcher, confirm_name="測試精密機械股份有限公司"
    )
    assert contacts.confirmed is True
    assert contacts.email == "sales@example.com.tw"


def test_without_a_name_to_confirm_nothing_is_rejected():
    """名錄上直接列出來的網址不需要驗證——呼叫端已經知道它是對的。"""
    fetcher = _Fetcher({BASE: "<html><body>02-2723-1234</body></html>"})
    contacts, _ = harvest_site_contacts(BASE, fetcher)
    assert contacts.confirmed is True
