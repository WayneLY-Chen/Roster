"""產生一份可以直接填寫的匯入範例檔。

欄位不是手寫死的，而是從 :data:`exporter.importer._COLUMN_ALIASES` 推導出來
——那份對照表就是匯入器實際認得的欄位。日後新增一個可匯入的欄位，範例檔會
自動跟著多一欄；手寫一份的話，範例遲早會跟真正的匯入邏輯脫節，而使用者只會
發現「照著範例填卻匯不進去」。
"""

from __future__ import annotations

from pathlib import Path

from core.errors import ExportError
from exporter.importer import _COLUMN_ALIASES

#: 範例資料。刻意用一眼就看得出是假的公司名與 example.com 網域，避免有人
#: 誤以為是真實名單而直接寄信出去。electronic mail 的 example.com 是 RFC 2606
#: 保留給文件使用的網域，不會有人真的收到。
_SAMPLE_ROWS: tuple[dict[str, str], ...] = (
    {
        "company_name": "範例科技股份有限公司",
        "tax_id": "12345678",
        "email": "sales@example.com",
        "phone": "02-1234-5678",
        "website": "https://www.example.com",
        "address": "台北市信義區信義路五段7號",
        "industry": "電子零組件",
        "contact_person": "王小明",
        "remark": "這一列是範例，匯入前請刪除",
    },
    {
        "company_name": "示範機械工業有限公司",
        "tax_id": "87654321",
        "email": "info@example.org",
        "phone": "04-2345-6789",
        "website": "https://www.example.org",
        "address": "台中市西屯區工業區一路100號",
        "industry": "機械設備",
        "contact_person": "陳美玲",
        "remark": "這一列是範例，匯入前請刪除",
    },
    {
        # 只有公司名稱是必填的，其餘留白也能匯入——用這一列示範給使用者看。
        "company_name": "只有名稱也可以匯入有限公司",
        "tax_id": "",
        "email": "",
        "phone": "",
        "website": "",
        "address": "",
        "industry": "",
        "contact_person": "",
        "remark": "其他欄位留白沒關係，之後可以用爬取或補齊功能填上",
    },
)

#: 匯入時唯一不能空白的欄位。`rows_to_records` 沒有它會直接拒絕整個檔案。
REQUIRED_FIELD = "company_name"


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def sample_columns() -> list[tuple[str, str]]:
    """回傳 ``(中文標題, 欄位代號)``，順序照匯入器的定義順序。

    每個欄位挑一個中文別名當標題——這是給台灣使用者填的表，中文標題比
    ``company_name`` 好懂，而匯入器對兩者一視同仁。
    """
    headers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field in _COLUMN_ALIASES.values():
        if field in seen:
            continue
        seen.add(field)
        # 取最長的中文別名。同一個欄位常有長短兩種寫法（「公司」與「公司名稱」、
        # 「統編」與「統一編號」），長的那個對照著空白表格填時比較不會會錯意。
        chinese = max(
            (alias for alias, target in _COLUMN_ALIASES.items()
             if target == field and _has_cjk(alias)),
            key=len,
            default=field,
        )
        headers.append((chinese, field))
    return headers


def write_sample(path: str | Path) -> Path:
    """把範例檔寫到 ``path``，副檔名決定格式（``.csv`` / ``.xlsx``）。

    CSV 一律用 ``utf-8-sig``。少了那個 BOM，Excel 在中文版 Windows 上會用
    系統的 Big5 去猜，中文標題會全部變成亂碼——而使用者只會覺得是程式壞了。
    """
    target = Path(path).expanduser()
    suffix = target.suffix.lower()
    columns = sample_columns()
    headers = [heading for heading, _ in columns]
    rows = [[row.get(field, "") for _, field in columns] for row in _SAMPLE_ROWS]

    target.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".csv":
        import csv

        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return target

    if suffix in {".xlsx", ".xls"}:
        import pandas as pd

        frame = pd.DataFrame(rows, columns=headers)
        frame.to_excel(target, index=False, sheet_name="匯入範例")
        return target

    raise ExportError(f"不支援的範例檔格式：{suffix or '（沒有副檔名）'}。請用 .csv 或 .xlsx。")


def sample_filename(suffix: str = ".xlsx") -> str:
    """建議的檔名，給存檔對話框當預設值。"""
    return f"匯入範例{suffix}"
