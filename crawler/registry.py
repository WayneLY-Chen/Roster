"""用統一編號補上公司登記資料。

名錄上抓到的是「這家公司對外怎麼寫自己」。這一步補的是另一半：經濟部商業司
的**商工登記公示資料**——這家公司在官方紀錄裡到底還在不在、資本額多少、
登記的負責人與地址是誰。

最有價值的其實不是資本額，是 ``Company_Status_Desc``。名錄網站不會把倒掉的
會員刪掉，所以一份剛爬回來的名單裡混著早就解散、撤銷、廢止的公司。寄開發信
之前先把它們標出來，省下來的是實實在在的時間。

## 授權

資料來自 https://data.gov.tw，適用**政府資料開放授權條款－第 1 版**：不限
目的（含商業利用）、免授權金、可改作。但有一項**強制義務**——

    「使用者利用依本條款提供之開放資料，及後續之衍生物，應以符合附件所示
     『顯名聲明』要求之方式，明確標示原資料提供機關之相關聲明；未盡顯名
     標示義務者，視為自始未取得開放資料之授權。」

所以 :data:`ATTRIBUTION` 不是裝飾。任何呈現這些資料的地方（畫面、匯出檔）
都必須帶著它，拿掉它等於從一開始就沒有取得授權。

## 這支 API 的兩件事

1. **只能用統一編號查。** 名稱查詢在這個資料集上回空陣列（實測過）。沒有
   統編的公司這一步幫不上忙，程式會照實說「沒有統編」，不會亂猜。
2. **它會忙線。** 尖峰時間回的不是 JSON 而是一頁「系統忙碌中」的 HTML。
   遇到就跳過那一筆、**不**標記成已查過，下一次補完自然會再試一次。

其餘規矩跟爬名錄完全一樣：遵守 robots.txt、沿用同一個速率限制器、
不停用 TLS 驗證。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import CRMError, CrawlError
from core.legal import OPEN_DATA_ATTRIBUTION, OPEN_DATA_ATTRIBUTION_SHORT
from core.logging_setup import get_logger
from crawler.fetcher import BaseFetcher, build_fetcher
from database.models import now
from database.repository import CompanyRepository
from database.session import session_scope
from verifier.validators import is_valid_tax_id

log = get_logger(LogCategory.CRAWL)

#: 商工登記公示資料－公司登記基本資料。
GCIS_DATASET_URL = (
    "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
)

#: 顯名標示。授權條款的強制義務，見模組說明。
#:
#: 條文本身放在 :mod:`core.legal`，跟其他使用條款的文字在一起——同一段文字
#: 會出現在設定畫面與匯出檔上，兩邊各寫一份遲早會不一致。
ATTRIBUTION = OPEN_DATA_ATTRIBUTION
ATTRIBUTION_SHORT = OPEN_DATA_ATTRIBUTION_SHORT

#: 已經查過的資料多久之後值得再查一次。
#:
#: 公司登記不常變，但「解散」這件事一旦發生就很重要。半年是「不會一直重複
#: 打同一支 API」與「不會拿著兩年前的狀態寄信」之間的折衷。
RECHECK_AFTER_DAYS = 180


class RegistryBusy(CRMError):
    """對方回了「系統忙碌中」而不是資料。跳過這一筆，下次再查。"""


@dataclass
class Registration:
    """一筆公司登記資料，已經整理成這支程式在用的形狀。"""

    tax_id: str
    company_name: str = ""
    status: str = ""
    capital_amount: int | None = None
    paid_in_capital: int | None = None
    responsible_name: str = ""
    location: str = ""
    setup_date: date | None = None
    register_organization: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == "核准設立"

    def extra_fields(self) -> dict[str, str]:
        """要原樣留在公司詳細資料裡的東西。

        這些不值得各自開一個資料庫欄位（不排序、不篩選），但使用者打開一家
        公司時看得到它們是有意義的——尤其是登記名稱，名錄上的簡稱跟登記
        全名常常對不起來。
        """
        fields: dict[str, str] = {}
        if self.company_name:
            fields["登記名稱"] = self.company_name
        if self.status:
            fields["登記狀態"] = self.status
        if self.paid_in_capital:
            fields["實收資本額"] = f"{self.paid_in_capital:,}"
        if self.setup_date:
            fields["設立日期"] = self.setup_date.isoformat()
        if self.register_organization:
            fields["登記機關"] = self.register_organization
        return fields


def roc_to_date(value: str | None) -> date | None:
    """民國日期字串轉成 ``date``。``"0760221"`` → 1987-02-21。

    商業司送出來的是七碼、年份補零的民國年。格式不對就回 ``None``——這是
    順帶的資訊，不值得讓整批補完因為一個怪日期停下來。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 7:
        return None
    try:
        return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
    except ValueError:
        return None


