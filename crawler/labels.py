"""把「標籤︰值」排版的名錄解析成欄位。

台灣的公協會名錄有一大類長這樣——一個區塊裡是一行一項：

    負責人︰ 董事長 林寬仁
    會員代表︰ 總經理 林伯洲
    地址︰ 台北市大安區忠孝東路四段132號十一樓
    Tel︰ 02-27115788      FAX︰ 02-27728848
    註冊編號︰ 0006        統一編號︰ 11022852
    營業範圍︰ 甲種旅行業

這些頁面多半是 2000 年代的 ASP／PHP 產物：整頁只有 ``<table>``、``<font>``
與 ``<br>``，沒有任何 class、沒有標題標籤。**沒有任何 CSS 選擇器指得到
「負責人」那一欄**——選擇器能指的只有「第三個 td 裡的第二個 font」，那種規則
換一頁順序變了就全錯。唯一穩定的線索是文字本身：標籤就寫在值的前面。

這個模組因此不看標籤，只看文字。它做兩件事：

* 認得出來的標籤（電話、傳真、統編……）填進對應的欄位；
* **認不出來的標籤原樣留著**。這一點是重點——每個名錄自己有什麼欄位是那個
  公會決定的，不是我們能事先列完的。旅行公會有「會員代表」「入會年月日」，
  化工公會有「代理廠商及代銷產品」，硬要塞進固定欄位不是漏掉就是塞錯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 全形冒號 U+FF1A、名錄最愛用的 U+FE30（︰），以及半形冒號。
# 半形冒號必須後接空白才算——否則 ``http://`` 的冒號、英文譯名裡的
# ``Chairman:Mr.Lin`` 都會被當成新標籤，把值攔腰切斷。
_SEPARATOR = r"(?:[：︰]|:(?=[ \t\r\n]|$))"
#: 標籤本身不含空白與分隔符號，長度有限——「負責人」「入會年月日」
#: 「代理廠商及代銷產品」都在範圍內，而一整句話不會。
_LABEL = r"[^\s：︰:]{1,24}"
_PAIR_RE = re.compile(rf"(?:(?<=\s)|^)({_LABEL})\s?{_SEPARATOR}")

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN_RE = re.compile(r"[A-Za-z]")

#: 一筆紀錄至少要有這麼多組「標籤︰值」才算是這種排版。一組可能只是內文裡
#: 剛好出現一個冒號。
MIN_PAIRS = 2

#: 值再長就不是欄位了，是把整段內文吃進來。
MAX_VALUE_CHARS = 500


def _normalise(label: str) -> str:
    """標籤的比對形式：去掉空白與裝飾符號、英文轉小寫。"""
    cleaned = re.sub(r"[\s　*※•·・\-_()（）\[\]【】]", "", label)
    return cleaned.lower()


#: 標籤 → 欄位名稱。key 是 :func:`_normalise` 之後的形式。
#:
#: 這張表是「常見的講法」，不是「全部的講法」——認不出來的標籤會原樣保留成
#: 自由欄位，所以漏列一個講法的代價只是它不會自動填進對應欄位，不是資料不見。
_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": (
        "公司名稱", "廠商名稱", "會員名稱", "商號名稱", "企業名稱", "公司行號",
        "單位名稱", "中文名稱", "名稱", "companyname", "company", "name",
    ),
    "english_name": (
        "英文名稱", "英文公司名稱", "公司英文名稱", "英文", "englishname", "english",
    ),
    "tax_id": (
        "統一編號", "統編", "公司統編", "營利事業統一編號", "稅籍編號",
        "taxid", "vatno", "vat",
    ),
    "email": (
        "email", "電子郵件", "電子信箱", "電子郵箱", "信箱", "電郵", "郵件", "mail",
    ),
    "phone": (
        "電話", "聯絡電話", "公司電話", "電話號碼", "服務電話", "tel", "telephone",
        "phone", "telno",
    ),
    "fax": ("傳真", "傳真號碼", "公司傳真", "fax", "faxno"),
    "website": (
        "網址", "公司網址", "網站", "官網", "公司網站", "website", "url", "web",
        "homepage",
    ),
    "address": (
        "地址", "公司地址", "通訊地址", "營業地址", "聯絡地址", "廠址", "住址",
        "address",
    ),
    "industry": (
        "營業範圍", "行業別", "產業別", "業別", "所屬產業", "所屬行業", "行業",
        "產業類別", "industry",
    ),
    "products": (
        "主要產品", "產品項目", "營業項目", "經營項目", "主要業務", "產品類別",
        "代理廠商及代銷產品", "代理品牌", "代銷產品", "products", "product",
    ),
    "contact_person": (
        "負責人", "負責人姓名", "代表人", "聯絡人", "董事長", "業務聯絡人",
        "聯絡窗口", "窗口", "contactperson", "contact",
    ),
}

#: 反查表：正規化後的標籤 → 欄位名稱。
FIELD_BY_LABEL: dict[str, str] = {
    _normalise(alias): field_name
    for field_name, aliases in _ALIASES.items()
    for alias in aliases
}

#: 這些欄位的值常常是「職稱 中文姓名 English Name」並列。英文譯名對寄信、
#: 稱呼都沒有用，留著只會讓聯絡人欄變成一整行字，所以只取中文那一段。
_PERSON_FIELDS = frozenset({"contact_person"})
_PERSON_LABELS = frozenset({"會員代表", "代表人", "負責人", "董事長", "總經理"})

#: 一望即知不是欄位標籤的字。這些是版面上的導覽與說明文字，剛好也用冒號。
#:
#: 不需要在這裡列 ``http`` 或 ``mailto``：半形冒號必須後接空白才算分隔符號，
#: 而 ``http://`` 與 ``mailto:someone@`` 的冒號後面都不是空白。列進來反而會
#: 誤傷——``tel`` 曾經在這張表裡，結果是所有寫「Tel︰」的名錄都抓不到電話。
_LABEL_STOPWORDS = frozenset({
    "註", "備註說明", "說明", "提醒", "注意", "警告", "查詢", "搜尋",
    "note", "warning", "error",
})


@dataclass(slots=True)
class LabelledRecord:
    """一筆紀錄被拆成的三塊。"""

    #: 第一個標籤之前的文字。這種版面幾乎一定把公司名稱放在最前面。
    heading: str = ""
    #: 對應得到欄位的部分。
    fields: dict[str, str] = field(default_factory=dict)
    #: 對應不到欄位的部分，保留原本的標籤文字當 key。
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def pair_count(self) -> int:
        return len(self.fields) + len(self.extra)


def split_cjk_english(text: str) -> tuple[str, str]:
    """把「中文… English…」拆成兩段。

    切點是最後一個含中文的詞之後。用在兩個地方：公司名稱與英文名稱並列的
    標題列，以及「董事長 林寬仁 Chairman:Mr.Lin Kuan Jen」這種姓名欄。

    整段都沒有中文、或中文在最後時原樣回傳，第二段是空字串。
    """
    tokens = text.split()
    last_cjk = -1
    for index, token in enumerate(tokens):
        if _CJK_RE.search(token):
            last_cjk = index
    if last_cjk < 0 or last_cjk == len(tokens) - 1:
        return text.strip(), ""

    tail = " ".join(tokens[last_cjk + 1 :])
    if not _LATIN_RE.search(tail):
        return text.strip(), ""
    return " ".join(tokens[: last_cjk + 1]), tail


def _clean_value(value: str) -> str:
    """把版面造成的換行與連續空白壓成單一空白。"""
    return re.sub(r"\s+", " ", value).strip(" 　\t:：︰、,，;；")


def _is_usable_label(label: str) -> bool:
    normalised = _normalise(label)
    if not normalised or len(normalised) > 20:
        return False
    if normalised in _LABEL_STOPWORDS:
        return False
    # 純數字、純標點不是標籤（「2024:」這種時間戳、比分都會誤中）。
    return bool(_CJK_RE.search(normalised) or _LATIN_RE.search(normalised))


def parse_record(text: str) -> LabelledRecord:
    """把一筆紀錄的純文字拆成標題、已知欄位與自由欄位。

    值的範圍是「這個標籤之後、下一個標籤之前」。同一個標籤重複出現時以第一次
    為準——名錄的頁尾常常再列一次聯絡方式，那是公會自己的電話，不是廠商的。
    """
    record = LabelledRecord()
    matches = list(_PAIR_RE.finditer(text))
    if not matches:
        record.heading = _clean_value(text)
        return record

    record.heading = _clean_value(text[: matches[0].start()])

    for index, match in enumerate(matches):
        label = match.group(1)
        if not _is_usable_label(label):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = _clean_value(text[match.end() : end])
        if not value or len(value) > MAX_VALUE_CHARS:
            continue

        field_name = FIELD_BY_LABEL.get(_normalise(label))
        if field_name in _PERSON_FIELDS or label in _PERSON_LABELS:
            chinese, _english = split_cjk_english(value)
            value = chinese or value

        # 對應得到欄位、而且那個欄位還空著 → 填進去。
        # 已經有值的話保留原本的標籤存成自由欄位：名錄常常中英文各列一次
        # （地址︰／Address︰），兩個都收才不會有一個被蓋掉。
        if field_name and field_name not in record.fields:
            record.fields[field_name] = value
        elif label not in record.extra:
            record.extra[label] = value

    return record
