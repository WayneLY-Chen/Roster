"""Import companies from CSV, Excel or JSON.

Interchange runs both ways, so import lives beside export. Incoming rows go
through exactly the same cleaning, validation and deduplication as crawled
records -- a spreadsheet from a trade show gets no shortcut past the rules.

Column names are matched loosely: the bilingual headers this app exports, plain
English field names, and common Chinese headers all resolve to the same field.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
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


#: 以這些字結尾的字串是**一家公司的名字**，不是欄位標題。
#:
#: 這一條是為了擋住「這個檔案根本沒有標題列」的情況。沒有它，第一列資料
#: （「東台精機股份有限公司」）會因為含有「公司」兩個字而被當成標題升上去，
#: 於是那一家公司**安靜地消失**，而畫面上寫著匯入成功。少一筆比報錯糟得多。
_CORPORATE_SUFFIXES = (
    "股份有限公司", "有限公司", "股份公司", "企業社", "工作室", "實業社",
    "商行", "企業行", "合夥", "商號", "co., ltd", "co.,ltd", "corporation", "inc.",
)

#: 標題再長就不是標題了，是一句話或一筆資料。
MAX_HEADER_CHARS = 20


def _looks_like_a_company_name(header: object) -> bool:
    """這個標題看起來是公司名稱那一欄嗎？（精確比對失敗之後才問。）"""
    text = str(header).strip().lower()
    if not text or text.startswith("unnamed:") or len(text) > MAX_HEADER_CHARS:
        return False
    if any(text.endswith(suffix) for suffix in _CORPORATE_SUFFIXES):
        return False        # 這是一家公司的名字，不是欄位標題
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

    # 標題裡有「英文」的，是英文名稱那一欄，不是公司名稱。
    #
    # 這一條要排在逐字比對前面。「Company Name (English)」拆開之後第一個
    # 認得的字是 company，於是它會搶走公司名稱那一欄——實測踩過：中英混排的
    # 名冊匯進來，公司名稱全部變成英文簡稱，中文全名反而被丟進自由欄位。
    if "英文" in text or "english" in text:
        return "english_name"

    # Exported headers look like "公司名稱 Company"; try each token.
    for token in text.replace("/", " ").replace("_", " ").split():
        canonical = _COLUMN_ALIASES.get(token)
        # 逐字比對很容易過頭：「負責人 name」裡的 name 會讓整欄變成公司名稱。
        # 對到公司名稱時多問一句「這個標題整體看起來像嗎」。
        if canonical == "company_name" and not _looks_like_a_company_name(text):
            continue
        if canonical:
            return canonical

    compact = text.replace(" ", "")
    canonical = _COLUMN_ALIASES.get(compact)
    if canonical == "company_name" and not _looks_like_a_company_name(text):
        return None
    return canonical


#: 找標題列時最多往下看幾列。
#:
#: 名冊上方的裝飾（大標題、製表日期、空白列、合併儲存格）很少超過十幾列，
#: 而看太多列會開始把**資料列**誤判成標題——資料列裡本來就會出現「公司名稱」
#: 那幾個字（例如備註欄寫著「公司名稱已變更」）。
MAX_HEADER_SCAN_ROWS = 15


def _header_score(cells: Iterable[object]) -> tuple[int, bool]:
    """這一列有多像標題列。回傳 ``(認得幾欄, 有沒有公司名稱那一欄)``。"""
    named = 0
    has_company = False
    for cell in cells:
        text = str(cell).strip()
        if not text or text.lower() in ("nan", "none") or text.lower().startswith("unnamed:"):
            continue
        canonical = _canonical(text)
        if canonical:
            named += 1
            has_company = has_company or canonical == "company_name"
        elif _looks_like_a_company_name(text):
            named += 1
            has_company = True
    return named, has_company


def _find_header_row(frame: pd.DataFrame) -> tuple[int, int, bool]:
    """哪一列才是標題列。回傳 ``(列號, 認得幾欄, 有沒有公司名稱)``。

    ## 為什麼需要這個

    「隨便一個 Excel」失敗最常見的原因不是欄位名稱，是**標題列不在第一行**。
    公協會與政府網站給的名冊上面幾乎一定有東西：一整列的大標題、製表日期、
    空白列、合併儲存格的說明。pandas 預設把第一列當欄名，於是欄名變成
    「台灣機械工業同業公會會員名冊」和一串 ``Unnamed: 1``，接著就報「找不到
    公司名稱欄」——而使用者打開檔案看，明明第 4 列就寫著「公司名稱」。

    做法是往下找最像標題的那一列：認得的欄位最多、而且**含有公司名稱那一欄**
    的優先。同分取最上面那一列（標題在資料上面，不會在下面）。
    """
    best = (0, 0, False)
    for index in range(min(MAX_HEADER_SCAN_ROWS, len(frame))):
        named, has_company = _header_score(frame.iloc[index].tolist())
        # 排序鍵：有公司名稱 > 認得的欄位多 > 靠上面。
        if (has_company, named) > (best[2], best[1]):
            best = (index, named, has_company)
    return best


def _promote_header(frame: pd.DataFrame, row: int) -> pd.DataFrame:
    """把第 ``row`` 列升成欄名，丟掉它上面的裝飾列。"""
    header = [str(cell).strip() for cell in frame.iloc[row].tolist()]
    body = frame.iloc[row + 1:].copy()
    body.columns = header
    # 整欄空白（合併儲存格留下來的）與整列空白（分隔用的空行）都不是資料。
    body = body.dropna(axis="columns", how="all").dropna(axis="index", how="all")
    return body.reset_index(drop=True)


def _best_sheet(sheets: dict[object, pd.DataFrame]) -> tuple[str, pd.DataFrame, int]:
    """挑出最像名單的那一張工作表，並把它的標題列升好。

    活頁簿常常不只一張表：「說明」「範例」「工作表1」跟真正的名冊放在一起，
    而真正的名冊不一定是第一張。挑的依據跟找標題列一樣——有公司名稱那一欄的
    優先，其次是認得的欄位多。
    """
    best_name, best_frame, best_row = "", None, 0
    best_key = (False, -1, -1)
    for name, raw in sheets.items():
        row, named, has_company = _find_header_row(raw)
        key = (has_company, named, len(raw))
        if key > best_key:
            best_key = key
            best_name, best_frame, best_row = str(name), raw, row
    if best_frame is None:                       # 空活頁簿
        return "", pd.DataFrame(), 0
    return best_name, _promote_header(best_frame, best_row), best_row


def _read_delimited(source: Path, encoding: str) -> pd.DataFrame:
    """讀一個 CSV／TSV，欄數不一致也照樣讀得進來。

    不用 ``pd.read_csv``，因為它**用第一行決定整個檔案有幾欄**。名冊上面壓著
    一行大標題時那一行只有一格，於是 pandas 認定這個檔案只有一欄，後面每一
    列都變成「壞掉的行」——不是報錯就是被 ``on_bad_lines="skip"`` 整批丟掉，
    最後拿到一張空表。而那正是我們要處理的檔案。

    分隔符號讓 :class:`csv.Sniffer` 判斷（同一批名冊裡逗號與 tab 都有），
    認不出來就用逗號。每一列補到最長的那一列的寬度，補出來的空格由
    :func:`_promote_header` 一併清掉。
    """
    text = source.read_text(encoding=encoding)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel                       # 認不出來就當一般 CSV
    rows = list(csv.reader(io.StringIO(text), dialect))
    width = max((len(row) for row in rows), default=0)
    if not width:
        return pd.DataFrame()
    padded = [row + [None] * (width - len(row)) for row in rows]
    return pd.DataFrame(padded, dtype=object)


def read_table(path: str | Path) -> pd.DataFrame:
    """Load a CSV, Excel or JSON file into a DataFrame."""
    source = Path(path).expanduser()
    if not source.exists():
        raise ExportError(f"import file not found: {source}")

    suffix = source.suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm", ".xls"):
            # ``header=None`` 而不是讓 pandas 猜：真正的標題列常常不在第一行
            # （上面壓著大標題、製表日期、空白列），而 ``sheet_name=None``
            # 是因為名冊不一定在第一張工作表。挑法見 :func:`_best_sheet`。
            sheets = pd.read_excel(source, sheet_name=None, header=None, dtype=str)
            name, frame, row = _best_sheet(sheets)
            if len(sheets) > 1 or row:
                log.info(
                    "{}：用工作表「{}」的第 {} 列當標題",
                    source.name, name, row + 1,
                )
            return frame
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
                    raw = _read_delimited(source, encoding)
                except UnicodeDecodeError:
                    continue
                row, _, _ = _find_header_row(raw)
                if row:
                    log.info("{}：用第 {} 列當標題", source.name, row + 1)
                return _promote_header(raw, row)
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
        headers = [str(c).strip() for c in frame.columns if str(c).strip()]
        found = "、".join(headers) or "（這個檔案沒有標題列）"

        # 欄名本身看起來就是資料的話，問題不是「名稱沒對上」，是這個檔案
        # 根本沒有標題列——那要講的話完全不一樣，不然使用者會一直去改一個
        # 不存在的標題。
        looks_like_data = any(
            any(str(h).lower().endswith(s) for s in _CORPORATE_SUFFIXES) for h in headers
        )
        if looks_like_data:
            raise ExportError(
                "這個檔案好像沒有標題列——最上面那一列看起來已經是資料了：\n"
                f"　{found}\n"
                "請在最上面插入一列，把每一欄的名字寫上去"
                "（至少要有一欄叫「公司名稱」），再匯入一次。"
            )

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
