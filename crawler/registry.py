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

1. **這個資料集只能用統一編號查。** 名稱查詢在它上面回空陣列（實測過）。
   要用名稱查得換另一個資料集，見下面「用名稱查」。
2. **它會忙線。** 尖峰時間回的不是 JSON 而是一頁「系統忙碌中」的 HTML。
   遇到就跳過那一筆、**不**標記成已查過，下一次補完自然會再試一次。

## 用名稱查

:data:`GCIS_NAME_DATASET_URL` 是**另一個**資料集，它支援 ``like`` 模糊查詢，
所以只有公司名字、沒有統編的那些也補得到。它有一個不寫在文件裡、但少了就
一定回空陣列的條件——``Company_Status eq 01``（核准設立）：

    Company_Name like 台灣積體電路 and Company_Status eq 01   → 1 筆
    Company_Name like 台灣積體電路                            → 0 筆

所以 :func:`name_query_url` 一律把狀態條件帶上。副作用是它**只查得到還活著
的公司**，這對這支程式來說剛好是想要的：查不到就是查不到，不會拿一家已經
解散的公司來充數。

模糊查詢會一次回好幾家（「台積電」會回「台積電機」「台積電梯」），所以
:func:`best_name_match` 只在正規化後的名稱**完全相同**時才算數。八成像不算
——補錯一家公司的統編，比留白還糟。

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
from verifier.normalize import company_name_key
from verifier.validators import is_valid_tax_id

log = get_logger(LogCategory.CRAWL)

#: 商工登記公示資料－公司登記基本資料。只能用統一編號查。
GCIS_DATASET_URL = (
    "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
)

#: 商工登記公示資料－支援名稱模糊查詢的那一份。見模組說明「用名稱查」。
GCIS_NAME_DATASET_URL = (
    "https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C"
)

#: 「核准設立」的狀態代碼。名稱查詢少了這個條件就一定回空陣列。
ACTIVE_STATUS_CODE = "01"

#: 一次名稱查詢最多看幾筆候選。模糊比對本來就會回一串，但真正的完全相符
#: 只會有一個，多要幾筆只是為了讓它不會剛好落在截斷之外。
NAME_MATCH_CANDIDATES = 10

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


def _rows(body: str) -> list[dict]:
    """把 API 回應拆成一串 dict。查無資料回空 list。

    :raises RegistryBusy: 回應根本不是 JSON（忙線時的 HTML 頁面）。
    """
    text = (body or "").strip()
    if not text:
        # 200 加空白內容就是「查無資料」，不是錯誤。
        return []
    if not text.startswith(("[", "{")):
        raise RegistryBusy("商業司回應不是資料，可能是系統忙碌中")

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RegistryBusy(f"無法解讀商業司的回應：{exc}") from exc

    rows = payload if isinstance(payload, list) else [payload]
    return [row for row in rows if isinstance(row, dict)]


def _to_registration(row: dict, fallback_tax_id: str = "") -> Registration:
    """一列 API 回應轉成 :class:`Registration`。兩個資料集的欄位名稱相同。"""
    return Registration(
        tax_id=str(row.get("Business_Accounting_NO") or fallback_tax_id).strip(),
        company_name=str(row.get("Company_Name") or "").strip(),
        status=str(row.get("Company_Status_Desc") or "").strip(),
        capital_amount=_amount(row.get("Capital_Stock_Amount")),
        paid_in_capital=_amount(row.get("Paid_In_Capital_Amount")),
        responsible_name=str(row.get("Responsible_Name") or "").strip(),
        location=str(row.get("Company_Location") or "").strip(),
        setup_date=roc_to_date(row.get("Company_Setup_Date")),
        register_organization=str(row.get("Register_Organization_Desc") or "").strip(),
    )


