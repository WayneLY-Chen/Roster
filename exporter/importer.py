"""Import companies from CSV, Excel or JSON.

Interchange runs both ways, so import lives beside export. Incoming rows go
through exactly the same cleaning, validation and deduplication as crawled
records -- a spreadsheet from a trade show gets no shortcut past the rules.

Column names are matched loosely: the bilingual headers this app exports, plain
English field names, and common Chinese headers all resolve to the same field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import ExportError
from core.logging_setup import get_logger
from core.schemas import RawCompany
from database.repository import CompanyRepository
from database.session import session_scope
from verifier.dedupe import deduplicate_batch
from verifier.mx import MXChecker
from verifier.service import CleaningService

log = get_logger(LogCategory.EXPORT)

# Every accepted spelling of each field, lowercased and stripped of spaces.
_COLUMN_ALIASES: dict[str, str] = {
    "company_name": "company_name",
    "company": "company_name",
    "companyname": "company_name",
    "name": "company_name",
    "公司": "company_name",
    "公司名": "company_name",
    "公司名稱": "company_name",
    "公司全名": "company_name",
    "公司行號": "company_name",
    "廠商": "company_name",
    "廠商名": "company_name",
    "廠商名稱": "company_name",
    "廠商全名": "company_name",
    "工廠名稱": "company_name",
    "企業名稱": "company_name",
    "事業名稱": "company_name",
    "機構名稱": "company_name",
    "單位名稱": "company_name",
    "商號名稱": "company_name",
    "會員名稱": "company_name",
    "客戶名稱": "company_name",
    "名稱": "company_name",
    "tax_id": "tax_id",
    "taxid": "tax_id",
    "統一編號": "tax_id",
    "統編": "tax_id",
    "email": "email",
    "e-mail": "email",
    "mail": "email",
    "電子郵件": "email",
    "信箱": "email",
    "phone": "phone",
    "tel": "phone",
    "telephone": "phone",
    "電話": "phone",
    "聯絡電話": "phone",
    "website": "website",
    "url": "website",
    "web": "website",
    "網站": "website",
    "網址": "website",
    "address": "address",
    "addr": "address",
    "地址": "address",
    "industry": "industry",
    "產業": "industry",
    "行業": "industry",
    "類別": "industry",
    "english_name": "english_name",
    "englishname": "english_name",
    "english": "english_name",
    "英文名稱": "english_name",
    "英文名": "english_name",
    "外文名稱": "english_name",
    "fax": "fax",
    "傳真": "fax",
    "傳真號碼": "fax",
    "products": "products",
    "product": "products",
    "主要產品": "products",
    "產品": "products",
    "代理產品": "products",
    "營業項目": "products",
    "contact_person": "contact_person",
    "contact": "contact_person",
    "contactperson": "contact_person",
    "聯絡人": "contact_person",
    "窗口": "contact_person",
    "remark": "remark",
    "note": "remark",
    "notes": "remark",
    "備註": "remark",
}


@dataclass
class ImportSummary:
    """Outcome of one import run."""

    file: str = ""
    rows_read: int = 0
    records_new: int = 0
    records_merged: int = 0
    records_duplicate: int = 0
    records_invalid: int = 0
    unmapped_columns: list[str] = field(default_factory=list)
    #: 這一次匯入實際碰到的公司編號（新增的與合併進去的都算）。
    #:
    #: 「匯入後自動補齊」需要它：補齊只該處理這一批，不該把使用者資料庫裡
    #: 既有的幾千家一起重跑一遍——那是另一個決定，該由使用者到「爬取」頁
    #: 自己按下去。
    company_ids: list[int] = field(default_factory=list)

    @property
    def records_stored(self) -> int:
        return self.records_new + self.records_merged


#: 標題裡出現這些字，就**不要**把它當成公司名稱，即使它含有「名稱」。
#:
#: 「負責人姓名」「聯絡人名稱」都含有名字類的字眼，猜錯的代價是整份名單的
#: 公司名稱欄變成一堆人名——比「認不出來」糟得多，因為它不會報錯。
_NOT_A_COMPANY_NAME = (
    "負責人", "代表人", "聯絡人", "窗口", "人員", "姓名", "承辦",
    "英文", "english", "產品", "地址", "電話", "傳真", "信箱", "備註",
)

#: 認不出確切標題時，照這個順序找「看起來像公司名稱」的欄位。
#:
#: 排序就是特異度：「公司名稱」一定是，「名稱」只是可能是。實際的名錄標題
#: 千奇百怪（「工廠名稱」「事業單位名稱」「廠商全名(中文)」），列不完，所以
#: 精確比對之外一定要有這一層——否則使用者拿到的是一句「找不到公司名稱欄」，
#: 而他的檔案裡明明就有。
_COMPANY_NAME_HINTS = (
    "公司名稱", "廠商名稱", "工廠名稱", "企業名稱", "事業名稱", "機構名稱",
    "單位名稱", "商號名稱", "會員名稱", "客戶名稱",
    "company name", "company_name", "companyname",
    "公司", "廠商", "工廠", "企業", "事業", "商號", "名稱",
    "company", "name",
)


def _looks_like_a_company_name(header: object) -> bool:
    """這個標題看起來是公司名稱那一欄嗎？（精確比對失敗之後才問。）"""
    text = str(header).strip().lower()
    if not text or text.startswith("unnamed:"):
        return False
    if any(bad in text for bad in _NOT_A_COMPANY_NAME):
        return False
    return any(hint in text for hint in _COMPANY_NAME_HINTS)


def _canonical(header: object) -> str | None:
    """Map one spreadsheet header to a RawCompany field, if we recognise it."""
    text = str(header).strip().lower()
    if not text:
        return None
    if text in _COLUMN_ALIASES:
        return _COLUMN_ALIASES[text]
    # Exported headers look like "公司名稱 Company"; try each token.
    for token in text.replace("/", " ").replace("_", " ").split():
        if token in _COLUMN_ALIASES:
            return _COLUMN_ALIASES[token]
    compact = text.replace(" ", "")
    return _COLUMN_ALIASES.get(compact)


def read_table(path: str | Path) -> pd.DataFrame:
    """Load a CSV, Excel or JSON file into a DataFrame."""
    source = Path(path).expanduser()
    if not source.exists():
        raise ExportError(f"import file not found: {source}")

    suffix = source.suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm", ".xls"):
            return pd.read_excel(source, dtype=str)
        if suffix == ".json":
            # Parse with the json module, not pandas: this app's own export is
            # an object with a "companies" array, and pd.read_json flattens
            # that into a frame whose column count rarely matches its row
            # count -- it raises before any unwrapping could happen, so the
            # app could never re-import its own JSON.
            payload = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                records = payload.get("companies", payload.get("data"))
                if records is None:
                    # A plain object: treat it as a single record.
                    records = [payload]
            else:
                records = payload
            if not isinstance(records, list):
                raise ExportError(f"{source.name} 的 JSON 結構無法解讀")
            return pd.DataFrame([r for r in records if isinstance(r, dict)], dtype=object)
        if suffix in (".csv", ".txt", ""):
            for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
                try:
                    return pd.read_csv(source, dtype=str, encoding=encoding)
                except UnicodeDecodeError:
                    continue
            raise ExportError(f"could not decode {source.name}; save it as UTF-8 CSV")
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"could not read {source.name}: {exc}") from exc

    raise ExportError(f"unsupported import format: {suffix or '(no extension)'}")


def rows_to_records(frame: pd.DataFrame, source_label: str) -> tuple[list[RawCompany], list[str]]:
    """Map a DataFrame onto records. Returns ``(records, unmapped_headers)``."""
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for column in frame.columns:
        canonical = _canonical(column)
        if canonical and canonical not in mapping.values():
            mapping[str(column)] = canonical
        else:
            unmapped.append(str(column))

    if "company_name" not in mapping.values():
        # 精確比對認不出來，再用「看起來像不像」找一次。
        #
        # 名錄的標題列不完：「工廠名稱」「事業單位名稱」「廠商全名(中文)」都
        # 是真的遇過的。少了這一層，使用者得到的是「找不到公司名稱欄」，而他
        # 的檔案裡明明就有一欄叫那個名字。
        for column in list(unmapped):
            if _looks_like_a_company_name(column):
                mapping[str(column)] = "company_name"
                unmapped.remove(column)
                log.info("把「{}」當成公司名稱那一欄", column)
                break

    if "company_name" not in mapping.values():
        # 錯誤訊息要講**使用者的檔案裡有什麼**，不是只講我們期待什麼。
        #
        # 原本這句只列出五個接受的名稱，讀的人沒辦法從它知道該去改哪一欄——
        # 尤其標題常常長得像「廠商全名(中文)」，看起來明明就對。
        found = "、".join(
            str(c).strip() for c in frame.columns if str(c).strip()
        ) or "（這個檔案沒有標題列）"
        raise ExportError(
            "找不到公司名稱那一欄。\n"
            f"你的檔案裡有這些欄位：{found}\n"
            "認得的名稱有：公司名稱、公司、廠商名稱、工廠名稱、企業名稱、"
            "名稱、company_name、company、name（含有這些字的也認得）。\n"
            "把公司名稱那一欄的標題改成上面任何一個就可以匯入了。"
        )

    # 對應不到的欄位不再丟掉，改成原樣保留為自由欄位。匯出時每個自由欄位
    # 各佔一欄，不收回來的話「匯出→在 Excel 改→匯入」這一趟就會把它們洗掉。
    # 空白與 pandas 給無標題欄位取的 "Unnamed: 3" 排除在外，那些是版面不是資料。
    keepable = [
        column for column in unmapped
        if column.strip() and not column.startswith("Unnamed:")
    ]

    def _cell(row, column: str) -> str:
        value = row.get(column)
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        text = str(value).strip()
        return "" if text.lower() == "nan" else text

    records: list[RawCompany] = []
    for _, row in frame.iterrows():
        values: dict[str, str] = {}
        for column, field_name in mapping.items():
            text = _cell(row, column)
            if text:
                values[field_name] = text
        if not values.get("company_name"):
            continue
        extra_fields = {
            column: text for column in keepable if (text := _cell(row, column))
        }
        remark = values.pop("remark", None)
        records.append(
            RawCompany(
                **values,
                source=source_label,
                extra={"remark": remark} if remark else {},
                extra_fields=extra_fields,
            )
        )

    return records, unmapped


def import_file(
    path: str | Path,
    source_label: str | None = None,
    config: AppConfig | None = None,
) -> ImportSummary:
    """Import a file into the database, applying the full cleaning pipeline."""
    config = config or get_config()
    source = Path(path).expanduser()
    frame = read_table(source)

    summary = ImportSummary(file=str(source), rows_read=len(frame))
    records, unmapped = rows_to_records(frame, source_label or f"import:{source.name}")
    summary.unmapped_columns = unmapped

    unique, dropped = deduplicate_batch(records)
    summary.records_duplicate += dropped

    with session_scope() as session:
        repo = CompanyRepository(session)
        mx = MXChecker(config, session) if config.verifier.check_mx else None
        cleaner = CleaningService(config, mx)

        cleaned, rejected = cleaner.clean_many(unique)
        summary.records_invalid = rejected

        for record in cleaned:
            company, merged = repo.upsert(record)
            if company.id is not None:
                summary.company_ids.append(company.id)
            if merged:
                summary.records_merged += 1
                summary.records_duplicate += 1
            else:
                summary.records_new += 1

    log.info(
        "imported {}: {} rows -> {} new, {} merged, {} duplicates, {} rejected",
        source.name,
        summary.rows_read,
        summary.records_new,
        summary.records_merged,
        summary.records_duplicate,
        summary.records_invalid,
    )
    return summary
