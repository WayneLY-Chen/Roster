"""網頁版的「Gmail 標籤」頁。後端是 :class:`controllers.labels.GmailLabelController`。

讀信箱要幾秒到幾十秒，這裡直接在請求裡做完（FastAPI 會放到執行緒池跑），
畫面上轉圈圈等。「預覽 → 勾選 → 套用」中間那份預覽結果存在伺服器上：這個
網頁只有自己一個人在用，不需要分使用者。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from controllers.labels import GmailLabelController
from core.config import AppConfig


class ScanRequest(BaseModel):
    label: str
    limit: int = Field(default=200, ge=10, le=2000)


class ApplyRequest(BaseModel):
    #: 預覽表上勾起來的那幾列的索引。
    indices: list[int]


class SyncRequest(BaseModel):
    tags: bool = True
    status: bool = True
    apply: bool = False


def gmail_router(config: AppConfig, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    controller = GmailLabelController(config)
    #: 最近一次預覽的結果，「套用」時照索引從這裡拿。
    pending: dict[str, Any] = {}

    def chosen(kind: str, indices: list[int]) -> tuple[Any, list]:
        scan = pending.get(kind)
        if scan is None:
            raise HTTPException(status_code=409, detail="請先按「預覽」。")
        items = scan.matches if kind == "classify" else scan.candidates
        return scan, [items[i] for i in indices if 0 <= i < len(items)]

    @router.get("/gmail")
    def gmail_page(request: Request):
        return templates.TemplateResponse(request, "gmail.html", {})

    @router.get("/api/gmail/labels")
    def labels():
        return {"labels": controller.labels()}

    @router.post("/api/gmail/classify/scan")
    def classify_scan(body: ScanRequest):
        scan = controller.scan_classify(body.label, body.limit)
        pending["classify"] = scan
        return {
            "summary": scan.describe(),
            "rows": [
                {"company_name": m.company_name, "address": m.address, "already": m.already}
                for m in scan.matches
            ],
        }

    @router.post("/api/gmail/classify/apply")
    def classify_apply(body: ApplyRequest):
        _scan, matches = chosen("classify", body.indices)
        added = controller.apply_classify(matches)
        pending.pop("classify", None)
        return {"summary": f"{added} 家公司貼上了標籤。"}

    @router.post("/api/gmail/harvest/scan")
    def harvest_scan(body: ScanRequest):
        scan = controller.scan_harvest(body.label, body.limit)
        pending["harvest"] = scan
        pending["harvest_label"] = body.label  # 套用時要知道貼哪個標籤
        return {
            "summary": scan.describe(),
            "rows": [
                {
                    "company_name": c.company_name,
                    "person": c.person or "",
                    "email": c.email or "",
                    "action": c.action,
                    "storable": c.storable,
                }
                for c in scan.candidates
            ],
        }

    @router.post("/api/gmail/harvest/apply")
    def harvest_apply(body: ApplyRequest):
        _scan, candidates = chosen("harvest", body.indices)
        summary = controller.apply_harvest(
            [c for c in candidates if c.storable], pending["harvest_label"]
        )
        pending.pop("harvest", None)
        return {"summary": summary.describe()}

    @router.post("/api/gmail/sync")
    def sync(body: SyncRequest):
        if not (body.tags or body.status):
            raise HTTPException(status_code=400, detail="至少勾一種要貼的標籤。")
        result = controller.sync(body.tags, body.status, body.apply)
        return {
            "summary": result.describe(),
            "rows": [
                {"label": c.label, "companies": c.companies, "messages": c.messages}
                for c in result.counts
            ],
        }

    return router