def _amount(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_response(body: str, tax_id: str) -> Registration | None:
    """把 API 回應轉成 :class:`Registration`。查無資料回 ``None``。

    :raises RegistryBusy: 回應根本不是 JSON（忙線時的 HTML 頁面）。
    """
    text = (body or "").strip()
    if not text:
        # 200 加空白內容就是「查無此統編」，不是錯誤。
        return None
    if not text.startswith(("[", "{")):
        raise RegistryBusy("商業司回應不是資料，可能是系統忙碌中")

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RegistryBusy(f"無法解讀商業司的回應：{exc}") from exc

    rows = payload if isinstance(payload, list) else [payload]
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return None

    row = rows[0]
    return Registration(
        tax_id=str(row.get("Business_Accounting_NO") or tax_id).strip(),
        company_name=str(row.get("Company_Name") or "").strip(),
        status=str(row.get("Company_Status_Desc") or "").strip(),
        capital_amount=_amount(row.get("Capital_Stock_Amount")),
        paid_in_capital=_amount(row.get("Paid_In_Capital_Amount")),
        responsible_name=str(row.get("Responsible_Name") or "").strip(),
        location=str(row.get("Company_Location") or "").strip(),
        setup_date=roc_to_date(row.get("Company_Setup_Date")),
        register_organization=str(row.get("Register_Organization_Desc") or "").strip(),
    )


def query_url(tax_id: str) -> str:
    """查一個統編的完整網址。

    篩選條件走 OData 的 ``$filter``。整段（含空白）都要 URL 編碼，
    ``quote`` 的 ``safe`` 留空就是這個意思——預設會放過 ``/``，而這裡
    沒有任何字元應該被放過。
    """
    condition = quote(f"Business_Accounting_NO eq {tax_id}", safe="")
    return f"{GCIS_DATASET_URL}?$format=json&$filter={condition}&$skip=0&$top=1"


def lookup(tax_id: str, fetcher: BaseFetcher) -> Registration | None:
    """查一個統編的登記資料。查無資料回 ``None``。

    :raises RegistryBusy: 對方忙線。
    :raises CrawlError: 連不上或被 robots.txt 擋下。
    """
    clean = "".join(ch for ch in str(tax_id or "") if ch.isdigit())
    if not is_valid_tax_id(clean):
        # 檢查碼過不了的統編一定查不到，不需要為它送一次請求。
        log.debug("跳過格式不正確的統一編號：{}", tax_id)
        return None
    result = fetcher.fetch(query_url(clean))
    return parse_response(result.html, clean)


@dataclass
class RegistrySummary:
    """一次補完作業的結果。"""

    considered: int = 0
    looked_up: int = 0
    matched: int = 0
    updated: int = 0
    defunct: int = 0
    not_found: int = 0
    skipped_no_tax_id: int = 0
    busy: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _needs_a_check(company, recheck_after_days: int) -> bool:
    checked = getattr(company, "registration_checked_at", None)
    if checked is None:
        return True
    return (now() - checked).days >= recheck_after_days


def _apply(company, registration: Registration) -> bool:
    """把登記資料寫進一家公司。回傳有沒有真的改到東西。

    只補空的欄位，不覆蓋使用者已經有的資料——除了登記狀態與資本額，那兩個
    本來就是這一步負責維護的，官方紀錄一定比舊值準。
    """
    changed = False

    if company.registration_status != registration.status:
        company.registration_status = registration.status
        changed = True
    if registration.capital_amount and company.capital_amount != registration.capital_amount:
        company.capital_amount = registration.capital_amount
        changed = True

    if registration.responsible_name and not (company.contact_person or "").strip():
        company.contact_person = registration.responsible_name
        changed = True
    if registration.location and not (company.address or "").strip():
        company.address = registration.location
        changed = True

    extras = dict(company.extra_fields or {})
    for key, value in registration.extra_fields().items():
        if extras.get(key) != value:
            extras[key] = value
            changed = True
    if changed:
        # 整包重新指派，不是就地改。SQLAlchemy 是靠「欄位換了一個物件」偵測
        # 變更的，直接改 dict 裡的鍵不會被寫回資料庫。
        company.extra_fields = extras

    company.registration_checked_at = now()
    if changed:
        company.updated_at = now()
    return changed


def enrich_registrations(
    limit: int | None = None,
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    company_ids: Iterable[int] | None = None,
    recheck_after_days: int = RECHECK_AFTER_DAYS,
) -> RegistrySummary:
    """對有統一編號的公司補上公司登記資料。"""
    config = config or get_config()
    summary = RegistrySummary()

    owned = fetcher is None
    fetcher = fetcher or build_fetcher(config)

    try:
        with session_scope() as session:
            repo = CompanyRepository(session)

            if company_ids is not None:
                candidates = [c for c in (repo.get(i) for i in company_ids) if c is not None]
            else:
                candidates = repo.all()

            targets = []
            for company in candidates:
                if not (company.tax_id or "").strip():
                    summary.skipped_no_tax_id += 1
                    continue
                if _needs_a_check(company, recheck_after_days):
                    targets.append(company)
            if limit:
                targets = targets[:limit]
            summary.considered = len(targets)

            for index, company in enumerate(targets, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    log.warning("補公司登記資料已取消")
                    break

                if progress is not None:
                    progress(index, len(targets), company.company_name)

                try:
                    registration = lookup(company.tax_id or "", fetcher)
                except RegistryBusy as exc:
                    summary.busy += 1
                    log.info("{}：{}", company.company_name, exc)
                    continue
                except CrawlError as exc:
                    summary.failed += 1
                    message = f"{company.company_name}：{exc}"
                    summary.errors.append(message)
                    log.warning(message)
                    continue
                except Exception as exc:
                    summary.failed += 1
                    message = f"{company.company_name}：{type(exc).__name__}: {exc}"
                    summary.errors.append(message)
                    log.warning(message)
                    continue

                summary.looked_up += 1
                if registration is None:
                    # 查無此統編也算查過了，否則每一次補完都會再問一遍同一批。
                    summary.not_found += 1
                    company.registration_checked_at = now()
                    session.commit()
                    continue

                summary.matched += 1
                if not registration.is_active:
                    summary.defunct += 1
                    log.info(
                        "{}（{}）登記狀態為「{}」",
                        company.company_name, company.tax_id, registration.status,
                    )
                if _apply(company, registration):
                    summary.updated += 1
                session.commit()
    finally:
        if owned:
            fetcher.close()

    log.info(
        "公司登記補完：查詢 {} 家、對到 {} 家、更新 {} 筆、已停業 {} 家、"
        "查無 {} 家、忙線跳過 {} 家",
        summary.considered, summary.matched, summary.updated,
        summary.defunct, summary.not_found, summary.busy,
    )
    return summary
