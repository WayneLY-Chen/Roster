"""名單品質分數：這一筆現在值不值得花時間。

一份爬回來的名單裡，每一列的「可用程度」差很多：有的只有公司名稱，有的信箱
電話地址齊全。使用者要做的第一件事一定是「先聯絡比較有機會的那些」，而那件
事目前只能靠一列一列看。這支模組把那個判斷寫成一個 0 到 100 的分數，讓表格
能直接照它排。

分數只有一個用途：**排序**。它不代表這家公司好不好、值不值得往來——那是使用
者的判斷，程式不該替他做。它代表的是「這一筆資料完整到什麼程度、能不能真的
聯絡得上、還新不新」。

刻意是**算出來的、不存進資料庫**。裡面有一項是「資料多新」，那是隨時間變動
的東西；存成欄位的話今天算出來的分數明天就不對了，而且沒有任何地方會知道它
不對。每次查詢重算一遍的成本是幾百筆的字串檢查，量不出來。

配分（滿分 100）：

    信箱      40   驗證過 40／沒驗過 28／驗證不通過 5
    電話      20
    資本額    15   要先做過「補公司登記資料」才會有
    資料新    10
    網站       5
    聯絡人     5
    地址       3
    統編       2

兩個一票否決：登記狀態已經是解散／撤銷／廢止的公司，還有標記為「不再聯絡」
的，一律壓到最低——資料再完整也不該排在前面。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.constants import EmailVerdict

#: ``CompanyFilter.order_by`` 用這個值表示「照名單品質排」。
#:
#: 它不是資料庫欄位，所以 SQL 排不了，由 repository 特別處理。
LEAD_SCORE_ORDER = "lead_score"

#: 介面上的名稱。只寫在這裡一份，畫面與匯出檔共用。
LEAD_SCORE_LABEL = "名單品質"

#: 登記狀態裡出現這些字，代表這家公司已經不在了。
#:
#: 用「包含」而不是「等於」：商業司回的是「核准設立」「解散」「撤銷」「廢止」
#: 「命令解散」「廢止許可」等等，同一個意思有好幾種寫法。
_DEAD_STATUS_WORDS = ("解散", "撤銷", "廢止", "註銷", "清算", "停業")

#: 一票否決之後最高只能得幾分。留一點分數而不是歸零，是為了讓這些筆之間仍然
#: 分得出前後（同樣都關門了，資料完整的那筆至少還查得到是誰）。
VETO_CEILING = 10

_EMAIL_POINTS = {
    EmailVerdict.VALID.value: 40,
    EmailVerdict.UNKNOWN.value: 28,
    EmailVerdict.EMPTY.value: 0,
    EmailVerdict.INVALID_SYNTAX.value: 5,
    EmailVerdict.DISPOSABLE.value: 5,
    EmailVerdict.NO_MX.value: 5,
}


def _text(company: Any, field: str) -> str:
    value = getattr(company, field, None)
    return str(value).strip() if value else ""


def _email_points(company: Any) -> int:
    if not _text(company, "email"):
        return 0
    verdict = _text(company, "email_verdict") or EmailVerdict.UNKNOWN.value
    return _EMAIL_POINTS.get(verdict, _EMAIL_POINTS[EmailVerdict.UNKNOWN.value])


def _capital_points(company: Any) -> int:
    """資本額的級距分數。

    用級距而不是照金額線性換算：資本額的分布橫跨好幾個數量級（一家一百萬、
    一家兩千八百億），線性換算的結果是除了台積電以外每一家都是 0 分。級距
    要回答的問題是「這是小行號、中小企業，還是有規模的公司」。
    """
    amount = getattr(company, "capital_amount", None)
    try:
        amount = int(amount) if amount is not None else 0
    except (TypeError, ValueError):
        return 0
    if amount >= 100_000_000:      # 一億以上
        return 15
    if amount >= 10_000_000:       # 一千萬以上
        return 12
    if amount >= 1_000_000:        # 一百萬以上
        return 8
    if amount > 0:
        return 4
    return 0


def _freshness_points(company: Any, now: datetime) -> int:
    """資料多新。

    看 ``updated_at``：使用者自己編輯過、或重爬時更新過的那一刻。沒有的話退回
    ``created_at``。兩個都沒有（手動塞進來的測試資料）就不給分，不當成很新。
    """
    stamp = getattr(company, "updated_at", None) or getattr(company, "created_at", None)
    if not isinstance(stamp, datetime):
        return 0
    days = (now - stamp).days
    if days <= 30:
        return 10
    if days <= 90:
        return 7
    if days <= 365:
        return 4
    return 0


def _is_defunct(company: Any) -> bool:
    status = _text(company, "registration_status")
    return any(word in status for word in _DEAD_STATUS_WORDS)


def explain(company: Any, now: datetime | None = None) -> list[tuple[str, int]]:
    """分數的組成，照配分由大到小。給詳細資料視窗顯示用。

    分開成兩個函式而不是讓 :func:`lead_score` 回傳明細：排序時每一筆都會呼叫
    一次，那條路上不需要也不該建一堆字串。
    """
    now = now or datetime.now()
    items = [
        ("信箱", _email_points(company)),
        ("電話", 20 if _text(company, "phone") else 0),
        ("資本額", _capital_points(company)),
        ("資料新舊", _freshness_points(company, now)),
        ("網站", 5 if _text(company, "website") else 0),
        ("聯絡人", 5 if _text(company, "contact_person") else 0),
        ("地址", 3 if _text(company, "address") else 0),
        ("統一編號", 2 if _text(company, "tax_id") else 0),
    ]
    if getattr(company, "do_not_contact", False):
        items.append(("已標記不再聯絡", -sum(v for _, v in items) + VETO_CEILING))
    elif _is_defunct(company):
        status = _text(company, "registration_status")
        items.append((f"登記狀態：{status}", -sum(v for _, v in items) + VETO_CEILING))
    return items


def lead_score(company: Any, now: datetime | None = None) -> int:
    """這一筆的名單品質，0 到 100。

    ``company`` 可以是 ORM 的 Company、也可以是 CompanyView——只用 getattr 讀
    欄位，兩邊都有的欄位就算得出來，缺的當作沒有。
    """
    now = now or datetime.now()
    total = (
        _email_points(company)
        + (20 if _text(company, "phone") else 0)
        + _capital_points(company)
        + _freshness_points(company, now)
        + (5 if _text(company, "website") else 0)
        + (5 if _text(company, "contact_person") else 0)
        + (3 if _text(company, "address") else 0)
        + (2 if _text(company, "tax_id") else 0)
    )
    if getattr(company, "do_not_contact", False) or _is_defunct(company):
        return min(total, VETO_CEILING)
    return max(0, min(100, total))
