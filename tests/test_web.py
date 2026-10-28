"""網頁版：只有自己的瀏覽器進得來，名單、匯出、爬取都接得上後端。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from core.schemas import CrawlSummary
from database.repository import CompanyRepository
from web.app import COOKIE_NAME, create_app
from web.jobs import CrawlJob

TOKEN = "test-token"


@pytest.fixture
def client(db_session, patch_config):
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="測試精密機械股份有限公司", name_key="測試精密機械", dedupe_key="n:a",
        email="sales@precision.com.tw", contact_person="王小明",
    )
    repo.create(company_name="另一家企業社", name_key="另一家", dedupe_key="n:b")
    db_session.commit()

    with TestClient(create_app(patch_config, TOKEN), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _log_in(client: TestClient) -> None:
    client.get(f"/?token={TOKEN}")


# ------------------------------------------------------------- 誰進得來


def test_without_the_token_nothing_is_served(client):
    assert client.get("/").status_code == 403
    assert client.get("/api/crawl").status_code == 403
    assert client.post("/api/crawl", json={}).status_code == 403


def test_the_token_becomes_a_strict_cookie_and_leaves_the_url(client):
    response = client.get(f"/?token={TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}={TOKEN}" in cookie
    assert "samesite=strict" in cookie.lower()
    assert "httponly" in cookie.lower()


def test_a_wrong_token_is_refused(client):
    client.cookies.set(COOKIE_NAME, "guess")
    assert client.get("/").status_code == 403


def test_a_foreign_host_header_is_refused_even_with_the_cookie(client):
    """DNS rebinding：攻擊者的網域解析到 127.0.0.1，瀏覽器會照樣帶上 cookie。"""
    _log_in(client)
    response = client.get("/", headers={"Host": "attacker.example:8000"})
    assert response.status_code == 400


# ------------------------------------------------------------- 名單與匯出


def test_the_list_shows_companies(client):
    _log_in(client)
    page = client.get("/").text
    assert "測試精密機械股份有限公司" in page
    assert "另一家企業社" in page
    assert "sales@precision.com.tw" in page


def test_search_narrows_the_list(client):
    _log_in(client)
    page = client.get("/", params={"q": "精密"}).text
    assert "測試精密機械股份有限公司" in page
    assert "另一家企業社" not in page


def test_export_downloads_only_what_the_search_matched(client):
    _log_in(client)
    response = client.get("/export", params={"format": "csv", "q": "精密"})

    assert response.status_code == 200
    body = response.content.decode("utf-8-sig")
    assert "測試精密機械股份有限公司" in body
    assert "另一家企業社" not in body


def test_an_unknown_export_format_is_refused(client):
    _log_in(client)
    assert client.get("/export", params={"format": "exe"}).status_code == 400


# ------------------------------------------------------------- 爬取


def test_an_unknown_source_is_refused(client):
    _log_in(client)
    assert client.post("/api/crawl", json={"source": "不存在的來源"}).status_code == 400


class _SlowController:
    """假的爬取控制器：等到被放行才結束，用來測「同時只能跑一個」。"""

    def __init__(self) -> None:
        self.release = threading.Event()

    def run(self, source, *, report, cancel_event):
        report({"source": source or "全部", "page": 1, "stored": 3, "total": 2})
        self.release.wait(5)
        return [CrawlSummary(source=source or "全部", status="Success", records_new=3)]


def test_only_one_crawl_runs_at_a_time_and_its_result_is_kept():
    controller = _SlowController()
    job = CrawlJob(controller)

    assert job.start("名錄A")
    assert not job.start("名錄B")
    assert job.status()["running"]

    controller.release.set()
    job.wait(5)

    state = job.status()
    assert not state["running"]
    assert state["results"][0]["records_new"] == 3
    assert state["progress"]["stored"] == 3


def test_a_failed_crawl_reports_its_error():
    class _Broken:
        def run(self, source, *, report, cancel_event):
            raise RuntimeError("名錄網站連不上")

    job = CrawlJob(_Broken())
    job.start(None)
    job.wait(5)

    state = job.status()
    assert not state["running"]
    assert "名錄網站連不上" in state["error"]


def test_a_crawl_started_from_the_page_runs_the_real_pipeline(client):
    """從網頁按下開始，到資料真的存進資料庫。用測試設定裡離線的 sample 來源。"""
    _log_in(client)

    assert client.post("/api/crawl", json={"source": "sample"}).status_code == 200
    client.app.state.crawl_job.wait(30)

    state = client.get("/api/crawl").json()
    assert not state["running"]
    assert state["error"] is None
    assert state["results"][0]["source"] == "sample"
    assert state["results"][0]["records_new"] > 0


# ------------------------------------------------------------- 標籤篩選


def test_the_tag_filter_narrows_the_list_and_the_export(client, db_session):
    repo = CompanyRepository(db_session)
    first = repo.search()[-1]
    repo.set_tags(first.id, ["客戶"])
    db_session.commit()
    _log_in(client)

    page = client.get("/", params={"tag": "客戶"}).text
    assert first.company_name in page
    assert page.count('class="company"') == 1

    body = client.get("/export", params={"format": "csv", "tag": "客戶"}).content.decode("utf-8-sig")
    assert first.company_name in body
    assert body.strip().count("\n") == 1  # 標題列 + 一家


# ------------------------------------------------------------- Gmail 標籤


@pytest.fixture
def gmail(monkeypatch):
    from tests.test_gmail_labels import ME, FakeGmail

    fake = FakeGmail()
    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", fake)
    monkeypatch.setenv("GMAIL_ADDRESS", ME)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    return fake


def test_the_gmail_page_lists_labels_and_classifies(client, gmail, db_session):
    gmail.add(1, "sales@precision.com.tw", "me@mycompany.com.tw", labels=("客戶",))
    _log_in(client)

    assert "Gmail 標籤" in client.get("/gmail").text
    assert client.get("/api/gmail/labels").json() == {"labels": ["供應商", "客戶"]}

    scan = client.post("/api/gmail/classify/scan", json={"label": "客戶", "limit": 50}).json()
    assert [row["company_name"] for row in scan["rows"]] == ["測試精密機械股份有限公司"]

    applied = client.post("/api/gmail/classify/apply", json={"indices": [0]}).json()
    assert "1 家" in applied["summary"]
    assert "客戶" in client.get("/", params={"tag": "客戶"}).text


def test_applying_without_a_preview_is_refused(client, gmail):
    _log_in(client)
    response = client.post("/api/gmail/classify/apply", json={"indices": [0]})
    assert response.status_code == 409


def test_sync_preview_does_not_touch_gmail(client, gmail, db_session):
    repo = CompanyRepository(db_session)
    company = next(c for c in repo.search() if c.email)
    repo.set_tags(company.id, ["VIP"])
    db_session.commit()
    gmail.add(1, company.email, "me@mycompany.com.tw")
    _log_in(client)

    preview = client.post("/api/gmail/sync", json={"tags": True, "status": False}).json()

    assert preview["rows"] == [{"label": "名單匠/VIP", "companies": 1, "messages": 1}]
    assert gmail.writes == []


def test_gmail_errors_are_explained_not_crashed(client, monkeypatch):
    """還沒設定 Gmail 時，畫面上要看得到原因，不是一個 500。"""
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setattr("core.credentials.get_secret", lambda _name: "")
    _log_in(client)

    response = client.get("/api/gmail/labels")

    assert response.status_code == 400
    assert "Gmail" in response.json()["detail"]