def parse_response(body: str, tax_id: str) -> Registration | None:
    """把統編查詢的回應轉成 :class:`Registration`。查無資料回 ``None``。

    :raises RegistryBusy: 回應根本不是 JSON（忙線時的 HTML 頁面）。
    """
    rows = _rows(body)
    return _to_registration(rows[0], tax_id) if rows else None


def parse_name_response(body: str) -> list[Registration]:
    """把名稱查詢的回應轉成一串 :class:`Registration`。

    模糊查詢會回好幾家，順序由對方決定，這裡原樣保留——挑哪一家是
    :func:`best_name_match` 的事，不是解析的事。

    :raises RegistryBusy: 對方忙線。
    """
    return [_to_registration(row) for row in _rows(body)]


def query_url(tax_id: str) -> str:
    """查一個統編的完整網址。

    篩選條件走 OData 的 ``$filter``。整段（含空白）都要 URL 編碼，
    ``quote`` 的 ``safe`` 留空就是這個意思——預設會放過 ``/``，而這裡
    沒有任何字元應該被放過。
    """
    condition = quote(f"Business_Accounting_NO eq {tax_id}", safe="")
    return f"{GCIS_DATASET_URL}?$format=json&$filter={condition}&$skip=0&$top=1"


def name_query_url(name: str, limit: int = NAME_MATCH_CANDIDATES) -> str:
    """用公司名稱模糊查詢的完整網址。

    ``and Company_Status eq 01`` 不能拿掉，見模組說明——少了它對方一律回
    空陣列，而不是回一個「查無資料」的錯誤，所以拿掉之後看起來會像是
    「這家公司沒登記」。
    """
    condition = quote(
        f"Company_Name like {name} and Company_Status eq {ACTIVE_STATUS_CODE}",
        safe="",
    )
    return f"{GCIS_NAME_DATASET_URL}?$format=json&$filter={condition}&$skip=0&$top={limit}"


def best_name_match(name: str, candidates: Iterable[Registration]) -> Registration | None:
    """候選裡跟 ``name`` 是同一家的那一個。沒有就回 ``None``。

    比對用 :func:`~verifier.normalize.company_name_key`，也就是去掉「股份有限
    公司」這類後綴、統一台/臺之後的形式，所以名錄上的「台灣積體電路製造」
    對得上登記名稱「台灣積體電路製造股份有限公司」。

    只接受完全相同的鍵。模糊查詢「台積電」會一起回「台積電機」「台積電梯」，
    而把統編、負責人、登記地址補到錯的公司上，比留白糟得多——留白至少看得
    出來是缺的。
    """
    target = company_name_key(name)
    if not target:
        return None
    for candidate in candidates:
        if company_name_key(candidate.company_name) == target:
            return candidate
    return None


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


def lookup_by_name(name: str, fetcher: BaseFetcher) -> Registration | None:
    """用公司名稱查登記資料。查無、或查回來的沒有一家對得上，回 ``None``。

    :raises RegistryBusy: 對方忙線。
    :raises CrawlError: 連不上或被 robots.txt 擋下。
    """
    clean = (name or "").strip()
    if not clean:
        return None
    result = fetcher.fetch(name_query_url(clean))
    matched = best_name_match(clean, parse_name_response(result.html))
    if matched is None:
        log.debug("「{}」在商業司查不到完全相符的登記名稱", clean)
    return matched


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


def apply_registration(company, registration: Registration) -> bool:
    """把登記資料寫進一家公司。回傳有沒有真的改到東西。

    只補空的欄位，不覆蓋使用者已經有的資料——除了登記狀態與資本額，那兩個
    本來就是這一步負責維護的，官方紀錄一定比舊值準。

    :mod:`crawler.complete` 也用這一支。同一件事只該有一份實作：「哪些欄位
    可以覆蓋、哪些只能補空的」是這個專案對使用者的承諾，兩邊各寫一份遲早
    會有一邊悄悄開始蓋掉人家手動填的資料。
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
                if apply_registration(company, registration):
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
