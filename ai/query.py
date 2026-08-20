"""用中文問自己的資料庫。

## 給工具，不要給 SQL

模型的工作只有一件：把「哪些台中的公司還沒聯絡過」翻成**一組條件**
（``city="台中"``、``never_emailed=True``）。查資料是
:class:`~database.repository.CompanyRepository` 做的，那一層已經被整套測試看
了很久，而且它只會 SELECT。

自然語言直接串成 SQL 這條路這裡完全沒有走——沒有任何一個地方接受模型寫的
SQL 片段，所以也沒有什麼要防的。條件名稱走白名單（:data:`PARAMS`），不認得的
一律丟掉並且**列給使用者看**。

## 兩條不能妥協的

**一、唯讀。** 這個模組只 import
:meth:`~database.repository.CompanyRepository.search` 與
:meth:`~database.repository.CompanyRepository.count`。新增、修改、刪除的工具
**不存在**——所以「叫它刪資料」的答案不是「它拒絕」，是它做不到。

**二、答案要附依據。** 「有 12 家」這種答案沒有辦法判斷是查出來的還是編出來
的。所以：

* 數字由 :func:`run` 從資料庫算出來，**模型碰不到它**。它只挑條件。
* 用了哪些條件會照實印出來（:meth:`Answer.criteria_text`）。
* 符合的公司直接列出來，點得開。

模型連「有幾家」那個數字都不經手，是這裡最重要的設計：一個編出來的數字看起來
跟真的一模一樣，使用者沒有任何辦法分辨。乾脆不給它機會寫。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ai.extract import ChatFn, parse_reply
from ai.prompts import QUERY_SYSTEM_PROMPT
from ai.provider import ChatMessage
from core.constants import LogCategory, PipelineStage
from core.errors import AIError
from core.logging_setup import get_logger
from core.schemas import CompanyFilter, CompanyView

log = get_logger(LogCategory.GUI)

#: 唯一一個查詢工具的名字。
FIND_TOOL = "find_companies"
#: 模型說「這個資料庫回答不了」時用的。
CANNOT_TOOL = "cannot_answer"

#: 一次最多列幾家。
#:
#: 這是**顯示**上限，不是查詢上限——「有幾家」那個數字永遠是完整的。列太多
#: 只會讓答案變成一面牆，而使用者要細看時本來就該去「公司資訊」頁篩。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: 問答用的 token 上限。回一個小 JSON 而已，不需要多。
QUERY_MAX_TOKENS = 1024


@dataclass(frozen=True, slots=True)
class Param:
    """一個模型可以填的條件。

    ``description`` 會原樣進到 system prompt 裡——參數清單只該有一份，寫兩份
    的話改了程式碼忘記改 prompt，模型會一直填一個早就不存在的參數。
    """

    name: str
    kind: str          # "text" | "bool" | "int" | "choice"
    description: str
    choices: tuple[str, ...] = ()

    def describe(self) -> str:
        kinds = {"text": "文字", "bool": "true／false", "int": "整數", "choice": "字串"}
        line = f"  {self.name}（{kinds.get(self.kind, self.kind)}）：{self.description}"
        if self.choices:
            line += "\n    只能是：" + "、".join(self.choices)
        return line


#: 模型可以填的每一個條件。這就是白名單本身。
#:
#: 刻意都是「篩選」而不是「欄位」：讓模型指定要看哪幾欄的話，它會開始挑它覺得
#: 重要的欄位，而使用者要的是同一張表。
PARAMS: tuple[Param, ...] = (
    Param("keyword", "text", "在公司名稱、信箱、電話、網站、地址、產業、聯絡人、備註裡找這個字"),
    Param("city", "text", "縣市或地區，比對地址。例如「台中」「新北」「南屯區」"),
    Param("industry", "text", "產業，例如「金屬加工」"),
    Param(
        "stage",
        "choice",
        "業務階段。這是使用者手動標的，很多名單會整批停在 New",
        tuple(PipelineStage.values()),
    ),
    Param("tag", "text", "標籤名稱"),
    Param("has_email", "bool", "有沒有信箱。true = 有，false = 沒有"),
    Param(
        "never_emailed",
        "bool",
        "true = 從來沒有寄過信給它（也就是「還沒聯絡過」）；false = 寄過至少一次",
    ),
    Param("created_within_days", "int", "最近幾天內收集進來的。「最近一個月」就填 30"),
    Param("follow_up_due", "bool", "true = 追蹤日期已經到了或過了"),
    Param(
        "order_by",
        "choice",
        "排序依據。名單品質分數是程式算的，問「最值得聯絡的」用它",
        ("lead_score", "updated_at", "created_at", "company_name"),
    ),
    Param("limit", "int", f"最多列幾家，預設 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}"),
)

_BY_NAME = {param.name: param for param in PARAMS}

#: 顯示用的中文標籤。答案裡的「依據」那一行靠它講人話。
_LABELS: dict[str, str] = {
    "keyword": "關鍵字",
    "city": "地址包含",
    "industry": "產業",
    "stage": "業務階段",
    "tag": "標籤",
    "has_email": "有信箱",
    "never_emailed": "還沒聯絡過",
    "created_within_days": "最近幾天收集的",
    "follow_up_due": "追蹤日期已到",
    "order_by": "排序",
    "limit": "最多列",
}

_ORDER_LABELS = {
    "lead_score": "名單品質分數",
    "updated_at": "最後更新時間",
    "created_at": "收集時間",
    "company_name": "公司名稱",
}


@dataclass(frozen=True, slots=True)
class Query:
    """模型挑好、而且已經過白名單的一組條件。"""

    arguments: dict[str, object] = field(default_factory=dict)
    #: ``count`` 或 ``list``。
    mode: str = "list"
    #: 模型寫了但不認得的條件名稱。列給使用者看——它是「這個模型在亂填」的證據。
    ignored: tuple[str, ...] = ()

    def criteria_text(self) -> str:
        """「依據」那一行。空條件時講明是整個資料庫，不要留白。"""
        if not self.arguments:
            return "沒有加任何條件（整個名單）"
        parts = []
        for name, value in self.arguments.items():
            label = _LABELS.get(name, name)
            if name == "order_by":
                parts.append(f"{label} = {_ORDER_LABELS.get(str(value), value)}")
            elif isinstance(value, bool):
                parts.append(label if value else f"不是「{label}」")
            else:
                parts.append(f"{label} = {value}")
        return "、".join(parts)


@dataclass(slots=True)
class Answer:
    """一次問答的完整結果，含所有「可以自己驗證」需要的東西。"""

    question: str = ""
    query: Query | None = None
    #: 符合條件的總數。這是**完整**的數字，不受 ``limit`` 影響。
    total: int = 0
    #: 列出來的那幾家（最多 ``limit`` 家）。
    companies: list[CompanyView] = field(default_factory=list)
    #: 模型說答不出來的理由。有值時上面那幾個都是空的。
    cannot: str = ""

    @property
    def answered(self) -> bool:
        return self.query is not None

    def headline(self) -> str:
        """答案的第一句。由程式寫，不是模型寫的。"""
        if self.cannot:
            return f"這個問題我答不出來：{self.cannot}"
        if self.total == 0:
            return "符合條件的一家都沒有。"
        if self.query is not None and self.query.mode == "count":
            return f"有 {self.total} 家。"
        shown = len(self.companies)
        if shown < self.total:
            return f"有 {self.total} 家，下面列出前 {shown} 家。"
        return f"有 {self.total} 家，全部列在下面。"

    def notes(self) -> list[str]:
        """畫面上要照實寫出來的每一句話。"""
        lines: list[str] = []
        if self.query is None:
            return lines
        lines.append(f"依據：{self.query.criteria_text()}")
        if self.query.ignored:
            lines.append(
                "模型填了 " + "、".join(self.query.ignored) + " 這些查不了的條件，"
                "已經忽略——這個數字大就代表它在亂填，換一個模型會比較準。"
            )
        return lines


# ----------------------------------------------------------------- 跟模型講話


def parameter_help() -> str:
    """給 system prompt 用的參數說明。從 :data:`PARAMS` 長出來。"""
    return "\n".join(param.describe() for param in PARAMS)


def build_messages(question: str, *, today: date | None = None) -> list[ChatMessage]:
    """組出問答用的訊息串。

    今天的日期要明講：模型不知道今天幾號，而「最近一個月」「這一季」全都要靠
    它。不講的話它會用訓練資料裡的某一天去算，而那個誤差沒有任何提示。
    """
    stamp = (today or date.today()).isoformat()
    return [
        ChatMessage("system", QUERY_SYSTEM_PROMPT.format(parameters=parameter_help())),
        ChatMessage("user", f"今天是 {stamp}。\n\n使用者的問題：{question}"),
    ]


def parse_call(reply: str) -> Query | str:
    """把模型的回覆變成一組條件，或是它說答不出來的理由（字串）。

    重用 :func:`ai.extract.parse_reply` 的容錯（markdown 圍欄、前後客套話），
    那是同一類問題：格式不對不該讓使用者再等一次、再付一次錢。
    """
    items = parse_reply(reply)
    if not items:
        raise AIError("模型沒有回傳看得懂的查詢條件，這個問題問不出結果。")

    call = items[0]
    tool = str(call.get("tool") or "").strip()

    if tool == CANNOT_TOOL:
        reason = str(call.get("reason") or "").strip()
        return reason or "模型沒有說為什麼。"

    if tool and tool != FIND_TOOL:
        # 模型自己發明了一個工具——最危險的那一種是 delete_companies。
        # 這裡不是「拒絕執行」，是根本沒有那個東西可以執行。
        log.warning("模型要求了一個不存在的工具：{!r}", tool)
        raise AIError(
            f"模型想用一個不存在的工具「{tool}」。這一版的助手只查得了資料，"
            "不能新增、修改或刪除——那些請到「公司資訊」頁去做。"
        )

    raw = call.get("arguments")
    if not isinstance(raw, dict):
        raw = {key: value for key, value in call.items() if key not in ("tool", "mode")}

    arguments: dict[str, object] = {}
    ignored: list[str] = []
    for name, value in raw.items():
        param = _BY_NAME.get(str(name).strip())
        if param is None:
            ignored.append(str(name))
            continue
        cleaned = _coerce(param, value)
        if cleaned is not None:
            arguments[param.name] = cleaned

    mode = str(call.get("mode") or "").strip().lower()
    return Query(
        arguments=arguments,
        mode="count" if mode == "count" else "list",
        ignored=tuple(ignored),
    )


def _coerce(param: Param, value: object) -> object | None:
    """把模型填的一格變成這個參數該有的型別。填不出來就當它沒填。"""
    if value is None or isinstance(value, (dict, list)):
        return None

    if param.kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "是", "有"):
            return True
        if text in ("false", "no", "0", "否", "沒有"):
            return False
        return None

    if param.kind == "int":
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    text = str(value).strip()
    if not text:
        return None
    if param.kind == "choice":
        match = next(
            (choice for choice in param.choices if choice.lower() == text.lower()), None
        )
        return match
    return text


# --------------------------------------------------------------------- 查資料


#: 「台」與「臺」在同一個資料庫裡混著出現是常態。
#:
#: 名錄有的寫「臺中市」有的寫「台中市」，而使用者問的時候只會打其中一種。
#: 比對地址是子字串比對，沒有處理這件事的話「台中」會漏掉所有寫「臺中」的
#: 公司——而漏掉的部分完全看不出來，答案只是安靜地變少。
_TAI = ("台", "臺")


def _city_spellings(city: str) -> list[str]:
    if "台" in city:
        return [city, city.replace("台", "臺")]
    if "臺" in city:
        return [city, city.replace("臺", "台")]
    return [city]


def to_filters(query: Query, *, today: date | None = None) -> list[CompanyFilter]:
    """把條件翻成 :class:`~core.schemas.CompanyFilter`。

    回傳一個 list 而不是單一個，是因為「台／臺」要各查一次再合併——見
    :data:`_TAI`。其餘情況永遠只有一個。
    """
    args = query.arguments
    now = datetime.combine(today or date.today(), datetime.min.time())

    base: dict[str, object] = {}
    if "keyword" in args:
        base["text"] = args["keyword"]
    if "industry" in args:
        base["industry"] = args["industry"]
    if "stage" in args:
        base["stages"] = [str(args["stage"])]
    if "tag" in args:
        base["tags"] = [str(args["tag"])]
    if "has_email" in args:
        base["has_email"] = bool(args["has_email"])
    if "never_emailed" in args:
        base["emailed"] = not bool(args["never_emailed"])
    if "created_within_days" in args:
        base["created_after"] = now - timedelta(days=int(args["created_within_days"]))
    if "follow_up_due" in args and args["follow_up_due"]:
        base["follow_up_before"] = (today or date.today())
    if "order_by" in args:
        base["order_by"] = str(args["order_by"])
        # 名稱由小到大才是使用者預期的「照名稱排」；其餘欄位由新到舊。
        base["descending"] = args["order_by"] != "company_name"

    city = str(args.get("city") or "").strip()
    if not city:
        return [CompanyFilter(**base)]
    return [CompanyFilter(**base, address=spelling) for spelling in _city_spellings(city)]


def run(query: Query, repo, *, today: date | None = None) -> tuple[int, list[CompanyView]]:
    """實際去查。``repo`` 是一個 :class:`~database.repository.CompanyRepository`。

    只呼叫它的 ``search``。**沒有**任何一條路會寫到資料庫——這個模組連
    ``upsert``／``update``／``delete`` 這幾個名字都沒有出現過。
    """
    limit = int(query.arguments.get("limit") or DEFAULT_LIMIT)
    limit = max(1, min(limit, MAX_LIMIT))

    seen: dict[int, CompanyView] = {}
    for criteria in to_filters(query, today=today):
        # 刻意不帶 limit 去查：總數要是完整的，而「台／臺」兩份結果合併之後
        # 才知道真正的數量。桌面資料庫幾千筆，這個代價可以接受。
        for company in repo.search_views(criteria):
            seen.setdefault(company.id, company)

    companies = list(seen.values())
    return len(companies), companies[:limit]


# --------------------------------------------------------------------- 主流程


RunQuery = Callable[[Query], tuple[int, list[CompanyView]]]
"""執行一組條件、回 ``(總數, 這幾家)`` 的函式。

