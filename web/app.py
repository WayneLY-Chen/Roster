"""網頁版的路由。

## 只給自己的瀏覽器用

伺服器只聽 ``127.0.0.1``，但光這樣不夠：你在同一個瀏覽器裡開的**任何網站**
都能對 ``http://127.0.0.1:埠號`` 送請求（叫它開始爬、把名單匯出）。所以：

* 每次啟動產生一組隨機通行碼，啟動檔打開的網址帶著它。第一次進來就換成
  ``SameSite=Strict`` 的 cookie——別的網站發出的請求不會帶這個 cookie。
* 檢查 ``Host`` 標頭只能是本機。這擋的是 DNS rebinding：攻擊者把自己的網域
  解析到 127.0.0.1，瀏覽器就會把那個網域當成同一個來源。
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from controllers import CompanyController, CrawlController, DashboardController, ExportController
from core.config import AppConfig
from core.constants import DISPLAY_NAME, VERSION
from core.i18n import CRAWL_STATUS_LABELS, VERDICT_LABELS, label
from core.schemas import CompanyFilter
from core.scoring import LEAD_SCORE_LABEL, LEAD_SCORE_ORDER
from web.jobs import CrawlJob

WEB_DIR = Path(__file__).resolve().parent

#: 一頁顯示幾家。
PAGE_SIZE = 50

#: 名單頁可以選的排序。值是 :class:`CompanyFilter` 的 ``order_by``。
SORT_OPTIONS = {
    LEAD_SCORE_ORDER: LEAD_SCORE_LABEL,
    "updated_at": "最近更新",
    "company_name": "公司名稱",
}

COOKIE_NAME = "roster_session"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class CrawlRequest(BaseModel):
    #: 來源名稱。``None`` 是全部啟用的來源。
    source: str | None = None


def create_app(config: AppConfig, token: str) -> FastAPI:
    """建立網頁版。``token`` 是這次啟動的通行碼。"""
    app = FastAPI(title=DISPLAY_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals.update(app_name=DISPLAY_NAME, version=VERSION)
    templates.env.filters["verdict"] = lambda value: label(value, VERDICT_LABELS)

    companies = CompanyController()
    crawls = CrawlController(config)
    exports = ExportController(config)
    dashboard = DashboardController()
    job = CrawlJob(crawls)
    app.state.crawl_job = job

    @app.middleware("http")
    async def only_this_browser(request: Request, call_next):
        host = (request.url.hostname or "").lower()
        if host not in _LOCAL_HOSTS:
            return PlainTextResponse("只接受本機的連線。", status_code=400)

        if request.method == "GET" and request.query_params.get("token") == token:
            # 換成 cookie 之後轉回不帶通行碼的網址，它才不會留在網址列與歷史紀錄裡。
            response = RedirectResponse(request.url.path, status_code=303)
            response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict")
            return response

        if not secrets.compare_digest(request.cookies.get(COOKIE_NAME, ""), token):
            return PlainTextResponse(
                "請用「網頁版」啟動檔開啟，或複製終端機裡印出的網址。", status_code=403
            )
        return await call_next(request)

    @app.get("/")
    def company_list(request: Request, q: str = "", sort: str = LEAD_SCORE_ORDER, page: int = 1):
        sort = sort if sort in SORT_OPTIONS else LEAD_SCORE_ORDER
        page = max(page, 1)
        text = q.strip() or None
        total = companies.count(CompanyFilter(text=text))
        rows = companies.search(
            CompanyFilter(
                text=text,
                order_by=sort,
                descending=sort != "company_name",
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            )
        )
        return templates.TemplateResponse(
            request,
            "companies.html",
            {
                "rows": rows,
                "q": q,
                "sort": sort,
                "sort_options": SORT_OPTIONS,
                "page": page,
                "pages": max(1, -(-total // PAGE_SIZE)),
                "total": total,
                "stats": dashboard.stats(),
                "formats": exports.formats(),
            },
        )

    @app.get("/crawl")
    def crawl_page(request: Request):
        return templates.TemplateResponse(
            request,
            "crawl.html",
            {
                "sources": crawls.enabled_source_names(),
                "recent": dashboard.recent_crawls(),
                "status_labels": CRAWL_STATUS_LABELS,
            },
        )

    @app.post("/api/crawl")
    def start_crawl(body: CrawlRequest):
        if body.source is not None and body.source not in crawls.source_names():
            raise HTTPException(status_code=400, detail="沒有這個來源")
        if not job.start(body.source):
            raise HTTPException(status_code=409, detail="已經有一個爬取在進行中")
        return job.status()

    @app.get("/api/crawl")
    def crawl_status():
        return job.status()

    @app.post("/api/crawl/cancel")
    def cancel_crawl():
        job.cancel()
        return JSONResponse({"ok": True})

    @app.get("/export")
    def export(format: str = "excel", q: str = ""):
        if format not in exports.formats():
            raise HTTPException(status_code=400, detail="不支援的格式")
        path, _count = exports.run(
            format, None, CompanyFilter(text=q.strip() or None),
            report=lambda _payload: None, cancel_event=None,
        )
        return FileResponse(path, filename=path.name)

    return app
