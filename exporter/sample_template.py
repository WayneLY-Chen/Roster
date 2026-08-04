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


def _instructions(columns: list[tuple[str, str]]) -> list[str]:
    """給 Excel 的「填寫說明」分頁，一列一句。"""
    lines = [
        "怎麼填這份表",
        "",
        "1. 「匯入範例」分頁的第一列是標題，請不要更動或刪除。",
        "2. 從第二列開始，一列填一家公司。",
        f"3. 只有「{sample_columns()[0][0]}」是必填，其餘留白都可以。",
        "4. 三列示範資料填完後請刪掉，否則它們也會被匯入。",
        "5. 存檔後回到程式的「匯入」頁，選這個檔案即可。",
        "",
        "標題可以改成別的寫法嗎",
        "",
        "可以。程式認得多種常見寫法，例如「公司名稱」也接受「公司」、",
        "「廠商名稱」、「company」、「name」；「電子郵件」也接受「信箱」、",
        "「email」、「mail」。認不出來的欄位會被略過，匯入前的預覽畫面會",
        "列出哪些欄位對應到什麼，可以先確認再匯入。",
        "",
        "各欄位說明",
        "",
    ]
    hints = {
        "company_name": "必填。公司全名。",
        "tax_id": "8 碼數字，程式會檢查檢核碼。",
        "email": "會自動驗證格式，並查詢該網域是否真的收信。",
        "phone": "市話或手機皆可，會自動正規化（全形轉半形、去掉 +886）。",
        "website": "沒有 https:// 也沒關係，程式會自動補上。",
        "address": "完整地址，程式會從開頭抓出縣市。",
        "industry": "自由填寫，例如「電子零組件」、「機械設備」。",
        "contact_person": "窗口姓名，會另外建立成聯絡人。",
        "remark": "自由備註，不影響任何自動處理。",
    }
    for heading, field in columns:
        lines.append(f"{heading}：{hints.get(field, '自由填寫。')}")
    lines += [
        "",
        "重複的公司會怎麼樣",
        "",
        "程式會用統一編號、電子郵件、公司名稱＋電話等組合比對既有資料。",
        "判定為同一家時只會補上空白欄位，不會覆蓋你已經整理好的內容。",
    ]
    return lines


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
        # 說明另開一個分頁，不要混進資料裡。匯入器讀的是第一個分頁
        # （`pd.read_excel` 的預設），所以第二個分頁怎麼寫都不會影響匯入。
        notes = pd.DataFrame(
            {"填寫說明": _instructions(columns)},
        )
        with pd.ExcelWriter(target) as writer:
            frame.to_excel(writer, index=False, sheet_name="匯入範例")
            notes.to_excel(writer, index=False, sheet_name="填寫說明")
        return target

    raise ExportError(f"不支援的範例檔格式：{suffix or '（沒有副檔名）'}。請用 .csv 或 .xlsx。")


def sample_filename(suffix: str = ".xlsx") -> str:
    """建議的檔名，給存檔對話框當預設值。"""
    return f"匯入範例{suffix}"
