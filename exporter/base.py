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
}

DEFAULT_COLUMNS = (
    "id",
    "company_name",
    "tax_id",
    "email",
    "phone",
    "website",
    "address",
    "industry",
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
    valid_fields = set(CompanyView.model_fields)

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


def build_dataframe(
    rows: list[CompanyView],
    config: AppConfig | None = None,
    columns: list[str] | None = None,
    translate_headers: bool = True,
) -> pd.DataFrame:
    """Shape rows into the export frame."""
    config = config or get_config()
    columns = columns or resolve_columns(config)
    date_format = config.exporter.date_format

    records = [
        {
            column: _format_value(getattr(row, column, None), date_format)
            for column in columns
        }
        for row in rows
    ]
    frame = pd.DataFrame(records, columns=columns)

    if translate_headers:
        frame = frame.rename(columns={c: HEADER_LABELS.get(c, c) for c in columns})
    return frame


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
