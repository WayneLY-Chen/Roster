"""Shared export machinery.

All three formats share one shaping step: :func:`build_dataframe` turns
:class:`~core.schemas.CompanyView` rows into a ``pandas`` DataFrame with the
columns named in ``exporter.columns``, in that order, with bilingual headers.
Format-specific code then only has to write the frame out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import ExportError
from core.legal import OPEN_DATA_ATTRIBUTION
from core.logging_setup import get_logger
from core.schemas import CompanyView

log = get_logger(LogCategory.EXPORT)

# Bilingual headers: the file is usually read by a Taiwanese sales team and
# sometimes by a spreadsheet tool that wants ASCII.
HEADER_LABELS: dict[str, str] = {
    "id": "ID",
    "company_name": "公司名稱 Company",
    "tax_id": "統一編號 Tax ID",
    "email": "Email",
    "phone": "電話 Phone",
    "website": "網站 Website",
    "address": "地址 Address",
    "industry": "產業 Industry",
    "english_name": "英文名稱 English Name",
    "fax": "傳真 Fax",
    "products": "主要產品 Products",
    "contact_person": "聯絡人 Contact",
    "pipeline_stage": "階段 Stage",
    "priority": "優先度 Priority",
    "status": "狀態 Status",
    "email_verdict": "Email 驗證 Verdict",
    "tags": "標籤 Tags",
    "source": "來源 Source",
    "source_url": "來源網址 Source URL",
    "follow_up_date": "追蹤日期 Follow-up",
    "created_at": "建立時間 Created",
    "updated_at": "更新時間 Updated",
    "remark": "備註 Remark",
    "lead_score": "名單品質 Lead Score",
    "capital_amount": "資本額 Capital",
    "registration_status": "登記狀態 Registration",
    "registration_checked_at": "登記查詢時間 Registry Checked",
}

DEFAULT_COLUMNS = (
    "id",
    "company_name",
    "lead_score",
    "tax_id",
    "email",
    "phone",
    "website",
    "address",
    "industry",
    "registration_status",
    "capital_amount",
    "contact_person",
    "pipeline_stage",
    "status",
    "tags",
    "source",
    "created_at",
    "updated_at",
)


def resolve_columns(config: AppConfig | None = None) -> list[str]:
    """Configured export columns, filtered to fields that actually exist."""
    config = config or get_config()
    requested = config.exporter.columns or list(DEFAULT_COLUMNS)
    # 算出來的欄位（名單品質）跟一般欄位一樣可以匯出，但它不在 model_fields
    # 裡——漏掉這一半的話，使用者在 config.yaml 填 lead_score 會被當成不存在
    # 的欄位丟掉，而且只有日誌裡有一行警告。
    valid_fields = set(CompanyView.model_fields) | set(CompanyView.model_computed_fields)

    columns = [name for name in requested if name in valid_fields]
    unknown = [name for name in requested if name not in valid_fields]
    if unknown:
        log.warning("ignoring unknown export columns: {}", ", ".join(unknown))
    return columns or list(DEFAULT_COLUMNS)


def _format_value(value: object, date_format: str) -> object:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, datetime):
        return value.strftime(date_format)
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return value


#: 匯出檔最多附加這麼多個自由欄位。名錄各自的欄位通常不到十個；設上限只是
#: 為了讓一份混了很多來源的資料不會變成幾百欄的試算表。
MAX_EXTRA_COLUMNS = 20


def extra_columns(rows: list[CompanyView], limit: int = MAX_EXTRA_COLUMNS) -> list[str]:
    """匯出資料裡出現過的自由欄位名稱，出現次數多的排前面。

    這些欄位是各個名錄自己的（「會員代表」「入會年月日」），欄位名稱由資料
    決定而不是由設定決定，所以只能在這裡從資料本身推。
    """
    counts: dict[str, int] = {}
    for row in rows:
        for key in getattr(row, "extra_fields", None) or {}:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts, key=lambda key: (-counts[key], key))[:limit]


def build_dataframe(
    rows: list[CompanyView],
    config: AppConfig | None = None,
    columns: list[str] | None = None,
    translate_headers: bool = True,
) -> pd.DataFrame:
    """Shape rows into the export frame.

    固定欄位之後會自動接上這批資料裡出現過的自由欄位，一個欄位一欄。少了這
    一段，使用者在公司詳細資料裡看得到的東西匯出之後就不見了。
    """
    config = config or get_config()
    columns = columns or resolve_columns(config)
    # 整包字典塞進一格對誰都沒有用；下面會把它攤成一欄一個欄位。
    columns = [column for column in columns if column != "extra_fields"]
    date_format = config.exporter.date_format
    extras = extra_columns(rows)

    records = []
    for row in rows:
        record = {
            column: _format_value(getattr(row, column, None), date_format)
            for column in columns
        }
        row_extras = getattr(row, "extra_fields", None) or {}
        for key in extras:
            record[key] = row_extras.get(key, "")
        records.append(record)

    all_columns = columns + extras
    frame = pd.DataFrame(records, columns=all_columns)

    if translate_headers:
        # 自由欄位沒有雙語標題可以查——它們的名稱就是名錄上原本的文字。
        frame = frame.rename(columns={c: HEADER_LABELS.get(c, c) for c in all_columns})
    return frame


#: 哪些欄位的內容來自政府開放資料。有其中任何一欄有值，匯出檔就必須帶著
#: 顯名標示——見 :mod:`crawler.registry` 的授權說明，那不是選配的。
REGISTRY_FIELDS = ("capital_amount", "registration_status")


def registry_attribution(rows: list[CompanyView]) -> str | None:
    """這批資料需要標示的資料來源；沒有用到開放資料時回 ``None``。

    以「資料真的在裡面」為判斷依據，而不是「欄位有沒有被選進來」：使用者
    只匯出公司名稱與信箱、完全沒有登記資料時，硬掛一行來源聲明只是雜訊；
    反過來只要有一筆帶著登記資料，這份檔案就必須標示。
    """
    for row in rows:
        if any(getattr(row, field, None) for field in REGISTRY_FIELDS):
            return OPEN_DATA_ATTRIBUTION
    return None


def timestamped_name(prefix: str, extension: str) -> str:
    """``companies-20260803-142530.xlsx``-style filename."""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{extension.lstrip('.')}"


class BaseExporter(ABC):
    """One output format."""

    #: File extension without the leading dot.
    extension: str = ""
    #: Human-readable format name, shown in the GUI.
    label: str = ""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def resolve_path(self, path: str | Path | None, prefix: str = "companies") -> Path:
        """Turn a user-supplied path (or ``None``) into a concrete file path."""
        output_dir = self.config.exporter.resolved_output_dir
        if path is None:
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir / timestamped_name(prefix, self.extension)

        target = Path(path).expanduser()
        if not target.is_absolute():
            target = output_dir / target
        if target.is_dir():
            target = target / timestamped_name(prefix, self.extension)
        elif target.suffix.lower() != f".{self.extension}":
            target = target.with_suffix(f".{self.extension}")

        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def export(
        self,
        rows: list[CompanyView],
        path: str | Path | None = None,
        columns: list[str] | None = None,
    ) -> Path:
        """Write ``rows`` and return the path written."""
        target = self.resolve_path(path)
        frame = build_dataframe(rows, self.config, columns)
        try:
            self._write(frame, target, rows)
        except OSError as exc:
            raise ExportError(f"could not write {target}: {exc}") from exc
        log.info("exported {} rows to {}", len(rows), target)
        return target

    @abstractmethod
    def _write(self, frame: pd.DataFrame, target: Path, rows: list[CompanyView]) -> None:
        """Write the shaped frame. ``rows`` is available for richer formats."""
