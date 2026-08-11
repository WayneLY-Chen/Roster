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
    "公司名稱": "company_name",
    "廠商名稱": "company_name",
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
        raise ExportError(
            "no company-name column found. Expected one of: "
            "company_name, company, name, 公司名稱, 廠商名稱"
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