做成參數而不是在這裡開資料庫連線，有一個實際的理由：**模型那一通可能要跑好
幾分鐘**（本機模型第一次還要載權重）。整段包在一個交易裡的話，那幾分鐘 SQLite
一直被佔著，同時間背景在跑的爬取就寫不進去。所以順序刻意是「先問完模型，再開
交易查一次」，而那個交易由呼叫端（:mod:`controllers.ai`）管。
"""


def ask(
    question: str, chat: ChatFn, run_query: RunQuery, *, today: date | None = None
) -> Answer:
    """一個中文問題進去，一個有依據的答案出來。"""
    text = (question or "").strip()
    if not text:
        raise AIError("先打一個問題，例如「哪些台中的公司還沒聯絡過？」")

    call = parse_call(chat(build_messages(text, today=today)))
    if isinstance(call, str):
        return Answer(question=text, cannot=call)

    total, companies = run_query(call)
    log.info("問答「{}」：{} 個條件，{} 筆", text, len(call.arguments), total)
    return Answer(question=text, query=call, total=total, companies=companies)


__all__ = [
    "CANNOT_TOOL",
    "DEFAULT_LIMIT",
    "FIND_TOOL",
    "MAX_LIMIT",
    "PARAMS",
    "QUERY_MAX_TOKENS",
    "Answer",
    "Param",
    "Query",
    "RunQuery",
    "ask",
    "build_messages",
    "parameter_help",
    "parse_call",
    "run",
    "to_filters",
]
